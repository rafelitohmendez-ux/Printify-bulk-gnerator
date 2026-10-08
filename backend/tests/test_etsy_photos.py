"""Offline tests for the atmospheric Etsy photo pipeline (Etsy, Printify and Gemini all mocked).

Run: pytest backend/tests/test_etsy_photos.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from offline_server import server  # noqa: E402
import etsy_photos  # noqa: E402

LISTING_ID = "987654321"
ENV = {
    "ETSY_API_KEY": "test-keystring",
    "ETSY_SHARED_SECRET": "test-secret",
    "ETSY_SHOP_ID": "424242",
    "ETSY_ACCESS_TOKEN": "seed-access",
    "ETSY_REFRESH_TOKEN": "seed-refresh",
}


class FakeEtsy:
    """httpx MockTransport handler emulating the Etsy endpoints the pipeline uses."""

    def __init__(self, valid_token="seed-access", upload_status=201):
        self.valid_token = valid_token
        self.upload_status = upload_status
        self.calls = []
        self.refreshes = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if url == etsy_photos.ETSY_TOKEN_URL:
            body = json.loads(request.content)
            assert body["grant_type"] == "refresh_token" and body["client_id"] == ENV["ETSY_API_KEY"]
            self.refreshes += 1
            self.valid_token = f"new-access-{self.refreshes}"
            return httpx.Response(200, json={
                "access_token": self.valid_token,
                "refresh_token": f"new-refresh-{self.refreshes}",
                "expires_in": 3600,
            })
        assert request.headers["x-api-key"] == f"{ENV['ETSY_API_KEY']}:{ENV['ETSY_SHARED_SECRET']}"
        if request.headers["authorization"] != f"Bearer {self.valid_token}":
            return httpx.Response(401, json={"error": "invalid_token"})
        if url.endswith(f"/shops/{ENV['ETSY_SHOP_ID']}") and request.method == "GET":
            return httpx.Response(200, json={"shop_id": 424242, "shop_name": "MidnightRotation"})
        if url.endswith(f"/listings/{LISTING_ID}/images") and request.method == "POST":
            if self.upload_status >= 400:
                return httpx.Response(self.upload_status, text="Etsy is having a bad day")
            return httpx.Response(self.upload_status, json={"listing_image_id": 1})
        return httpx.Response(404, text=f"unexpected {request.method} {url}")

    def uploads(self):
        return [r for r in self.calls if r.url.path.endswith("/images")]


@pytest.fixture
def db(monkeypatch):
    mongomock_motor = pytest.importorskip("mongomock_motor")
    database = mongomock_motor.AsyncMongoMockClient()["t"]
    capsules, settings = database.capsules, database.settings
    monkeypatch.setattr(server, "capsules_coll", capsules)
    monkeypatch.setattr(server, "settings_coll", settings)
    return capsules, settings


@pytest.fixture
def etsy(monkeypatch):
    fake = FakeEtsy()
    monkeypatch.setattr(etsy_photos, "_http_client", lambda timeout=60.0: httpx.AsyncClient(
        transport=httpx.MockTransport(fake), timeout=timeout))
    return fake


@pytest.fixture
def env(monkeypatch):
    for k, v in ENV.items():
        monkeypatch.setenv(k, v)


@pytest.fixture
def printify(monkeypatch):
    """Printify product poll: returns the Etsy listing ID after `ready_after` polls (None = never)."""
    state = {"polls": 0, "ready_after": 1, "fail_first": False}

    async def fake_get_product(shop_id, product_id):
        state["polls"] += 1
        if state["fail_first"] and state["polls"] == 1:
            raise RuntimeError("Printify 502")
        ready = state["ready_after"] is not None and state["polls"] >= state["ready_after"]
        return {"id": product_id, "external": {"id": LISTING_ID} if ready else None}

    monkeypatch.setattr(etsy_photos, "get_product", fake_get_product)
    return state


@pytest.fixture
def gemini(monkeypatch):
    calls = []

    async def fake_generate(capsule):
        calls.append(capsule["id"])
        return b"\x89PNG fake photo"

    monkeypatch.setattr(etsy_photos, "_generate_photo", fake_generate)
    return calls


def seed_capsule(db, **overrides):
    capsules, settings = db
    cap = server.Capsule(
        capsule_name="Glitch Saint", title="t", description="d", front_concept="f", back_concept="b",
        tags=["Gothic Streetwear"], front_image_b64="QUFB", back_image_b64="QkJC", status="approved",
    ).model_dump()
    cap.update({"printify_product_id": "pid-1", "printify_push_status": "success"}, **overrides)
    asyncio.run(capsules.insert_one(dict(cap)))
    asyncio.run(settings.update_one({"id": "config"}, {"$set": {"printify_shop_id": 1}}, upsert=True))
    return cap


def run(db, cap_id, **kw):
    capsules, settings = db
    kw.setdefault("poll_timeout", 1)
    kw.setdefault("poll_interval", 0.01)
    return asyncio.run(etsy_photos.run_etsy_photos(cap_id, capsules, settings, **kw))


def stored(db, cap_id):
    return asyncio.run(db[0].find_one({"id": cap_id}, {"_id": 0}))


def auth_doc(db):
    return asyncio.run(db[1].find_one({"id": etsy_photos.AUTH_DOC_ID}, {"_id": 0}))


# ---- missing config -> skipped -------------------------------------------------------
def test_missing_env_vars_skips_with_names(db, etsy, printify, gemini, caplog):
    cap = seed_capsule(db)
    with caplog.at_level("WARNING"):
        run(db, cap["id"])
    doc = stored(db, cap["id"])
    assert doc["etsy_photos_status"] == "skipped"
    for name in ("ETSY_API_KEY", "ETSY_SHARED_SECRET", "ETSY_SHOP_ID", "ETSY_REFRESH_TOKEN"):
        assert name in doc["etsy_photos_error"]
    assert "ETSY_ACCESS_TOKEN" not in doc["etsy_photos_error"]  # optional seed
    assert any("Etsy photos skipped" in r.getMessage() and "ETSY_API_KEY" in r.getMessage() for r in caplog.records)
    assert etsy.calls == [] and printify["polls"] == 0 and gemini == []


def test_single_missing_var_named(db, etsy, env, printify, gemini, monkeypatch):
    monkeypatch.setenv("ETSY_SHARED_SECRET", "")
    cap = seed_capsule(db)
    run(db, cap["id"])
    assert stored(db, cap["id"])["etsy_photos_error"] == "Missing env var(s): ETSY_SHARED_SECRET"


def test_not_pushed_capsule_skipped(db, etsy, env, printify, gemini):
    cap = seed_capsule(db, printify_product_id=None)
    run(db, cap["id"])
    doc = stored(db, cap["id"])
    assert doc["etsy_photos_status"] == "skipped" and "not been pushed" in doc["etsy_photos_error"]


# ---- token storage + refresh ------------------------------------------------------------
def test_401_refreshes_and_saves_new_tokens_to_mongo(db, etsy, env, printify, gemini):
    etsy.valid_token = "already-rotated"  # env-seeded access token is stale -> 401
    cap = seed_capsule(db)
    run(db, cap["id"])
    assert stored(db, cap["id"])["etsy_photos_status"] == "success"
    assert etsy.refreshes == 1
    auth = auth_doc(db)
    assert auth["access_token"] == "new-access-1"
    assert auth["refresh_token"] == "new-refresh-1"  # rotated refresh token persisted
    assert auth["expires_at"] > time.time() + 3000
    assert len(etsy.uploads()) == 2  # 401, then retried with the new token


def test_env_only_seeds_once_mongo_wins_afterwards(db, etsy, env, printify, gemini, monkeypatch):
    asyncio.run(db[1].insert_one({
        "id": etsy_photos.AUTH_DOC_ID, "access_token": "mongo-access", "refresh_token": "mongo-refresh",
        "expires_at": time.time() + 3600,
    }))
    etsy.valid_token = "mongo-access"
    cap = seed_capsule(db)
    run(db, cap["id"])
    assert stored(db, cap["id"])["etsy_photos_status"] == "success"
    assert etsy.refreshes == 0
    assert all(r.headers.get("authorization") == "Bearer mongo-access" for r in etsy.uploads())
    assert auth_doc(db)["refresh_token"] == "mongo-refresh"


def test_expired_token_refreshed_before_call(db, etsy, env, printify, gemini):
    asyncio.run(db[1].insert_one({
        "id": etsy_photos.AUTH_DOC_ID, "access_token": "old", "refresh_token": "mongo-refresh",
        "expires_at": time.time() - 10,
    }))
    cap = seed_capsule(db)
    run(db, cap["id"])
    assert etsy.refreshes == 1
    assert len(etsy.uploads()) == 1  # no wasted 401 round-trip
    assert auth_doc(db)["refresh_token"] == "new-refresh-1"


def test_refresh_failure_recorded(db, etsy, env, printify, gemini, monkeypatch):
    etsy.valid_token = "rotated-elsewhere"

    def bad_refresh(request):
        if str(request.url) == etsy_photos.ETSY_TOKEN_URL:
            return httpx.Response(400, json={"error": "invalid_grant"})
        return etsy(request)

    monkeypatch.setattr(etsy_photos, "_http_client", lambda timeout=60.0: httpx.AsyncClient(
        transport=httpx.MockTransport(bad_refresh), timeout=timeout))
    cap = seed_capsule(db)
    run(db, cap["id"])
    doc = stored(db, cap["id"])
    assert doc["etsy_photos_status"] == "failed"
    assert "token refresh failed: HTTP 400" in doc["etsy_photos_error"]


# ---- timing ---------------------------------------------------------------------------------
def test_missing_listing_id_times_out_cleanly(db, etsy, env, printify, gemini):
    printify["ready_after"] = None
    cap = seed_capsule(db)
    run(db, cap["id"], poll_timeout=0.05, poll_interval=0.01)
    doc = stored(db, cap["id"])
    assert doc["etsy_photos_status"] == "failed"
    assert "Timed out" in doc["etsy_photos_error"] and "pid-1" in doc["etsy_photos_error"]
    assert printify["polls"] >= 2
    assert gemini == [] and etsy.uploads() == []


def test_listing_appears_after_polling_and_transient_errors(db, etsy, env, printify, gemini):
    printify.update(ready_after=4, fail_first=True)
    cap = seed_capsule(db)
    run(db, cap["id"])
    assert stored(db, cap["id"])["etsy_photos_status"] == "success"
    assert printify["polls"] == 4


# ---- success / upload failure -----------------------------------------------------------
def test_success(db, etsy, env, printify, gemini):
    cap = seed_capsule(db)
    run(db, cap["id"])
    doc = stored(db, cap["id"])
    assert doc["etsy_photos_status"] == "success" and doc["etsy_photos_error"] is None
    assert doc["etsy_listing_id"] == LISTING_ID
    (upload,) = etsy.uploads()
    assert upload.url.path == f"/v3/application/shops/{ENV['ETSY_SHOP_ID']}/listings/{LISTING_ID}/images"
    assert b'name="rank"' in upload.content and b"fake photo" in upload.content
    assert gemini == [cap["id"]]


def test_upload_http_error_recorded(db, etsy, env, printify, gemini, caplog):
    etsy.upload_status = 500
    cap = seed_capsule(db)
    with caplog.at_level("WARNING"):
        run(db, cap["id"])
    doc = stored(db, cap["id"])
    assert doc["etsy_photos_status"] == "failed"
    assert "HTTP 500" in doc["etsy_photos_error"] and "bad day" in doc["etsy_photos_error"]
    assert any("Etsy photos failed" in r.getMessage() for r in caplog.records)


def test_status_visible_in_approved_list(db, etsy, env, printify, gemini):
    cap = seed_capsule(db)
    run(db, cap["id"])
    (item,) = asyncio.run(server.list_approved())
    assert item.etsy_photos_status == "success" and item.etsy_photos_error is None


# ---- endpoints ----------------------------------------------------------------------------------
def test_etsy_check_returns_shop_without_tokens(db, etsy, env):
    etsy.valid_token = "stale"  # forces a refresh
    res = asyncio.run(server.etsy_check(None))
    assert res["shop_name"] == "MidnightRotation" and res["token_expires_at"]
    dumped = json.dumps(res)
    for secret in ("new-access", "new-refresh", "seed-access", "seed-refresh", "test-secret"):
        assert secret not in dumped


def test_etsy_check_missing_config_503(db, etsy):
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.etsy_check(None))
    assert e.value.status_code == 503 and "ETSY_API_KEY" in e.value.detail


def test_retry_endpoint_validation(db):
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.retry_etsy_photos("nope", None))
    assert e.value.status_code == 404
    draft = seed_capsule(db, status="draft")
    with pytest.raises(server.HTTPException) as e:
        asyncio.run(server.retry_etsy_photos(draft["id"], None))
    assert e.value.status_code == 400


def test_retry_endpoint_runs_pipeline(db, etsy, env, printify, gemini):
    cap = seed_capsule(db, etsy_photos_status="failed", etsy_photos_error="old")

    async def go():
        res = await server.retry_etsy_photos(cap["id"], None)
        assert res["etsy_photos_status"] == "pending"
        doc = await db[0].find_one({"id": cap["id"]})
        assert doc["etsy_photos_status"] == "pending"
        with pytest.raises(server.HTTPException) as e:  # double-click guard
            await server.retry_etsy_photos(cap["id"], None)
        assert e.value.status_code == 409
        await server._etsy_photo_tasks[cap["id"]]

    asyncio.run(go())
    doc = stored(db, cap["id"])
    assert doc["etsy_photos_status"] == "success" and doc["etsy_photos_error"] is None
