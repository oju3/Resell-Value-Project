"""Applies schema.sql to the database in DATABASE_URL, then prints table/RLS status.

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

sql = (ROOT / "schema.sql").read_text()

conn = psycopg2.connect(db_url)
conn.autocommit = False
try:
    with conn.cursor() as cur:
        cur.execute(sql)
    conn.commit()
    print("Schema applied successfully.\n")
except Exception as e:
    conn.rollback()
    print(f"Schema apply FAILED, rolled back: {e}", file=sys.stderr)
    sys.exit(1)

with conn.cursor() as cur:
    cur.execute(
        """
        SELECT tablename, rowsecurity
        FROM pg_tables
        WHERE schemaname = 'public'
        ORDER BY tablename;
        """
    )
    rows = cur.fetchall()
    print(f"{'table':<25} rls_enabled")
    print("-" * 40)
    for tablename, rowsecurity in rows:
        print(f"{tablename:<25} {rowsecurity}")

    print("\nPolicies:")
    cur.execute(
        """
        SELECT tablename, policyname, cmd
        FROM pg_policies
        WHERE schemaname = 'public'
        ORDER BY tablename, policyname;
        """
    )
    for tablename, policyname, cmd in cur.fetchall():
        print(f"  {tablename:<25} {policyname:<15} {cmd}")

conn.close()
