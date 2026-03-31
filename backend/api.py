from flask import Flask, jsonify, request
from flask_cors import CORS
import json
import os
import sys
import requests as req
sys.path.append(os.path.dirname(os.path.dirname(__file__)))
from backend.database import get_connection
from backend.prompts import get_prompt

app = Flask(__name__)
CORS(app)

@app.route('/api/trending', methods=['GET'])
def get_trending():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, topic, summary, sources, article_links, created_at
        FROM summaries
        WHERE summary_date >= date('now', '-1 day')
        ORDER BY id DESC
        LIMIT 8
    ''')
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        result.append({
            'id': row['id'],
            'topic': row['topic'],
            'summary': row['summary'],
            'sources': json.loads(row['sources']),
            'links': json.loads(row['article_links']),
            'created_at': row['created_at']
        })

    return jsonify(result)

@app.route('/api/summary/<int:summary_id>', methods=['GET'])
def get_summary(summary_id):
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM summaries WHERE id = ?', (summary_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'Not found'}), 404

    return jsonify({
        'id': row['id'],
        'topic': row['topic'],
        'summary': row['summary'],
        'sources': json.loads(row['sources']),
        'links': json.loads(row['article_links']),
        'created_at': row['created_at']
    })

@app.route('/api/search', methods=['GET'])
def search():
    query = request.args.get('q', '')
    if not query:
        return jsonify([])

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, topic, summary, sources, article_links, created_at
        FROM summaries
        WHERE topic LIKE ? OR summary LIKE ?
        ORDER BY created_at DESC
        LIMIT 10
    ''', (f'%{query}%', f'%{query}%'))
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        result.append({
            'id': row['id'],
            'topic': row['topic'],
            'summary': row['summary'],
            'sources': json.loads(row['sources']),
            'links': json.loads(row['article_links']),
            'created_at': row['created_at']
        })

    return jsonify(result)

@app.route('/api/archive', methods=['GET'])
def get_archive():
    date = request.args.get('date', '')
    if not date:
        return jsonify([])

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, topic, summary, sources, article_links, created_at
        FROM summaries
        WHERE summary_date = ?
        ORDER BY id DESC
    ''', (date,))
    rows = cursor.fetchall()
    conn.close()

    result = []
    for row in rows:
        result.append({
            'id': row['id'],
            'topic': row['topic'],
            'summary': row['summary'],
            'sources': json.loads(row['sources']),
            'links': json.loads(row['article_links']),
            'created_at': row['created_at']
        })

    return jsonify(result)

@app.route('/api/summarize/custom', methods=['POST'])
def custom_summarize():
    data = request.get_json()
    summary_id = data.get('summary_id')
    custom_instruction = data.get('instruction', '').strip()

    if not summary_id or not custom_instruction:
        return jsonify({'error': 'Missing summary_id or instruction'}), 400

    if len(custom_instruction) > 200:
        return jsonify({'error': 'Prompt too long. Max 200 characters.'}), 400

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM summaries WHERE id = ?', (summary_id,))
    row = cursor.fetchone()
    conn.close()

    if not row:
        return jsonify({'error': 'Summary not found'}), 404

    articles_text = f"Topic: {row['topic']}\nExisting Summary: {row['summary']}"
    prompt = get_prompt(articles_text, custom_instruction)

    response = req.post("http://localhost:11434/api/generate", json={
        "model": "llama3.2:3b",
        "prompt": prompt,
        "stream": False
    })

    new_summary = response.json()['response']

    return jsonify({
        'topic': row['topic'],
        'summary': new_summary,
        'custom': True
    })

chat_rate_limit = {}

@app.route('/api/chat/messages', methods=['GET'])
def get_messages():
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, message, sent_at
        FROM chat_messages
        ORDER BY id DESC
        LIMIT 50
    ''')
    rows = cursor.fetchall()
    conn.close()

    result = [{'id': r['id'], 'message': r['message'], 'sent_at': r['sent_at']} for r in rows]
    result.reverse()
    return jsonify(result)

@app.route('/api/chat/send', methods=['POST'])
def send_message():
    import time
    data = request.get_json()
    message = data.get('message', '').strip()
    user_id = request.remote_addr

    if not message:
        return jsonify({'error': 'Empty message'}), 400

    if len(message) > 200:
        return jsonify({'error': 'Max 200 characters'}), 400

    now = time.time()
    history = chat_rate_limit.get(user_id, [])
    history = [t for t in history if now - t < 300]

    if len(history) >= 2:
        wait = int(300 - (now - history[0]))
        return jsonify({'error': f'Limit reached. Wait {wait} seconds.'}), 429

    history.append(now)
    chat_rate_limit[user_id] = history

    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('INSERT INTO chat_messages (message, sent_at) VALUES (?, ?)',
                   (message, str(__import__('datetime').datetime.now())))
    conn.commit()
    conn.close()

    return jsonify({'success': True})
@app.route('/api/status', methods=['GET'])
def status():
    import time
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT created_at FROM summaries ORDER BY id DESC LIMIT 1')
    row = cursor.fetchone()
    conn.close()
    
    last_update = row['created_at'] if row else None
    
    next_update = None
    if last_update:
        from datetime import datetime, timedelta
        last_dt = datetime.fromisoformat(last_update)
        next_dt = last_dt + timedelta(hours=4)
        next_update = next_dt.strftime("%I:%M %p")
    
    return jsonify({
        'last_update': last_update,
        'next_update': next_update,
        'status': 'live'
    })
if __name__ == "__main__":
    app.run(debug=True, port=5000)