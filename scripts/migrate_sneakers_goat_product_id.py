"""One-time migration (idempotent): adds sneakers.goat_product_id.

Nullable on purpose. It is populated by scripts/fetch_goat_product_ids.py from
KicksDB's unified products endpoint, and a sneaker with no GOAT listing (SKU
format mismatch, delisted, never carried) keeps NULL. NULL means "not looked up
or not found" and must never be filled with a placeholder.

TEXT, not an integer, even though the observed GOAT value looks numeric
("1293064"). It is an external vendor identifier: nothing here does arithmetic
on it, leading zeros would be significant if they ever appeared, and the format
is KicksDB's to change. The same response field carries a UUID for stockx.

No UNIQUE constraint and no index -- neither was specified, and a UNIQUE would
turn a duplicate id from the vendor into a mid-backfill failure rather than
something the loader can report.

Never prints the connection string itself.
"""
import os
import sys
from pathlib import Path

import psycopg2
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

db_url = os.environ.get("DATABASE_URL")
if not db_url:
    print("DATABASE_URL not found in .env", file=sys.stderr)
    sys.exit(1)

conn = psycopg2.connect(db_url)
conn.autocommit = False
try:
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE sneakers
            ADD COLUMN IF NOT EXISTS goat_product_id TEXT;
        """)
        cur.execute("""
            COMMENT ON COLUMN sneakers.goat_product_id IS
                'GOAT source_product_id from KicksDB /v3/unified/products/{sku}. '
                'NULL means not looked up, or no goat entry in the response -- never a placeholder. '
                'Populated by scripts/fetch_goat_product_ids.py.';
        """)
    conn.commit()
    print("sneakers.goat_product_id migrated successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'sneakers' AND column_name = 'goat_product_id';
    """)
    row = cur.fetchone()
    print(f"  {row[0]:<20}{row[1]:<12}null={row[2]}")

    cur.execute("""
        SELECT COUNT(*), COUNT(goat_product_id) FROM sneakers;
    """)
    total, populated = cur.fetchone()
    print(f"\n  sneakers: {total}   goat_product_id populated: {populated}   NULL: {total - populated}")

conn.close()
