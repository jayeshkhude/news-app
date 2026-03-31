import feedparser
import sqlite3
import os
from datetime import datetime
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from backend.database import get_connection

RSS_FEEDS = {
    "NDTV": "https://feeds.feedburner.com/NDTV-LatestNews",
    "The Hindu": "https://www.thehindu.com/news/feeder/default.rss",
    "Indian Express": "https://indianexpress.com/feed/",
    "Hindustan Times": "https://www.hindustantimes.com/feeds/rss/topstories/rssfeed.xml",
    "Times of India": "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "BBC India": "https://feeds.bbci.co.uk/news/world/asia/india/rss.xml",
}

def collect_articles():
    conn = get_connection()
    cursor = conn.cursor()
    total = 0

    for source, url in RSS_FEEDS.items():
        print(f"Fetching from {source}...")
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title = entry.get('title', '')
                link = entry.get('link', '')
                description = entry.get('summary', '')
                published = entry.get('published', str(datetime.now()))

                try:
                    cursor.execute('''
                        INSERT OR IGNORE INTO articles 
                        (title, link, source, description, published, fetched_at)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (title, link, source, description, published, str(datetime.now())))
                    if cursor.rowcount > 0:
                        total += 1
                except Exception as e:
                    print(f"DB error: {e}")

        except Exception as e:
            print(f"Error fetching {source}: {e}")

    conn.commit()
    conn.close()
    print(f"Done. {total} new articles saved.")
    return total
    
if __name__ == "__main__":
    collect_articles()