"""Populates sneakers.goat_product_id from KicksDB's unified products endpoint.

    GET https://api.kicks.dev/v3/unified/products/{sku}
    Authorization: Bearer {KICKS_API_KEY}

The response carries one object per shop in data[]; this script wants the one
where shop_name == "goat", field source_product_id.

SKU NORMALIZATION -- the reason exact matching would fail silently
------------------------------------------------------------------
The response sku uses a space ("FV5029 006"); sneakers.style_code uses a hyphen
("FV5029-006"). Chosen rule: STRIP ALL NON-ALPHANUMERICS AND UPPERCASE, applied
to BOTH sides before any comparison. Picked over replace-space-with-hyphen
because it also absorbs any other separator the vendor might use (dots, double
spaces, none at all) without a second fix later.

Both sides are normalized. Normalizing only one would compare "FV5029006"
against "FV5029-006" and match nothing, with no error -- every sneaker would
silently stay NULL and look like "GOAT doesn't carry these."

CACHING
-------
The full raw response body is written to cache/kicksdb_unified/{style_code}.json
BEFORE anything is parsed, so a parsing bug or an unexpected shape never
destroys the response it came from. fetch_unified_product() writes the file and
returns the same text it wrote; extraction only ever runs on already-cached data.

RATE LIMITING
-------------
Sequential, no parallelism, no artificial delay. 50 calls against a
50,000/month plan does not need throttling. There is no retry logic: a 429 is
treated as a hard stop rather than something to sleep through, because the
correct throttle interval is unknown until a real 429 is seen.

BATCH ORDER
-----------
Run with --limit 3 first. The selection is ordered so FV5029-006 (Jordan 4 Bred
Reimagined) is always in that first batch, and its extracted id is printed
against the manually confirmed value 1293064 for checking. Then re-run with no
--limit for the rest; that re-fetches the first 3 as well, which is 50 calls
total and not worth a skip flag on this plan.

KNOWN LIMITATION
----------------
No fallback lookup. A sneaker with no goat entry -- different SKU format,
delisted, never carried -- keeps goat_product_id NULL and is only reported to
console. This script does not investigate why.

Never prints, logs, or echoes KICKS_API_KEY or DATABASE_URL. The key goes into
a request header and nowhere else; failures print the style_code and HTTP
status only.
"""
import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
CACHE_DIR = ROOT / "cache" / "kicksdb_unified"

UNIFIED_PRODUCTS_URL = "https://api.kicks.dev/v3/unified/products/"

# The shop whose id we want. Other entries in data[] (stockx, etc.) are cached
# with the rest of the response but ignored here.
GOAT_SHOP_NAME = "goat"

# Manually confirmed against a real call, used as the first-batch check.
VERIFICATION_STYLE_CODE = "FV5029-006"
VERIFICATION_EXPECTED_ID = "1293064"

REQUEST_TIMEOUT_SECONDS = 30


def normalize_sku(value):
    """Strips every non-alphanumeric character and uppercases.

    "FV5029 006" and "FV5029-006" both become "FV5029006". Applied to BOTH the
    response sku and the stored style_code -- see the module docstring.
    """
    if value is None:
        return None
    return re.sub(r"[^A-Za-z0-9]", "", value).upper()


def fetch_unified_product(style_code, api_key):
    """Calls the unified endpoint for one style_code and caches the raw body.

    Returns the response body as text. The file is written BEFORE this returns,
    so the raw response exists on disk before any caller parses it.

    Raises urllib.error.HTTPError to the caller, which decides what is fatal.
    """
    request = urllib.request.Request(
        UNIFIED_PRODUCTS_URL + urllib.parse.quote(style_code),
        headers={"Authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        raw_text = response.read().decode("utf-8")

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    (CACHE_DIR / f"{style_code}.json").write_text(raw_text)

    return raw_text


def extract_goat_product_id(raw_text, style_code):
    """Returns the goat source_product_id from a cached response, or None.

    None means "no usable goat entry" and the caller must leave the column NULL.
    Three ways that happens, all of them reported rather than guessed around:
      - data[] holds no object with shop_name == "goat"
      - the goat object's sku does not normalize to this sneaker's style_code
      - the goat object has no source_product_id

    The sku check matters because the endpoint is queried by SKU but the
    response is not assumed to be for that SKU -- writing back an id from a
    different product would be exactly the silent fabrication this avoids.
    """
    payload = json.loads(raw_text)
    entries = payload.get("data") or []
    wanted_sku = normalize_sku(style_code)

    for entry in entries:
        if entry.get("shop_name") != GOAT_SHOP_NAME:
            continue

        entry_sku = normalize_sku(entry.get("sku"))
        if entry_sku != wanted_sku:
            print(
                f"  [{style_code}] goat entry SKU mismatch "
                f"(response {entry.get('sku')!r} -> {entry_sku!r}, expected {wanted_sku!r}) — leaving NULL"
            )
            return None

        product_id = entry.get("source_product_id")
        if not product_id:
            print(f"  [{style_code}] goat entry has no source_product_id — leaving NULL")
            return None

        return product_id

    shops = sorted({e.get("shop_name") for e in entries if e.get("shop_name")})
    print(f"  [{style_code}] no goat entry — leaving NULL (shops present: {shops or 'none'})")
    return None


def select_sneakers(conn, limit):
    """Sneakers to process, with the verification SKU forced into the first batch.

    ORDER BY puts VERIFICATION_STYLE_CODE first so that --limit 3 always
    includes the sneaker whose goat id is manually confirmed; without that, a
    small first batch could contain only unverifiable rows.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT id, style_code, name
            FROM sneakers
            ORDER BY (style_code = %s) DESC, id
            LIMIT %s;
            """,
            (VERIFICATION_STYLE_CODE, limit),
        )
        return cur.fetchall()


def main():
    parser = argparse.ArgumentParser(
        description="Populate sneakers.goat_product_id from KicksDB. "
                    "Run with --limit 3 first and check the printed verification id."
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
    print(f"Processing {len(sneakers)} sneaker(s). Cache: {CACHE_DIR}\n")

    found = 0
    missing = []
    for sneaker_id, style_code, name in sneakers:
        try:
            raw_text = fetch_unified_product(style_code, api_key)
        except urllib.error.HTTPError as e:
            # 429 and credential failures abort: both mean every remaining call
            # would fail the same way, and neither has a designed response yet
            # (no throttle interval measured, no key to fall back to).
            if e.code == 429:
                print(f"\n429 rate limited on {style_code}. Stopping — no throttling is "
                      f"designed into this script. Re-run once the limit resets, or add a "
                      f"delay based on the observed Retry-After.", file=sys.stderr)
                conn.close()
                sys.exit(1)
            if e.code in (401, 403):
                print(f"\nHTTP {e.code} on {style_code}: KICKS_API_KEY rejected. Stopping.",
                      file=sys.stderr)
                conn.close()
                sys.exit(1)
            print(f"  [{style_code}] HTTP {e.code} — leaving NULL")
            missing.append(style_code)
            continue
        except urllib.error.URLError as e:
            print(f"  [{style_code}] request failed ({e.reason}) — leaving NULL")
            missing.append(style_code)
            continue

        product_id = extract_goat_product_id(raw_text, style_code)

        if style_code == VERIFICATION_STYLE_CODE:
            status = "MATCH" if product_id == VERIFICATION_EXPECTED_ID else "MISMATCH"
            print(f"  [{style_code}] VERIFICATION: extracted {product_id!r}, "
                  f"expected {VERIFICATION_EXPECTED_ID!r} -> {status}")

        if product_id is None:
            missing.append(style_code)
            continue

        # Committed per sneaker rather than once at the end: this is a network
        # loop, and a failure at sneaker 40 should not discard the first 39.
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "UPDATE sneakers SET goat_product_id = %s WHERE id = %s;",
                    (product_id, sneaker_id),
                )
        found += 1
        print(f"  [{style_code}] {product_id}  ({name})")

    print(f"\nDone. Populated {found}/{len(sneakers)}.")
    if missing:
        print(f"Left NULL ({len(missing)}): {', '.join(missing)}")

    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*), COUNT(goat_product_id) FROM sneakers;")
        total, populated = cur.fetchone()
        print(f"Catalogue: {populated}/{total} sneakers have a goat_product_id.")

    conn.close()


if __name__ == "__main__":
    main()
