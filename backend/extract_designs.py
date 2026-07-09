"""
Extract Designs: regenerates a clean, print-ready design file for every
product already pushed to Etsy (per etsy_upload_processed.json). Pulls each
product's back_concept straight from its live Printify description, then asks
Gemini for an isolated graphic on a plain black background (no shirt, no
scene) suitable for DTG printing. Saves locally only - no Printify or Etsy
writes.

Usage:
    python extract_designs.py            # dry run, lists what would be generated
    python extract_designs.py --apply    # generate and save PNGs
"""
import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient
from google import genai
from google.genai import types

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import get_product, PrintifyError  # noqa: E402
from bulk_seo_update import extract_capsule_name, extract_back_concept  # noqa: E402

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
genai_client = genai.Client(api_key=GEMINI_API_KEY)

PROCESSED_FILE = ROOT_DIR / "etsy_upload_processed.json"
OUTPUT_DIR = ROOT_DIR / "new_designs"
MANIFEST_FILE = OUTPUT_DIR / "manifest.json"


async def generate_design_image(back_concept: str) -> Optional[bytes]:
    """Ask Gemini for an isolated print-ready graphic - no shirt, no scene."""
    prompt = (
        "Stark white ink illustration on pure black background. No shirt, "
        "no environment, no background scene. Just the graphic design: "
        f"{back_concept}. Gothic industrial aesthetic, high contrast, "
        "suitable for DTG t-shirt printing. Square format 1024x1024."
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


def load_processed_ids() -> list:
    if not PROCESSED_FILE.exists():
        return []
    return json.loads(PROCESSED_FILE.read_text())


async def main(apply: bool):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    shop_id = settings.get("printify_shop_id")
    if not shop_id:
        print("No printify_shop_id configured in settings. Aborting.")
        client.close()
        return
    shop_id = int(shop_id)

    product_ids = load_processed_ids()
    if not product_ids:
        print(f"No product IDs found in {PROCESSED_FILE.name} - nothing to process.")
        client.close()
        return

    print(f"Extract Designs - {'APPLY' if apply else 'DRY RUN'}\n")
    print(f"Found {len(product_ids)} product(s) in {PROCESSED_FILE.name}.\n")
    print("-" * 70)

    if apply:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    done, skipped, errors = 0, 0, 0
    seen_filenames: dict = {}
    manifest: list = []

    for pid in product_ids:
        try:
            product = await get_product(shop_id, pid)
        except PrintifyError as e:
            print(f"\n({pid}) ERROR fetching product: {e}")
            errors += 1
            continue

        title = (product.get("title") or "").strip()
        capsule_name = extract_capsule_name(title)
        print(f"\n[{capsule_name}] ({pid})")

        back_concept = extract_back_concept(product.get("description") or "")
        if not back_concept:
            print("  SKIP - no back concept extractable from description")
            skipped += 1
            continue
        print(f"  Back concept: {back_concept[:80]}")

        fname = re.sub(r"[^a-z0-9]+", "_", capsule_name.lower()).strip("_") or pid
        if fname in seen_filenames:
            fname = f"{fname}_{pid[-6:]}"
        seen_filenames[fname] = pid
        out_path = OUTPUT_DIR / f"{fname}.png"

        if not apply:
            print(f"  DRY RUN - would generate -> {out_path}")
            done += 1
            continue

        try:
            image_bytes = await generate_design_image(back_concept)
            if not image_bytes:
                print("  ERROR - Gemini returned no image")
                errors += 1
                continue
            out_path.write_bytes(image_bytes)
            print(f"  APPLIED - saved to {out_path}")
            manifest.append({
                "product_id": pid,
                "capsule_name": capsule_name,
                "back_concept": back_concept,
                "filename": out_path.name,
            })
            done += 1
        except Exception as e:
            print(f"  ERROR - unexpected exception: {e}")
            errors += 1
            continue
        await asyncio.sleep(1.0)

    if apply and manifest:
        MANIFEST_FILE.write_text(json.dumps(manifest, indent=2))
        print(f"\nWrote manifest for {len(manifest)} design(s) -> {MANIFEST_FILE}")

    print("\n" + "=" * 70)
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {done} processed, {skipped} skipped, {errors} errors")
    if not apply and done:
        print("Re-run with --apply to actually generate and save these design files.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Extract clean print-ready designs from product back concepts")
    parser.add_argument("--apply", action="store_true", help="Generate and save design files (default is dry-run)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
