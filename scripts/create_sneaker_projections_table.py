"""One-time migration (idempotent): creates sneaker_projections.

One row per sneaker holding a tuned weighted-linear trend fit over
goat_daily_sales, plus the backtest evidence behind it. Built by
scripts/build_sneaker_projections.py.

A DERIVED, REBUILDABLE table. UNIQUE (sneaker_id) with ON CONFLICT DO UPDATE:
re-running the builder as goat_daily_sales accumulates refreshes each sneaker's
projection in place. Same reasoning as goat_daily_sales, deliberately NOT
market_sales -- there is no meaningful "second projection" for a sneaker, only a
fresher one.

HOW THE HALF-LIFE IS CHOSEN vs HOW THE STORED FIT IS PRODUCED -- these differ.
The half-life is selected by backtest: train on the first 75% of a sneaker's
daily rows, measure MAPE against the held-out last 25%, keep the candidate with
the lowest MAPE. The slope/intercept/residual_stdev stored here are then REFIT
on 100% of the rows at that chosen half-life. The backtest's only job is
choosing the half-life; the production fit should not throw away the most
recent 25% of real data forever.

confidence_tier is never null and never omitted. Four values, and the
distinction between the last two carries real information:
  normal            MAPE <= 15%
  low_confidence    15% < MAPE <= 25%
  suppressed        MAPE > 25%   -- evaluated, found unreliable
  insufficient_data fewer than 8 daily rows -- could not be evaluated at all
A suppressed sneaker still gets a row. Omitting it would be indistinguishable
from "never evaluated", which is a different and worse unknown.

half_life_candidates records the full swept list, not just the winner, so a row
built under one candidate range stays auditable if that range changes later.

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
            CREATE TABLE IF NOT EXISTS sneaker_projections (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,

                -- UNIQUE, not just FK: one current projection per sneaker.
                -- Also the ON CONFLICT target. No separate index needed --
                -- UNIQUE creates one.
                sneaker_id BIGINT NOT NULL UNIQUE REFERENCES sneakers(id),

                -- All five NULL together when confidence_tier is
                -- 'insufficient_data'. Never partially populated.
                half_life_days INT,
                slope_per_day NUMERIC,
                intercept NUMERIC,
                residual_stdev NUMERIC,
                mape NUMERIC,

                -- Daily rows the fit was built from. NOT NULL because it is
                -- always known, including for insufficient_data rows, where it
                -- is the whole reason the sneaker could not be evaluated.
                n_rows INT NOT NULL,

                -- x = 0 in the fitted line. See the COMMENT below: intercept
                -- cannot be evaluated without this.
                reference_date DATE,

                confidence_tier TEXT NOT NULL CHECK (confidence_tier IN
                    ('normal', 'low_confidence', 'suppressed', 'insufficient_data')),

                -- The whole swept list, not just the winner.
                half_life_candidates JSONB NOT NULL,

                computed_at TIMESTAMPTZ NOT NULL DEFAULT now()
            );
        """)
        cur.execute("""
            COMMENT ON TABLE sneaker_projections IS
                'Tuned weighted-linear trend per sneaker over goat_daily_sales, with the backtest '
                'evidence behind it. Derived and rebuildable: UNIQUE (sneaker_id) with ON CONFLICT '
                'DO UPDATE, so re-running the builder refreshes rather than duplicates. The '
                'half-life is CHOSEN by backtest on the first 75%% of rows, but the stored fit is '
                'REFIT on 100%% of rows at that half-life. Linear trend only -- non-linear fitting '
                'was deliberately deferred (overfitting risk on 22-100 points). Stores the inputs '
                'Monte Carlo will consume later; does not run Monte Carlo.';
        """)
        cur.execute("""
            COMMENT ON COLUMN sneaker_projections.reference_date IS
                'x = 0 for the fitted line: predicted = intercept + slope_per_day * '
                '(target_date - reference_date in days). Equals the sneaker''s earliest '
                'goat_daily_sales date at build time. Stored rather than re-derived because '
                'MIN(sale_date) shifts if older daily rows ever arrive, which would silently '
                'invalidate every stored intercept.';
        """)
        cur.execute("""
            COMMENT ON COLUMN sneaker_projections.n_rows IS
                'Count of goat_daily_sales rows the fit was built from. Lets a stored MAPE be read '
                'in context -- 10%% off 22 points is not 10%% off 100 points. Populated even for '
                'insufficient_data rows, where it is the reason for that tier.';
        """)
        cur.execute("""
            COMMENT ON COLUMN sneaker_projections.confidence_tier IS
                'normal (MAPE<=15), low_confidence (15<MAPE<=25), suppressed (MAPE>25, evaluated '
                'and found unreliable), insufficient_data (<8 daily rows, could not be evaluated). '
                'Suppressed rows are still inserted -- absence would look like never-evaluated.';
        """)
        cur.execute("""
            COMMENT ON COLUMN sneaker_projections.mape IS
                'Mean absolute percentage error of the winning half-life against the held-out last '
                '25%% of rows. Backtest evidence for the choice, NOT the error of the stored fit '
                '(which was refit on 100%% of rows).';
        """)

        # RLS on every table is this schema's standing convention. Derived
        # market data, so public read / service-role write.
        cur.execute("ALTER TABLE sneaker_projections ENABLE ROW LEVEL SECURITY;")
        cur.execute("""
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = 'sneaker_projections'
              AND policyname = 'public read';
        """)
        if not cur.fetchone():
            cur.execute('CREATE POLICY "public read" ON sneaker_projections FOR SELECT USING (true);')

    conn.commit()
    print("sneaker_projections created successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT column_name, data_type, is_nullable
        FROM information_schema.columns
        WHERE table_name = 'sneaker_projections'
        ORDER BY ordinal_position;
    """)
    print("sneaker_projections:")
    for name, dtype, nullable in cur.fetchall():
        print(f"  {name:<22}{dtype:<28}null={nullable}")

    cur.execute("""
        SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint
        WHERE conrelid = 'sneaker_projections'::regclass AND contype IN ('u', 'c')
        ORDER BY contype;
    """)
    print()
    for name, defn in cur.fetchall():
        print(f"  {name}: {defn}")

    cur.execute("SELECT rowsecurity FROM pg_tables WHERE tablename = 'sneaker_projections';")
    print("\nRLS enabled:", cur.fetchone()[0])

    cur.execute("SELECT COUNT(*) FROM sneaker_projections;")
    print("rows:", cur.fetchone()[0])

conn.close()
