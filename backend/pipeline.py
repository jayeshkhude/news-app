
import os
import sys
from datetime import datetime

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from backend.database import init_db
from backend.collector import collect_articles
from backend.summarizer import run_summarizer


def run_pipeline(force_summarize: bool = False) -> None:
    init_db()
    print(f"\n--- Pipeline started at {datetime.now().strftime('%H:%M:%S')} ---")

    new_count = collect_articles()

    if new_count == 0 and not force_summarize:
        print("No new articles found, skipping summarization")
        print("--- Pipeline done ---\n")
        return

    if new_count == 0 and force_summarize:
        print("Force run: summarizing from articles already in the database…")

    print(f"{new_count} new article(s) collected this run; running summarization…")
    run_summarizer()
    print("--- Pipeline done ---\n")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="NewsLens ingest + summarize pipeline")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run summarization even if no new RSS rows were inserted",
    )
    args = parser.parse_args()
    run_pipeline(force_summarize=args.force)
