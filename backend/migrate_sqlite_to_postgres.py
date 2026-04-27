import os
import sqlite3
import sys

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from backend.database import default_sqlite_path, get_connection, init_db, is_postgres, sync_postgres_sequences


TABLES = [
    ("users", ["id", "google_sub", "email", "name", "avatar_url", "created_at"]),
    ("articles", ["id", "title", "link", "source", "description", "published", "fetched_at", "image_url", "title_hash", "link_canonical"]),
    ("summaries", ["id", "topic", "headline", "summary", "sources", "article_links", "created_at", "summary_date", "category", "importance_score", "links_hash", "content_hash", "thumbnail_url"]),
    ("chat_messages", ["id", "message", "sent_at"]),
    ("custom_prompt_uses", ["id", "user_key", "use_date", "used_at"]),
    ("news_votes", ["id", "user_id", "summary_id", "vote", "created_at"]),
    ("news_comments", ["id", "user_id", "summary_id", "comment", "created_at"]),
    ("backups", ["id", "created_at", "payload_json"]),
]


def _source_path() -> str:
    if len(sys.argv) > 1 and sys.argv[1].strip():
        return sys.argv[1].strip()
    env_path = os.environ.get("SQLITE_IMPORT_PATH", "").strip()
    if env_path:
        return env_path
    sqlite_env = os.environ.get("SQLITE_DB_PATH", "").strip()
    if sqlite_env:
        return sqlite_env
    return default_sqlite_path()


def main():
    if not is_postgres():
        raise SystemExit("Set DATABASE_URL to a PostgreSQL connection string before running this migration.")

    source_path = _source_path()
    if not os.path.exists(source_path):
        raise SystemExit(f"SQLite source not found: {source_path}")

    init_db()

    source = sqlite3.connect(source_path)
    source.row_factory = sqlite3.Row
    target = get_connection()
    cursor = target.cursor()

    try:
        for table_name, columns in TABLES:
            col_sql = ", ".join(columns)
            placeholders = ", ".join(["?"] * len(columns))
            rows = source.execute(f"SELECT {col_sql} FROM {table_name} ORDER BY id").fetchall()
            if not rows:
                print(f"{table_name}: 0 rows")
                continue

            insert_sql = f"INSERT INTO {table_name} ({col_sql}) VALUES ({placeholders}) ON CONFLICT DO NOTHING"
            inserted = 0
            for row in rows:
                cursor.execute(insert_sql, [row[col] for col in columns])
                if cursor.rowcount and cursor.rowcount > 0:
                    inserted += cursor.rowcount
            print(f"{table_name}: copied {inserted} row(s) from {len(rows)} source row(s)")

        target.commit()
        sync_postgres_sequences(target)
    finally:
        source.close()
        target.close()

    print("SQLite to PostgreSQL migration complete.")


if __name__ == "__main__":
    main()
