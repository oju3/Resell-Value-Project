#!/usr/bin/env python3
"""Recurring job: refreshes a rotating subset of sneakers' eBay sold comps via
the caffein.dev/ebay-sold-listings Apify actor, keeping sold_comps current
after the one-time backfill (scripts/apify_backfill.py). Reuses that script's
filtering/parsing/write logic from scripts/comp_pipeline.py rather than
reimplementing it.

THIS IS NOT THE BACKFILL. It does not skip sneakers that already have rows —
refreshing existing sneakers is the entire point. ON CONFLICT (item_id) DO
NOTHING in comp_pipeline.write_comps prevents double-counting listings
already in sold_comps.

Sneaker selection: each run picks the SNEAKERS_PER_RUN sneakers whose most
recent sold_comps.scraped_at is oldest (sneakers with zero comps sort first,
as maximally stale). No separate last_refreshed_at column on sneakers is
used for this — scraped_at only advances when a row is actually written, so
a sneaker that fails this run (actor error, DB error) simply stays stale and
is picked up again next run. Adding a column updated regardless of outcome
would need to replicate that "only on success" semantics by hand and would
be a second source of truth that can drift from sold_comps; the existing
column already gives the right behavior for free.

Stall detection: a sneaker whose actor call succeeds but yields zero newly-
inserted rows (e.g. every returned comp is already in sold_comps under a
different sneaker_id — see the cross-colourway contamination BLOCKED item in
docs/comp_filtering_spec.md) never advances its scraped_at, so it can be
re-selected every run, burning budget without the catalogue rotating
forward. This job cannot fix that (the underlying keyword-matching issue is
BLOCKED for Phase 3) but it does detect and surface it: any sneaker with
written == 0 this run is logged to comp_rejections as rejection_rule =
'stalled_no_new_rows' (item_id/title NULL — it's a per-run marker, not a
per-comp rejection) and reported in the run summary. If the same sneaker's
previous 'stalled_no_new_rows' marker has no successful sold_comps write
after it, this run's stall is flagged as CONSECUTIVE and printed loudly in
the summary so it doesn't go unnoticed. See docs/refresh_schedule.md.

Does NOT save raw actor responses to data/raw_comps/ — that directory exists
for the one-time backfill's --from-cache re-filtering replay. This job has
no equivalent replay use case, and persisting every run's raw responses
would grow that directory unboundedly for a job that's meant to run weekly
or daily indefinitely.

Never prints, logs, or echoes APIFY_TOKEN or DATABASE_URL. Any URL that gets
logged has the token query param redacted first (via comp_pipeline).

Usage:
    python3 scripts/refresh_comps.py

Exits non-zero if the run fails entirely (sneakers were selected but none
were successfully refreshed), so Railway surfaces it as a failed job.
"""
import os
import sys
import time
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

from comp_pipeline import MIN_PRICE_BY_TIER, call_actor, filter_comp, log_rejections, write_comps

ROOT = Path(__file__).resolve().parent.parent

# --- Config: edit these to change cadence/volume, not the logic below. ---

# Sneakers refreshed per run. At 13/run the 50-sneaker catalogue cycles
# roughly monthly. See docs/refresh_schedule.md for the budget arithmetic.
SNEAKERS_PER_RUN = 13

# Comps requested per sneaker per actor call.
COMPS_PER_SNEAKER = 20

# Hard ceiling on actor calls this run, above SNEAKERS_PER_RUN as a runaway
# guard (e.g. against a misconfiguration that raises SNEAKERS_PER_RUN
# without raising this). Not meant to bind in normal operation.
MAX_CALLS_PER_RUN = 15

# Shorter than the backfill's 90 days -- this tops up recent sales, it does
# not rebuild history.
DAYS_TO_SCRAPE = 30

# Apify actor cost at ~20 comps/call. The Apify cost API (comp_pipeline.
# fetch_run_cost) is unreliable in practice, so the run summary estimates
# cost from this known rate instead of querying it.
ESTIMATED_COST_PER_CALL_USD = 0.08

# Matches apify_backfill.py; avoids hammering the actor between calls.
SLEEP_BETWEEN_CALLS_SECONDS = 2

# --- End config. ---

STALL_RULE = "stalled_no_new_rows"


class Stats:
    def __init__(self):
        self.actor_calls = 0
        self.sneakers_refreshed = 0
        self.rows_written = 0
        self.rows_rejected = 0
        self.conflict_skips = 0
        self.stalled_sneakers = []
        self.consecutive_stall_sneakers = []


def select_sneakers_to_refresh(conn, limit):
    """Picks the `limit` sneakers whose most recent sold_comps.scraped_at is
    oldest. LEFT JOIN (not INNER) so a sneaker with zero sold_comps rows
    sorts first, as maximally stale, rather than being excluded."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT s.id, s.name, s.style_code, s.hype_tier
            FROM sneakers s
            LEFT JOIN (
                SELECT sneaker_id, MAX(scraped_at) AS last_scraped
                FROM sold_comps
                GROUP BY sneaker_id
            ) sc ON sc.sneaker_id = s.id
            ORDER BY sc.last_scraped ASC NULLS FIRST, s.id
            LIMIT %s
            """,
            (limit,),
        )
        return cur.fetchall()


def previous_stall_at(conn, sneaker_id):
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT rejected_at FROM comp_rejections
            WHERE sneaker_id = %s AND rejection_rule = %s
            ORDER BY rejected_at DESC
            LIMIT 1
            """,
            (sneaker_id, STALL_RULE),
        )
        row = cur.fetchone()
        return row[0] if row else None


def has_write_since(conn, sneaker_id, since):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM sold_comps WHERE sneaker_id = %s AND scraped_at > %s LIMIT 1",
            (sneaker_id, since),
        )
        return cur.fetchone() is not None


def log_stall(conn, sneaker_id):
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO comp_rejections (sneaker_id, item_id, title, rejection_rule) VALUES (%s, NULL, NULL, %s)",
            (sneaker_id, STALL_RULE),
        )


def process_sneaker(conn, token, sneaker, stats):
    sneaker_id, name, style_code, hype_tier = sneaker

    min_price = MIN_PRICE_BY_TIER.get(hype_tier)
    if min_price is None:
        print(f"[{name}] SKIPPED — hype_tier={hype_tier!r} has no min_price mapping (expected 1, 2, or 3)")
        return "skipped_no_tier"

    if stats.actor_calls >= MAX_CALLS_PER_RUN:
        return "would_exceed_max_calls"

    if stats.actor_calls > 0:
        time.sleep(SLEEP_BETWEEN_CALLS_SECONDS)

    query = f"{name} {style_code}"
    try:
        items = call_actor(token, query, min_price, COMPS_PER_SNEAKER, DAYS_TO_SCRAPE)
    except Exception as e:
        print(f"[{name}] ACTOR ERROR — {e}", file=sys.stderr)
        return "actor_error"

    stats.actor_calls += 1

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

    try:
        written, skipped_item_ids = write_comps(conn, sneaker_id, accepted_rows)
        log_rejections(conn, sneaker_id, rejected_items)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[{name}] DB WRITE ERROR — {e}", file=sys.stderr)
        return "db_error"

    print(
        f"[{name}] tier={hype_tier} raw={len(items)} new_inserted={written} "
        f"on_conflict_skipped={len(skipped_item_ids)}"
    )
    for rule, n in sorted(rejections_by_rule.items()):
        print(f"    rejected[{rule}]: {n}")

    if written == 0:
        prev_stall = previous_stall_at(conn, sneaker_id)
        consecutive = prev_stall is not None and not has_write_since(conn, sneaker_id, prev_stall)
        try:
            log_stall(conn, sneaker_id)
            conn.commit()
        except Exception as e:
            conn.rollback()
            print(f"[{name}] DB WRITE ERROR (stall marker) — {e}", file=sys.stderr)
        stats.stalled_sneakers.append(name)
        if consecutive:
            stats.consecutive_stall_sneakers.append(name)
            print(
                f"    !!! CONSECUTIVE STALL — {name} has produced zero new rows across "
                f"multiple runs with no successful write in between. Likely cross-colourway "
                f"contamination (docs/comp_filtering_spec.md, 'Cross-colourway keyword "
                f"contamination — BLOCKED'). Investigate before it burns further budget. !!!"
            )
        else:
            print(f"    STALLED — 0 new rows this run (raw={len(items)}); logged as {STALL_RULE}")

    stats.rows_written += written
    stats.rows_rejected += len(rejected_items)
    stats.conflict_skips += len(skipped_item_ids)

    return "processed"


def main():
    load_dotenv(ROOT / ".env")
    token = os.environ.get("APIFY_TOKEN")
    db_url = os.environ.get("DATABASE_URL")
    if not token:
        print("APIFY_TOKEN not found in .env", file=sys.stderr)
        sys.exit(1)
    if not db_url:
        print("DATABASE_URL not found in .env", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    conn.autocommit = False

    stats = Stats()
    sneakers = select_sneakers_to_refresh(conn, SNEAKERS_PER_RUN)

    for sneaker in sneakers:
        result = process_sneaker(conn, token, sneaker, stats)
        if result == "would_exceed_max_calls":
            print(f"MAX_CALLS_PER_RUN ({MAX_CALLS_PER_RUN}) reached — aborting before the next actor call.")
            break
        if result == "processed":
            stats.sneakers_refreshed += 1

    estimated_cost = stats.actor_calls * ESTIMATED_COST_PER_CALL_USD

    print("\n--- Summary ---")
    print(f"Sneakers selected: {len(sneakers)}")
    print(f"Sneakers refreshed: {stats.sneakers_refreshed}")
    print(f"Actor calls made: {stats.actor_calls}")
    print(f"New rows inserted: {stats.rows_written}")
    print(f"Rows rejected: {stats.rows_rejected}")
    print(f"ON CONFLICT skips: {stats.conflict_skips}")
    print(f"Estimated cost: ${estimated_cost:.2f} ({stats.actor_calls} calls x ${ESTIMATED_COST_PER_CALL_USD:.2f}/call, estimate only)")
    print(f"Sneakers stalled (0 new rows): {len(stats.stalled_sneakers)}")
    if stats.stalled_sneakers:
        for name in stats.stalled_sneakers:
            print(f"  - {name}")
    if stats.consecutive_stall_sneakers:
        print(f"\n!!! {len(stats.consecutive_stall_sneakers)} sneaker(s) stalled on CONSECUTIVE runs — see warnings above !!!")
        for name in stats.consecutive_stall_sneakers:
            print(f"  - {name}")

    conn.close()

    if sneakers and stats.sneakers_refreshed == 0:
        print("\nRun FAILED entirely — 0 of the selected sneakers were successfully refreshed.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"refresh_comps.py FAILED: {e}", file=sys.stderr)
        sys.exit(1)
