"""
Cleanup Etsy Images: for every product in etsy_upload_processed.json, removes
stale duplicate atmospheric photos from its matched Etsy listing (leftover
from repeated upload_etsy_images.py runs), then regenerates one fresh
atmospheric photo using the exact-design multimodal approach.

Image classification (via Etsy's per-image hex_code / dominant color):
    hex_code is None        -> "pending", very recently uploaded, Etsy
                                hasn't finished color-indexing it yet
    hex_code parses "light" -> original Printify mockup (near-white
                                studio background) - always kept
    hex_code parses "dark"  -> atmospheric photo candidate for deletion

"Keeper" (surviving atmospheric image before regeneration) = the pending
image if exactly one exists (Etsy assigns rank 1 to the most recent
upload), else the dark-hex image with the highest listing_image_id.
Everything else atmospheric is deleted immediately.

The keeper itself is only deleted AFTER a fresh photo is confirmed
uploaded - if regeneration fails or is skipped for a listing, the keeper
is left in place so the listing is never left without an atmospheric photo.

Usage:
    python cleanup_etsy_images.py                    # dry run, all listings
    python cleanup_etsy_images.py --apply             # execute, all listings
    python cleanup_etsy_images.py --product-id XYZ    # dry run, single listing
    python cleanup_etsy_images.py --product-id XYZ --apply
"""
import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import get_product, PrintifyError  # noqa: E402
from upload_etsy_images import (  # noqa: E402
    etsy_request,
    ETSY_API_BASE,
    _tokens,
    fetch_etsy_listings,
    match_etsy_listing,
    process_product,
)

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

PROCESSED_FILE = ROOT_DIR / "etsy_upload_processed.json"

LIGHT_BRIGHTNESS_THRESHOLD = 235  # 0-255 avg channel; >= this counts as white/light


def load_processed_ids() -> list:
    if not PROCESSED_FILE.exists():
        return []
    return json.loads(PROCESSED_FILE.read_text())


def _is_light(hex_code: Optional[str]) -> bool:
    if not hex_code or len(hex_code) != 6:
        return False
    try:
        r, g, b = int(hex_code[0:2], 16), int(hex_code[2:4], 16), int(hex_code[4:6], 16)
    except ValueError:
        return False
    return (r + g + b) / 3 >= LIGHT_BRIGHTNESS_THRESHOLD


async def fetch_listing_images(listing_id: int) -> List[Dict[str, Any]]:
    resp = await etsy_request("GET", f"{ETSY_API_BASE}/listings/{listing_id}/images")
    if resp.status_code >= 400:
        print(f"  ERROR fetching images: {resp.status_code} {resp.text}")
        return []
    return resp.json().get("results", [])


async def delete_listing_image(shop_id: str, listing_id: int, image_id: int) -> bool:
    resp = await etsy_request(
        "DELETE", f"{ETSY_API_BASE}/shops/{shop_id}/listings/{listing_id}/images/{image_id}"
    )
    return resp.status_code < 400


def classify(images: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Split a listing's images into light (always kept), keeper (surviving
    atmospheric image), and stale (atmospheric images to delete now)."""
    pending = [img for img in images if img.get("hex_code") is None]
    dark = [img for img in images if img.get("hex_code") and not _is_light(img["hex_code"])]
    atmospheric = pending + dark

    if not atmospheric:
        return {"keeper": None, "stale": []}

    if len(pending) == 1:
        keeper = pending[0]
    elif pending:
        keeper = min(pending, key=lambda i: i.get("rank", 999))
    else:
        keeper = max(dark, key=lambda i: i.get("listing_image_id", 0))

    stale = [img for img in atmospheric if img["listing_image_id"] != keeper["listing_image_id"]]
    return {"keeper": keeper, "stale": stale}


async def main(apply: bool, product_id: Optional[str] = None):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    printify_shop_id = settings.get("printify_shop_id")
    if not printify_shop_id:
        print("No printify_shop_id configured in settings. Aborting.")
        client.close()
        return
    printify_shop_id = int(printify_shop_id)
    etsy_shop_id = _tokens["shop_id"]

    product_ids = load_processed_ids()
    if product_id:
        product_ids = [p for p in product_ids if p == product_id]
    if not product_ids:
        print(f"No matching product ID(s) found in {PROCESSED_FILE.name}.")
        client.close()
        return

    mode = "SINGLE LISTING" if product_id else "ALL LISTINGS"
    print(f"Cleanup Etsy Images - {mode} - {'APPLY' if apply else 'DRY RUN'}\n")
    print("Fetching Etsy listings...")
    etsy_listings = await fetch_etsy_listings()
    print(f"Found {len(etsy_listings)} active Etsy listing(s), {len(product_ids)} product(s) to process.\n")
    print("-" * 70)

    stale_deleted, keepers_superseded, regenerated, skipped, errors = 0, 0, 0, 0, 0

    for pid in product_ids:
        try:
            product = await get_product(printify_shop_id, pid)
        except PrintifyError as e:
            print(f"\n({pid}) ERROR fetching product: {e}")
            errors += 1
            continue

        title = (product.get("title") or "").strip()
        print(f"\n[{title[:60]}] ({pid})")

        listing = match_etsy_listing(product, etsy_listings)
        if not listing:
            print("  SKIP - no matching Etsy listing found by title")
            skipped += 1
            continue
        listing_id = listing.get("listing_id")

        images = await fetch_listing_images(listing_id)
        plan = classify(images)
        keeper = plan["keeper"]
        stale = plan["stale"]

        if not stale:
            print("  No stale atmospheric duplicates")
        for img in stale:
            img_id = img["listing_image_id"]
            print(f"  {'DELETE' if apply else 'WOULD DELETE'} stale listing_image_id={img_id} "
                  f"(hex={img.get('hex_code')}, rank={img.get('rank')})")
            if apply:
                ok = await delete_listing_image(etsy_shop_id, listing_id, img_id)
                if ok:
                    stale_deleted += 1
                else:
                    print(f"    ERROR deleting {img_id}")
                    errors += 1
                await asyncio.sleep(0.5)
            else:
                stale_deleted += 1

        if not apply:
            if keeper:
                print(f"  Regeneration would supersede + delete keeper listing_image_id="
                      f"{keeper['listing_image_id']} (hex={keeper.get('hex_code')})")
            continue

        try:
            ok = await process_product(product, etsy_listings, apply=True)
        except Exception as e:
            print(f"  ERROR regenerating: {e}")
            errors += 1
            continue

        if ok:
            regenerated += 1
            if keeper:
                deleted_ok = await delete_listing_image(etsy_shop_id, listing_id, keeper["listing_image_id"])
                if deleted_ok:
                    print(f"  Superseded - deleted previous keeper listing_image_id={keeper['listing_image_id']}")
                    keepers_superseded += 1
                else:
                    print(f"  WARNING - regenerated OK but failed to delete old keeper {keeper['listing_image_id']}")
                    errors += 1
        else:
            skipped += 1
            if keeper:
                print(f"  Regeneration skipped/failed - keeping existing keeper listing_image_id="
                      f"{keeper['listing_image_id']} in place")
        await asyncio.sleep(1.0)

    print("\n" + "=" * 70)
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {stale_deleted} stale image(s) "
          f"{'deleted' if apply else 'would be deleted'}, {keepers_superseded} keeper(s) superseded, "
          f"{regenerated} listing(s) regenerated, {skipped} skipped, {errors} errors")
    if not apply:
        print("Re-run with --apply to delete stale images and regenerate fresh atmospheric photos.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Delete stale atmospheric Etsy images and regenerate fresh ones")
    parser.add_argument("--apply", action="store_true", help="Delete stale images and regenerate (default is dry-run)")
    parser.add_argument("--product-id", default=None, help="Only process this single source Printify product ID")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, product_id=args.product_id))
