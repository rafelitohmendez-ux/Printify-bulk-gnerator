"""One-shot catalog fix for live listings (run on the server from the dashboard).

For each Printify product in the shop (newest first, up to `limit`):
  1. Etsy photos: if the listing's rank-1 image is an old generated (atmospheric) photo,
     move it to rank 4 and put the back-print mockup at rank 1.
  2. Text: rebuild the keyword-first title, 13 buyer-phrase tags and the description
     (with the AI-use disclosure, noting an AI scene photo if the listing has one),
     save it on Printify and re-sync only the text to Etsy (photos are left alone).

Progress is stored in settings {"id": "catalog_fix"} so the dashboard can poll it.
Products without an extractable back concept get photos fixed but text skipped.
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import etsy_photos
from bulk_seo_update import generate_seo
from printify_client import list_products, publish_product_text, update_product
from seo import build_description, extract_back_concept, extract_capsule_name, filter_etsy_tags, infer_category, MAX_TAGS

logger = logging.getLogger("midnightrotation.catalog_fix")

STATUS_DOC_ID = "catalog_fix"
MAX_LOG_LINES = 60
DELAY_SECONDS = 1.0
_sleep = asyncio.sleep  # patched in tests


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
    if listing_id:
        await publish_product_text(shop_id, pid)
    if capsule:
        await capsules_coll.update_one(
            {"id": capsule["id"]},
            {"$set": {"seo_title": title, "seo_tags": tags, "seo_description": description}},
        )
    parts.append(f"text: {title}" + (" (synced to Etsy)" if listing_id else " (Printify only)"))
    return " | ".join(parts)


async def run_catalog_fix(settings_coll, capsules_coll, limit: Optional[int] = None) -> Dict[str, Any]:
    status: Dict[str, Any] = {
        "id": STATUS_DOC_ID, "state": "running", "limit": limit, "started_at": _now(), "finished_at": None,
        "total": 0, "done": 0, "failed": 0, "log": [], "error": None,
    }

    async def save():
        status["log"] = status["log"][-MAX_LOG_LINES:]
        await settings_coll.update_one({"id": STATUS_DOC_ID}, {"$set": status}, upsert=True)

    await save()
    try:
        settings = await settings_coll.find_one({"id": "config"}, {"_id": 0}) or {}
        if not settings.get("printify_shop_id"):
            raise RuntimeError("printify_shop_id is not configured in settings")
        shop_id = int(settings["printify_shop_id"])

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
        if limit:
            products = products[:limit]
        status["total"] = len(products)
        await save()

        for product in products:
            label = f"{(product.get('title') or '')[:60]} ({product.get('id')})"
            try:
                summary = await fix_one(product, shop_id, auth, capsules_coll, photo_listing_ids)
                status["done"] += 1
                status["log"].append(f"OK {label}: {summary}")
            except etsy_photos.EtsyAPIError as e:
                status["failed"] += 1
                status["log"].append(f"FAILED {label}: {e}")
                if e.status in (403, 429):
                    raise RuntimeError(f"Stopped: Etsy refused or rate limited ({e.status})")
            except Exception as e:
                status["failed"] += 1
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
