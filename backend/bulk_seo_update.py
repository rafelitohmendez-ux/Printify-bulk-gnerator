"""
Bulk SEO updater: pulls every product from Printify, sends its existing
title + back concept (extracted from description) to Gemini for a design-specific
keyword and design tags, builds the keyword-first title, 13 buyer-phrase tags and
the clean description with seo.py, then pushes the updates back via Printify's
update_product API.

Usage:
    python bulk_seo_update.py                    # dry run, shows what would change
    python bulk_seo_update.py --apply            # apply to all products
    python bulk_seo_update.py --product-id XYZ   # dry run, single product
    python bulk_seo_update.py --product-id XYZ --apply  # apply, single product
    python bulk_seo_update.py --apply --publish  # apply and re-sync title/description/tags to Etsy
                                                 # (photos on Etsy are left untouched)
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
from printify_client import (  # noqa: E402
    PrintifyError, get_product, list_products, prioritize_back_mockup, publish_product_text, update_product,
)
from title_words import RECENT_TITLES_WINDOW, find_overused_words, overused_words_instruction  # noqa: E402
from title_words import words as title_words  # noqa: E402
import seo  # noqa: E402
from seo import DESCRIPTION_TEMPLATE, build_description, extract_back_concept, extract_capsule_name  # noqa: E402,F401

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
genai_client = genai.Client(api_key=GEMINI_API_KEY)

# Words fixed in the title formulas repeat by design; never flag them as overused
FORMULA_WORDS = {
    w for f in seo.GOTHIC_TITLE_FORMULAS + seo.CYBER_TITLE_FORMULAS for w in title_words(re.sub(r"\{\w+\}", " ", f))
}

SEO_SYSTEM_PROMPT = """You are an expert Etsy SEO specialist for MidnightRotation, a gothic industrial dark alternative streetwear brand.

Given a product's capsule name and back graphic concept, return:

1. "secondary_keyword": ONE buyer search phrase (2-5 words, max 34 characters) naming the
   most specific visual subject of the back graphic, ending in Tee or Shirt.
   Examples: "Plague Doctor Back Print Tee", "Crow Skull Graphic Shirt", "Glitch Saint Tee".
   Do NOT include the capsule name. The shop builds the full title itself; the poetic
   design name goes AFTER the buyer keywords.

2. "tags": 5 design-specific Etsy tags that:
   - Name visual elements explicitly described in the back concept - do NOT invent details.
   - Are 2-4 word phrases a buyer would actually type, each ending in a product word
     (Shirt, Tee, Tshirt, Clothing): "Hooded Monk Shirt" not "Monks", "Steam Piston Tee" not "Steam Pistons".
   - Are max 20 characters each.
   - Are NEVER image-prompt fragments like "Ash Cloud Graphic", "Bone Soot Design", "Rust Texture Art".
   The shop adds its own broad tags (Oversized Goth Shirt, Back Print Shirt, Graphic Tee Men...) - don't repeat them.

Return ONLY raw JSON, no prose, no code fences:
{
  "secondary_keyword": "...",
  "tags": ["tag1", "tag2", "tag3", "tag4", "tag5"]
}"""


async def generate_seo(
    capsule_name: str,
    back_concept: str,
    overused_words: Optional[List[str]] = None,
    category: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Ask Gemini for a design-specific keyword + design tags, then build the
    keyword-first title and 13 buyer-phrase tags with seo.py.

    overused_words: words recent titles keep repeating; the keyword is asked to avoid
    them (except where they're part of the capsule name).
    category: 'cyber' or 'gothic'; inferred from the name/concept when omitted.
    Returns {"title", "tags", "secondary_keyword"} or None on failure.
    """
    category = category or seo.infer_category(f"{capsule_name} {back_concept}")
    prompt = (
        f"Capsule name: {capsule_name}\n"
        f"Back graphic concept: {back_concept}\n\n"
        "Generate the secondary keyword and 5 design tags for this listing."
    )
    name_words = set(title_words(capsule_name))
    avoid = [w for w in (overused_words or []) if w.lower() not in name_words]
    if avoid:
        prompt += "\n\n" + overused_words_instruction(avoid) + " This applies to the secondary keyword."
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
    except Exception as e:
        print(f"    Gemini error: {e}")
        return None
    secondary = seo.clean_secondary(data.get("secondary_keyword"), category)
    return {
        "title": seo.build_title(capsule_name, secondary, category),
        "tags": seo.build_tags(data.get("tags") or [], category),
        "secondary_keyword": secondary,
    }


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


async def main(apply: bool, product_id: Optional[str], publish: bool = False):
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
    new_titles: List[str] = []  # rolling window for the overused-word check

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

        overused = find_overused_words(new_titles[-RECENT_TITLES_WINDOW:], extra_exclude=FORMULA_WORDS)
        if overused:
            print(f"  Avoiding overused words: {', '.join(overused)}")
        seo_data = await generate_seo(
            capsule_name, back_concept, overused_words=overused,
            category=seo.infer_category(" ".join([current_title, " ".join(p.get("tags") or [])])),
        )
        if not seo_data:
            print(f"  ERROR — Gemini returned no usable data")
            errors += 1
            continue

        new_title = seo_data.get("title", "")[:140]
        new_titles.append(new_title)
        new_tags = seo_data.get("tags", [])
        new_desc = build_description(capsule_name, back_concept)

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
                if publish and (p.get("external") or {}).get("id"):
                    try:
                        await publish_product_text(shop_id, pid)
                        print(f"  Synced text to Etsy v")
                    except PrintifyError as e:
                        print(f"  WARNING: Etsy sync failed: {e}")
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
    parser.add_argument("--publish", action="store_true",
                        help="With --apply: re-sync title/description/tags of published products to Etsy")
    args = parser.parse_args()
    if args.publish and not args.apply:
        parser.error("--publish requires --apply")
    asyncio.run(main(apply=args.apply, product_id=args.product_id, publish=args.publish))
