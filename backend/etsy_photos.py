"""Atmospheric Etsy photos for approved capsules (runs on the deployed backend).

Flow (triggered after auto-push publishes a capsule, or via the retry endpoint):
  1. Poll Printify until the product has an external Etsy listing ID (Printify
     publishes to Etsy asynchronously, so the listing doesn't exist right away).
  2. Generate a themed atmospheric photo with Gemini (generate_mockups.py).
  3. Upload it to the Etsy listing as the primary image (rank 1), then find the
     back-print mockup among the listing's images (perceptual hash vs Printify's
     back mockup) and re-rank it to 2.
The outcome is saved on the capsule as etsy_photos_status / etsy_photos_error.

Etsy OAuth tokens live in Mongo (settings collection, {"id": "etsy_auth"}, the
same doc etsy_client.py uses) because Render's filesystem resets on every
deploy/restart and Etsy rotates the refresh token on every refresh. The
ETSY_ACCESS_TOKEN / ETSY_REFRESH_TOKEN env vars only seed Mongo the first time.

Env vars:
    ETSY_API_KEY        - app keystring (also the OAuth client_id)
    ETSY_SHARED_SECRET  - app shared secret (x-api-key is "KEYSTRING:SECRET")
    ETSY_SHOP_ID        - numeric Etsy shop ID
    ETSY_REFRESH_TOKEN  - seed only, needed until Mongo holds a token
    ETSY_ACCESS_TOKEN   - optional seed (refreshed on first 401 if absent/stale)
"""
import asyncio
import base64
import io
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

import httpx

from printify_client import get_product

logger = logging.getLogger("midnightrotation.etsy_photos")

ETSY_API_BASE = "https://openapi.etsy.com/v3/application"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
AUTH_DOC_ID = "etsy_auth"
TOKEN_EXPIRY_MARGIN_SECONDS = 60

LISTING_POLL_INTERVAL_SECONDS = 20
LISTING_POLL_TIMEOUT_SECONDS = 600

RATE_LIMIT_MAX_RETRIES = 4
RATE_LIMIT_BASE_DELAY_SECONDS = 2
RATE_LIMIT_MAX_DELAY_SECONDS = 60

# Back-mockup matching (perceptual dHash, 256 bits over the chest/back print area): accept
# the closest Etsy image only if it's within MAX_DISTANCE and clearly closer than the runner-up.
# A 64-bit whole-image hash can't tell a blank front mockup from the back mockup (both are a
# dark shirt on white), so the hash is taken from the center crop where the print sits.
MOCKUP_HASH_SIZE = 16
MOCKUP_HASH_CROP = (0.25, 0.2, 0.75, 0.75)  # left, top, right, bottom as fractions
BACK_MATCH_MAX_DISTANCE = 40
BACK_MATCH_MIN_MARGIN = 16

# Listings that got an atmospheric photo (pipeline or backfill), keyed by listing_id
PHOTO_LOG_COLLECTION = "etsy_photo_log"

_sleep = asyncio.sleep  # patched in tests

# Serializes token refreshes: Etsy rotates the refresh token, so two concurrent
# refreshes with the same token would make the second one fail.
_refresh_lock = asyncio.Lock()


def _http_client(timeout: float = 60.0) -> httpx.AsyncClient:
    """Overridden in tests with a mock transport."""
    return httpx.AsyncClient(timeout=timeout)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class EtsyConfigError(Exception):
    """Required Etsy configuration is missing (capsule is marked 'skipped')."""


class EtsyAPIError(Exception):
    def __init__(self, what: str, status: int, body: str):
        self.status = status
        super().__init__(f"{what} failed: HTTP {status}: {_short(body)}")


def _short(text: str, limit: int = 300) -> str:
    return re.sub(r"\s+", " ", text or "").strip()[:limit]


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


class EtsyAuth:
    """Etsy API access with tokens persisted in Mongo."""

    def __init__(self, settings_coll):
        self.settings_coll = settings_coll
        self.api_key = _env("ETSY_API_KEY")
        self.shared_secret = _env("ETSY_SHARED_SECRET")
        self.shop_id = _env("ETSY_SHOP_ID")
        self.auth: Dict[str, Any] = {}

    async def load(self) -> None:
        """Load tokens from Mongo, seeding from env vars if Mongo has none.

        Raises EtsyConfigError naming every missing env var.
        """
        doc = await self.settings_coll.find_one({"id": AUTH_DOC_ID}, {"_id": 0}) or {}
        if not self.shop_id and doc.get("shop_id"):
            self.shop_id = str(doc["shop_id"])
        missing = [n for n, v in (
            ("ETSY_API_KEY", self.api_key),
            ("ETSY_SHARED_SECRET", self.shared_secret),
            ("ETSY_SHOP_ID", self.shop_id),
        ) if not v]
        if not doc.get("refresh_token") and not _env("ETSY_REFRESH_TOKEN"):
            missing.append("ETSY_REFRESH_TOKEN")
        if missing:
            raise EtsyConfigError(f"Missing env var(s): {', '.join(missing)}")

        env_refresh = _env("ETSY_REFRESH_TOKEN")
        # A never-refreshed env seed is superseded by a different (newer) env token; once a
        # refresh has happened, Mongo's pair is the live one and env values are stale.
        reseed = (
            doc.get("source") == "env_seed" and env_refresh and env_refresh != doc.get("refresh_token")
        )
        if not doc.get("refresh_token") or reseed:
            doc = {
                "id": AUTH_DOC_ID,
                "client_id": self.api_key,
                "shop_id": self.shop_id,
                "access_token": _env("ETSY_ACCESS_TOKEN"),
                "refresh_token": _env("ETSY_REFRESH_TOKEN"),
                "expires_at": None,  # unknown for env-seeded tokens: try it, refresh on 401
                "source": "env_seed",
                "seeded_at": _now_iso(),
            }
            await self.settings_coll.update_one({"id": AUTH_DOC_ID}, {"$set": doc}, upsert=True)
            logger.info("Seeded Etsy tokens in Mongo from ETSY_* env vars")
        self.auth = doc

    def _expired(self) -> bool:
        if not self.auth.get("access_token"):
            return True
        expires_at = self.auth.get("expires_at")
        return expires_at is not None and time.time() >= float(expires_at) - TOKEN_EXPIRY_MARGIN_SECONDS

    async def refresh(self) -> None:
        """Exchange the refresh token for a new pair and save BOTH to Mongo."""
        async with _refresh_lock:
            # Another request may have refreshed while we waited for the lock
            current = await self.settings_coll.find_one({"id": AUTH_DOC_ID}, {"_id": 0}) or {}
            if current.get("access_token") and current.get("access_token") != self.auth.get("access_token"):
                self.auth = current
                if not self._expired():
                    return
            async with _http_client(30.0) as c:
                resp = await c.post(ETSY_TOKEN_URL, json={
                    "grant_type": "refresh_token",
                    "client_id": self.api_key,
                    "refresh_token": self.auth.get("refresh_token"),
                })
            if resp.status_code >= 400:
                # Another process (deployed backend vs a local script) may have rotated the
                # pair a moment ago; if Mongo now holds a different one, use that instead.
                latest = await self.settings_coll.find_one({"id": AUTH_DOC_ID}, {"_id": 0}) or {}
                if latest.get("refresh_token") and latest.get("refresh_token") != self.auth.get("refresh_token"):
                    self.auth = latest
                    logger.info("Etsy token was rotated by another process; using the newer pair from Mongo")
                    return
                err = EtsyAPIError("Etsy token refresh", resp.status_code, resp.text)
                if "invalid_grant" in resp.text:
                    err.args = (f"{err} - the stored refresh token is dead; re-authorize with "
                                "`python backend/etsy_oauth_setup.py` (saves a new pair to Mongo)",)
                raise err
            data = resp.json()
            patch = {
                "client_id": self.api_key,
                "shop_id": self.shop_id,
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token") or self.auth.get("refresh_token"),
                "expires_at": time.time() + int(data.get("expires_in") or 3600),
                "refreshed_at": _now_iso(),
                "source": "refresh",
            }
            await self.settings_coll.update_one({"id": AUTH_DOC_ID}, {"$set": patch}, upsert=True)
            self.auth.update(patch)
            logger.info("Etsy access token refreshed and saved to Mongo")

    def _headers(self) -> Dict[str, str]:
        return {
            "x-api-key": f"{self.api_key}:{self.shared_secret}",
            "Authorization": f"Bearer {self.auth.get('access_token')}",
        }

    async def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        """Call the Etsy API (path relative to ETSY_API_BASE, or a full URL).

        Refreshes first if expired and once more on a 401; retries 429s with backoff.
        """
        if not self.auth:
            await self.load()
        if self._expired():
            await self.refresh()
        url = path if path.startswith("http") else f"{ETSY_API_BASE}{path}"
        resp = await self._send(method, url, **kwargs)
        if resp.status_code == 401:
            await self.refresh()
            resp = await self._send(method, url, **kwargs)
        return resp

    async def _send(self, method: str, url: str, **kwargs) -> httpx.Response:
        for attempt in range(RATE_LIMIT_MAX_RETRIES + 1):
            async with _http_client() as c:
                resp = await c.request(method, url, headers=self._headers(), **kwargs)
            if resp.status_code != 429 or attempt == RATE_LIMIT_MAX_RETRIES:
                return resp
            try:
                delay = float(resp.headers.get("retry-after") or 0)
            except ValueError:
                delay = 0
            delay = min(max(delay, RATE_LIMIT_BASE_DELAY_SECONDS * 2 ** attempt), RATE_LIMIT_MAX_DELAY_SECONDS)
            logger.warning(f"Etsy rate limited (429) on {method} {url}; retry {attempt + 1} in {delay:.0f}s")
            await _sleep(delay)
        return resp

    def expires_at_iso(self) -> Optional[str]:
        exp = self.auth.get("expires_at")
        return datetime.fromtimestamp(float(exp), timezone.utc).isoformat() if exp else None


async def check_etsy(settings_coll) -> Dict[str, Any]:
    """Verify the Etsy token works (refreshing if needed). Never returns token values."""
    auth = EtsyAuth(settings_coll)
    await auth.load()
    resp = await auth.request("GET", f"/shops/{auth.shop_id}")
    if resp.status_code >= 400:
        raise EtsyAPIError("Etsy GET shop", resp.status_code, resp.text)
    shop = resp.json()
    return {
        "ok": True,
        "shop_id": auth.shop_id,
        "shop_name": shop.get("shop_name"),
        "token_expires_at": auth.expires_at_iso(),
        "token_source": auth.auth.get("source"),
    }


async def wait_for_published_product(
    shop_id: int,
    product_id: str,
    timeout: float = LISTING_POLL_TIMEOUT_SECONDS,
    interval: float = LISTING_POLL_INTERVAL_SECONDS,
) -> Optional[Dict[str, Any]]:
    """Poll Printify until the product has its external Etsy listing ID (product["external"]["id"]).

    Returns the product, or None on timeout.
    """
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            product = await get_product(shop_id, product_id)
            if ((product or {}).get("external") or {}).get("id"):
                return product
        except Exception as e:  # transient Printify errors: keep polling until the deadline
            logger.info(f"Printify poll {attempt} for {product_id} failed, retrying: {_short(str(e))}")
        if time.monotonic() + interval > deadline:
            return None
        await asyncio.sleep(interval)


def back_mockup_src(product: Dict[str, Any]) -> Optional[str]:
    from printify_client import _pick_back_mockup

    img = _pick_back_mockup((product or {}).get("images") or [])
    return (img or {}).get("src")


# -----------------------------
# Image ranking on Etsy
# -----------------------------
def dhash(image_bytes: bytes, size: int = 8, crop: Optional[Tuple[float, float, float, float]] = None) -> int:
    """size*size-bit perceptual difference hash: survives resizing/re-encoding by Etsy's CDN."""
    from PIL import Image

    img = Image.open(io.BytesIO(image_bytes)).convert("L")
    if crop:
        w, h = img.size
        img = img.crop((int(w * crop[0]), int(h * crop[1]), int(w * crop[2]), int(h * crop[3])))
    img = img.resize((size + 1, size), Image.LANCZOS)
    px = list(img.getdata())
    bits = 0
    for row in range(size):
        for col in range(size):
            bits = (bits << 1) | (px[row * (size + 1) + col] > px[row * (size + 1) + col + 1])
    return bits


def mockup_hash(image_bytes: bytes) -> int:
    return dhash(image_bytes, MOCKUP_HASH_SIZE, MOCKUP_HASH_CROP)


def _hamming(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


async def _download(url: str) -> bytes:
    async with _http_client(30.0) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return resp.content


async def move_back_mockup_to_rank2(
    auth: "EtsyAuth", listing_id: str, back_src: Optional[str], exclude_ids: List[int]
) -> str:
    """Find the listing's back-print mockup by appearance and re-rank it to 2. Returns a note."""
    if not back_src:
        return "back mockup reorder skipped: Printify product has no back mockup"
    try:
        back_hash = mockup_hash(await _download(back_src))
    except Exception as e:
        return f"back mockup reorder skipped: could not load Printify back mockup ({_short(str(e), 120)})"
    resp = await auth.request("GET", f"/listings/{listing_id}/images")
    if resp.status_code >= 400:
        return f"back mockup reorder skipped: Etsy GET images HTTP {resp.status_code}"
    images = resp.json().get("results") or []
    scored = []
    for img in images:
        if img.get("listing_image_id") in exclude_ids:
            continue
        url = img.get("url_570xN") or img.get("url_fullxfull")
        if not url:
            continue
        try:
            scored.append((_hamming(back_hash, mockup_hash(await _download(url))), img))
        except Exception:
            continue
    if not scored:
        return "back mockup reorder skipped: no comparable Etsy images"
    scored.sort(key=lambda s: s[0])
    best_dist, best = scored[0]
    runner_up = scored[1][0] if len(scored) > 1 else None
    if best_dist > BACK_MATCH_MAX_DISTANCE or (runner_up is not None and runner_up - best_dist < BACK_MATCH_MIN_MARGIN):
        return (f"back mockup reorder skipped: no clear match (best distance {best_dist}, "
                f"runner-up {runner_up}); atmospheric photo is still rank 1")
    # Etsy doesn't shift the other images when one is re-ranked (it leaves a tie, and the tie
    # can show the old image first), so give every remaining image an explicit rank from 3 on.
    rest = [i for i in sorted(images, key=lambda i: int(i.get("rank") or 999))
            if i is not best and i.get("listing_image_id") not in exclude_ids]
    wanted = [(best, 2)] + [(img, n) for n, img in enumerate(rest, 3)]
    moved_back = False
    for img, rank in wanted:
        if int(img.get("rank") or 0) == rank:
            continue
        resp = await auth.request(
            "POST",
            f"/shops/{auth.shop_id}/listings/{listing_id}/images",
            files={"listing_image_id": (None, str(img["listing_image_id"])), "rank": (None, str(rank))},
        )
        if resp.status_code >= 400:
            what = "back mockup reorder" if img is best else f"re-ranking image {img['listing_image_id']} to {rank}"
            return f"{what} failed: HTTP {resp.status_code}: {_short(resp.text, 150)}"
        moved_back = moved_back or img is best
    if not moved_back:
        return "back mockup already at rank 2"
    return f"back mockup (image {best['listing_image_id']}) moved to rank 2"


async def upload_primary_photo(
    auth: "EtsyAuth", listing_id: str, image_bytes: bytes, file_name: str, back_src: Optional[str]
) -> str:
    """Upload the atmospheric photo as rank 1, then put the back mockup at rank 2. Returns a note.

    Raises EtsyAPIError if the upload itself fails (the reorder is best-effort).
    """
    resp = await auth.request(
        "POST",
        f"/shops/{auth.shop_id}/listings/{listing_id}/images",
        files={"image": (file_name, image_bytes, "image/png")},
        data={"rank": "1"},
    )
    if resp.status_code >= 400:
        raise EtsyAPIError("Etsy image upload", resp.status_code, resp.text)
    new_id = (resp.json() or {}).get("listing_image_id")
    try:
        return await move_back_mockup_to_rank2(auth, listing_id, back_src, [new_id] if new_id else [])
    except Exception as e:
        return f"back mockup reorder failed: {type(e).__name__}: {_short(str(e), 150)}"


async def log_listing_photo(log_coll, listing_id: str, product_id: Optional[str], source: str, note: str) -> None:
    await log_coll.update_one(
        {"listing_id": str(listing_id)},
        {"$set": {"listing_id": str(listing_id), "printify_product_id": product_id, "status": "success",
                  "source": source, "note": note, "uploaded_at": _now_iso()}},
        upsert=True,
    )


# -----------------------------
# Pipeline for one approved capsule
# -----------------------------
async def _generate_photo(capsule: Dict[str, Any]) -> Optional[bytes]:
    """Generate the atmospheric photo (imported lazily so a Gemini/config problem is a recorded failure)."""
    from generate_mockups import generate_background_image, infer_theme_prompt

    scene_prompt = infer_theme_prompt({
        "title": capsule.get("title") or "",
        "description": capsule.get("description") or "",
        "tags": (capsule.get("tags") or []) + [capsule.get("theme_seed") or ""],
    })
    return await generate_background_image(
        scene_prompt,
        capsule.get("back_concept") or "",
        design_image_bytes=base64.b64decode(capsule["back_image_b64"]),
    )


async def _record(capsules_coll, capsule_id: str, status: str, error: Optional[str] = None, **extra) -> Dict[str, Any]:
    update = {"etsy_photos_status": status, "etsy_photos_error": error, "etsy_photos_updated_at": _now_iso(), **extra}
    await capsules_coll.update_one({"id": capsule_id}, {"$set": update})
    if status in ("failed", "skipped"):
        logger.warning(f"Etsy photos {status} for capsule {capsule_id}: {error}")
    return update


async def run_etsy_photos(
    capsule_id: str,
    capsules_coll,
    settings_coll,
    poll_timeout: float = LISTING_POLL_TIMEOUT_SECONDS,
    poll_interval: float = LISTING_POLL_INTERVAL_SECONDS,
) -> Dict[str, Any]:
    """Generate + upload the atmospheric photo for one capsule and record the outcome on it."""
    try:
        capsule = await capsules_coll.find_one({"id": capsule_id}, {"_id": 0})
        if not capsule:
            return {"etsy_photos_status": "skipped", "etsy_photos_error": "Capsule not found"}
        pid = capsule.get("printify_product_id")
        if not pid:
            return await _record(capsules_coll, capsule_id, "skipped", "Capsule has not been pushed to Printify")
        if not capsule.get("back_image_b64"):
            return await _record(capsules_coll, capsule_id, "skipped", "Capsule has no back image")
        settings = await settings_coll.find_one({"id": "config"}, {"_id": 0}) or {}
        if not settings.get("printify_shop_id"):
            return await _record(capsules_coll, capsule_id, "skipped", "printify_shop_id is not configured in settings")

        auth = EtsyAuth(settings_coll)
        try:
            await auth.load()
        except EtsyConfigError as e:
            return await _record(capsules_coll, capsule_id, "skipped", str(e))

        product = await wait_for_published_product(
            int(settings["printify_shop_id"]), pid, timeout=poll_timeout, interval=poll_interval
        )
        if not product:
            return await _record(
                capsules_coll, capsule_id, "failed",
                f"Timed out after {int(poll_timeout)}s waiting for Printify to publish product {pid} "
                "to Etsy (no external Etsy listing ID yet). Retry with POST /api/capsules/{id}/etsy-photos.",
            )
        listing_id = str(product["external"]["id"])

        image_bytes = await _generate_photo(capsule)
        if not image_bytes:
            return await _record(capsules_coll, capsule_id, "failed", "Gemini returned no atmospheric image",
                                 etsy_listing_id=listing_id)

        cname = re.sub(r"[^a-z0-9]+", "_", (capsule.get("capsule_name") or pid).lower()).strip("_")[:40] or pid
        note = await upload_primary_photo(auth, listing_id, image_bytes, f"{cname}_etsy.png", back_mockup_src(product))
        await log_listing_photo(capsules_coll.database[PHOTO_LOG_COLLECTION], listing_id, pid, "approve", note)
        logger.info(f"Uploaded atmospheric photo to Etsy listing {listing_id} for capsule {capsule_id} ({pid}); {note}")
        return await _record(capsules_coll, capsule_id, "success", None, etsy_listing_id=listing_id, etsy_photos_note=note)
    except EtsyAPIError as e:
        return await _record(capsules_coll, capsule_id, "failed", str(e))
    except Exception as e:
        return await _record(capsules_coll, capsule_id, "failed", f"{type(e).__name__}: {_short(str(e))}")
