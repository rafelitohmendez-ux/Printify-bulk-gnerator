"""End-to-end backend tests for TwelveHoursCO capsule API.

Covers: generate (AI call, ~20-60s), approve, deny, approved list, stats, image, CSV export.
"""
import os
import re
import pytest
import requests

BASE_URL = os.environ["REACT_APP_BACKEND_URL"].rstrip("/") if os.environ.get("REACT_APP_BACKEND_URL") else "https://approve-grind.preview.emergentagent.com"
API = f"{BASE_URL}/api"


@pytest.fixture(scope="module")
def session():
    s = requests.Session()
    s.headers.update({"Content-Type": "application/json"})
    return s


# ---------- Health ----------
class TestHealth:
    def test_root(self, session):
        r = session.get(f"{API}/")
        assert r.status_code == 200
        data = r.json()
        assert data["service"] == "TwelveHoursCO"
        assert data["status"] == "online"

    def test_stats_shape(self, session):
        r = session.get(f"{API}/capsules/stats")
        assert r.status_code == 200
        data = r.json()
        assert "approved" in data
        assert isinstance(data["approved"], int)


# ---------- Generate + Approve + Deny full flow ----------
@pytest.fixture(scope="module")
def generated_capsule(session):
    """Generate one capsule (heavy AI call). Cached for downstream tests."""
    r = session.post(f"{API}/capsules/generate", timeout=180)
    if r.status_code != 200:
        pytest.fail(f"generate failed {r.status_code}: {r.text[:500]}")
    return r.json()


class TestGenerate:
    def test_generate_returns_valid_capsule(self, generated_capsule):
        c = generated_capsule
        assert c["id"]
        assert c["status"] == "draft"
        assert c["capsule_name"]
        assert c["title"]
        assert c["description"]
        assert c["front_concept"]
        assert c["back_concept"]
        # base64 images present
        assert c["front_image_b64"] and len(c["front_image_b64"]) > 100
        assert c["back_image_b64"] and len(c["back_image_b64"]) > 100

    def test_generate_tags_exactly_13_with_required(self, generated_capsule):
        tags = generated_capsule["tags"]
        assert isinstance(tags, list)
        assert len(tags) == 13, f"expected 13 tags, got {len(tags)}: {tags}"
        lower = [t.lower() for t in tags]
        assert "gothic streetwear" in lower
        assert "back print shirt" in lower

    def test_generate_description_template(self, generated_capsule):
        desc = generated_capsule["description"]
        assert desc.startswith("THE GRIND //")
        assert "Gildan 5000" in desc
        assert "Machine wash cold" in desc
        # capsule_name in upper case appears
        assert generated_capsule["capsule_name"].upper() in desc


# ---------- Image endpoint ----------
class TestImage:
    def test_image_front(self, session, generated_capsule):
        cid = generated_capsule["id"]
        r = session.get(f"{API}/capsules/{cid}/image/front")
        assert r.status_code == 200
        ct = r.headers.get("Content-Type", "")
        assert ct in ("image/jpeg", "image/png"), f"unexpected ct: {ct}"
        # validate magic bytes match content-type
        if ct == "image/jpeg":
            assert r.content[:3] == b"\xff\xd8\xff", "ct says jpeg but bytes are not"
        else:
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n", "ct says png but bytes are not"

    def test_image_back(self, session, generated_capsule):
        cid = generated_capsule["id"]
        r = session.get(f"{API}/capsules/{cid}/image/back")
        assert r.status_code == 200
        ct = r.headers.get("Content-Type", "")
        assert ct in ("image/jpeg", "image/png")
        if ct == "image/jpeg":
            assert r.content[:3] == b"\xff\xd8\xff"
        else:
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n"

    def test_image_invalid_side(self, session, generated_capsule):
        cid = generated_capsule["id"]
        r = session.get(f"{API}/capsules/{cid}/image/middle")
        assert r.status_code == 400


# ---------- Approve ----------
class TestApproveFlow:
    def test_approve_capsule(self, session, generated_capsule):
        cid = generated_capsule["id"]
        before = session.get(f"{API}/capsules/stats").json()["approved"]
        r = session.post(f"{API}/capsules/{cid}/approve")
        assert r.status_code == 200
        body = r.json()
        assert body["id"] == cid
        assert body["status"] == "approved"
        assert body["approved_at"]
        # no base64 in public model
        assert "front_image_b64" not in body
        # stats incremented
        after = session.get(f"{API}/capsules/stats").json()["approved"]
        assert after == before + 1

    def test_approved_list_contains(self, session, generated_capsule):
        cid = generated_capsule["id"]
        r = session.get(f"{API}/capsules/approved")
        assert r.status_code == 200
        items = r.json()
        ids = [c["id"] for c in items]
        assert cid in ids
        item = next(c for c in items if c["id"] == cid)
        assert item["status"] == "approved"
        assert "front_image_b64" not in item

    def test_approve_unknown_returns_404(self, session):
        r = session.post(f"{API}/capsules/nonexistent-id-xyz/approve")
        assert r.status_code == 404


# ---------- Deny flow (needs its own generated capsule) ----------
class TestDenyFlow:
    def test_deny_deletes_capsule(self, session):
        gen = session.post(f"{API}/capsules/generate", timeout=180)
        if gen.status_code != 200:
            pytest.skip(f"generate failed for deny test: {gen.status_code} {gen.text[:200]}")
        cid = gen.json()["id"]
        # deny
        r = session.post(f"{API}/capsules/{cid}/deny")
        assert r.status_code == 200
        # subsequent image fetch -> 404
        img = session.get(f"{API}/capsules/{cid}/image/front")
        assert img.status_code == 404
        # approve on deleted -> 404
        appr = session.post(f"{API}/capsules/{cid}/approve")
        assert appr.status_code == 404

    def test_deny_unknown_returns_404(self, session):
        r = session.post(f"{API}/capsules/nonexistent-id-xyz/deny")
        assert r.status_code == 404


# ---------- CSV export ----------
class TestExportCSV:
    def test_csv_export(self, session, generated_capsule):
        r = session.get(f"{API}/capsules/export.csv")
        assert r.status_code == 200
        ct = r.headers.get("Content-Type", "")
        assert "text/csv" in ct
        cd = r.headers.get("Content-Disposition", "")
        assert "attachment" in cd
        assert "twelvehoursco_approved.csv" in cd
        # header row + at least one approved capsule
        body = r.text
        assert "capsule_name" in body
        assert "front_image_url" in body
        # the approved capsule id should be in body
        assert generated_capsule["id"] in body
