"""
Create Draft Products From Extracted Designs
==============================================
For every design in new_designs/manifest.json (produced by extract_designs.py),
creates a brand-new Printify draft product whose back graphic is the clean
extracted design (instead of the original hallucinated/regenerated mockup).
Back-print-only - no front/left-chest placement. Generates a fresh SEO
title/tags via generate_seo() and builds the description from the shared
template.

Products are created as DRAFTS ONLY - nothing is published to Etsy. Review
and publish manually from the Printify dashboard.

Requires backend/new_designs/manifest.json to exist (run
extract_designs.py --apply first).

Usage:
    python create_products_from_designs.py            # dry run
    python create_products_from_designs.py --apply     # create drafts on Printify
"""
import argparse
import asyncio
import base64
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import push_capsule_as_draft, PrintifyError  # noqa: E402
from bulk_seo_update import generate_seo, DESCRIPTION_TEMPLATE  # noqa: E402

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

DESIGNS_DIR = ROOT_DIR / "new_designs"
MANIFEST_FILE = DESIGNS_DIR / "manifest.json"


def load_manifest() -> list:
    if not MANIFEST_FILE.exists():
        return []
    return json.loads(MANIFEST_FILE.read_text())


async def main(apply: bool, product_id: Optional[str] = None):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    shop_id = settings.get("printify_shop_id")
    print_provider_id = settings.get("printify_print_provider_id")
    if not shop_id or not print_provider_id:
        print("printify_shop_id and printify_print_provider_id must both be configured in settings. Aborting.")
        client.close()
        return
    shop_id = int(shop_id)
    print_provider_id = int(print_provider_id)

    entries = load_manifest()
    if not entries:
        print(f"No entries found in {MANIFEST_FILE.name} - run extract_designs.py --apply first.")
        client.close()
        return

    if product_id:
        entries = [e for e in entries if e["product_id"] == product_id]
        if not entries:
            print(f"No entry for product_id {product_id} found in {MANIFEST_FILE.name}.")
            client.close()
            return

    mode = "SINGLE PRODUCT" if product_id else "ALL PRODUCTS"
    print(f"Create Draft Products From Extracted Designs - {mode} - {'APPLY' if apply else 'DRY RUN'}\n")
    print(f"Found {len(entries)} design(s) to process.\n")
    print("-" * 70)

    created, skipped, errors = 0, 0, 0

    for entry in entries:
        pid = entry["product_id"]
        capsule_name = entry["capsule_name"]
        back_concept = entry["back_concept"]
        design_path = DESIGNS_DIR / entry["filename"]

        print(f"\n[{capsule_name}] (source product {pid})")

        if not design_path.exists():
            print(f"  SKIP - design file missing: {design_path}")
            skipped += 1
            continue

        if not apply:
            print(f"  DRY RUN - would create back-print-only draft product using back={design_path.name}")
            created += 1
            continue

        try:
            back_bytes = design_path.read_bytes()

            seo = await generate_seo(capsule_name, back_concept)
            if not seo:
                print("  ERROR - Gemini returned no usable SEO data")
                errors += 1
                continue
            title = seo.get("title", "")[:140]
            tags = seo.get("tags", [])
            description = DESCRIPTION_TEMPLATE.format(
                capsule_name=capsule_name.upper(),
                back_graphic=back_concept.rstrip(".").lower(),
            )

            capsule: Dict[str, Any] = {
                "capsule_name": capsule_name,
                "title": title,
                "description": description,
                "tags": tags,
                "back_image_b64": base64.b64encode(back_bytes).decode("utf-8"),
            }

            product = await push_capsule_as_draft(shop_id, print_provider_id, capsule)
            new_pid = product.get("id")
            print(f"  APPLIED - created draft Printify product {new_pid} ('{title}')")
            created += 1
        except PrintifyError as e:
            print(f"  ERROR creating draft: {e}")
            errors += 1
            continue
        except Exception as e:
            print(f"  ERROR - unexpected exception: {e}")
            errors += 1
            continue
        await asyncio.sleep(1.0)

    print("\n" + "=" * 70)
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {created} created, {skipped} skipped, {errors} errors")
    if not apply and created:
        print("Re-run with --apply to actually create these as draft Printify products.")
        print("NOTE: drafts are NOT published to Etsy automatically - review and publish manually.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Create draft Printify products from extracted designs")
    parser.add_argument("--apply", action="store_true", help="Create draft products on Printify (default is dry-run)")
    parser.add_argument("--product-id", default=None, help="Only process this single source Printify product ID")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, product_id=args.product_id))
