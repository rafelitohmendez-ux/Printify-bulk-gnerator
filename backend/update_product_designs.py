"""
Update Product Designs: pushes freshly-regenerated design files (from
new_designs/, keyed by ORIGINAL product_id in manifest.json) into the
already-live "new" back-print-only Printify products (mapped via
_original_to_new_pid_map.json), updating each product's back print image
IN PLACE rather than creating a new product.

Requires:
    backend/_original_to_new_pid_map.json  - [{original_pid, new_pid, capsule_name}, ...]
    backend/new_designs/manifest.json      - keyed by product_id (the ORIGINAL pid)

Usage:
    python update_product_designs.py                    # dry run, all pairs
    python update_product_designs.py --apply             # apply, all pairs
    python update_product_designs.py --product-id XYZ    # dry run, single pair (original_pid)
    python update_product_designs.py --product-id XYZ --apply
"""
import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import upload_image_base64, update_back_print_image, PrintifyError  # noqa: E402

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

MAPPING_FILE = ROOT_DIR / "_original_to_new_pid_map.json"
MANIFEST_FILE = ROOT_DIR / "new_designs" / "manifest.json"
DESIGNS_DIR = ROOT_DIR / "new_designs"


def load_mapping() -> list:
    if not MAPPING_FILE.exists():
        return []
    return json.loads(MAPPING_FILE.read_text())


def load_manifest_by_product_id() -> dict:
    if not MANIFEST_FILE.exists():
        return {}
    entries = json.loads(MANIFEST_FILE.read_text())
    return {e["product_id"]: e for e in entries}


async def main(apply: bool, product_id: Optional[str] = None):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    shop_id = settings.get("printify_shop_id")
    if not shop_id:
        print("No printify_shop_id configured in settings. Aborting.")
        client.close()
        return
    shop_id = int(shop_id)

    mapping = load_mapping()
    if product_id:
        mapping = [m for m in mapping if m["original_pid"] == product_id]
    if not mapping:
        print(f"No matching pair(s) found in {MAPPING_FILE.name}.")
        client.close()
        return

    manifest = load_manifest_by_product_id()

    mode = "SINGLE PRODUCT" if product_id else "ALL PRODUCTS"
    print(f"Update Product Designs - {mode} - {'APPLY' if apply else 'DRY RUN'}\n")
    print(f"Found {len(mapping)} pair(s) to process.\n")
    print("-" * 70)

    updated, skipped, errors = 0, 0, 0

    for m in mapping:
        original_pid = m["original_pid"]
        new_pid = m["new_pid"]
        capsule_name = m["capsule_name"]
        print(f"\n[{capsule_name}] (original {original_pid} -> new {new_pid})")

        entry = manifest.get(original_pid)
        if not entry:
            print(f"  SKIP - no manifest entry for {original_pid}")
            skipped += 1
            continue

        design_path = DESIGNS_DIR / entry["filename"]
        if not design_path.exists():
            print(f"  SKIP - design file missing: {design_path}")
            skipped += 1
            continue

        if not apply:
            print(f"  DRY RUN - would upload {design_path.name} and update back print on {new_pid}")
            updated += 1
            continue

        try:
            design_bytes = design_path.read_bytes()
            b64 = base64.b64encode(design_bytes).decode("utf-8")
            upload = await upload_image_base64(design_path.name, b64)
            new_image_id = upload.get("id")
            if not new_image_id:
                print(f"  ERROR - image upload missing id: {upload}")
                errors += 1
                continue

            await update_back_print_image(shop_id, new_pid, new_image_id)
            print(f"  APPLIED - updated back print image on {new_pid}")
            updated += 1
        except PrintifyError as e:
            print(f"  ERROR: {e}")
            errors += 1
            continue
        except Exception as e:
            print(f"  ERROR - unexpected exception: {e}")
            errors += 1
            continue
        await asyncio.sleep(1.0)

    print("\n" + "=" * 70)
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {updated} updated, {skipped} skipped, {errors} errors")
    if not apply and updated:
        print("Re-run with --apply to actually push these updates to Printify.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Update the back print image on existing new_pid products in place")
    parser.add_argument("--apply", action="store_true", help="Apply updates to Printify (default is dry-run)")
    parser.add_argument("--product-id", default=None, help="Only process this single ORIGINAL Printify product ID")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, product_id=args.product_id))
