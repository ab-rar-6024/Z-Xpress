"""
One-off: copy every table from the local XAMPP database into Supabase.

    python migrate_mysql_to_supabase.py            # refuses if Supabase already has data
    python migrate_mysql_to_supabase.py --replace  # wipes the Supabase tables first

Needs XAMPP MySQL running (reads db_config.json -> mysql) and
db_config.json -> postgres.url filled in.  The MySQL data is only READ.
"""
import os
import sys

import pymysql
import psycopg2
from psycopg2.extras import execute_values

import db

HERE   = os.path.dirname(os.path.abspath(__file__))
TABLES = ["app_users", "license", "place_zones", "zones", "rates",
          "users", "day_wise", "bookings"]
BATCH  = 500


def main():
    replace = "--replace" in sys.argv

    # ---- source: MySQL (read-only use) ----
    m = db.CONFIG["mysql"]
    src = pymysql.connect(host=m["host"], port=int(m["port"]), user=m["user"],
                          password=m["password"], database=m["database"],
                          charset="utf8mb4")
    scur = src.cursor()

    # ---- target: Supabase ----
    url = db.CONFIG["postgres"]["url"]
    if not url or "YOUR-PASSWORD" in url.upper():
        sys.exit("Set db_config.json -> postgres.url first.")
    dst = psycopg2.connect(url, connect_timeout=20, **({} if "sslmode" in url else {"sslmode": "require"}))
    dcur = dst.cursor()

    print("Creating tables (if missing)...")
    with open(os.environ.get("ZX_SCHEMA_FILE") or os.path.join(HERE, "supabase_schema.sql"), encoding="utf-8") as f:
        dcur.execute(f.read())
    dst.commit()

    # ---- safety: never silently mix with existing data ----
    existing = {}
    for t in TABLES:
        dcur.execute(f'SELECT COUNT(*) FROM "{t}"')
        existing[t] = dcur.fetchone()[0]
    if any(existing.values()):
        print("Supabase already contains rows:", {k: v for k, v in existing.items() if v})
        if not replace:
            sys.exit("Aborted. Re-run with --replace to wipe those tables and reload from MySQL.")
        print("--replace given: truncating Supabase tables...")
        dcur.execute("TRUNCATE " + ", ".join(f'"{t}"' for t in TABLES) + " RESTART IDENTITY")
        dst.commit()

    # ---- copy ----
    for t in TABLES:
        scur.execute(f"SELECT * FROM `{t}`")
        cols = [d[0] for d in scur.description]
        rows = scur.fetchall()
        if rows:
            col_sql = ", ".join(f'"{c}"' for c in cols)
            for i in range(0, len(rows), BATCH):
                execute_values(
                    dcur,
                    f'INSERT INTO "{t}" ({col_sql}) OVERRIDING SYSTEM VALUE VALUES %s',
                    rows[i:i + BATCH])
        # next generated id must continue after the highest copied id
        dcur.execute(
            f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
            f'COALESCE((SELECT MAX(id) FROM "{t}"), 0) + 1, false)')
        dst.commit()
        print(f"  {t:12s} {len(rows):6d} rows copied")

    # ---- verify ----
    print("\nVerifying...")
    ok = True
    for t in TABLES:
        scur.execute(f"SELECT COUNT(*) FROM `{t}`")
        a = scur.fetchone()[0]
        dcur.execute(f'SELECT COUNT(*) FROM "{t}"')
        b = dcur.fetchone()[0]
        flag = "OK " if a == b else "MISMATCH"
        ok &= (a == b)
        print(f"  {flag} {t:12s} mysql={a}  supabase={b}")
    scur.execute("SELECT COALESCE(SUM(total_amount),0), COALESCE(SUM(weight),0) FROM bookings")
    a = scur.fetchone()
    dcur.execute("SELECT COALESCE(SUM(total_amount),0), COALESCE(SUM(weight),0) FROM bookings")
    b = dcur.fetchone()
    same = (a[0] == b[0] and a[1] == b[1])
    ok &= same
    print(f"  {'OK ' if same else 'MISMATCH'} bookings totals  mysql={a}  supabase={b}")

    src.close(); dst.close()
    print("\nDONE - data matches." if ok else "\nPROBLEM - see MISMATCH above.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
