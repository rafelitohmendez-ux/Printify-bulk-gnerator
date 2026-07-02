"""
Bulk SEO updater: pulls every product from Printify, sends its existing
title + back concept (extracted from description) to Gemini to generate
an improved SEO title, 13 tags, and updated description, then pushes
the updates back via Printify's update_product API.

Usage:
    python bulk_seo_update.py                    # dry run, shows what would change
    python bulk_seo_update.py --apply            # apply to all products
    python bulk_seo_update.py --product-id XYZ   # dry run, single product
    python bulk_seo_update.py --product-id XYZ --apply  # apply, single product
"""
import argparse
import asyncio
import json
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
from printify_client import get_product, list_products, update_product, prioritize_back_mockup, PrintifyError  # noqa: E402

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
genai_client = genai.Client(api_key=GEMINI_API_KEY)

DESCRIPTION_TEMPLATE = """THE GRIND // {capsule_name}

A premium, heavy-hitting alternative staple designed for the late-night rotation. Featuring a clean minimalist left-chest graphic on the front and an aggressive, oversized {back_graphic} filling the back.

* Built on the classic Gildan 5000 heavy cotton tee for a structured, boxy streetwear fit.
* Premium DTG printing with stark white ink for maximum contrast.
* 100% Cotton (Fiber content may vary for different colors).
* True to size (Size up for an oversized look).

Care Instructions: Machine wash cold, inside out, with like colors. Tumble dry low or hang dry to preserve print longevity."""

SEO_SYSTEM_PROMPT = """You are an expert Etsy SEO specialist for MidnightRotation, a gothic industrial dark alternative streetwear brand.

Given a product's capsule name and back graphic concept, generate:
1. A unique, highly specific SEO title (max 140 chars) that:
   - Starts with the capsule name
   - Includes the most specific visual element from the back concept
   - Uses high-search-volume Etsy keywords for this niche
   - Avoids generic phrases used across all listings
   - Format: "{capsule_name} - {specific visual descriptor} | {2-3 search terms}"

2. Exactly 13 SEO tags that:
   - MUST include "Gothic Streetwear" and "Back Print Shirt"
   - Include 2 gift/occasion tags (e.g. "Goth Gift For Him", "Alt Fashion Gift")
   - Include 2 broad aesthetic umbrella tags (e.g. "Dark Academia Tshirt", "Grunge Clothing")
   - Include 9 tags drawn ONLY from visual elements explicitly described in the
     back concept — do NOT invent details not mentioned. If the concept mentions
     monks, chains, and steam pistons, tag those. Do not add visual elements
     (textures, objects, materials) that aren't in the description.
   - Each tag max 20 characters
   - Natural Etsy search phrases, never generic
   - Every tag must be a 2-4 word search phrase a buyer would actually type
     on Etsy — never a bare noun or single concept word. "Steam Piston Tee"
     not "Steam Pistons". "Hooded Monk Shirt" not "Monks".

Return ONLY raw JSON, no prose, no code fences:
{
  "title": "...",
  "tags": ["tag1", "tag2", ..., "tag13"]
}"""


def extract_back_concept(description: str) -> str:
    """Extract the back graphic concept from a product description."""
    match = re.search(
        r"aggressive, oversized (.+?) filling the back",
        description,
        re.DOTALL,
    )
    if match:
        return match.group(1).strip()
    return ""


def extract_capsule_name(title: str) -> str:
    """Extract capsule name (everything before first - or |)."""
    name = re.split(r"[-|]", title)[0].strip()
    name = re.sub(r"\s+[Tt]ee$", "", name).strip()
    return name


async def generate_seo(capsule_name: str, back_concept: str) -> Optional[Dict[str, Any]]:
    """Call Gemini to generate improved title and tags."""
    prompt = (
        f"Capsule name: {capsule_name}\n"
        f"Back graphic concept: {back_concept}\n\n"
        "Generate improved SEO title and 13 tags for this listing."
    )
    try:
        response = await asyncio.to_thread(
            genai_client.models.generate_content,
            model="gemini-2.5-flash",
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=SEO_SYSTEM_PROMPT),
        )
        text = response.text or ""
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip(), flags=re.MULTILINE).strip()
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not match:
            return None
        data = json.loads(match.group(0))
        tags = data.get("tags") or []
        required = ["Gothic Streetwear", "Back Print Shirt"]
        for r in required:
            if not any(str(t).lower() == r.lower() for t in tags):
                tags.insert(0, r)
        data["tags"] = [t[:20] for t in tags[:13]]
        return data
    except Exception as e:
        print(f"    Gemini error: {e}")
        return None


async def fetch_target_products(shop_id: int, product_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return products to process — single or all."""
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
    print(f"Bulk SEO updater — {mode} — {'APPLY' if apply else 'DRY RUN'}\n")

    products = await fetch_target_products(shop_id, product_id)
    print(f"Found {len(products)} product(s) to process.\n")
    print("-" * 70)

    updated, skipped, errors = 0, 0, 0

    for p in products:
        pid = str(p.get("id"))
        current_title = (p.get("title") or "").strip()
        current_desc = (p.get("description") or "").strip()
        capsule_name = extract_capsule_name(current_title)
        back_concept = extract_back_concept(current_desc)

        print(f"\n[{capsule_name}] ({pid})")
        print(f"  Current title: {current_title[:80]}...")

        if not back_concept:
            print(f"  SKIP — could not extract back concept from description")
            skipped += 1
            continue

        print(f"  Back concept:  {back_concept[:80]}")

        seo = await generate_seo(capsule_name, back_concept)
        if not seo:
            print(f"  ERROR — Gemini returned no usable data")
            errors += 1
            continue

        new_title = seo.get("title", "")[:140]
        new_tags = seo.get("tags", [])
        new_desc = DESCRIPTION_TEMPLATE.format(
            capsule_name=capsule_name.upper(),
            back_graphic=back_concept.rstrip(".").lower(),
        )

        print(f"  New title:     {new_title}")
        print(f"  New tags:      {', '.join(new_tags)}")

        if apply:
            try:
                await update_product(shop_id, pid, {
                    "title": new_title,
                    "tags": new_tags,
                    "description": new_desc,
                })
                print(f"  APPLIED v")
                updated += 1
                try:
                    await prioritize_back_mockup(shop_id, pid)
                    print(f"  Thumbnail re-set to back-default v")
                except PrintifyError as e:
                    print(f"  WARNING: thumbnail re-priority failed: {e}")
            except PrintifyError as e:
                print(f"  ERROR pushing update: {e}")
                errors += 1
        else:
            updated += 1

        await asyncio.sleep(1.0)  # generous rate limit buffer

    print("\n" + "=" * 70)
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {updated} updated, {skipped} skipped, {errors} errors")
    if not apply and updated:
        print("Re-run with --apply to push these changes to Printify.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Push updates to Printify (default is dry-run)")
    parser.add_argument("--product-id", default=None, help="Only process this Printify product ID")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, product_id=args.product_id))
