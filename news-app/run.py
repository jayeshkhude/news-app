import threading
import subprocess
import sys
import os

def run_api():
    os.system(f"{sys.executable} backend/api.py")

def run_scheduler():
    os.system(f"{sys.executable} backend/scheduler.py")

if __name__ == "__main__":
    print("Starting NewsLens...")
    
    api_thread = threading.Thread(target=run_api)
    scheduler_thread = threading.Thread(target=run_scheduler)
    
    api_thread.daemon = True
    scheduler_thread.daemon = True
    
    api_thread.start()
    scheduler_thread.start()
    
    print("API running on http://localhost:5000")
    print("Scheduler running every 4 hours")
    print("Press Ctrl+C to stop everything")
    
    try:
        api_thread.join()
        scheduler_thread.join()
    except KeyboardInterrupt:
        print("\nStopped.")
