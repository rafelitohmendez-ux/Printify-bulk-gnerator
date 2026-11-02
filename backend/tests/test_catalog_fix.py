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

    async def fake_arrange(auth, listing_id, back_src, ids, **kw):
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


def test_limit_one_tests_newest_live_listing(db, fakes, monkeypatch):
    draft_first = [dict(products()[2])] + products()[:2]  # newest product is a Printify-only draft

    async def fake_list(shop_id, page=1, limit=50):
        return {"data": draft_first if page == 1 else []}

    monkeypatch.setattr(catalog_fix, "list_products", fake_list)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, limit=1))
    assert status["total"] == 1 and [pid for pid, _ in fakes["update"]] == ["p1"]  # skipped the draft


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
    assert started == {"ok": True, "state": "running", "limit": 1, "mode": "fix"}
    assert status["state"] == "done" and status["total"] == 1


# ---------- Printify rate limit, sync and retry modes ----------
from printify_client import PrintifyError  # noqa: E402


def rate_limited_publish(fail_times):
    """publish_product_text that returns 429 for the first `fail_times[pid]` calls per product."""
    seen = {}

    async def publish(shop_id, pid):
        seen[pid] = seen.get(pid, 0) + 1
        if seen[pid] <= fail_times.get(pid, 0):
            raise PrintifyError("Printify POST publish.json -> 429: Too Many Attempts.", status=429)
    publish.seen = seen
    return publish


def test_429_is_waited_out_and_retried(db, fakes, monkeypatch):
    pub = rate_limited_publish({"p1": 2})
    monkeypatch.setattr(catalog_fix, "publish_product_text", pub)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules))
    assert status["failed"] == 0 and pub.seen["p1"] == 3
    assert sum("rate limit: waiting" in line for line in status["log"]) == 2


def test_persistent_429_recorded_as_publish_failure_then_retry_only_publishes(db, fakes, monkeypatch):
    pub = rate_limited_publish({"p1": 99})
    monkeypatch.setattr(catalog_fix, "publish_product_text", pub)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules))
    assert status["failed"] == 1 and status["failed_ids"] == [{"id": "p1", "stage": "publish"}]
    assert pub.seen["p1"] == catalog_fix.PUBLISH_ATTEMPTS
    assert "text saved in Printify" in [l for l in status["log"] if l.startswith("FAILED")][0]

    # Retry: Printify is fine now; only the publish is redone (no Gemini, no photos)
    fakes["seo"].clear(); fakes["arrange"].clear()
    pub2 = rate_limited_publish({})
    monkeypatch.setattr(catalog_fix, "publish_product_text", pub2)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="retry"))
    assert status["total"] == 1 and status["done"] == 1 and status["failed_ids"] == []
    assert pub2.seen == {"p1": 1} and fakes["seo"] == [] and fakes["arrange"] == []


def test_retry_redoes_full_fix_for_non_publish_failures(db, fakes, monkeypatch):
    asyncio.run(db.settings.update_one({"id": catalog_fix.STATUS_DOC_ID},
                                       {"$set": {"failed_ids": [{"id": "p3", "stage": "fix"}]}}, upsert=True))
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="retry"))
    assert status["total"] == 1 and [pid for pid, _ in fakes["update"]] == ["p3"] and len(fakes["seo"]) == 1


def test_sync_mode_only_publishes_live_products(db, fakes):
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="sync"))
    assert status["total"] == 2 and fakes["publish"] == ["p1", "p2"]
    assert fakes["update"] == [] and fakes["seo"] == [] and fakes["arrange"] == []


def test_publishes_are_spaced_out(db, fakes, monkeypatch):
    t = [1000.0]
    waits = []

    async def fake_sleep(sec):
        waits.append(sec)
        t[0] += sec

    monkeypatch.setattr(catalog_fix, "_sleep", fake_sleep)
    monkeypatch.setattr(catalog_fix, "_clock", lambda: t[0])
    catalog_fix._last_publish[0] = 0.0
    asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="sync"))
    # second publish waits so the two are PUBLISH_INTERVAL_SECONDS apart (minus the 1s product delay)
    assert catalog_fix.PUBLISH_INTERVAL_SECONDS - catalog_fix.DELAY_SECONDS in waits


# ---------- price mode ----------
def gildan_variants():
    # S-XL cost $11.00, 2XL $13.20, 3XL $15.10; one disabled colour variant
    return [
        {"id": 1, "title": "Black / S", "cost": 1100, "price": 4499, "is_enabled": True},
        {"id": 2, "title": "Black / XL", "cost": 1100, "price": 4499, "is_enabled": True},
        {"id": 3, "title": "Black / 2XL", "cost": 1320, "price": 4499, "is_enabled": True},
        {"id": 4, "title": "Black / 3XL", "cost": 1510, "price": 4499, "is_enabled": True},
        {"id": 5, "title": "White / S", "cost": 1100, "price": 4499, "is_enabled": False},
    ]


def test_plan_prices_keeps_size_upcharge_and_profit_floor():
    plan = catalog_fix.plan_prices({"variants": gildan_variants()}, 2999)
    prices = {v["id"]: v["price"] for v in plan["variants"]}
    assert prices == {1: 2999, 2: 2999, 3: 3299, 4: 3499, 5: 4499}  # +$2.20 -> +$3, +$4.10 -> +$5
    assert [v["is_enabled"] for v in plan["variants"]] == [True, True, True, True, False]
    assert plan["min_profit"] == 1899 and plan["max_profit"] == 1989
    # absurdly low base price: floor at cost + $5 (ending in .99)
    low = {v["id"]: v["price"] for v in catalog_fix.plan_prices({"variants": gildan_variants()}, 1200)["variants"]}
    assert low[1] == 1599 and low[4] == 2099


def test_price_mode_updates_all_and_syncs_live_prices(db, fakes, monkeypatch):
    def with_variants():
        out = []
        for p in products():
            p = dict(p)
            p["variants"] = gildan_variants()
            out.append(p)
        return out

    async def fake_list(shop_id, page=1, limit=50):
        return {"data": with_variants() if page == 1 else []}

    price_pubs = []

    async def fake_price_pub(shop_id, pid):
        price_pubs.append(pid)

    monkeypatch.setattr(catalog_fix, "list_products", fake_list)
    monkeypatch.setattr(catalog_fix, "publish_product_prices", fake_price_pub)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="price", price_cents=2999))
    assert status["state"] == "done" and status["done"] == 3 and status["price_cents"] == 2999
    assert [pid for pid, _ in fakes["update"]] == ["p1", "p2", "p3"]
    assert all(set(body) == {"variants"} for _, body in fakes["update"])  # text untouched
    assert price_pubs == ["p1", "p2"] and fakes["publish"] == []  # only live ones; text not re-sent
    assert fakes["seo"] == [] and fakes["arrange"] == []
    assert any("$29.99-$34.99" in line and "synced to Etsy" in line for line in status["log"])


def test_price_mode_skips_products_already_at_price(db, fakes, monkeypatch):
    already = catalog_fix.plan_prices({"variants": gildan_variants()}, 2999)["variants"]

    async def fake_list(shop_id, page=1, limit=50):
        p = dict(products()[0])
        p["variants"] = [dict(v, cost=g["cost"]) for v, g in zip(already, gildan_variants())]
        return {"data": [p] if page == 1 else []}

    monkeypatch.setattr(catalog_fix, "list_products", fake_list)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="price", price_cents=2999))
    assert fakes["update"] == [] and "price: already $29.99" in status["log"][-1]


# ---------- photos mode ----------
MOCKUP_IMAGES = [
    {"src": "https://img/front.png", "position": "front", "variant_ids": [1], "is_selected_for_publishing": True},
    {"src": "https://img/back.png", "position": "back", "variant_ids": [1], "is_selected_for_publishing": False},
    {"src": "https://img/back-person.png", "position": "back", "variant_ids": [1], "is_selected_for_publishing": False},
]


def photo_products(desc_note=None):
    out = []
    for p in products():
        p = dict(p)
        p["images"] = [dict(i) for i in MOCKUP_IMAGES]
        if desc_note:
            p["description"] = "Intro. " + desc_note + " Outro."
        out.append(p)
    return out


@pytest.fixture
def photo_fakes(fakes, monkeypatch):
    """L1 arranges fine; L2 has no back view on Etsy (only the old AI scene)."""
    calls = {"fields": []}

    async def arrange(auth, listing_id, back_src, ids, **kw):
        fakes["arrange"].append((listing_id, ids))
        if listing_id == "L2":
            return "back mockup not moved: no comparable Etsy images; lifestyle photo at rank 1"
        return "back mockup (image 3) at rank 1; lifestyle photo at rank 4"

    async def fields(shop_id, pid, text=False, variants=False, images=False):
        calls["fields"].append((pid, text, variants, images))

    monkeypatch.setattr(etsy_photos, "arrange_listing_photos", arrange)
    monkeypatch.setattr(catalog_fix, "publish_product_fields", fields)
    return calls


def test_photos_mode_restores_listings_without_back_view(db, fakes, photo_fakes, monkeypatch):
    async def fake_list(shop_id, page=1, limit=50):
        return {"data": photo_products(seo.PHOTO_NOTE_WITH_AI_SCENE) if page == 1 else []}

    monkeypatch.setattr(catalog_fix, "list_products", fake_list)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="photos"))
    assert status["total"] == 2 and status["done"] == 2 and status["restored"] == 1
    assert status["needs_attention"] == []
    ((pid, body),) = fakes["update"]  # only the restored product is touched in Printify
    assert pid == "p2"
    imgs = {i["src"]: i for i in body["images"]}
    assert imgs["https://img/back.png"]["is_default"] and imgs["https://img/back.png"]["is_selected_for_publishing"]
    assert imgs["https://img/front.png"]["is_selected_for_publishing"]
    assert sum(i["is_default"] for i in body["images"]) == 1
    # the AI-scene note goes, since restoring replaces the Etsy photos
    assert seo.PHOTO_NOTE_MOCKUPS in body["description"] and "AI-generated illustration" not in body["description"]
    assert photo_fakes["fields"] == [("p2", True, False, True)]  # images + text, not prices
    assert fakes["seo"] == [] and fakes["publish"] == []
    assert any("mockups re-sent from Printify" in line for line in status["log"])


def test_photos_restore_without_text_change(db, fakes, photo_fakes, monkeypatch):
    async def fake_list(shop_id, page=1, limit=50):
        return {"data": photo_products() if page == 1 else []}

    monkeypatch.setattr(catalog_fix, "list_products", fake_list)
    asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="photos"))
    assert photo_fakes["fields"] == [("p2", False, False, True)]
    assert set(fakes["update"][0][1]) == {"images"}


def test_photos_test_run_stops_after_first_restore(db, fakes, photo_fakes, monkeypatch):
    many = []
    for n in range(5):
        p = dict(photo_products()[1])  # every product is like L2 (needs a restore)
        p["id"] = f"q{n}"
        many.append(p)

    async def fake_list(shop_id, page=1, limit=50):
        return {"data": many if page == 1 else []}

    monkeypatch.setattr(catalog_fix, "list_products", fake_list)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, limit=1, mode="photos"))
    assert status["restored"] == 1 and len(photo_fakes["fields"]) == 1 and status["done"] == 1
    assert "stopped after the first restored listing" in status["log"][-1]


def test_photos_restore_impossible_without_back_mockup(db, fakes, photo_fakes, monkeypatch):
    async def fake_list(shop_id, page=1, limit=50):
        p = dict(photo_products()[1])
        p["images"] = [MOCKUP_IMAGES[0]]  # front only
        return {"data": [p] if page == 1 else []}

    monkeypatch.setattr(catalog_fix, "list_products", fake_list)
    status = asyncio.run(catalog_fix.run_catalog_fix(db.settings, db.capsules, mode="photos"))
    assert status["failed"] == 1 and status["failed_ids"] == [{"id": "p2", "stage": "photos"}]
    assert "no back mockup" in status["log"][-1]


def test_endpoint_rejects_unknown_mode(db, monkeypatch):
    monkeypatch.setattr(server, "settings_coll", db.settings)
    with pytest.raises(server.HTTPException):
        asyncio.run(server.start_catalog_fix(server.CatalogFixRequest(mode="nuke"), None))


def test_price_setting_validated_and_exposed(db, monkeypatch):
    monkeypatch.setattr(server, "settings_coll", db.settings)
    monkeypatch.setattr(server, "capsules_coll", db.capsules)
    out = asyncio.run(server.update_settings(server.SettingsUpdate(listing_price_cents=2999), None))
    assert out["listing_price_cents"] == 2999
    with pytest.raises(server.HTTPException):
        asyncio.run(server.update_settings(server.SettingsUpdate(listing_price_cents=500), None))
    assert server.listing_price({}) == 4499
