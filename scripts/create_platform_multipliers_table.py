"""One-time migration: creates platform_multipliers table (idempotent), its
RLS/policy, and seeds the 2026-08-03 eBay/StockX/GOAT measurement.
See docs/platform_multipliers.md.

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
            CREATE TABLE IF NOT EXISTS platform_multipliers (
                id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                platform TEXT NOT NULL,
                multiplier NUMERIC NOT NULL,
                band_low NUMERIC,
                band_high NUMERIC,
                sample_size INT,
                method TEXT,
                confidence TEXT CHECK (confidence IN ('none', 'low', 'medium', 'high')),
                is_proxy BOOLEAN NOT NULL DEFAULT false,
                proxy_source TEXT,
                measured_date DATE NOT NULL,
                notes TEXT,
                UNIQUE (platform, measured_date)
            );
        """)
        cur.execute("""
            COMMENT ON TABLE platform_multipliers IS
                'platform_price = ebay_deadstock_median * multiplier; '
                'net_payout = platform_price * (1 - fee_percent) - fixed_fee (fee_percent/fixed_fee from platform_fees). '
                'eBay is the numeraire (multiplier = 1.00, confidence = high, is_proxy = false, a definitional '
                'reference rather than a measurement) -- the multiplier converts an eBay price into an estimated '
                'price on the target platform, never the reverse. Multiplier and fee are separate stages and must '
                'not be collapsed into one number.';
        """)

        cur.execute("ALTER TABLE platform_multipliers ENABLE ROW LEVEL SECURITY;")
        cur.execute("""
            SELECT 1 FROM pg_policies
            WHERE schemaname = 'public' AND tablename = 'platform_multipliers' AND policyname = 'public read';
        """)
        if not cur.fetchone():
            cur.execute('CREATE POLICY "public read" ON platform_multipliers FOR SELECT USING (true);')

        cur.execute("""
            INSERT INTO platform_multipliers
                (platform, multiplier, band_low, band_high, sample_size, method,
                 confidence, is_proxy, proxy_source, measured_date, notes)
            VALUES
                ('ebay', 1.00, NULL, NULL, NULL, NULL,
                 'high', false, NULL, '2026-08-03',
                 'Numeraire / definitional reference, not a measurement. All other platform '
                 'multipliers are expressed relative to the eBay deadstock median.'),
                ('stockx', 1.50, 1.40, 1.90, 12, 'ebay_deadstock_median_vs_stockx_size10_last_sale',
                 'low', false, NULL, '2026-08-03',
                 'Median of 12 sneaker-level ratios (eBay deadstock median vs StockX size-10 last sale). '
                 'Band is the IQR of the 12 ratios (1.40-1.91), rounded. See docs/platform_multipliers.md '
                 'for limitations.'),
                ('goat', 1.50, NULL, NULL, NULL, 'proxied_from_stockx',
                 'none', true, 'stockx', '2026-08-03',
                 'Not measured -- value copied from the StockX row as a placeholder. GOAT was never '
                 'sampled directly. Do not treat as an independent measurement.')
            ON CONFLICT (platform, measured_date) DO UPDATE SET
                multiplier = EXCLUDED.multiplier,
                band_low = EXCLUDED.band_low,
                band_high = EXCLUDED.band_high,
                sample_size = EXCLUDED.sample_size,
                method = EXCLUDED.method,
                confidence = EXCLUDED.confidence,
                is_proxy = EXCLUDED.is_proxy,
                proxy_source = EXCLUDED.proxy_source,
                notes = EXCLUDED.notes;
        """)
    conn.commit()
    print("platform_multipliers migrated successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Migration FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT platform, multiplier, band_low, band_high, sample_size, confidence, is_proxy, proxy_source
        FROM platform_multipliers
        ORDER BY platform;
    """)
    rows = cur.fetchall()
    cols = ("platform", "multiplier", "band_low", "band_high", "n", "confidence", "is_proxy", "proxy_source")
    fmt = "{:<8}{:<12}{:<10}{:<10}{:<5}{:<11}{:<9}{:<12}"
    print(fmt.format(*cols))
    print("-" * 77)
    for r in rows:
        print(fmt.format(*[("" if v is None else v) for v in r]))

    cur.execute("""
        SELECT tablename, rowsecurity FROM pg_tables
        WHERE schemaname = 'public' AND tablename = 'platform_multipliers';
    """)
    print("\nRLS:", cur.fetchone())

conn.close()
