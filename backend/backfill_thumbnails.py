"""
Backfill script: re-applies prioritize_back_mockup() to every Printify
product that was already pushed BEFORE that logic existed in the codebase.

This fixes exactly the bug seen on "Vicar Assembly Tee" — a listing whose
default storefront thumbnail may still be a front-mockup (or unset),
instead of the oversized back-print graphic that's the actual visual hook.

Usage:
    pip install motor python-dotenv httpx --break-system-packages
    python backfill_thumbnails.py            # dry run, lists what would change
    python backfill_thumbnails.py --apply    # actually applies the fix
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import get_product, prioritize_back_mockup, PrintifyError  # noqa: E402

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

MONGO_URL = os.environ["MONGO_URL"]
DB_NAME = os.environ["DB_NAME"]


async def main(apply: bool):
    client = AsyncIOMotorClient(MONGO_URL)
    db = client[DB_NAME]
    capsules_coll = db.capsules

    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    shop_id = settings.get("printify_shop_id")
    if not shop_id:
        print("No printify_shop_id configured in settings. Aborting.")
        return

    docs = await capsules_coll.find(
        {"status": "approved", "printify_product_id": {"$exists": True, "$ne": None}},
        {"_id": 0, "id": 1, "capsule_name": 1, "printify_product_id": 1},
    ).to_list(1000)

    print(f"Found {len(docs)} approved capsules with a Printify product ID.\n")

    fixed, already_ok, errors = 0, 0, 0
    for d in docs:
        pid = d["printify_product_id"]
        name = d.get("capsule_name", "?")
        try:
            product = await get_product(int(shop_id), pid)
            images = product.get("images") or []
            default_img = next((img for img in images if img.get("is_default")), None)
            current_pos = (default_img or {}).get("position", "none")

            if current_pos == "back":
                print(f"  OK     {name} ({pid}) — already back-default")
                already_ok += 1
                continue

            print(f"  FIX    {name} ({pid}) — default was '{current_pos}'")
            if apply:
                new_src = await prioritize_back_mockup(int(shop_id), pid)
                print(f"         -> set default to {new_src}")
            fixed += 1
        except PrintifyError as e:
            print(f"  ERROR  {name} ({pid}) — {e}")
            errors += 1
        await asyncio.sleep(0.3)  # be gentle with Printify rate limits

    print(f"\n{'APPLIED' if apply else 'DRY RUN'}: {fixed} fixed, {already_ok} already correct, {errors} errors")
    if not apply and fixed:
        print("Re-run with --apply to actually push these changes to Printify.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Actually push the fix (default is dry-run)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply))
