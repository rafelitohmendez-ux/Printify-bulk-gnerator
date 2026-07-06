"""
Contextual Background Thumbnail Generator
==========================================
For each Printify product, infers its gothic sub-theme from the title/tags
and extracts its back-print concept from the description, then generates a
single complete product photo via Gemini image generation showing the shirt
naturally in its themed environment with the back print already on it.
Uploads the generated photo to Printify and sets it as the default product
thumbnail.

Usage:
    python generate_mockups.py                        # dry run, all products
    python generate_mockups.py --apply                 # apply to all products
    python generate_mockups.py --product-id XYZ        # dry run, single product
    python generate_mockups.py --product-id XYZ --apply
"""
import argparse
import asyncio
import base64
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import (  # noqa: E402
    get_product,
    list_products,
    update_product,
    upload_image_base64,
    PrintifyError,
)
from bulk_seo_update import extract_back_concept  # noqa: E402

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


async def generate_background_image(scene_prompt: str, back_concept: str) -> Optional[bytes]:
    """Generate a complete product photo via Gemini: the shirt shown naturally
    in its themed environment, back print already rendered on it. Returns raw
    image bytes, or None."""
    prompt = (
        f"A black heavy cotton t-shirt displayed naturally in {scene_prompt}, "
        f"the back of the shirt facing camera, featuring {back_concept} printed in "
        f"stark white ink. Cinematic lighting, gothic industrial aesthetic, "
        f"photorealistic product photography. "
        f"The shirt is displayed upright and hanging naturally, back facing the camera "
        f"directly. The shirt is NOT laying flat, NOT on a surface, NOT folded. "
        f"It is suspended or hanging in the environment."
    )
    try:
        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-2.5-flash-image",
            contents=prompt,
            config=types.GenerateContentConfig(response_modalities=["IMAGE"]),
        )
    except Exception as exc:
        print(f"    Gemini image generation error: {exc}")
        return None
    if not response.candidates:
        return None
    content = response.candidates[0].content
    if not content or not content.parts:
        return None
    for part in content.parts:
        if getattr(part, "inline_data", None) and part.inline_data.data:
            return part.inline_data.data
    return None


async def process_product(shop_id: int, product: Dict[str, Any], apply: bool, output_file: str = "test_composite.png") -> bool:
    pid = str(product.get("id"))
    title = (product.get("title") or "").strip()
    print(f"\n[{title[:60]}] ({pid})")

    back_concept = extract_back_concept(product.get("description") or "")
    if not back_concept:
        print("  SKIP - no back concept extractable from description")
        return False

    scene_prompt = infer_theme_prompt(product)
    print(f"  Scene: {scene_prompt[:80]}...")
    print(f"  Back concept: {back_concept[:80]}")

    image_bytes = await generate_background_image(scene_prompt, back_concept)
    if not image_bytes:
        print("  ERROR - Gemini returned no image")
        return False
    print(f"  Product photo generated ({len(image_bytes)} bytes)")

    if not apply:
        preview_path = ROOT_DIR / output_file
        preview_path.write_bytes(image_bytes)
        print(f"  DRY RUN - image saved to {preview_path}")
        print("  Re-run with --apply to upload and set as default thumbnail")
        return True

    b64 = base64.b64encode(image_bytes).decode("utf-8")
    cname = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or pid
    try:
        upload = await upload_image_base64(f"{cname}_thumb.png", b64)
    except PrintifyError as e:
        print(f"  ERROR uploading image: {e}")
        return False

    new_image_id = upload.get("id")
    if not new_image_id:
        print(f"  ERROR - upload response missing id: {upload}")
        return False

    variant_ids = [v.get("id") for v in (product.get("variants") or []) if v.get("id") is not None]
    updated_images = [{
        "id": new_image_id,
        "variant_ids": variant_ids,
        "position": "back",
        "is_default": True,
        "is_selected_for_publishing": True,
    }]
    for img in (product.get("images") or []):
        updated_images.append({
            "src": img.get("src"),
            "variant_ids": img.get("variant_ids", []),
            "position": img.get("position"),
            "is_default": False,
            "is_selected_for_publishing": bool(img.get("is_selected_for_publishing")),
        })

    try:
        await update_product(shop_id, pid, {"images": updated_images})
        print("  APPLIED - product photo set as default thumbnail")
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


async def main(apply: bool, product_id: Optional[str], output_file: str = "test_composite.png"):
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
        ok = await process_product(shop_id, p, apply, output_file)
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
    parser.add_argument("--output-file", default="test_composite.png", help="Filename for the dry-run preview image (default: test_composite.png)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, product_id=args.product_id, output_file=args.output_file))
