"""
Backfill atmospheric photos onto existing Etsy listings
=======================================================
For live Etsy listings created before the automatic photo pipeline worked:
generates a themed atmospheric photo with Gemini (using the Printify back print
as the design reference), uploads it as the PRIMARY image (rank 1) and moves the
back-print mockup to rank 2 (same code path as the deployed app, etsy_photos.py).

Safe to re-run: listings that already got a photo are skipped, based on
  - the etsy_photo_log Mongo collection (written by this script and the app)
  - capsules whose etsy_photos_status is 'success'
  - Printify products listed in etsy_upload_processed.json (old upload script)
A listing is only logged as done after a successful upload.

Etsy tokens come from the shared Mongo record settings {"id": "etsy_auth"}.

Usage (dry run is the default - nothing is generated or uploaded):
    python backfill_etsy_photos.py --limit 10 --cyber-first
    python backfill_etsy_photos.py --listing-id 1234567890
    python backfill_etsy_photos.py --apply --limit 5 --cyber-first --delay 30
    # Don't trust etsy_upload_processed.json; work through everything in batches of 5:
    python backfill_etsy_photos.py --apply --recheck-processed --cyber-first --limit 1000 --batch-size 5

With --batch-size the run stops early if 2+ listings in a batch fail; any run stops
on Etsy 403, Etsy 429 after retries, or a Gemini quota error.
"""
import argparse
import asyncio
import json
import os
import re
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from bulk_seo_update import extract_back_concept  # noqa: E402
from etsy_photos import (  # noqa: E402
    BACK_MATCH_MAX_DISTANCE,
    PHOTO_LOG_COLLECTION,
    EtsyAPIError,
    EtsyAuth,
    EtsyConfigError,
    _download,
    _hamming,
    back_mockup_src,
    dhash,
    log_listing_photo,
    upload_primary_photo,
)
from printify_client import list_products  # noqa: E402

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

PROCESSED_FILE = ROOT_DIR / "etsy_upload_processed.json"
CYBER_PATTERN = re.compile(r"cyber|techwear", re.IGNORECASE)  # also matches cyberpunk / cybergoth
GEMINI_IMAGE_CALLS_PER_LISTING = 1
ETSY_PAGE_SIZE = 100
RECHECK_MOCKUP = "mockup"
RECHECK_PHOTO = "photo"
RECHECK_CONCURRENCY = 4
RECHECK_MAX_MOCKUPS = 12  # Printify mockups compared per listing
QUOTA_PATTERN = re.compile(r"RESOURCE_EXHAUSTED|quota", re.IGNORECASE)


# -----------------------------
# Pure selection logic (unit-tested)
# -----------------------------
def is_cyber(listing: Dict[str, Any]) -> bool:
    text = " ".join([listing.get("title") or ""] + list(listing.get("tags") or []))
    return bool(CYBER_PATTERN.search(text))


def decide(
    listing: Dict[str, Any],
    product: Optional[Dict[str, Any]],
    logged_listing_ids: Set[str],
    capsule_listing_ids: Set[str],
    processed_product_ids: Set[str],
    recheck: Optional[Dict[str, str]] = None,
) -> Tuple[bool, str]:
    """(process?, reason) for one Etsy listing.

    recheck maps Printify product IDs from etsy_upload_processed.json to what the
    listing's first Etsy image actually is: RECHECK_MOCKUP, RECHECK_PHOTO or an error.
    """
    lid = str(listing.get("listing_id"))
    if lid in logged_listing_ids:
        return False, "already has atmospheric photo (etsy_photo_log)"
    if lid in capsule_listing_ids:
        return False, "already has atmospheric photo (approve pipeline)"
    if not product:
        return False, "no Printify product linked to this listing"
    pid = str(product.get("id"))
    rechecked = False
    if pid in processed_product_ids:
        status = (recheck or {}).get(pid)
        if status is None:
            return False, "already has atmospheric photo (etsy_upload_processed.json)"
        if status == RECHECK_PHOTO:
            return False, "already has atmospheric photo (rechecked: first Etsy image is not a mockup)"
        if status != RECHECK_MOCKUP:
            return False, "in etsy_upload_processed.json, recheck failed (kept skipped)"
        rechecked = True
    if not extract_back_concept(product.get("description") or ""):
        return False, "no back concept in the Printify description"
    reason = "needs atmospheric photo"
    if rechecked:
        reason += " (rechecked: first Etsy image is a Printify mockup)"
    if not back_mockup_src(product):
        reason += " (no Printify back mockup: text-only photo, no rank-2 reorder)"
    return True, reason


def select(
    listings: Iterable[Dict[str, Any]],
    products_by_listing: Dict[str, Dict[str, Any]],
    logged_listing_ids: Set[str],
    capsule_listing_ids: Set[str],
    processed_product_ids: Set[str],
    limit: int,
    cyber_first: bool,
    recheck: Optional[Dict[str, str]] = None,
) -> Tuple[List[Tuple[Dict[str, Any], Dict[str, Any], str]], List[Tuple[Dict[str, Any], str]], int]:
    """Returns (selected [(listing, product, reason)], skipped [(listing, reason)], eligible_count)."""
    eligible, skipped = [], []
    for listing in listings:
        product = products_by_listing.get(str(listing.get("listing_id")))
        ok, reason = decide(listing, product, logged_listing_ids, capsule_listing_ids, processed_product_ids, recheck)
        if ok:
            eligible.append((listing, product, reason))
        else:
            skipped.append((listing, reason))
    if cyber_first:
        eligible.sort(key=lambda item: not is_cyber(item[0]))  # stable: keeps Etsy order within groups
    return eligible[:limit], skipped, len(eligible)


def stop_reason(error: Exception) -> Optional[str]:
    """Errors that should stop the whole run rather than move on to the next listing."""
    status = getattr(error, "status", None)
    if isinstance(error, EtsyAPIError) and status == 403:
        return "Etsy returned 403 (permission/token problem)"
    if isinstance(error, EtsyAPIError) and status == 429:
        return "Etsy still rate limiting (429) after retries"
    if QUOTA_PATTERN.search(str(error)):
        return "Gemini quota exhausted"
    return None


def classify_first_image(first_hash: int, mockup_hashes: List[int]) -> str:
    """RECHECK_MOCKUP if the listing's first image looks like one of the product's Printify mockups."""
    if any(_hamming(first_hash, h) <= BACK_MATCH_MAX_DISTANCE for h in mockup_hashes):
        return RECHECK_MOCKUP
    return RECHECK_PHOTO


def mockup_srcs(product: Dict[str, Any]) -> List[str]:
    images = product.get("images") or []
    chosen = [i for i in images if i.get("is_selected_for_publishing")] or images
    return [i["src"] for i in chosen if i.get("src")][:RECHECK_MAX_MOCKUPS]


# -----------------------------
# Data loading
# -----------------------------
async def fetch_listings(auth: EtsyAuth, listing_id: Optional[str]) -> List[Dict[str, Any]]:
    if listing_id:
        resp = await auth.request("GET", f"/listings/{listing_id}")
        if resp.status_code >= 400:
            raise SystemExit(f"Etsy GET listing {listing_id} failed: HTTP {resp.status_code} {resp.text[:200]}")
        return [resp.json()]
    listings: List[Dict[str, Any]] = []
    offset = 0
    while True:
        resp = await auth.request(
            "GET", f"/shops/{auth.shop_id}/listings/active", params={"limit": ETSY_PAGE_SIZE, "offset": offset}
        )
        if resp.status_code >= 400:
            raise SystemExit(f"Etsy GET active listings failed: HTTP {resp.status_code} {resp.text[:200]}")
        batch = resp.json().get("results") or []
        listings.extend(batch)
        if len(batch) < ETSY_PAGE_SIZE:
            return listings
        offset += ETSY_PAGE_SIZE


async def fetch_products_by_listing(shop_id: int) -> Dict[str, Dict[str, Any]]:
    """Printify products keyed by their external Etsy listing ID."""
    by_listing: Dict[str, Dict[str, Any]] = {}
    page = 1
    while True:
        result = await list_products(shop_id, page=page, limit=50)
        batch = result.get("data") or []
        for p in batch:
            lid = (p.get("external") or {}).get("id")
            if lid:
                by_listing[str(lid)] = p
        if len(batch) < 50:
            return by_listing
        page += 1


async def load_done_sets(db) -> Tuple[Set[str], Set[str], Set[str]]:
    logged = {
        d["listing_id"] async for d in db[PHOTO_LOG_COLLECTION].find({"status": "success"}, {"listing_id": 1, "_id": 0})
    }
    from_capsules = {
        str(d["etsy_listing_id"]) async for d in db.capsules.find(
            {"etsy_photos_status": "success", "etsy_listing_id": {"$ne": None}}, {"etsy_listing_id": 1, "_id": 0}
        )
    }
    processed = set(json.loads(PROCESSED_FILE.read_text())) if PROCESSED_FILE.exists() else set()
    return logged, from_capsules, {str(p) for p in processed}


async def recheck_first_image(auth: EtsyAuth, listing_id: str, product: Dict[str, Any]) -> str:
    """Look at what the listing's first Etsy image really is (no Gemini calls)."""
    resp = await auth.request("GET", f"/listings/{listing_id}/images")
    if resp.status_code >= 400:
        return f"error: Etsy GET images HTTP {resp.status_code}"
    images = sorted(resp.json().get("results") or [], key=lambda i: int(i.get("rank") or 999))
    if not images:
        return RECHECK_MOCKUP  # no images at all: definitely needs the photo
    url = images[0].get("url_570xN") or images[0].get("url_fullxfull")
    srcs = mockup_srcs(product)
    if not url or not srcs:
        return "error: nothing to compare"
    first_hash = dhash(await _download(url))
    hashes = []
    for src in srcs:
        try:
            hashes.append(dhash(await _download(src)))
        except Exception:
            continue
        if classify_first_image(first_hash, hashes[-1:]) == RECHECK_MOCKUP:
            return RECHECK_MOCKUP
    return classify_first_image(first_hash, hashes) if hashes else "error: could not load Printify mockups"


async def recheck_processed(
    auth: EtsyAuth, listings: List[Dict[str, Any]], products: Dict[str, Dict[str, Any]],
    skip_listing_ids: Set[str], processed: Set[str],
) -> Dict[str, str]:
    """{printify_product_id: status} for linked listings that etsy_upload_processed.json marks as done."""
    todo = []
    for listing in listings:
        lid = str(listing.get("listing_id"))
        product = products.get(lid)
        if product and lid not in skip_listing_ids and str(product.get("id")) in processed:
            todo.append((lid, product))
    sem = asyncio.Semaphore(RECHECK_CONCURRENCY)

    async def one(lid: str, product: Dict[str, Any]) -> Tuple[str, str]:
        async with sem:
            try:
                return str(product["id"]), await recheck_first_image(auth, lid, product)
            except Exception as e:
                return str(product["id"]), f"error: {type(e).__name__}: {str(e)[:120]}"

    print(f"\nRechecking {len(todo)} listing(s) from etsy_upload_processed.json on Etsy...")
    return dict(await asyncio.gather(*(one(lid, p) for lid, p in todo)))


# -----------------------------
# Apply
# -----------------------------
async def process_one(auth: EtsyAuth, db, listing: Dict[str, Any], product: Dict[str, Any]) -> str:
    import generate_mockups
    from generate_mockups import generate_background_image, infer_theme_prompt

    lid = str(listing["listing_id"])
    back_src = back_mockup_src(product)
    design = None
    if back_src:
        try:
            design = await _download(back_src)
        except Exception as e:
            print(f"    WARNING could not download Printify back print, using text-only prompt: {e}")
    image = await generate_background_image(
        infer_theme_prompt(product), extract_back_concept(product.get("description") or ""), design
    )
    if not image:
        error = getattr(generate_mockups, "LAST_IMAGE_ERROR", None)
        raise RuntimeError(f"Gemini returned no image{f': {error}' if error else ''}")
    name = re.sub(r"[^a-z0-9]+", "_", (listing.get("title") or lid).lower()).strip("_")[:40] or lid
    note = await upload_primary_photo(auth, lid, image, f"{name}_etsy.png", back_src)
    await log_listing_photo(db[PHOTO_LOG_COLLECTION], lid, str(product.get("id")), "backfill", note)
    return note


async def main(args: argparse.Namespace) -> int:
    apply = args.apply
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

    print(f"Etsy photo backfill - {'APPLY' if apply else 'DRY RUN (nothing generated or uploaded)'}")
    print(f"  limit={args.limit} cyber_first={args.cyber_first} delay={args.delay}s"
          + (f" listing_id={args.listing_id}" if args.listing_id else ""))

    try:
        listings = await fetch_listings(auth, args.listing_id)
    except EtsyAPIError as e:
        print(f"\nERROR: {e}")
        return 1
    products = await fetch_products_by_listing(int(settings["printify_shop_id"]))
    logged, from_capsules, processed = await load_done_sets(db)
    recheck = None
    if args.recheck_processed:
        recheck = await recheck_processed(auth, listings, products, logged | from_capsules, processed)
        counts = Counter(s if s in (RECHECK_MOCKUP, RECHECK_PHOTO) else "error" for s in recheck.values())
        print(f"  mockup first (re-queued): {counts[RECHECK_MOCKUP]} | photo first (kept skipped): "
              f"{counts[RECHECK_PHOTO]} | could not check (kept skipped): {counts['error']}")
        for pid, status in recheck.items():
            if status not in (RECHECK_MOCKUP, RECHECK_PHOTO):
                print(f"    printify={pid}: {status}")
    selected, skipped, eligible = select(
        listings, products, logged, from_capsules, processed, args.limit, args.cyber_first, recheck
    )

    print(f"\nActive Etsy listings: {len(listings)} | linked to Printify: "
          f"{sum(1 for l in listings if str(l.get('listing_id')) in products)} | "
          f"need a photo: {eligible} | skipped: {len(skipped)}")
    print("\nSkipped by reason:")
    for reason, n in Counter(r for _, r in skipped).most_common():
        print(f"  {n:4d}  {reason}")

    print(f"\n{'Would process' if not apply else 'Processing'} {len(selected)} listing(s):")
    for i, (listing, product, reason) in enumerate(selected, 1):
        tag = "CYBER" if is_cyber(listing) else "     "
        print(f"  {i:2d}. [{tag}] {listing.get('listing_id')}  {(listing.get('title') or '')[:70]}")
        print(f"        printify={product.get('id')}  -> {reason}")

    calls = len(selected) * GEMINI_IMAGE_CALLS_PER_LISTING
    print(f"\nEstimated Gemini image calls: {calls} for this batch "
          f"({eligible * GEMINI_IMAGE_CALLS_PER_LISTING} for the whole backlog)")
    if not apply:
        print("\nDRY RUN complete. Re-run with --apply to generate and upload.")
        return 0

    done, skipped_now = 0, 0
    failures: List[Tuple[str, str]] = []
    stopped: Optional[str] = None
    started = time.monotonic()
    size = args.batch_size if args.batch_size > 0 else max(len(selected), 1)
    batches = [selected[s:s + size] for s in range(0, len(selected), size)]
    i = 0
    for b, batch in enumerate(batches, 1):
        if len(batches) > 1:
            print(f"\n--- Batch {b}/{len(batches)} ---")
        batch_failed = 0
        for j, (listing, product, _) in enumerate(batch):
            i += 1
            lid = str(listing["listing_id"])
            # Re-check right before working, in case another run/the app just did it
            if await db[PHOTO_LOG_COLLECTION].find_one({"listing_id": lid, "status": "success"}):
                skipped_now += 1
                print(f"\n[{i}/{len(selected)}] {lid} - already done, skipping")
                continue
            print(f"\n[{i}/{len(selected)}] {lid} {(listing.get('title') or '')[:60]}")
            try:
                note = await process_one(auth, db, listing, product)
                done += 1
                print(f"    OK - photo is rank 1; {note}")
            except Exception as e:
                batch_failed += 1
                failures.append((lid, f"{type(e).__name__}: {str(e)[:300]}"))
                print(f"    FAILED - {failures[-1][1]}")
                stopped = stop_reason(e)
                if stopped:
                    break
            if j < len(batch) - 1 and args.delay > 0:
                await asyncio.sleep(args.delay)
        if not stopped and batch_failed >= 2 and len(batches) > 1:
            stopped = f"{batch_failed} listings failed in batch {b}"
        if stopped:
            break
        if b < len(batches) and args.batch_pause > 0:
            print(f"\n(pausing {args.batch_pause:.0f}s between batches)")
            await asyncio.sleep(args.batch_pause)

    print("\n" + "=" * 60)
    if stopped:
        print(f"STOPPED EARLY: {stopped}")
    print(f"SUMMARY: {done} uploaded, {len(failures)} failed, {skipped_now} skipped "
          f"in {time.monotonic() - started:.0f}s. Remaining backlog: {eligible - done}.")
    for lid, err in failures:
        print(f"  FAILED {lid}: {err}")
    if failures:
        print("Retry one with: python backend/backfill_etsy_photos.py --apply --listing-id <ID>")
    return 1 if failures or stopped else 0


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill atmospheric photos onto existing Etsy listings.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", default=True, help="List what would be processed (default)")
    mode.add_argument("--apply", action="store_true", help="Generate and upload for real")
    parser.add_argument("--limit", type=int, default=5, help="Max listings to process (default 5)")
    parser.add_argument("--cyber-first", action="store_true", help="Prioritize listings with cyber/cyberpunk/techwear")
    parser.add_argument("--delay", type=float, default=30, help="Seconds between listings (default 30)")
    parser.add_argument("--listing-id", help="Process a single Etsy listing")
    parser.add_argument("--recheck-processed", action="store_true",
                        help="Don't trust etsy_upload_processed.json: check each listing's first Etsy image and "
                             "re-queue it if that image is a Printify mockup (no Gemini calls)")
    parser.add_argument("--batch-size", type=int, default=0,
                        help="Process in batches of N; stops if 2+ fail in a batch (default: one batch)")
    parser.add_argument("--batch-pause", type=float, default=60, help="Seconds between batches (default 60)")
    return parser.parse_args(argv)


if __name__ == "__main__":
    sys.exit(asyncio.run(main(parse_args())))
