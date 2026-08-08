"""One-time migration for the portfolio layer (idempotent):

  1. owned_sneakers.purchase_source  -- new column + CHECK on a fixed value list
  2. sold_sneakers                   -- new table for sold pairs
  3. RLS + per-user policies on sold_sneakers, mirroring owned_sneakers

Sold pairs live in their own table rather than behind a status flag on
owned_sneakers, so "what I own" and "what I sold" stay two clean queries.

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

# Lowercase slugs, deliberately. sale_platform values must match
# platform_fees.platform ('ebay', 'stockx', 'goat') exactly or the banded fee
# lookup silently returns zero rows, so both columns use one casing
# convention. Display casing ("SNKRS", "In-store") belongs to the frontend.
PURCHASE_SOURCES = ("snkrs", "stockx", "goat", "ebay", "in_store", "other")

conn = psycopg2.connect(db_url)
conn.autocommit = False
try:
    with conn.cursor() as cur:
        cur.execute("""
            ALTER TABLE owned_sneakers
            ADD COLUMN IF NOT EXISTS purchase_source TEXT;
        """)
        # CHECK rather than an enum: widening this list later is a one-line
        # constraint swap, where ALTER TYPE on an enum is a heavier migration.
        # Added separately from the column so the script stays idempotent.
        cur.execute("""
            SELECT 1 FROM pg_constraint
            WHERE conname = 'owned_sneakers_purchase_source_check';
        """)
        if not cur.fetchone():
            cur.execute("""
                ALTER TABLE owned_sneakers
                ADD CONSTRAINT owned_sneakers_purchase_source_check
                CHECK (purchase_source IN ('snkrs','stockx','goat','ebay','in_store','other'));
            """)

        cur.execute("""
            CREATE TABLE IF NOT EXISTS sold_sneakers (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                user_id UUID NOT NULL REFERENCES auth.users(id),
                sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
                size TEXT,

                -- Copied from owned_sneakers at the moment of sale, because
                -- that row is deleted by the same transaction.
                purchase_price NUMERIC NOT NULL,
                purchase_date DATE,
                purchase_source TEXT,

                sale_price NUMERIC NOT NULL,
                sale_platform TEXT NOT NULL CHECK (sale_platform IN ('ebay','stockx','goat')),
                sale_date DATE NOT NULL,

                -- Frozen at sale time. platform_fees is mutable -- this repo
                -- has already reseeded it twice -- so deriving these on read
                -- would retroactively rewrite what a past sale earned.
                fee_percent_applied NUMERIC NOT NULL,
                fixed_fee_applied NUMERIC NOT NULL,
                fee_amount NUMERIC NOT NULL,
                realized_pl NUMERIC NOT NULL,

                sold_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("""
            COMMENT ON TABLE sold_sneakers IS
                'Historical record of sold pairs. Self-contained by design: purchase and fee '
                'figures are copied in at sale time so the row cannot change when platform_fees '
                'or owned_sneakers do. sneaker_id stays a foreign key rather than a copied name '
                '-- catalogue identity is stable and it is the same shoe; the tradeoff is that '
                'renaming a sneaker re-renders past sales under the new name.';
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_sold_sneakers_user_id
            ON sold_sneakers (user_id);
        """)

        # RLS mirrors owned_sneakers. Defence-in-depth only: this API connects
        # with psycopg2, where auth.uid() is NULL and the role bypasses RLS,
        # so the real protection is the explicit WHERE user_id = %s carried by
        # every query in app/portfolio.py. See app/db.py::get_conn.
        cur.execute("ALTER TABLE sold_sneakers ENABLE ROW LEVEL SECURITY;")
        for policy, verb, clause in (
            ("select own", "SELECT", "USING (auth.uid() = user_id)"),
            ("insert own", "INSERT", "WITH CHECK (auth.uid() = user_id)"),
            ("update own", "UPDATE", "USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id)"),
            ("delete own", "DELETE", "USING (auth.uid() = user_id)"),
        ):
            cur.execute("""
                SELECT 1 FROM pg_policies
                WHERE schemaname = 'public' AND tablename = 'sold_sneakers' AND policyname = %s;
            """, (policy,))
            if not cur.fetchone():
                cur.execute(f'CREATE POLICY "{policy}" ON sold_sneakers FOR {verb} {clause};')

    conn.commit()
    print("Portfolio schema migrated successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'sold_sneakers'
        ORDER BY ordinal_position;
    """)
    print("sold_sneakers:")
    for name, dtype, nullable in cur.fetchall():
        print(f"  {name:<22}{dtype:<28}null={nullable}")

    cur.execute("""
        SELECT pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conname = 'owned_sneakers_purchase_source_check';
    """)
    print("\nowned_sneakers.purchase_source:", cur.fetchone()[0])

    cur.execute("""
        SELECT policyname, cmd FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'sold_sneakers' ORDER BY policyname;
    """)
    print("\nsold_sneakers policies:")
    for policyname, cmd in cur.fetchall():
        print(f"  {policyname:<14}{cmd}")

    cur.execute("SELECT rowsecurity FROM pg_tables WHERE tablename = 'sold_sneakers';")
    print("\nRLS enabled:", cur.fetchone()[0])

conn.close()
