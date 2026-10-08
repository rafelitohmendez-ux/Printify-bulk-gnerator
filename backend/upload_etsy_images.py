"""
Etsy Direct Image Uploader
===========================
Bypasses Printify's mockup pipeline entirely: for each Printify product,
finds the matching Etsy listing by title, generates a themed atmospheric
product photo via Gemini (same logic as generate_mockups.py), and uploads
it directly to Etsy as the listing's primary image via Etsy's Open API v3.

Requires in backend/.env:
    MONGO_URL, DB_NAME  - Etsy OAuth tokens are read from / saved to the shared
                           Mongo record settings {"id": "etsy_auth"} (same one the
                           deployed backend uses), so a token rotated by either
                           side keeps working for both.
    ETSY_API_KEY        - Etsy app Keystring
    ETSY_SHARED_SECRET  - Etsy app Shared Secret (combined with the
                           Keystring as "KEYSTRING:SECRET" for x-api-key)
    ETSY_SHOP_ID        - numeric Etsy shop ID
    ETSY_ACCESS_TOKEN / ETSY_REFRESH_TOKEN - only used to seed Mongo if it has
                           no token yet (see etsy_photos.EtsyAuth)

Usage:
    python upload_etsy_images.py                    # dry run, all products
    python upload_etsy_images.py --apply             # apply to all products
    python upload_etsy_images.py --product-id XYZ    # dry run, single product
    python upload_etsy_images.py --product-id XYZ --apply
"""
import argparse
import asyncio
import html
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx
from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import list_products, get_product, _pick_back_mockup, PrintifyError  # noqa: E402
from bulk_seo_update import extract_capsule_name, extract_back_concept  # noqa: E402
from generate_mockups import infer_theme_prompt, generate_background_image  # noqa: E402
from etsy_photos import ETSY_API_BASE, EtsyAuth  # noqa: E402,F401

ROOT_DIR = Path(__file__).parent
ENV_PATH = ROOT_DIR / ".env"
load_dotenv(ENV_PATH)

PROCESSED_FILE = ROOT_DIR / "etsy_upload_processed.json"

# App identifiers only - OAuth tokens live in Mongo (see EtsyAuth). Other scripts
# read _tokens["shop_id"], so keep this shape.
_tokens = {
    "api_key": os.environ.get("ETSY_API_KEY", "").strip(),
    "shared_secret": os.environ.get("ETSY_SHARED_SECRET", "").strip(),
    "shop_id": os.environ.get("ETSY_SHOP_ID", "").strip(),
}

_auth: Optional[EtsyAuth] = None


def load_processed() -> list:
    if PROCESSED_FILE.exists():
        return json.loads(PROCESSED_FILE.read_text())
    return []


def mark_processed(pid: str) -> None:
    processed = load_processed()
    if pid not in processed:
        processed.append(pid)
        PROCESSED_FILE.write_text(json.dumps(processed, indent=2))


async def _get_auth() -> EtsyAuth:
    """EtsyAuth bound to the shared Mongo settings collection (created on first use)."""
    global _auth
    if _auth is None:
        db = AsyncIOMotorClient(os.environ["MONGO_URL"])[os.environ["DB_NAME"]]
        auth = EtsyAuth(db.settings)
        await auth.load()
        _tokens["shop_id"] = auth.shop_id
        _auth = auth
    return _auth


async def etsy_request(method: str, url: str, **kwargs) -> httpx.Response:
    """Call the Etsy API. Tokens come from Mongo; refreshed when expired or on a 401
    (new pair saved back to Mongo), and 429s are retried with backoff."""
    auth = await _get_auth()
    return await auth.request(method, url, **kwargs)


async def fetch_etsy_listings() -> List[Dict[str, Any]]:
    """Fetch all active listings for the configured Etsy shop."""
    shop_id = (await _get_auth()).shop_id
    listings: List[Dict[str, Any]] = []
    offset = 0
    limit = 100
    while True:
        resp = await etsy_request(
            "GET",
            f"{ETSY_API_BASE}/shops/{shop_id}/listings/active",
            params={"limit": limit, "offset": offset},
        )
        if resp.status_code >= 400:
            print(f"ERROR fetching Etsy listings: {resp.status_code} {resp.text}")
            break
        data = resp.json()
        batch = data.get("results") or []
        listings.extend(batch)
        if len(batch) < limit:
            break
        offset += limit
    return listings


def match_etsy_listing(product: Dict[str, Any], listings: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Find the Etsy listing matching the Printify product. Prefers an exact
    full-title match (Printify pushes identical titles to Etsy, so this is
    unambiguous). Falls back to capsule-name matching when it narrows to
    exactly one candidate.

    When multiple listings share a capsule name (e.g. two different designs
    both starting "Voltage Requiem") - which in practice also means the
    Printify title has since diverged from Etsy's (e.g. after an SEO title
    rewrite that was never republished to Etsy, so the exact-title match
    above no longer fires) - narrows further by comparing the back_concept
    extracted from each side's description. That phrase is re-embedded
    verbatim by bulk_seo_update.py's description rewrite, so it stays
    identical on both sides even though the title and surrounding prose
    diverge. Refuses to guess only if neither of these resolves to exactly
    one candidate."""
    product_title = (product.get("title") or "").strip().lower()
    for listing in listings:
        if (listing.get("title") or "").strip().lower() == product_title:
            return listing

    product_name = extract_capsule_name(product.get("title") or "").lower()
    candidates = []
    for listing in listings:
        listing_name = extract_capsule_name(listing.get("title") or "").lower()
        if product_name and listing_name and (
            product_name == listing_name or
            listing_name.startswith(product_name) or
            product_name.startswith(listing_name)
        ):
            candidates.append(listing)

    if len(candidates) == 1:
        return candidates[0]

    if len(candidates) > 1:
        product_back_concept = html.unescape(extract_back_concept(product.get("description") or ""))
        if product_back_concept:
            back_concept_matches = [
                listing for listing in candidates
                if html.unescape(extract_back_concept(listing.get("description") or "")) == product_back_concept
            ]
            if len(back_concept_matches) == 1:
                return back_concept_matches[0]

    return None


async def upload_listing_image(listing_id: int, image_bytes: bytes, file_name: str) -> httpx.Response:
    shop_id = (await _get_auth()).shop_id
    return await etsy_request(
        "POST",
        f"{ETSY_API_BASE}/shops/{shop_id}/listings/{listing_id}/images",
        files={"image": (file_name, image_bytes, "image/png")},
        data={"rank": "1"},
    )


async def process_product(
    product: Dict[str, Any],
    etsy_listings: List[Dict[str, Any]],
    apply: bool,
    local_image_bytes: Optional[bytes] = None,
    listing_id_override: Optional[int] = None,
) -> bool:
    pid = str(product.get("id"))
    title = (product.get("title") or "").strip()
    print(f"\n[{title[:60]}] ({pid})")

    if listing_id_override is not None:
        listing_id = listing_id_override
        print(f"  Using listing_id override (skipping title match): {listing_id}")
    else:
        listing = match_etsy_listing(product, etsy_listings)
        if not listing:
            print("  SKIP - no matching Etsy listing found by title")
            return False
        listing_id = listing.get("listing_id")
        print(f"  Matched Etsy listing: {listing.get('title', '')[:60]} ({listing_id})")

    if local_image_bytes is not None:
        image_bytes = local_image_bytes
        print(f"  Using pre-generated local image ({len(image_bytes)} bytes) - skipping Gemini")
    else:
        back_concept = extract_back_concept(product.get("description") or "")
        if not back_concept:
            print("  SKIP - no back concept extractable from description")
            return False

        scene_prompt = infer_theme_prompt(product)
        print(f"  Scene: {scene_prompt[:80]}...")

        design_image_bytes = None
        back_image = _pick_back_mockup(product.get("images") or [])
        if back_image and back_image.get("src"):
            try:
                async with httpx.AsyncClient(timeout=60.0) as c:
                    resp = await c.get(back_image["src"])
                    resp.raise_for_status()
                    design_image_bytes = resp.content
                print(f"  Using actual back print as design reference ({len(design_image_bytes)} bytes)")
            except Exception as e:
                print(f"  WARNING - could not download back print image, falling back to text-only: {e}")

        image_bytes = await generate_background_image(scene_prompt, back_concept, design_image_bytes)
        if not image_bytes:
            print("  ERROR - Gemini returned no image")
            return False
        print(f"  Product photo generated ({len(image_bytes)} bytes)")

    if not apply:
        preview_path = ROOT_DIR / f"etsy_preview_{pid}.png"
        preview_path.write_bytes(image_bytes)
        print(f"  DRY RUN - image saved to {preview_path}")
        print("  Re-run with --apply to upload to Etsy as the primary listing image")
        return True

    cname = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")[:40] or pid
    resp = await upload_listing_image(listing_id, image_bytes, f"{cname}.png")
    if resp.status_code >= 400:
        print(f"  ERROR uploading to Etsy: {resp.status_code} {resp.text}")
        return False

    mark_processed(pid)
    print("  APPLIED - uploaded as primary Etsy listing image")
    return True


async def fetch_target_products(shop_id: int, product_id: Optional[str]) -> List[Dict[str, Any]]:
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


async def main(apply: bool, product_id: Optional[str], image_path: Optional[str] = None, listing_id: Optional[int] = None):
    if image_path and not product_id:
        print("--image-path requires --product-id (it only applies to a single product). Aborting.")
        return
    if listing_id and not product_id:
        print("--listing-id requires --product-id (it only applies to a single product). Aborting.")
        return

    local_image_bytes = None
    if image_path:
        path = Path(image_path)
        if not path.exists():
            print(f"--image-path file not found: {path}. Aborting.")
            return
        local_image_bytes = path.read_bytes()

    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    printify_shop_id = settings.get("printify_shop_id")
    if not printify_shop_id:
        print("No printify_shop_id configured in settings. Aborting.")
        client.close()
        return
    printify_shop_id = int(printify_shop_id)

    mode = "SINGLE PRODUCT" if product_id else "ALL PRODUCTS"
    print(f"Etsy Direct Image Uploader - {mode} - {'APPLY' if apply else 'DRY RUN'}\n")

    print("Fetching Etsy listings...")
    etsy_listings = await fetch_etsy_listings()
    print(f"Found {len(etsy_listings)} active Etsy listing(s).")

    products = await fetch_target_products(printify_shop_id, product_id)
    print(f"Found {len(products)} Printify product(s) to process.")

    if not product_id and apply:
        already_done = set(load_processed())
        if already_done:
            before = len(products)
            products = [p for p in products if str(p.get("id")) not in already_done]
            print(f"Skipping {before - len(products)} product(s) already applied in a prior run.")

    print("-" * 70)

    done, skipped, errors = 0, 0, 0
    for p in products:
        try:
            ok = await process_product(
                p, etsy_listings, apply,
                local_image_bytes=local_image_bytes,
                listing_id_override=listing_id,
            )
        except Exception as e:
            pid = str(p.get("id"))
            title = (p.get("title") or "").strip()
            print(f"  ERROR - unexpected exception processing {title[:60]} ({pid}): {e}")
            errors += 1
            await asyncio.sleep(1.0)
            continue
        if ok:
            done += 1
        else:
            skipped += 1
        await asyncio.sleep(1.0)

    print("\n" + "=" * 70)
    print(f"{'APPLIED' if apply else 'DRY RUN'}: {done} processed, {skipped} skipped, {errors} errors")
    if not apply and done:
        print("Re-run with --apply to push these images to Etsy.")

    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Etsy Direct Image Uploader")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--product-id", default=None, help="Only process this Printify product ID")
    parser.add_argument("--image-path", default=None, help="Upload this local PNG instead of generating one via Gemini (requires --product-id)")
    parser.add_argument("--listing-id", type=int, default=None, help="Skip title-matching and upload straight to this Etsy listing_id (requires --product-id)")
    args = parser.parse_args()
    asyncio.run(main(apply=args.apply, product_id=args.product_id, image_path=args.image_path, listing_id=args.listing_id))
