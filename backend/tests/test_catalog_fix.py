"""Offline tests for catalog_fix.py and the AI disclosure in descriptions."""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_server import server  # noqa: E402,F401
import catalog_fix  # noqa: E402
import etsy_photos  # noqa: E402
import seo  # noqa: E402


# ---------- disclosure ----------
def test_description_discloses_ai_and_mockups():
    d = seo.build_description("Ash Communion", "a burnt chalice")
    assert "created with AI-assisted design tools" in d and "finalized by hand" in d
    assert seo.PHOTO_NOTE_MOCKUPS in d and "AI-generated illustration" not in d
    d2 = seo.build_description("Ash Communion", "a burnt chalice", ai_scene_photo=True)
    assert "styled scene photo, which is an AI-generated illustration" in d2
    assert seo.extract_back_concept(d2) == "a burnt chalice"


# ---------- catalog fix ----------
@pytest.fixture
def db(monkeypatch):
    mongomock_motor = pytest.importorskip("mongomock_motor")
    database = mongomock_motor.AsyncMongoMockClient()["t"]
    asyncio.run(database.settings.insert_one({"id": "config", "printify_shop_id": 7}))

    async def no_sleep(_):
        return None

    monkeypatch.setattr(catalog_fix, "_sleep", no_sleep)
    return database


OLD_DESC = ("THE GRIND // IRON CHAPEL\n\nA premium staple... and an aggressive, oversized "
            "iron chapel of pistons filling the back.")


def products():
    return [
        {"id": "p1", "title": "Iron Chapel | Midnight Rotation Drop | Gothic Back Print Heavy Tee",
         "description": OLD_DESC, "tags": [], "external": {"id": "L1"}, "images": []},
        {"id": "p2", "title": "Mystery", "description": "no concept here", "tags": [],
         "external": {"id": "L2"}, "images": []},
        {"id": "p3", "title": "Static Rite Tee | Cyberpunk Gothic Back Print", "tags": ["Cyber Goth Shirt"],
         "description": OLD_DESC.replace("iron chapel of pistons", "a glitching saint"), "external": None, "images": []},
    ]


@pytest.fixture
def fakes(monkeypatch):
    calls = {"update": [], "publish": [], "arrange": [], "seo": []}

    async def fake_list(shop_id, page=1, limit=50):
        return {"data": products() if page == 1 else []}

    async def fake_update(shop_id, pid, body):
        calls["update"].append((pid, body))

    async def fake_publish(shop_id, pid):
        calls["publish"].append(pid)

    async def fake_seo(name, back, overused_words=None, category=None):
        calls["seo"].append((name, back, category))
        return {"title": f"Oversized Goth Shirt, Back Print Graphic Tee - {name} | Dark", "tags": ["Back Print Shirt"]}

    async def fake_first(auth, listing_id, product):
        return (99, "rank 1 was a generated photo") if listing_id == "L1" else (None, "rank 1 is a Printify mockup")

    async def fake_arrange(auth, listing_id, back_src, ids):
        calls["arrange"].append((listing_id, ids))
        return "arranged"

    class FakeAuth:
        def __init__(self, coll):
            pass

        async def load(self):
            return None

    monkeypatch.setattr(catalog_fix, "list_products", fake_list)
    monkeypatch.setattr(catalog_fix, "update_product", fake_update)
    monkeypatch.setattr(catalog_fix, "publish_product_text", fake_publish)
    monkeypatch.setattr(catalog_fix, "generate_seo", fake_seo)
    monkeypatch.setattr(etsy_photos, "generated_first_image_id", fake_first)
    monkeypatch.setattr(etsy_photos, "arrange_listing_photos", fake_arrange)
    monkeypatch.setattr(etsy_photos, "EtsyAuth", FakeAuth)
    return calls


def test_fix_all(db, fakes):
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules))
    assert status["state"] == "done" and status["total"] == 3 and status["done"] == 3 and status["failed"] == 0
    # Photos: old generated photo on L1 moved to rank 4; L2 just reordered; p3 not on Etsy
    assert fakes["arrange"] == [("L1", [99]), ("L2", [])]
    # Text: p1 and p3 rewritten (p2 has no back concept); only Etsy-linked p1 re-synced
    assert [pid for pid, _ in fakes["update"]] == ["p1", "p3"]
    assert fakes["publish"] == ["p1"]
    body = dict(fakes["update"])["p1"]
    assert body["title"].startswith("Oversized Goth Shirt") and "Iron Chapel" in body["title"]
    assert "AI-generated illustration" in body["description"]  # L1 keeps its old AI photo at rank 4
    assert "AI-generated illustration" not in dict(fakes["update"])["p3"]["description"]
    assert fakes["seo"][1][2] == "cyber"
    stored = asyncio.run(catalog_fix.get_status(db.settings))
    assert stored["state"] == "done" and any("text: skipped" in line for line in stored["log"])


def test_limit_one_tests_newest_only(db, fakes):
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, limit=1))
    assert status["total"] == 1 and [pid for pid, _ in fakes["update"]] == ["p1"]


def test_uses_capsule_data_and_updates_capsule(db, fakes):
    asyncio.run(db.capsules.insert_one({"id": "c1", "printify_product_id": "p2", "capsule_name": "Null Hour",
                                         "back_summary": "a dead clock tower", "theme_seed": "Late-Night Void"}))
    asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules))
    assert ("Null Hour", "a dead clock tower", "gothic") in fakes["seo"]
    cap = asyncio.run(db.capsules.find_one({"id": "c1"}))
    assert cap["seo_title"].endswith("Null Hour | Dark") and "AI-assisted" in cap["seo_description"]


def test_per_product_failure_is_logged_and_run_continues(db, fakes, monkeypatch):
    async def boom(*a, **k):
        return None

    monkeypatch.setattr(catalog_fix, "generate_seo", boom)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules))
    assert status["state"] == "done" and status["failed"] == 2 and status["done"] == 1


def test_missing_shop_fails_cleanly(db, fakes):
    asyncio.run(db.settings.update_one({"id": "config"}, {"$unset": {"printify_shop_id": ""}}))
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules))
    assert status["state"] == "failed" and "printify_shop_id" in status["error"]


def test_endpoints(db, fakes, monkeypatch):
    monkeypatch.setattr(server, "settings_coll", db.settings)
    monkeypatch.setattr(server, "capsules_coll", db.capsules)

    async def go():
        started = await server.start_catalog_fix(server.CatalogFixRequest(limit=1), None)
        await server._catalog_fix_task
        return started, await server.catalog_fix_status(None)

    started, status = asyncio.run(go())
    assert started == {"ok": True, "state": "running", "limit": 1}
    assert status["state"] == "done" and status["total"] == 1
