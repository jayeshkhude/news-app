import hashlib
import secrets
import threading
from flask import Flask, jsonify, request, make_response, session
from flask_cors import CORS
from groq import Groq
import json
import os
import sys
import requests as req
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
try:
    from google.oauth2 import id_token
    from google.auth.transport import requests as google_requests
except Exception:
    id_token = None
    google_requests = None
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

def _load_local_env():
    
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    dotenv_path = os.path.join(repo_root, '.env')
    if not os.path.exists(dotenv_path):
        return

    try:
        with open(dotenv_path, "r", encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key = key.strip()
                value = value.strip().strip('"').strip("'")
                if key and key not in os.environ:
                    os.environ[key] = value
    except OSError:
        pass

_load_local_env()

from backend.database import get_connection, init_db, is_postgres
from backend.prompts import get_prompt

app = Flask(__name__, static_folder='../frontend', static_url_path='')

_secret = os.environ.get("FLASK_SECRET_KEY") or os.environ.get("SECRET_KEY")
if not _secret and os.environ.get("ADMIN_TOKEN"):
    _secret = hashlib.sha256(
        (os.environ.get("ADMIN_TOKEN", "") + "newslens-admin-session").encode()
    ).hexdigest()
if not _secret:
    _secret = os.urandom(32).hex()
app.secret_key = _secret
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("SESSION_COOKIE_SECURE", "0").strip().lower() in ("1", "true", "yes")
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

allowed_origins_raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
allowed_origins = [o.strip() for o in allowed_origins_raw.split(",") if o.strip()]
if allowed_origins:
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

init_db()


def _is_local_host(hostname: str | None) -> bool:
    raw = (hostname or "").split(":", 1)[0].strip().lower()
    return raw in {"localhost", "127.0.0.1", "::1"}


def _request_is_https() -> bool:
    forwarded = (request.headers.get("X-Forwarded-Proto") or "").split(",", 1)[0].strip().lower()
    if forwarded:
        return forwarded == "https"
    return bool(request.is_secure)


@app.after_request
def _relax_secure_cookie_for_localhost(resp):
    # Keep secure cookies for deployed HTTPS, but allow localhost testing over http.
    if not app.config.get("SESSION_COOKIE_SECURE"):
        return resp
    if _request_is_https():
        return resp
    if not _is_local_host(request.host):
        return resp

    session_cookie = app.config.get("SESSION_COOKIE_NAME", "session")
    values = resp.headers.getlist("Set-Cookie")
    if not values:
        return resp

    resp.headers.pop("Set-Cookie", None)
    prefix = f"{session_cookie}="
    for value in values:
        if value.startswith(prefix):
            parts = [part for part in value.split("; ") if part.lower() != "secure"]
            resp.headers.add("Set-Cookie", "; ".join(parts))
        else:
            resp.headers.add("Set-Cookie", value)
    return resp


def _row_text(v):
    if v is None:
        return ""
    if isinstance(v, list):
        return " ".join(str(x) for x in v).strip()
    return str(v).strip()


def _coerce_chat_content(content):
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for p in content:
            if isinstance(p, dict):
                t = p.get("text")
                if isinstance(t, str):
                    parts.append(t)
                elif t is not None:
                    parts.append(str(t))
                else:
                    parts.append(str(p))
            else:
                parts.append(str(p))
        return " ".join(parts).strip()
    return str(content)


def get_groq_client():
    return Groq(api_key=os.environ.get("GROQ_API_KEY"))

def _now_iso():
    return datetime.now(IST).isoformat(timespec="seconds")

IST = ZoneInfo("Asia/Kolkata")


def _created_at_iso_utc(value):
    """RFC3339 UTC for JSON. Naive DB timestamps are treated as UTC (Render/Linux default)."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        raw_iso = raw.replace(" ", "T", 1)
        if raw_iso.endswith("Z"):
            dt = datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(raw_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return raw_iso + "Z" if not raw_iso.endswith("Z") else raw_iso


def _dt_iso_utc(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

def _client_key():
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "unknown"


def _session_user():
    u = session.get("user")
    if not isinstance(u, dict):
        return None
    uid = u.get("id")
    try:
        uid = int(uid)
    except (TypeError, ValueError):
        return None
    return {
        "id": uid,
        "email": _row_text(u.get("email")),
        "name": _row_text(u.get("name")),
        "avatar_url": _row_text(u.get("avatar_url")),
    }


def _insert_and_get_id(cursor, query: str, params):
    if is_postgres():
        cursor.execute(query + " RETURNING id", params)
        row = cursor.fetchone()
        return int(row["id"]) if row and row.get("id") is not None else None
    cursor.execute(query, params)
    return cursor.lastrowid


def _with_votes_and_score(rows, user_id=None):
    if not rows:
        return []
    ids = [int(r["id"]) for r in rows]
    conn = get_connection()
    cursor = conn.cursor()
    placeholders = ",".join(["?"] * len(ids))
    cursor.execute(
        f"""
        SELECT summary_id, COALESCE(SUM(vote), 0) AS score
        FROM news_votes
        WHERE summary_id IN ({placeholders})
        GROUP BY summary_id
        """,
        ids,
    )
    score_map = {int(r["summary_id"]): int(r["score"]) for r in cursor.fetchall()}
    my_votes = {}
    if user_id is not None:
        cursor.execute(
            f"""
            SELECT summary_id, vote
            FROM news_votes
            WHERE user_id = ? AND summary_id IN ({placeholders})
            """,
            [user_id] + ids,
        )
        my_votes = {int(r["summary_id"]): int(r["vote"]) for r in cursor.fetchall()}
    conn.close()
    payload = []
    for row in rows:
        sid = int(row["id"])
        item = _summary_public_dict(row)
        item["vote_score"] = score_map.get(sid, 0)
        item["my_vote"] = my_votes.get(sid, 0) if user_id is not None else 0
        payload.append(item)
    return payload

def _require_admin():
    if session.get("admin_ok") is True:
        return None
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not token:
        # Open admin mode fallback for simple/free-tier setups when token is not configured.
        return None
    provided = (request.headers.get("X-Admin-Token") or "").strip()
    if provided and secrets.compare_digest(provided, token):
        return None
    return jsonify({"error": "Unauthorized"}), 401


_PUBLIC_ARTICLE_LINK_CAP = 30


def _summary_public_dict(row):
    headline = _row_text(row["headline"])
    thumb = ""
    try:
        thumb = _row_text(row["thumbnail_url"])
    except Exception:
        thumb = ""
    return {
        "id": row["id"],
        "topic": row["topic"],
        "headline": headline or None,
        "summary": row["summary"],
        "sources": json.loads(row["sources"]),
        "links": _public_links_from_row(row),
        "created_at": _created_at_iso_utc(row["created_at"]),
        "category": _row_text(row["category"]) or None,
        "thumbnail_url": thumb or None,
    }


# Homepage "latest news": only today's summaries (IST), newest first.
LATEST_NEWS_MAX = 20
_LATEST_NEWS_FETCH_CAP = 120


def _parse_created_at_ist(value):
    """Parse DB created_at into an aware datetime in IST (naive strings are treated as IST)."""
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        raw_iso = raw.replace(" ", "T", 1)
        if raw_iso.endswith("Z"):
            dt = datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(raw_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        return dt.astimezone(IST)
    except ValueError:
        return None


def _rows_for_latest_news():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, topic, headline, summary, sources, article_links, created_at, category,
               importance_score, summary_date ,thumbnail_url 
        FROM summaries
        ORDER BY id DESC
        LIMIT ?
        """,
        (_LATEST_NEWS_FETCH_CAP,),
    )
    rows = cursor.fetchall()
    conn.close()
    
    # We no longer strictly filter by today's date so we always have recent news.
    # The ORDER BY id DESC already guarantees we're looking at the latest fetched stories.
    picked = rows

    # Final safety-net: avoid showing identical stories in latest/trending
    # even if the DB already contains duplicates.
    def _norm(s):
        return " ".join(_row_text(s).lower().split())

    seen = set()
    out = []
    for r in picked:
        # Deduplicate strictly by headline to avoid exact duplicates
        key = _norm(r["headline"])
        if not key:
            key = _norm(r["topic"])
            
        if key in seen:
            continue
        seen.add(key)
        out.append(r)
        if len(out) >= LATEST_NEWS_MAX:
            break
    return out


def _public_links_from_row(row):
    """[{link, source}, ...] for public APIs. New rows store per-article pairs; legacy rows were link[] + short unique sources[]."""
    try:
        raw = json.loads(row["article_links"] or "[]")
    except (TypeError, json.JSONDecodeError, ValueError):
        raw = []
    try:
        sources = json.loads(row["sources"] or "[]")
    except (TypeError, json.JSONDecodeError, ValueError):
        sources = []
    if not raw:
        return []
    out = []
    if isinstance(raw[0], dict) and "link" in raw[0]:
        out = [
            {"link": str(item.get("link") or ""), "source": str(item.get("source") or "Source")}
            for item in raw
        ]
    else:
        for i, url in enumerate(raw):
            if isinstance(url, dict):
                out.append(
                    {
                        "link": str(url.get("link", "")),
                        "source": str(url.get("source", "Source")),
                    }
                )
                continue
            src = sources[i] if i < len(sources) else "Source"
            out.append({"link": str(url), "source": str(src)})
    seen_url = set()
    capped = []
    for item in out:
        u = _row_text(item.get("link"))
        if not u:
            continue
        key = u.lower()
        if key in seen_url:
            continue
        seen_url.add(key)
        capped.append(item)
        if len(capped) >= _PUBLIC_ARTICLE_LINK_CAP:
            break
    return capped


@app.errorhandler(404)
def _not_found(_e):
    if request.path.startswith("/api/"):
        return jsonify({"error": "Not found"}), 404
    return app.send_static_file("404.html"), 404


@app.after_request
def _api_cache_headers(resp):
    if request.path.startswith("/api/admin"):
        resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, private"
        resp.headers["Pragma"] = "no-cache"
        return resp
    if not request.path.startswith("/api/"):
        return resp
    reader_paths = {
        "/api/trending",
        "/api/latest-news",
        "/api/titles",
        "/api/status",
        "/api/search",
        "/api/archive",
        "/api/articles/recent",
        "/api/articles",
    }
    if request.path in reader_paths or request.path.startswith("/api/summary/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp

@app.route('/')
def index():
    return app.send_static_file('index.html')


@app.route('/home')
def home():
    return app.send_static_file('index.html')


@app.route('/overlord')
def overlord():
    return app.send_static_file('admin.html')


@app.route('/admin')
def admin_page():
    return app.send_static_file('admin.html')


@app.route('/analytics')
def analytics_page():
    return app.send_static_file('analytics.html')

@app.route('/briefs')
@app.route('/reels')
@app.route('/reels.html')
def reels_page():
    return app.send_static_file('reels.html')


@app.route('/contact')
def contact_page():
    return app.send_static_file('contact.html')


@app.route('/database')
def database_page():
    return app.send_static_file('database.html')


@app.route('/admin-login')
def admin_login_page():
    return app.send_static_file('admin-login.html')

@app.route('/api/trending', methods=['GET'])
def get_trending():
    rows = _rows_for_latest_news()
    user = _session_user()
    items = _with_votes_and_score(rows, user["id"] if user else None)
    items.sort(key=lambda x: (x.get("vote_score", 0), x.get("id", 0)), reverse=True)
    return jsonify(items)


@app.route('/api/latest-news', methods=['GET'])
def get_latest_news():
    return get_trending()


@app.route('/api/reels', methods=['GET'])
def get_reels():
    return get_trending()


@app.route('/api/articles/recent', methods=['GET'])
@app.route('/api/articles', methods=['GET'])
def articles_recent():
    limit = request.args.get('limit', '25')
    try:
        n = max(1, min(50, int(limit)))
    except ValueError:
        n = 25
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, title, source, link, published, fetched_at
        FROM articles
        ORDER BY id DESC
        LIMIT ?
        """,
        (n,),
    )
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    seen = set()
    deduped = []
    for r in rows:
        title_key = " ".join(_row_text(r.get("title")).lower().split())
        link_key = _row_text(r.get("link")).lower()
        key = (title_key, link_key)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(r)
    return jsonify(deduped)


@app.route('/api/titles', methods=['GET'])
def get_titles():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, topic, headline, created_at
        FROM summaries
        ORDER BY id DESC
        LIMIT 30
        """
    )
    rows = cursor.fetchall()
    conn.close()
    out = []
    for r in rows:
        h = _row_text(r["headline"])
        out.append(
            {
                "id": r["id"],
                "topic": r["topic"],
                "headline": h or None,
                "created_at": _created_at_iso_utc(r["created_at"]),
            }
        )
    return jsonify(out)

@app.route('/api/summary/<int:summary_id>', methods=['GET'])
def get_summary(summary_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM summaries WHERE id = ?', (summary_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'Not found'}), 404

    user = _session_user()
    items = _with_votes_and_score([row], user["id"] if user else None)
    return jsonify(items[0] if items else _summary_public_dict(row))

@app.route('/api/search', methods=['GET'])
def search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, topic, headline, summary, sources, article_links, created_at, category,
               importance_score, summary_date
        FROM summaries
        WHERE topic LIKE ? OR summary LIKE ? OR IFNULL(headline, '') LIKE ?
        ORDER BY created_at DESC
        LIMIT 10
        """,
        (f"%{query}%", f"%{query}%", f"%{query}%"),
    )
    rows = cursor.fetchall()
    conn.close()
    user = _session_user()
    items = _with_votes_and_score(rows, user["id"] if user else None)
    items.sort(key=lambda x: (x.get("vote_score", 0), x.get("id", 0)), reverse=True)
    return jsonify(items)

@app.route('/api/archive', methods=['GET'])
def get_archive():
    date = request.args.get('date', '')
    if not date:
        return jsonify([])

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, topic, headline, summary, sources, article_links, created_at, category,
               importance_score, summary_date
        FROM summaries
        WHERE summary_date = ?
        ORDER BY created_at DESC, id DESC
        """,
        (date,),
    )
    rows = cursor.fetchall()
    conn.close()
    user = _session_user()
    items = _with_votes_and_score(rows, user["id"] if user else None)
    items.sort(key=lambda x: (x.get("vote_score", 0), x.get("id", 0)), reverse=True)
    return jsonify(items)

@app.route('/api/summarize/custom', methods=['POST'])
def custom_summarize():
    data = request.get_json()
    summary_id = data.get('summary_id')
    custom_instruction = _row_text(data.get("instruction"))

    if not summary_id or not custom_instruction:
        return jsonify({'error': 'Missing summary_id or instruction'}), 400

    if len(custom_instruction) > 200:
        return jsonify({'error': 'Prompt too long. Max 200 characters.'}), 400

    # Server-side abuse prevention: one custom prompt per user_key per day
    user_key = _client_key()
    today = str(datetime.now().date())
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT 1 FROM custom_prompt_uses WHERE user_key = ? AND use_date = ?', (user_key, today))
    already = cursor.fetchone()
    if already:
        conn.close()
        return jsonify({'error': 'Free usage done for today.'}), 429

    cursor.execute('SELECT * FROM summaries WHERE id = ?', (summary_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'Summary not found'}), 404

    articles_text = f"Topic: {row['topic']}\nExisting Summary: {row['summary']}"
    prompt = get_prompt(articles_text, custom_instruction)

    if not (os.environ.get("GROQ_API_KEY") or "").strip():
        return jsonify({'error': 'Summarizer is not configured right now.'}), 503
    client = get_groq_client()
    response = client.chat.completions.create(
        model="llama-3.1-8b-instant",
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400
    )
    new_summary = _coerce_chat_content(response.choices[0].message.content)

    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            'INSERT INTO custom_prompt_uses (user_key, use_date, used_at) VALUES (?, ?, ?)',
            (user_key, today, _now_iso())
        )
        conn.commit()
    finally:
        conn.close()

    return jsonify({
        'topic': row['topic'],
        'headline': _row_text(row["headline"]) or None,
        'summary': new_summary,
        'custom': True
    })

@app.route('/api/auth/google', methods=['POST'])
def auth_google():
    if id_token is None or google_requests is None:
        return jsonify({"error": "Google auth dependency is missing"}), 503
    body = request.get_json(silent=True) or {}
    credential = _row_text(body.get("credential"))
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    if not credential:
        return jsonify({"error": "Missing credential"}), 400
    if not client_id:
        return jsonify({"error": "GOOGLE_CLIENT_ID is not configured"}), 503
    try:
        info = id_token.verify_oauth2_token(credential, google_requests.Request(), client_id)
    except Exception:
        return jsonify({"error": "Invalid Google token"}), 401

    google_sub = _row_text(info.get("sub"))
    email = _row_text(info.get("email")).lower()
    name = _row_text(info.get("name"))
    avatar_url = _row_text(info.get("picture"))
    if not bool(info.get("email_verified", False)):
        return jsonify({"error": "Google email is not verified"}), 401
    if not google_sub or not email:
        return jsonify({"error": "Google identity missing fields"}), 400

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        INSERT INTO users (google_sub, email, name, avatar_url, created_at)
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(google_sub) DO UPDATE SET
            email = excluded.email,
            name = excluded.name,
            avatar_url = excluded.avatar_url
        """,
        (google_sub, email, name, avatar_url, _now_iso()),
    )
    conn.commit()
    cursor.execute("SELECT id, email, name, avatar_url FROM users WHERE google_sub = ?", (google_sub,))
    row = cursor.fetchone()
    conn.close()
    session["user"] = {"id": row["id"], "email": row["email"], "name": row["name"], "avatar_url": row["avatar_url"]}
    return jsonify({"ok": True, "user": dict(row)})


@app.route('/api/auth/me', methods=['GET'])
def auth_me():
    user = _session_user()
    if not user:
        return jsonify({"user": None})
    return jsonify({"user": user})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session.pop("user", None)
    return jsonify({"ok": True})


@app.route('/api/news/<int:summary_id>/vote', methods=['POST'])
def vote_news(summary_id):
    user = _session_user()
    if not user:
        return jsonify({"error": "Login required"}), 401
    body = request.get_json(silent=True) or {}
    vote = body.get("vote")
    try:
        vote = int(vote)
    except (TypeError, ValueError):
        return jsonify({"error": "Vote must be 1 or -1"}), 400
    if vote not in (-1, 1):
        return jsonify({"error": "Vote must be 1 or -1"}), 400

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM summaries WHERE id = ?", (summary_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({"error": "News not found"}), 404
    cursor.execute(
        """
        INSERT INTO news_votes (user_id, summary_id, vote, created_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(user_id, summary_id) DO UPDATE SET vote = excluded.vote
        """,
        (user["id"], summary_id, vote, _now_iso()),
    )
    conn.commit()
    cursor.execute("SELECT COALESCE(SUM(vote), 0) AS score FROM news_votes WHERE summary_id = ?", (summary_id,))
    score = int(cursor.fetchone()["score"])
    conn.close()
    return jsonify({"ok": True, "summary_id": summary_id, "vote": vote, "score": score})


@app.route('/api/news/<int:summary_id>/comments', methods=['GET'])
def list_comments(summary_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT c.id, c.comment, c.created_at, u.name, u.email
        FROM news_comments c
        JOIN users u ON u.id = c.user_id
        WHERE c.summary_id = ?
        ORDER BY c.id DESC
        LIMIT 200
        """,
        (summary_id,),
    )
    rows = cursor.fetchall()
    conn.close()
    return jsonify(
        [
            {
                "id": r["id"],
                "comment": r["comment"],
                "created_at": r["created_at"],
                "user_name": r["name"] or r["email"].split("@")[0],
            }
            for r in rows
        ]
    )


@app.route('/api/news/<int:summary_id>/comments', methods=['POST'])
def add_comment(summary_id):
    user = _session_user()
    if not user:
        return jsonify({"error": "Login required"}), 401
    body = request.get_json(silent=True) or {}
    comment = _row_text(body.get("comment"))
    if not comment:
        return jsonify({"error": "Comment cannot be empty"}), 400
    if len(comment) > 500:
        return jsonify({"error": "Comment too long (max 500 chars)"}), 400
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM summaries WHERE id = ?", (summary_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({"error": "News not found"}), 404
    new_id = _insert_and_get_id(
        cursor,
        "INSERT INTO news_comments (user_id, summary_id, comment, created_at) VALUES (?, ?, ?, ?)",
        (user["id"], summary_id, comment, _now_iso()),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "id": new_id})


@app.route('/api/chat/messages', methods=['GET'])
@app.route('/api/chat/send', methods=['POST'])
def chat_deleted():
    return jsonify({"error": "Chat has been removed"}), 410


@app.route('/api/admin/chat/messages', methods=['GET'])
@app.route('/api/admin/chat/delete_all', methods=['POST'])
@app.route('/api/admin/chat/delete/<int:message_id>', methods=['POST'])
def admin_chat_deleted(message_id=None):
    auth = _require_admin()
    if auth:
        return auth
    return jsonify({"error": "Chat has been removed"}), 410

def _next_pipeline_run_after(now):
    """Next run at 00:00, 04:00, 08:00, 12:00, 16:00, 20:00 Asia/Kolkata."""
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    ist_now = now.astimezone(IST)
    today = ist_now.date()
    for hour in (0, 4, 8, 12, 16, 20):
        cand = datetime.combine(today, datetime.min.time(), tzinfo=IST).replace(
            hour=hour, minute=0, second=0, microsecond=0
        )
        if cand > ist_now:
            return cand
    return datetime.combine(today + timedelta(days=1), datetime.min.time(), tzinfo=IST)


def _fmt_clock_12h(dt):
    h12 = dt.hour % 12
    if h12 == 0:
        h12 = 12
    ampm = "AM" if dt.hour < 12 else "PM"
    return f"{h12}:{dt.minute:02d} {ampm}"


@app.route('/api/status', methods=['GET'])
def status():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT created_at FROM summaries ORDER BY id DESC LIMIT 1')
    row = cursor.fetchone()
    conn.close()

    last_update = _created_at_iso_utc(row["created_at"]) if row else None

    now = datetime.now(timezone.utc)
    next_dt = _next_pipeline_run_after(now)
    next_update = _fmt_clock_12h(next_dt)

    return jsonify({
        'last_update': last_update,
        'next_update': next_update,
        'next_update_iso': _dt_iso_utc(next_dt),
        'db_engine': 'postgres' if is_postgres() else 'sqlite',
        'status': 'live'
    })


@app.route('/api/config', methods=['GET'])
def public_config():
    return jsonify(
        {
            "google_client_id": os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
        }
    )


@app.route('/api/admin/ping', methods=['GET'])
def admin_ping():
    auth = _require_admin()
    if auth:
        return auth
    return jsonify({"ok": True})


@app.route("/api/admin/login", methods=["POST"])
def admin_login():
    expected = os.environ.get("ADMIN_TOKEN", "").strip()
    if not expected:
        session.clear()
        session["admin_ok"] = True
        session.permanent = True
        return jsonify({"ok": True, "mode": "open"})
    data = request.get_json(silent=True) or {}
    token = (data.get("token") or "").strip()
    if not token or not secrets.compare_digest(token, expected):
        return jsonify({"error": "Invalid token"}), 401
    session.clear()
    session["admin_ok"] = True
    session.permanent = True
    return jsonify({"ok": True})


@app.route("/api/admin/logout", methods=["POST"])
def admin_logout():
    session.pop("admin_ok", None)
    return jsonify({"ok": True})


@app.route("/api/admin/run-pipeline", methods=["POST"])
def admin_run_pipeline():
    auth = _require_admin()
    if auth:
        return auth
    data = request.get_json(silent=True) or {}
    force = bool(data.get("force", True))

    def _job():
        try:
            from backend.pipeline import run_pipeline

            run_pipeline(force_summarize=force)
        except Exception as e:
            print(f"Pipeline error: {e}")

    threading.Thread(target=_job, daemon=True).start()
    return jsonify({"ok": True, "started": True, "force": force})


@app.route('/api/admin/stats', methods=['GET'])
def admin_stats():
    auth = _require_admin()
    if auth:
        return auth
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT COUNT(*) AS c FROM articles')
    articles = cursor.fetchone()['c']
    cursor.execute('SELECT COUNT(*) AS c FROM summaries')
    summaries = cursor.fetchone()['c']
    cursor.execute('SELECT COUNT(*) AS c FROM chat_messages')
    chats = cursor.fetchone()['c']
    cursor.execute('SELECT COUNT(*) AS c FROM custom_prompt_uses')
    prompt_uses = cursor.fetchone()['c']
    conn.close()
    return jsonify({
        'articles': articles,
        'summaries': summaries,
        'chat_messages': chats,
        'custom_prompt_uses': prompt_uses
    })

@app.route('/api/admin/latest', methods=['GET'])
def admin_latest():
    auth = _require_admin()
    if auth:
        return auth

    limit = request.args.get('limit', '20')
    try:
        limit_n = max(1, min(100, int(limit)))
    except Exception:
        limit_n = 20

    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, title, source, link, published, fetched_at
        FROM articles
        ORDER BY id DESC
        LIMIT ?
    ''', (limit_n,))
    articles_pulled = [dict(r) for r in cursor.fetchall()]

    cursor.execute('''
        SELECT id, topic, headline, category, created_at, summary_date
        FROM summaries
        ORDER BY id DESC
        LIMIT ?
    ''', (limit_n,))
    summaries = [dict(r) for r in cursor.fetchall()]

    cursor.execute('''
        SELECT id, user_key, use_date, used_at
        FROM custom_prompt_uses
        ORDER BY id DESC
        LIMIT ?
    ''', (limit_n,))
    custom_prompt_uses = [dict(r) for r in cursor.fetchall()]

    latest_rows = _rows_for_latest_news()
    latest_news = [_summary_public_dict(r) for r in latest_rows]

    conn.close()
    return jsonify({
        'articles_pulled': articles_pulled,
        'summaries': summaries,
        'latest_news': latest_news,
        'articles': articles_pulled,
        'custom_prompt_uses': custom_prompt_uses
    })


@app.route('/api/admin/backup', methods=['POST'])
def admin_backup():
    auth = _require_admin()
    if auth:
        return auth
    payload = _fetch_export_payload(None)
    conn = get_connection()
    cursor = conn.cursor()
    backup_id = _insert_and_get_id(
        cursor,
        "INSERT INTO backups (created_at, payload_json) VALUES (?, ?)",
        (_now_iso(), json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()
    conn.close()
    return jsonify({"ok": True, "backup_id": backup_id, "created_at": payload["exported_at"]})
@app.route('/api/stats', methods=['GET'])
def get_stats():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS c FROM articles")
    article_count = int(cursor.fetchone()["c"])
    cursor.execute("SELECT COUNT(*) AS c FROM summaries")
    summary_count = int(cursor.fetchone()["c"])
    cursor.execute("SELECT COUNT(*) AS c FROM users")
    user_count = int(cursor.fetchone()["c"])
    cursor.execute("SELECT COUNT(*) AS c FROM news_votes")
    vote_count = int(cursor.fetchone()["c"])
    conn.close()
    return jsonify({
        "articles": article_count,
        "summaries": summary_count,
        "users": user_count,
        "votes": vote_count
    })



@app.route('/api/admin/analytics', methods=['GET'])
def admin_analytics():
    auth = _require_admin()
    if auth:
        return auth
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT COUNT(*) AS c FROM articles")
    article_count = int(cursor.fetchone()["c"])
    cursor.execute("SELECT COUNT(*) AS c FROM summaries")
    summary_count = int(cursor.fetchone()["c"])
    cursor.execute("SELECT COUNT(*) AS c FROM users")
    user_count = int(cursor.fetchone()["c"])
    cursor.execute("SELECT COUNT(*) AS c FROM news_comments")
    comment_count = int(cursor.fetchone()["c"])
    cursor.execute("SELECT COUNT(*) AS c FROM news_votes")
    vote_count = int(cursor.fetchone()["c"])
    cursor.execute(
        """
        SELECT s.id, IFNULL(s.headline, s.topic) AS title, COALESCE(SUM(v.vote), 0) AS score
        FROM summaries s
        LEFT JOIN news_votes v ON v.summary_id = s.id
        WHERE date(s.created_at) = date('now', 'localtime') OR s.summary_date = date('now', 'localtime')
        GROUP BY s.id
        ORDER BY score DESC, s.id DESC
        LIMIT 20
        """
    )
    top_news = [{"id": r["id"], "title": r["title"], "score": int(r["score"])} for r in cursor.fetchall()]
    cursor.execute(
        """
        SELECT summary_date AS day, COUNT(*) AS c
        FROM summaries
        GROUP BY summary_date
        ORDER BY summary_date DESC
        LIMIT 7
        """
    )
    daily = [{"day": r["day"], "summaries": int(r["c"])} for r in cursor.fetchall()]

    cursor.execute(
        """
        SELECT source, COUNT(*) AS c
        FROM articles
        WHERE IFNULL(source, '') != '' AND date(fetched_at) >= date('now', '-7 days', 'localtime')
        GROUP BY source
        ORDER BY c DESC
        """
    )
    articles_by_source = [{"source": r["source"], "count": int(r["c"])} for r in cursor.fetchall()]

    cursor.execute(
        """
        SELECT category, COUNT(*) AS c
        FROM summaries
        WHERE IFNULL(category, '') != '' AND summary_date >= date('now', '-7 days', 'localtime')
        GROUP BY category
        ORDER BY c DESC
        """
    )
    summaries_by_category = [{"category": r["category"], "count": int(r["c"])} for r in cursor.fetchall()]

    conn.close()
    return jsonify(
        {
            "overview": {
                "articles": article_count,
                "summaries": summary_count,
                "users": user_count,
                "comments": comment_count,
                "votes": vote_count,
            },
            "top_news": top_news,
            "daily_summaries": daily,
            "articles_by_source": articles_by_source,
            "summaries_by_category": summaries_by_category,
        }
    )

def _export_limit_from_request():
    if (request.args.get("all") or "").lower() in ("1", "true", "yes"):
        return None
    limit = request.args.get("limit", "200")
    try:
        return max(1, min(50_000_000, int(limit)))
    except Exception:
        return 200


def _fetch_export_payload(limit_n):
    conn = get_connection()
    cursor = conn.cursor()

    if limit_n is None:
        cursor.execute(
            "SELECT id, title, source, link, description, published, fetched_at FROM articles ORDER BY id DESC"
        )
    else:
        cursor.execute(
            "SELECT id, title, source, link, description, published, fetched_at FROM articles ORDER BY id DESC LIMIT ?",
            (limit_n,),
        )
    articles = [dict(r) for r in cursor.fetchall()]

    if limit_n is None:
        cursor.execute(
            "SELECT id, topic, headline, summary, sources, article_links, created_at, summary_date, category, importance_score FROM summaries ORDER BY id DESC"
        )
    else:
        cursor.execute(
            "SELECT id, topic, headline, summary, sources, article_links, created_at, summary_date, category, importance_score FROM summaries ORDER BY id DESC LIMIT ?",
            (limit_n,),
        )
    summaries = [dict(r) for r in cursor.fetchall()]

    if limit_n is None:
        cursor.execute("SELECT id, message, sent_at FROM chat_messages ORDER BY id DESC")
    else:
        cursor.execute(
            "SELECT id, message, sent_at FROM chat_messages ORDER BY id DESC LIMIT ?",
            (limit_n,),
        )
    chat_messages = [dict(r) for r in cursor.fetchall()]

    if limit_n is None:
        cursor.execute("SELECT id, user_key, use_date, used_at FROM custom_prompt_uses ORDER BY id DESC")
    else:
        cursor.execute(
            "SELECT id, user_key, use_date, used_at FROM custom_prompt_uses ORDER BY id DESC LIMIT ?",
            (limit_n,),
        )
    custom_prompt_uses = [dict(r) for r in cursor.fetchall()]

    conn.close()
    return {
        "exported_at": _now_iso(),
        "limit": "all" if limit_n is None else limit_n,
        "articles": articles,
        "summaries": summaries,
        "chat_messages": chat_messages,
        "custom_prompt_uses": custom_prompt_uses,
    }


@app.route('/api/admin/export', methods=['GET'])
def admin_export():
    auth = _require_admin()
    if auth:
        return auth
    limit_n = _export_limit_from_request()
    return jsonify(_fetch_export_payload(limit_n))


@app.route("/api/admin/export-file", methods=["GET"])
@app.route("/api/admin/export_file", methods=["GET"])
def admin_export_file():
    auth = _require_admin()
    if auth:
        return auth
    limit_n = _export_limit_from_request()
    payload = _fetch_export_payload(limit_n)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    resp = make_response(body)
    resp.headers['Content-Type'] = 'application/json; charset=utf-8'
    resp.headers['Content-Disposition'] = 'attachment; filename="newslens-export.json"'
    return resp

@app.route('/api/admin/purge', methods=['POST'])
def admin_purge():
    auth = _require_admin()
    if auth:
        return auth
    data = request.get_json(silent=True) or {}
    confirm = _row_text(data.get("confirm")).lower()
    if confirm != 'delete will be permanent':
        return jsonify({'error': 'Confirmation phrase required: "delete will be permanent"'}), 400

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM articles')
    a = cursor.rowcount
    cursor.execute('DELETE FROM summaries')
    s = cursor.rowcount
    cursor.execute('DELETE FROM chat_messages')
    c = cursor.rowcount
    cursor.execute('DELETE FROM custom_prompt_uses')
    u = cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'deleted': {'articles': a, 'summaries': s, 'chat_messages': c, 'custom_prompt_uses': u}})

@app.route('/api/cron/run', methods=['GET'])
def cron_run():
    secret = request.args.get('secret', '')
    expected = os.environ.get('CRON_SECRET', '')
    if expected and secret != expected:
        return jsonify({'error': 'unauthorized'}), 401

    def _job():
        try:
            from backend.pipeline import run_pipeline
            run_pipeline(force_summarize=False)
        except Exception as e:
            print(f"Cron error: {e}")

    threading.Thread(target=_job, daemon=True).start()
    return jsonify({'ok': True, 'started': True})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
