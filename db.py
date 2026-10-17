"""
Database layer for ZXpress.

Two interchangeable back-ends, chosen by db_config.json (see below):

    "postgres"  -> Supabase (hosted PostgreSQL, reached over the internet)
    "mysql"     -> local XAMPP MariaDB/MySQL (the original setup)

app.py is written in MySQL dialect and was left that way on purpose, so
switching back to XAMPP later is just a config change.  For the Postgres
back-end every statement is translated on the fly by _translate() and the
cursor mimics the pymysql API the rest of the app already uses
(dictionary cursors, lastrowid, executemany, commit/rollback/close).

Config lookup order (first one found wins):
    1. environment variables   ZX_DB_BACKEND / ZX_DB_URL
    2. db_config.json in %APPDATA%\ZXpress   (written by the app's Database page)
    3. db_config.json next to ZXpress.exe   <- edit this on the client PC,
                                               no rebuild needed
    4. db_config.json bundled inside the exe
    5. built-in defaults (XAMPP MySQL, as before)
"""
import json
import os
import re
import sys
import threading
import time
from datetime import date, datetime


# ─── Config ────────────────────────────────────────────────

_DEFAULTS = {
    "backend": "mysql",
    "mysql": {
        "host": "localhost",
        "port": 3306,
        "user": "root",
        "password": "",
        "database": "booking_system",
        "charset": "utf8mb4",
    },
    "postgres": {
        # Easiest: paste the Supabase "Session pooler" connection string here
        #   postgresql://postgres.<ref>:<password>@aws-0-<region>.pooler.supabase.com:5432/postgres
        "url": "",
        "pool_size": 4,
        "connect_timeout": 15,
    },
}


def _app_dir():
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.abspath(__file__))


def _bundle_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))


def _user_dir():
    """Per-user folder - used when the exe's own folder is read-only."""
    d = os.environ.get("ZX_CONFIG_DIR")
    if d:
        return d
    return os.path.join(os.environ.get("APPDATA") or os.path.expanduser("~"), "ZXpress")


def _dir_writable(d):
    try:
        os.makedirs(d, exist_ok=True)
        probe = os.path.join(d, ".zx_write_test")
        with open(probe, "w") as f:
            f.write("x")
        os.remove(probe)
        return True
    except Exception:
        return False


def _config_write_path():
    """Where the app saves settings it changes at run time (backend, port).
    Same file the loader reads last, so it always wins."""
    user_file = os.path.join(_user_dir(), "db_config.json")
    app_file  = os.path.join(_app_dir(), "db_config.json")
    if os.environ.get("ZX_CONFIG_DIR") or os.path.exists(user_file):
        return user_file
    if _dir_writable(_app_dir()):
        return app_file
    return user_file


def _merge(dst, src):
    for k, v in src.items():
        if isinstance(v, dict) and isinstance(dst.get(k), dict):
            _merge(dst[k], v)
        else:
            dst[k] = v
    return dst


def _load_config():
    cfg = json.loads(json.dumps(_DEFAULTS))
    # later one overrides earlier: bundled -> next to exe -> per-user folder
    for path in (os.path.join(_bundle_dir(), "db_config.json"),
                 os.path.join(_app_dir(), "db_config.json"),
                 os.path.join(_user_dir(), "db_config.json")):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    user_cfg = json.load(f)
                for k, v in user_cfg.items():
                    if isinstance(v, dict) and isinstance(cfg.get(k), dict):
                        cfg[k].update(v)
                    else:
                        cfg[k] = v
            except Exception as e:                    # bad JSON must not kill the app silently
                sys.stderr and sys.stderr.write(f"[db] ignoring bad {path}: {e}\n")
    if os.environ.get("ZX_DB_BACKEND"):
        cfg["backend"] = os.environ["ZX_DB_BACKEND"].strip().lower()
    if os.environ.get("ZX_DB_URL"):
        cfg["postgres"]["url"] = os.environ["ZX_DB_URL"].strip()
    if os.environ.get("ZX_MYSQL_DB"):                 # lets tests use a scratch copy
        cfg["mysql"]["database"] = os.environ["ZX_MYSQL_DB"].strip()
    cfg["backend"] = str(cfg["backend"]).strip().lower()
    if cfg["backend"] in ("supabase", "postgresql", "pg"):
        cfg["backend"] = "postgres"
    return cfg


CONFIG  = _load_config()
BACKEND = CONFIG["backend"]            # "postgres" or "mysql"
IS_POSTGRES = BACKEND == "postgres"

# kept for any old code that still imports it
DB_CONFIG = CONFIG["mysql"]


class DatabaseUnavailable(Exception):
    """The active database cannot be reached (XAMPP not running, no internet...).
    app.py turns this into the recovery screen instead of a crash."""
    def __init__(self, message, backend=None):
        super().__init__(message)
        self.backend = backend


def save_config(changes):
    """Merge `changes` (nested dict) into the saved db_config.json and into the
    live CONFIG.  Only the keys given are touched; the rest of the file
    (e.g. the Supabase password) is preserved."""
    path = _config_write_path()
    current = None
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8-sig") as f:
                current = json.load(f)
            if not isinstance(current, dict) or not current:
                current = None
        except Exception:
            current = None
    if current is None:
        # file missing / empty / unreadable: start from the settings in effect now
        # (backend, Supabase string, MySQL login) so the new file is COMPLETE and
        # a deleted config can never silently flip the chosen database.
        current = json.loads(json.dumps(CONFIG))
    _merge(current, changes)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(current, f, indent=2)
    os.replace(tmp, path)
    _merge(CONFIG, changes)
    if "backend" in changes:                      # keep the live switch in sync
        global BACKEND, IS_POSTGRES
        name = {"supabase": "postgres", "postgresql": "postgres", "pg": "postgres",
                "xampp": "mysql"}.get(str(changes["backend"]).lower(), str(changes["backend"]).lower())
        if name in ("postgres", "mysql"):
            CONFIG["backend"] = name
            BACKEND, IS_POSTGRES = name, name == "postgres"
            reset_pools()
    return path


def set_backend(name):
    """Switch the live back-end ("postgres" or "mysql") and remember it."""
    global BACKEND, IS_POSTGRES
    name = {"supabase": "postgres", "postgresql": "postgres", "pg": "postgres",
            "xampp": "mysql"}.get(str(name).lower(), str(name).lower())
    if name not in ("postgres", "mysql"):
        raise ValueError(f"unknown backend {name!r}")
    BACKEND, IS_POSTGRES = name, name == "postgres"
    reset_pools()
    save_config({"backend": name})
    return name


def reset_pools():
    """Drop pooled Postgres connections (after a config/back-end change)."""
    global _pg_pool
    with _pg_pool_lock:
        pool, _pg_pool = _pg_pool, None
    if pool is not None:
        for conn, _ in list(pool._idle):
            try: conn.close()
            except Exception: pass


# ─── MySQL back-end (original behaviour) ───────────────────

class _MySQLConn:
    """pymysql connection exposing the mysql-connector style API."""
    def __init__(self, conn, dict_cursor_cls):
        self._conn = conn
        self._dict = dict_cursor_cls

    def cursor(self, dictionary=False, **kwargs):
        return self._conn.cursor(self._dict) if dictionary else self._conn.cursor()

    def commit(self):   self._conn.commit()
    def rollback(self): self._conn.rollback()
    def close(self):    self._conn.close()


def _mysql_connect():
    import pymysql
    import pymysql.cursors
    c = CONFIG["mysql"]
    conn = pymysql.connect(
        host=c["host"], port=int(c["port"]), user=c["user"],
        password=c["password"], database=c["database"],
        charset=c.get("charset", "utf8mb4"), autocommit=False,
        connect_timeout=5,
    )
    return _MySQLConn(conn, pymysql.cursors.DictCursor)


# ─── PostgreSQL / Supabase back-end ────────────────────────

# every table has an integer "id" primary key
_ID_TABLES = {"app_users", "bookings", "day_wise", "license",
              "place_zones", "rates", "users", "zones"}

_RE_ALTER_AI   = re.compile(r"ALTER\s+TABLE\s+`?(\w+)`?\s+AUTO_INCREMENT\s*=\s*(\d+)", re.I)
_RE_DATEFMT_YM = re.compile(r"DATE_FORMAT\(\s*([^,()]+?)\s*,\s*'%Y-%m'\s*\)", re.I)
_RE_UNSIGNED   = re.compile(r"\bAS\s+UNSIGNED\b", re.I)
_RE_LIKE       = re.compile(r"\bLIKE\b", re.I)
_RE_INSERT     = re.compile(r"^\s*INSERT\s+INTO\s+`?(\w+)`?", re.I)
_RE_USERNAME   = re.compile(r"\busername\s*=\s*%s", re.I)


def _translate(sql, want_returning=True):
    """MySQL dialect -> PostgreSQL.  Returns (sql, appended_returning_id)."""
    sql = sql.replace("`", '"')

    m = _RE_ALTER_AI.search(sql)
    if m:                                    # reset the id sequence
        return (f"SELECT setval(pg_get_serial_sequence('{m.group(1)}','id'), "
                f"{int(m.group(2))}, false)"), False

    sql = _RE_DATEFMT_YM.sub(r"to_char(\1, 'YYYY-MM')", sql)
    sql = _RE_UNSIGNED.sub("AS INTEGER", sql)
    sql = _RE_LIKE.sub("ILIKE", sql)         # MySQL LIKE is case-insensitive
    sql = _RE_USERNAME.sub("LOWER(username) = LOWER(%s)", sql)   # ...and so was username = ?

    returning = False
    if want_returning:
        m = _RE_INSERT.match(sql)
        if m and m.group(1).lower() in _ID_TABLES and "RETURNING" not in sql.upper():
            sql = sql.rstrip().rstrip(";") + " RETURNING id"
            returning = True
    return sql, returning


class _PgCursor:
    """Looks like a pymysql cursor on top of a psycopg2 one."""
    def __init__(self, cur):
        self._c = cur
        self.lastrowid = None

    def execute(self, sql, params=None):
        sql, returning = _translate(sql)
        self._c.execute(sql, params)
        if returning:
            row = self._c.fetchone()
            if row is None:
                self.lastrowid = None
            elif isinstance(row, dict):
                self.lastrowid = next(iter(row.values()))
            else:
                self.lastrowid = row[0]
        return self

    def executemany(self, sql, seq_of_params):
        from psycopg2.extras import execute_batch
        sql, _ = _translate(sql, want_returning=False)
        execute_batch(self._c, sql, list(seq_of_params), page_size=200)

    def fetchone(self):          return self._c.fetchone()
    def fetchall(self):          return self._c.fetchall()
    def fetchmany(self, n=None): return self._c.fetchmany(n) if n else self._c.fetchmany()
    def close(self):             self._c.close()

    @property
    def rowcount(self):    return self._c.rowcount
    @property
    def description(self): return self._c.description

    def __iter__(self):    return iter(self._c)
    def __enter__(self):   return self
    def __exit__(self, *a): self.close()


class _PgPool:
    """Tiny thread-safe pool.  Opening a TLS connection to Supabase costs
    several hundred ms, so connections are reused between requests."""
    IDLE_PING_SECS = 20

    def __init__(self):
        self._lock  = threading.Lock()
        self._idle  = []                       # [(conn, last_used)]
        self._max   = int(CONFIG["postgres"].get("pool_size", 4))

    def _open(self):
        import psycopg2
        c = CONFIG["postgres"]
        url = (c.get("url") or "").strip()
        if not url or "[YOUR-PASSWORD]" in url or "YOUR_PASSWORD" in url:
            raise RuntimeError(
                "Supabase is not configured. Put your connection string in "
                "db_config.json -> postgres.url (next to ZXpress.exe).")
        kw = dict(connect_timeout=int(c.get("connect_timeout", 15)),
                  keepalives=1, keepalives_idle=30,
                  keepalives_interval=10, keepalives_count=3)
        if "sslmode" not in url:
            kw["sslmode"] = "require"
        conn = psycopg2.connect(url, **kw)
        conn.autocommit = False
        return conn

    def get(self):
        while True:
            with self._lock:
                item = self._idle.pop() if self._idle else None
            if item is None:
                return self._open()
            conn, last = item
            if conn.closed:
                continue
            if time.time() - last > self.IDLE_PING_SECS:
                try:
                    cur = conn.cursor(); cur.execute("SELECT 1"); cur.close(); conn.rollback()
                except Exception:
                    try: conn.close()
                    except Exception: pass
                    continue
            return conn

    def put(self, conn):
        try:
            if conn.closed:
                return
            conn.rollback()                    # never hand back an open transaction
        except Exception:
            try: conn.close()
            except Exception: pass
            return
        with self._lock:
            if len(self._idle) < self._max:
                self._idle.append((conn, time.time()))
                return
        conn.close()


_pg_pool = None
_pg_pool_lock = threading.Lock()


def _get_pg_pool():
    global _pg_pool
    with _pg_pool_lock:
        if _pg_pool is None:
            _pg_pool = _PgPool()
        return _pg_pool


class _PgConn:
    def __init__(self, conn, pool):
        self._conn, self._pool = conn, pool

    def cursor(self, dictionary=False, **kwargs):
        if dictionary:
            from psycopg2.extras import RealDictCursor
            return _PgCursor(self._conn.cursor(cursor_factory=RealDictCursor))
        return _PgCursor(self._conn.cursor())

    def commit(self):   self._conn.commit()
    def rollback(self): self._conn.rollback()

    def close(self):
        if self._conn is not None:
            self._pool.put(self._conn)
            self._conn = None


# ─── Public API ────────────────────────────────────────────

_last_autostart = 0.0


def get_db_connection():
    """Fresh connection (pooled for Postgres) with the cursor(dictionary=True) /
    commit() / close() API the whole app uses.

    Raises DatabaseUnavailable (never a raw driver error) when the active
    database cannot be reached.  For XAMPP it first tries - at most once every
    3 min - to start MySQL itself, so the first login "just works"."""
    global _last_autostart
    try:
        if IS_POSTGRES:
            pool = _get_pg_pool()
            return _PgConn(pool.get(), pool)
        return _mysql_connect()
    except Exception as first_error:
        if not IS_POSTGRES and CONFIG.get("auto_start_mysql", True) \
                and time.time() - _last_autostart > 180:
            _last_autostart = time.time()
            try:
                import dbtools
                ok, _msg, _port = dbtools.ensure_mysql()
                if ok:
                    return _mysql_connect()
            except Exception:
                pass
        raise DatabaseUnavailable(
            f"{backend_label()} is not reachable: {first_error}", BACKEND) from first_error


def upsert_place_zone(cursor, place, zone, place_code):
    """INSERT a place_zones row, or update zone (and place_code when given)
    if `place` already exists.  Dialect-specific, so it lives here."""
    if IS_POSTGRES:
        cursor.execute(
            """INSERT INTO place_zones (place, zone, place_code)
               VALUES (%s, %s, %s)
               ON CONFLICT (place) DO UPDATE SET
                   zone = EXCLUDED.zone,
                   place_code = COALESCE(EXCLUDED.place_code, place_zones.place_code)""",
            (place, zone, place_code))
    else:
        cursor.execute(
            """INSERT INTO place_zones (place, zone, place_code)
               VALUES (%s, %s, %s)
               ON DUPLICATE KEY UPDATE
                   zone = VALUES(zone),
                   place_code = COALESCE(VALUES(place_code), place_code)""",
            (place, zone, place_code))


def backend_label():
    return "Supabase (PostgreSQL)" if IS_POSTGRES else "XAMPP (MySQL)"


def test_connection():
    """Returns (ok, message) - used by the launcher and `python db.py`."""
    try:
        conn = get_db_connection()
        cur = conn.cursor(dictionary=True)
        cur.execute("SELECT COUNT(*) AS n FROM bookings")
        n = cur.fetchone()["n"]
        cur.close(); conn.close()
        return True, f"{backend_label()} OK - {n} bookings"
    except Exception as e:
        return False, f"{backend_label()} FAILED: {e}"


def check_license():
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    cursor.execute("SELECT expiry_date FROM license ORDER BY id DESC LIMIT 1")
    row = cursor.fetchone()
    cursor.close()
    conn.close()

    if not row:
        return False, "No license found. Contact admin."

    expiry = row['expiry_date']
    if isinstance(expiry, str):
        expiry = datetime.strptime(expiry, "%Y-%m-%d").date()

    if date.today() >= expiry:
        return False, f"License expired on {expiry.strftime('%d-%m-%Y')}. Contact admin."

    return True, "Valid"


if __name__ == "__main__":
    ok, msg = test_connection()
    print(msg)
    sys.exit(0 if ok else 1)
