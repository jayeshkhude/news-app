import sqlite3
import os
import hashlib

DB_PATH = os.path.join(os.path.dirname(__file__), '..', 'data', 'news.db')

def get_connection():
    conn = sqlite3.connect(DB_PATH, timeout=30.0)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
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
            fetched_at TEXT
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
            summary_date TEXT
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

    _migrate_summaries(cursor)
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


if __name__ == "__main__":
    init_db()