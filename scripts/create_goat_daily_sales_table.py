"""One-time migration (idempotent): creates goat_daily_sales.

GOAT's PRE-AGGREGATED daily feed, from KicksDB
/v3/goat/products/{id}/sales/daily. One row per DAY, not per transaction.
Populated by scripts/fetch_goat_daily_sales.py.

DISTINCT FROM market_sales. That table holds individual transactions from the
/sales endpoint; this one holds GOAT's own daily rollup from /sales/daily.
Different endpoint, different response shape, different grain. Do not join or
compare them casually -- avg_amount here is GOAT's average, not something
recomputed from market_sales rows.

avg_amount is already an average across that day's sales. `orders` is the count
it was computed from, and is the liquidity/confidence signal for that day --
taken as-is from the response, never recomputed.

SPARSE BY DESIGN. The endpoint returns a row only for days on which at least
one sale happened, so a low-volume sneaker produces a gapped series (confirmed
on product_id 158522: 22 rows across a ~90 day window, with real 5+ day gaps).
An absent date means no sale occurred that day. Nothing fills, interpolates, or
backfills those gaps -- the gaps ARE the trading-activity signal.

UNIQUE (goat_product_id, sale_date), and the loader uses ON CONFLICT DO UPDATE.
Unlike an individual transaction, a daily aggregate for a given date is a single
fact that a fresher pull should overwrite. There is no meaningful "duplicate
daily aggregate", so re-running the loader is idempotent rather than additive --
the opposite of market_sales, whose no-dedup gap is deliberate and documented
there.

No currency column. Every sale in the underlying /sales endpoint was verified
USD, and this feed aggregates those same sales, so the same currency applies.
The loader hard-stops if a currency field ever appears on a real response with
a non-USD value, rather than silently assuming USD here too.

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
            CREATE TABLE IF NOT EXISTS goat_daily_sales (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
                goat_product_id TEXT NOT NULL,
                sale_date DATE NOT NULL,

                -- GOAT's own average for that day, and the order count it was
                -- computed from. Both stored as returned, never recomputed.
                avg_amount NUMERIC,
                orders INT,

                raw_response JSONB,
                ingested_at TIMESTAMPTZ NOT NULL DEFAULT now(),

                -- The dedup key. One daily aggregate per product per date; a
                -- re-run refreshes it in place rather than appending.
                UNIQUE (goat_product_id, sale_date)
            );
        """)
        cur.execute("""
            COMMENT ON TABLE goat_daily_sales IS
                'GOAT pre-aggregated daily sales from KicksDB /v3/goat/products/{id}/sales/daily. '
                'One row per DAY, and only for days with at least one sale -- gaps are real and are '
                'never filled or interpolated. Distinct from market_sales, which holds individual '
                'transactions from the /sales endpoint. avg_amount and orders come straight from '
                'GOAT and are never recomputed. UNIQUE (goat_product_id, sale_date): re-running the '
                'loader refreshes a day in place, it does not duplicate.';
        """)
        cur.execute("""
            COMMENT ON COLUMN goat_daily_sales.orders IS
                'Count of sales the day''s avg_amount was computed from. This is the per-day '
                'liquidity/confidence signal -- use as-is, do not recompute from market_sales.';
        """)
        cur.execute("""
            COMMENT ON COLUMN goat_daily_sales.raw_response IS
                'The individual daily object as returned, not the full response envelope. '
                'The complete envelope is cached to cache/kicksdb_goat_daily/{goat_product_id}.json.';
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_goat_daily_sales_sneaker
            ON goat_daily_sales (sneaker_id);
        """)

        # RLS on every table is this schema's standing convention. Market data,
        # so public read / service-role write, matching market_sales.
        cur.execute("ALTER TABLE goat_daily_sales ENABLE ROW LEVEL SECURITY;")
        cur.execute("""
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = 'goat_daily_sales'
              AND policyname = 'public read';
        """)
        if not cur.fetchone():
            cur.execute('CREATE POLICY "public read" ON goat_daily_sales FOR SELECT USING (true);')

    conn.commit()
    print("goat_daily_sales created successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'goat_daily_sales'
        ORDER BY ordinal_position;
    """)
    print("goat_daily_sales:")
    for name, dtype, nullable in cur.fetchall():
        print(f"  {name:<20}{dtype:<28}null={nullable}")

    cur.execute("""
        SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = 'goat_daily_sales'::regclass AND contype = 'u';
    """)
    print("\nunique constraint:", cur.fetchone())

    cur.execute("SELECT rowsecurity FROM pg_tables WHERE tablename = 'goat_daily_sales';")
    print("RLS enabled:", cur.fetchone()[0])

    cur.execute("SELECT COUNT(*) FROM goat_daily_sales;")
    print("rows:", cur.fetchone()[0])

conn.close()
