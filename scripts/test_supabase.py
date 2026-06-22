"""Sanity check: prove we can reach Supabase Postgres and run a query.

Run with:
    ./venv/bin/python scripts/test_supabase.py

Exits 0 on success, 1 on any failure. Prints what it sees so you can tell
*why* a failure happened (missing env, bad password, network, etc.).
"""

import os
import sys

# Make the project root importable when this script is run directly from scripts/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import psycopg

import config


def main() -> int:
    if not config.SUPABASE_DATABASE_URL:
        print("FAIL: SUPABASE_DATABASE_URL is not set in .env")
        return 1

    # Show host:port only — never the password.
    try:
        host_part = config.SUPABASE_DATABASE_URL.split("@", 1)[1].split("/", 1)[0]
    except IndexError:
        print("FAIL: SUPABASE_DATABASE_URL is malformed")
        return 1

    print(f"Connecting to {host_part} ...")

    try:
        with psycopg.connect(config.SUPABASE_DATABASE_URL, connect_timeout=10) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT version(), current_database(), current_user;")
                version, db, user = cur.fetchone()
        print(f"OK: connected as {user} to db '{db}'")
        print(f"    server: {version.split(',')[0]}")
        return 0
    except Exception as e:
        print(f"FAIL: {type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
