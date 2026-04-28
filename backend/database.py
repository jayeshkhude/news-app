import hashlib
import os
import sqlite3
import tempfile

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:
    psycopg = None
    dict_row = None


_PSYCOPG_AVAILABLE = psycopg is not None and dict_row is not None


_DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "..", "data", "news.db")
_POSTGRES_PREFIXES = ("postgres://", "postgresql://")


def _normalize_database_url(db_url: str) -> str:
    raw = (db_url or "").strip()
    if raw.lower().startswith("postgres://"):
        return "postgresql://" + raw[len("postgres://"):]
    return raw


def _resolve_db_config():
    explicit = os.environ.get("SQLITE_DB_PATH", "").strip()
    if explicit:
        return "sqlite", explicit

    db_url = _normalize_database_url(os.environ.get("DATABASE_URL", ""))
    if db_url.lower().startswith(_POSTGRES_PREFIXES):
        # Allow the app to boot even when psycopg isn't installed locally.
        # In production, psycopg should be present (see requirements.txt).
        if not _PSYCOPG_AVAILABLE:
            return "sqlite", _DEFAULT_DB_PATH
        return "postgres", db_url

    if db_url.lower().startswith("sqlite:///"):
        raw = db_url[len("sqlite:///") :].strip()
        if raw:
            return "sqlite", raw

    return "sqlite", _DEFAULT_DB_PATH


DB_ENGINE, DB_TARGET = _resolve_db_config()
DB_PATH = DB_TARGET if DB_ENGINE == "sqlite" else ""
DATABASE_URL = DB_TARGET if DB_ENGINE == "postgres" else ""


def is_postgres() -> bool:
    return DB_ENGINE == "postgres"


def default_sqlite_path() -> str:
    return _DEFAULT_DB_PATH


def _ensure_psycopg() -> None:
    if _PSYCOPG_AVAILABLE:
        return
    raise RuntimeError(
        "PostgreSQL support requires psycopg. Add `psycopg[binary]` to your environment "
        "before using DATABASE_URL with a postgres connection string."
    )


def _translate_sql(query: str) -> str:
    return query.replace("?", "%s")


class PostgresCursorCompat:
    def __init__(self, cursor):
        self._cursor = cursor

    def execute(self, query, params=None):
        sql = _translate_sql(query)
        if params is None:
            self._cursor.execute(sql)
        else:
            self._cursor.execute(sql, params)
        return self

    def executemany(self, query, seq_of_params):
        self._cursor.executemany(_translate_sql(query), seq_of_params)
        return self

    def fetchone(self):
        return self._cursor.fetchone()

    def fetchall(self):
        return self._cursor.fetchall()

    def close(self):
        self._cursor.close()

    def __iter__(self):
        return iter(self._cursor)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    def __getattr__(self, name):
        return getattr(self._cursor, name)


class PostgresConnectionCompat:
    def __init__(self, conn):
        self._conn = conn

    def cursor(self, *args, **kwargs):
        return PostgresCursorCompat(self._conn.cursor(*args, **kwargs))

    def commit(self):
        return self._conn.commit()

    def rollback(self):
        return self._conn.rollback()

    def close(self):
        return self._conn.close()

    def __getattr__(self, name):
        return getattr(self._conn, name)


def _ensure_db_dir(db_path: str) -> None:
    folder = os.path.dirname(os.path.abspath(db_path))
    if not folder:
        return
    os.makedirs(folder, exist_ok=True)


def get_connection():
    if is_postgres():
        _ensure_psycopg()
        conn = psycopg.connect(DATABASE_URL, row_factory=dict_row)
        return PostgresConnectionCompat(conn)

    db_path = DB_PATH
    try:
        _ensure_db_dir(db_path)
        conn = sqlite3.connect(db_path, timeout=30.0)
    except (OSError, sqlite3.OperationalError):
        db_path = os.path.join(tempfile.gettempdir(), "news.db")
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

    if is_postgres():
        _create_postgres_schema(cursor)
    else:
        _create_sqlite_schema(cursor)

    _migrate_summaries(cursor)
    _migrate_articles(cursor)
    _create_common_indexes(cursor)
    conn.commit()

    if is_postgres():
        sync_postgres_sequences(conn)

    conn.commit()
    conn.close()
    print(f"Database ready ({DB_ENGINE})")


def _create_sqlite_schema(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT,
            link TEXT UNIQUE,
            source TEXT,
            description TEXT,
            published TEXT,
            fetched_at TEXT,
            image_url TEXT,
            title_hash TEXT DEFAULT '',
            link_canonical TEXT DEFAULT ''
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            topic TEXT,
            headline TEXT DEFAULT '',
            summary TEXT,
            sources TEXT,
            article_links TEXT,
            created_at TEXT,
            summary_date TEXT,
            category TEXT DEFAULT '',
            importance_score INTEGER DEFAULT 0,
            links_hash TEXT DEFAULT '',
            content_hash TEXT DEFAULT '',
            thumbnail_url TEXT DEFAULT ''
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            message TEXT,
            sent_at TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_prompt_uses (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_key TEXT,
            use_date TEXT,
            used_at TEXT,
            UNIQUE(user_key, use_date)
        )
        """
    )

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


def _create_postgres_schema(cursor):
    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS articles (
            id BIGSERIAL PRIMARY KEY,
            title TEXT,
            link TEXT UNIQUE,
            source TEXT,
            description TEXT,
            published TEXT,
            fetched_at TEXT,
            image_url TEXT,
            title_hash TEXT DEFAULT '',
            link_canonical TEXT DEFAULT ''
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS summaries (
            id BIGSERIAL PRIMARY KEY,
            topic TEXT,
            headline TEXT DEFAULT '',
            summary TEXT,
            sources TEXT,
            article_links TEXT,
            created_at TEXT,
            summary_date TEXT,
            category TEXT DEFAULT '',
            importance_score INTEGER DEFAULT 0,
            links_hash TEXT DEFAULT '',
            content_hash TEXT DEFAULT '',
            thumbnail_url TEXT DEFAULT ''
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id BIGSERIAL PRIMARY KEY,
            message TEXT,
            sent_at TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS custom_prompt_uses (
            id BIGSERIAL PRIMARY KEY,
            user_key TEXT,
            use_date TEXT,
            used_at TEXT,
            UNIQUE(user_key, use_date)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS users (
            id BIGSERIAL PRIMARY KEY,
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
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            summary_id BIGINT NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
            vote SMALLINT NOT NULL CHECK(vote IN (-1, 1)),
            created_at TEXT,
            UNIQUE(user_id, summary_id)
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS news_comments (
            id BIGSERIAL PRIMARY KEY,
            user_id BIGINT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            summary_id BIGINT NOT NULL REFERENCES summaries(id) ON DELETE CASCADE,
            comment TEXT NOT NULL,
            created_at TEXT
        )
        """
    )

    cursor.execute(
        """
        CREATE TABLE IF NOT EXISTS backups (
            id BIGSERIAL PRIMARY KEY,
            created_at TEXT,
            payload_json TEXT NOT NULL
        )
        """
    )


def _create_common_indexes(cursor):
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


def _column_names(cursor, table_name: str):
    if is_postgres():
        cursor.execute(
            """
            SELECT column_name AS name
            FROM information_schema.columns
            WHERE table_schema = 'public' AND table_name = ?
            """,
            (table_name,),
        )
    else:
        cursor.execute(f"PRAGMA table_info({table_name})")
    return {str(row["name"]).strip() for row in cursor.fetchall()}


def _migrate_summaries(cursor):
    cols = _column_names(cursor, "summaries")
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

    cursor.execute("SELECT id, article_links FROM summaries WHERE COALESCE(links_hash, '') = ''")
    rows = cursor.fetchall()
    for row in rows:
        raw = str(row["article_links"] or "").strip()
        if not raw:
            continue
        links_hash = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        cursor.execute("UPDATE summaries SET links_hash = ? WHERE id = ?", (links_hash, row["id"]))

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
    cols = _column_names(cursor, "articles")
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


def sync_postgres_sequences(conn=None):
    if not is_postgres():
        return

    close_after = False
    if conn is None:
        conn = get_connection()
        close_after = True

    cursor = conn.cursor()
    for table_name in (
        "articles",
        "summaries",
        "chat_messages",
        "custom_prompt_uses",
        "users",
        "news_votes",
        "news_comments",
        "backups",
    ):
        cursor.execute(
            f"""
            SELECT setval(
                pg_get_serial_sequence('{table_name}', 'id'),
                COALESCE((SELECT MAX(id) FROM {table_name}), 1),
                (SELECT COALESCE(MAX(id), 0) > 0 FROM {table_name})
            )
            """
        )
    conn.commit()

    if close_after:
        conn.close()


if __name__ == "__main__":
    init_db()
