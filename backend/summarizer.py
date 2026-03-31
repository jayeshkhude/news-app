import os
import sys
import json
from groq import Groq
from datetime import datetime
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from backend.database import get_connection
from backend.clusterer import cluster_articles
from backend.prompts import get_prompt

MODEL = "llama3-8b-8192"

def get_client():
    return Groq(api_key=os.environ.get("GROQ_API_KEY"))

def summarize_cluster(cluster, custom_instruction=None):
    articles = cluster['articles']
    articles_text = ""
    for a in articles:
        articles_text += f"Source: {a['source']}\nTitle: {a['title']}\nDescription: {a['description']}\n\n"
    prompt = get_prompt(articles_text, custom_instruction)
    response = get_client().chat.completions.create(
        model=MODEL,
        messages=[{"role": "user", "content": prompt}],
        max_tokens=400
    )
    return response.choices[0].message.content

def run_summarizer(custom_instruction=None):
    clusters = cluster_articles()
    if not clusters:
        print("No clusters to summarize")
        return
    conn = get_connection()
    cursor = conn.cursor()
    today = str(datetime.now().date())
    for cluster in clusters:
        print(f"Summarizing: {cluster['topic'][:50]}...")
        try:
            summary = summarize_cluster(cluster, custom_instruction)
            sources = list(set([a['source'] for a in cluster['articles']]))
            links = [a['link'] for a in cluster['articles']]
            cursor.execute('''
                INSERT INTO summaries (topic, summary, sources, article_links, created_at, summary_date)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                cluster['topic'],
                summary,
                json.dumps(sources),
                json.dumps(links),
                str(datetime.now()),
                today
            ))
            print(f"Saved: {cluster['topic'][:50]}")
        except Exception as e:
            print(f"Error: {e}")
    conn.commit()
    conn.close()
    print("\nAll summaries done")

if __name__ == "__main__":
    run_summarizer()