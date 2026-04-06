import os
import sys
import json
import re
import hashlib
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from groq import Groq
from datetime import datetime
from zoneinfo import ZoneInfo
IST = ZoneInfo("Asia/Kolkata")
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from backend.database import get_connection
from backend.clusterer import cluster_articles
from backend.prompts import get_cluster_json_prompt

MODEL = "llama-3.1-8b-instant"


def get_client():
    return Groq(api_key=os.environ.get("GROQ_API_KEY"))


def _text_field(v):
    """Coerce JSON/LLM/DB values to a stripped string (never call .strip() on a bare list/dict)."""
    if v is None:
        return ""
    if isinstance(v, bytes):
        return v.decode("utf-8", errors="replace").strip()
    if isinstance(v, str):
        return v.strip()
    if isinstance(v, (list, tuple, set)):
        return " ".join(_text_field(x) for x in v).strip()
    if isinstance(v, dict):
        for key in ("text", "content", "value", "headline", "summary"):
            t = v.get(key)
            if t is None:
                continue
            inner = _text_field(t)
            if inner:
                return inner
        return str(v).strip()
    return str(v).strip()


def _normalize_json_root(data):
    """LLM sometimes returns [{...}] or wraps the object; we need a dict with .get()."""
    if isinstance(data, dict):
        return data
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                return item
    return {}


def _coerce_llm_content(content):
    """Groq/OpenAI-style message.content: str or list of str / {text: ...} parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                t = part.get("text")
                if isinstance(t, str):
                    parts.append(t)
                elif t is not None:
                    parts.append(_text_field(t))
            else:
                parts.append(str(part))
        return " ".join(parts).strip()
    if isinstance(content, dict):
        t = content.get("text")
        if isinstance(t, str):
            return t
        if t is not None:
            return _text_field(t)
        return str(content)
    return str(content)


def _strip_json_fence(s: str) -> str:
    s = _text_field(s)
    if s.startswith("```"):
        s = re.sub(r"^```\w*\n?", "", s)
        s = re.sub(r"\n?```$", "", s)
    return s.strip()

_TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "utm_name",
    "utm_reader",
    "utm_viz_id",
    "utm_pubreferrer",
    "utm_swu",
    "gclid",
    "fbclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "mkt_tok",
    "ref",
    "ref_src",
}


def _canonicalize_url(url: str) -> str:
    u = _text_field(url)
    if not u:
        return ""
    try:
        parts = urlsplit(u)
    except Exception:
        return u.strip()

    scheme = (parts.scheme or "https").lower()
    netloc = (parts.netloc or "").lower()
    path = parts.path or ""
    if path != "/" and path.endswith("/"):
        path = path[:-1]

    q = []
    for k, v in parse_qsl(parts.query or "", keep_blank_values=True):
        if not k:
            continue
        kl = k.lower()
        if kl in _TRACKING_PARAMS or kl.startswith("utm_"):
            continue
        q.append((k, v))
    q.sort(key=lambda kv: (kv[0].lower(), kv[1]))
    query = urlencode(q, doseq=True)

    # drop fragment
    return urlunsplit((scheme, netloc, path, query, "")).strip()


def _canonical_link_items(items):
    """
    Stable JSON for link+source pairs so dedupe works across runs.
    """
    out = []
    for it in (items or []):
        if isinstance(it, dict):
            link = _canonicalize_url(it.get("link"))
            source = _text_field(it.get("source")) or "Source"
        elif isinstance(it, (list, tuple)) and len(it) >= 1:
            link = _canonicalize_url(it[0])
            source = _text_field(it[1]) if len(it) > 1 else "Source"
        else:
            link = _canonicalize_url(it)
            source = "Source"
        if not link:
            continue
        out.append({"link": link, "source": source})

    # sort by link then source for stability
    out.sort(key=lambda d: (d.get("link", ""), d.get("source", "")))
    return out


def _sha256_text(s: str) -> str:
    return hashlib.sha256((_text_field(s) or "").encode("utf-8")).hexdigest()


def _parse_headline_summary(raw, fallback_topic):
    text = _strip_json_fence(raw)
    try:
        data = _normalize_json_root(json.loads(text))
        headline = _text_field(data.get("headline"))
        summary = _text_field(data.get("summary"))
        if headline and summary:
            return headline, summary
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    # brace slice fallback
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            data = _normalize_json_root(json.loads(text[start : end + 1]))
            headline = _text_field(data.get("headline"))
            summary = _text_field(data.get("summary"))
            if headline and summary:
                return headline, summary
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
    ft = _text_field(fallback_topic)
    return (ft[:90] if ft else "Update"), (text or _text_field(raw))

def summarize_cluster(cluster, custom_instruction=None):
    articles = cluster["articles"]
    category = _text_field(cluster.get("category")) or "other"
    lines = []
    for i, a in enumerate(articles, start=1):
        desc = _text_field(a.get("description"))
        title = _text_field(a.get("title"))
        source = _text_field(a.get("source"))
        lines.append(
            f"[{i}] Source: {source}\n"
            f"Title: {title}\n"
            f"Description: {desc}\n"
        )
    articles_text = "\n".join(lines)
    extra = None
    if custom_instruction is not None:
        extra = _text_field(custom_instruction) or None
    prompt = get_cluster_json_prompt(articles_text, category, extra)
    response = get_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=520,
        temperature=0.35,
    )
    raw = _coerce_llm_content(response.choices[0].message.content)
    topic_key = _text_field(cluster.get("topic"))
    headline, summary = _parse_headline_summary(raw, topic_key)
    return _text_field(headline), _text_field(summary)
def run_summarizer(custom_instruction=None):
    clusters = cluster_articles()
    if not clusters:
        print("No clusters to summarize")
        return
    conn = get_connection()
    cursor = conn.cursor()
    today = str(datetime.now(IST).date())
    inserted = 0
    for cluster in clusters:
        topic_label = _text_field(cluster.get("topic"))
        print(f"Summarizing: {topic_label[:50]}...")
        articles = cluster["articles"]
        sources = sorted({_text_field(a.get("source")) for a in articles})
        link_items_raw = [
            {"source": _text_field(a.get("source")), "link": _text_field(a.get("link"))}
            for a in articles
        ]
        link_items = _canonical_link_items(link_items_raw)
        links_key = json.dumps(link_items, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        links_hash = _sha256_text(links_key)

        cursor.execute(
            """
            SELECT 1 FROM summaries
            WHERE summary_date = ? AND (links_hash = ? OR article_links = ?)
            """,
            (today, links_hash, links_key),
        )
        if cursor.fetchone():
            print(f"Skip duplicate links for today: {topic_label[:40]}")
            continue

        try:
            headline, summary = summarize_cluster(cluster, custom_instruction)
            norm_headline = re.sub(r"\s+", " ", _text_field(headline).lower()).strip()
            norm_summary = re.sub(r"\s+", " ", _text_field(summary).lower()).strip()
            content_hash = _sha256_text(norm_headline + "\n" + norm_summary)

            cursor.execute(
                """
                SELECT 1 FROM summaries
                WHERE summary_date = ? AND (content_hash = ? OR links_hash = ?)
                """,
                (today, content_hash, links_hash),
            )
            if cursor.fetchone():
                print(f"Skip duplicate content for today: {_text_field(headline)[:50]}")
                continue

            cursor.execute(
                """
                INSERT INTO summaries (
                    topic, headline, summary, sources, article_links,
                    created_at, summary_date, category, importance_score,
                    links_hash, content_hash
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    topic_label,
                    headline,
                    summary,
                    json.dumps(sources),
                    links_key,
                    str(datetime.now(IST)),
                    today,
                    _text_field(cluster.get("category")) or "other",
                    0,
                    links_hash,
                    content_hash,
                ),
            )
            inserted += 1
            print(f"Saved: {_text_field(headline)[:60]}")
        except Exception as e:
            print(f"Error: {e}")
    conn.commit()
    conn.close()
    print(f"\nSummaries done ({inserted} new row(s))")


if __name__ == "__main__":
    run_summarizer()
