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

Every attempt (not just failures) is logged to refresh_runs — a table kept
separate from comp_rejections on purpose. comp_rejections is strictly
per-listing (one row per rejected comp, per docs/comp_filtering_spec.md's
Auditing section); refresh_runs is per-sneaker-per-attempt bookkeeping for
this job, including outcome = 'ok' runs, which is what makes spend-per-sneaker
analysis possible later, not just failure forensics. See
docs/refresh_schedule.md.

Zero-write outcomes: a sneaker whose actor call succeeds but yields zero
newly-inserted rows never advances its scraped_at, so it can be re-selected
every run, burning budget without the catalogue rotating forward. That much
is cause-agnostic — but the cause itself is not one thing, and lumping it
into a single generic 'stalled' outcome (as an earlier version of this job
did) would repeat the exact mistake refresh_runs was split out of
comp_rejections to avoid: a reader having to cross-check raw_returned /
on_conflict_skipped to know what the row actually means. So outcome states
its own cause directly:

- 'no_listings_found' — raw_returned = 0. The actor found nothing in the
  30-day window. Nothing to be contaminated by; just a genuinely quiet
  30 days for that sneaker (see docs/refresh_schedule.md, "Raw-count
  variation is a velocity signal, not degradation").
- 'all_filtered' — raw_returned > 0 but every item was rejected by
  filter_comp (kids listings, bundles, etc.) — filtering working as
  intended, unrelated to contamination.
- 'no_new_sales' — every accepted row already existed in sold_comps under
  this SAME sneaker_id. The 30-day window overlapped a previous scrape;
  these are the same real sales already on file, not new ones. Expected,
  not a problem.
- 'cross_sneaker_conflict' — at least one accepted row already existed in
  sold_comps under a DIFFERENT sneaker_id (see the cross-colourway
  contamination BLOCKED item in docs/comp_filtering_spec.md). This is the
  one that's actually diagnostic of a data-quality problem.

This job cannot fix cross-sneaker conflicts (the underlying keyword-matching
issue is BLOCKED for Phase 3) but it does detect and surface them. Only
'cross_sneaker_conflict' triggers the consecutive-run check: if the
immediately preceding refresh_runs row for that sneaker was also
'cross_sneaker_conflict', this run is flagged as CONSECUTIVE and printed
loudly in the summary so it doesn't go unnoticed. The other three zero-write
outcomes are routine and never escalate.

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

from comp_pipeline import (
    MIN_PRICE_BY_TIER,
    call_actor,
    fetch_remaining_budget_usd,
    filter_comp,
    find_existing_owners,
    log_rejections,
    write_comps,
)

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

# Safety margin on the pre-run credit check below: abort before spending
# anything this run unless remaining Apify budget covers this multiple of
# the worst-case full-run cost, since ESTIMATED_COST_PER_CALL_USD is itself
# an estimate and actual per-call cost can vary.
CREDIT_CHECK_BUFFER = 1.2

# --- End config. ---


class Stats:
    def __init__(self):
        self.actor_calls = 0
        self.sneakers_refreshed = 0
        self.rows_written = 0
        self.rows_rejected = 0
        self.conflict_skips = 0
        self.no_progress_sneakers = []  # (name, outcome) — any zero-write outcome
        self.consecutive_conflict_sneakers = []  # cross_sneaker_conflict on back-to-back runs


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


def fetch_last_outcome(conn, sneaker_id):
    """Most recent refresh_runs.outcome for this sneaker, or None if it's
    never been attempted. Read before this run's row is inserted, so the
    consecutive-cross_sneaker_conflict check compares against the prior
    attempt, not itself."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT outcome FROM refresh_runs
            WHERE sneaker_id = %s
            ORDER BY run_at DESC
            LIMIT 1
            """,
            (sneaker_id,),
        )
        row = cur.fetchone()
        return row[0] if row else None


def log_run(conn, sneaker_id, raw_returned, new_rows_inserted, on_conflict_skipped,
            cross_sneaker_skips, outcome):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO refresh_runs
                (sneaker_id, raw_returned, new_rows_inserted, on_conflict_skipped,
                 cross_sneaker_skips, outcome)
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (sneaker_id, raw_returned, new_rows_inserted, on_conflict_skipped,
             cross_sneaker_skips, outcome),
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

    # Read before this run's own refresh_runs row is inserted, so the
    # consecutive-cross_sneaker_conflict check below compares against the
    # prior attempt, not itself.
    prev_outcome = fetch_last_outcome(conn, sneaker_id)

    query = f"{name} {style_code}"
    try:
        items = call_actor(token, query, min_price, COMPS_PER_SNEAKER, DAYS_TO_SCRAPE)
    except Exception as e:
        print(f"[{name}] ACTOR ERROR — {e}", file=sys.stderr)
        try:
            log_run(conn, sneaker_id, None, None, None, None, "actor_error")
            conn.commit()
        except Exception as log_e:
            conn.rollback()
            print(f"[{name}] DB WRITE ERROR (run log) — {log_e}", file=sys.stderr)
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
        skip_owners = find_existing_owners(conn, skipped_item_ids)
    except Exception as e:
        conn.rollback()
        print(f"[{name}] DB WRITE ERROR — {e}", file=sys.stderr)
        try:
            log_run(conn, sneaker_id, len(items), None, None, None, "db_error")
            conn.commit()
        except Exception as log_e:
            conn.rollback()
            print(f"[{name}] DB WRITE ERROR (run log) — {log_e}", file=sys.stderr)
        return "db_error"

    cross_sneaker_skips = sum(
        1 for item_id in skipped_item_ids
        if skip_owners.get(item_id) is not None and skip_owners[item_id] != sneaker_id
    )

    print(
        f"[{name}] tier={hype_tier} raw={len(items)} new_inserted={written} "
        f"on_conflict_skipped={len(skipped_item_ids)} cross_sneaker_skips={cross_sneaker_skips}"
    )
    for rule, n in sorted(rejections_by_rule.items()):
        print(f"    rejected[{rule}]: {n}")

    if written > 0:
        run_outcome = "ok"
    elif len(items) == 0:
        run_outcome = "no_listings_found"
    elif len(accepted_rows) == 0:
        run_outcome = "all_filtered"
    elif cross_sneaker_skips > 0:
        run_outcome = "cross_sneaker_conflict"
    else:
        run_outcome = "no_new_sales"

    try:
        log_run(conn, sneaker_id, len(items), written, len(skipped_item_ids),
                cross_sneaker_skips, run_outcome)
        conn.commit()
    except Exception as e:
        conn.rollback()
        print(f"[{name}] DB WRITE ERROR (run log) — {e}", file=sys.stderr)

    if run_outcome != "ok":
        stats.no_progress_sneakers.append((name, run_outcome))

        if run_outcome == "cross_sneaker_conflict":
            consecutive = prev_outcome == "cross_sneaker_conflict"
            if consecutive:
                stats.consecutive_conflict_sneakers.append(name)
                print(
                    f"    !!! CONSECUTIVE CROSS-SNEAKER CONFLICT — {name} has had comps already "
                    f"claimed by a different sneaker_id on back-to-back attempts. Likely "
                    f"cross-colourway contamination (docs/comp_filtering_spec.md, "
                    f"'Cross-colourway keyword contamination — BLOCKED'). Investigate before it "
                    f"burns further budget. !!!"
                )
            else:
                print(
                    f"    CROSS_SNEAKER_CONFLICT — {cross_sneaker_skips} of {len(skipped_item_ids)} "
                    f"skipped comp(s) already claimed by a different sneaker; logged to refresh_runs"
                )
        elif run_outcome == "no_listings_found":
            print(f"    NO_LISTINGS_FOUND — actor returned 0 comps in the {DAYS_TO_SCRAPE}-day "
                  f"window; expected for low-velocity inventory, not a data issue")
        elif run_outcome == "all_filtered":
            print(f"    ALL_FILTERED — raw={len(items)} but every item was rejected by filter_comp "
                  f"(kids/bundle noise); no comps to write, not a stall")
        elif run_outcome == "no_new_sales":
            print(f"    NO_NEW_SALES — {len(skipped_item_ids)} comp(s) already on file under this "
                  f"same sneaker (window overlap); no new sales this run, expected")

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

    # Pre-run credit check — see docs/refresh_schedule.md, "Known false
    # positive: mid-run credit exhaustion". Aborts before any actor calls
    # if remaining monthly budget can't cover this run's worst case, so a
    # run doesn't get truncated mid-way and log spurious zero-write outcomes.
    worst_case_cost = SNEAKERS_PER_RUN * ESTIMATED_COST_PER_CALL_USD * CREDIT_CHECK_BUFFER
    remaining_budget = fetch_remaining_budget_usd(token)
    if remaining_budget is None:
        print(
            "WARNING: could not determine remaining Apify budget (limits API call failed) — "
            "proceeding without a pre-run credit check.",
            file=sys.stderr,
        )
    elif remaining_budget < worst_case_cost:
        print(
            f"Aborting before any actor calls — remaining Apify budget (${remaining_budget:.2f}) "
            f"is below this run's buffered worst-case cost (${worst_case_cost:.2f} = "
            f"{SNEAKERS_PER_RUN} x ${ESTIMATED_COST_PER_CALL_USD:.2f} x {CREDIT_CHECK_BUFFER} buffer). "
            "See docs/refresh_schedule.md, 'Known false positive: mid-run credit exhaustion'.",
            file=sys.stderr,
        )
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
    print(f"Sneakers with no new rows this run: {len(stats.no_progress_sneakers)}")
    if stats.no_progress_sneakers:
        for name, outcome in stats.no_progress_sneakers:
            print(f"  - {name} ({outcome})")
    if stats.consecutive_conflict_sneakers:
        print(f"\n!!! {len(stats.consecutive_conflict_sneakers)} sneaker(s) had CONSECUTIVE cross_sneaker_conflict — see warnings above !!!")
        for name in stats.consecutive_conflict_sneakers:
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
