"""Printify API v1 client wrapper."""
import base64
import asyncio
import logging
import os
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("printify")

_background_tasks: set = set()

PRINTIFY_BASE_URL = "https://api.printify.com/v1"

# Blueprint ID 6 = Gildan 5000 "Unisex Heavy Cotton Tee" on Printify catalog
GILDAN_5000_BLUEPRINT_ID = 6

DEFAULT_VARIANT_PRICE_CENTS = 4499  # $44.99


class PrintifyError(Exception):
    pass


def _get_token() -> str:
    token = os.environ.get("PRINTIFY_API_TOKEN")
    if not token:
        raise PrintifyError("PRINTIFY_API_TOKEN not configured in backend/.env")
    return token


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        base_url=PRINTIFY_BASE_URL,
        headers={
            "Authorization": f"Bearer {_get_token()}",
            "User-Agent": "MidnightRotation-Dashboard/1.0",
            "Content-Type": "application/json;charset=utf-8",
        },
        timeout=60.0,
    )


async def _check(resp: httpx.Response) -> Any:
    if resp.status_code >= 400:
        try:
            detail = resp.json()
        except Exception:
            detail = resp.text
        raise PrintifyError(f"Printify {resp.request.method} {resp.request.url.path} -> {resp.status_code}: {detail}")
    if resp.status_code == 204 or not resp.content:
        return None
    return resp.json()


async def list_shops() -> List[Dict[str, Any]]:
    async with _client() as c:
        return await _check(await c.get("/shops.json"))


async def list_print_providers(blueprint_id: int = GILDAN_5000_BLUEPRINT_ID) -> List[Dict[str, Any]]:
    async with _client() as c:
        return await _check(await c.get(f"/catalog/blueprints/{blueprint_id}/print_providers.json"))


async def list_variants(blueprint_id: int, print_provider_id: int, show_out_of_stock: int = 0) -> Dict[str, Any]:
    async with _client() as c:
        return await _check(
            await c.get(
                f"/catalog/blueprints/{blueprint_id}/print_providers/{print_provider_id}/variants.json",
                params={"show-out-of-stock": show_out_of_stock},
            )
        )


async def upload_image_base64(file_name: str, base64_data: str) -> Dict[str, Any]:
    async with _client() as c:
        return await _check(
            await c.post(
                "/uploads/images.json",
                json={"file_name": file_name, "contents": base64_data},
            )
        )


async def create_product(shop_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with _client() as c:
        return await _check(await c.post(f"/shops/{shop_id}/products.json", json=payload))


async def get_product(shop_id: int, product_id: str) -> Dict[str, Any]:
    async with _client() as c:
        return await _check(await c.get(f"/shops/{shop_id}/products/{product_id}.json"))


async def list_products(shop_id: int, page: int = 1, limit: int = 50) -> Dict[str, Any]:
    """List products for a shop directly from Printify (paginated).
    Use this instead of relying on local DB records, since those may be
    incomplete after an infrastructure migration."""
    async with _client() as c:
        return await _check(
            await c.get(
                f"/shops/{shop_id}/products.json",
                params={"page": page, "limit": limit},
            )
        )


async def update_product(shop_id: int, product_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    async with _client() as c:
        return await _check(
            await c.put(f"/shops/{shop_id}/products/{product_id}.json", json=payload)
        )


async def update_back_print_image(shop_id: int, product_id: str, new_image_id: str) -> Dict[str, Any]:
    """Swap the back-placeholder image on an existing product's print area,
    in place - preserves variant_ids and the image's x/y/scale/angle.
    Used to push a corrected design without creating a new product."""
    product = await get_product(shop_id, product_id)
    print_areas = product.get("print_areas") or []

    target_area = None
    target_placeholder = None
    for area in print_areas:
        for ph in area.get("placeholders") or []:
            if ph.get("position") == "back" and ph.get("images"):
                target_area = area
                target_placeholder = ph
                break
        if target_area:
            break

    if not target_area or not target_placeholder:
        raise PrintifyError(f"Product {product_id} has no back placeholder with an existing image")

    old_image = target_placeholder["images"][0]
    new_placeholders = [{
        "position": "back",
        "images": [{
            "id": new_image_id,
            "x": old_image.get("x", 0.5),
            "y": old_image.get("y", 0.5),
            "scale": old_image.get("scale", 1.0),
            "angle": old_image.get("angle", 0),
        }],
    }]

    return await update_product(shop_id, product_id, {
        "print_areas": [{
            "variant_ids": target_area.get("variant_ids", []),
            "placeholders": new_placeholders,
        }]
    })


async def delete_product(shop_id: int, product_id: str) -> None:
    """Delete a product from Printify (e.g. draft cleanup)."""
    async with _client() as c:
        await _check(await c.delete(f"/shops/{shop_id}/products/{product_id}.json"))


async def publish_product(shop_id: int, product_id: str) -> None:
    """Publish a product live to its sales channel (e.g. Etsy)."""
    async with _client() as c:
        await _check(
            await c.post(
                f"/shops/{shop_id}/products/{product_id}/publish.json",
                json={"title": True, "description": True, "images": True,
                      "variants": True, "tags": True, "keyFeatures": True,
                      "shipping_template": True}
            )
        )


def _pick_front_mockup(images: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """From a product's image list, pick the front-view mockup that best
    showcases a small left-chest design. Prefer 'close'/'detail'/'chest' URLs."""
    fronts = [img for img in images if (img.get("position") or "").lower() == "front"]
    if not fronts:
        return None
    for keyword in ("close", "detail", "zoom", "chest", "crop"):
        for img in fronts:
            if keyword in (img.get("src") or "").lower():
                return img
    return fronts[0]


async def prioritize_front_mockup(shop_id: int, product_id: str) -> Optional[str]:
    """Mark a front-view mockup as default + selected for publishing.
    Returns src of chosen image, or None if no update made."""
    product = await get_product(shop_id, product_id)
    images = product.get("images") or []
    if not images:
        return None
    chosen = _pick_front_mockup(images)
    if not chosen:
        return None
    chosen_src = chosen.get("src")
    updated = []
    for img in images:
        is_chosen = img.get("src") == chosen_src
        updated.append({
            "src": img.get("src"),
            "variant_ids": img.get("variant_ids", []),
            "position": img.get("position"),
            "is_default": is_chosen,
            "is_selected_for_publishing": is_chosen
            or bool(img.get("is_selected_for_publishing")),
        })
    await update_product(shop_id, product_id, {"images": updated})
    return chosen_src


def _pick_back_mockup(images: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """From a product's image list, pick the best back-view mockup."""
    backs = [img for img in images if (img.get("position") or "").lower() == "back"]
    if not backs:
        return None
    for keyword in ("flat", "full", "lifestyle"):
        for img in backs:
            if keyword in (img.get("src") or "").lower():
                return img
    return backs[0]


async def prioritize_back_mockup(shop_id: int, product_id: str) -> Optional[str]:
    """Mark a back-view mockup as default + selected for publishing.
    Returns src of chosen image, or None if no update made."""
    product = await get_product(shop_id, product_id)
    images = product.get("images") or []
    if not images:
        return None
    chosen = _pick_back_mockup(images)
    if not chosen:
        return None
    chosen_src = chosen.get("src")
    updated = []
    for img in images:
        is_chosen = img.get("src") == chosen_src
        updated.append({
            "src": img.get("src"),
            "variant_ids": img.get("variant_ids", []),
            "position": img.get("position"),
            "is_default": is_chosen,
            "is_selected_for_publishing": is_chosen or bool(img.get("is_selected_for_publishing")),
        })
    await update_product(shop_id, product_id, {"images": updated})
    return chosen_src


async def _retry_prioritize_back_mockup(shop_id: int, product_id: str) -> None:
    """Background retry: Printify needs time to finish generating mockups, and
    sometimes overwrites the default-image flag while still doing so, so we set
    it once after an initial delay and again after a longer one."""
    try:
        await asyncio.sleep(15)
        await prioritize_back_mockup(shop_id, product_id)
        await asyncio.sleep(30)
        await prioritize_back_mockup(shop_id, product_id)
    except Exception:
        logger.warning("background prioritize_back_mockup retry failed (non-fatal)", exc_info=True)


def _pick_black_variants(variants_payload: Dict[str, Any], max_variants: int = 8) -> List[int]:
    """From a variants response, return variant IDs whose color is black."""
    variants = variants_payload.get("variants") or []
    black_ids: List[int] = []
    for v in variants:
        options = v.get("options") or {}
        color = (options.get("color") or "").strip().lower()
        title = (v.get("title") or "").lower()
        if "black" in color or "black" in title:
            black_ids.append(int(v.get("id")))
    return black_ids[:max_variants]


async def push_capsule_as_draft(
    shop_id: int,
    print_provider_id: int,
    capsule: Dict[str, Any],
) -> Dict[str, Any]:
    """Upload both images and create a draft product on Printify.

    Returns the created product dict (has 'id' which is the Printify product id).
    """
    # 1. Upload images
    front_b64 = capsule.get("front_image_b64")
    back_b64 = capsule.get("back_image_b64")
    if not back_b64:
        raise PrintifyError("Capsule missing back image data")

    cname = (capsule.get("capsule_name") or "capsule").replace(" ", "_").lower()
    back_upload = await upload_image_base64(f"{cname}_back.png", back_b64)
    back_image_id = back_upload.get("id")
    if not back_image_id:
        raise PrintifyError(f"Image upload missing id: back={back_upload}")

    front_image_id = None
    if front_b64:
        front_upload = await upload_image_base64(f"{cname}_front.png", front_b64)
        front_image_id = front_upload.get("id")
        if not front_image_id:
            raise PrintifyError(f"Image upload missing id: front={front_upload}")

    # 2. Get black variants for this blueprint+provider
    variants_payload = await list_variants(GILDAN_5000_BLUEPRINT_ID, print_provider_id, show_out_of_stock=0)
    variant_ids = _pick_black_variants(variants_payload)
    if not variant_ids:
        raise PrintifyError(
            f"No black variants found for blueprint {GILDAN_5000_BLUEPRINT_ID} / "
            f"provider {print_provider_id}. Verify this provider carries black Gildan 5000."
        )

    # 3. Construct product payload: back placement always, front (left-chest)
    # placement only if a front image was supplied.
    variants_array = [
        {"id": vid, "price": DEFAULT_VARIANT_PRICE_CENTS, "is_enabled": True}
        for vid in variant_ids
    ]
    placeholders = [
        {
            "position": "back",
            "images": [
                {
                    "id": back_image_id,
                    "x": 0.5,
                    "y": 0.5,
                    "scale": 1.0,
                    "angle": 0,
                }
            ],
        },
    ]
    if front_image_id:
        placeholders.insert(0, {
            "position": "front",
            "images": [
                {
                    "id": front_image_id,
                    "x": 0.27,  # left-chest area
                    "y": 0.32,
                    "scale": 0.18,
                    "angle": 0,
                }
            ],
        })
    print_areas = [{"variant_ids": variant_ids, "placeholders": placeholders}]

    product_payload = {
        "title": capsule.get("title") or capsule.get("capsule_name", "Untitled"),
        "description": capsule.get("description") or "",
        "blueprint_id": GILDAN_5000_BLUEPRINT_ID,
        "print_provider_id": print_provider_id,
        "variants": variants_array,
        "print_areas": print_areas,
        "tags": (capsule.get("tags") or [])[:13],
    }

    # 4. Create as draft (no publish call - product stays unpublished until user clicks
    # publish in Printify or via a separate endpoint).
    product = await create_product(shop_id, product_payload)

    # 5. Promote a back-view mockup as default so the oversized back graphic is the
    # storefront thumbnail — it's the main visual statement of the design.
    pid = product.get("id")
    if pid:
        task = asyncio.create_task(_retry_prioritize_back_mockup(shop_id, str(pid)))
        _background_tasks.add(task)
        task.add_done_callback(_background_tasks.discard)

    return product
