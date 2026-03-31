DEFAULT_PROMPT = "Write ONE balanced paragraph of 5-8 lines summarizing what happened. Mention where sources had different angles. Use simple clear English. No opinion. Start directly with the news."

def get_prompt(articles_text, custom_instruction=None):
    instruction = custom_instruction if custom_instruction else DEFAULT_PROMPT
    return f"""You are a news summarizer. Below are multiple news articles about the same topic.

{instruction}

Articles:
{articles_text}

Summary:"""