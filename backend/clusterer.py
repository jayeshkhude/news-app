import os
import sys
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from backend.database import get_connection
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.cluster import KMeans
import json
from datetime import datetime, date

def cluster_articles():
    conn = get_connection()
    cursor = conn.cursor()

    cursor.execute('''
        SELECT id, title, description, source, link 
        FROM articles 
        WHERE fetched_at >= date('now', '-1 day')
    ''')
    articles = cursor.fetchall()
    conn.close()

    if len(articles) < 3:
        print("Not enough articles to cluster")
        return []

    texts = [f"{a['title']} {a['description']}" for a in articles]

    vectorizer = TfidfVectorizer(stop_words='english', max_features=500)
    X = vectorizer.fit_transform(texts)

    num_clusters = min(8, len(articles) // 3)
    kmeans = KMeans(n_clusters=num_clusters, random_state=42, n_init=10)
    kmeans.fit(X)

    clusters = {}
    for idx, label in enumerate(kmeans.labels_):
        if label not in clusters:
            clusters[label] = []
        clusters[label].append(dict(articles[idx]))

    result = []
    for label, group in clusters.items():
        if len(group) >= 2:
            topic_title = group[0]['title'][:60]
            result.append({
                'topic': topic_title,
                'articles': group
            })
            print(f"Cluster {label}: {len(group)} articles - {topic_title}")

    return result

if __name__ == "__main__":
    clusters = cluster_articles()
    print(f"\nTotal clusters: {len(clusters)}")
