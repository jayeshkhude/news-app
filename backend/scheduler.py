import sys
import os

sys.path.append(os.path.dirname(os.path.dirname(__file__)))


def _load_local_env():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    dotenv_path = os.path.join(repo_root, ".env")
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
                if key:
                    # Local dev expects `.env` to be the source of truth.
                    os.environ[key] = value
    except OSError:
        pass


_load_local_env()

from apscheduler.schedulers.blocking import BlockingScheduler
from datetime import datetime
from zoneinfo import ZoneInfo

from backend.database import init_db
from backend.pipeline import run_pipeline

if __name__ == "__main__":
    init_db()
    run_on_start = os.environ.get("RUN_PIPELINE_ON_START", "0").strip().lower() in ("1", "true", "yes")
    if run_on_start:
        print("Running pipeline once on startup...")
        run_pipeline(force_summarize=False)
    else:
        print("Startup pipeline disabled (RUN_PIPELINE_ON_START=0) to reduce free-tier memory spikes.")

    scheduler = BlockingScheduler(timezone=ZoneInfo("Asia/Kolkata"))
    scheduler.add_job(
        lambda: run_pipeline(force_summarize=False),
        "cron",
        hour="0,4,8,12,16,20",
        minute=0,
    )
    print("Scheduler: collect + summarize every 4 hours (12am, 4am, 8am, 12pm, 4pm, 8pm).")
    print("Press Ctrl+C to stop.")

    try:
        scheduler.start()
    except KeyboardInterrupt:
        print("Scheduler stopped.")
