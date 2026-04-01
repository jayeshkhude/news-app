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
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from backend.database import get_connection, init_db
from backend.prompts import get_prompt

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

allowed_origins_raw = os.environ.get("ALLOWED_ORIGINS", "").strip()
allowed_origins = [o.strip() for o in allowed_origins_raw.split(",") if o.strip()]
if allowed_origins:
    CORS(app, resources={r"/api/*": {"origins": allowed_origins}})

init_db()


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

def _require_admin():
    if session.get("admin_ok") is True:
        return None
    token = os.environ.get("ADMIN_TOKEN", "").strip()
    if not token:
        return jsonify({"error": "Admin not configured"}), 503
    provided = (request.headers.get("X-Admin-Token") or "").strip()
    if provided and secrets.compare_digest(provided, token):
        return None
    return jsonify({"error": "Unauthorized"}), 401


_PUBLIC_ARTICLE_LINK_CAP = 30


def _summary_public_dict(row):
    headline = _row_text(row["headline"])
    return {
        "id": row["id"],
        "topic": row["topic"],
        "headline": headline or None,
        "summary": row["summary"],
        "sources": json.loads(row["sources"]),
        "links": _public_links_from_row(row),
        "created_at": _created_at_iso_utc(row["created_at"]),
        "category": _row_text(row["category"]) or None,
    }


def _diversify_by_category(rows, limit):
    """Prefer at most one story per category, then fill by score order."""
    out = []
    seen_ids = set()
    seen_cat = set()
    for row in rows:
        rid = row["id"]
        c = _row_text(row["category"] if "category" in row.keys() else "").lower() or "other"
        if c in seen_cat:
            continue
        seen_cat.add(c)
        out.append(row)
        seen_ids.add(rid)
        if len(out) >= limit:
            return out
    for row in rows:
        rid = row["id"]
        if rid in seen_ids:
            continue
        out.append(row)
        seen_ids.add(rid)
        if len(out) >= limit:
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
        "/api/titles",
        "/api/status",
        "/api/search",
        "/api/archive",
    }
    if request.path in reader_paths or request.path.startswith("/api/summary/"):
        resp.headers["Cache-Control"] = "no-store, max-age=0, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp

@app.route('/')
def index():
    return app.send_static_file('index.html')

@app.route('/api/trending', methods=['GET'])
def get_trending():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, topic, headline, summary, sources, article_links, created_at, category,
               importance_score, summary_date
        FROM summaries
        ORDER BY (summary_date >= date('now', '-2 day')) DESC,
                 summary_date DESC,
                 importance_score DESC,
                 id DESC
        LIMIT 40
        """
    )
    rows = cursor.fetchall()
    conn.close()
    picked = _diversify_by_category(rows, 8)
    return jsonify([_summary_public_dict(r) for r in picked])

@app.route('/api/titles', methods=['GET'])
def get_titles():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, topic, headline, created_at, category, importance_score, summary_date
        FROM summaries
        ORDER BY (summary_date >= date('now', '-2 day')) DESC,
                 summary_date DESC,
                 importance_score DESC,
                 id DESC
        LIMIT 28
        """
    )
    rows = cursor.fetchall()
    conn.close()
    picked = _diversify_by_category(rows, 12)
    out = []
    for r in picked:
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

    return jsonify(_summary_public_dict(row))

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
    return jsonify([_summary_public_dict(r) for r in rows])

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
        ORDER BY importance_score DESC, id DESC
        """,
        (date,),
    )
    rows = cursor.fetchall()
    conn.close()
    return jsonify([_summary_public_dict(r) for r in rows])

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

chat_rate_limit = {}


def _fetch_chat_messages():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT id, message, sent_at
        FROM chat_messages
        ORDER BY id DESC
        LIMIT 50
        """
    )
    rows = cursor.fetchall()
    conn.close()
    result = [{"id": r["id"], "message": r["message"], "sent_at": r["sent_at"]} for r in rows]
    result.reverse()
    return result


@app.route('/api/chat/messages', methods=['GET'])
def get_messages():
    return jsonify(_fetch_chat_messages())


@app.route('/api/admin/chat/messages', methods=['GET'])
def admin_chat_messages():
    auth = _require_admin()
    if auth:
        return auth
    return jsonify(_fetch_chat_messages())

@app.route('/api/chat/send', methods=['POST'])
def send_message():
    data = request.get_json()
    message = _row_text(data.get("message"))
    user_id = _client_key()

    if not message:
        return jsonify({'error': 'Empty message'}), 400

    if len(message) > 200:
        return jsonify({'error': 'Max 200 characters'}), 400

    now = time.time()
    history = chat_rate_limit.get(user_id, [])
    history = [t for t in history if now - t < 300]

    if len(history) >= 2:
        wait = int(300 - (now - history[0]))
        return jsonify({'error': f'Limit reached. Wait {wait} seconds.'}), 429

    history.append(now)
    chat_rate_limit[user_id] = history

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO chat_messages (message, sent_at) VALUES (?, ?)',
                   (message, _now_iso()))
    conn.commit()
    conn.close()

    return jsonify({'success': True})

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
        'status': 'live'
    })


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
        return jsonify({"error": "Admin not configured"}), 503
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


@app.route('/api/admin/chat/delete_all', methods=['POST'])
def admin_delete_all_chat():
    auth = _require_admin()
    if auth:
        return auth
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM chat_messages')
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return jsonify({'success': True, 'deleted': deleted})

@app.route('/api/admin/chat/delete/<int:message_id>', methods=['POST'])
def admin_delete_chat_message(message_id):
    auth = _require_admin()
    if auth:
        return auth
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('DELETE FROM chat_messages WHERE id = ?', (message_id,))
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    if deleted == 0:
        return jsonify({'error': 'Not found'}), 404
    return jsonify({'success': True, 'deleted': deleted})

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
    articles = [dict(r) for r in cursor.fetchall()]

    cursor.execute('''
        SELECT id, topic, created_at, summary_date
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

    conn.close()
    return jsonify({
        'articles': articles,
        'summaries': summaries,
        'custom_prompt_uses': custom_prompt_uses
    })

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