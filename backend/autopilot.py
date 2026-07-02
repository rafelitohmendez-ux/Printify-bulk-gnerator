"""
MidnightRotation Autopilot System
==================================
Handles the full monthly lifecycle of your Etsy/Printify catalog:

STAGE 1 — Last Chance (Month 1, Day 1)
  Listings with 50+ views and 0 orders enter a 7-day last-chance window.
  They are flagged in purge_history.json for manual markdown on Etsy (30% off).

STAGE 2 — Hibernation (Month 1, Day 8)
  Last-chance listings that still have 0 orders get unpublished from Etsy
  (deactivated but NOT deleted — design is preserved).

STAGE 3 — Revival Window (Day 90)
  Hibernated listings reactivate for a 2-week window to catch a new buyer pool
  or seasonal demand spike.

STAGE 4 — Permanent Delete (Day 104)
  Listings that survive revival with 0 orders are permanently deleted from
  Printify and their slot is logged for replacement capsule generation.

NEW LISTING PIPELINE
  Automatically runs SEO optimization + thumbnail correction on any newly
  pushed product that hasn't been processed yet.

Usage:
    # Full monthly run (dry-run)
    python autopilot.py --csv etsy_ads_export.csv

    # Full monthly run (apply)
    python autopilot.py --csv etsy_ads_export.csv --apply

    # New listing pipeline only (no CSV needed)
    python autopilot.py --new-listings-only
    python autopilot.py --new-listings-only --apply

    # Revival check only
    python autopilot.py --revival-only
    python autopilot.py --revival-only --apply
"""
import argparse
import asyncio
import csv
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from dotenv import load_dotenv
from motor.motor_asyncio import AsyncIOMotorClient

sys.path.insert(0, str(Path(__file__).parent))
from printify_client import (
    list_products,
    update_product,
    prioritize_back_mockup,
    PrintifyError,
)
from bulk_seo_update import generate_seo, extract_back_concept, extract_capsule_name, DESCRIPTION_TEMPLATE

ROOT_DIR = Path(__file__).parent
load_dotenv(ROOT_DIR / ".env")

PURGE_HISTORY_FILE = ROOT_DIR / "purge_history.json"

# Thresholds
MIN_IMPRESSIONS_FOR_PURGE = 50   # listing must have 50+ views before eligible
LAST_CHANCE_DAYS = 7             # days in last-chance markdown window
HIBERNATION_DAYS = 90            # days before revival window opens
REVIVAL_DAYS = 14                # length of revival window
PROCESSED_SEO_FILE = ROOT_DIR / "seo_processed.json"  # tracks which product IDs have been SEO'd


# ─── Purge History ───────────────────────────────────────────────────────────

def load_history() -> Dict[str, Any]:
    if PURGE_HISTORY_FILE.exists():
        return json.loads(PURGE_HISTORY_FILE.read_text())
    return {"last_chance": {}, "hibernated": {}, "revival": {}, "deleted": []}


def save_history(history: Dict[str, Any]):
    PURGE_HISTORY_FILE.write_text(json.dumps(history, indent=2))


def load_seo_processed() -> Dict[str, str]:
    if PROCESSED_SEO_FILE.exists():
        return json.loads(PROCESSED_SEO_FILE.read_text())
    return {}


def save_seo_processed(data: Dict[str, str]):
    PROCESSED_SEO_FILE.write_text(json.dumps(data, indent=2))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def days_since(iso_str: str) -> int:
    then = datetime.fromisoformat(iso_str)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - then).days


# ─── CSV Parsing ─────────────────────────────────────────────────────────────

def parse_etsy_csv(csv_path: str) -> List[Dict[str, Any]]:
    """Parse Etsy Ads CSV export. Returns list of listing dicts."""
    listings = []
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            title = (row.get("Listing") or row.get("listing") or "").strip()
            if not title:
                continue
            try:
                views = int(str(row.get("Views") or row.get("views") or "0").replace(",", ""))
                clicks = int(str(row.get("Clicks") or row.get("clicks") or "0").replace(",", ""))
                orders = int(str(row.get("Orders") or row.get("orders") or "0").replace(",", ""))
            except ValueError:
                continue
            listings.append({
                "title": title,
                "views": views,
                "clicks": clicks,
                "orders": orders,
            })
    return listings


def match_listing_to_product(listing_title: str, products: List[Dict]) -> Optional[Dict]:
    """Find Printify product whose title starts with the same capsule name."""
    listing_name = extract_capsule_name(listing_title).lower()
    for p in products:
        product_name = extract_capsule_name(p.get("title") or "").lower()
        if product_name and listing_name and (
            product_name == listing_name or
            listing_name.startswith(product_name) or
            product_name.startswith(listing_name)
        ):
            return p
    return None


# ─── Printify: unpublish + delete ────────────────────────────────────────────

async def unpublish_product(shop_id: int, product_id: str) -> None:
    """Remove a product from its Etsy sales channel (deactivate listing)."""
    import httpx
    from printify_client import _client, _check
    async with _client() as c:
        await _check(
            await c.post(
                f"/shops/{shop_id}/products/{product_id}/unpublish.json"
            )
        )


async def delete_product(shop_id: int, product_id: str) -> None:
    """Permanently delete a product from Printify."""
    from printify_client import _client, _check
    async with _client() as c:
        await _check(
            await c.delete(f"/shops/{shop_id}/products/{product_id}.json")
        )


async def republish_product(shop_id: int, product_id: str) -> None:
    """Re-publish a hibernated product to its Etsy channel."""
    from printify_client import _client, _check
    async with _client() as c:
        await _check(
            await c.post(
                f"/shops/{shop_id}/products/{product_id}/publish.json",
                json={"title": True, "description": True, "images": True,
                      "variants": True, "tags": True, "keyFeatures": True,
                      "shipping_template": True}
            )
        )


# ─── Stage 1: Last Chance ────────────────────────────────────────────────────

async def run_last_chance(
    listings: List[Dict],
    products: List[Dict],
    history: Dict,
    apply: bool
) -> List[Dict]:
    """Flag zero-performing listings for Last Chance markdown."""
    print("\n── STAGE 1: LAST CHANCE ────────────────────────────────────────")
    candidates = []
    already_in_cycle = (
        set(history["last_chance"].keys()) |
        set(history["hibernated"].keys()) |
        set(history["revival"].keys())
    )

    for listing in listings:
        if listing["orders"] > 0:
            continue
        if listing["views"] < MIN_IMPRESSIONS_FOR_PURGE:
            print(f"  SKIP  {listing['title'][:50]} — only {listing['views']} views (need {MIN_IMPRESSIONS_FOR_PURGE}+)")
            continue

        product = match_listing_to_product(listing["title"], products)
        if not product:
            print(f"  SKIP  {listing['title'][:50]} — no matching Printify product found")
            continue

        pid = str(product.get("id"))
        if pid in already_in_cycle:
            print(f"  SKIP  {listing['title'][:50]} — already in purge cycle")
            continue

        print(f"  LAST CHANCE  {listing['title'][:50]} ({pid}) — {listing['views']} views, 0 orders")
        candidates.append({"title": listing["title"], "product_id": pid})

        if apply:
            history["last_chance"][pid] = {
                "title": listing["title"],
                "entered_at": now_iso(),
                "views": listing["views"],
            }

    if apply and candidates:
        save_history(history)
        print(f"\n  ✅ {len(candidates)} listings entered Last Chance.")
        print("  ACTION REQUIRED: Apply 30% markdown manually on Etsy for these listings.")
    elif candidates:
        print(f"\n  DRY RUN: {len(candidates)} listings would enter Last Chance.")
        print("  Re-run with --apply to log them.")

    return candidates


# ─── Stage 2: Hibernation ────────────────────────────────────────────────────

async def run_hibernation(
    listings: List[Dict],
    products: List[Dict],
    shop_id: int,
    history: Dict,
    apply: bool
):
    """Hibernate Last Chance listings that still have 0 orders after 7 days."""
    print("\n── STAGE 2: HIBERNATION ────────────────────────────────────────")
    listing_orders = {extract_capsule_name(l["title"]).lower(): l["orders"] for l in listings}
    hibernated = 0

    for pid, data in list(history["last_chance"].items()):
        if days_since(data["entered_at"]) < LAST_CHANCE_DAYS:
            print(f"  WAIT  {data['title'][:50]} — only {days_since(data['entered_at'])} days in Last Chance")
            continue

        capsule_name = extract_capsule_name(data["title"]).lower()
        current_orders = listing_orders.get(capsule_name, 0)

        if current_orders > 0:
            print(f"  CONVERTED  {data['title'][:50]} — {current_orders} orders, removing from cycle")
            if apply:
                del history["last_chance"][pid]
            continue

        print(f"  HIBERNATE  {data['title'][:50]} ({pid}) — 0 orders after {LAST_CHANCE_DAYS} days")
        if apply:
            try:
                await unpublish_product(shop_id, pid)
                history["hibernated"][pid] = {
                    "title": data["title"],
                    "hibernated_at": now_iso(),
                    "original_views": data.get("views", 0),
                }
                del history["last_chance"][pid]
                hibernated += 1
                print(f"    ✅ Deactivated on Etsy")
            except PrintifyError as e:
                print(f"    ❌ ERROR: {e}")
            await asyncio.sleep(0.5)

    if apply:
        save_history(history)
        print(f"\n  ✅ {hibernated} listings hibernated.")
    else:
        print(f"\n  DRY RUN: listings ready to hibernate shown above.")


# ─── Stage 3: Revival ────────────────────────────────────────────────────────

async def run_revival(
    shop_id: int,
    history: Dict,
    apply: bool
):
    """Re-activate hibernated listings that have been dormant for 90 days."""
    print("\n── STAGE 3: REVIVAL WINDOW ─────────────────────────────────────")
    revived = 0

    for pid, data in list(history["hibernated"].items()):
        age = days_since(data["hibernated_at"])
        if age < HIBERNATION_DAYS:
            print(f"  WAIT  {data['title'][:50]} — hibernated {age} days (need {HIBERNATION_DAYS})")
            continue

        print(f"  REVIVE  {data['title'][:50]} ({pid}) — hibernated {age} days")
        if apply:
            try:
                await republish_product(shop_id, pid)
                history["revival"][pid] = {
                    "title": data["title"],
                    "revived_at": now_iso(),
                    "original_views": data.get("original_views", 0),
                }
                del history["hibernated"][pid]
                revived += 1
                print(f"    ✅ Re-activated on Etsy for {REVIVAL_DAYS}-day window")
            except PrintifyError as e:
                print(f"    ❌ ERROR: {e}")
            await asyncio.sleep(0.5)

    if apply:
        save_history(history)
        print(f"\n  ✅ {revived} listings re-activated for revival window.")
    else:
        print(f"\n  DRY RUN: listings ready for revival shown above.")


# ─── Stage 4: Permanent Delete ───────────────────────────────────────────────

async def run_permanent_delete(
    listings: List[Dict],
    shop_id: int,
    history: Dict,
    apply: bool
):
    """Permanently delete revival listings that still have 0 orders."""
    print("\n── STAGE 4: PERMANENT DELETE ───────────────────────────────────")
    listing_orders = {extract_capsule_name(l["title"]).lower(): l["orders"] for l in listings}
    deleted = 0
    slots_to_fill = []

    for pid, data in list(history["revival"].items()):
        age = days_since(data["revived_at"])
        if age < REVIVAL_DAYS:
            print(f"  WAIT  {data['title'][:50]} — {age} days into revival (need {REVIVAL_DAYS})")
            continue

        capsule_name = extract_capsule_name(data["title"]).lower()
        current_orders = listing_orders.get(capsule_name, 0)

        if current_orders > 0:
            print(f"  SURVIVED  {data['title'][:50]} — converted during revival, keeping")
            if apply:
                del history["revival"][pid]
            continue

        print(f"  DELETE  {data['title'][:50]} ({pid}) — 0 orders after full cycle")
        slots_to_fill.append(data["title"])

        if apply:
            try:
                await delete_product(shop_id, pid)
                history["deleted"].append({
                    "title": data["title"],
                    "product_id": pid,
                    "deleted_at": now_iso(),
                    "reason": "zero orders after last-chance + hibernation + revival cycle",
                })
                del history["revival"][pid]
                deleted += 1
                print(f"    ✅ Permanently deleted from Printify")
            except PrintifyError as e:
                print(f"    ❌ ERROR: {e}")
            await asyncio.sleep(0.5)

    if apply:
        save_history(history)
        print(f"\n  ✅ {deleted} listings permanently deleted.")
        if slots_to_fill:
            print(f"\n  ⚠️  ACTION REQUIRED: Generate {len(slots_to_fill)} replacement capsule(s)")
            print("  Run your generator dashboard and approve new capsules to fill these slots:")
            for title in slots_to_fill:
                print(f"    - Replace: {title}")
    else:
        print(f"\n  DRY RUN: {len(slots_to_fill)} listings would be permanently deleted.")


# ─── New Listing SEO Pipeline ────────────────────────────────────────────────

async def run_new_listing_pipeline(
    shop_id: int,
    apply: bool
):
    """Auto-apply SEO + thumbnail to any new product not yet processed."""
    print("\n── NEW LISTING PIPELINE ────────────────────────────────────────")
    processed = load_seo_processed()
    products = []
    page = 1
    while True:
        try:
            result = await list_products(shop_id, page=page, limit=50)
        except PrintifyError as e:
            print(f"  ERROR fetching products: {e}")
            break
        batch = result.get("data") or []
        if not batch:
            break
        products.extend(batch)
        if len(batch) < 50:
            break
        page += 1

    new_count = 0
    for p in products:
        pid = str(p.get("id"))
        if pid in processed:
            continue

        title = (p.get("title") or "").strip()
        description = (p.get("description") or "").strip()
        capsule_name = extract_capsule_name(title)
        back_concept = extract_back_concept(description)

        if not back_concept:
            print(f"  SKIP  {title[:50]} — no back concept extractable")
            processed[pid] = "skipped"
            continue

        print(f"  NEW   {title[:50]} ({pid})")
        seo = await generate_seo(capsule_name, back_concept)
        if not seo:
            print(f"    ERROR — Gemini returned no data")
            continue

        new_title = seo.get("title", "")[:140]
        new_tags = seo.get("tags", [])
        new_desc = DESCRIPTION_TEMPLATE.format(
            capsule_name=capsule_name.upper(),
            back_graphic=back_concept.rstrip(".").lower(),
        )

        print(f"    Title: {new_title}")
        print(f"    Tags:  {', '.join(new_tags[:5])}...")

        if apply:
            try:
                await update_product(shop_id, pid, {
                    "title": new_title,
                    "tags": new_tags,
                    "description": new_desc,
                })
                await prioritize_back_mockup(shop_id, pid)
                processed[pid] = now_iso()
                new_count += 1
                print(f"    ✅ SEO + thumbnail applied")
            except PrintifyError as e:
                print(f"    ❌ ERROR: {e}")
        else:
            new_count += 1

        await asyncio.sleep(1.0)

    if apply:
        save_seo_processed(processed)
        print(f"\n  ✅ {new_count} new listings processed.")
    else:
        print(f"\n  DRY RUN: {new_count} new listings would be processed.")


# ─── Summary Report ──────────────────────────────────────────────────────────

def print_summary(history: Dict):
    print("\n" + "=" * 65)
    print("MIDNIGHTROTATION AUTOPILOT — CATALOG HEALTH REPORT")
    print("=" * 65)
    print(f"  In Last Chance window:  {len(history['last_chance'])}")
    print(f"  Hibernated:             {len(history['hibernated'])}")
    print(f"  In Revival window:      {len(history['revival'])}")
    print(f"  Total deleted (all time): {len(history['deleted'])}")
    print("=" * 65)

    if history["last_chance"]:
        print("\n  ⚠️  ACTION REQUIRED — Apply 30% markdown on Etsy for:")
        for pid, data in history["last_chance"].items():
            print(f"    - {data['title'][:60]}")

    if history["revival"]:
        print("\n  👁  WATCHING — Revival window active for:")
        for pid, data in history["revival"].items():
            days_left = REVIVAL_DAYS - days_since(data["revived_at"])
            print(f"    - {data['title'][:50]} ({max(0, days_left)} days left)")


# ─── Main ────────────────────────────────────────────────────────────────────

async def main(
    csv_path: Optional[str],
    apply: bool,
    new_listings_only: bool,
    revival_only: bool,
):
    client = AsyncIOMotorClient(os.environ["MONGO_URL"])
    db = client[os.environ["DB_NAME"]]
    settings = await db.settings.find_one({"id": "config"}, {"_id": 0}) or {}
    shop_id = settings.get("printify_shop_id")
    if not shop_id:
        print("No printify_shop_id in settings. Aborting.")
        client.close()
        return
    shop_id = int(shop_id)

    mode = "APPLY" if apply else "DRY RUN"
    print(f"\nMidnightRotation Autopilot — {mode}")
    print(f"Shop ID: {shop_id}")
    print(f"Time: {now_iso()}\n")

    history = load_history()

    # New listing pipeline — runs regardless of CSV
    await run_new_listing_pipeline(shop_id, apply)

    if new_listings_only:
        print_summary(history)
        client.close()
        return

    # Revival check — runs regardless of CSV
    await run_revival(shop_id, history, apply)

    if revival_only:
        print_summary(history)
        client.close()
        return

    # CSV-dependent stages
    if not csv_path:
        print("\nNo CSV provided — skipping Last Chance and Delete stages.")
        print("Export your Etsy Ads CSV and run:")
        print("  python autopilot.py --csv your_export.csv")
        print_summary(history)
        client.close()
        return

    listings = parse_etsy_csv(csv_path)
    print(f"Loaded {len(listings)} listings from CSV.")

    # Fetch all Printify products for matching
    all_products = []
    page = 1
    while True:
        try:
            result = await list_products(shop_id, page=page, limit=50)
        except PrintifyError as e:
            print(f"ERROR fetching products: {e}")
            break
        batch = result.get("data") or []
        if not batch:
            break
        all_products.extend(batch)
        if len(batch) < 50:
            break
        page += 1

    await run_last_chance(listings, all_products, history, apply)
    await run_hibernation(listings, all_products, shop_id, history, apply)
    await run_permanent_delete(listings, shop_id, history, apply)

    print_summary(history)
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="MidnightRotation Autopilot System")
    parser.add_argument("--csv", default=None, help="Path to Etsy Ads CSV export")
    parser.add_argument("--apply", action="store_true", help="Apply changes (default is dry-run)")
    parser.add_argument("--new-listings-only", action="store_true", help="Only run new listing SEO pipeline")
    parser.add_argument("--revival-only", action="store_true", help="Only check revival windows")
    args = parser.parse_args()
    asyncio.run(main(
        csv_path=args.csv,
        apply=args.apply,
        new_listings_only=args.new_listings_only,
        revival_only=args.revival_only,
    ))
