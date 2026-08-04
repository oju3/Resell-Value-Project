"""One-time migration: reframes refresh_runs.outcome so a zero-write run
states its own cause directly, instead of a single generic 'stalled' value
that required cross-checking raw_returned/on_conflict_skipped to interpret
-- the same problem this table was split out of comp_rejections to avoid.
See docs/refresh_schedule.md.

'stalled' is replaced by four specific outcomes:
- no_listings_found: raw_returned = 0 (nothing in the 30-day window)
- all_filtered: raw_returned > 0 but every item was rejected by filter_comp
- no_new_sales: every accepted row already existed in sold_comps under this
  SAME sneaker_id (window overlap between runs -- expected, not a problem)
- cross_sneaker_conflict: at least one accepted row already existed under a
  DIFFERENT sneaker_id (the actual cross-colourway contamination signal)

Adds cross_sneaker_skips as supporting detail on the row, not as something a
reader must consult to know what 'stalled' meant.

Existing rows: refresh_comps.py never stored per-run item_ids, so which
sub-case of "everything conflicted" applies (no_new_sales vs
cross_sneaker_conflict) can't be reconstructed retroactively for rows written
before this migration. Backfilling those into cross_sneaker_conflict would
assert contamination with no evidence, so they're backfilled to the
non-alarming no_new_sales instead, and cross_sneaker_skips is left NULL
(unknown) rather than 0 (confirmed none) for every pre-migration row.

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
        cur.execute("ALTER TABLE refresh_runs ADD COLUMN IF NOT EXISTS cross_sneaker_skips INT;")

        # Drop the old constraint before backfilling -- it only allows the
        # legacy 'stalled' value, so it must be gone before rows are
        # rewritten to the new outcome values below.
        cur.execute("ALTER TABLE refresh_runs DROP CONSTRAINT IF EXISTS refresh_runs_outcome_check;")

        cur.execute(
            "UPDATE refresh_runs SET outcome = 'no_listings_found' "
            "WHERE outcome = 'stalled' AND raw_returned = 0;"
        )
        cur.execute(
            "UPDATE refresh_runs SET outcome = 'all_filtered' "
            "WHERE outcome = 'stalled' AND raw_returned > 0 AND on_conflict_skipped = 0;"
        )
        cur.execute(
            "UPDATE refresh_runs SET outcome = 'no_new_sales' "
            "WHERE outcome = 'stalled' AND on_conflict_skipped > 0;"
        )

        cur.execute("""
            ALTER TABLE refresh_runs ADD CONSTRAINT refresh_runs_outcome_check
                CHECK (outcome IN (
                    'ok', 'no_listings_found', 'all_filtered', 'no_new_sales',
                    'cross_sneaker_conflict', 'actor_error', 'db_error'
                ));
        """)

        cur.execute("""
            COMMENT ON COLUMN refresh_runs.outcome IS
                'Single field a query reads to know what happened to a run -- ok, or one of four '
                'zero-write causes: no_listings_found (raw_returned=0), all_filtered (every raw '
                'item rejected by filter_comp), no_new_sales (all accepted rows already in '
                'sold_comps under this SAME sneaker_id -- window overlap, expected, not a '
                'problem), cross_sneaker_conflict (at least one accepted row already in '
                'sold_comps under a DIFFERENT sneaker_id -- the real contamination signal), or '
                'actor_error/db_error. Only cross_sneaker_conflict escalates to the loud '
                'consecutive-run warning in refresh_comps.py. See docs/refresh_schedule.md.';
        """)
        cur.execute("""
            COMMENT ON COLUMN refresh_runs.cross_sneaker_skips IS
                'Count of this run''s on_conflict_skipped rows whose existing sold_comps owner is '
                'a different sneaker_id -- supporting detail behind cross_sneaker_conflict, not '
                'itself the field that changes outcome''s meaning. NULL for rows written before '
                'this column existed (genuinely unknown -- per-run item_ids were not retained to '
                'reconstruct it retroactively), not 0.';
        """)
    conn.commit()
    print("refresh_runs outcome migration applied successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("SELECT outcome, count(*) FROM refresh_runs GROUP BY outcome ORDER BY outcome;")
    print("outcome distribution after migration:")
    for outcome, count in cur.fetchall():
        print(f"  {outcome:<25} {count}")

conn.close()
