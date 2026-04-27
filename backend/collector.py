import feedparser
import os
import hashlib
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from zoneinfo import ZoneInfo
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from backend.database import get_connection, is_postgres

RSS_FEEDS = {
    "NDTV": "https://feeds.feedburner.com/NDTV-LatestNews",
    "The Hindu": "https://www.thehindu.com/news/feeder/default.rss",
    "Indian Express": "https://indianexpress.com/feed/",
    "Hindustan Times": "https://www.hindustantimes.com/feeds/rss/topstories/rssfeed.xml",
    "Times of India": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "BBC India": "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml",
}

_TRACKING_PARAMS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content", "utm_id",
    "utm_name", "utm_reader", "utm_viz_id", "utm_pubreferrer", "utm_swu",
    "gclid", "fbclid", "igshid", "mc_cid", "mc_eid", "mkt_tok", "ref", "ref_src",
}
IST = ZoneInfo("Asia/Kolkata")
try:
    _feed_limit = int(os.environ.get("MAX_ENTRIES_PER_FEED", "40"))
except ValueError:
    _feed_limit = 40
MAX_ENTRIES_PER_FEED = max(10, min(100, _feed_limit))


def _feed_text(x):
    if x is None:
        return ""
    if isinstance(x, list):
        return " ".join(str(i) for i in x).strip()
    return str(x).strip()


def _canonicalize_url(url: str) -> str:
    u = _feed_text(url)
    if not u:
        return ""
    try:
        parts = urlsplit(u)
    except Exception:
        return u
    scheme = (parts.scheme or "https").lower()
    netloc = (parts.netloc or "").lower()
    path = parts.path or ""
    if path != "/" and path.endswith("/"):
        path = path[:-1]
    q = []
    for k, v in parse_qsl(parts.query or "", keep_blank_values=True):
        kl = (k or "").lower()
        if not kl or kl in _TRACKING_PARAMS or kl.startswith("utm_"):
            continue
        q.append((k, v))
    q.sort(key=lambda kv: (kv[0].lower(), kv[1]))
    return urlunsplit((scheme, netloc, path, urlencode(q, doseq=True), "")).strip()


def _title_hash(title: str) -> str:
    normalized = " ".join(_feed_text(title).lower().split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest() if normalized else ""


def _extract_image_url(entry):
    candidates = []
    
    # --- Existing checks ---
    media_content = entry.get("media_content") or []
    for m in media_content:
        if isinstance(m, dict):
            candidates.append(_feed_text(m.get("url")))
    media_thumb = entry.get("media_thumbnail") or []
    for m in media_thumb:
        if isinstance(m, dict):
            candidates.append(_feed_text(m.get("url")))
    image_obj = entry.get("image")
    if isinstance(image_obj, dict):
        candidates.append(_feed_text(image_obj.get("href")))
    links = entry.get("links") or []
    for l in links:
        if not isinstance(l, dict):
            continue
        ltype = _feed_text(l.get("type")).lower()
        if "image" in ltype:
            candidates.append(_feed_text(l.get("href")))

    # --- NEW: check enclosures (NDTV, ToI, Hindu use this) ---
    enclosures = entry.get("enclosures") or []
    for enc in enclosures:
        if isinstance(enc, dict):
            etype = _feed_text(enc.get("type")).lower()
            if "image" in etype:
                candidates.append(_feed_text(enc.get("href") or enc.get("url")))

    # --- NEW: parse <img> from HTML description/summary ---
    import re as _re
    for field in ("summary", "content", "description"):
        raw = entry.get(field)
        if isinstance(raw, list):
            raw = " ".join(str(x.get("value", x) if isinstance(x, dict) else x) for x in raw)
        raw = str(raw or "")
        if raw:
            imgs = _re.findall(r'<img[^>]+src=["\']([^"\']+)["\']', raw, _re.IGNORECASE)
            candidates.extend(imgs)

    valid_candidates = []
    for c in candidates:
        if not c:
            continue
        lc = c.lower()
        # Avoid common tracking pixels or generic logos
        if any(bad in lc for bad in ["logo", "1x1", ".gif", "pixel", "icon"]):
            continue
        if lc.startswith("http://") or lc.startswith("https://"):
            valid_candidates.append(c)

    if valid_candidates:
        return valid_candidates[0]
    return ""

def _entry_datetime_ist(entry):
    published_parsed = entry.get("published_parsed")
    if not published_parsed:
        return None
    try:
        dt_utc = datetime(
            published_parsed.tm_year,
            published_parsed.tm_mon,
            published_parsed.tm_mday,
            published_parsed.tm_hour,
            published_parsed.tm_min,
            published_parsed.tm_sec,
            tzinfo=timezone.utc,
        )
        return dt_utc.astimezone(IST)
    except Exception:
        return None


def collect_articles():
    conn = get_connection()
    cursor = conn.cursor()
    total = 0

    today_ist = datetime.now(IST).date()
    skipped_old = 0

    for source, url in RSS_FEEDS.items():
        print(f"Fetching from {source}...")
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:MAX_ENTRIES_PER_FEED]:
                entry_dt = _entry_datetime_ist(entry)
                if entry_dt is None or entry_dt.date() != today_ist:
                    skipped_old += 1
                    continue

                title = _feed_text(entry.get("title", ""))
                link = _feed_text(entry.get("link", ""))
                description = _feed_text(entry.get("summary", ""))
                published = entry_dt.isoformat(timespec="seconds")
                image_url = _extract_image_url(entry)
                canonical_link = _canonicalize_url(link)
                title_hash = _title_hash(title)

                cursor.execute(
                    """
                    SELECT 1 FROM articles
                    WHERE link_canonical = ?
                       OR (title_hash = ? AND title_hash <> '')
                    LIMIT 1
                    """,
                    (canonical_link, title_hash),
                )
                if cursor.fetchone():
                    continue

                try:
                    insert_sql = '''
                        INSERT OR IGNORE INTO articles 
                        (title, link, source, description, published, fetched_at, title_hash, link_canonical, image_url)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    '''
                    if is_postgres():
                        insert_sql = '''
                            INSERT INTO articles 
                            (title, link, source, description, published, fetched_at, title_hash, link_canonical, image_url)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                            ON CONFLICT DO NOTHING
                        '''
                    cursor.execute(insert_sql, (title, link, source, description, published, datetime.now(IST).isoformat(timespec="seconds"), title_hash, canonical_link, image_url))
                    if cursor.rowcount > 0:
                        total += 1
                except Exception as e:
                    print(f"DB error: {e}")

        except Exception as e:
            print(f"Error fetching {source}: {e}")

    conn.commit()
    conn.close()
    print(f"Done. {total} new articles saved. Skipped non-today items: {skipped_old}.")
    return total
    
if __name__ == "__main__":
    collect_articles()
