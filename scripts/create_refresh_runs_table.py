"""One-time migration: creates refresh_runs table (idempotent), its index,
RLS, and public-read policy. See docs/refresh_schedule.md.

refresh_runs is per-sneaker-per-attempt bookkeeping for scripts/refresh_comps.py,
kept separate from comp_rejections (which stays strictly per-listing, per
docs/comp_filtering_spec.md's Auditing section).

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
            CREATE TABLE IF NOT EXISTS refresh_runs (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                sneaker_id BIGINT NOT NULL REFERENCES sneakers(id),
                run_at TIMESTAMPTZ NOT NULL DEFAULT now(),
                raw_returned INT,
                new_rows_inserted INT,
                on_conflict_skipped INT,
                outcome TEXT NOT NULL CHECK (outcome IN ('ok', 'stalled', 'actor_error', 'db_error'))
            );
        """)
        cur.execute("""
            CREATE INDEX IF NOT EXISTS idx_refresh_runs_sneaker_run_at
                ON refresh_runs (sneaker_id, run_at DESC);
        """)
        cur.execute("""
            COMMENT ON TABLE refresh_runs IS
                'Per-sneaker-per-attempt log written by scripts/refresh_comps.py, one row per '
                'attempt including outcome = ''ok'' (not just failures) -- see docs/refresh_schedule.md. '
                'Distinct from comp_rejections, which stays strictly per-listing.';
        """)

        cur.execute("ALTER TABLE refresh_runs ENABLE ROW LEVEL SECURITY;")
        cur.execute("""
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = 'refresh_runs' AND policyname = 'public read';
        """)
        if not cur.fetchone():
            cur.execute('CREATE POLICY "public read" ON refresh_runs FOR SELECT USING (true);')
    conn.commit()
    print("refresh_runs migrated successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT tablename, rowsecurity
        FROM pg_tables
        WHERE schemaname = 'public' AND tablename = 'refresh_runs';
    """)
    print(f"{'table':<20} rls_enabled")
    print("-" * 35)
    for tablename, rowsecurity in cur.fetchall():
        print(f"{tablename:<20} {rowsecurity}")

    print("\nPolicies:")
    cur.execute("""
        SELECT tablename, policyname, cmd
        FROM pg_policies
        WHERE schemaname = 'public' AND tablename = 'refresh_runs';
    """)
    for tablename, policyname, cmd in cur.fetchall():
        print(f"  {tablename:<20} {policyname:<15} {cmd}")

conn.close()
