"""
ZXpress database tools:  XAMPP starter, backups, and Supabase <-> XAMPP copy.

Used by the launcher (start MySQL), by app.py (/database and /recovery pages)
and by db.py (auto-start MySQL on first connect).

Rules every copy follows:
  * both sides are backed up to  db_backups/<time>_<what>/  BEFORE anything changes
  * the target is replaced inside ONE transaction - any error rolls it back
  * row counts and the bookings amount total are verified before COMMIT
  * if the target holds bookings the source does not, it refuses unless forced
"""
import datetime as _dt
import decimal
import json
import os
import re
import socket
import string
import subprocess
import sys
import threading
import time

import db

TABLES = ["app_users", "license", "place_zones", "zones",
          "rates", "users", "day_wise", "bookings"]

DIRECTIONS = {
    "mysql_to_postgres": ("mysql", "postgres"),     # XAMPP    -> Supabase
    "postgres_to_mysql": ("postgres", "mysql"),     # Supabase -> XAMPP
}
LABEL = {"mysql": "XAMPP", "postgres": "Supabase"}


class TransferBlocked(Exception):
    """Copy refused for a safety reason (needs the admin to confirm)."""


# ═══════════════════════════ XAMPP / MySQL ═══════════════════════════

def find_mysqld():
    """Path of XAMPP's mysqld.exe, or None."""
    cands = []
    root = db.CONFIG.get("xampp_path") or os.environ.get("XAMPP_HOME")
    if root:
        cands.append(os.path.join(root, "mysql", "bin", "mysqld.exe"))
    for d in string.ascii_uppercase:
        for sub in ("xampp", r"Program Files\xampp", r"Program Files (x86)\xampp"):
            cands.append(f"{d}:\\{sub}\\mysql\\bin\\mysqld.exe")
    for p in cands:
        if os.path.exists(p):
            return p
    return None


def listening_ports():
    """Set of TCP ports in LISTENING state (one fast netstat call), or None.
    Probing closed ports with sockets is slow on Windows (~0.5 s each)."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"], timeout=10, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stderr=subprocess.DEVNULL)
    except Exception:
        return None
    ports = set()
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            try:
                ports.add(int(parts[1].rsplit(":", 1)[1]))
            except (IndexError, ValueError):
                pass
    return ports


def port_listening(port, host="127.0.0.1", timeout=0.6):
    lp = listening_ports()
    if lp is not None:
        return int(port) in lp
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def mysql_probe(port, timeout=3):
    """"ours" = a MySQL that accepts our login AND has our database,
    "server" = accepts our login but the database does not exist yet,
    None = nothing usable on that port."""
    try:
        import pymysql
        c = db.CONFIG["mysql"]
        host = c.get("host", "localhost")
        host = "127.0.0.1" if host in ("localhost", "") else host
        conn = pymysql.connect(host=host, port=int(port),
                               user=c["user"], password=c["password"], connect_timeout=timeout)
        try:
            cur = conn.cursor()
            cur.execute("SHOW DATABASES LIKE %s", (c["database"],))
            return "ours" if cur.fetchone() else "server"
        finally:
            conn.close()
    except Exception:
        return None


def mysql_ok(port):
    """(True, "") when a MySQL on `port` is usable (see mysql_probe)."""
    r = mysql_probe(port)
    return (r is not None), ("" if r else "no usable MySQL on this port")


def find_free_port(start, busy=None):
    busy = busy or port_listening
    for p in range(int(start), int(start) + 40):
        if not busy(p):
            return p
    raise RuntimeError("no free port found near %s" % start)


def _error_log_tail(mysqld, lines=14):
    data_dir = os.path.join(os.path.dirname(os.path.dirname(mysqld)), "data")
    best = None
    try:
        for n in os.listdir(data_dir):
            if n.lower().endswith(".err") or n.lower() == "mysql_error.log":
                p = os.path.join(data_dir, n)
                if best is None or os.path.getmtime(p) > os.path.getmtime(best):
                    best = p
        if best:
            with open(best, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:]).strip()
    except Exception:
        pass
    return ""


def explain_error(tail):
    t = (tail or "").lower()
    if "bind on tcp/ip port" in t or "address already in use" in t or "do you already have another mysqld" in t:
        return "The MySQL port is already used by another program."
    if "aria recovery failed" in t or "corrupt" in t or "innodb: error" in t or "tablespace" in t and "error" in t:
        return "The XAMPP data folder looks damaged (often after a power cut)."
    if "pid file" in t or "mysql.pid" in t:
        return "A stale MySQL process/pid file is blocking the start."
    if "can't create/write" in t or "access denied" in t and "file" in t:
        return "MySQL cannot write to its folder (permissions)."
    return ""


def ensure_mysql(log=None):
    """Make sure XAMPP MySQL is running and reachable.  Returns (ok, message, port).

    1. already answering on the configured port        -> done
    2. answering on a nearby port (started earlier)    -> adopt that port
    3. configured port busy with something else        -> start on a free port
    4. start mysqld.exe directly (no Control Panel, no admin rights)"""
    log = log or (lambda m: None)
    _started = [False]                             # True once we launched mysqld ourselves
    cfg_port = int(db.CONFIG["mysql"]["port"])
    lp = listening_ports()

    def busy(p):
        return (int(p) in lp) if lp is not None else port_listening(p)

    def _ready(p, prefix=""):
        if lp is not None and not busy(p) and not _started[0]:
            return None                            # nothing listens there - skip the slow probe
        state = mysql_probe(p, timeout=2)
        if state is None:
            return None
        extra = ("" if state == "ours" else
                 f" (database '{db.CONFIG['mysql']['database']}' does not exist yet - "
                 f"copy your data from Supabase on the Database page)")
        return True, prefix + f"MySQL is running on port {p}." + extra, p

    r = _ready(cfg_port)
    if r:
        return r

    for p in range(3306, 3322):                       # started earlier on another port?
        if p != cfg_port and busy(p):
            r = _ready(p)
            if r and mysql_probe(p) == "ours":
                db.save_config({"mysql": {"port": p}})
                log(f"MySQL found on port {p}")
                return True, f"MySQL was already running on port {p}; using it.", p

    mysqld = find_mysqld()
    if not mysqld:
        return False, ("XAMPP MySQL (mysqld.exe) was not found on any drive. "
                       "Install XAMPP, or switch to Supabase."), cfg_port

    note = ""
    port = cfg_port
    if busy(cfg_port):
        port = find_free_port(cfg_port + 1, busy)
        note = f"Port {cfg_port} is used by another program - starting MySQL on {port}. "
        log(note)

    bindir = os.path.dirname(mysqld)
    ini = os.path.join(bindir, "my.ini")
    args = [mysqld] + ([f"--defaults-file={ini}"] if os.path.exists(ini) else []) + [f"--port={port}"]
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    log("Starting MySQL: " + " ".join(args))
    try:
        proc = subprocess.Popen(args, cwd=bindir, creationflags=flags,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        return False, f"Could not launch mysqld.exe: {e}", port

    _started[0] = True
    deadline = time.time() + float(db.CONFIG.get("mysql_start_timeout", 35))
    while time.time() < deadline:                 # hard limit: never hang the app
        time.sleep(1)
        r = _ready(port, note)
        if r:
            if port != cfg_port:
                db.save_config({"mysql": {"port": port}})
            return r
        if proc.poll() is not None:
            break
    db._last_autostart = time.time()          # don't make every page wait for another try
    tail = _error_log_tail(mysqld)
    hint = explain_error(tail)
    msg = "MySQL did not start. " + (hint + " " if hint else "")
    if tail:
        msg += "Last log lines: " + tail.replace("\n", " | ")[-400:]
    return False, msg, port


def pid_on_port(port):
    """PID listening on TCP `port` (or None)."""
    try:
        out = subprocess.check_output(
            ["netstat", "-ano", "-p", "TCP"], timeout=10, text=True, errors="replace",
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), stderr=subprocess.DEVNULL)
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[0].upper() == "TCP" and parts[3].upper() == "LISTENING":
            if parts[1].rsplit(":", 1)[-1] == str(int(port)):
                try:
                    return int(parts[4])
                except ValueError:
                    return None
    return None


def process_info(pid):
    """(name, executable path) of a process; path may be '' for services we cannot read."""
    try:
        code, out, _ = _ps(f"$p = Get-CimInstance Win32_Process -Filter 'ProcessId={int(pid)}' "
                           f"-ErrorAction SilentlyContinue; if ($p) {{ $p.Name + '|' + $p.ExecutablePath }}", timeout=20)
        name, _, path = out.partition("|")
        return name.strip(), path.strip()
    except Exception:
        return "", ""


def _same_path(a, b):
    return bool(a and b) and os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


def change_mysql_port(new_port, log=None):
    """Move XAMPP's MySQL to `new_port`: stop it cleanly where it runs now, then
    start it on the new port.  Never touches a MySQL that is not XAMPP's.
    Returns (ok, message, port)."""
    log = log or (lambda m: None)
    try:
        new_port = int(new_port)
        if not 1024 <= new_port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return False, "Port must be a number from 1024 to 65535.", int(db.CONFIG["mysql"]["port"])

    mysqld = find_mysqld()
    cur = int(db.CONFIG["mysql"]["port"])
    if not mysqld:
        return False, "XAMPP MySQL (mysqld.exe) was not found on this PC.", cur

    # 1) where is XAMPP's MySQL running right now?
    running = None                                   # (port, pid)
    for p_ in sorted({cur, *range(3306, 3322), new_port}):
        pid = pid_on_port(p_)
        if pid:
            name, path = process_info(pid)
            if _same_path(path, mysqld):
                running = (p_, pid)
                break

    # 2) already there?
    if running and running[0] == new_port:
        db.save_config({"mysql": {"port": new_port}})
        return True, f"XAMPP MySQL is already running on port {new_port}.", new_port

    # 3) is the wanted port taken by something else?
    other = pid_on_port(new_port)
    if other and (not running or other != running[1]):
        name, path = process_info(other)
        who = name or "another program"
        if name and "mysqld" in name.lower() and not path:
            who += " (a different MySQL / Windows service)"
        free = find_free_port(new_port + 1)
        return False, (f"Port {new_port} is already used by {who} (PID {other}) - it was NOT changed. "
                       f"Pick another number, for example {free}."), cur

    # 4) stop XAMPP's MySQL cleanly where it runs
    if running:
        old_port, old_pid = running
        log(f"Stopping XAMPP MySQL on port {old_port}...")
        c = db.CONFIG["mysql"]
        admin = os.path.join(os.path.dirname(mysqld), "mysqladmin.exe")
        stopped = False
        if os.path.exists(admin):
            args = [admin, "--host=127.0.0.1", f"--port={old_port}", f"--user={c['user']}", "shutdown"]
            if c.get("password"):
                args.insert(-1, f"--password={c['password']}")
            try:
                subprocess.run(args, capture_output=True, timeout=40,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            except Exception:
                pass
            for _ in range(40):                      # up to ~20 s for a clean shutdown
                if not pid_on_port(old_port):
                    stopped = True
                    break
                time.sleep(0.5)
        if not stopped:
            return False, (f"Could not stop MySQL on port {old_port} cleanly, so it was left running. "
                           f"Close it from the XAMPP Control Panel (Stop on the MySQL row) and try again."), old_port
        time.sleep(1)

    # 5) start it on the new port
    db.save_config({"mysql": {"port": new_port}})
    ok, msg, port = ensure_mysql(log)
    if ok:
        pre = f"MySQL was stopped on port {running[0]} and " if running else ""
        return True, pre + f"started on port {port}.", port
    return False, msg, port


# ═══════════════════════════ raw connections ═══════════════════════════

def raw_connect(backend):
    if backend == "mysql":
        import pymysql
        import pymysql.cursors
        c = db.CONFIG["mysql"]
        kw = dict(host=c.get("host", "localhost"), port=int(c["port"]),
                  user=c["user"], password=c["password"], charset="utf8mb4",
                  autocommit=False, connect_timeout=8,
                  cursorclass=pymysql.cursors.DictCursor)
        try:
            return pymysql.connect(database=c["database"], **kw)
        except pymysql.err.OperationalError as e:
            if e.args and e.args[0] == 1049:          # unknown database -> create it
                boot = pymysql.connect(**{**kw, "autocommit": True})
                try:
                    boot.cursor().execute("CREATE DATABASE IF NOT EXISTS `%s` "
                                          "CHARACTER SET utf8mb4" % c["database"].replace("`", ""))
                finally:
                    boot.close()
                return pymysql.connect(database=c["database"], **kw)
            raise
    import psycopg2
    url = (db.CONFIG["postgres"].get("url") or "").strip()
    if not url or "YOUR-PASSWORD" in url.upper():
        raise RuntimeError("Supabase connection string is not set in db_config.json")
    kw = {} if "sslmode" in url else {"sslmode": "require"}
    conn = psycopg2.connect(url, connect_timeout=15, **kw)
    conn.autocommit = False
    return conn


def _cursor(conn, backend):
    if backend == "postgres":
        from psycopg2.extras import RealDictCursor
        return conn.cursor(cursor_factory=RealDictCursor)
    return conn.cursor()


def _q(backend, name):
    return f'"{name}"' if backend == "postgres" else f"`{name}`"


def _backups_dir():
    d = os.path.join(db._app_dir(), "db_backups")
    if not db._dir_writable(d):
        d = os.path.join(db._user_dir(), "db_backups")
        os.makedirs(d, exist_ok=True)
    return d


# ═══════════════════════════ summary ═══════════════════════════


def _friendly_error(backend, e):
    msg = (str(e).strip().splitlines()[0][:300] if str(e).strip() else repr(e))
    low = msg.lower()
    if backend == "postgres" and ("tenant or user not found" in low or "timeout expired" in low
                                  or "timed out" in low or "could not translate host" in low
                                  or "connection refused" in low or "server closed the connection" in low):
        msg += (" | The free Supabase project is probably PAUSED (it pauses after about a week without use). "
                "Open the Supabase dashboard and click 'Restore project', wait a minute or two, then try again.")
    return msg


def summary(backend):
    """What is in `backend` right now (never raises)."""
    out = {"backend": backend, "label": LABEL[backend], "ok": False}
    conn = None
    try:
        conn = raw_connect(backend)
        cur = _cursor(conn, backend)
        tables = {}
        for t in TABLES:
            try:
                cur.execute(f"SELECT COUNT(*) AS n FROM {_q(backend, t)}")
                tables[t] = int(cur.fetchone()["n"])
            except Exception:
                conn.rollback()
                tables[t] = None
        cur.execute("SELECT MAX(booking_date) AS d, COALESCE(SUM(total_amount),0) AS s FROM " + _q(backend, "bookings"))
        r = cur.fetchone()
        out.update(ok=True, tables=tables,
                   latest=str(r["d"]) if r["d"] else "",
                   total_amount=float(r["s"]))
        conn.rollback()
    except Exception as e:
        out["error"] = _friendly_error(backend, e)
    finally:
        try:
            conn and conn.close()
        except Exception:
            pass
    return out


# ═══════════════════════════ read / write all ═══════════════════════════

def read_all(conn, backend):
    cur = _cursor(conn, backend)
    data = {}
    for t in TABLES:
        cur.execute(f"SELECT * FROM {_q(backend, t)} ORDER BY id")
        data[t] = [dict(r) for r in cur.fetchall()]
    conn.rollback()
    return data


def _json_default(o):
    if isinstance(o, decimal.Decimal):
        return str(o)
    if isinstance(o, _dt.datetime):
        if o.tzinfo is not None:
            o = o.astimezone().replace(tzinfo=None)
        return o.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(o, _dt.date):
        return o.isoformat()
    return str(o)


def backup(backend, label, note="", kind="snapshot"):
    """Dump every table of `backend` to db_backups/<time>_<label>_<backend>/ .
    `note` is the plain-English reason shown on the Backup page; `kind` is
    "overwritten" (this data was just deleted/replaced) or "snapshot".
    Returns the folder path."""
    conn = raw_connect(backend)
    try:
        data = read_all(conn, backend)
    finally:
        conn.close()
    folder = os.path.join(_backups_dir(),
                          f"{_dt.datetime.now():%Y%m%d_%H%M%S}_{label}_{backend}")
    os.makedirs(folder, exist_ok=True)
    for t, rows in data.items():
        with open(os.path.join(folder, f"{t}.json"), "w", encoding="utf-8") as f:
            json.dump(rows, f, default=_json_default, ensure_ascii=False)
    with open(os.path.join(folder, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump({"backend": backend, "label": label, "note": note, "kind": kind,
                   "created": _dt.datetime.now().isoformat(timespec="seconds"),
                   "counts": {t: len(r) for t, r in data.items()}}, f, indent=2)
    return folder


def _legacy_note(label, backend):
    me = LABEL.get(backend, backend)
    other = LABEL.get("postgres" if backend == "mysql" else "mysql")
    if label.startswith("before_overwrite_by"):
        return f"{me} data was deleted and replaced by {other} data"
    if label.startswith("before_copy_to"):
        return f"Snapshot of {me} taken before copying it to {other}"
    if label.startswith("before_switch"):
        return f"Snapshot of {me} taken before switching database"
    if label.startswith("before_restore"):
        return f"{me} data was replaced by restoring an older backup"
    return "Database backup"


def list_backups():
    out = []
    base = _backups_dir()
    for n in sorted(os.listdir(base), reverse=True):
        m = os.path.join(base, n, "manifest.json")
        if os.path.exists(m):
            try:
                with open(m, encoding="utf-8") as f:
                    mf = json.load(f)
                b = mf.get("backend")
                note = mf.get("note") or _legacy_note(mf.get("label", ""), b)
                kind = mf.get("kind") or ("overwritten" if "overwrite" in mf.get("label", "")
                                          or "restore" in mf.get("label", "") else "snapshot")
                out.append({"name": n, "backend": b, "db_label": LABEL.get(b, b),
                            "label": mf.get("label"), "note": note, "kind": kind,
                            "created": mf.get("created"), "counts": mf.get("counts", {})})
            except Exception:
                pass
    return out


def load_backup(name):
    base = os.path.realpath(_backups_dir())
    folder = os.path.realpath(os.path.join(base, name))
    if not folder.startswith(base + os.sep) or not os.path.isdir(folder):
        raise ValueError("backup not found")
    data = {}
    for t in TABLES:
        p = os.path.join(folder, f"{t}.json")
        data[t] = json.load(open(p, encoding="utf-8")) if os.path.exists(p) else []
    return data


def _for_target(v, backend):
    """Adapt one value read from the other database."""
    if isinstance(v, _dt.datetime):
        if backend == "mysql" and v.tzinfo is not None:
            return v.astimezone().replace(tzinfo=None)
        if backend == "postgres" and v.tzinfo is None:
            return v.astimezone()
    return v


def _target_columns(cur, backend, table):
    if backend == "postgres":
        cur.execute("SELECT column_name AS c FROM information_schema.columns "
                    "WHERE table_schema='public' AND table_name=%s", (table,))
    else:
        cur.execute("SELECT column_name AS c FROM information_schema.columns "
                    "WHERE table_schema=DATABASE() AND table_name=%s", (table,))
    return {r["c"] if isinstance(r, dict) else r[0] for r in cur.fetchall()}


def _ensure_schema(conn, backend):
    cur = conn.cursor()
    if backend == "postgres":
        path = os.path.join(db._bundle_dir(), "supabase_schema.sql")
        with open(path, encoding="utf-8") as f:
            cur.execute(f.read())
    else:
        path = os.path.join(db._bundle_dir(), "mysql_schema.sql")
        with open(path, encoding="utf-8") as f:
            sql = f.read()
        sql = re.sub(r"(?m)^\s*--.*$", "", sql)             # drop comment lines first
        for stmt in [x.strip() for x in sql.split(";") if x.strip()]:
            cur.execute(stmt)
    conn.commit()


def write_all(conn, backend, data, progress=None):
    """Replace every table of the target with `data` in ONE transaction.
    Verifies counts and the bookings amount before COMMIT; any problem rolls back."""
    progress = progress or (lambda p, m: None)
    cur = conn.cursor() if backend == "postgres" else conn.cursor()
    try:
        if backend == "postgres":
            from psycopg2.extras import execute_values
            cur.execute("TRUNCATE " + ", ".join(_q(backend, t) for t in TABLES) + " RESTART IDENTITY")
        else:
            for t in TABLES:
                cur.execute(f"DELETE FROM {_q(backend, t)}")

        for i, t in enumerate(TABLES):
            rows = data.get(t) or []
            progress(40 + int(50 * i / len(TABLES)), f"Writing {t} ({len(rows)} rows)...")
            if not rows:
                continue
            valid = _target_columns(cur, backend, t)
            cols = [c for c in rows[0].keys() if c in valid]
            tuples = [tuple(_for_target(r.get(c), backend) for c in cols) for r in rows]
            col_sql = ", ".join(_q(backend, c) for c in cols)
            if backend == "postgres":
                execute_values(cur, f'INSERT INTO "{t}" ({col_sql}) OVERRIDING SYSTEM VALUE VALUES %s',
                               tuples, page_size=1000)
            else:
                ph = ", ".join(["%s"] * len(cols))
                for k in range(0, len(tuples), 1000):
                    cur.executemany(f"INSERT INTO `{t}` ({col_sql}) VALUES ({ph})", tuples[k:k + 1000])

        progress(92, "Verifying...")
        dcur = _cursor(conn, backend)
        for t in TABLES:
            dcur.execute(f"SELECT COUNT(*) AS n FROM {_q(backend, t)}")
            n = int(dcur.fetchone()["n"])
            if n != len(data.get(t) or []):
                raise RuntimeError(f"verification failed: {t} has {n} rows, expected {len(data.get(t) or [])}")
        dcur.execute("SELECT COALESCE(SUM(total_amount),0) AS s FROM " + _q(backend, "bookings"))
        got = decimal.Decimal(str(dcur.fetchone()["s"]))
        want = sum((decimal.Decimal(str(r.get("total_amount") or 0)) for r in data.get("bookings") or []),
                   decimal.Decimal(0))
        if got != want:
            raise RuntimeError(f"verification failed: bookings total {got} != {want}")
        conn.commit()
    except Exception:
        conn.rollback()
        raise

    # keep the id counters moving forward (outside the data transaction)
    c2 = conn.cursor()
    for t in TABLES:
        if backend == "postgres":
            c2.execute(f"SELECT setval(pg_get_serial_sequence('{t}','id'), "
                       f'COALESCE((SELECT MAX(id) FROM "{t}"),0)+1, false)')
        else:
            c2.execute(f"SELECT COALESCE(MAX(id),0)+1 AS n FROM `{t}`")
            nxt = c2.fetchone()
            nxt = nxt["n"] if isinstance(nxt, dict) else nxt[0]
            c2.execute(f"ALTER TABLE `{t}` AUTO_INCREMENT = {int(nxt)}")
    conn.commit()


# ═══════════════════════════ preview / transfer / switch ═══════════════════════════

def _awb_set(data):
    return {r.get("awb_no") for r in data.get("bookings") or [] if r.get("awb_no")}


def preview(direction):
    """What a copy would do - shown to the admin before they confirm."""
    src_b, dst_b = DIRECTIONS[direction]
    s, d = summary(src_b), summary(dst_b)
    out = {"direction": direction, "source": s, "target": d, "target_only_awb": None,
           "can_run": s["ok"] and d["ok"] or (s["ok"] and dst_b == "mysql")}
    if s["ok"] and d["ok"]:
        try:
            sc, dc = raw_connect(src_b), raw_connect(dst_b)
            try:
                cs, cd = _cursor(sc, src_b), _cursor(dc, dst_b)
                cs.execute("SELECT awb_no FROM " + _q(src_b, "bookings"))
                cd.execute("SELECT awb_no FROM " + _q(dst_b, "bookings"))
                a = {r["awb_no"] for r in cs.fetchall() if r["awb_no"]}
                b = {r["awb_no"] for r in cd.fetchall() if r["awb_no"]}
                out["target_only_awb"] = len(b - a)
            finally:
                sc.close(); dc.close()
        except Exception as e:
            out["preview_error"] = str(e)
    return out


def transfer(direction, progress=None, force=False):
    """Replace the target database with the source's data.  Returns a report."""
    progress = progress or (lambda p, m: None)
    src_b, dst_b = DIRECTIONS[direction]
    t0 = time.time()

    if "mysql" in (src_b, dst_b):                 # XAMPP side must be running
        progress(1, "Starting XAMPP MySQL...")
        ok, msg, _ = ensure_mysql()
        if not ok:
            raise RuntimeError(msg)

    progress(3, f"Reading {LABEL[src_b]}...")
    sconn = raw_connect(src_b)
    try:
        data = read_all(sconn, src_b)
    finally:
        sconn.close()

    progress(12, f"Connecting to {LABEL[dst_b]}...")
    dconn = raw_connect(dst_b)
    try:
        _ensure_schema(dconn, dst_b)
        cur = _cursor(dconn, dst_b)
        cur.execute("SELECT awb_no FROM " + _q(dst_b, "bookings"))
        target_awb = {r["awb_no"] for r in cur.fetchall() if r["awb_no"]}
        dconn.rollback()
        only_target = len(target_awb - _awb_set(data))
        if only_target and not force:
            raise TransferBlocked(
                f"{LABEL[dst_b]} has {only_target} booking(s) that {LABEL[src_b]} does not have. "
                f"Copying would permanently replace them (a backup is kept). "
                f"Tick the confirmation box to proceed.")

        progress(20, f"Backing up {LABEL[src_b]} and {LABEL[dst_b]}...")
        b_src = backup(src_b, f"before_copy_to_{dst_b}",
                       note=f"Snapshot of {LABEL[src_b]} taken before copying it to {LABEL[dst_b]}",
                       kind="snapshot")
        b_dst = backup(dst_b, f"before_overwrite_by_{src_b}",
                       note=f"{LABEL[dst_b]} data was deleted and replaced by {LABEL[src_b]} data "
                            f"(this is the {LABEL[dst_b]} data as it was before)",
                       kind="overwritten")

        progress(35, f"Replacing {LABEL[dst_b]} data...")
        write_all(dconn, dst_b, data, progress)
    finally:
        try:
            dconn.close()
        except Exception:
            pass

    progress(100, "Done")
    return {"direction": direction,
            "counts": {t: len(r) for t, r in data.items()},
            "replaced_target_only_bookings": only_target,
            "backup_source": b_src, "backup_target": b_dst,
            "seconds": round(time.time() - t0, 1)}


def backup_origin(name):
    """Which database a backup came from ("mysql" / "postgres")."""
    base = os.path.realpath(_backups_dir())
    folder = os.path.realpath(os.path.join(base, name))
    if not folder.startswith(base + os.sep):
        raise ValueError("backup not found")
    with open(os.path.join(folder, "manifest.json"), encoding="utf-8") as f:
        return json.load(f).get("backend")


def _target_only_awb(conn, backend, data):
    cur = _cursor(conn, backend)
    cur.execute("SELECT awb_no FROM " + _q(backend, "bookings"))
    have = {r["awb_no"] for r in cur.fetchall() if r["awb_no"]}
    conn.rollback()
    return len(have - _awb_set(data))


def backup_preview(name, target=None):
    """What putting backup `name` into `target` (default: its own database) would do."""
    origin = backup_origin(name)
    target = target or origin
    data = load_backup(name)
    out = {"name": name, "origin": origin, "target": target,
           "backup_counts": {t: len(r) for t, r in data.items()},
           "target_summary": summary(target), "target_only_awb": None}
    if out["target_summary"]["ok"]:
        try:
            conn = raw_connect(target)
            try:
                out["target_only_awb"] = _target_only_awb(conn, target, data)
            finally:
                conn.close()
        except Exception as e:
            out["preview_error"] = str(e)
    return out


def restore_backup(name, target=None, progress=None, force=False):
    """Put a saved backup into the database it came from, or - with `target` -
    into the OTHER database (a "copy").  The target's current data is backed up
    first, and the write is all-or-nothing.  Refuses (TransferBlocked) when the
    target holds bookings the backup does not have, unless `force` is set."""
    progress = progress or (lambda p, m: None)
    origin = backup_origin(name)
    target = target or origin
    data = load_backup(name)
    if target == "mysql":
        ok, msg, _ = ensure_mysql()
        if not ok:
            raise RuntimeError(msg)
    progress(10, f"Connecting to {LABEL[target]}...")
    conn = raw_connect(target)
    try:
        _ensure_schema(conn, target)
        only = _target_only_awb(conn, target, data)
        if only and not force:
            raise TransferBlocked(
                f"{LABEL[target]} has {only} booking(s) that this backup does not have. "
                f"They would be permanently replaced (a backup of them is kept). "
                f"Tick the confirmation box to proceed.")
        progress(20, f"Backing up current {LABEL[target]} data...")
        if origin == target:
            why = f"{LABEL[target]} data was replaced by restoring its own backup {name}"
        else:
            why = f"{LABEL[target]} data was replaced by a copy of the {LABEL[origin]} backup {name}"
        b = backup(target, "before_restore", note=why + f" (this is the {LABEL[target]} data as it was before)",
                   kind="overwritten")
        write_all(conn, target, data, progress)
    finally:
        conn.close()
    progress(100, "Done")
    return {"restored": name, "target": target, "origin": origin, "backup_before": b,
            "counts": {t: len(r) for t, r in data.items()}}


def backup_now(backend):
    """Manual snapshot of one database (shows up in the backup list)."""
    if backend == "mysql":
        ok, msg, _ = ensure_mysql()
        if not ok:
            raise RuntimeError(msg)
    return backup(backend, "manual",
                  note=f"Manual backup of {LABEL[backend]} taken from the Backup page", kind="snapshot")


def switch_backend(to, progress=None):
    """Make `to` the live database.  Backs up the database being left (if it is
    reachable) and the one being joined; refuses if `to` is unreachable."""
    to = {"supabase": "postgres", "xampp": "mysql"}.get(to, to)
    if to not in ("mysql", "postgres"):
        raise ValueError("unknown database")
    progress = progress or (lambda p, m: None)
    cur_b = db.BACKEND

    progress(10, f"Checking {LABEL[to]}...")
    if to == "mysql":
        ok, msg, _ = ensure_mysql()
        if not ok:
            raise RuntimeError(msg)
    s = summary(to)
    if not s["ok"]:
        raise RuntimeError(f"{LABEL[to]} is not reachable, so the switch was cancelled. {s.get('error','')}")

    backups = {}
    for i, b in enumerate(dict.fromkeys([cur_b, to])):
        progress(30 + 25 * i, f"Backing up {LABEL[b]}...")
        try:
            if b == to and b != cur_b:
                why = f"Snapshot of {LABEL[b]} taken at the moment you switched to it (before using it)"
            elif b == cur_b and b != to:
                why = f"Snapshot of {LABEL[b]} taken before switching away from it to {LABEL[to]}"
            else:
                why = f"Snapshot of {LABEL[b]} taken before switching"
            backups[b] = backup(b, f"before_switch_to_{to}", note=why, kind="snapshot")
        except Exception as e:
            backups[b] = f"(not backed up: {str(e)[:120]})"

    progress(85, "Switching...")
    db.set_backend(to)
    progress(100, "Done")
    return {"from": cur_b, "to": to, "backups": backups, "target_summary": s}


# ═══════════════════════════ background jobs ═══════════════════════════

_job = {"state": "idle", "pct": 0, "message": "", "result": None, "error": None, "kind": ""}
_job_lock = threading.Lock()


def job_status():
    with _job_lock:
        return dict(_job)


def start_job(kind, func, *args, **kwargs):
    """Run func(progress=cb, ...) in a thread.  Only one job at a time."""
    with _job_lock:
        if _job["state"] == "running":
            raise RuntimeError("Another database job is already running.")
        _job.update(state="running", pct=0, message="Starting...", result=None, error=None, kind=kind)

    def progress(p, m):
        with _job_lock:
            _job.update(pct=int(p), message=m)

    def run():
        try:
            res = func(*args, progress=progress, **kwargs)
            with _job_lock:
                _job.update(state="done", pct=100, result=res, message="Done")
        except TransferBlocked as e:
            with _job_lock:
                _job.update(state="blocked", error=str(e), message=str(e))
        except Exception as e:
            with _job_lock:
                _job.update(state="error", error=str(e), message=str(e))

    threading.Thread(target=run, daemon=True).start()


# ═══════════════════════════ Supabase keep-alive ═══════════════════════════
# The free plan pauses a project after about a week with no activity.  A tiny
# request once a day (database + API) keeps it awake.  `ZXpress.exe --keepalive`
# does one ping and exits; a Windows scheduled task runs it every day.

TASK_NAME = "ZXpress Supabase KeepAlive"


def _supabase_ref():
    url = db.CONFIG["postgres"].get("url") or ""
    m = re.search(r"postgres\.([a-z0-9]{15,25})[:@]", url)
    return m.group(1) if m else None


def keepalive_log_path():
    d = db._app_dir()
    if not db._dir_writable(d):
        d = db._user_dir()
        os.makedirs(d, exist_ok=True)
    return os.path.join(d, "keepalive.log")


def keepalive(write_log=True):
    """One tiny database query + one API request.  Never raises."""
    import urllib.request
    import urllib.error
    out = {"time": _dt.datetime.now().isoformat(timespec="seconds"),
           "db_ok": False, "http": None, "ok": False, "message": ""}
    try:
        conn = raw_connect("postgres")
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.fetchone()
            conn.rollback()
            out["db_ok"] = True
        finally:
            conn.close()
    except Exception as e:
        out["message"] = _friendly_error("postgres", e)
    ref = _supabase_ref()
    if ref:
        try:
            req = urllib.request.Request(f"https://{ref}.supabase.co/rest/v1/",
                                         headers={"User-Agent": "ZXpress-keepalive"})
            with urllib.request.urlopen(req, timeout=15) as r:
                out["http"] = r.status
        except urllib.error.HTTPError as e:
            out["http"] = e.code                 # 401/404 still counts as an API request
        except Exception as e:
            out["message"] = (out["message"] + " " if out["message"] else "") + "API: " + str(e)[:80]
    out["ok"] = bool(out["db_ok"] or out["http"])
    if write_log:
        try:
            path = keepalive_log_path()
            line = (f"{out['time']}  {'OK  ' if out['ok'] else 'FAIL'}  db={'ok' if out['db_ok'] else 'no'}"
                    f"  api={out['http']}  {out['message']}".rstrip() + chr(10))
            old = []
            if os.path.exists(path):
                with open(path, encoding="utf-8", errors="replace") as f:
                    old = f.readlines()[-199:]
            with open(path, "w", encoding="utf-8") as f:
                f.writelines(old + [line])
        except Exception:
            pass
    return out


def keepalive_last():
    """Last line of keepalive.log (dict) or None."""
    try:
        with open(keepalive_log_path(), encoding="utf-8", errors="replace") as f:
            lines = [x.strip() for x in f.readlines() if x.strip()]
        if not lines:
            return None
        last = lines[-1]
        return {"line": last, "ok": "  OK  " in last, "when": last[:19]}
    except Exception:
        return None


def _ps(script, timeout=40):
    r = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                        "-Command", script], capture_output=True, text=True, timeout=timeout,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _keepalive_command():
    """(program, arguments) the scheduled task should run."""
    if getattr(sys, "frozen", False):
        return sys.executable, "--keepalive"
    return sys.executable, '"' + os.path.abspath(__file__) + '" --keepalive'


def keepalive_task_status(name=TASK_NAME):
    code, out, _ = _ps(f"$t = Get-ScheduledTask -TaskName '{name}' -ErrorAction SilentlyContinue; "
                       f"if ($t) {{ $i = $t | Get-ScheduledTaskInfo; "
                       f"'yes|' + $t.State + '|' + $i.LastRunTime + '|' + $i.NextRunTime }} else {{ 'no' }}")
    if out.startswith("yes|"):
        _, state, last, nxt = (out.split("|") + ["", "", ""])[:4]
        return {"installed": True, "state": state, "last_run": last, "next_run": nxt}
    return {"installed": False}


def keepalive_task_install(at="12:00", name=TASK_NAME):
    prog, args = _keepalive_command()
    esc = lambda x: x.replace("'", "''")
    script = (f"$a = New-ScheduledTaskAction -Execute '{esc(prog)}' -Argument '{esc(args)}'; "
              f"$t = New-ScheduledTaskTrigger -Daily -At '{at}'; "
              f"$s = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries "
              f"-DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 5); "
              f"Register-ScheduledTask -TaskName '{name}' -Action $a -Trigger $t -Settings $s "
              f"-Description 'Pings Supabase once a day so the free project is not paused.' -Force | Out-Null; 'ok'")
    code, out, err = _ps(script)
    if "ok" not in out:
        raise RuntimeError(err or out or "could not create the scheduled task")
    return keepalive_task_status(name)


def keepalive_task_remove(name=TASK_NAME):
    _ps(f"Unregister-ScheduledTask -TaskName '{name}' -Confirm:$false -ErrorAction SilentlyContinue")
    return keepalive_task_status(name)


if __name__ == "__main__":
    if "--keepalive" in sys.argv:
        res = keepalive()
        print(res)
        sys.exit(0 if res["ok"] else 1)
