#!/usr/bin/env python3
"""One-time backfill: pulls up to 90 days of eBay sold comps per sneaker via
the caffein.dev/ebay-sold-listings Apify actor, applies the rules in
docs/comp_filtering_spec.md, and writes survivors to sold_comps (rejects go
to comp_rejections with the rule that rejected them).

THIS IS A ONE-TIME BACKFILL, NOT THE RECURRING CRON JOB.

WARNING: --dry-run still calls the Apify actor and spends Apify credit. The
actor call happens regardless of --dry-run; only the database write (to
sold_comps / comp_rejections) is skipped.

itemCondition is sent as "new" on every call, exactly as given in the
actor's verified input schema — it is not varied per sneaker or tier.
Because of that, conditionId 3000 (Pre-Owned) is expected to be rare-to-
absent from the results in practice. The Condition rule's handling of 3000
in this script is a defensive backstop for any mistagged or imperfectly
filtered listings the actor lets through, not a primary data source — the
same non-authoritative-prefilter role docs/comp_filtering_spec.md assigns
to minPrice.

Never prints, logs, or echoes APIFY_TOKEN or DATABASE_URL. Any URL that
gets logged has the token query param redacted first.

Usage:
    python3 scripts/apify_backfill.py [--limit N] [--count N] [--dry-run]
                                       [--force] [--max-calls N] [--from-cache]

--from-cache reads previously saved responses from data/raw_comps/ instead of
calling the actor at all — no APIFY_TOKEN required, no Apify credit spent.
Lets the filter/aggregation logic be exercised for free against real data.
"""
import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from comp_pipeline import (
    MIN_PRICE_BY_TIER,
    call_actor,
    fetch_run_cost,
    filter_comp,
    find_existing_owners,
    log_rejections,
    write_comps,
)

ROOT = Path(__file__).resolve().parent.parent
RAW_COMPS_DIR = ROOT / "data" / "raw_comps"

DAYS_TO_SCRAPE = 90

# Not specified in the spec; chosen as a conservative default to avoid
# hammering the actor between per-sneaker calls.
SLEEP_BETWEEN_CALLS_SECONDS = 2


class Stats:
    def __init__(self):
        self.actor_calls = 0
        self.total_cost = 0.0
        self.cost_fully_known = True
        self.rows_written = 0
        self.rows_rejected = 0
        self.conflict_skips = 0
        self.low_confidence_sneakers = []


def fetch_sneakers(conn, limit):
    sql = "SELECT id, name, style_code, hype_tier FROM sneakers ORDER BY id"
    params = ()
    if limit:
        sql += " LIMIT %s"
        params = (limit,)
    with conn.cursor() as cur:
        cur.execute(sql, params)
        return cur.fetchall()


def has_existing_comps(conn, sneaker_id):
    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM sold_comps WHERE sneaker_id = %s LIMIT 1", (sneaker_id,))
        return cur.fetchone() is not None


def save_raw_response(style_code, items):
    RAW_COMPS_DIR.mkdir(parents=True, exist_ok=True)
    path = RAW_COMPS_DIR / f"{style_code}.json"
    path.write_text(json.dumps(items, indent=2))
    return path


def load_cached_response(style_code):
    path = RAW_COMPS_DIR / f"{style_code}.json"
    if not path.exists():
        return None
    return json.loads(path.read_text())


def process_sneaker(conn, token, sneaker, args, stats):
    sneaker_id, name, style_code, hype_tier = sneaker

    min_price = MIN_PRICE_BY_TIER.get(hype_tier)
    if min_price is None:
        print(f"[{name}] SKIPPED — hype_tier={hype_tier!r} has no min_price mapping (expected 1, 2, or 3)")
        return "skipped_no_tier"

    if not args.force and has_existing_comps(conn, sneaker_id):
        print(f"[{name}] SKIPPED — sold_comps rows already exist (use --force to reprocess)")
        return "skipped_existing"

    if args.from_cache:
        items = load_cached_response(style_code)
        if items is None:
            print(f"[{name}] SKIPPED — no cached response at data/raw_comps/{style_code}.json")
            return "skipped_no_cache"
        cost_display = "n/a (--from-cache, no actor call)"
    else:
        if stats.actor_calls >= args.max_calls:
            return "would_exceed_max_calls"

        if stats.actor_calls > 0:
            time.sleep(SLEEP_BETWEEN_CALLS_SECONDS)

        query = f"{name} {style_code}"
        try:
            items = call_actor(token, query, min_price, args.count, DAYS_TO_SCRAPE)
        except Exception as e:
            print(f"[{name}] ACTOR ERROR — {e}", file=sys.stderr)
            return "actor_error"

        stats.actor_calls += 1
        cost = fetch_run_cost(token)
        if cost is not None:
            stats.total_cost += cost
            cost_display = f"${stats.total_cost:.4f}"
        else:
            stats.cost_fully_known = False
            cost_display = f"${stats.total_cost:.4f} (partial — some calls' cost unknown)"

        save_raw_response(style_code, items)

    seen_thumbnails = set()
    accepted_rows = []
    rejected_items = []
    rejections_by_rule = {}
    for item in items:
        outcome, result = filter_comp(item, seen_thumbnails)
        if outcome == "accept":
            accepted_rows.append(result)
        else:
            rejected_items.append((item, result))
            rejections_by_rule[result] = rejections_by_rule.get(result, 0) + 1

    written = len(accepted_rows)
    skipped_item_ids = []
    skip_owners = {}
    if not args.dry_run:
        try:
            written, skipped_item_ids = write_comps(conn, sneaker_id, accepted_rows)
            log_rejections(conn, sneaker_id, rejected_items)
            conn.commit()
            skip_owners = find_existing_owners(conn, skipped_item_ids)
        except Exception as e:
            conn.rollback()
            print(f"[{name}] DB WRITE ERROR — {e}", file=sys.stderr)
            return "db_error"

    survivor_count = len(accepted_rows)
    low_confidence = survivor_count < 5

    condition_counts = {}
    for r in accepted_rows:
        condition_counts[r["condition_id"]] = condition_counts.get(r["condition_id"], 0) + 1

    all_prices = [r["sold_price"] for r in accepted_rows]
    deadstock_prices = [r["sold_price"] for r in accepted_rows if r["condition_id"] == 1000]
    all_median = statistics.median(all_prices) if all_prices else None
    deadstock_median = statistics.median(deadstock_prices) if deadstock_prices else None
    all_median_display = f"${all_median:.2f}" if all_median is not None else "n/a"
    deadstock_median_display = f"${deadstock_median:.2f}" if deadstock_median is not None else "n/a"

    print(
        f"[{name}] tier={hype_tier} raw={len(items)} surviving={survivor_count} "
        f"written={written}{' (dry-run, not persisted)' if args.dry_run else ''} "
        f"low_confidence={low_confidence}"
    )
    print(f"    deadstock_median (conditionId=1000 only, n={len(deadstock_prices)}): {deadstock_median_display}")
    print(f"    all_conditions_median (mixed conditionId, n={len(all_prices)}): {all_median_display}")
    if skipped_item_ids:
        print(f"    ON CONFLICT skips: {len(skipped_item_ids)} (surviving={survivor_count} but only {written} newly inserted)")
        for item_id in skipped_item_ids:
            owner = skip_owners.get(item_id)
            if owner is not None and owner != sneaker_id:
                print(f"      item_id={item_id} already in sold_comps under sneaker_id={owner} (cross-sneaker duplicate listing)")
            else:
                print(f"      item_id={item_id} already in sold_comps under this sneaker (rerun)")
    print(
        "    surviving conditionId breakdown: "
        f"1000={condition_counts.get(1000, 0)} "
        f"1500={condition_counts.get(1500, 0)} "
        f"3000={condition_counts.get(3000, 0)}"
    )
    for rule, n in sorted(rejections_by_rule.items()):
        print(f"    rejected[{rule}]: {n}")
    print(f"    running total: {stats.actor_calls} actor calls, cost {cost_display}")

    stats.rows_written += written
    stats.rows_rejected += len(rejected_items)
    stats.conflict_skips += len(skipped_item_ids)
    if low_confidence:
        stats.low_confidence_sneakers.append(name)

    return "processed"


def main():
    parser = argparse.ArgumentParser(
        description=(
            "One-time backfill of eBay sold comps into sold_comps via the Apify "
            "caffein.dev/ebay-sold-listings actor. This is NOT the recurring cron job. "
            "WARNING: --dry-run still calls the actor and spends Apify credit — "
            "it only skips the database write."
        )
    )
    parser.add_argument("--limit", type=int, default=None, help="Process only the first N sneakers (default: all)")
    parser.add_argument("--count", type=int, default=20, help="Comps requested per sneaker (default: 20)")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Call the actor and report results; write nothing to the DB. "
             "The actor call still happens and still costs Apify credit.",
    )
    parser.add_argument("--force", action="store_true", help="Reprocess sneakers that already have sold_comps rows")
    parser.add_argument(
        "--max-calls", type=int, default=50,
        help="Hard ceiling on actor calls this run; abort before exceeding it (default: 50)",
    )
    parser.add_argument(
        "--from-cache", action="store_true",
        help="Read previously saved responses from data/raw_comps/ instead of calling the "
             "actor. No APIFY_TOKEN needed, no Apify credit spent. Lets filter/aggregation "
             "logic be exercised for free; skips sneakers with no cached file.",
    )
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    token = None if args.from_cache else os.environ.get("APIFY_TOKEN")
    db_url = os.environ.get("DATABASE_URL")
    if not args.from_cache and not token:
        print("APIFY_TOKEN not found in .env", file=sys.stderr)
        sys.exit(1)
    if not db_url:
        print("DATABASE_URL not found in .env", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    stats = Stats()
    sneakers = fetch_sneakers(conn, args.limit)

    for sneaker in sneakers:
        result = process_sneaker(conn, token, sneaker, args, stats)
        if result == "would_exceed_max_calls":
            print(f"--max-calls ({args.max_calls}) reached — aborting before the next actor call.")
            break

    cost_note = "" if stats.cost_fully_known else " (partial — some calls' cost unknown)"
    print("\n--- Summary ---")
    print(f"Actor calls made: {stats.actor_calls}")
    print(f"Total cost: ${stats.total_cost:.4f}{cost_note}")
    print(f"Rows written: {stats.rows_written}{' (dry-run, not persisted)' if args.dry_run else ''}")
    print(f"Rows rejected: {stats.rows_rejected}")
    print(f"ON CONFLICT skips (surviving rows not newly inserted): {stats.conflict_skips}")
    print(f"Sneakers flagged low_confidence: {len(stats.low_confidence_sneakers)}")
    if stats.low_confidence_sneakers:
        for name in stats.low_confidence_sneakers:
            print(f"  - {name}")

    conn.close()


if __name__ == "__main__":
    main()
