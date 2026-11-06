"""
cloud_scan_batch.py — Standalone scanner batch script for GitHub Actions (100% Free, No Card)
or Render / local cron jobs.
In a single run, this script:
  1. Pulls all 2,583 Rolimons v2 items + recent trade ads + live sale activity
  2. Seeds active traders from trade ads
  3. Scans `SCAN_BATCH_SIZE` (default 120) trader inventories via Rolimons & Roblox APIs
  4. Matches any UAID ownership swaps into verified trade proofs in `void_data.sqlite3`
  5. Prints a summary of proofs, traders, and UAIDs so you can check it in the GitHub Actions log
"""
import os
import time
from pathlib import Path

from data.roblox import RobloxClient
from data.rolimons import RolimonsClient
from db import Database
from tracker.scanner import InventoryScanner
from tracker.seed import seed_traders_from_ads

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "void_data.sqlite3"


def run_batch(batch_size: int = 120, delay_seconds: float = 0.8) -> None:
    db = Database(DB_PATH)
    rolimons = RolimonsClient(cache_ttl=300)
    roblox = RobloxClient(backoff_seconds=4.0)
    scanner = InventoryScanner(db=db, rolimons=rolimons, roblox=roblox, match_window_seconds=86400)

    print("=== [1/3] Fetching Rolimons v2 catalog, trade ads, and live sales ===")
    items = rolimons.fetch_items(force=True)
    if items:
        db.upsert_items(items)
    items_catalog = db.get_all_items()

    ads = rolimons.fetch_recent_ads()
    if ads:
        added_traders = seed_traders_from_ads(db, ads)
        print(f"Seeded/updated {added_traders} active traders from recent ads.")

    sale_acts = rolimons.fetch_sale_activity()
    if sale_acts:
        db.record_live_sales(sale_acts)

    print(f"=== [2/3] Scanning {batch_size} trader inventories ===")
    total_moves = 0
    total_new_trades = 0
    for i in range(batch_size):
        res = scanner.scan_one_step(items_catalog)
        total_moves += int(res.get("moves", 0))
        total_new_trades += len(res.get("new_trades", []))
        if (i + 1) % 25 == 0:
            print(
                f"  Scanned {i + 1}/{batch_size} traders | UAID moves found: {total_moves} | New proofs: {total_new_trades}"
            )
        time.sleep(delay_seconds)

    stats = db.get_stats()
    print("=== [3/3] Batch Complete — Current Database Stats ===")
    print(f"  Items indexed   : {stats['items']:,} ({stats['rolimons_items']:,} Rolimons)")
    print(f"  Tracked traders : {stats['traders']:,} ({stats['scanned_traders']:,} scanned)")
    print(f"  Tracked UAIDs   : {stats['tracked_uaids']:,}")
    print(f"  Verified Proofs : {stats['proofs']:,}")


if __name__ == "__main__":
    size = int(os.environ.get("SCAN_BATCH_SIZE", "120"))
    run_batch(batch_size=size)
