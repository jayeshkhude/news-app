"""Lightweight topic bucket from text — no LLM."""

import re

# (category, keyword substrings)
_KEYWORDS = [
    (
        "politics",
        (
            "election", "minister", "parliament", "lok sabha", "rajya sabha",
            "bjp", "congress", "government", "cabinet", "vote", "campaign",
            "supreme court", "high court", "verdict", "bill ", "policy",
            "opposition", "chief minister", "governor", "assembly",
        ),
    ),
    (
        "economy",
        (
            "market", "stock", "sensex", "nifty", "rupee", "gdp", "inflation",
            "rbi", "reserve bank", "bank", "loan", "interest rate", "fiscal",
            "trade", "export", "import", "economy", "investment", "startup funding",
        ),
    ),
    (
        "world",
        (
            "un ", "united nations", "nato", "foreign", "diplomat", "embassy",
            "ukraine", "gaza", "israel", "china", "us president", "white house",
            "european", "summit", "global", "international",
        ),
    ),
    (
        "sports",
        (
            "cricket", "ipl", "match", "tournament", "olympic", "fifa",
            "tennis", "badminton", "hockey", "football", "basketball",
            "world cup", "championship", "player", "coach", "stadium",
        ),
    ),
    (
        "science_tech",
        (
            "ai ", " artificial intelligence", "tech", "software", "space",
            "nasa", "isro", "launch", "satellite", "climate study", "research",
            "scientists", "vaccine", "health study", "cyber", "chip",
        ),
    ),
    (
        "society",
        (
            "festival", "weather", "cyclone", "flood", "earthquake", "accident",
            "fire ", "culture", "film release", "celebrity", "education",
            "school", "university admission", "traffic",
        ),
    ),
]


def detect_category(text: str) -> str:
    if not text:
        return "other"
    t = " " + re.sub(r"\s+", " ", text.lower()) + " "
    best = "other"
    best_score = 0
    for cat, words in _KEYWORDS:
        score = sum(1 for w in words if w in t)
        if score > best_score:
            best_score = score
            best = cat
    return best if best_score > 0 else "other"
