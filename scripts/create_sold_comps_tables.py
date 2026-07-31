"""One-time migration: creates sold_comps and comp_rejections tables (idempotent),
their indexes, RLS, and public-read policies. See docs/comp_filtering_spec.md.

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
            CREATE TABLE IF NOT EXISTS sold_comps (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
                item_id TEXT NOT NULL UNIQUE,
                title TEXT,
                sold_price NUMERIC NOT NULL,
                shipping_price NUMERIC,
                total_price NUMERIC,
                currency TEXT NOT NULL,
                size TEXT,
                condition_id INT NOT NULL,
                listing_type TEXT,
                thumbnail_url TEXT,
                ended_at DATE NOT NULL,
                source TEXT NOT NULL DEFAULT 'apify_ebay',
                scraped_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sold_comps_sneaker ON sold_comps (sneaker_id);")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sold_comps_ended_at ON sold_comps (ended_at);")

        cur.execute("""
            CREATE TABLE IF NOT EXISTS comp_rejections (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
                item_id TEXT,
                title TEXT,
                rejection_rule TEXT NOT NULL,
                rejected_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_comp_rejections_sneaker ON comp_rejections (sneaker_id);")

        cur.execute("ALTER TABLE sold_comps ENABLE ROW LEVEL SECURITY;")
        cur.execute("ALTER TABLE comp_rejections ENABLE ROW LEVEL SECURITY;")

        for table in ("sold_comps", "comp_rejections"):
            cur.execute(
                """
                SELECT 1 FROM pg_policies
                WHERE schemaname = 'public' AND tablename = %s AND policyname = 'public read';
                """,
                (table,),
            )
            if not cur.fetchone():
                cur.execute(f'CREATE POLICY "public read" ON {table} FOR SELECT USING (true);')
    conn.commit()
    print("sold_comps and comp_rejections migrated successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT tablename, rowsecurity
        FROM pg_tables
        WHERE schemaname = 'public' AND tablename IN ('sold_comps', 'comp_rejections')
        ORDER BY tablename;
    """)
    print(f"{'table':<20} rls_enabled")
    print("-" * 35)
    for tablename, rowsecurity in cur.fetchall():
        print(f"{tablename:<20} {rowsecurity}")

    print("\nPolicies:")
    cur.execute("""
        SELECT tablename, policyname, cmd
        FROM pg_policies
        WHERE schemaname = 'public' AND tablename IN ('sold_comps', 'comp_rejections')
        ORDER BY tablename, policyname;
    """)
    for tablename, policyname, cmd in cur.fetchall():
        print(f"  {tablename:<20} {policyname:<15} {cmd}")

conn.close()
