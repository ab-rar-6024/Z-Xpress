"""
Repair / rescue for a damaged XAMPP MySQL data folder  ("MySQL shutdown unexpectedly").

What `repair_xampp()` does - and it never deletes anything:

  1. stops XAMPP's MySQL (cleanly; a forced stop only for XAMPP's own mysqld.exe)
  2. zips the whole data folder  ->  db_backups\\xampp_data_before_repair_<time>.zip
  3. renames  data  ->  data_old_<time>
  ATTEMPT 1 "reset"
  4. builds a fresh  data  from  mysql\\backup
  5. copies the user's database folders + ibdata1 from data_old (system folders and the
     damaged log files are left behind), starts MySQL, verifies the tables open
     -> failure = automatic rollback (data_old is put back)
  ATTEMPT 2 "deep rescue"  (only if attempt 1 failed)
  6. copies the original data to data_rescue_<time>, starts MySQL on that COPY in
     innodb_force_recovery mode (levels 1..6), dumps every user database to .sql
  7. rebuilds a clean data folder, imports the dump, verifies
     -> failure = automatic rollback again
  LAST RESORT: returns status "failed" with the reason, the rescue file and the advice
     to switch to Supabase.  The original data is always still there.

Everything that starts a MySQL on a folder other than XAMPP's real one uses a rewritten
temporary my.ini, so the real data folder can never be touched by a rescue instance.
"""
import datetime as _dt
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

import db
import dbtools

SYSTEM_DIRS = {"mysql", "performance_schema", "phpmyadmin", "test", "sys", "information_schema"}
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


# ═══════════════════════════════ paths ═══════════════════════════════

class Paths:
    """Where XAMPP's MySQL lives.  Tests build one pointing at scratch folders."""
    def __init__(self, mysqld, data=None, backup=None, ini=None):
        self.mysqld = mysqld
        self.bin = os.path.dirname(mysqld)
        self.root = os.path.dirname(self.bin)
        self.data = data or os.path.join(self.root, "data")
        self.backup = backup or os.path.join(self.root, "backup")
        self.ini = ini if ini is not None else os.path.join(self.bin, "my.ini")

    def exe(self, name):
        return os.path.join(self.bin, name)


def default_paths():
    m = dbtools.find_mysqld()
    return Paths(m) if m else None


def _norm(s):
    return (s or "").replace('"', "").replace("/", "\\").rstrip("\\").lower()


def _mb(n):
    return round(n / 1048576.0, 1)


def _dir_size(path):
    total = 0
    for r, _d, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(r, f))
            except OSError:
                pass
    return total


def _stamp():
    return _dt.datetime.now().strftime("%Y%m%d_%H%M%S")


def _user_db_dirs(data_dir):
    out = []
    try:
        for n in sorted(os.listdir(data_dir)):
            full = os.path.join(data_dir, n)
            if os.path.isdir(full) and n.lower() not in SYSTEM_DIRS and not n.lower().startswith(("data_", "#")):
                out.append(n)
    except OSError:
        pass
    return out


# ═══════════════════════════════ starting / stopping ═══════════════════════════════

def _ini_datadir(ini):
    try:
        with open(ini, encoding="utf-8", errors="replace") as f:
            for line in f:
                m = re.match(r'\s*datadir\s*=\s*(.+?)\s*$', line, re.I)
                if m:
                    return _norm(m.group(1))
    except OSError:
        pass
    return None


def _temp_ini(p, datadir, port):
    """A copy of my.ini whose data paths point at `datadir`, so a rescue/scratch instance
    can never open XAMPP's real data folder."""
    text = ""
    if p.ini and os.path.exists(p.ini):
        with open(p.ini, encoding="utf-8", errors="replace") as f:
            text = f.read()
    d = datadir.replace("\\", "/")
    tmp = tempfile.mkdtemp(prefix="zxmy_")
    sock = os.path.join(tmp, f"zx_{port}.sock").replace("\\", "/")
    if text:
        for key in ("datadir", "innodb_data_home_dir", "innodb_log_group_home_dir"):
            text = re.sub(rf'(?im)^\s*{key}\s*=.*$', f'{key}="{d}"', text)
        text = re.sub(r'(?im)^\s*port\s*=.*$', f'port={port}', text)
        text = re.sub(r'(?im)^\s*socket\s*=.*$', f'socket="{sock}"', text)
        text = re.sub(r'(?im)^\s*tmpdir\s*=.*$', f'tmpdir="{tmp.replace(chr(92), "/")}"', text)
    else:
        text = (f'[mysqld]\nport={port}\nbasedir="{p.root.replace(chr(92), "/")}"\ndatadir="{d}"\n'
                f'innodb_data_home_dir="{d}"\ninnodb_log_group_home_dir="{d}"\n'
                f'innodb_log_file_size=5M\ninnodb_buffer_pool_size=16M\n')
    path = os.path.join(tmp, "my.ini")
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


def _args(p, port, datadir=None, extra=()):
    """mysqld command line.  The real my.ini is used only when it points at the very
    data folder we are about to start; otherwise a rewritten temporary copy is used."""
    datadir = datadir or p.data
    if p.ini and os.path.exists(p.ini) and _ini_datadir(p.ini) == _norm(datadir):
        ini = p.ini
    else:
        ini = _temp_ini(p, datadir, port)
    return [p.mysqld, f"--defaults-file={ini}", f"--port={port}"] + list(extra)


def start_instance(p, port, datadir=None, extra=()):
    return subprocess.Popen(_args(p, port, datadir, extra), cwd=p.bin, creationflags=NO_WINDOW,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def wait_ready(port, proc, timeout=40):
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None and proc.poll() is not None:
            return False
        if dbtools.mysql_probe(port, timeout=2) is not None:
            return True
        time.sleep(1)
    return False


def _cred_args():
    c = db.CONFIG["mysql"]
    a = ["-u", c["user"]]
    if c.get("password"):
        a.append(f"--password={c['password']}")
    return a


def _pid_alive(pid):
    try:
        out = subprocess.check_output(["tasklist", "/FI", f"PID eq {int(pid)}", "/NH"], text=True,
                                      errors="replace", creationflags=NO_WINDOW, timeout=15)
        return str(int(pid)) in out
    except Exception:
        return False


def stop_instance(p, port, proc=None, timeout=25):
    """Stop an instance we started: clean shutdown first, forced only as a last resort."""
    admin = p.exe("mysqladmin.exe")
    if os.path.exists(admin):
        try:
            subprocess.run([admin, "--no-defaults", "-h", "127.0.0.1", f"-P{port}"] + _cred_args() + ["shutdown"],
                           capture_output=True, timeout=45, creationflags=NO_WINDOW)
        except Exception:
            pass
    end = time.time() + timeout
    while time.time() < end:
        if proc is not None:
            if proc.poll() is not None:
                return True
        elif not dbtools.pid_on_port(port):
            return True
        time.sleep(0.5)
    if proc is not None and proc.poll() is None:
        try:
            proc.kill()
            proc.wait(10)
        except Exception:
            pass
    return not dbtools.pid_on_port(port)


def _xampp_pids(p):
    """PIDs of mysqld.exe processes that belong to THIS data folder / ini (never another MySQL)."""
    code, out, _ = dbtools._ps(
        "Get-CimInstance Win32_Process -Filter \"Name='mysqld.exe'\" | ForEach-Object "
        "{ [string]$_.ProcessId + '|' + $_.ExecutablePath + '|' + $_.CommandLine }", timeout=25)
    pids = []
    want_ini = _norm(p.ini) if p.ini else None
    want_data = _norm(p.data)
    for line in out.splitlines():
        parts = line.split("|", 2)
        if len(parts) < 3:
            continue
        pid, exe, cmd = parts
        if _norm(exe) != _norm(p.mysqld):
            continue
        c = _norm(cmd)
        inidata = _ini_datadir(p.ini) if p.ini else None
        # a process belongs to us if its command line names our ini AND our ini points at our data,
        # or it names our data folder directly
        if (want_ini and want_ini in c and inidata == want_data) or want_data in c:
            try:
                pids.append(int(pid))
            except ValueError:
                pass
    return pids


def _ports_of_pid(pid):
    ports = []
    try:
        out = subprocess.check_output(["netstat", "-ano", "-p", "TCP"], text=True, errors="replace",
                                      creationflags=NO_WINDOW, timeout=15)
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[3].upper() == "LISTENING" and parts[4] == str(pid):
                try:
                    ports.append(int(parts[1].rsplit(":", 1)[1]))
                except ValueError:
                    pass
    except Exception:
        pass
    return ports


def stop_xampp_mysql(p, log=None):
    """Stop XAMPP's MySQL (clean shutdown; forced kill of that exact process only if it hangs).
    Returns True when no mysqld of this data folder is left."""
    log = log or (lambda m: None)
    pids = _xampp_pids(p)
    for pid in pids:
        ports = _ports_of_pid(pid)
        log(f"Stopping MySQL (PID {pid}, ports {ports or 'none'})...")
        if ports:
            admin = p.exe("mysqladmin.exe")
            if os.path.exists(admin):
                try:
                    subprocess.run([admin, "--no-defaults", "-h", "127.0.0.1", f"-P{ports[0]}"] + _cred_args() + ["shutdown"],
                                   capture_output=True, timeout=45, creationflags=NO_WINDOW)
                except Exception:
                    pass
        for _ in range(40):
            if not _pid_alive(pid):
                break
            time.sleep(0.5)
        if _pid_alive(pid):
            log(f"MySQL (PID {pid}) did not stop cleanly - ending that process.")
            subprocess.run(["taskkill", "/PID", str(pid), "/F"], capture_output=True, creationflags=NO_WINDOW)
            time.sleep(1)
    return not _xampp_pids(p)


# ═══════════════════════════════ diagnosis ═══════════════════════════════

_DAMAGE_RE = re.compile(
    r"corrupt|aria recovery failed|\[error\] innodb|innodb: error|assertion failure|got signal \d+|"
    r"marked as crashed|crash recovery failed|upgrade after a crash|missing log file|"
    r"log sequence number .{0,60}(?:in the future|greater than)|ib_logfile\d? (?:is of different size|is corrupt)|"
    r"unable to read page|innodb: page .{0,40}(?:corrupt|checksum)|plugin 'innodb' (?:init function )?returned error|"
    r"unknown/unsupported storage engine: innodb|can't open and lock privilege tables")


def _log_tail(data_dir, lines=40):
    best = None
    try:
        for n in os.listdir(data_dir):
            if n.lower().endswith(".err") or n.lower() == "mysql_error.log":
                fp = os.path.join(data_dir, n)
                if best is None or os.path.getmtime(fp) > os.path.getmtime(best):
                    best = fp
        if best:
            with open(best, "r", encoding="utf-8", errors="replace") as f:
                return "".join(f.readlines()[-lines:]).strip()
    except OSError:
        pass
    return ""


def diagnose(p=None):
    """Plain-English guess of what is wrong, from the error log and a few quick checks."""
    p = p or default_paths()
    default_data = p is not None and _norm(p.data) == _norm(os.path.join(p.root, "data"))
    if p and default_data:
        cfg_port = int(db.CONFIG["mysql"]["port"])
        if dbtools.mysql_probe(cfg_port, timeout=2) == "ours":
            return {"code": "healthy", "title": "XAMPP MySQL is running fine",
                    "detail": f"MySQL answers on port {cfg_port} and the database opens. No repair is needed.",
                    "repairable": False, "tail": ""}
    if not p:
        return {"code": "no_xampp", "title": "XAMPP was not found",
                "detail": "XAMPP MySQL (mysqld.exe) is not installed on any drive of this PC.", "repairable": False,
                "tail": ""}
    tail = _log_tail(p.data, 40)
    low = tail.lower()
    res = {"code": "unknown", "title": "Cause not clear from the log",
           "detail": "Try 'Repair XAMPP data'. If that does not help, create a rescue file and send it to support.",
           "repairable": True, "tail": tail[-1500:]}
    try:                                               # can the program even run?
        r = subprocess.run([p.mysqld, "--version"], capture_output=True, text=True, timeout=20, creationflags=NO_WINDOW)
        if r.returncode != 0:
            return {**res, "code": "runtime_missing", "repairable": False,
                    "title": "MySQL cannot run on this PC",
                    "detail": "mysqld.exe fails to start (exit code %s). A Visual C++ 2015-2022 runtime is probably "
                              "missing, or this Windows is too old. Install the runtime or use Supabase." % r.returncode}
    except Exception as e:
        return {**res, "code": "runtime_missing", "repairable": False, "title": "MySQL cannot run on this PC",
                "detail": f"mysqld.exe could not be started: {e}"}
    try:
        free = shutil.disk_usage(p.root).free
        if free < 200 * 1048576:
            return {**res, "code": "disk_full", "repairable": False, "title": "The disk is almost full",
                    "detail": f"Only {_mb(free)} MB free. Free some space first - MySQL cannot write."}
    except OSError:
        pass
    if "bind on tcp/ip port" in low or "address already in use" in low or "do you already have another mysqld" in low:
        return {**res, "code": "port_busy", "repairable": False, "title": "The MySQL port is used by another program",
                "detail": "Use 'Start MySQL' (it picks a free port) or change the port on the Database page."}
    if _DAMAGE_RE.search(low):
        return {**res, "code": "data_damaged", "title": "The XAMPP data folder looks damaged",
                "detail": "Usually caused by a power cut or closing the PC while MySQL was running. "
                          "'Repair XAMPP data' rebuilds it and keeps your databases."}
    if "pid file" in low or "mysql.pid" in low:
        return {**res, "code": "stale_pid", "title": "A leftover MySQL process file is blocking the start",
                "detail": "'Repair XAMPP data' clears it."}
    if "access denied" in low and "file" in low or "can't create/write" in low:
        return {**res, "code": "permissions", "title": "MySQL cannot write to its folder",
                "detail": "Run the app as Administrator, or check that antivirus is not blocking the xampp folder."}
    if not os.path.isdir(p.data):
        return {**res, "code": "no_data", "title": "The data folder is missing", "detail": f"{p.data} does not exist."}
    return res


# ═══════════════════════════════ helpers for the repair ═══════════════════════════════

class Step(Exception):
    pass


def _zip_dir(src, dest_zip, progress=None, skip_locked=True):
    skipped = []
    with zipfile.ZipFile(dest_zip, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        for r, _d, files in os.walk(src):
            for f in files:
                full = os.path.join(r, f)
                try:
                    z.write(full, os.path.relpath(full, os.path.dirname(src)))
                except (PermissionError, OSError):
                    if not skip_locked:
                        raise
                    skipped.append(os.path.relpath(full, src))
    return skipped


def _free_space_check(p, need_bytes, where):
    free = shutil.disk_usage(where).free
    if free < need_bytes:
        raise Step(f"Not enough free disk space: {_mb(need_bytes)} MB needed, {_mb(free)} MB free. "
                   f"Nothing was changed.")


def _build_fresh_data(p, old_dir, new_dir, user_dbs):
    """Fresh data folder from mysql\\backup + the user's databases + ibdata1 from the old one."""
    if not os.path.isdir(p.backup):
        raise Step(f"The XAMPP backup folder is missing: {p.backup}")
    shutil.copytree(p.backup, new_dir)
    for n in user_dbs:                                   # user databases only
        dest = os.path.join(new_dir, n)
        if os.path.exists(dest):
            shutil.rmtree(dest)
        shutil.copytree(os.path.join(old_dir, n), dest)
    ib = os.path.join(old_dir, "ibdata1")
    if os.path.exists(ib):
        shutil.copy2(ib, os.path.join(new_dir, "ibdata1"))
    for f in os.listdir(new_dir):                        # fresh logs/pids must not clash with the old ibdata1
        low = f.lower()
        if low.startswith(("ib_logfile", "ibtmp")) or low in ("mysql.pid",):
            try:
                os.remove(os.path.join(new_dir, f))
            except OSError:
                pass


def verify(port):
    """Open the app's database and read the tables.  Returns {table: rows}; raises if it is not usable."""
    import pymysql
    c = db.CONFIG["mysql"]
    conn = pymysql.connect(host="127.0.0.1", port=int(port), user=c["user"], password=c["password"],
                           database=c["database"], connect_timeout=8)
    counts = {}
    try:
        cur = conn.cursor()
        for t in dbtools.TABLES:
            try:
                if t == "bookings":                      # SUM(total_amount) is not indexed -> reads every data page
                    cur.execute("SELECT COUNT(*), COALESCE(SUM(total_amount),0), COALESCE(MAX(id),0) FROM `bookings`")
                else:
                    cur.execute(f"SELECT COUNT(*), COALESCE(MAX(id),0) FROM `{t}`")
                counts[t] = int(cur.fetchone()[0])
            except Exception:
                counts[t] = None
        if counts.get("bookings") is None or counts.get("app_users") is None:
            raise Step("The core tables (bookings, app_users) cannot be read.")
    finally:
        conn.close()
    return counts


def _rollback(p, old_dir, failed_label, ts, log):
    """Put the original folder back as `data`; keep the failed attempt for inspection."""
    if os.path.isdir(p.data):
        failed = f"{p.data}_{failed_label}_{ts}"
        os.rename(p.data, failed)
        log(f"Failed attempt kept as {os.path.basename(failed)}")
    os.rename(old_dir, p.data)
    log("Original data folder restored.")


# ═══════════════════════════════ attempt 2: deep rescue ═══════════════════════════════

def _dump_databases(p, port, dbs, out_sql, log):
    """Returns (size_bytes, insert_statements)."""
    exe = p.exe("mysqldump.exe")
    if not os.path.exists(exe) or not dbs:
        return 0, 0
    cmd = [exe, "--no-defaults", "-h", "127.0.0.1", f"-P{port}"] + _cred_args() + [
        "--default-character-set=utf8mb4", "--skip-lock-tables", "--force", "--databases"] + dbs
    with open(out_sql, "wb") as f:
        subprocess.run(cmd, stdout=f, stderr=subprocess.PIPE, timeout=1800, creationflags=NO_WINDOW)
    size = os.path.getsize(out_sql) if os.path.exists(out_sql) else 0
    inserts = 0
    try:
        with open(out_sql, "rb") as f:
            for line in f:
                if line.startswith(b"INSERT INTO"):
                    inserts += 1
    except OSError:
        pass
    log(f"Dump written: {_mb(size)} MB, {inserts} data block(s)")
    return size, inserts


def _deep_rescue(p, old_dir_name, ts, log, progress):
    """Start MySQL on a COPY of the damaged data in innodb_force_recovery mode and dump every
    user database.  Returns (sql_path, level) or (None, None)."""
    rescue = f"{p.data}_rescue_{ts}"
    log("Copying the damaged data for the rescue instance...")
    shutil.copytree(p.data, rescue)
    for f in os.listdir(rescue):                         # recoverable logs/pid: safe to drop on the copy
        low = f.lower()
        if low.startswith("aria_log") or low in ("mysql.pid",):
            try:
                os.remove(os.path.join(rescue, f))
            except OSError:
                pass
    dbs = _user_db_dirs(rescue)
    out_sql = os.path.join(dbtools._backups_dir(), f"xampp_rescue_dump_{ts}.sql")
    tmp_port = dbtools.find_free_port(3350)
    best = None                                          # (inserts, size, level, path)
    for level in range(1, 7):
        progress(60 + level * 4, f"Deep rescue: recovery level {level}...")
        log(f"Starting rescue instance (innodb_force_recovery={level}) on port {tmp_port}")
        proc = start_instance(p, tmp_port, datadir=rescue, extra=[f"--innodb_force_recovery={level}"])
        try:
            if wait_ready(tmp_port, proc, timeout=40):
                cand = out_sql.replace(".sql", f"_L{level}.sql")
                size, inserts = _dump_databases(p, tmp_port, dbs, cand, log)
                if size > 2000 and (best is None or (inserts, size) > (best[0], best[1])):
                    if best and os.path.exists(best[3]):
                        os.remove(best[3])
                    best = (inserts, size, level, cand)
                elif os.path.exists(cand):
                    os.remove(cand)
                if inserts > 0 and size > 20000:         # real data exported - good enough
                    break
        finally:
            stop_instance(p, tmp_port, proc)
        time.sleep(1)
    if not best:
        return None, None
    final = out_sql
    if os.path.exists(final):
        os.remove(final)
    os.rename(best[3], final)
    return final, best[2]


def _import_dump(p, port, sql_path, log):
    exe = p.exe("mysql.exe")
    with open(sql_path, "rb") as f:
        r = subprocess.run([exe, "--no-defaults", "-h", "127.0.0.1", f"-P{port}"] + _cred_args() +
                           ["--default-character-set=utf8mb4", "--force"], stdin=f, capture_output=True,
                           timeout=1800, creationflags=NO_WINDOW)
    return r.returncode == 0


# ═══════════════════════════════ the repair ═══════════════════════════════

def repair_xampp(progress=None, paths=None, port=None, log=None, force_deep=False):
    """Repair a damaged XAMPP data folder.  Returns a result dict (never raises for expected
    failures); `status` is "ok", "partial" or "failed".  The original data is never deleted."""
    progress = progress or (lambda pct, msg: None)
    notes = []

    def say(m):
        notes.append(m)
        if log:
            log(m)

    p = paths or default_paths()
    res = {"status": "failed", "attempts": [], "notes": notes, "message": ""}
    if not p:
        res["message"] = "XAMPP MySQL (mysqld.exe) was not found on this PC."
        return res
    ts = _stamp()
    old_dir = f"{p.data}_old_{ts}"
    res.update(data_old=old_dir)
    try:
        # ---------- diagnose + preconditions ----------
        progress(2, "Looking at what is wrong...")
        res["diagnosis"] = diagnose(p)
        if res["diagnosis"]["code"] in ("runtime_missing", "disk_full", "no_xampp"):
            res["message"] = res["diagnosis"]["title"] + ". " + res["diagnosis"]["detail"]
            return res
        if not os.path.isdir(p.data):
            raise Step("The XAMPP data folder does not exist - nothing to repair.")
        size = _dir_size(p.data)
        _free_space_check(p, int(size * 3.2) + 50 * 1048576, p.root)

        # ---------- stop ----------
        progress(6, "Stopping MySQL...")
        if not stop_xampp_mysql(p, say):
            raise Step("MySQL could not be stopped. Close it in the XAMPP Control Panel and try again. Nothing was changed.")

        # ---------- safety zip ----------
        progress(12, "Making a safety copy (zip) of the data folder...")
        zip_path = os.path.join(dbtools._backups_dir(), f"xampp_data_before_repair_{ts}.zip")
        skipped = _zip_dir(p.data, zip_path)
        res["zip"] = zip_path
        say(f"Safety copy: {os.path.basename(zip_path)} ({_mb(os.path.getsize(zip_path))} MB)")
        if skipped:
            say(f"{len(skipped)} file(s) were locked and not zipped (they stay in data_old).")

        # ---------- rename ----------
        progress(25, "Renaming the damaged data folder...")
        try:
            os.rename(p.data, old_dir)
        except OSError as e:
            raise Step(f"The data folder could not be renamed (is MySQL still running?): {e}")
        user_dbs = _user_db_dirs(old_dir)
        say(f"Databases found: {', '.join(user_dbs) or 'none'}")
        use_port = port or (db.CONFIG["mysql"]["port"] if not dbtools.port_listening(db.CONFIG["mysql"]["port"])
                            else dbtools.find_free_port(int(db.CONFIG["mysql"]["port"]) + 1))
        use_port = int(use_port)

        # ---------- attempt 1: reset ----------
        a1 = {"name": "reset", "ok": False, "detail": ""}
        res["attempts"].append(a1)
        if force_deep:
            a1["detail"] = "skipped (deep rescue requested)"
        else:
            progress(32, "Attempt 1: building a fresh data folder...")
            proc = None
            try:
                _build_fresh_data(p, old_dir, p.data, user_dbs)
                progress(45, "Attempt 1: starting MySQL...")
                proc = start_instance(p, use_port)
                if not wait_ready(use_port, proc, timeout=45):
                    raise Step("MySQL did not start on the rebuilt folder.")
                counts = verify(use_port)
                a1.update(ok=True, detail="Databases open normally.")
                res.update(status="ok", level="reset", counts=counts, port=use_port,
                           message=f"Repaired. {counts.get('bookings')} bookings are readable.")
                progress(100, "Done")
                _remember_port(use_port)
                return res
            except Exception as e:
                a1["detail"] = str(e)
                say(f"Attempt 1 failed: {e}")
                if proc is not None:
                    stop_instance(p, use_port, proc)
                time.sleep(1)
                _rollback(p, old_dir, "failed1", ts, say)
        if force_deep:
            os.rename(old_dir, p.data)                   # undo the rename; the deep rescue works on a copy

        # ---------- attempt 2: deep rescue ----------
        a2 = {"name": "deep rescue", "ok": False, "detail": ""}
        res["attempts"].append(a2)
        sql_path, level = _deep_rescue(p, os.path.basename(old_dir), ts, say, progress)
        if not sql_path:
            a2["detail"] = "No recovery level could read the databases."
            res["message"] = ("The data could not be recovered automatically. Your original data is untouched. "
                              "Switch to Supabase to keep working, and send the rescue file to support.")
            progress(100, "Failed")
            return res
        res["dump"] = sql_path
        say(f"Data exported at recovery level {level}.")
        progress(85, "Rebuilding a clean data folder and importing...")
        os.rename(p.data, old_dir)
        proc = None
        try:
            shutil.copytree(p.backup, p.data)
            proc = start_instance(p, use_port)
            if not wait_ready(use_port, proc, timeout=45):
                raise Step("MySQL did not start on the clean folder.")
            if not _import_dump(p, use_port, sql_path, say):
                say("The import reported warnings.")
            counts = verify(use_port)
            a2.update(ok=True, detail=f"Recovered through the dump (recovery level {level}).")
            warn = _deep_warnings(old_dir, counts)
            # a deep rescue may have skipped damaged pages, so it is NEVER reported as a clean "ok"
            res.update(status="partial", level=f"deep (level {level})", counts=counts, port=use_port, warnings=warn)
            res["message"] = (f"Recovered through a deep rescue (recovery level {level}): {counts.get('bookings')} bookings are readable. "
                              "Some rows may be missing - compare with Supabase or a backup before relying on it."
                              + (" WARNING: " + "; ".join(warn) + "." if warn else ""))
            progress(100, "Done")
            _remember_port(use_port)
            return res
        except Exception as e:
            a2["detail"] = str(e)
            say(f"Attempt 2 failed: {e}")
            if proc is not None:
                stop_instance(p, use_port, proc)
            time.sleep(1)
            _rollback(p, old_dir, "failed2", ts, say)
            res["message"] = ("Automatic recovery did not succeed. Your original data folder was restored exactly as it was; "
                              "the exported data is in " + os.path.basename(sql_path) + ". Switch to Supabase to keep working.")
            progress(100, "Failed")
            return res
    except Step as e:
        res["message"] = str(e)
        progress(100, "Stopped")
        return res
    except Exception as e:                               # unexpected: still leave the original in place
        res["message"] = f"Unexpected problem: {e}"
        try:
            if not os.path.isdir(p.data) and os.path.isdir(old_dir):
                os.rename(old_dir, p.data)
                say("Original data folder restored.")
        except Exception:
            pass
        progress(100, "Stopped")
        return res


def _old_table_bytes(old_dir, table):
    d = os.path.join(old_dir, db.CONFIG["mysql"]["database"])
    best = 0
    for ext in (".ibd", ".MYD"):
        fp = os.path.join(d, table + ext)
        if os.path.exists(fp):
            best = max(best, os.path.getsize(fp))
    return best


def _deep_warnings(old_dir, counts):
    """Plain-English warnings when the recovery looks suspiciously empty."""
    out = []
    for t in ("bookings", "rates", "zones", "app_users"):
        n = counts.get(t)
        if (n == 0 or n is None) and _old_table_bytes(old_dir, t) > 100 * 1024:
            out.append(f"{t}: recovered EMPTY although the damaged table file is {_mb(_old_table_bytes(old_dir, t))} MB")
    return out


def _remember_port(port):
    try:
        if int(db.CONFIG["mysql"]["port"]) != int(port):
            db.save_config({"mysql": {"port": int(port)}})
    except Exception:
        pass


# ═══════════════════════════════ rescue file ═══════════════════════════════

def create_rescue_file(progress=None, paths=None):
    """One zip to send to support: the data folder, my.ini, the error log and a diagnosis text."""
    progress = progress or (lambda pct, msg: None)
    p = paths or default_paths()
    if not p:
        raise RuntimeError("XAMPP MySQL (mysqld.exe) was not found on this PC.")
    ts = _stamp()
    out = os.path.join(dbtools._backups_dir(), f"xampp_rescue_{ts}.zip")
    progress(10, "Collecting information...")
    d = diagnose(p)
    info = [f"ZXpress rescue file {ts}", f"XAMPP mysqld: {p.mysqld}", f"Data folder: {p.data}",
            f"Windows: {sys.platform}", f"Cause guess: {d['code']} - {d['title']}", d["detail"], "",
            "--- last error-log lines ---", d.get("tail", ""), "", "--- data folder listing ---"]
    try:
        for n in sorted(os.listdir(p.data)):
            fp = os.path.join(p.data, n)
            info.append(f"{'<dir>' if os.path.isdir(fp) else _mb(os.path.getsize(fp))} {n}")
    except OSError as e:
        info.append(f"cannot list: {e}")
    progress(30, "Zipping the data folder (this can take a minute)...")
    skipped = []
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr("diagnosis.txt", "\n".join(str(x) for x in info))
        if p.ini and os.path.exists(p.ini):
            z.write(p.ini, "my.ini")
        for r, _dirs, files in os.walk(p.data):
            for f in files:
                full = os.path.join(r, f)
                try:
                    z.write(full, os.path.join("data", os.path.relpath(full, p.data)))
                except (PermissionError, OSError):
                    skipped.append(os.path.relpath(full, p.data))
        if skipped:
            z.writestr("skipped_locked_files.txt", "\n".join(skipped))
    progress(100, "Done")
    return {"path": out, "size_mb": _mb(os.path.getsize(out)), "skipped": len(skipped), "diagnosis": d}
