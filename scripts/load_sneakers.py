"""Loads data/jordans_seed.csv into the sneakers table, then prints verification output.

Never prints the connection string itself.
"""
import csv
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

with open(ROOT / "data" / "jordans_seed.csv") as f:
    rows = list(csv.DictReader(f))

conn = psycopg2.connect(db_url)
try:
    with conn.cursor() as cur:
        for row in rows:
            cur.execute(
                """
                INSERT INTO sneakers (name, brand, style_code, colorway, release_date, hype_tier)
                VALUES (%s, %s, %s, %s, %s, %s);
                """,
                (
                    row["name"],
                    "Jordan",
                    row["style_code"],
                    row["colorway"],
                    row["release_date"],
                    int(row["hype_tier"]),
                ),
            )
    conn.commit()
    print(f"Loaded {len(rows)} rows into sneakers.\n")
except Exception as e:
    conn.rollback()
    print(f"Load FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute("SELECT count(*) FROM sneakers;")
    print("Row count:", cur.fetchone()[0])

    cur.execute("SELECT hype_tier, count(*) FROM sneakers GROUP BY hype_tier ORDER BY hype_tier;")
    print("\nTier distribution:")
    for tier, count in cur.fetchall():
        print(f"  tier {tier}: {count}")

    cur.execute(
        "SELECT id, name, style_code, colorway, release_date, hype_tier "
        "FROM sneakers WHERE id IN (1, 2, 3, 25, 50) ORDER BY id;"
    )
    print("\n5 sample rows (including row 25, the Off-White Sail correction):")
    for r in cur.fetchall():
        print(f"  {r}")

conn.close()
