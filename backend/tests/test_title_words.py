"""Offline tests for overused title-word detection (no Mongo/Gemini/Printify calls).

Run: pytest backend/tests/test_title_words.py
"""
import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_server import server  # noqa: E402
import bulk_seo_update  # noqa: E402
import title_words  # noqa: E402

# 10 recent titles: "Cybernetic" in 7, "Decaying" in 4, "Hymn" and "Shrine" in 2 (under threshold)
RECENT = [
    "Decaying Saint - Cybernetic Halo Tee | Gothic Streetwear | Cyberpunk Techwear",
    "Iron Vigil | Cybernetic Monk Back Print | Dark Industrial Goth Shirt",
    "Decaying Choir - Gothic Back Print Tee | Industrial Streetwear",
    "Wired Seraph | Cyber Goth Back Print Tee | Cybernetic Wings | Industrial Techno",
    "Ash Hymn - Decaying Cathedral Tee | Dark Alt Streetwear",
    "Null Psalm Tee | Cybernetic Antenna Shrine | Techwear Gothic",
    "Glitch Relic - Cybernetic Reliquary Shirt | Cyberpunk Gothic",
    "Rust Hymn | Decaying Engine Shrine Tee | Gothic Industrial",
    "Static Crown - Cybernetic Crown Oversized Tee | Dark Streetwear",
    "Bone Circuit | Cybernetic cybernetic Ossuary | Gothic Shirt",  # counted once per title
]


def test_finds_words_in_3_plus_titles_most_frequent_first():
    assert title_words.find_overused_words(RECENT) == ["Cybernetic", "Decaying"]  # "Shrine"/"Hymn" only in 2


def test_counts_once_per_title_and_case_insensitive():
    titles = ["Hollow Hollow HOLLOW Tee", "hollow saint", "Iron Vigil"]
    assert title_words.find_overused_words(titles) == []
    assert title_words.find_overused_words(titles + ["The Hollow Hours"]) == ["Hollow"]


def test_brand_and_seo_keywords_never_flagged():
    titles = ["Gothic Streetwear Tee Shirt Cyberpunk Techwear Goth Industrial Dark"] * 10
    assert title_words.find_overused_words(titles) == []


def test_stopwords_and_short_words_ignored():
    assert title_words.find_overused_words(["The X of a Tee and"] * 5) == []


def test_formula_vocabulary_excluded_in_server():
    # Words fixed in the title formulas repeat by design
    assert {"oversized", "midnight", "back", "print", "heavy", "cotton"} <= server.FORMULA_WORDS
    names = ["Iron Vigil", "Ash Psalm", "Bone Choir", "Rust Saint", "Null Hour",
             "Glass Relic", "Wire Halo", "Salt Moth", "Lead Bell", "Coal Wake"]
    hints = ["Monastic", "Occult", "Hydraulic", "Graveyard", "Folk Horror",
             "Bone Church", "Storm Liturgy", "Crow Sermon", "Wire Crown", "Ash Liturgy"]
    titles = [f.format(capsule_name=n, theme_hint=h) for f, n, h in zip(server.SEO_TITLE_FORMULAS, names, hints)]
    assert server.find_overused_words(titles, extra_exclude=server.FORMULA_WORDS) == []


def test_system_prompt_lists_overused_words():
    prompt = server.build_text_system_prompt([], server.SEO_TITLE_FORMULAS[0], overused_words=["Cybernetic", "Decaying"])
    assert "OVERUSED WORDS TO AVOID THIS TIME: 'Cybernetic', 'Decaying'" in prompt
    assert "OVERUSED" not in server.build_text_system_prompt([], server.SEO_TITLE_FORMULAS[0])


def test_banned_words_feature_unchanged():
    prompt = server.build_text_system_prompt(["blood"], server.SEO_TITLE_FORMULAS[0], overused_words=["Decaying"])
    assert "ABSOLUTELY DO NOT use any of these banned words" in prompt and "'blood'" in prompt


@pytest.fixture
def db(monkeypatch):
    mongomock_motor = pytest.importorskip("mongomock_motor")
    database = mongomock_motor.AsyncMongoMockClient()["t"]
    monkeypatch.setattr(server, "capsules_coll", database.capsules)
    monkeypatch.setattr(server, "settings_coll", database.settings)
    return database


def test_generate_capsule_passes_overused_words(db, monkeypatch):
    docs = [{"id": f"c{i}", "capsule_name": t.split(" ")[0] + f" {i}", "title": t, "created_at": f"2026-10-0{i % 9}"}
            for i, t in enumerate(RECENT)]
    asyncio.run(db.capsules.insert_many(docs))
    seen = {}

    async def fake_text(*args, **kwargs):
        seen.update(kwargs)
        return {"capsule_name": "Fresh Name", "title": "Fresh Name Tee", "front_concept": "f", "back_concept": "b", "tags": []}

    async def fake_image(prompt):
        return None

    monkeypatch.setattr(server, "llm_generate_text", fake_text)
    monkeypatch.setattr(server, "llm_generate_image", fake_image)
    asyncio.run(server._generate_capsule({"active_theme": "auto"}))
    assert seen["overused_words"][:2] == ["Cybernetic", "Decaying"]
    assert len(seen["banned_names"]) <= 15


class FakeModels:
    def __init__(self):
        self.prompts = []

    def generate_content(self, **kwargs):
        self.prompts.append(kwargs["contents"])
        return types.SimpleNamespace(text=json.dumps({"title": "T", "tags": ["Goth Tee"]}))


def test_generate_seo_prompt_avoids_overused_except_capsule_name(monkeypatch):
    models = FakeModels()
    monkeypatch.setattr(bulk_seo_update, "genai_client", types.SimpleNamespace(models=models))
    asyncio.run(bulk_seo_update.generate_seo("Decaying Saint", "a saint", overused_words=["Cybernetic", "Decaying"]))
    prompt = models.prompts[-1]
    assert "OVERUSED WORDS TO AVOID THIS TIME: 'Cybernetic'." in prompt  # 'Decaying' is in the capsule name
    asyncio.run(bulk_seo_update.generate_seo("Iron Vigil", "a monk"))
    assert "OVERUSED" not in models.prompts[-1]


def test_recent_seo_overused_words_prefers_live_seo_titles(db):
    docs = [{"id": f"a{i}", "status": "approved", "approved_at": f"2026-10-{10 + i}",
             "title": "Generator Title", "seo_title": t} for i, t in enumerate(RECENT)]
    asyncio.run(db.capsules.insert_many(docs))
    assert asyncio.run(server.recent_seo_overused_words())[:2] == ["Cybernetic", "Decaying"]
