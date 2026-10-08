"""Offline tests for Etsy's 20-character tag limit (no Mongo/Gemini/Printify/Etsy calls).

Run: pytest backend/tests/test_etsy_tags.py
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

LONG = "Industrial Gothic Streetwear Tee"  # 32 chars


class FakeModels:
    def __init__(self, payload):
        self.text = json.dumps(payload)

    def generate_content(self, **kwargs):
        return types.SimpleNamespace(text=self.text)


def fake_genai(payload):
    return types.SimpleNamespace(models=FakeModels(payload))


def assert_valid(tags):
    assert all(len(t) <= server.ETSY_TAG_MAX_LEN for t in tags), tags
    assert len({t.lower() for t in tags}) == len(tags), tags


# ---- helpers -----------------------------------------------------------------
def test_constants_fit_limit():
    for t in server.REQUIRED_TAGS + server.FALLBACK_TAGS["cyber"] + server.FALLBACK_TAGS["gothic"]:
        assert len(t) <= server.ETSY_TAG_MAX_LEN, t


def test_filter_drops_long_and_dupes_without_truncating():
    out = server.filter_etsy_tags(["Goth Tee", LONG, "goth tee", "  Rave Shirt  ", "", "X" * 20, "X" * 21])
    assert out == ["Goth Tee", "Rave Shirt", "X" * 20]
    assert not any(LONG.startswith(t) and t != LONG for t in out)  # nothing cut mid-word


def test_finalize_keeps_required_first_and_fills_to_13_with_category_fallbacks():
    tags = server.finalize_tags([LONG, "Glitch Saint Tee", "glitch saint tee", "back print shirt"], "cyber")
    assert tags[:2] == server.REQUIRED_TAGS
    assert len(tags) == 13
    assert_valid(tags)
    assert "Glitch Saint Tee" in tags and LONG not in tags
    assert set(server.FALLBACK_TAGS["cyber"]) <= set(tags)  # own category used before the other


def test_finalize_gothic_uses_gothic_fallbacks_first():
    tags = server.finalize_tags(["Bone Church Tee"], "gothic")
    assert tags[:3] == server.REQUIRED_TAGS + ["Bone Church Tee"]
    assert tags[3:8] == server.FALLBACK_TAGS["gothic"]
    assert len(tags) == 13


def test_finalize_caps_at_13_and_prefers_llm_tags_over_fallbacks():
    llm = [f"Tag Number {i}" for i in range(20)]
    tags = server.finalize_tags(llm, "cyber")
    assert len(tags) == 13 and tags[2:] == llm[:11]


def test_finalize_never_exceeds_available_tags():
    # Required + 5 own + 5 other fallbacks = 12 unique: accept fewer than 13 rather than invent tags
    tags = server.finalize_tags([LONG] * 13, "cyber")
    assert len(tags) == 12
    assert_valid(tags)


# ---- llm_generate_text -------------------------------------------------------
@pytest.mark.parametrize("category", ["cyber", "gothic"])
def test_llm_generate_text_enforces_limit(monkeypatch, category):
    payload = {
        "capsule_name": "Glitch Saint", "title": "t", "front_concept": "f", "back_concept": "b",
        "tags": ["Gothic Streetwear", LONG, "Cyberpunk Gothic Shirt Gift", "Glitch Saint Tee",
                 "GLITCH SAINT TEE", "Back Print Shirt"],
    }
    monkeypatch.setattr(server, "genai_client", fake_genai(payload))
    data = asyncio.run(server.llm_generate_text("seed", [], category=category))
    tags = data["tags"]
    assert tags[:2] == server.REQUIRED_TAGS
    assert len(tags) == 13
    assert_valid(tags)
    assert "Glitch Saint Tee" in tags
    assert tags[3:8] == server.FALLBACK_TAGS[category]


# ---- generate_seo (bulk_seo_update) --------------------------------------------
def test_generate_seo_drops_instead_of_truncating(monkeypatch):
    payload = {"title": "t", "tags": [LONG, "Bone Church Tee", "bone church tee", "Goth Gift For Him"]}
    monkeypatch.setattr(bulk_seo_update, "genai_client", fake_genai(payload))
    data = asyncio.run(bulk_seo_update.generate_seo("Bone Church", "ossuary"))
    tags = data["tags"]
    assert_valid(tags)
    assert LONG[:20] not in tags  # the old t[:20] behavior produced "Industrial Gothic St"
    assert {"Gothic Streetwear", "Back Print Shirt", "Bone Church Tee", "Goth Gift For Him"} == set(tags)


# ---- approve endpoint ------------------------------------------------------------
@pytest.fixture
def mock_db(monkeypatch):
    mongomock_motor = pytest.importorskip("mongomock_motor")
    db = mongomock_motor.AsyncMongoMockClient()["t"]
    capsules, settings = db.capsules, db.settings
    monkeypatch.setattr(server, "capsules_coll", capsules)
    monkeypatch.setattr(server, "settings_coll", settings)
    return capsules, settings


def make_capsule():
    return server.Capsule(
        capsule_name="Glitch Saint", title="t", description="d", front_concept="f", back_concept="b",
        tags=["Gothic Streetwear"], front_image_b64="QUFB", back_image_b64="QkJC",
    ).model_dump()


def test_approve_filters_edited_tags(mock_db):
    capsules, _ = mock_db
    cap = make_capsule()
    asyncio.run(capsules.insert_one(dict(cap)))
    edited = ["My Edited Tag", LONG, "my edited tag", "Cyber Goth Shirt"]
    res = asyncio.run(server.approve_capsule(cap["id"], server.ApprovePayload(tags=edited), None))
    # Pure filter: no required tags re-added, no padding
    assert res.tags == ["My Edited Tag", "Cyber Goth Shirt"]


def test_approve_auto_push_filters_seo_tags_sent_to_printify(mock_db, monkeypatch):
    capsules, settings = mock_db
    cap = make_capsule()
    asyncio.run(capsules.insert_one(dict(cap)))
    asyncio.run(settings.insert_one({
        "id": "config", "printify_auto_push": True, "printify_shop_id": 1, "printify_print_provider_id": 2,
    }))
    sent = {}

    async def fake_push(**kw):
        return {"id": "pid123"}

    async def fake_seo(name, concept):
        return {"title": "New Title", "tags": ["Gothic Streetwear", LONG, "Rave Goth Tee", "rave goth tee"]}

    async def fake_update(shop_id, pid, body):
        sent.update(body)

    async def noop(*a, **k):
        return None

    monkeypatch.setattr(server, "push_capsule_as_draft", fake_push)
    monkeypatch.setattr(server, "generate_seo", fake_seo)
    monkeypatch.setattr(server, "update_product", fake_update)
    monkeypatch.setattr(server, "prioritize_back_mockup", noop)
    monkeypatch.setattr(server, "publish_product", noop)
    # Block the best-effort Etsy atmospheric step so no real Etsy/Gemini call can happen
    monkeypatch.setitem(sys.modules, "generate_mockups", types.ModuleType("generate_mockups"))
    monkeypatch.setitem(sys.modules, "upload_etsy_images", types.ModuleType("upload_etsy_images"))

    asyncio.run(server.approve_capsule(cap["id"], None, None))
    assert sent["tags"] == ["Gothic Streetwear", "Rave Goth Tee"]
