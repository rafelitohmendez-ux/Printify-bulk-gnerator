"""Offline tests for backfill_etsy_photos.py selection/skip logic (no Etsy/Printify/Gemini calls).

Run: pytest backend/tests/test_backfill_etsy_photos.py
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_server import server  # noqa: E402,F401  (sets dummy env before backend imports)
import backfill_etsy_photos as bf  # noqa: E402
import etsy_photos  # noqa: E402

DESC = ("A premium staple. Featuring a clean minimalist left-chest graphic on the front and an aggressive, "
        "oversized hooded monk with chains filling the back.")


def listing(lid, title="Iron Vigil - Gothic Tee", tags=()):
    return {"listing_id": lid, "title": title, "tags": list(tags)}


def product(pid, lid, description=DESC, back=True):
    images = [{"src": f"https://printify.test/{pid}-front.png", "position": "front"}]
    if back:
        images.append({"src": f"https://printify.test/{pid}-back.png", "position": "back"})
    return {"id": pid, "external": {"id": str(lid)}, "description": description, "images": images}


def test_is_cyber_matches_title_or_tags():
    assert bf.is_cyber(listing(1, "Glitch Saint | Cyberpunk Gothic Tee"))
    assert bf.is_cyber(listing(2, "Iron Vigil", tags=["Techwear Shirt"]))
    assert bf.is_cyber(listing(3, "Wired Angel", tags=["Cybergoth Tee"]))
    assert not bf.is_cyber(listing(4, "Bone Church - Gothic Tee", tags=["Occult Shirt"]))


@pytest.mark.parametrize("setup,expected", [
    (dict(logged={"10"}), "already has atmospheric photo (etsy_photo_log)"),
    (dict(capsules={"10"}), "already has atmospheric photo (approve pipeline)"),
    (dict(processed={"p10"}), "already has atmospheric photo (etsy_upload_processed.json)"),
    (dict(no_product=True), "no Printify product linked to this listing"),
    (dict(description="Plain description"), "no back concept in the Printify description"),
])
def test_decide_skip_reasons(setup, expected):
    prod = None if setup.get("no_product") else product("p10", 10, description=setup.get("description", DESC))
    ok, reason = bf.decide(listing(10), prod, setup.get("logged", set()), setup.get("capsules", set()),
                           setup.get("processed", set()))
    assert (ok, reason) == (False, expected)


def test_decide_process_and_no_back_mockup_note():
    assert bf.decide(listing(10), product("p10", 10), set(), set(), set()) == (True, "needs atmospheric photo")
    ok, reason = bf.decide(listing(10), product("p10", 10, back=False), set(), set(), set())
    assert ok and "no Printify back mockup" in reason


def test_select_cyber_first_limit_and_skips():
    listings = [
        listing(1, "Bone Church Tee"),
        listing(2, "Glitch Saint Cyberpunk Tee"),
        listing(3, "Ash Psalm Tee"),
        listing(4, "Wired Angel", tags=["Techwear Shirt"]),
        listing(5, "Already Done Cyber Tee"),
        listing(6, "Unlinked Listing"),
    ]
    products = {str(i): product(f"p{i}", i) for i in (1, 2, 3, 4, 5)}
    selected, skipped, eligible = bf.select(listings, products, {"5"}, set(), set(), limit=3, cyber_first=True)
    assert [l["listing_id"] for l, _, _ in selected] == [2, 4, 1]  # cyber first, then Etsy order
    assert eligible == 4
    assert sorted((l["listing_id"], r) for l, r in skipped) == [
        (5, "already has atmospheric photo (etsy_photo_log)"),
        (6, "no Printify product linked to this listing"),
    ]
    selected, _, _ = bf.select(listings, products, {"5"}, set(), set(), limit=10, cyber_first=False)
    assert [l["listing_id"] for l, _, _ in selected] == [1, 2, 3, 4]


def test_cli_defaults_are_safe():
    args = bf.parse_args([])
    assert args.apply is False and args.limit == 5 and args.delay == 30 and not args.cyber_first
    args = bf.parse_args(["--apply", "--limit", "2", "--cyber-first", "--delay", "45", "--listing-id", "99"])
    assert args.apply and args.limit == 2 and args.cyber_first and args.delay == 45 and args.listing_id == "99"


# ---- main(): dry run uploads nothing; apply is idempotent ------------------------------------
@pytest.fixture
def harness(monkeypatch, tmp_path):
    mongomock_motor = pytest.importorskip("mongomock_motor")
    client = mongomock_motor.AsyncMongoMockClient()
    db = client["t"]
    asyncio.run(db.settings.insert_one({"id": "config", "printify_shop_id": 1}))
    asyncio.run(db.settings.insert_one({"id": etsy_photos.AUTH_DOC_ID, "access_token": "a", "refresh_token": "r",
                                        "expires_at": 9e18}))
    for k, v in {"ETSY_API_KEY": "k", "ETSY_SHARED_SECRET": "s", "ETSY_SHOP_ID": "1",
                 "MONGO_URL": "mongodb://x", "DB_NAME": "t"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(bf, "AsyncIOMotorClient", lambda url: client)
    monkeypatch.setattr(bf, "PROCESSED_FILE", tmp_path / "none.json")
    listings = [listing(1, "Bone Church Tee"), listing(2, "Glitch Cyber Tee"), listing(3, "Ash Psalm Tee")]

    async def fake_listings(auth, listing_id):
        return [l for l in listings if not listing_id or str(l["listing_id"]) == listing_id]

    async def fake_products(shop_id):
        return {str(i): product(f"p{i}", i) for i in (1, 2, 3)}

    processed = []

    async def fake_process(auth, db_, lst, prod):
        processed.append(lst["listing_id"])
        await etsy_photos.log_listing_photo(db_[etsy_photos.PHOTO_LOG_COLLECTION], str(lst["listing_id"]),
                                            prod["id"], "backfill", "moved")
        return "back mockup moved to rank 2"

    async def no_sleep(_):
        return None

    monkeypatch.setattr(bf, "fetch_listings", fake_listings)
    monkeypatch.setattr(bf, "fetch_products_by_listing", fake_products)
    monkeypatch.setattr(bf, "process_one", fake_process)
    monkeypatch.setattr(bf.asyncio, "sleep", no_sleep)
    return db, processed


def test_dry_run_processes_nothing(harness, capsys):
    db, processed = harness
    rc = asyncio.run(bf.main(bf.parse_args(["--limit", "10", "--cyber-first"])))
    out = capsys.readouterr().out
    assert rc == 0 and processed == []
    assert "DRY RUN" in out and "Estimated Gemini image calls: 3" in out
    assert out.index("[CYBER] 2") < out.index("1  Bone Church")


def test_apply_then_rerun_is_idempotent(harness, capsys):
    db, processed = harness
    asyncio.run(bf.main(bf.parse_args(["--apply", "--limit", "2", "--cyber-first", "--delay", "0"])))
    assert processed == [2, 1]
    asyncio.run(bf.main(bf.parse_args(["--apply", "--limit", "5", "--delay", "0"])))
    assert processed == [2, 1, 3]  # second run only picks up what's left
    out = capsys.readouterr().out
    assert "already has atmospheric photo (etsy_photo_log)" in out
    assert "SUMMARY: 1 uploaded, 0 failed" in out
