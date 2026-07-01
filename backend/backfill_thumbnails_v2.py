"""
Backfill script v2: re-applies prioritize_back_mockup() to Printify product(s),
found by querying Printify directly rather than Mongo (local DB history is
incomplete after the migration off the old Emergent-hosted backend).

Usage:
    python backfill_thumbnails_v2.py                          # dry run, all products
    python backfill_thumbnails_v2.py --apply                  # apply fix, all products
    python backfill_thumbnails_v2.py --product-id 12345        # dry run, single product
    python backfill_thumbnails_v2.py --product-id 12345 --apply  # apply, single product
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import get_product, list_products, prioritize_back_mockup, PrintifyError  # noqa: E402

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")


async def fetch_target_products(shop_id: int, product_id: Optional[str]) -> List[Dict[str, Any]]:
    """Return the list of products to process: either a single product
    (single-product mode) or every page of the shop's catalog."""
    if product_id:
        try:
            product = await get_product(shop_id, product_id)
        except PrintifyError as e:
            print(f"ERROR fetching product {product_id}: {e}")
            return []
        return [product]

    all_products: List[Dict[str, Any]] = []
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

    if product_id:
        print(f"Single-product mode: shop {shop_id}, product {product_id}\n")
    else:
        print(f"Querying Printify directly for shop {shop_id} (bypassing Mongo history)...\n")

    products = await fetch_target_products(shop_id, product_id)

    fixed, already_ok, errors, no_back_image = 0, 0, 0, 0

    for p in products:
        pid = str(p.get("id"))
        title = (p.get("title") or "?")[:60]
        images = p.get("images") or []

        has_back = any((img.get("position") or "").lower() == "back" for img in images)
        if not has_back:
            print(f"  SKIP   {title} ({pid}) — no back-position image found at all")
            no_back_image += 1
            continue

        default_img = next((img for img in images if img.get("is_default")), None)
        current_pos = (default_img or {}).get("position", "none")

        if current_pos == "back":
            print(f"  OK     {title} ({pid}) — already back-default")
            already_ok += 1
            continue

        print(f"  FIX    {title} ({pid}) — default was '{current_pos}'")
        if apply:
            try:
                new_src = await prioritize_back_mockup(shop_id, pid)
                print(f"         -> set default to {new_src}")
            except PrintifyError as e:
                print(f"         -> ERROR: {e}")
                errors += 1
                continue
        fixed += 1
        await asyncio.sleep(0.3)  # be gentle with Printify rate limits

    print(f"\nTotal products checked: {len(products)}")
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {fixed} fixed, {already_ok} already correct, "
          f"{no_back_image} skipped (no back image), {errors} errors")
    if not apply and fixed:
        print("Re-run with --apply to actually push these changes to Printify.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually push the fix (default is dry-run)")
    parser.add_argument("--product-id", default=None, help="Only process this single Printify product ID")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, product_id=args.product_id))
