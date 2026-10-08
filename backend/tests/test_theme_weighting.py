"""Offline tests for theme selection and title formulas (no Mongo/Gemini/Printify calls).

Run: pytest backend/tests/test_theme_weighting.py
"""
import os
import random
import sys
from collections import Counter
from pathlib import Path

import pytest

# Dummy env set before import so server.py never needs real secrets (load_dotenv won't override these).
for _k, _v in {
    "MONGO_URL": "mongodb://localhost:1",
    "DB_NAME": "offline_test",
    "GEMINI_API_KEY": "offline-dummy",
    "ADMIN_API_KEY": "offline-dummy",
    "CORS_ORIGINS": "http://localhost:3000",
}.items():
    os.environ.setdefault(_k, _v)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import server  # noqa: E402

PICKS = 1000
LONGEST_CAPSULE_NAME = "X" * 30
ETSY_TITLE_LIMIT = 140


def simulate(settings: dict, n: int = PICKS, seed: int = 1234) -> list:
    """Run resolve_theme n times, updating recently_used_themes like _generate_capsule does."""
    random.seed(seed)
    settings = dict(settings)
    picks = []
    for _ in range(n):
        theme = server.resolve_theme(settings)
        picks.append(theme)
        settings["recently_used_themes"] = server.record_theme_use(
            settings.get("recently_used_themes") or [], theme["key"]
        )
    return picks


def test_auto_mode_is_roughly_60_40_cyber_gothic():
    picks = simulate({"active_theme": "auto", "niche_weight": 0.7})
    cats = Counter(server.theme_category(t) for t in picks)
    cyber_share = cats["cyber"] / PICKS
    print(f"cyber={cats['cyber']} gothic={cats['gothic']} ({cyber_share:.1%} cyber)")
    assert 0.55 <= cyber_share <= 0.65


def test_cyber_weight_constant_drives_split():
    # Fresh picks (no recently-used history) so anti-repeat fallback doesn't kick in
    original = server.CYBER_THEME_WEIGHT
    try:
        random.seed(99)
        server.CYBER_THEME_WEIGHT = 0.0
        assert all(server.theme_category(server.resolve_theme({"active_theme": "auto"})) == "gothic" for _ in range(200))
        server.CYBER_THEME_WEIGHT = 1.0
        assert all(server.theme_category(server.resolve_theme({"active_theme": "auto"})) == "cyber" for _ in range(200))
    finally:
        server.CYBER_THEME_WEIGHT = original


def test_anti_repeat_within_category():
    picks = simulate({"active_theme": "auto"})
    window = max(len(server.DEFAULT_THEMES) // 2, 5)
    # A theme should never repeat while it's still in the recently-used window
    # (unless both categories were exhausted, which the pool sizes make impossible).
    last_seen = {}
    for i, t in enumerate(picks):
        if t["key"] in last_seen:
            assert i - last_seen[t["key"]] >= window, t["key"]
        last_seen[t["key"]] = i


def test_falls_back_to_other_category_when_exhausted():
    cyber_keys = [t["key"] for t in server.DEFAULT_THEMES if t["category"] == "cyber"]
    original = server.CYBER_THEME_WEIGHT
    try:
        server.CYBER_THEME_WEIGHT = 1.0  # always prefer cyber...
        random.seed(7)
        for _ in range(50):
            t = server.resolve_theme({"active_theme": "auto", "recently_used_themes": cyber_keys})
            assert server.theme_category(t) == "gothic"  # ...but every cyber theme is recently used
    finally:
        server.CYBER_THEME_WEIGHT = original


def test_niche_bias_still_applies_within_gothic():
    picks = simulate({"active_theme": "auto", "niche_weight": 1.0, "recently_used_themes": []}, n=2000)
    gothic = [t for t in picks if server.theme_category(t) == "gothic"]
    niche_share = sum(t["key"] in server.NICHE_THEME_KEYS for t in gothic) / len(gothic)
    plain = simulate({"active_theme": "auto", "niche_weight": 0.0}, n=2000)
    plain_gothic = [t for t in plain if server.theme_category(t) == "gothic"]
    plain_share = sum(t["key"] in server.NICHE_THEME_KEYS for t in plain_gothic) / len(plain_gothic)
    assert niche_share > plain_share


def test_theme_catalog():
    keys = [t["key"] for t in server.DEFAULT_THEMES]
    assert len(keys) == len(set(keys))
    assert all(t["category"] in ("cyber", "gothic") for t in server.DEFAULT_THEMES)
    cats = Counter(t["category"] for t in server.DEFAULT_THEMES)
    assert cats["cyber"] >= 15
    for k in ("techno_gothic", "neon_mortuary", "y2k_gothic"):
        assert next(t for t in server.DEFAULT_THEMES if t["key"] == k)["category"] == "cyber"
    assert set(keys) <= set(server.THEME_HINT_OVERRIDES)


@pytest.mark.parametrize("theme,expected", [
    ({"name": "Cyber Saints", "prompt": "chrome halos"}, "cyber"),
    ({"name": "Rust Psalm", "prompt": "cybernetic monks"}, "cyber"),
    ({"name": "Rust Psalm", "prompt": "iron monks"}, "gothic"),
])
def test_custom_theme_category(theme, expected):
    assert server.theme_category(theme) == expected


def test_custom_themes_join_auto_pool():
    settings = {
        "active_theme": "auto",
        "custom_themes": [{"name": "Cyber Hymn", "prompt": "x"}, {"name": "Moss Altar", "prompt": "y"}],
    }
    keys = {t["key"] for t in simulate(settings, n=500)}
    assert "custom:Cyber Hymn" in keys and "custom:Moss Altar" in keys


def test_title_formulas_by_category():
    assert server.title_formulas_for("cyber") is server.CYBER_SEO_TITLE_FORMULAS
    assert server.title_formulas_for("gothic") is server.SEO_TITLE_FORMULAS
    assert len(server.SEO_TITLE_FORMULAS) == 10
    assert 3 <= len(server.CYBER_SEO_TITLE_FORMULAS) <= 4
    terms = ("cyber goth", "cybergoth", "cyberpunk", "techwear", "industrial techno")
    for f in server.CYBER_SEO_TITLE_FORMULAS:
        assert any(term in f.lower() for term in terms), f


def test_titles_fit_etsy_limit():
    hints = list(server.THEME_HINT_OVERRIDES.values()) + [
        server.default_theme_hint("cyber"), server.default_theme_hint("gothic"),
    ]
    longest_hint = max(hints, key=len)
    for f in server.SEO_TITLE_FORMULAS + server.CYBER_SEO_TITLE_FORMULAS:
        assert "{capsule_name}" in f
        title = f.format(capsule_name=LONGEST_CAPSULE_NAME, theme_hint=longest_hint)
        assert len(title) <= ETSY_TITLE_LIMIT, (len(title), title)


def test_prompts_keep_white_on_black_and_required_tags():
    prompt = server.build_text_system_prompt([], server.SEO_TITLE_FORMULAS[0])
    assert "stark white ink on solid black" in prompt
    assert "The front is a whisper; the back is a scream." in prompt
    assert "'Gothic Streetwear' and 'Back Print Shirt'" in prompt
    assert "cyber goth" in prompt.lower()
    back = server.build_back_prompt("x")
    assert "#000000" in back and "no color" in back and "circuit linework" in back
