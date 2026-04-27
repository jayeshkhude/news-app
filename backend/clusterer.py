import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from backend.database import get_connection
from backend.category_detect import detect_category
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import MiniBatchKMeans

IST = ZoneInfo("Asia/Kolkata")

def cluster_articles():
    conn = get_connection()
    cursor = conn.cursor()
    today = datetime.now(IST).date().isoformat()
    cursor.execute(
        """
        SELECT id, title, description, source, link, image_url
        FROM articles
        WHERE fetched_at LIKE ? OR published LIKE ?
        """,
        (f"{today}%", f"{today}%"),
    )
    articles = cursor.fetchall()
    conn.close()

    if len(articles) < 3:
        print("Not enough fresh (today) articles to cluster")
        return []

    def _plain(x):
        if x is None:
            return ""
        if isinstance(x, list):
            return " ".join(str(i) for i in x)
        return str(x)

    texts = [f"{_plain(a['title'])} {_plain(a['description'])}" for a in articles]

    vectorizer = TfidfVectorizer(stop_words='english', max_features=250)
    X = vectorizer.fit_transform(texts)

    num_clusters = min(8, len(articles) // 3)
    kmeans = MiniBatchKMeans(n_clusters=num_clusters, random_state=42, n_init=5, batch_size=128)
    kmeans.fit(X)

    clusters = {}
    for idx, label in enumerate(kmeans.labels_):
        if label not in clusters:
            clusters[label] = []
        row = dict(articles[idx])
        if isinstance(row.get("description"), list):
            row["description"] = " ".join(str(x) for x in row["description"])
        row["description"] = str(row.get("description") or "")
        if isinstance(row.get("title"), list):
            row["title"] = " ".join(str(x) for x in row["title"])
        row["title"] = str(row.get("title") or "")
        clusters[label].append(row)

    result = []
    for label, group in clusters.items():
        if len(group) >= 2:
            titles = [str(a.get("title") or "") for a in group]
            topic_title = (max(titles, key=len)[:80] if titles else "") or (titles[0][:80] if titles else "")
            blob = " ".join(
                f"{a.get('title') or ''} {a.get('description') or ''}" for a in group
            )
            category = detect_category(blob)
            result.append(
                {
                    "topic": topic_title,
                    "articles": group,
                    "category": category,
                }
            )
            print(f"Cluster {label}: {len(group)} articles [{category}] — {topic_title[:50]}")

    return result

if __name__ == "__main__":
    clusters = cluster_articles()
    print(f"\nTotal clusters: {len(clusters)}")
