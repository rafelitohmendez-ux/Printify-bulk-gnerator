"""One-shot catalog fix for live listings (run on the server from the dashboard).

For each Printify product in the shop (newest first, up to `limit`):
  1. Etsy photos: if the listing's rank-1 image is an old generated (atmospheric) photo,
     move it to rank 4 and put the back-print mockup at rank 1.
  2. Text: rebuild the keyword-first title, 13 buyer-phrase tags and the description
     (with the AI-use disclosure, noting an AI scene photo if the listing has one),
     save it on Printify and re-sync only the text to Etsy (photos are left alone).

Progress is stored in settings {"id": "catalog_fix"} so the dashboard can poll it.
Products without an extractable back concept get photos fixed but text skipped.

Modes:
  fix    - the full fix above (all products, or the newest live one with limit=1)
  sync   - only re-send each live product's current Printify text to Etsy (no Gemini, no photos)
  retry  - redo only the products that failed in the previous run
  price  - set every product's price (S-XL = the base price; bigger sizes keep their extra
           Printify cost on top) and re-send prices to Etsy
  photos - re-run the photo arrangement on live listings and list the ones where the
           back-print mockup could not be moved to the first slot

Printify rate-limits publishing to Etsy (about 200 per 30 minutes), so publishes are paced
and a 429 is waited out and retried.
"""
import asyncio
import logging
import math
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import etsy_photos
from bulk_seo_update import generate_seo
from printify_client import (
    PrintifyError, list_products, publish_product_prices, publish_product_text, update_product,
)
from seo import build_description, extract_back_concept, extract_capsule_name, filter_etsy_tags, infer_category, MAX_TAGS

logger = logging.getLogger("midnightrotation.catalog_fix")

MODES = ("fix", "sync", "retry", "price", "photos")
STATUS_DOC_ID = "catalog_fix"
MIN_PROFIT_CENTS = 500  # never price a variant below its Printify cost + $5
MAX_LOG_LINES = 60
DELAY_SECONDS = 1.0
PUBLISH_INTERVAL_SECONDS = 10.0   # ~180 publishes per 30 min, under Printify's ~200
PUBLISH_ATTEMPTS = 5
RATE_LIMIT_WAIT_SECONDS = 60.0
MAX_RATE_LIMIT_WAIT_SECONDS = 300.0
_sleep = asyncio.sleep  # patched in tests
_clock = time.monotonic  # patched in tests
_last_publish = [0.0]


class PublishFailed(Exception):
    """Printify text saved, but re-syncing it to Etsy failed (retry only needs the publish)."""


async def publish_paced(shop_id: int, pid: str, on_wait=None, prices: bool = False) -> None:
    """Re-send text (or prices) to Etsy, spaced out and retried on Printify's 429 rate limit."""
    for attempt in range(1, PUBLISH_ATTEMPTS + 1):
        gap = PUBLISH_INTERVAL_SECONDS - (_clock() - _last_publish[0])
        if gap > 0:
            await _sleep(gap)
        _last_publish[0] = _clock()
        try:
            await (publish_product_prices if prices else publish_product_text)(shop_id, pid)
            return
        except PrintifyError as e:
            if e.status != 429 or attempt == PUBLISH_ATTEMPTS:
                raise PublishFailed(str(e)[:300]) from e
            wait = min(e.retry_after or RATE_LIMIT_WAIT_SECONDS * attempt, MAX_RATE_LIMIT_WAIT_SECONDS)
            if on_wait:
                await on_wait(wait)
            await _sleep(wait)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _all_products(shop_id: int) -> List[Dict[str, Any]]:
    products: List[Dict[str, Any]] = []
    page = 1
    while True:
        result = await list_products(shop_id, page=page, limit=50)
        batch = result.get("data") or []
        products.extend(batch)
        if len(batch) < 50:
            return products
        page += 1


async def get_status(settings_coll) -> Dict[str, Any]:
    doc = await settings_coll.find_one({"id": STATUS_DOC_ID}, {"_id": 0}) or {}
    doc.pop("id", None)
    return doc or {"state": "idle"}


async def fix_one(
    product: Dict[str, Any],
    shop_id: int,
    auth: Optional["etsy_photos.EtsyAuth"],
    capsules_coll,
    photo_listing_ids: set,
    on_wait=None,
) -> str:
    """Fix one product. Returns a one-line summary; raises on a hard failure."""
    pid = str(product.get("id"))
    listing_id = str((product.get("external") or {}).get("id") or "") or None
    parts: List[str] = []

    # 1. Photos on Etsy
    has_ai_scene = bool(listing_id and listing_id in photo_listing_ids)
    if listing_id and auth:
        photo_id, why = await etsy_photos.generated_first_image_id(auth, listing_id, product)
        if photo_id:
            has_ai_scene = True
        note = await etsy_photos.arrange_listing_photos(
            auth, listing_id, etsy_photos.back_mockup_src(product), [photo_id] if photo_id else []
        )
        parts.append(f"photos: {why}; {note}")
    elif listing_id:
        parts.append("photos: skipped (Etsy not configured)")
    else:
        parts.append("photos: skipped (not on Etsy yet)")

    # 2. Text
    capsule = await capsules_coll.find_one({"printify_product_id": pid}, {"_id": 0, "front_image_b64": 0, "back_image_b64": 0})
    name = (capsule or {}).get("capsule_name") or extract_capsule_name(product.get("title") or "")
    back = (capsule or {}).get("back_summary") or extract_back_concept(product.get("description") or "") \
        or (capsule or {}).get("back_concept") or ""
    if not name or not back:
        parts.append("text: skipped (no design name or back concept found)")
        return " | ".join(parts)
    category = infer_category(" ".join([product.get("title") or "", " ".join(product.get("tags") or []),
                                        (capsule or {}).get("theme_seed") or ""]))
    seo_data = await generate_seo(name, back, category=category)
    if not seo_data:
        raise RuntimeError("Gemini returned no SEO data")
    title = seo_data["title"][:140]
    tags = filter_etsy_tags(seo_data["tags"])[:MAX_TAGS]
    description = build_description(name, back, ai_scene_photo=has_ai_scene)
    await update_product(shop_id, pid, {"title": title, "tags": tags, "description": description})
    if capsule:
        await capsules_coll.update_one(
            {"id": capsule["id"]},
            {"$set": {"seo_title": title, "seo_tags": tags, "seo_description": description}},
        )
    if listing_id:
        await publish_paced(shop_id, pid, on_wait)
    parts.append(f"text: {title}" + (" (synced to Etsy)" if listing_id else " (Printify only)"))
    return " | ".join(parts)


def plan_prices(product: Dict[str, Any], base_cents: int) -> Optional[Dict[str, Any]]:
    """New price per enabled variant: base for the cheapest size, plus the extra Printify cost
    of bigger sizes (rounded up to whole dollars). Never below cost + MIN_PROFIT_CENTS.
    Returns {"variants": [...], "min_price", "max_price", "min_profit", "max_profit"} or None."""
    variants = product.get("variants") or []
    enabled = [v for v in variants if v.get("is_enabled")]
    if not enabled:
        return None
    costs = {v["id"]: int(v.get("cost") or 0) for v in enabled}
    base_cost = min(costs.values())
    out, prices, profits = [], [], []
    for v in variants:
        if not v.get("is_enabled"):
            out.append({"id": v["id"], "price": v.get("price"), "is_enabled": False})
            continue
        cost = costs[v["id"]]
        price = base_cents + int(math.ceil(max(0, cost - base_cost) / 100.0)) * 100
        if cost and price < cost + MIN_PROFIT_CENTS:
            price = int(math.ceil((cost + MIN_PROFIT_CENTS) / 100.0)) * 100 - 1
        out.append({"id": v["id"], "price": price, "is_enabled": True})
        prices.append(price)
        if cost:
            profits.append(price - cost)
    return {
        "variants": out, "min_price": min(prices), "max_price": max(prices),
        "min_profit": min(profits) if profits else None, "max_profit": max(profits) if profits else None,
    }


def _usd(cents: Optional[int]) -> str:
    return "?" if cents is None else f"${cents / 100:.2f}"


async def reprice_one(product: Dict[str, Any], shop_id: int, base_cents: int, on_wait=None) -> str:
    pid = str(product.get("id"))
    plan = plan_prices(product, base_cents)
    if not plan:
        return "price: skipped (no enabled sizes)"
    current = {v["id"]: v.get("price") for v in product.get("variants") or []}
    if all(current.get(v["id"]) == v["price"] for v in plan["variants"]):
        return f"price: already {_usd(plan['min_price'])}"
    await update_product(shop_id, pid, {"variants": plan["variants"]})
    if (product.get("external") or {}).get("id"):
        await publish_paced(shop_id, pid, on_wait, prices=True)
        where = "synced to Etsy"
    else:
        where = "Printify only"
    rng = _usd(plan["min_price"]) if plan["min_price"] == plan["max_price"] else \
        f"{_usd(plan['min_price'])}-{_usd(plan['max_price'])}"
    profit = "" if plan["min_profit"] is None else \
        f", profit before Etsy fees {_usd(plan['min_profit'])}-{_usd(plan['max_profit'])}"
    return f"price: {rng}{profit} ({where})"


def listing_url(listing_id: str) -> str:
    return f"https://www.etsy.com/listing/{listing_id}"


async def check_photos_one(product: Dict[str, Any], auth) -> Dict[str, Any]:
    """Re-run the photo arrangement for one live listing. Returns {"note", "attention"}."""
    listing_id = str((product.get("external") or {}).get("id"))
    photo_id, why = await etsy_photos.generated_first_image_id(auth, listing_id, product)
    note = await etsy_photos.arrange_listing_photos(
        auth, listing_id, etsy_photos.back_mockup_src(product), [photo_id] if photo_id else []
    )
    attention = None
    if "back mockup not moved" in note or "failed" in note:
        attention = {
            "id": str(product.get("id")), "title": (product.get("title") or "")[:90],
            "url": listing_url(listing_id), "reason": note[:200],
        }
    return {"note": f"photos: {why}; {note}", "attention": attention}


async def run_catalog_fix(
    settings_coll, capsules_coll, limit: Optional[int] = None, mode: str = "fix",
    price_cents: Optional[int] = None,
) -> Dict[str, Any]:
    previous = await get_status(settings_coll)
    status: Dict[str, Any] = {
        "id": STATUS_DOC_ID, "state": "running", "mode": mode, "limit": limit, "started_at": _now(),
        "finished_at": None, "total": 0, "done": 0, "failed": 0, "failed_ids": [], "log": [], "error": None,
        "needs_attention": [], "price_cents": price_cents if mode == "price" else None,
    }

    async def save():
        status["log"] = status["log"][-MAX_LOG_LINES:]
        await settings_coll.update_one({"id": STATUS_DOC_ID}, {"$set": status}, upsert=True)

    async def on_wait(seconds):
        status["log"].append(f"Printify rate limit: waiting {int(seconds)}s before retrying...")
        await save()

    await save()
    try:
        settings = await settings_coll.find_one({"id": "config"}, {"_id": 0}) or {}
        if not settings.get("printify_shop_id"):
            raise RuntimeError("printify_shop_id is not configured in settings")
        shop_id = int(settings["printify_shop_id"])

        auth = None
        photo_listing_ids: set = set()
        if mode == "price" and not price_cents:
            raise RuntimeError("No listing price set")
        if mode not in ("sync", "price"):
            auth = etsy_photos.EtsyAuth(settings_coll)
            try:
                await auth.load()
            except etsy_photos.EtsyConfigError as e:
                auth = None
                status["log"].append(f"Etsy not configured, photos will be skipped: {e}")
            photo_listing_ids = {
                d["listing_id"] async for d in capsules_coll.database[etsy_photos.PHOTO_LOG_COLLECTION].find(
                    {"status": "success"}, {"listing_id": 1, "_id": 0})
            }

        products = await _all_products(shop_id)
        stages: Dict[str, str] = {}
        if mode in ("sync", "photos"):
            products = [p for p in products if (p.get("external") or {}).get("id")]
        if mode == "photos" and not auth:
            raise RuntimeError("Etsy is not configured (ETSY_API_KEY / ETSY_SHARED_SECRET on Render)")
        elif mode == "retry":
            stages = {f["id"]: f.get("stage", "fix") for f in previous.get("failed_ids") or []}
            products = [p for p in products if str(p.get("id")) in stages]
            price_cents = previous.get("price_cents") or price_cents
        if limit:
            # A test run should exercise a live listing: prefer products already on Etsy
            live = [p for p in products if (p.get("external") or {}).get("id")]
            products = (live or products)[:limit]
        status["total"] = len(products)
        await save()

        for product in products:
            pid = str(product.get("id"))
            label = f"{(product.get('title') or '')[:60]} ({pid})"
            try:
                stage = stages.get(pid)
                if mode == "sync" or stage == "publish":
                    await publish_paced(shop_id, pid, on_wait)
                    summary = "text synced to Etsy"
                elif stage == "publish_prices":
                    await publish_paced(shop_id, pid, on_wait, prices=True)
                    summary = "prices synced to Etsy"
                elif mode == "price" or stage == "price":
                    summary = await reprice_one(product, shop_id, int(price_cents), on_wait)
                elif (mode == "photos" or stage == "photos") and auth:
                    result = await check_photos_one(product, auth)
                    summary = result["note"]
                    if result["attention"]:
                        status["needs_attention"].append(result["attention"])
                else:
                    summary = await fix_one(product, shop_id, auth, capsules_coll, photo_listing_ids, on_wait)
                status["done"] += 1
                status["log"].append(f"OK {label}: {summary}")
            except PublishFailed as e:
                prices = mode == "price" or stages.get(pid) in ("price", "publish_prices")
                status["failed"] += 1
                status["failed_ids"].append({"id": pid, "stage": "publish_prices" if prices else "publish"})
                what = "prices" if prices else "text"
                status["log"].append(f"FAILED {label}: {what} saved in Printify, Etsy sync failed: {e}")
            except etsy_photos.EtsyAPIError as e:
                status["failed"] += 1
                status["failed_ids"].append({"id": pid, "stage": stages.get(pid) or (mode if mode in ("price", "photos") else "fix")})
                status["log"].append(f"FAILED {label}: {e}")
                if e.status in (403, 429):
                    raise RuntimeError(f"Stopped: Etsy refused or rate limited ({e.status})")
            except Exception as e:
                status["failed"] += 1
                status["failed_ids"].append({"id": pid, "stage": stages.get(pid) or (mode if mode in ("price", "photos") else "fix")})
                status["log"].append(f"FAILED {label}: {type(e).__name__}: {str(e)[:300]}")
            await save()
            await _sleep(DELAY_SECONDS)
        status["state"] = "done"
    except Exception as e:
        logger.exception("catalog fix failed")
        status["state"] = "failed"
        status["error"] = f"{type(e).__name__}: {str(e)[:300]}"
    status["finished_at"] = _now()
    await save()
    return status
