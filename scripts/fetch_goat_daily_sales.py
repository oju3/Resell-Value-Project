"""Populates goat_daily_sales with GOAT's pre-aggregated daily feed.

    GET https://api.kicks.dev/v3/goat/products/{goat_product_id}/sales/daily
    Authorization: Bearer {KICKS_API_KEY}

DIFFERENT ENDPOINT FROM fetch_goat_sales.py
-------------------------------------------
That script calls /sales -- individual transactions, one row per sale, written
to market_sales. This one calls /sales/daily -- GOAT's own daily rollup, one
row per DAY, written to goat_daily_sales. Different response shape entirely:
{product_id, avg_amount, orders, date} here versus
{product_id, type, size_us, currency, amount, location, purchased_at} there.
The two must not be conflated, and neither table is derived from the other.

avg_amount is already an average across that day's sales; there are no
per-sale rows in this feed. `orders` is the count that average was computed
from and is the per-day liquidity/confidence signal. Both are inserted as-is
and never recomputed.

NO GAP FILLING
--------------
The endpoint returns a row only for days with at least one sale, so a sparse
sneaker yields a gapped series -- confirmed on product_id 158522 (33 total
/sales transactions): 22 rows across a ~90 day window with real 5+ day gaps.
This script inserts exactly the rows returned and nothing else. It does not
generate rows for missing dates and does not interpolate avg_amount across
gaps. An absent date means no sale happened that day; inventing a row for it
would fabricate trading activity that did not occur.

DEDUPLICATION -- IDEMPOTENT, UNLIKE market_sales
------------------------------------------------
goat_daily_sales is UNIQUE (goat_product_id, sale_date) and this loader uses
ON CONFLICT DO UPDATE, so re-running refreshes each day's numbers in place.
A daily aggregate for a date is one fact that a fresher pull should overwrite;
there is no meaningful duplicate. ingested_at is bumped to now() on update so
the row records when it was last refreshed, not when it first landed.

PRODUCT_ID TYPE MISMATCH
------------------------
sneakers.goat_product_id is TEXT ("1293064"); the response returns product_id
as a JSON NUMBER (1293064). Python compares those as unequal without error, so
every entry would be dropped and every sneaker would report zero days -- a
silent failure looking like "GOAT has no daily data". Each entry's product_id
is str()-ed before comparison, and the TEXT value from the database is what
gets inserted.

CURRENCY
--------
goat_daily_sales has no currency column: every sale in the underlying /sales
endpoint was verified USD, and this feed aggregates those same sales. The
verified /sales/daily response carries no currency field at all. If one ever
appears with a non-USD value, this script STOPS and reports it rather than
silently assuming USD here too.

CACHING
-------
The full raw response body is written to
cache/kicksdb_goat_daily/{goat_product_id}.json BEFORE anything is parsed,
same discipline as the other two fetch scripts.

RATE LIMITING / ERRORS
----------------------
Same posture as fetch_goat_sales.py: sequential, no parallelism, no artificial
delay, no retry logic. 429 and 401/403 are hard stops; other HTTP errors are
logged per-sneaker and the loop continues.

BATCH ORDER
-----------
Run with --limit 3 first. Selection is ordered so FV5029-006 (Jordan 4 Bred
Reimagined, goat_product_id 1293064) is always in that first batch. Row count,
date range, calendar span and gap count are printed per sneaker so a sparse
series is immediately distinguishable from a consecutive one. Then re-run with
no --limit for the remaining 40 -- safe to re-run, see the dedup note above.

The 7 sneakers with no goat_product_id are out of scope entirely.

Never prints, logs, or echoes KICKS_API_KEY or DATABASE_URL. The key goes into
a request header and nowhere else; failures print the identifier and HTTP
status only.
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2
from psycopg2.extras import Json, execute_values
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "kicksdb_goat_daily"

GOAT_DAILY_SALES_URL_TEMPLATE = "https://api.kicks.dev/v3/goat/products/{}/sales/daily"

# Same first-batch check as the other two fetch scripts.
VERIFICATION_STYLE_CODE = "FV5029-006"

# The currency every underlying /sales row was verified to use. The daily feed
# carries no currency field; this is only used to recognise one if it appears.
EXPECTED_CURRENCY = "USD"

REQUEST_TIMEOUT_SECONDS = 30


def fetch_goat_daily_sales(goat_product_id, api_key):
    """Calls the daily-sales endpoint for one goat_product_id, caches raw body.

    Returns the response body as text. The file is written BEFORE this returns,
    so the raw response exists on disk before any caller parses it.

    Raises urllib.error.HTTPError to the caller, which decides what is fatal.
    """
    request = urllib.request.Request(
        GOAT_DAILY_SALES_URL_TEMPLATE.format(urllib.parse.quote(str(goat_product_id))),
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw_text = response.read().decode("utf-8")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{goat_product_id}.json").write_text(raw_text)

    return raw_text


def insert_daily_rows(conn, sneaker_id, goat_product_id, entries):
    """Inserts or refreshes one sneaker's daily rows. Returns rows written.

    ON CONFLICT (goat_product_id, sale_date) DO UPDATE: a re-run overwrites
    that day's figures instead of duplicating. ingested_at is reset to now() so
    it means "last refreshed", which is the useful reading for a row that can
    be updated in place.

    raw_response stores the individual daily object -- not the full envelope,
    which is already on disk in CACHE_DIR -- so any field not mapped to a
    column survives.

    Inserts exactly what it is given. No row is generated for a missing date.
    """
    if not entries:
        return 0

    rows = [
        (
            sneaker_id,
            goat_product_id,        # the TEXT value from sneakers, not the JSON number
            entry.get("date"),
            entry.get("avg_amount"),
            entry.get("orders"),
            Json(entry),
        )
        for entry in entries
    ]

    with conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO goat_daily_sales
                    (sneaker_id, goat_product_id, sale_date, avg_amount, orders, raw_response)
                VALUES %s
                ON CONFLICT (goat_product_id, sale_date) DO UPDATE SET
                    sneaker_id   = EXCLUDED.sneaker_id,
                    avg_amount   = EXCLUDED.avg_amount,
                    orders       = EXCLUDED.orders,
                    raw_response = EXCLUDED.raw_response,
                    ingested_at  = now();
                """,
                rows,
            )
    return len(rows)


def select_sneakers(conn, limit):
    """Sneakers with a populated goat_product_id, verification SKU first.

    goat_product_id IS NOT NULL is the whole scope filter: the 7 sneakers
    without one are excluded here and nowhere else, so there is one place to
    look for why a sneaker was skipped.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, style_code, name, goat_product_id
            FROM sneakers
            WHERE goat_product_id IS NOT NULL
            ORDER BY (style_code = %s) DESC, id
            LIMIT %s;
            """,
            (VERIFICATION_STYLE_CODE, limit),
        )
        return cur.fetchall()


def main():
    parser = argparse.ArgumentParser(
        description="Populate goat_daily_sales from KicksDB's daily feed. "
                    "Run with --limit 3 first and check the printed date ranges."
    )
    parser.add_argument("--limit", type=int, default=None,
                        help="Process only the first N sneakers (verification SKU always included).")
    args = parser.parse_args()

    load_dotenv(ROOT / ".env")
    api_key = os.environ.get("KICKS_API_KEY")
    db_url = os.environ.get("DATABASE_URL")
    if not api_key:
        print("KICKS_API_KEY not found in .env", file=sys.stderr)
        sys.exit(1)
    if not db_url:
        print("DATABASE_URL not found in .env", file=sys.stderr)
        sys.exit(1)

    conn = psycopg2.connect(db_url)
    sneakers = select_sneakers(conn, args.limit)
    print(f"Processing {len(sneakers)} sneaker(s) with a goat_product_id. Cache: {CACHE_DIR}\n")

    total_rows = 0
    no_days = []
    for sneaker_id, style_code, name, goat_product_id in sneakers:
        try:
            raw_text = fetch_goat_daily_sales(goat_product_id, api_key)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                print(f"\n429 rate limited on {style_code} (goat {goat_product_id}). Stopping — "
                      f"no throttling is designed into this script. Re-run once the limit resets, "
                      f"or add a delay based on the observed Retry-After.", file=sys.stderr)
                conn.close()
                sys.exit(1)
            if e.code in (401, 403):
                print(f"\nHTTP {e.code} on {style_code}: KICKS_API_KEY rejected. Stopping.",
                      file=sys.stderr)
                conn.close()
                sys.exit(1)
            print(f"  [{style_code}] HTTP {e.code} — skipped, no rows inserted")
            continue
        except urllib.error.URLError as e:
            print(f"  [{style_code}] request failed ({e.reason}) — skipped, no rows inserted")
            continue

        payload = json.loads(raw_text)

        # Pagination guard, same as fetch_goat_sales.py. The verified sample had
        # "meta": null. Anything else may carry a cursor, and silently keeping
        # page one while reporting success is the failure this stops.
        meta = payload.get("meta")
        if meta:
            print(f"\n[{style_code}] meta is not empty on a real response: {meta!r}\n"
                  f"This script does not page. Stopping so paging can be added deliberately "
                  f"rather than silently capturing only the first page.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        entries = payload.get("data") or []

        # The daily feed carries no currency field. If one ever appears with a
        # non-USD value, the no-currency-column assumption in goat_daily_sales
        # is wrong and this stops rather than storing an unlabelled figure.
        for entry in entries:
            currency = entry.get("currency")
            if currency is not None and currency != EXPECTED_CURRENCY:
                print(f"\n[{style_code}] daily response carries currency={currency!r}, "
                      f"not {EXPECTED_CURRENCY}. goat_daily_sales has no currency column because "
                      f"every underlying sale was verified USD. Stopping rather than storing an "
                      f"amount whose currency is unrecorded.", file=sys.stderr)
                conn.close()
                sys.exit(1)

        # str() on the response's numeric product_id before comparing to the
        # TEXT column -- see the module docstring.
        matched = []
        mismatched = 0
        for entry in entries:
            if str(entry.get("product_id")) != str(goat_product_id):
                mismatched += 1
                continue
            matched.append(entry)

        if mismatched:
            print(f"  [{style_code}] {mismatched} entr(ies) had a different product_id — dropped")

        written = insert_daily_rows(conn, sneaker_id, goat_product_id, matched)
        total_rows += written

        if written == 0:
            no_days.append(style_code)
            print(f"  [{style_code}] goat {goat_product_id}: 0 daily row(s)  ({name})")
            continue

        # Date range plus span/gaps, so a sparse series is visible at a glance
        # rather than being mistaken for a consecutive one.
        dates = sorted(e["date"] for e in matched if e.get("date"))
        first, last = dates[0], dates[-1]
        span_days = (
            __import__("datetime").date.fromisoformat(last)
            - __import__("datetime").date.fromisoformat(first)
        ).days + 1
        gaps = span_days - len(dates)
        print(f"  [{style_code}] goat {goat_product_id}: {written} daily row(s), "
              f"{first} → {last} ({span_days} calendar days, {gaps} with no sale)  ({name})")

    print(f"\nDone. Inserted/refreshed {total_rows} row(s) across {len(sneakers)} sneaker(s).")
    if no_days:
        print(f"No daily rows returned ({len(no_days)}): {', '.join(no_days)}")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT sneaker_id), MIN(sale_date), MAX(sale_date) "
                    "FROM goat_daily_sales;")
        rows, sneaker_count, min_d, max_d = cur.fetchone()
        print(f"goat_daily_sales now holds {rows} row(s) across {sneaker_count} sneaker(s), "
              f"{min_d} → {max_d}.")

    conn.close()


if __name__ == "__main__":
    main()
