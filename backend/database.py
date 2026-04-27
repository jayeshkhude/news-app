import sqlite3
import os
import hashlib

_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "news.db")

def _resolve_db_path() -> str:
    # Deployment-friendly precedence:
    # 1) SQLITE_DB_PATH (explicit path)
    # 2) DATABASE_URL with sqlite:///... form
    # 3) project default ./data/news.db
    explicit = os.environ.get("SQLITE_DB_PATH", "").strip()
    if explicit:
        return explicit
    db_url = os.environ.get("DATABASE_URL", "").strip()
    if db_url.lower().startswith("sqlite:///"):
        raw = db_url[len("sqlite:///") :].strip()
        if raw:
            return raw
    return _DEFAULT_DB_PATH

DB_PATH = _resolve_db_path()


def _ensure_db_dir(db_path: str) -> None:
    folder = os.path.dirname(os.path.abspath(db_path))
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)

def get_connection():
    # Render/containers may not have the ./data directory created (or writable) by default.
    # Ensure parent directory exists; if it still fails, fall back to /tmp.
    db_path = DB_PATH
    try:
        _ensure_db_dir(db_path)
        conn = sqlite3.connect(db_path, timeout=30.0)
    except sqlite3.OperationalError:
        db_path = os.path.join("/tmp", "news.db")
        _ensure_db_dir(db_path)
        conn = sqlite3.connect(db_path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
    except sqlite3.Error:
        pass
    return conn

def init_db():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            link TEXT UNIQUE,
            source TEXT,
            description TEXT,
            published TEXT,
            fetched_at TEXT,
            image_url TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            summary TEXT,
            sources TEXT,
            article_links TEXT,
            created_at TEXT,
            summary_date TEXT,
            thumbnail_url TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT,
            sent_at TEXT
        )
    ''')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS custom_prompt_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key TEXT,
            use_date TEXT,
            used_at TEXT,
            UNIQUE(user_key, use_date)
        )
    ''')

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            google_sub TEXT UNIQUE,
            email TEXT UNIQUE,
            name TEXT,
            avatar_url TEXT,
            created_at TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS news_votes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            summary_id INTEGER NOT NULL,
            vote INTEGER NOT NULL CHECK(vote IN (-1, 1)),
            created_at TEXT,
            UNIQUE(user_id, summary_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(summary_id) REFERENCES summaries(id) ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS news_comments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            summary_id INTEGER NOT NULL,
            comment TEXT NOT NULL,
            created_at TEXT,
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(summary_id) REFERENCES summaries(id) ON DELETE CASCADE
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT,
            payload_json TEXT NOT NULL
        )
        """
    )

    _migrate_summaries(cursor)
    _migrate_articles(cursor)
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_votes_summary
        ON news_votes(summary_id)
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_comments_summary
        ON news_comments(summary_id, id DESC)
        """
    )
    conn.commit()
    conn.close()
    print("Database ready")


def _migrate_summaries(cursor):
    cursor.execute("PRAGMA table_info(summaries)")
    cols = {row[1] for row in cursor.fetchall()}
    if "category" not in cols:
        cursor.execute("ALTER TABLE summaries ADD COLUMN category TEXT DEFAULT ''")
    if "headline" not in cols:
        cursor.execute("ALTER TABLE summaries ADD COLUMN headline TEXT DEFAULT ''")
    if "importance_score" not in cols:
        cursor.execute("ALTER TABLE summaries ADD COLUMN importance_score INTEGER DEFAULT 0")
    if "links_hash" not in cols:
        cursor.execute("ALTER TABLE summaries ADD COLUMN links_hash TEXT DEFAULT ''")
    if "content_hash" not in cols:
        cursor.execute("ALTER TABLE summaries ADD COLUMN content_hash TEXT DEFAULT ''")
    if "thumbnail_url" not in cols:
        cursor.execute("ALTER TABLE summaries ADD COLUMN thumbnail_url TEXT DEFAULT ''")

    # Backfill links_hash for existing rows so the unique index can be created.
    # (We only hash existing article_links as-is; new writes use a canonical form in summarizer.py.)
    cursor.execute("SELECT id, article_links FROM summaries WHERE IFNULL(links_hash, '') = ''")
    rows = cursor.fetchall()
    for r in rows:
        raw = (r[1] or "").strip()
        if not raw:
            continue
        h = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cursor.execute("UPDATE summaries SET links_hash = ? WHERE id = ?", (h, r[0]))

    # Remove existing duplicates (keep newest id) before enforcing uniqueness.
    cursor.execute(
        """
        DELETE FROM summaries
        WHERE links_hash <> ''
          AND id NOT IN (
            SELECT MAX(id)
            FROM summaries
            WHERE links_hash <> ''
            GROUP BY summary_date, links_hash
          )
        """
    )

    # DB-level dedupe (partial unique index avoids conflicts on empty hashes).
    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_summaries_unique_date_links
        ON summaries(summary_date, links_hash)
        WHERE links_hash <> ''
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_summaries_date_created
        ON summaries(summary_date, created_at)
        """
    )


def _migrate_articles(cursor):
    cursor.execute("PRAGMA table_info(articles)")
    cols = {row[1] for row in cursor.fetchall()}
    if "title_hash" not in cols:
        cursor.execute("ALTER TABLE articles ADD COLUMN title_hash TEXT DEFAULT ''")
    if "link_canonical" not in cols:
        cursor.execute("ALTER TABLE articles ADD COLUMN link_canonical TEXT DEFAULT ''")
    if "image_url" not in cols:
        cursor.execute("ALTER TABLE articles ADD COLUMN image_url TEXT DEFAULT ''")

    cursor.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS idx_articles_unique_link_canonical
        ON articles(link_canonical)
        WHERE link_canonical <> ''
        """
    )
    cursor.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_articles_title_hash
        ON articles(title_hash)
        """
    )


if __name__ == "__main__":
    init_db()