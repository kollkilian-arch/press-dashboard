from typing import Dict, List, Optional
import database as db

_cache: Optional[Dict[str, List[str]]] = None


def _load():
    global _cache
    kws = db.get_keywords()
    _cache = {cat: [k["keyword"].lower() for k in words] for cat, words in kws.items()}


def invalidate():
    global _cache
    _cache = None


def classify(text: str, default: str = "sonstige") -> str:
    global _cache
    if _cache is None:
        _load()

    lower = text.lower()
    scores: Dict[str, int] = {}
    for cat, keywords in (_cache or {}).items():
        score = sum(1 for kw in keywords if kw in lower)
        if score:
            scores[cat] = score

    if not scores:
        return default
    return max(scores, key=lambda c: scores[c])
