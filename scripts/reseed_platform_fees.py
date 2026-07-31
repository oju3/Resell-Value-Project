"""Adds price-band columns to platform_fees, then clears and reseeds it with
the current fee schedules for eBay, StockX, and GOAT.

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
            ALTER TABLE platform_fees
                ADD COLUMN IF NOT EXISTS min_price NUMERIC,
                ADD COLUMN IF NOT EXISTS max_price NUMERIC;
        """)

        # Replace UNIQUE(platform) with UNIQUE(platform, min_price) so a
        # platform can have multiple price bands (e.g. eBay's $0-150 / $150+ split).
        cur.execute("""
            SELECT conname FROM pg_constraint
            WHERE conrelid = 'platform_fees'::regclass
              AND contype = 'u'
              AND conkey = (
                  SELECT array_agg(attnum) FROM pg_attribute
                  WHERE attrelid = 'platform_fees'::regclass AND attname = 'platform'
              );
        """)
        old_constraint = cur.fetchone()
        if old_constraint:
            cur.execute(f'ALTER TABLE platform_fees DROP CONSTRAINT "{old_constraint[0]}";')

        cur.execute("""
            ALTER TABLE platform_fees
                ADD CONSTRAINT platform_fees_platform_min_price_key UNIQUE (platform, min_price);
        """)

        cur.execute("DELETE FROM platform_fees;")

        # GOAT's 12.40% = 9.5% commission + 2.9% cash-out fee, bundled as one flat rate.
        # StockX and GOAT rates assume a standard (Level 1) seller tier.
        cur.execute("""
            INSERT INTO platform_fees
                (platform, fee_percent, fixed_fee, min_condition, default_days_to_sell, min_price, max_price)
            VALUES
                ('ebay',   13.25, 0.30, 'any',       7, 0,   150),
                ('ebay',    8.00, 0,    'any',       7, 150, NULL),
                ('stockx', 12.00, 0,    'deadstock', 3, 0,   NULL),
                ('goat',   12.40, 0,    'good',      5, 0,   NULL);
        """)
    conn.commit()
    print("platform_fees reseeded successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Reseed FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("""
        SELECT id, platform, fee_percent, fixed_fee, min_condition,
               default_days_to_sell, min_price, max_price
        FROM platform_fees
        ORDER BY platform, min_price;
    """)
    rows = cur.fetchall()
    cols = ("id", "platform", "fee_percent", "fixed_fee", "min_condition",
            "days_to_sell", "min_price", "max_price")
    fmt = "{:<4}{:<9}{:<12}{:<10}{:<14}{:<13}{:<10}{:<10}"
    print(fmt.format(*cols))
    print("-" * 82)
    for r in rows:
        print(fmt.format(*[("" if v is None else v) for v in r]))

conn.close()
