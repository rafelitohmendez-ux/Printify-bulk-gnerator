"""
Contextual Background Thumbnail Generator
==========================================
For each Printify product, infers its gothic sub-theme from the title/tags,
generates a matching dark atmospheric background via Gemini image generation,
downloads the product's existing back-print mockup, composites the mockup
onto the new background with Pillow, uploads the composite to Printify, and
sets it as the default product thumbnail.

Usage:
    python generate_mockups.py                        # dry run, all products
    python generate_mockups.py --apply                 # apply to all products
    python generate_mockups.py --product-id XYZ        # dry run, single product
    python generate_mockups.py --product-id XYZ --apply
"""
import argparse
import asyncio
import base64
import io
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from google import genai
from google.genai import types
from PIL import Image, ImageOps

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import (  # noqa: E402
    get_product,
    list_products,
    update_product,
    upload_image_base64,
    PrintifyError,
)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# Keyword -> background scene prompt. Matched against the product's title +
# tags + description (lowercased). First match wins, so more specific themes
# are listed before broader ones.
THEME_BACKGROUNDS = [
    ("bone church", "a cavernous gothic ossuary interior, walls stacked floor to ceiling with bones and skulls, cold shafts of light through stained glass, dust hanging in the air"),
    ("ossuary", "a cavernous gothic ossuary interior, walls stacked floor to ceiling with bones and skulls, cold shafts of light through stained glass, dust hanging in the air"),
    ("catacomb", "a cavernous gothic ossuary interior, walls stacked floor to ceiling with bones and skulls, cold shafts of light through stained glass, dust hanging in the air"),
    ("asylum", "a decaying plague-era asylum ward corridor, peeling medical gothic walls, rusted iron beds, dim greenish light, quarantine signage"),
    ("plague", "a decaying plague ward corridor, cracked apothecary shelving, dim greenish light, medieval death-aesthetic gloom"),
    ("y2k", "a neon-lit urban decay alley at night, glitching CRT static glow, wet asphalt reflections, millennium-bug dread"),
    ("cyber mortuary", "a server room lit by dying fluorescent tubes, tangled cables, dead monitors, cold data-tomb atmosphere"),
    ("digital decay", "a server room lit by dying fluorescent tubes, tangled cables, dead monitors, cold data-tomb atmosphere"),
    ("techno", "a neon-lit urban decay alley at night, glitching CRT static glow, wet asphalt reflections, millennium-bug dread"),
    ("cyber", "a neon-lit urban decay alley at night, glitching CRT static glow, wet asphalt reflections, millennium-bug dread"),
    ("brutalist", "a raw concrete brutalist interior, monolithic columns, cold overhead light, cathedral-scale emptiness"),
    ("occult", "a candlelit stone ritual chamber, sigils scorched into the floor, deep enveloping shadow"),
    ("storm", "a lightning-lit stormfront over a flooded industrial wasteland, dark rolling clouds, driving rain"),
    ("crow", "a rain-slicked graveyard at dusk, crows perched on broken headstones, fog rolling between crypts"),
    ("folk horror", "a torch-lit forest clearing with a wicker effigy, rural cult ritual ground, deep autumn dusk"),
    ("deep sea", "a sunken cathedral underwater, bioluminescent light filtering through drowned stone arches"),
    ("diesel", "an oil-slicked engine yard at night, diesel haze, rusted pipework, industrial fuel-ritual atmosphere"),
    ("industrial", "an abandoned factory floor, rusted machinery, hanging chains, shafts of dusty light through broken skylights"),
]
DEFAULT_BACKGROUND = "a dim industrial gothic interior, rusted metal surfaces, cold directional light, dust hanging in the air"

BACKGROUND_STYLE_PREFIX = (
    "A stark, moody, dark atmospheric photo-real background scene for a streetwear "
    "product photo. High contrast, cinematic low-key lighting, desaturated gothic "
    "industrial color palette. NO people, NO text, NO logos, NO shirts, NO product "
    "of any kind in frame - an empty environment/backdrop only. "
)


def infer_theme_prompt(product: Dict[str, Any]) -> str:
    """Infer a background scene prompt from the product's title/tags/description."""
    haystack = " ".join([
        product.get("title") or "",
        product.get("description") or "",
        " ".join(product.get("tags") or []),
    ]).lower()
    for keyword, prompt in THEME_BACKGROUNDS:
        if keyword in haystack:
            return prompt
    return DEFAULT_BACKGROUND


async def generate_background_image(scene_prompt: str) -> Optional[bytes]:
    """Generate a background image via Gemini. Returns raw image bytes, or None."""
    try:
        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-2.5-flash-image",
            contents=BACKGROUND_STYLE_PREFIX + scene_prompt,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )
    except Exception as exc:
        print(f"    Gemini background generation error: {exc}")
        return None
    if not response.candidates:
        return None
    for part in response.candidates[0].content.parts:
        if getattr(part, "inline_data", None) and part.inline_data.data:
            return part.inline_data.data
    return None


def _pick_back_image(images: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """From a product's image list, pick the best back-view mockup."""
    backs = [img for img in images if (img.get("position") or "").lower() == "back"]
    if not backs:
        return None
    for keyword in ("flat", "full", "lifestyle"):
        for img in backs:
            if keyword in (img.get("src") or "").lower():
                return img
    return backs[0]


async def download_image(url: str) -> bytes:
    async with httpx.AsyncClient(timeout=60.0) as c:
        resp = await c.get(url)
        resp.raise_for_status()
        return resp.content


def composite_mockup_on_background(background_bytes: bytes, mockup_bytes: bytes) -> bytes:
    """Cut the shirt mockup out of its (typically flat/light) backdrop and
    composite it onto the generated atmospheric background.

    The cutout is a luminance-threshold matte, not true background removal —
    Printify mockups are shot on a plain light backdrop, so this holds up well
    in practice, but can leave a faint halo on busy/light-colored mockups.
    """
    bg = Image.open(io.BytesIO(background_bytes)).convert("RGBA")
    mockup = Image.open(io.BytesIO(mockup_bytes)).convert("RGBA")

    # Scale mockup to ~85% of background height, preserving aspect ratio.
    target_h = int(bg.height * 0.85)
    scale = target_h / mockup.height
    mockup = mockup.resize((max(1, int(mockup.width * scale)), target_h), Image.LANCZOS)

    # Cut out the near-white/light backdrop the mockup was shot on.
    gray = ImageOps.grayscale(mockup)
    mask = gray.point(lambda p: 0 if p > 235 else 255)
    mockup.putalpha(mask)

    x = (bg.width - mockup.width) // 2
    y = (bg.height - mockup.height) // 2
    bg.paste(mockup, (x, y), mockup)

    out = io.BytesIO()
    bg.convert("RGB").save(out, format="PNG")
    return out.getvalue()


async def process_product(shop_id: int, product: Dict[str, Any], apply: bool) -> bool:
    pid = str(product.get("id"))
    title = (product.get("title") or "").strip()
    print(f"\n[{title[:60]}] ({pid})")

    images = product.get("images") or []
    back_image = _pick_back_image(images)
    if not back_image or not back_image.get("src"):
        print("  SKIP - no back mockup image found")
        return False

    scene_prompt = infer_theme_prompt(product)
    print(f"  Background scene: {scene_prompt[:80]}...")

    background_bytes = await generate_background_image(scene_prompt)
    if not background_bytes:
        print("  ERROR - Gemini returned no background image")
        return False

    try:
        mockup_bytes = await download_image(back_image["src"])
    except httpx.HTTPError as e:
        print(f"  ERROR downloading mockup: {e}")
        return False

    composite_bytes = composite_mockup_on_background(background_bytes, mockup_bytes)
    print(f"  Composite generated ({len(composite_bytes)} bytes)")

    if not apply:
        print("  DRY RUN - would upload composite and set as default thumbnail")
        return True

    b64 = base64.b64encode(composite_bytes).decode("utf-8")
    cname = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or pid
    try:
        upload = await upload_image_base64(f"{cname}_thumb.png", b64)
    except PrintifyError as e:
        print(f"  ERROR uploading composite: {e}")
        return False

    new_src = upload.get("preview_url") or upload.get("src")
    if not new_src:
        print(f"  ERROR - upload response missing preview_url: {upload}")
        return False

    updated_images = [{
        "src": new_src,
        "variant_ids": back_image.get("variant_ids", []),
        "position": back_image.get("position"),
        "is_default": True,
        "is_selected_for_publishing": True,
    }]
    for img in images:
        updated_images.append({
            "src": img.get("src"),
            "variant_ids": img.get("variant_ids", []),
            "position": img.get("position"),
            "is_default": False,
            "is_selected_for_publishing": bool(img.get("is_selected_for_publishing")),
        })

    try:
        await update_product(shop_id, pid, {"images": updated_images})
        print("  APPLIED - composite set as default thumbnail")
        return True
    except PrintifyError as e:
        print(f"  ERROR setting thumbnail: {e}")
        return False


async def fetch_target_products(shop_id: int, product_id: Optional[str]) -> List[Dict[str, Any]]:
    if product_id:
        try:
            return [await get_product(shop_id, product_id)]
        except PrintifyError as e:
            print(f"ERROR fetching product {product_id}: {e}")
            return []
    all_products = []
    page = 1
    while True:
        try:
            result = await list_products(shop_id, page=page, limit=50)
        except PrintifyError as e:
            print(f"ERROR fetching page {page}: {e}")
            break
        batch = result.get("data") or []
        if not batch:
            break
        all_products.extend(batch)
        if len(batch) < 50:
            break
        page += 1
    return all_products


async def main(apply: bool, product_id: Optional[str]):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    shop_id = settings.get("printify_shop_id")
    if not shop_id:
        print("No printify_shop_id configured in settings. Aborting.")
        client.close()
        return
    shop_id = int(shop_id)

    mode = "SINGLE PRODUCT" if product_id else "ALL PRODUCTS"
    print(f"Contextual Background Thumbnail Generator - {mode} - {'APPLY' if apply else 'DRY RUN'}\n")

    products = await fetch_target_products(shop_id, product_id)
    print(f"Found {len(products)} product(s) to process.")
    print("-" * 70)

    done, skipped = 0, 0
    for p in products:
        ok = await process_product(shop_id, p, apply)
        if ok:
            done += 1
        else:
            skipped += 1
        await asyncio.sleep(1.0)

    print("\n" + "=" * 70)
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {done} processed, {skipped} skipped")
    if not apply and done:
        print("Re-run with --apply to push these thumbnails to Printify.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Contextual Background Thumbnail Generator")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--product-id", default=None, help="Only process this Printify product ID")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, product_id=args.product_id))
