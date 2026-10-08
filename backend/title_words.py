"""Detect content words that recent product titles keep repeating (e.g. "Decaying" in 4 of 10).

Shared by server.py (capsule generation) and bulk_seo_update.py (SEO titles), which
pass the result to their prompts as "overused words to avoid this time".
"""
import re
from collections import Counter
from typing import Iterable, List

RECENT_TITLES_WINDOW = 20
OVERUSED_MIN_TITLES = 3

STOPWORDS = {
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "into", "is", "it", "its", "of", "on",
    "or", "the", "to", "with", "without", "your", "you", "his", "her", "him", "our", "this", "that",
}

# Brand / SEO vocabulary that titles repeat on purpose
SEO_KEYWORDS = {
    "gothic", "goth", "goths", "streetwear", "tee", "tees", "shirt", "shirts", "tshirt", "t-shirt",
    "cyberpunk", "techwear", "industrial", "dark", "cyber", "cybergoth", "techno", "rave",
    "oversized", "back", "print", "graphic", "heavy", "cotton", "unisex", "alt", "alternative",
    "y2k", "grunge", "midnight", "rotation", "midnightrotation", "drop", "gift", "gifts", "idea",
    "academia", "aesthetic", "clothing", "apparel", "fashion", "style", "wear", "top", "men",
    "women", "mens", "womens",
}

_WORD_RE = re.compile(r"[A-Za-z][A-Za-z'-]*[A-Za-z]|[A-Za-z]")


def words(text: str) -> List[str]:
    return [w.lower() for w in _WORD_RE.findall(text or "")]


def find_overused_words(
    titles: Iterable[str],
    min_titles: int = OVERUSED_MIN_TITLES,
    extra_exclude: Iterable[str] = (),
) -> List[str]:
    """Content words appearing in at least `min_titles` of the given titles, most frequent first.

    Counts each word once per title. Stopwords, brand/SEO keywords, extra_exclude
    (e.g. words baked into the title formulas) and words under 3 letters are ignored.
    """
    exclude = STOPWORDS | SEO_KEYWORDS | {w.lower() for w in extra_exclude}
    counts = Counter()
    for title in titles:
        counts.update({w for w in words(title) if len(w) >= 3 and w not in exclude})
    overused = [(w, n) for w, n in counts.items() if n >= min_titles]
    overused.sort(key=lambda wn: (-wn[1], wn[0]))
    return [w.capitalize() for w, _ in overused]


def overused_words_instruction(overused: List[str]) -> str:
    joined = ", ".join(f"'{w}'" for w in overused)
    return (
        f"OVERUSED WORDS TO AVOID THIS TIME: {joined}. These already appear in "
        f"{OVERUSED_MIN_TITLES}+ of the last {RECENT_TITLES_WINDOW} product titles. Do not use them "
        "or close variants (e.g. 'Decay'/'Decayed' for 'Decaying') - choose fresh, specific vocabulary."
    )
