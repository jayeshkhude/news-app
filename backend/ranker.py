"""One batched LLM call per run to order today's stories by importance — cheap fallback if it fails."""
import json
import os
import re
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from groq import Groq

from backend.database import get_connection

MODEL = "llama-3.1-8b-instant"


def _client():
    return Groq(api_key=os.environ.get("GROQ_API_KEY"))


def _llm_content_to_str(content):
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
                    parts.append(str(t))
            else:
                parts.append(str(part))
        return " ".join(parts).strip()
    if isinstance(content, dict):
        t = content.get("text")
        if isinstance(t, str):
            return t
        if t is not None:
            return str(t)
    return str(content)


def _parse_id_list(text):
    text = _llm_content_to_str(text)
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```\w*\n?", "", text)
        text = re.sub(r"\n?```$", "", text)
    m = re.search(r"\[[\s\d,]+\]", text)
    if not m:
        return None
    try:
        arr = json.loads(m.group())
        out = []
        for x in arr:
            try:
                out.append(int(x))
            except (TypeError, ValueError):
                continue
        return out if out else None
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def rank_summaries_for_date(summary_date: str) -> None:
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, headline, topic, summary, category, sources
        FROM summaries
        WHERE summary_date = ?
        ORDER BY id DESC
        LIMIT 18
        """,
        (summary_date,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()

    if not rows:
        return
    if len(rows) == 1:
        conn = get_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE summaries SET importance_score = ? WHERE id = ?",
            (1000, rows[0]["id"]),
        )
        conn.commit()
        conn.close()
        return

    lines = []
    for r in rows:
        hid = r["id"]
        head = (r.get("headline") or r.get("topic") or "")[:90]
        snip = (r.get("summary") or "")[:140].replace("\n", " ")
        cat = r.get("category") or "other"
        lines.append(f"{hid}: [{cat}] {head} | {snip}")
    block = "\n".join(lines)

    prompt = f"""You order news stories by public interest for a general reader (India + world).
Prefer civic impact, safety, economy, and major verified events over gossip or sensation.
Compare what each story is *about*, not similar words.

Return ONLY a JSON array of story IDs from most to least important. Example: [12,5,9]
No explanation. Same IDs as below, each exactly once.

Stories:
{block}"""

    ordered = None
    try:
        resp = _client().chat.completions.create(
            model=MODEL,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=120,
            temperature=0.2,
        )
        ordered = _parse_id_list(resp.choices[0].message.content)
    except Exception as e:
        print(f"Ranker LLM skipped: {e}")

    ids_present = {r["id"] for r in rows}
    if not ordered:
        def _src_count(r):
            try:
                return len(json.loads(r.get("sources") or "[]"))
            except (TypeError, json.JSONDecodeError, ValueError):
                return 0

        rows_sorted = sorted(rows, key=_src_count, reverse=True)
        ordered = [r["id"] for r in rows_sorted]
    else:
        seen = set()
        clean = []
        for i in ordered:
            if i in ids_present and i not in seen:
                clean.append(i)
                seen.add(i)
        for i in ids_present:
            if i not in seen:
                clean.append(i)
        ordered = clean

    conn = get_connection()
    c = conn.cursor()
    base = 1000
    for i, sid in enumerate(ordered):
        c.execute(
            "UPDATE summaries SET importance_score = ? WHERE id = ?",
            (base - i, sid),
        )
    conn.commit()
    conn.close()
    print(f"Ranked {len(ordered)} summaries for {summary_date}")
