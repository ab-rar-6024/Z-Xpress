import subprocess
import time
import os
import sys

# ── CRITICAL FIX: PyInstaller --windowed builds have NO console, so Windows
#    sets sys.stdout / sys.stderr to None (not closed file objects — actually
#    None). Any print() or click.echo() call anywhere in the app (including
#    Flask/Werkzeug's own startup banner, or the app's own print() warning
#    lines) raises AttributeError: 'NoneType' object has no attribute 'write'.
#    Because this happens inside a background thread, Python's default
#    thread-exception reporting ALSO tries to write to the (None) stderr,
#    that secondary failure is silently swallowed by the threading module,
#    and the whole thread dies with zero trace anywhere. This one block
#    fixes the entire "Flask silently never starts" failure mode.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import threading
import json

# ── PATHS (auto-patched by INSTALL.bat) ────────────────────
MYSQLD       = r"D:\xampp\mysql\bin\mysqld.exe"
MYSQL_INI    = r"D:\xampp\mysql\bin\my.ini"
MYSQL_BIN    = r"D:\xampp\mysql\bin\mysql.exe"

# ── STRIP any accidental quotes baked in by old installer ──
MYSQLD    = MYSQLD.strip('"').strip("'")
MYSQL_INI = MYSQL_INI.strip('"').strip("'")
MYSQL_BIN = MYSQL_BIN.strip('"').strip("'")

# ── DERIVED PATHS (always dynamic, never patched) ──────────
# When frozen by PyInstaller, __file__ points inside the temp
# _MEIxxxxx extraction folder, so PROJECT_DIR is only used for
# things that live NEXT TO the exe (like the .installed flag
# and flask_error.log) — never for templates/static/schema,
# which must go through resource_path() instead.
PROJECT_DIR = os.path.dirname(os.path.abspath(sys.argv[0])) \
    if getattr(sys, "frozen", False) else os.path.dirname(os.path.abspath(__file__))


def resource_path(rel):
    """
    Resolve a path to a bundled resource (templates, static, schema.sql,
    logo.ico, etc). When frozen by PyInstaller with --onefile, these are
    extracted at runtime into sys._MEIPASS. When running as a plain .pyw
    script, they simply live next to this file.
    """
    try:
        base = sys._MEIPASS  # type: ignore[attr-defined]
    except AttributeError:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


# Prefer the project's venv if it exists, else fall back to whatever Python
# launched us. NOTE: PYTHON_EXE / PIP are now ONLY used for the "running as
# a plain script, not frozen" case. The frozen exe never spawns python.exe
# or pip.exe — Flask runs in-thread and packages are bundled at build time.
_VENV_PY  = os.path.join(PROJECT_DIR, "venv", "Scripts", "python.exe")
_VENV_PYW = os.path.join(PROJECT_DIR, "venv", "Scripts", "pythonw.exe")

IS_FROZEN = getattr(sys, "frozen", False)

if IS_FROZEN:
    PYTHON     = sys.executable
    PYTHON_EXE = sys.executable
elif os.path.exists(_VENV_PY):
    PYTHON     = _VENV_PYW if os.path.exists(_VENV_PYW) else _VENV_PY
    PYTHON_EXE = _VENV_PY
else:
    PYTHON     = sys.executable                                    # python.exe or pythonw.exe
    PYTHON_EXE = PYTHON.replace("pythonw.exe", "python.exe")       # always python.exe for subprocesses

PIP = os.path.join(os.path.dirname(PYTHON_EXE), "pip.exe")
if not os.path.exists(PIP):
    PIP = os.path.join(os.path.dirname(PYTHON_EXE), "Scripts", "pip.exe")

# If MYSQL_BIN path is wrong/missing, derive it from MYSQLD
if not os.path.exists(MYSQL_BIN):
    MYSQL_BIN = os.path.join(os.path.dirname(MYSQLD), "mysql.exe")
if not os.path.exists(MYSQL_INI):
    MYSQL_INI = os.path.join(os.path.dirname(MYSQLD), "my.ini")

# Last resort: scan all drives A-Z for xampp mysql
MYSQL_BIN_DIR = os.path.dirname(MYSQLD)
if not os.path.exists(MYSQLD) or not os.path.exists(MYSQL_BIN):
    import string
    for _d in string.ascii_uppercase:
        _cand = _d + r':\xampp\mysql\bin\mysqld.exe'
        if os.path.exists(_cand):
            MYSQLD        = _cand
            MYSQL_BIN     = os.path.join(os.path.dirname(_cand), 'mysql.exe')
            MYSQL_INI     = os.path.join(os.path.dirname(_cand), 'my.ini')
            MYSQL_BIN_DIR = os.path.dirname(_cand)
            break

MYSQL_BIN_DIR = os.path.dirname(MYSQLD)                           # e.g. D:\xampp\mysql\bin
PROJECT       = PROJECT_DIR
ICON          = resource_path("logo.ico")


def _set_window_icon(win):
    """Give a Tk window the Z logo (title bar + taskbar) instead of the generic
    page icon.  Never raises - a missing icon must not stop the app."""
    try:
        if os.path.exists(ICON):
            win.iconbitmap(ICON)
    except Exception:
        pass
LOG           = os.path.join(PROJECT, "flask_error.log")
URL           = "http://127.0.0.1:8877"
INSTALL_FLAG  = os.path.join(PROJECT, ".installed")

# Only relevant when running as a plain .pyw script (not frozen).
# In the frozen exe these are already bundled, so do_install() skips
# this list entirely — see run_installer() below.
PACKAGES = ["flask", "pywebview", "pymysql", "psycopg2-binary", "mysql-connector-python", "reportlab", "pandas", "openpyxl"]

IMPORT_NAMES = {
    "pywebview":              "webview",
    "pymysql":                "pymysql",
    "psycopg2-binary":        "psycopg2",
    "mysql-connector-python": "mysql.connector",
    "reportlab":              "reportlab",
    "flask":                  "flask",
    "pandas":                 "pandas",
    "openpyxl":               "openpyxl",
}

# ── HELPERS ─────────────────────────────────────────────────
def is_package_installed(pkg):
    result = subprocess.run(
        [PYTHON_EXE, "-c", f"import {pkg}"],
        capture_output=True
    )
    return result.returncode == 0

def is_mysql_running():
    result = subprocess.run(
        ['tasklist', '/FI', 'IMAGENAME eq mysqld.exe'],
        capture_output=True, text=True
    )
    return 'mysqld.exe' in result.stdout

def is_flask_running(log_exception=False):
    # Force IPv4 explicitly, AND explicitly bypass any system-configured
    # proxy (corporate network policy / VPN client / some background app
    # setting Windows' system proxy). urllib.request respects system proxy
    # settings by default, even for 127.0.0.1 — this can cause the request
    # to hang/timeout against a proxy that can't route to loopback, making
    # a perfectly healthy local Flask server look "down" to this check.
    import urllib.request
    try:
        proxy_handler = urllib.request.ProxyHandler({})  # empty dict = no proxy at all
        opener = urllib.request.build_opener(proxy_handler)
        opener.open("http://127.0.0.1:8877", timeout=2)
        return True
    except Exception as e:
        if log_exception:
            try:
                with open(LOG, "a") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Health check failed: {type(e).__name__}: {e}\n")
            except Exception:
                pass
        return False

def is_installed():
    if not os.path.exists(INSTALL_FLAG):
        return False
    try:
        with open(INSTALL_FLAG, "r") as f:
            data = json.load(f)
            return data.get("installed") == True
    except Exception:
        return False

def mark_installed():
    with open(INSTALL_FLAG, "w") as f:
        json.dump({"installed": True, "version": "1.0"}, f)

# ── START MYSQL ─────────────────────────────────────────────
def start_mysql(log_fn, update_fn=None):
    """Returns False when the active database could not be reached/started."""
    import db as _db
    if _db.IS_POSTGRES:
        # Online database: no XAMPP / MySQL / local ports involved.
        if update_fn:
            update_fn("Connecting to online database...", 10)
        log_fn("Connecting to online database (Supabase)...")
        ok, msg = _db.test_connection()
        log_fn(("✓ " if ok else "✗ ") + msg)
        if update_fn:
            update_fn(("✓ " if ok else "✗ ") + "Online database" + (" connected" if ok else " unreachable"), 40)
        if not ok:
            try:
                with open(LOG, "a", encoding="utf-8") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
            except Exception:
                pass
        return ok
    # XAMPP: dbtools finds mysqld.exe, picks a free port if 3306 is busy,
    # starts it directly (no Control Panel / admin rights) and verifies it.
    if update_fn:
        update_fn("Starting Database (MySQL)...", 10)
    log_fn("Starting Database (MySQL)...")
    import dbtools
    ok, msg, port = dbtools.ensure_mysql(log_fn)
    log_fn(("✓ " if ok else "✗ ") + msg)
    if update_fn:
        update_fn(("✓ MySQL ready on port %s" % port) if ok else "✗ MySQL not started - opening recovery", 40)
    if not ok:
        try:
            with open(LOG, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except Exception:
            pass
    return ok

# ── START FLASK ─────────────────────────────────────────────
# UPDATED: Flask is now imported and run in-thread inside THIS process,
# instead of being spawned as a `python.exe app.py` subprocess. A frozen
# single-file exe has no separate python.exe to spawn, so the old
# subprocess.Popen([PYTHON_EXE, APP], ...) approach cannot work once
# packaged. Running in-thread works identically whether launcher.pyw is
# run as a plain script or as a frozen exe.
def start_flask(log_fn, update_fn=None):
    if update_fn:
        update_fn("Starting Flask...", 85)
    log_fn("Starting Flask...")

    try:
        # Make sure the bundled/adjacent app.py can be found on sys.path
        if PROJECT not in sys.path:
            sys.path.insert(0, PROJECT)
        from app import app as flask_app
    except Exception as e:
        try:
            with open(LOG, "w") as f:
                f.write(f"Import error: {e}\n")
        except Exception:
            pass
        log_fn(f"✗ Failed to import app.py: {e}")
        return False

    def run():
        # ── Raw socket pre-test ──────────────────────────────────────────
        # Bind a plain socket to the exact host:port BEFORE touching Flask/
        # Werkzeug at all. If this fails, the problem is 100% at the OS/
        # Windows level (e.g. a Hyper-V/WSL port exclusion range) and has
        # nothing to do with Flask, our code, or PyInstaller packaging.
        import socket
        import traceback as _tb
        try:
            _test_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            _test_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            _test_sock.bind(("127.0.0.1", 8877))
            _test_sock.close()
            with open(LOG, "a") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Raw socket bind test on 127.0.0.1:8877 SUCCEEDED\n")
        except Exception as e:
            with open(LOG, "a") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Raw socket bind test FAILED: {type(e).__name__}: {e}\n")
                f.write(_tb.format_exc() + "\n")
            return  # no point even trying Flask if the raw bind already failed

        try:
            with open(LOG, "a") as f:
                f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Calling flask_app.run() on port 8877...\n")
        except Exception:
            pass
        try:
            flask_app.run(host="127.0.0.1", port=8877, debug=False, use_reloader=False)
        except SystemExit as e:
            try:
                with open(LOG, "a") as f:
                    f.write(f"Flask exited via sys.exit(): {e}\n")
            except Exception:
                pass
        except Exception as e:
            try:
                with open(LOG, "a") as f:
                    f.write(f"Flask crashed: {e}\n")
                    f.write(_tb.format_exc() + "\n")
            except Exception:
                pass
        except BaseException as e:
            try:
                with open(LOG, "a") as f:
                    f.write(f"Flask crashed (unexpected): {type(e).__name__}: {e}\n")
                    f.write(_tb.format_exc() + "\n")
            except Exception:
                pass

    threading.Thread(target=run, daemon=True).start()

    # Wait up to 15s for Flask to actually respond
    for i in range(15):
        time.sleep(1)
        is_last_attempt = (i == 14)
        if is_flask_running(log_exception=is_last_attempt):
            log_fn("✓ Backend ready")
            if update_fn:
                update_fn("✓ Backend ready", 95)
            return True

    # Flask didn't start — read log and show error
    error_msg = ""
    try:
        with open(LOG, "r") as f:
            error_msg = f.read().strip()
    except Exception:
        pass

    log_fn("✗ Flask failed to start!")
    if error_msg:
        log_fn("ERROR LOG:")
        for line in error_msg.splitlines()[-20:]:
            log_fn(line)
    return False

# ── OPEN APP WINDOW ─────────────────────────────────────────
def open_app_window():
    import webview
    webview.create_window(
        "ST Courier System",
        URL,
        width=1280,
        height=800,
        resizable=True,
        fullscreen=False
    )
    webview.start(icon=ICON if os.path.exists(ICON) else None)

# ══════════════════════════════════════════════════════════
#   DESKTOP SHORTCUT HELPER
# ══════════════════════════════════════════════════════════
def create_desktop_shortcut(target_exe, icon_path=None, name="Z Xpress"):
    """
    Creates a Desktop .lnk pointing at target_exe.

    Uses PowerShell's own WScript.Shell COM object via subprocess, NOT
    Python's win32com.client. win32com relies on compiled DLLs
    (pywintypes*.dll, pythoncom*.dll) that PyInstaller's --hidden-import
    flag does not reliably bundle — the import (or the COM call itself)
    can fail inside the frozen exe even though it works fine in a normal
    dev environment. Shelling out to the system's own powershell.exe has
    zero bundling requirements and is exactly what INSTALL.bat already
    did successfully.

    IMPORTANT: the actual Desktop folder is resolved via WScript.Shell's
    own SpecialFolders("Desktop") property, NOT by hardcoding
    "%USERPROFILE%\\Desktop". When OneDrive's "Backup" / Known Folder
    Move feature is enabled, Windows silently redirects the real shell
    desktop to somewhere like %USERPROFILE%\\OneDrive\\Desktop — a
    hardcoded path would then create a working .lnk file that sits in a
    folder Explorer still shows you, but that is NOT what Windows
    actually renders as the visual desktop, so the icon appears to
    "not exist" even though it's really there. SpecialFolders("Desktop")
    always resolves to whichever folder is currently active.
    """
    icon     = icon_path if (icon_path and os.path.exists(icon_path)) else target_exe
    work_dir = os.path.dirname(target_exe)

    ps_script = (
        f'$s = New-Object -ComObject WScript.Shell; '
        f'$desktop = $s.SpecialFolders("Desktop"); '
        f'$sc = $s.CreateShortcut((Join-Path $desktop "{name}.lnk")); '
        f'$sc.TargetPath = "{target_exe}"; '
        f'$sc.WorkingDirectory = "{work_dir}"; '
        f'$sc.IconLocation = "{icon}"; '
        f'$sc.Description = "Z Xpress Courier System"; '
        f'$sc.Save(); '
        f'Write-Output $desktop'
    )
    result = subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps_script],
        capture_output=True, text=True, timeout=15,
        creationflags=subprocess.CREATE_NO_WINDOW
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"PowerShell shortcut creation failed "
            f"(exit {result.returncode}): {result.stderr.strip()}"
        )
    resolved_desktop = result.stdout.strip()
    shortcut_path = os.path.join(resolved_desktop, f"{name}.lnk") if resolved_desktop else None
    if not shortcut_path or not os.path.exists(shortcut_path):
        raise RuntimeError(
            f"Shortcut command ran but the .lnk file was not found "
            f"(resolved Desktop folder: {resolved_desktop or 'unknown'})"
        )

# ══════════════════════════════════════════════════════════
#   FIRST-RUN SETUP WIZARD — install location + desktop icon
# ══════════════════════════════════════════════════════════
# Shown exactly once, before the very first install begins. Lets the user
# pick where the app should live and whether to create a Desktop shortcut.
# Only meaningful for the frozen exe — a --onefile build is a single
# relocatable file, so "installing" it just means copying it somewhere
# sensible and (optionally) making a shortcut. This step is skipped
# entirely when running as a plain .pyw dev script.
TRANSIENT_FOLDER_NAMES = {"downloads", "desktop", "temp", "tmp", "appdata"}

def run_setup_wizard():
    if not IS_FROZEN:
        return True  # nothing to relocate when running as a plain script

    current_exe = os.path.abspath(sys.executable)
    cur_dir     = os.path.dirname(current_exe)
    cur_dir_name = os.path.basename(cur_dir).lower()

    # If already sitting in a sensible, permanent-looking folder, default to
    # staying right there (no copy needed). Otherwise (Desktop/Downloads/
    # Temp/etc — i.e. probably just extracted or run from a USB stick)
    # suggest a proper install folder under the user's profile.
    if cur_dir_name in TRANSIENT_FOLDER_NAMES or "temp" in cur_dir.lower():
        default_dir = os.path.join(os.environ.get("USERPROFILE", "C:\\"), "Z Xpress")
    else:
        default_dir = cur_dir

    wiz = tk.Tk()
    _set_window_icon(wiz)
    wiz.title("Z Xpress — Setup")
    wiz.geometry("540x270")
    wiz.resizable(False, False)
    wiz.configure(bg="#1a1a2e")
    wiz.eval('tk::PlaceWindow . center')

    tk.Label(wiz, text="Z Xpress Setup", font=("Arial", 18, "bold"),
             bg="#1a1a2e", fg="white").pack(pady=(18, 6))
    tk.Label(wiz, text="Choose where to install the application.",
             font=("Arial", 10), bg="#1a1a2e", fg="#aaaaaa").pack()

    path_var = tk.StringVar(value=default_dir)
    frm = tk.Frame(wiz, bg="#1a1a2e")
    frm.pack(pady=16, padx=24, fill="x")
    entry = tk.Entry(frm, textvariable=path_var, font=("Consolas", 10))
    entry.pack(side="left", fill="x", expand=True, ipady=4)

    def browse():
        chosen = filedialog.askdirectory(initialdir=path_var.get() or "C:\\")
        if chosen:
            base = os.path.basename(chosen.rstrip("\\/")).lower()
            path_var.set(chosen if base == "z xpress" else os.path.join(chosen, "Z Xpress"))

    tk.Button(frm, text="Browse...", command=browse).pack(side="left", padx=(8, 0))

    shortcut_var = tk.BooleanVar(value=True)
    tk.Checkbutton(
        wiz, text="Create a Desktop shortcut", variable=shortcut_var,
        bg="#1a1a2e", fg="white", selectcolor="#1a1a2e",
        activebackground="#1a1a2e", activeforeground="white",
        font=("Arial", 10)
    ).pack(pady=8)

    result = {"go": False}

    def do_install_click():
        result["go"] = True
        wiz.destroy()

    def do_cancel_click():
        wiz.destroy()
        os._exit(0)

    btn_frame = tk.Frame(wiz, bg="#1a1a2e")
    btn_frame.pack(pady=14)
    tk.Button(btn_frame, text="Install", width=14, bg="#198754", fg="white",
              command=do_install_click).pack(side="left", padx=6)
    tk.Button(btn_frame, text="Cancel", width=14,
              command=do_cancel_click).pack(side="left", padx=6)

    wiz.protocol("WM_DELETE_WINDOW", do_cancel_click)
    wiz.mainloop()

    if not result["go"]:
        return False

    target_dir = os.path.abspath(path_var.get().strip() or default_dir)
    try:
        os.makedirs(target_dir, exist_ok=True)
    except Exception as e:
        messagebox.showerror("Setup Error", f"Could not create folder:\n{target_dir}\n\n{e}")
        return False

    target_exe = os.path.join(target_dir, os.path.basename(current_exe))
    relocated  = False

    if os.path.normcase(target_exe) != os.path.normcase(current_exe):
        try:
            import shutil
            shutil.copy2(current_exe, target_exe)
            relocated = True
        except Exception as e:
            messagebox.showwarning(
                "Copy Failed",
                f"Could not copy to:\n{target_dir}\n\n{e}\n\nContinuing from the current location instead."
            )
            target_exe = current_exe
            target_dir = cur_dir

    shortcut_ok = True
    if shortcut_var.get():
        try:
            create_desktop_shortcut(target_exe, target_exe)   # exe has the icon embedded; _MEIPASS/logo.ico is a temp file that disappears
        except Exception as e:
            shortcut_ok = False
            try:
                with open(LOG, "a") as f:
                    f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] Desktop shortcut creation failed: {e}\n")
            except Exception:
                pass

    if relocated:
        # The copy at target_exe is now the real install. Everything this
        # process would do from here (writing .installed, the Excel backup,
        # flask_error.log) resolves relative to sys.executable — which for
        # THIS running process is still the old location. Rather than try
        # to relocate a live process, just tell the user and exit; the
        # actual first-time install (MySQL/schema/Flask setup) happens the
        # next time they launch from the new location or the Desktop icon.
        msg = (
            f"Z Xpress has been installed to:\n{target_dir}\n\n"
            f"Please use the Desktop icon (or that folder) to launch it "
            f"from now on. This window will now close."
        )
        if shortcut_var.get() and not shortcut_ok:
            msg += (
                "\n\nNote: the Desktop shortcut could not be created "
                f"automatically. You can create one yourself by right-"
                f"clicking:\n{target_exe}\nand choosing 'Send to > Desktop "
                f"(create shortcut)'."
            )
        messagebox.showinfo("Installed", msg)
        os._exit(0)

    return True

# ══════════════════════════════════════════════════════════
#   INSTALLER WINDOW (First Time Only)
# ══════════════════════════════════════════════════════════
def run_installer():
    inst = tk.Tk()
    _set_window_icon(inst)
    inst.title("ST Courier — Setup")
    inst.geometry("500x380")
    inst.resizable(False, False)
    inst.configure(bg="#1a1a2e")
    inst.eval('tk::PlaceWindow . center')

    tk.Label(inst, text="ST Courier System",
             font=("Arial", 20, "bold"),
             bg="#1a1a2e", fg="white").pack(pady=15)
    tk.Label(inst, text="First Time Setup — Please wait...",
             font=("Arial", 11),
             bg="#1a1a2e", fg="#aaaaaa").pack()

    status_var = tk.StringVar(value="Checking packages...")
    tk.Label(inst, textvariable=status_var,
             font=("Arial", 11, "bold"),
             bg="#1a1a2e", fg="#00d4ff").pack(pady=8)

    progress = ttk.Progressbar(inst, length=440, mode='determinate')
    progress.pack(pady=4)

    log_frame = tk.Frame(inst, bg="#0d0d1a")
    log_frame.pack(padx=20, pady=8, fill="both", expand=True)
    log_box = tk.Text(log_frame, height=10, bg="#0d0d1a",
                      fg="#00ff88", font=("Consolas", 9),
                      relief="flat", state="disabled")
    log_box.pack(fill="both", expand=True, padx=5, pady=5)

    def log(msg):
        log_box.config(state="normal")
        log_box.insert("end", f"  {msg}\n")
        log_box.see("end")
        log_box.config(state="disabled")
        inst.update_idletasks()

    def update_status(msg, value):
        status_var.set(msg)
        progress['value'] = value
        inst.update_idletasks()

    def do_install():
        # UPDATED: when frozen into a single exe, all packages are already
        # bundled by PyInstaller at build time — there is no pip.exe to call
        # and nothing to install. Only attempt live pip installs when running
        # as a plain, unfrozen .pyw script (e.g. during development).
        if IS_FROZEN:
            log("✓ All required packages are bundled into this application")
            update_status("Preparing...", 40)
        else:
            total = len(PACKAGES)
            for idx, pkg in enumerate(PACKAGES):
                import_name = IMPORT_NAMES.get(pkg, pkg)
                pct = int((idx / total) * 40)
                if is_package_installed(import_name):
                    log(f"✓ {pkg} already installed")
                    update_status(f"✓ {pkg} ready", pct)
                else:
                    update_status(f"Installing {pkg}...", pct)
                    log(f"pip install {pkg} ...")
                    result = subprocess.run(
                        [PIP, "install", pkg, "--quiet"],
                        capture_output=True, text=True,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    if result.returncode == 0:
                        log(f"✓ {pkg} installed")
                    else:
                        log(f"✗ {pkg} FAILED: {result.stderr.strip()[:80]}")

        # Start MySQL
        start_mysql(log, update_status)

        # Import schema (local MySQL only - Supabase already has its tables)
        import db as _db
        update_status("Setting up Database...", 70)
        log("Importing schema...")
        schema_file = resource_path("schema.sql")
        if _db.IS_POSTGRES:
            log("✓ Online database - no local schema import needed")
        elif os.path.exists(schema_file):
            try:
                with open(schema_file, "r", encoding="utf-8") as sf:
                    result = subprocess.run(
                        [MYSQL_BIN, "-u", "root", "--force"],
                        stdin=sf,
                        capture_output=True, text=True,
                        timeout=30,
                        creationflags=subprocess.CREATE_NO_WINDOW
                    )
                if result.returncode == 0:
                    log("✓ Schema imported")
                else:
                    log(f"⚠ Schema warning: {result.stderr.strip()[:100]}")
                    log("  (continuing anyway...)")
            except subprocess.TimeoutExpired:
                log("⚠ Schema import timed out — continuing anyway")
            except Exception as e:
                log(f"⚠ Schema error: {e} — continuing anyway")
        else:
            log("⚠ schema.sql not found — skipping")

        # Start Flask
        ok = start_flask(log, update_status)
        if not ok:
            update_status("✗ Flask failed — check flask_error.log", 100)
            log(f"Log file: {LOG}")
            return  # Don't open window if Flask failed

        mark_installed()
        log(f"✓ Saved: {INSTALL_FLAG}")
        log("✓ Setup Complete!")
        update_status("✓ Setup Complete!", 100)
        time.sleep(1)

        inst.destroy()
        open_app_window()

    threading.Thread(target=do_install, daemon=True).start()
    inst.mainloop()

# ══════════════════════════════════════════════════════════
#   NORMAL LAUNCH
# ══════════════════════════════════════════════════════════
def run_launcher():
    root = tk.Tk()
    _set_window_icon(root)
    root.title("ST Courier")
    root.geometry("400x220")
    root.resizable(False, False)
    root.configure(bg="#1a1a2e")
    root.eval('tk::PlaceWindow . center')

    tk.Label(root, text="ST Courier System",
             font=("Arial", 18, "bold"),
             bg="#1a1a2e", fg="white").pack(pady=20)
    status_label = tk.Label(root, text="Starting...",
                             font=("Arial", 11),
                             bg="#1a1a2e", fg="#00d4ff")
    status_label.pack()

    progress = ttk.Progressbar(root, length=320, mode='determinate')
    progress.pack(pady=15)

    err_label = tk.Label(root, text="",
                         font=("Consolas", 9),
                         bg="#1a1a2e", fg="#ff4444",
                         wraplength=380)
    err_label.pack(pady=4)

    def update_status(msg, value):
        status_label.config(text=msg)
        progress['value'] = value
        root.update_idletasks()

    def log(msg):
        pass  # silent on normal launch

    def start_app():
        db_ok = start_mysql(log, update_status)
        if db_ok is False:
            # Never leave the user with a dead window: the app still opens and
            # shows the recovery page (start MySQL / change port / switch DB).
            update_status("Database not reachable - opening recovery page...", 50)
        else:
            update_status("Database Ready ✓", 50)
        time.sleep(0.5)

        update_status("Starting Backend...", 65)
        ok = start_flask(log, update_status)

        if not ok:
            # Show error and open log file
            err_msg = ""
            try:
                with open(LOG, "r") as f:
                    lines = f.read().strip().splitlines()
                    err_msg = "\n".join(lines[-5:])
            except Exception:
                err_msg = f"Check {LOG}"
            err_label.config(text=f"Flask failed!\n{err_msg}")
            update_status("✗ Failed to start", 100)
            os.startfile(LOG) if os.path.exists(LOG) else None
            return

        update_status("✓ Opening ST Courier...", 100)
        time.sleep(0.5)
        root.after(0, lambda: (root.destroy(), open_app_window()))

    threading.Thread(target=start_app, daemon=True).start()
    root.mainloop()

# ══════════════════════════════════════════════════════════
#   ENTRY POINT
# ══════════════════════════════════════════════════════════
if __name__ == "__main__" and "--keepalive" in sys.argv:
    # Headless: one tiny request so the free Supabase project is not paused.
    # Run daily by a Windows scheduled task (see Database page).  No window.
    try:
        import dbtools
        sys.exit(0 if dbtools.keepalive()["ok"] else 1)
    except SystemExit:
        raise
    except Exception:
        sys.exit(2)

if __name__ == "__main__":
    if is_installed():
        run_launcher()
    else:
        if run_setup_wizard():
            run_installer()
        # else: user cancelled — run_setup_wizard already exited the process
