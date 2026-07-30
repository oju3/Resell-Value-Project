"""Applies seed_data.sql to the database in DATABASE_URL, then prints both tables.

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

sql = (ROOT / "scripts" / "seed_data.sql").read_text()

conn = psycopg2.connect(db_url)
try:
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("Seed data inserted successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Seed FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    print("platform_fees:")
    cur.execute(
        "SELECT id, platform, fee_percent, fixed_fee, min_condition, default_days_to_sell "
        "FROM platform_fees ORDER BY id;"
    )
    for row in cur.fetchall():
        print(f"  {row}")

    print("\ncondition_multipliers:")
    cur.execute(
        "SELECT id, condition, multiplier, uncertainty_percent "
        "FROM condition_multipliers ORDER BY id;"
    )
    for row in cur.fetchall():
        print(f"  {row}")

conn.close()
