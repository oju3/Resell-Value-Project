"""Populates market_sales with real GOAT sold transactions from KicksDB.

    GET https://api.kicks.dev/v3/goat/products/{goat_product_id}/sales
    Authorization: Bearer {KICKS_API_KEY}

Runs over every sneaker with a non-null goat_product_id. The 7 sneakers whose
goat_product_id is NULL (unresolved SKU mismatches) are out of scope entirely
-- this script does not attempt to look them up.

PRODUCT_ID TYPE MISMATCH
------------------------
sneakers.goat_product_id is TEXT ("1293064"). The sales response returns
product_id as a JSON NUMBER (1293064). Python compares those as unequal
without error, so every entry would be skipped and every sneaker would report
zero sales -- a silent failure that looks like "GOAT has no sales data." Each
entry's product_id is therefore str()-ed before comparison, and the TEXT value
from the database is what gets inserted.

PAGINATION -- NOT HANDLED, DETECTED
-----------------------------------
The one verified response returned a flat data[] array with "meta": null, and
that is all this script assumes. It does NOT assume that holds everywhere: if
meta ever comes back non-empty on a real response, the script STOPS and prints
the meta it saw, rather than silently capturing page one and reporting success.
Add paging deliberately once there is a real cursor to page on.

CACHING
-------
The full raw response body is written to
cache/kicksdb_goat_sales/{goat_product_id}.json BEFORE anything is parsed, same
discipline as fetch_goat_product_ids.py.

CURRENCY
--------
amount and currency are inserted exactly as returned. No conversion, no
normalization, no assumption that USD is universal. Every observed sale so far
has returned "USD"; if a non-USD value appears it is stored as-is and stays
visible rather than being quietly converted into a wrong number.

PURCHASE TYPE
-------------
Every entry returned is inserted, whatever its `type` value, into
purchase_type. The verified sample only contained PURCHASE_TYPE_SALE. No
filter is applied because none was specified -- if the endpoint also returns
asks/bids/other types, they land too and the column makes them separable.
Worth checking on the first batch.

KNOWN GAP -- NO DEDUPLICATION
-----------------------------
market_sales has no unique constraint and this script has no ON CONFLICT
handling. RE-RUNNING THIS SCRIPT INSERTS DUPLICATE ROWS. Accepted for a first
pass, stated here so it is visible.

RATE LIMITING / ERRORS
----------------------
Same posture as fetch_goat_product_ids.py: sequential, no parallelism, no
artificial delay, no retry logic. 429 and 401/403 are hard stops; other HTTP
errors are logged per-sneaker and the loop continues.

BATCH ORDER
-----------
Run with --limit 3 first. Selection is ordered so FV5029-006 (Jordan 4 Bred
Reimagined, goat_product_id 1293064) is always in that first batch. Row counts
are printed per sneaker. Then re-run with no --limit for the rest -- note that
re-running re-inserts the first 3 (see the dedup gap above).

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
CACHE_DIR = ROOT / "cache" / "kicksdb_goat_sales"

GOAT_SALES_URL_TEMPLATE = "https://api.kicks.dev/v3/goat/products/{}/sales"

# Same first-batch check as fetch_goat_product_ids.py.
VERIFICATION_STYLE_CODE = "FV5029-006"

REQUEST_TIMEOUT_SECONDS = 30


def fetch_goat_sales(goat_product_id, api_key):
    """Calls the sales endpoint for one goat_product_id and caches the raw body.

    Returns the response body as text. The file is written BEFORE this returns,
    so the raw response exists on disk before any caller parses it.

    Raises urllib.error.HTTPError to the caller, which decides what is fatal.
    """
    request = urllib.request.Request(
        GOAT_SALES_URL_TEMPLATE.format(urllib.parse.quote(str(goat_product_id))),
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw_text = response.read().decode("utf-8")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{goat_product_id}.json").write_text(raw_text)

    return raw_text


def insert_sales(conn, sneaker_id, goat_product_id, entries):
    """Inserts one sneaker's sale rows. Returns the number of rows written.

    No ON CONFLICT clause: market_sales has no unique constraint, so this
    appends unconditionally and a re-run duplicates. See the dedup gap in the
    module docstring.

    raw_response stores the individual sale object -- not the full envelope,
    which is already on disk in CACHE_DIR -- so any field not mapped to a
    column survives.
    """
    if not entries:
        return 0

    rows = [
        (
            sneaker_id,
            goat_product_id,          # the TEXT value from sneakers, not the JSON number
            entry.get("size_us"),
            entry.get("currency"),
            entry.get("amount"),
            entry.get("type"),
            entry.get("location"),
            entry.get("purchased_at"),
            Json(entry),
        )
        for entry in entries
    ]

    with conn:
        with conn.cursor() as cur:
            execute_values(
                cur,
                """
                INSERT INTO market_sales
                    (sneaker_id, goat_product_id, size_us, currency, amount,
                     purchase_type, location, purchased_at, raw_response)
                VALUES %s;
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
        description="Populate market_sales with GOAT sales from KicksDB. "
                    "Run with --limit 3 first and check the per-sneaker row counts."
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
    no_sales = []
    for sneaker_id, style_code, name, goat_product_id in sneakers:
        try:
            raw_text = fetch_goat_sales(goat_product_id, api_key)
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

        # Pagination guard. The verified sample had "meta": null. Anything else
        # may carry a cursor, and silently keeping page one while reporting
        # success is the failure this stops.
        meta = payload.get("meta")
        if meta:
            print(f"\n[{style_code}] meta is not empty on a real response: {meta!r}\n"
                  f"This script does not page. Stopping so paging can be added deliberately "
                  f"rather than silently capturing only the first page.", file=sys.stderr)
            conn.close()
            sys.exit(1)

        entries = payload.get("data") or []

        # str() on the response's numeric product_id before comparing to the
        # TEXT column -- see the module docstring. A mismatch means the response
        # is for a different product, so those entries are dropped rather than
        # attributed to this sneaker.
        matched = []
        mismatched = 0
        for entry in entries:
            if str(entry.get("product_id")) != str(goat_product_id):
                mismatched += 1
                continue
            matched.append(entry)

        if mismatched:
            print(f"  [{style_code}] {mismatched} entr(ies) had a different product_id — dropped")

        written = insert_sales(conn, sneaker_id, goat_product_id, matched)
        total_rows += written

        if written == 0:
            no_sales.append(style_code)
        print(f"  [{style_code}] goat {goat_product_id}: {written} sale row(s)  ({name})")

    print(f"\nDone. Inserted {total_rows} row(s) across {len(sneakers)} sneaker(s).")
    if no_sales:
        print(f"No sales returned ({len(no_sales)}): {', '.join(no_sales)}")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(DISTINCT sneaker_id) FROM market_sales;")
        rows, sneaker_count = cur.fetchone()
        print(f"market_sales now holds {rows} row(s) across {sneaker_count} sneaker(s).")
        cur.execute("SELECT currency, COUNT(*) FROM market_sales GROUP BY currency ORDER BY 2 DESC;")
        print("currencies present:", cur.fetchall())
        cur.execute("SELECT purchase_type, COUNT(*) FROM market_sales GROUP BY purchase_type ORDER BY 2 DESC;")
        print("purchase types present:", cur.fetchall())

    conn.close()


if __name__ == "__main__":
    main()
