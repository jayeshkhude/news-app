import sys
import os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

from apscheduler.schedulers.blocking import BlockingScheduler
from backend.collector import collect_articles
from backend.clusterer import cluster_articles
from backend.summarizer import run_summarizer
from backend.database import init_db, get_connection
from datetime import datetime

def run_pipeline():
    print(f"\n--- Pipeline Started at {datetime.now().strftime('%H:%M:%S')} ---")
    
    new_count = collect_articles()
    
    if new_count == 0:
        print("No new articles found, skipping summarization")
        print("--- Pipeline Done ---\n")
        return
    
    print(f"{new_count} new articles, running summarization...")
    run_summarizer()
    print("--- Pipeline Done ---\n")

if __name__ == "__main__":
    init_db()
    print("Running pipeline once on startup...")
    run_pipeline()

    scheduler = BlockingScheduler()
    scheduler.add_job(run_pipeline, 'interval', hours=4)
    print("Scheduler running. Next job in 4 hours.")
    print("Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("Scheduler stopped.")