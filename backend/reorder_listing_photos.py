"""
Re-order photos on existing Etsy listings to the 4-photo checklist
=================================================================
Older listings got a dark atmospheric photo uploaded as the PRIMARY image (rank 1),
with the back-print mockup at rank 2. The checklist is now:
  1. back-print mockup (primary thumbnail)
  2. front left-chest mockup
  3. size chart (added by hand on Etsy)
  4. lifestyle / atmospheric photo

For each active listing linked to a Printify product this script:
  - checks whether rank 1 is a Printify mockup or a generated photo (perceptual hash)
  - if it's a generated photo, moves it to rank 4
  - moves the back-print mockup to rank 1
No images are generated, uploaded or deleted.

Usage (dry run is the default - nothing is changed):
    python reorder_listing_photos.py
    python reorder_listing_photos.py --listing-id 1234567890 --apply
    python reorder_listing_photos.py --apply --limit 10
"""
import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from backfill_etsy_photos import (  # noqa: E402
    RECHECK_PHOTO,
    classify_first_image,
    fetch_listings,
    fetch_products_by_listing,
    mockup_srcs,
)
from etsy_photos import (  # noqa: E402
    EtsyAPIError,
    EtsyAuth,
    EtsyConfigError,
    _download,
    arrange_listing_photos,
    back_mockup_src,
    dhash,
)

load_dotenv(Path(__file__).parent / ".env")


async def first_image_is_photo(auth: EtsyAuth, listing_id: str, product: dict):
    """(listing_image_id of rank-1 image if it's a generated photo else None, note)."""
    resp = await auth.request("GET", f"/listings/{listing_id}/images")
    if resp.status_code >= 400:
        return None, f"Etsy GET images HTTP {resp.status_code}"
    images = sorted(resp.json().get("results") or [], key=lambda i: int(i.get("rank") or 999))
    if not images:
        return None, "listing has no images"
    first = images[0]
    url = first.get("url_570xN") or first.get("url_fullxfull")
    hashes = []
    for src in mockup_srcs(product):
        try:
            hashes.append(dhash(await _download(src)))
        except Exception:
            continue
    if not url or not hashes:
        return None, "could not compare rank-1 image with Printify mockups"
    if classify_first_image(dhash(await _download(url)), hashes) == RECHECK_PHOTO:
        return first["listing_image_id"], "rank 1 is a generated photo"
    return None, "rank 1 is already a Printify mockup"


async def main(args) -> int:
    db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    if not settings.get("printify_shop_id"):
        print("No printify_shop_id configured in settings. Aborting.")
        return 1
    auth = EtsyAuth(db.settings)
    try:
        await auth.load()
    except EtsyConfigError as e:
        print(f"Etsy not configured: {e}")
        return 1

    print(f"Reorder listing photos - {'APPLY' if args.apply else 'DRY RUN (nothing changed)'}")
    listings = await fetch_listings(auth, args.listing_id)
    products = await fetch_products_by_listing(int(settings["printify_shop_id"]))
    todo = [l for l in listings if str(l.get("listing_id")) in products][: args.limit]
    print(f"Active listings: {len(listings)} | linked to Printify: {len(todo)} (limit {args.limit})\n")

    changed = failed = 0
    for listing in todo:
        lid = str(listing["listing_id"])
        product = products[lid]
        print(f"[{lid}] {(listing.get('title') or '')[:70]}")
        try:
            photo_id, note = await first_image_is_photo(auth, lid, product)
            print(f"    {note}")
            if not args.apply:
                print("    would put back mockup at rank 1" + (" and that photo at rank 4" if photo_id else ""))
                continue
            result = await arrange_listing_photos(auth, lid, back_mockup_src(product), [photo_id] if photo_id else [])
            print(f"    {result}")
            changed += 1
        except EtsyAPIError as e:
            failed += 1
            print(f"    FAILED {e}")
            if e.status in (403, 429):
                print("Stopping: Etsy refused or rate limited the request.")
                break
        await asyncio.sleep(args.delay)

    print(f"\nDone: {changed} arranged, {failed} failed." + ("" if args.apply else " Dry run: rerun with --apply."))
    return 0 if not failed else 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="re-rank images on Etsy (default: dry run)")
    ap.add_argument("--listing-id", help="only this Etsy listing")
    ap.add_argument("--limit", type=int, default=1000)
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between listings")
    sys.exit(asyncio.run(main(ap.parse_args())))
