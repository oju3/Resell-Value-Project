"""One-time migration (idempotent): creates market_sales.

Real sold transactions pulled from GOAT via KicksDB, one row per sale.
Populated by scripts/fetch_goat_sales.py.

No `source` column by design. Every row in this table came from GOAT by
construction -- the table itself is the provenance. If a second platform is
added later, that is a new table, or a source column added at that point with
a backfill for these rows; adding one now would be a column with exactly one
value in it forever.

goat_product_id is TEXT here to match sneakers.goat_product_id exactly. The
sales response returns product_id as a JSON NUMBER (1293064, not "1293064"),
so the loader stringifies it before comparing or inserting -- storing it as a
number on one side and text on the other is how a join silently returns zero
rows later.

KNOWN GAP -- NO DEDUPLICATION. There is deliberately no unique constraint on
(goat_product_id, purchased_at, size_us, amount) or anything else. Re-running
fetch_goat_sales.py WILL insert duplicate rows. That is accepted for the first
pass and is recorded here so it is visible rather than discovered later; it is
not silently worked around in the loader either.

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
            CREATE TABLE IF NOT EXISTS market_sales (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
                goat_product_id TEXT NOT NULL,

                -- Payload columns are nullable: a response row missing one
                -- field should still land with the rest intact rather than
                -- failing the whole batch. raw_response preserves whatever
                -- was actually returned.
                size_us TEXT,
                currency TEXT,
                amount NUMERIC,
                purchase_type TEXT,
                location TEXT,
                purchased_at TIMESTAMPTZ,

                raw_response JSONB,
                ingested_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("""
            COMMENT ON TABLE market_sales IS
                'Real GOAT sold transactions from KicksDB /v3/goat/products/{id}/sales, one row per sale. '
                'No source column: every row is GOAT by construction. '
                'NO DEDUPLICATION -- re-running scripts/fetch_goat_sales.py inserts duplicates. '
                'amount/currency are stored exactly as returned, never converted.';
        """)
        cur.execute("""
            COMMENT ON COLUMN market_sales.raw_response IS
                'The individual sale object as returned, not the full response envelope. '
                'The complete envelope is cached to cache/kicksdb_goat_sales/{goat_product_id}.json. '
                'Keeps any field not mapped to a column above from being lost.';
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_market_sales_sneaker
            ON market_sales (sneaker_id);
        """)

        # RLS on every table is this schema's standing convention (all 13
        # existing tables have it). Market data, so public read / service-role
        # write, matching sold_comps.
        cur.execute("ALTER TABLE market_sales ENABLE ROW LEVEL SECURITY;")
        cur.execute("""
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = 'market_sales'
              AND policyname = 'public read';
        """)
        if not cur.fetchone():
            cur.execute('CREATE POLICY "public read" ON market_sales FOR SELECT USING (true);')

    conn.commit()
    print("market_sales created successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'market_sales'
        ORDER BY ordinal_position;
    """)
    print("market_sales:")
    for name, dtype, nullable in cur.fetchall():
        print(f"  {name:<20}{dtype:<28}null={nullable}")

    cur.execute("SELECT rowsecurity FROM pg_tables WHERE tablename = 'market_sales';")
    print("\nRLS enabled:", cur.fetchone()[0])

    cur.execute("SELECT COUNT(*) FROM market_sales;")
    print("rows:", cur.fetchone()[0])

    cur.execute("SELECT COUNT(goat_product_id) FROM sneakers;")
    print(f"sneakers eligible for fetch (goat_product_id NOT NULL): {cur.fetchone()[0]}")

conn.close()
