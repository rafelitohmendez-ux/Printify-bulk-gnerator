"""Atmospheric Etsy photos for approved capsules (runs on the deployed backend).

Flow (triggered after auto-push publishes a capsule, or via the retry endpoint):
  1. Poll Printify until the product has an external Etsy listing ID (Printify
     publishes to Etsy asynchronously, so the listing doesn't exist right away).
  2. Generate a themed atmospheric photo with Gemini (generate_mockups.py).
  3. Upload it to the Etsy listing as the primary image (rank 1).
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
import logging
import os
import re
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import httpx

from printify_client import get_product

logger = logging.getLogger("midnightrotation.etsy_photos")

ETSY_API_BASE = "https://openapi.etsy.com/v3/application"
ETSY_TOKEN_URL = "https://api.etsy.com/v3/public/oauth/token"
AUTH_DOC_ID = "etsy_auth"
TOKEN_EXPIRY_MARGIN_SECONDS = 60

LISTING_POLL_INTERVAL_SECONDS = 20
LISTING_POLL_TIMEOUT_SECONDS = 600

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

        if not doc.get("refresh_token"):
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
                raise EtsyAPIError("Etsy token refresh", resp.status_code, resp.text)
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
        """Call the Etsy API, refreshing first if expired and once more on a 401."""
        if not self.auth:
            await self.load()
        if self._expired():
            await self.refresh()
        url = f"{ETSY_API_BASE}{path}"
        async with _http_client() as c:
            resp = await c.request(method, url, headers=self._headers(), **kwargs)
        if resp.status_code == 401:
            await self.refresh()
            async with _http_client() as c:
                resp = await c.request(method, url, headers=self._headers(), **kwargs)
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


async def wait_for_etsy_listing_id(
    shop_id: int,
    product_id: str,
    timeout: float = LISTING_POLL_TIMEOUT_SECONDS,
    interval: float = LISTING_POLL_INTERVAL_SECONDS,
) -> Optional[str]:
    """Poll Printify until the product has its external Etsy listing ID, or None on timeout."""
    deadline = time.monotonic() + timeout
    attempt = 0
    while True:
        attempt += 1
        try:
            product = await get_product(shop_id, product_id)
            listing_id = ((product or {}).get("external") or {}).get("id")
            if listing_id:
                return str(listing_id)
        except Exception as e:  # transient Printify errors: keep polling until the deadline
            logger.info(f"Printify poll {attempt} for {product_id} failed, retrying: {_short(str(e))}")
        if time.monotonic() + interval > deadline:
            return None
        await asyncio.sleep(interval)


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

        listing_id = await wait_for_etsy_listing_id(
            int(settings["printify_shop_id"]), pid, timeout=poll_timeout, interval=poll_interval
        )
        if not listing_id:
            return await _record(
                capsules_coll, capsule_id, "failed",
                f"Timed out after {int(poll_timeout)}s waiting for Printify to publish product {pid} "
                "to Etsy (no external Etsy listing ID yet). Retry with POST /api/capsules/{id}/etsy-photos.",
            )

        image_bytes = await _generate_photo(capsule)
        if not image_bytes:
            return await _record(capsules_coll, capsule_id, "failed", "Gemini returned no atmospheric image",
                                 etsy_listing_id=listing_id)

        cname = re.sub(r"[^a-z0-9]+", "_", (capsule.get("capsule_name") or pid).lower()).strip("_")[:40] or pid
        resp = await auth.request(
            "POST",
            f"/shops/{auth.shop_id}/listings/{listing_id}/images",
            files={"image": (f"{cname}_etsy.png", image_bytes, "image/png")},
            data={"rank": "1"},
        )
        if resp.status_code >= 400:
            return await _record(
                capsules_coll, capsule_id, "failed",
                f"Etsy image upload failed: HTTP {resp.status_code}: {_short(resp.text)}",
                etsy_listing_id=listing_id,
            )
        logger.info(f"Uploaded atmospheric photo to Etsy listing {listing_id} for capsule {capsule_id} ({pid})")
        return await _record(capsules_coll, capsule_id, "success", None, etsy_listing_id=listing_id)
    except Exception as e:
        return await _record(capsules_coll, capsule_id, "failed", f"{type(e).__name__}: {_short(str(e))}")
