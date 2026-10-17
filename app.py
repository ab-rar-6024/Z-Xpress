import io
import os
import re
import sys
import tempfile
import threading
from itertools import groupby
from operator import itemgetter
from flask import send_file
from functools import wraps
from datetime import date, timedelta
import math
import uuid
from pathlib import Path
from flask import (Flask, render_template, request, redirect,
                   send_file, jsonify, url_for, session, flash)
import pandas as pd
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer, PageBreak
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import landscape

from db import get_db_connection, check_license, upsert_place_zone


# ============================================================
# RESOURCE PATH HELPER  (NEW)
# ============================================================
def _resource_path(rel):
    """
    Resolve a path to a bundled resource (templates/, static/, schema.sql, etc).
    When frozen into a PyInstaller --onefile exe, these are extracted at
    runtime into sys._MEIPASS. When running as a plain script, they live
    next to this file as usual.
    """
    try:
        base = sys._MEIPASS  # type: ignore[attr-defined]
    except AttributeError:
        base = os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, rel)


app = Flask(
    __name__,
    template_folder=_resource_path("templates"),
    static_folder=_resource_path("static"),
)
app.secret_key = __import__('os').environ.get("SECRET_KEY", "dev-secret-change-in-prod")

# ── Remember-me support (NEW) ──────────────────────────────
# When a user checks "Remember me" on /login, we mark the session as
# permanent. Flask then keeps it alive for PERMANENT_SESSION_LIFETIME
# instead of expiring it the moment the browser closes.
app.permanent_session_lifetime = timedelta(days=30)


# ============================================================
# GLOBAL ERROR LOGGER  (NEW — diagnostic)
# ============================================================
import traceback as _traceback

def _crash_log_path():
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        base = "."
    return os.path.join(base, "app_crash.log")

@app.errorhandler(Exception)
def _log_unhandled_exception(e):
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):      # 404 etc. are normal, not crashes
        return e
    try:
        with open(_crash_log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n[{__import__('datetime').datetime.now()}] "
                    f"{request.method} {request.path}\n")
            f.write(_traceback.format_exc())
            f.write("\n")
    except Exception:
        pass
    raise e


import json
import base64

def _remember_file_path():
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        base = "."
    return os.path.join(base, "remembered_login.dat")


def _save_remembered_login(username, password):
    try:
        payload = {
            "username": username,
            "password": base64.b64encode(password.encode("utf-8")).decode("ascii"),
        }
        with open(_remember_file_path(), "w", encoding="utf-8") as f:
            json.dump(payload, f)
    except Exception:
        pass


def _load_remembered_login():
    path = _remember_file_path()
    if not os.path.exists(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        username = payload.get("username", "")
        password = base64.b64decode(payload.get("password", "")).decode("utf-8")
        return username, password
    except Exception:
        return None, None


def _clear_remembered_login():
    try:
        path = _remember_file_path()
        if os.path.exists(path):
            os.remove(path)
    except Exception:
        pass


# ============================================================
# DOWNLOADS HELPER
# ============================================================
def _downloads_dir():
    try:
        import winreg
        key = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key) as k:
            val, _ = winreg.QueryValueEx(k, "{374DE290-123F-4565-9164-39C4925E467B}")
            p = Path(os.path.expandvars(val))
            if p.exists():
                return p
    except Exception:
        pass
    return Path.home() / "Downloads"


def save_to_downloads(data_bytes, base_name, ext=".xlsx", open_after=True):
    folder = _downloads_dir()
    folder.mkdir(parents=True, exist_ok=True)
    safe   = re.sub(r'[<>:"/\\|?*]', '_', base_name).strip() or "export"
    target = folder / f"{safe}{ext}"
    n = 1
    while target.exists():
        target = folder / f"{safe} ({n}){ext}"
        n += 1
    target.write_bytes(data_bytes)
    if open_after:
        try:
            os.startfile(str(target))
        except Exception:
            pass
    return str(target)


# ============================================================
# AUTH DECORATORS
# ============================================================
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "username" not in session:
            return redirect(url_for("login"))
        if session.get("role") != "admin":
            return "Access denied — admins only.", 403
        return f(*args, **kwargs)
    return decorated

# ============================================================
# LOGIN / LOGOUT
# ============================================================
@app.route("/login", methods=["GET", "POST"])
def login():
    if "username" in session:
        return redirect(url_for("dashboard"))

    valid, lic_message = check_license()
    if not valid:
        return render_template("login.html", error=None, license_error=lic_message)

    error = None
    remembered_username, remembered_password = _load_remembered_login()

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()
        remember = request.form.get("remember") == "1"

        db     = get_db_connection()
        cursor = db.cursor(dictionary=True)
        cursor.execute(
            "SELECT username, password, role FROM app_users "
            "WHERE username = %s LIMIT 1",
            (username,)
        )
        user = cursor.fetchone()
        db.close()

        if user and user["password"] == password:
            session.permanent   = remember
            session["username"] = user["username"]
            session["role"]     = user["role"]

            if remember:
                _save_remembered_login(username, password)
            else:
                _clear_remembered_login()

            return redirect(url_for("dashboard"))

        error = "Invalid username or password."
        remembered_username, remembered_password = username, password

    return render_template(
        "login.html",
        error=error,
        license_error=None,
        remembered_username=remembered_username or "",
        remembered_password=remembered_password or "",
        remember_checked=bool(remembered_username),
    )


@app.route("/api/verify-login", methods=["POST"])
@login_required
def api_verify_login():
    data     = request.get_json() or {}
    username = (data.get("username") or "").strip()
    password = (data.get("password") or "").strip()

    if not username or not password:
        return jsonify({"ok": False, "message": "Username and password are required."})

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT username, password, role FROM app_users "
        "WHERE username = %s LIMIT 1",
        (username,)
    )
    user = cursor.fetchone()
    db.close()

    if not user:
        return jsonify({"ok": False, "message": "Invalid username or password."})
    if user["password"] != password:
        return jsonify({"ok": False, "message": "Invalid username or password."})
    if user["role"] != "admin":
        return jsonify({"ok": False, "message": "Only admin accounts can perform this action."})

    return jsonify({"ok": True})


@app.route("/favicon.ico")
def favicon():
    return redirect("/static/img/favicon.png")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ============================================================
# CHANGE PASSWORD  (NEW)
# ============================================================
@app.route("/change-password", methods=["GET", "POST"])
def change_password():
    valid, lic_message = check_license()
    if not valid:
        return render_template("login.html", error=None, license_error=lic_message)

    error = None

    if request.method == "POST":
        username          = request.form.get("username", "").strip()
        current_password  = request.form.get("current_password", "").strip()
        new_password      = request.form.get("new_password", "").strip()
        confirm_password  = request.form.get("confirm_password", "").strip()

        if not all([username, current_password, new_password, confirm_password]):
            error = "All fields are required."
        elif new_password != confirm_password:
            error = "New password and confirmation do not match."
        elif len(new_password) < 4:
            error = "New password must be at least 4 characters."
        else:
            db     = get_db_connection()
            cursor = db.cursor(dictionary=True)
            cursor.execute(
                "SELECT username, password FROM app_users WHERE username = %s LIMIT 1",
                (username,)
            )
            user = cursor.fetchone()

            if not user or user["password"] != current_password:
                db.close()
                error = "Username or current password is incorrect."
            else:
                cursor.execute(
                    "UPDATE app_users SET password = %s WHERE username = %s",
                    (new_password, username)
                )
                db.commit()
                db.close()
                flash("Password changed successfully. Please sign in with your new password.")
                return redirect(url_for("login"))

    return render_template("change_password.html", error=error)

# ============================================================
# DASHBOARD
# ============================================================
@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


@app.route("/api/dashboard")
@login_required
def api_dashboard():
    from datetime import date, timedelta, datetime as dt

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    today  = date.today()
    m_start = today.replace(day=1).isoformat()
    today_s = today.isoformat()

    cursor.execute("""
        SELECT COUNT(*) AS cnt,
               COALESCE(SUM(total_amount),0) AS rev,
               COALESCE(SUM(weight),0) AS wt
        FROM bookings WHERE booking_date = %s
    """, (today_s,))
    t = cursor.fetchone()

    cursor.execute("""
        SELECT COUNT(*) AS cnt,
               COALESCE(SUM(total_amount),0) AS rev,
               COALESCE(SUM(weight),0) AS wt
        FROM bookings WHERE booking_date >= %s
    """, (m_start,))
    m = cursor.fetchone()

    cursor.execute("""
        SELECT COUNT(*) AS cnt, COALESCE(SUM(total_amount),0) AS rev
        FROM bookings
    """)
    a = cursor.fetchone()

    kpis = {
        "today_count":   int(t["cnt"]),
        "today_revenue": float(t["rev"]),
        "today_weight":  float(t["wt"]),
        "month_count":   int(m["cnt"]),
        "month_revenue": float(m["rev"]),
        "month_weight":  float(m["wt"]),
        "total_count":   int(a["cnt"]),
        "total_revenue": float(a["rev"]),
    }

    trend = []
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        cursor.execute("""
            SELECT COALESCE(SUM(total_amount),0) AS rev
            FROM bookings WHERE booking_date = %s
        """, (day.isoformat(),))
        row = cursor.fetchone()
        trend.append({
            "label":   day.strftime("%a %d"),
            "revenue": float(row["rev"])
        })

    six_month_trend = []
    for i in range(5, -1, -1):
        year  = today.year
        month = today.month - i
        while month <= 0:
            month += 12
            year  -= 1
        m_start_i = date(year, month, 1)
        if month == 12:
            m_end_i = date(year + 1, 1, 1)
        else:
            m_end_i = date(year, month + 1, 1)

        cursor.execute("""
            SELECT COUNT(*) AS cnt,
                   COALESCE(SUM(total_amount),0) AS rev,
                   COALESCE(SUM(weight),0) AS wt
            FROM bookings
            WHERE booking_date >= %s AND booking_date < %s
        """, (m_start_i.isoformat(), m_end_i.isoformat()))
        row = cursor.fetchone()
        six_month_trend.append({
            "label":   m_start_i.strftime("%b %Y"),
            "revenue": float(row["rev"]),
            "count":   int(row["cnt"]),
            "weight":  float(row["wt"]),
        })

    cursor.execute("""
        SELECT code, MAX(code_fullform) AS name,
               COALESCE(SUM(total_amount),0) AS revenue
        FROM bookings
        WHERE booking_date >= %s AND code != ''
        GROUP BY code
        ORDER BY revenue DESC
        LIMIT 5
    """, (m_start,))
    top_clients = [
        {"code": r["code"], "name": r["name"] or r["code"], "revenue": float(r["revenue"])}
        for r in cursor.fetchall()
    ]

    zone_labels = {1:"Chennai", 2:"Tamil Nadu", 3:"South India", 4:"North Metro", 5:"ROI"}
    cursor.execute("""
        SELECT zone, COUNT(*) AS cnt
        FROM bookings
        WHERE zone IS NOT NULL AND zone != ''
        GROUP BY zone ORDER BY CAST(zone AS UNSIGNED) ASC
    """)
    zones = []
    for r in cursor.fetchall():
        try:
            z = int(r["zone"])
        except (ValueError, TypeError):
            z = 5
        zones.append({
            "zone":  z,
            "label": zone_labels.get(z, "ROI"),
            "count": int(r["cnt"])
        })

    cursor.execute("""
        SELECT code, code_fullform, awb_no, destination,
               weight, zone, total_amount, booking_date
        FROM bookings
        ORDER BY id DESC LIMIT 10
    """)
    recent = []
    for r in cursor.fetchall():
        row = dict(r)
        if hasattr(row.get("booking_date"), "isoformat"):
            row["booking_date"] = row["booking_date"].isoformat()
        recent.append(row)

    db.close()

    return jsonify({
        "kpis":             kpis,
        "trend":            trend,
        "six_month_trend":  six_month_trend,
        "top_clients":      top_clients,
        "zones":            zones,
        "recent":           recent,
    })

# ============================================================
# ZONE ENTRY
# ============================================================
@app.route("/zone-entry", methods=["GET", "POST"])
@login_required
def zone_entry():
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    if request.method == "POST":
        action = request.form.get("_action", "add")

        if action in ("edit", "delete") and session.get("role") != "admin":
            db.close()
            return "Access denied: admins only.", 403

        if action == "delete":
            del_id = request.form.get("del_id")
            if del_id:
                cursor.execute("DELETE FROM place_zones WHERE id = %s", (del_id,))
                db.commit()
            db.close()
            return redirect("/zone-entry")

        place      = request.form.get("place", "").strip().upper()
        zone       = request.form.get("zone", "5").strip()
        place_code = request.form.get("place_code", "").strip().upper() or None

        if not place or not zone:
            db.close()
            return "Place and Zone are required.", 400

        try:
            zone = int(zone)
            if zone not in range(1, 6):
                raise ValueError
        except (ValueError, TypeError):
            db.close()
            return "Zone must be a number between 1 and 5.", 400

        if action == "edit":
            edit_id = request.form.get("edit_id")
            cursor.execute(
                "UPDATE place_zones SET place=%s, zone=%s, place_code=%s WHERE id=%s",
                (place, zone, place_code, edit_id)
            )
        else:
            upsert_place_zone(cursor, place, zone, place_code)

        db.commit()
        db.close()
        return redirect("/zone-entry")

    cursor.execute("SELECT * FROM place_zones ORDER BY place ASC")
    zones = cursor.fetchall()
    db.close()

    zone_labels = {1: "Chennai", 2: "Tamil Nadu", 3: "South India",
                   4: "North Metro", 5: "ROI"}
    for z in zones:
        z["zone_label"] = zone_labels.get(int(z["zone"]), "ROI")

    return render_template("zone_entry.html", zones=zones, role=session.get("role"))


# ============================================================
# RATE ENTRY
# ============================================================
@app.route("/rate-entry", methods=["GET", "POST"])
@login_required
def rate_entry():

    def num(value):
        value = str(value).strip()
        return None if value == "" else float(value)

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    search    = request.args.get("search", "").strip()
    limit_str = request.args.get("limit", "20")
    page      = int(request.args.get("page", 1))

    limit = None if limit_str == "all" else int(limit_str) if limit_str.isdigit() else 20

    if request.method == "POST":
        action = request.form.get("_action", "add")

        if action in ("edit", "delete") and session.get("role") != "admin":
            db.close()
            return "Access denied: admins only.", 403

        if action == "delete":
            cursor.execute("DELETE FROM rates WHERE id=%s", (request.form["del_id"],))
            db.commit()
            db.close()
            return redirect(url_for('rate_entry', search=search, limit=limit_str, page=page))

        zone            = int(request.form.get("zone") or 5)
        code            = request.form.get("code",          "").strip()
        code_fullform   = request.form.get("code_fullform", "").strip()
        place           = request.form.get("place",         "").strip()
        rate_250g       = num(request.form.get("rate_250g",     ""))
        rate_500g       = num(request.form.get("rate_500g",     ""))
        rate_500g_1     = num(request.form.get("rate_500g_1",   ""))
        rate_1_to_3kg   = num(request.form.get("rate_1_to_3kg",   ""))
        rate_3_to_10kg  = num(request.form.get("rate_3_to_10kg",  ""))
        rate_above_10kg = num(request.form.get("rate_above_10kg", ""))
        fuel            = num(request.form.get("fuel", ""))

        if action == "edit":
            cursor.execute("""
                UPDATE rates SET
                    code=%s, code_fullform=%s, place=%s, zone=%s,
                    rate_250g=%s, rate_500g=%s, rate_500g_1=%s,
                    rate_1_to_3kg=%s, rate_3_to_10kg=%s,
                    rate_above_10kg=%s, fuel=%s
                WHERE id=%s
            """, (code, code_fullform, place, zone,
                  rate_250g, rate_500g, rate_500g_1,
                  rate_1_to_3kg, rate_3_to_10kg, rate_above_10kg, fuel,
                  request.form["edit_id"]))
        else:
            cursor.execute("""
                INSERT INTO rates (
                    code, code_fullform, place, zone,
                    rate_250g, rate_500g, rate_500g_1,
                    rate_1_to_3kg, rate_3_to_10kg, rate_above_10kg, fuel
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (code, code_fullform, place, zone,
                  rate_250g, rate_500g, rate_500g_1,
                  rate_1_to_3kg, rate_3_to_10kg, rate_above_10kg, fuel))

        db.commit()
        db.close()
        return redirect(url_for('rate_entry', search=search, limit=limit_str, page=1))

    base_query = "FROM rates"
    params     = []
    if search:
        base_query += " WHERE code LIKE %s OR code_fullform LIKE %s OR place LIKE %s"
        like   = f"%{search}%"
        params = [like, like, like]

    cursor.execute(f"SELECT COUNT(*) AS total {base_query}", params)
    total = cursor.fetchone()["total"]

    if limit:
        offset      = (page - 1) * limit
        total_pages = (total + limit - 1) // limit
        cursor.execute(
            f"SELECT * {base_query} ORDER BY id DESC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
    else:
        total_pages = 1
        page        = 1
        cursor.execute(f"SELECT * {base_query} ORDER BY id DESC", params)

    rates = cursor.fetchall()
    db.close()

    return render_template(
        "rate_entry.html",
        rates=rates, search=search, limit=limit_str,
        page=page, total_pages=total_pages, total=total,
        role=session.get("role")
    )

# ============================================================
# BOOKING ENTRY
# ============================================================
@app.route("/booking-entry", methods=["GET", "POST"])
@login_required
def booking_entry():

    def txt(v):
        if v is None: return ""
        return str(v).strip()

    def num(v):
        if v is None: return 0.0
        v = str(v).strip()
        return 0.0 if v == "" else float(v)

    def to_date(v):
        if v is None: return None
        v = str(v).strip()
        if v == "" or v.lower() == "none": return None
        return v

    if request.method == "POST":
        db     = get_db_connection()
        cursor = db.cursor(dictionary=True)
        action = request.form.get("_action", "add")

        if action in ("edit", "delete") and session.get("role") != "admin":
            db.close()
            return "Access denied: admins only.", 403

        if action == "delete":
            del_id = request.form.get("del_id")
            if del_id:
                cursor.execute("DELETE FROM bookings WHERE id=%s", (del_id,))
                db.commit()
            db.close()
            return redirect("/booking-entry")

        code          = txt(request.form.get("code"))
        code_fullform = txt(request.form.get("code_fullform"))
        booking_date  = to_date(request.form.get("booking_date"))
        awb_no        = txt(request.form.get("awb_no")).upper()
        destination   = txt(request.form.get("destination"))
        weight        = num(request.form.get("weight"))
        courier       = txt(request.form.get("courier"))
        zone          = txt(request.form.get("zone"))
        auto_amount   = num(request.form.get("auto_amount"))


        fuel         = 0.0
        total_amount = round(auto_amount, 2)

        if action == "edit":
            edit_id = request.form.get("edit_id")

            cursor.execute("""
                UPDATE bookings SET
                    code=%s, code_fullform=%s, booking_date=%s, awb_no=%s, destination=%s,
                    weight=%s, courier=%s, zone=%s, auto_amount=%s,
                    fuel=%s, total_amount=%s
                WHERE id=%s
            """, (code, code_fullform, booking_date, awb_no, destination, weight,
                  courier, zone, auto_amount, fuel, total_amount,
                  edit_id))
        else:
            cursor.execute("""
                INSERT INTO bookings (
                    code, code_fullform, booking_date, awb_no, destination, weight,
                    courier, zone, auto_amount, fuel, total_amount,
                    client_name, inv_no, inv_date
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, (code, code_fullform, booking_date, awb_no, destination, weight,
                  courier, zone, auto_amount, fuel, total_amount,
                  '', '', None))

        db.commit()
        db.close()
        return redirect("/booking-entry")

    today = date.today().strftime("%Y-%m-%d")
    return render_template("booking_entry.html", role=session.get("role"), today=today)

# ============================================================
# BOOKING IMPORT  (fixed: reads Auto Amount / Total Amount / Fuel
#                  from the day-book export so totals are preserved)
# ============================================================
@app.route("/booking-import", methods=["POST"])
@login_required
def booking_import():
    from datetime import datetime

    def zone_name_to_number(zone_text):
        zone_map = {
            'CHENNAI': 1, 'TAMIL NADU': 2, 'TAMILNADU': 2,
            'SOUTH INDIA': 3, 'SOUTH': 3,
            'NORTH METRO': 4, 'NORTH': 4,
            'ROI': 5, 'REST OF INDIA': 5
        }
        z = str(zone_text).strip().upper()
        try:
            num = int(float(z))
            if 1 <= num <= 5:
                return num
        except (ValueError, TypeError):
            pass
        for k, v in zone_map.items():
            if z == k or (k == 'CHENNAI' and z in ['CHENNAI', 'MADRAS']):
                return v
        for kw, val in zone_map.items():
            if kw in z:
                return val
        return 5

    if 'excel_file' not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files['excel_file']
    if file.filename == '':
        return jsonify({"error": "Empty filename"}), 400

    try:
        df = pd.read_excel(file)
    except Exception as e:
        return jsonify({"error": f"Failed to read Excel: {str(e)}"}), 400

    df.columns = [str(c).strip() for c in df.columns]

    required_cols = ['Code', 'Date', 'AWB Number', 'Destination', 'Weight', 'Zone']
    for col in required_cols:
        if col not in df.columns:
            return jsonify({"error": f"Missing column: {col}"}), 400

    def first_present(*names):
        for n in names:
            if n in df.columns:
                return n
        return None

    auto_col  = first_present('Auto Amount', 'Amount')
    fuel_col  = first_present('Fuel')
    total_col = first_present('Total Amount')

    def cell_num(row, col):
        if not col:
            return None
        val = row.get(col)
        try:
            if pd.isna(val):
                return None
        except Exception:
            pass
        try:
            return float(val)
        except (ValueError, TypeError):
            return None

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT * FROM rates")
    all_rates      = cursor.fetchall()
    rate_cache     = {}
    fullform_cache = {}
    for r in all_rates:
        rate_cache[(r['code'].upper(), r['zone'])] = r
        if r['code'].upper() not in fullform_cache:
            fullform_cache[r['code'].upper()] = r.get('code_fullform', '') or ''
    db.close()

    preview_rows = []
    errors       = []
    slabs = [
        (0.25, 'rate_250g'), (0.50, 'rate_500g'), (1.00, 'rate_500g_1'),
        (3.00, 'rate_1_to_3kg'), (10.00, 'rate_3_to_10kg'),
        (float('inf'), 'rate_above_10kg')
    ]

    for idx, row in df.iterrows():
        row_num = idx + 2
        try:
            code     = str(row['Code']).strip().upper()
            date_val = row['Date']
            if pd.isna(date_val):
                raise ValueError("Date is empty")
            if hasattr(date_val, 'strftime'):
                date_str = (date_val.date() if hasattr(date_val, 'date') else date_val).isoformat()
            else:
                date_str = str(date_val).strip().split(' ')[0]
                for fmt in ('%Y-%m-%d', '%d-%m-%Y', '%d/%m/%Y'):
                    try:
                        date_str = datetime.strptime(date_str, fmt).date().isoformat()
                        break
                    except:
                        continue
                else:
                    raise ValueError(f"Invalid date format: {date_str}")

            awb_no        = str(row['AWB Number']).strip().upper()
            destination   = str(row['Destination']).strip().upper()
            weight        = float(row['Weight'])
            zone_num      = zone_name_to_number(str(row['Zone']).strip())
            rate_row      = rate_cache.get((code, zone_num))
            code_fullform = fullform_cache.get(code, '')

            file_auto  = cell_num(row, auto_col)
            file_fuel  = cell_num(row, fuel_col)
            file_total = cell_num(row, total_col)

            if file_auto is not None and file_auto > 0:
                auto_amount = round(file_auto)
            else:
                auto_amount = 0.0
                if rate_row:
                    for max_w, col_name in slabs:
                        if weight <= max_w:
                            rate        = float(rate_row.get(col_name) or 0)
                            auto_amount = rate * math.ceil(weight) if max_w > 1 else rate
                            break
                auto_amount = round(auto_amount)
                if not rate_row:
                    errors.append(
                        f"Row {row_num}: No rate for '{code}' zone {zone_num} — amount set 0.00"
                    )

            courier_val = "ST COURIER"
            if 'Courier' in df.columns:
                c = row.get('Courier')
                try:
                    if not pd.isna(c):
                        courier_val = str(c).strip()
                except Exception:
                    pass

            preview_rows.append({
                "temp_id":      row_num,
                "code":         code,
                "code_fullform": code_fullform,
                "booking_date": date_str,
                "awb_no":       awb_no,
                "destination":  destination,
                "weight":       weight,
                "zone":         zone_num,
                "auto_amount":  auto_amount,
                "fuel":         file_fuel,
                "total_amount": file_total,
                "courier":      courier_val,
                "valid":        True
            })
        except Exception as e:
            errors.append(f"Row {row_num}: {str(e)}")
            preview_rows.append({"temp_id": row_num, "valid": False, "error": str(e)})

    return jsonify({"rows": preview_rows, "errors": errors[:50]})


# ============================================================
# BOOKING IMPORT SAVE  (fixed: honours pre-computed fuel/total
#                       from the exported file)
# ============================================================
@app.route("/booking-import-save", methods=["POST"])
@login_required
def booking_import_save():
    data = request.get_json()
    rows = data.get("rows", [])
    if not rows:
        return jsonify({"success": False, "message": "No data to save"})

    db       = get_db_connection()
    cursor   = db.cursor(dictionary=True)
    inserted = 0
    errors   = []

    def to_num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    # One lookup for every AWB (instead of one query per row) and one batched
    # INSERT - this keeps big imports fast when the database is online.
    wanted   = list({r['awb_no'] for r in rows if r.get("valid") and r.get('awb_no')})
    existing = set()
    for i in range(0, len(wanted), 500):
        chunk = wanted[i:i + 500]
        cursor.execute(
            "SELECT awb_no FROM bookings WHERE awb_no IN (%s)" % ",".join(["%s"] * len(chunk)),
            tuple(chunk))
        existing.update(r["awb_no"] for r in cursor.fetchall())

    batch = []
    for row in rows:
        if not row.get("valid"):
            errors.append(f"Row {row['temp_id']}: invalid, skipped")
            continue
        try:
            if row['awb_no'] in existing:
                errors.append(f"Row {row['temp_id']}: AWB '{row['awb_no']}' exists — skipped")
                continue

            auto_amount = float(row.get('auto_amount') or 0)
            file_fuel   = to_num(row.get('fuel'))
            file_total  = to_num(row.get('total_amount'))

            if file_total is not None and file_total > 0:
                fuel_amt  = file_fuel if file_fuel is not None else round(file_total - auto_amount, 2)
                total_amt = round(file_total, 2)
            else:
                fuel_amt  = 0.0
                total_amt = round(auto_amount, 2)

            batch.append((
                row['code'], row.get('code_fullform', ''),
                row['booking_date'], row['awb_no'],
                row['destination'], row['weight'],
                row.get('courier', 'ST COURIER'),
                str(row['zone']), auto_amount, fuel_amt, total_amt,
                '', '', None
            ))
            existing.add(row['awb_no'])

        except Exception as e:
            errors.append(f"Row {row['temp_id']}: {str(e)}")

    if batch:
        try:
            cursor.executemany("""
                INSERT INTO bookings (
                    code, code_fullform, booking_date, awb_no, destination, weight,
                    courier, zone, auto_amount, fuel, total_amount,
                    client_name, inv_no, inv_date
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, batch)
            inserted = len(batch)
        except Exception as e:
            db.rollback()
            db.close()
            return jsonify({"success": False, "message": f"Import failed, nothing saved: {e}"})

    db.commit()
    db.close()
    return jsonify({"success": True, "inserted": inserted, "errors": errors[:50]})


# ============================================================
# API: bookings (AJAX)
# ============================================================
@app.route("/api/bookings")
@login_required
def api_bookings():
    limit  = request.args.get("limit", "20")
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    sql = """
        SELECT id, code, code_fullform, booking_date, awb_no, destination, weight,
               courier, zone, auto_amount, fuel, total_amount,
               client_name, inv_no, inv_date
        FROM bookings
        ORDER BY id DESC
    """
    if limit == "all":
        cursor.execute(sql)
    else:
        try:
            lim = int(limit)
        except ValueError:
            lim = 20
        cursor.execute(sql + f" LIMIT {lim}")

    rows   = cursor.fetchall()
    db.close()
    result = []
    for r in rows:
        row = dict(r)
        for k, v in list(row.items()):
            if hasattr(v, 'strftime'):
                row[k]          = v.strftime('%d/%m/%Y')
                row[k + '_iso'] = v.strftime('%Y-%m-%d')
            elif hasattr(v, 'isoformat'):
                row[k] = v.isoformat()
        result.append(row)
    return jsonify(result)

# ============================================================
# INVOICE / STATEMENT
# ============================================================
def _resolve_fuel_rate(cursor, code, provided_rate):
    if provided_rate and provided_rate > 0:
        return provided_rate
    if not code or code == "ALL":
        return 0.0
    cursor.execute(
        "SELECT fuel FROM rates WHERE UPPER(code) = %s ORDER BY id DESC LIMIT 1",
        (code.upper(),)
    )
    fuel_row = cursor.fetchone()
    if not fuel_row:
        return 0.0
    fuel_val = fuel_row.get("fuel")
    if fuel_val is None:
        return 0.0
    try:
        return float(fuel_val)
    except (TypeError, ValueError):
        return 0.0


@app.route("/invoice", methods=["GET", "POST"])
@login_required
def invoice():
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    from_date = request.args.get("from_date", "").strip()
    to_date   = request.args.get("to_date",   "").strip()
    month     = request.args.get("month",     "").strip()
    code      = request.args.get("code",      "ALL").strip()
    try:
        fuel_rate = float(request.args.get("fuel_rate", "0") or 0)
    except (ValueError, TypeError):
        fuel_rate = 0.0

    if month:
        try:
            y, m = (int(p) for p in month.split("-"))
            from_date = date(y, m, 1).isoformat()
            if m == 12:
                last_day = date(y, 12, 31)
            else:
                last_day = date(y, m + 1, 1) - timedelta(days=1)
            to_date = last_day.isoformat()
        except (ValueError, TypeError):
            pass
    elif from_date and not to_date:
        to_date = from_date
    elif to_date and not from_date:
        from_date = to_date

    fuel_rate = _resolve_fuel_rate(cursor, code, fuel_rate)

    limit_str = request.args.get("limit", "20")
    page      = int(request.args.get("page", 1))
    limit     = None if limit_str == "all" else (int(limit_str) if limit_str.isdigit() else 20)

    base_query = "FROM bookings WHERE booking_date IS NOT NULL"
    params     = []

    if from_date and to_date:
        base_query += " AND booking_date BETWEEN %s AND %s"
        params.extend([from_date, to_date])
    elif from_date:
        base_query += " AND booking_date >= %s"
        params.append(from_date)
    elif to_date:
        base_query += " AND booking_date <= %s"
        params.append(to_date)

    if code and code != "ALL":
        base_query += " AND code = %s"
        params.append(code)

    cursor.execute(f"SELECT COUNT(*) AS total {base_query}", params)
    total = cursor.fetchone()["total"]

    if limit:
        offset      = (page - 1) * limit
        total_pages = (total + limit - 1) // limit
        cursor.execute(
            f"SELECT booking_date, destination, awb_no, weight, total_amount "
            f"{base_query} ORDER BY booking_date ASC, id ASC LIMIT %s OFFSET %s",
            params + [limit, offset]
        )
    else:
        total_pages = 1
        page        = 1
        cursor.execute(
            f"SELECT booking_date, destination, awb_no, weight, total_amount "
            f"{base_query} ORDER BY booking_date ASC, id ASC",
            params
        )

    rows = cursor.fetchall()
    cursor.execute("SELECT DISTINCT code FROM bookings ORDER BY code")
    codes = cursor.fetchall()

    cursor.execute("""
        SELECT DISTINCT DATE_FORMAT(booking_date, '%Y-%m') AS ym
        FROM bookings
        WHERE booking_date IS NOT NULL
        ORDER BY ym DESC
    """)
    months = []
    for r in cursor.fetchall():
        ym = r["ym"]
        if ym:
            y, m = ym.split("-")
            months.append({"value": ym, "label": date(int(y), int(m), 1).strftime("%B %Y")})

    db.close()

    return render_template(
        "invoice.html",
        rows=rows, codes=codes, months=months,
        from_date=from_date, to_date=to_date,
        selected_code=code, selected_month=month, fuel_rate=fuel_rate,
        limit=limit_str, page=page,
        total_pages=total_pages, total=total,
        role=session.get("role")
    )

# ============================================================
# SALES CHECKING
# ============================================================
@app.route("/sales-checking", methods=["GET", "POST"])
@login_required
def sales_checking():
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    show_results = (request.method == "POST")

    client_code = request.form.get("client_code", "").strip()
    awb_no      = request.form.get("awb_no", "").strip()
    destination = request.form.get("destination", "").strip()
    single_date = request.form.get("date", "").strip()

    rows         = []
    total_amount = 0

    if show_results:
        query = """
            SELECT code, code_fullform, awb_no, destination,
                   COUNT(*) AS sum_count, SUM(total_amount) AS amount
            FROM bookings WHERE 1=1
        """
        params = []

        if client_code:
            query += " AND (code LIKE %s OR code_fullform LIKE %s)"
            params.append(f"%{client_code}%")
            params.append(f"%{client_code}%")
        if awb_no:
            query += " AND awb_no LIKE %s"
            params.append(f"%{awb_no}%")
        if destination:
            query += " AND destination LIKE %s"
            params.append(f"%{destination}%")
        if single_date:
            query += " AND booking_date = %s"
            params.append(single_date)

        query += " GROUP BY code, code_fullform, awb_no, destination ORDER BY MAX(id) DESC"

        cursor.execute(query, params)
        rows         = cursor.fetchall()
        total_amount = sum(r["amount"] for r in rows) if rows else 0

    db.close()
    return render_template(
        "sales_checking.html",
        rows=rows, total_amount=total_amount,
        filters=request.form, show_results=show_results,
        role=session.get("role")
    )

# ============================================================
# DAY WISE
# ============================================================
@app.route("/day-wise", methods=["GET", "POST"])
@login_required
def day_wise():
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    def insert_manual_row(entry_date, total_weight, total_sales):
        awb_placeholder = f"DAYWISE-{entry_date}-{uuid.uuid4().hex[:6].upper()}"
        cursor.execute("""
            INSERT INTO bookings (
                code, code_fullform, booking_date, awb_no, destination, weight,
                courier, zone, auto_amount, fuel, total_amount,
                client_name, inv_no, inv_date
            ) VALUES ('', '', %s, %s, 'MANUAL DAY-WISE ENTRY', %s, '', '', %s, 0, %s, '', '', NULL)
        """, (entry_date, awb_placeholder, total_weight, total_sales, total_sales))
        return cursor.lastrowid, awb_placeholder

    if request.method == "POST":
        action = request.form.get("_action", "add")

        if action in ("edit", "delete") and session.get("role") != "admin":
            db.close()
            return "Access denied: admins only.", 403

        fd = request.form.get("from_date", "").strip()
        td = request.form.get("to_date",   "").strip()
        redirect_args = {"from": fd, "to": td} if fd and td else {}

        if action == "delete":
            del_date = request.form.get("del_date", "").strip()
            if del_date:
                cursor.execute("DELETE FROM bookings WHERE booking_date = %s", (del_date,))
                db.commit()
            db.close()
            return redirect(url_for("day_wise", **redirect_args))

        entry_date = request.form.get("entry_date", "").strip()

        try:
            total_weight = float(request.form.get("total_weight") or 0)
        except (TypeError, ValueError):
            total_weight = 0.0

        try:
            total_sales = float(request.form.get("total_sales") or 0)
        except (TypeError, ValueError):
            total_sales = 0.0

        if not entry_date:
            db.close()
            return "Date is required", 400

        if action == "edit":
            old_date = request.form.get("edit_id", "").strip()
            if old_date:
                cursor.execute("DELETE FROM bookings WHERE booking_date = %s", (old_date,))

            insert_manual_row(entry_date, total_weight, total_sales)
            db.commit()
        else:
            insert_manual_row(entry_date, total_weight, total_sales)
            db.commit()

        db.close()
        return redirect(url_for("day_wise", **redirect_args))

    from_date = request.args.get("from", "").strip()
    to_date   = request.args.get("to",   "").strip()

    if from_date and to_date:
        cursor.execute("""
            SELECT booking_date, SUM(weight) AS total_weight, SUM(total_amount) AS total_sales
            FROM bookings WHERE booking_date BETWEEN %s AND %s
            GROUP BY booking_date ORDER BY booking_date ASC
        """, (from_date, to_date))
    else:
        cursor.execute("""
            SELECT booking_date, SUM(weight) AS total_weight, SUM(total_amount) AS total_sales
            FROM bookings GROUP BY booking_date ORDER BY booking_date ASC
        """)

    rows = cursor.fetchall()
    db.close()

    from datetime import datetime
    for r in rows:
        if hasattr(r["booking_date"], "strftime"):
            r["entry_date_iso"] = r["booking_date"].isoformat()
            r["entry_date"]     = r["booking_date"].strftime("%d-%m-%Y")
        else:
            try:
                d = datetime.strptime(str(r["booking_date"]), "%Y-%m-%d")
                r["entry_date_iso"] = d.strftime("%Y-%m-%d")
                r["entry_date"]     = d.strftime("%d-%m-%Y")
            except Exception:
                r["entry_date_iso"] = str(r["booking_date"])
                r["entry_date"]     = str(r["booking_date"])
        r["total_weight"] = round(float(r["total_weight"] or 0), 3)
        r["total_sales"]  = round(float(r["total_sales"]  or 0), 2)

    grand_weight = round(sum(r["total_weight"] for r in rows), 3)
    grand_sales  = round(sum(r["total_sales"]  for r in rows), 2)

    return render_template("day_wise.html", rows=rows,
                           from_date=from_date, to_date=to_date,
                           grand_weight=grand_weight, grand_sales=grand_sales,
                           role=session.get("role"))


# ============================================================
# DAY WISE PDF
# ============================================================
@app.route("/day-wise-pdf", methods=["POST"])
@login_required
def day_wise_pdf():
    from datetime import datetime

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    from_date = request.form.get("from_date", "").strip()
    to_date   = request.form.get("to_date",   "").strip()

    if from_date and to_date:
        cursor.execute("""
            SELECT booking_date, SUM(weight) AS total_weight, SUM(total_amount) AS total_sales
            FROM bookings WHERE booking_date BETWEEN %s AND %s
            GROUP BY booking_date ORDER BY booking_date ASC
        """, (from_date, to_date))
    else:
        cursor.execute("""
            SELECT booking_date, SUM(weight) AS total_weight, SUM(total_amount) AS total_sales
            FROM bookings GROUP BY booking_date ORDER BY booking_date ASC
        """)

    rows = cursor.fetchall()
    db.close()

    if not rows:
        return jsonify({"status": "error", "message": "No records found"})

    formatted = []
    for r in rows:
        if hasattr(r["booking_date"], "strftime"):
            date_str = r["booking_date"].strftime("%d-%m-%Y")
        else:
            try:
                date_str = datetime.strptime(str(r["booking_date"]), "%Y-%m-%d").strftime("%d-%m-%Y")
            except Exception:
                date_str = str(r["booking_date"])
        formatted.append({
            "entry_date":   date_str,
            "total_weight": round(float(r["total_weight"] or 0), 3),
            "total_sales":  round(float(r["total_sales"]  or 0), 2),
        })

    grand_weight = round(sum(r["total_weight"] for r in formatted), 3)
    grand_sales  = round(sum(r["total_sales"]  for r in formatted), 2)

    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4,
                            leftMargin=15*mm, rightMargin=15*mm,
                            topMargin=15*mm, bottomMargin=15*mm)

    title_style = ParagraphStyle('title', fontSize=13, alignment=TA_CENTER,
                                 spaceAfter=4, fontName='Helvetica-Bold')
    sub_style   = ParagraphStyle('sub',   fontSize=9,  alignment=TA_CENTER,
                                 spaceAfter=8, textColor=colors.grey)

    elements = []
    elements.append(Paragraph("Professional Couriers", title_style))
    elements.append(Paragraph("Day Wise Report", title_style))
    if from_date and to_date:
        elements.append(Paragraph(f"Period: {from_date}  to  {to_date}", sub_style))
    elements.append(Spacer(1, 4*mm))

    header = ['SNO', 'DATE', 'Total Weight', 'Total Sales Amount']
    data   = [header]

    for i, r in enumerate(formatted, start=1):
        data.append([str(i), r["entry_date"], f"{r['total_weight']:.3f}", f"{r['total_sales']:.2f}"])

    data.append(['', 'Grand Total', f"{grand_weight:.3f}", f"{grand_sales:.2f}"])

    col_widths = [20*mm, 45*mm, 55*mm, 55*mm]
    total_rows = len(data)

    table = Table(data, colWidths=col_widths, repeatRows=1)
    table.setStyle(TableStyle([
        ('BACKGROUND',    (0, 0),  (-1, 0),              colors.HexColor('#212529')),
        ('TEXTCOLOR',     (0, 0),  (-1, 0),              colors.white),
        ('FONTNAME',      (0, 0),  (-1, 0),              'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0),  (-1, 0),              9),
        ('ALIGN',         (0, 0),  (-1, 0),              'CENTER'),
        ('VALIGN',        (0, 0),  (-1, 0),              'MIDDLE'),
        ('TOPPADDING',    (0, 0),  (-1, 0),              5),
        ('BOTTOMPADDING', (0, 0),  (-1, 0),              5),
        ('FONTNAME',      (0, 1),  (-1, -2),             'Helvetica'),
        ('FONTSIZE',      (0, 1),  (-1, -2),             9),
        ('VALIGN',        (0, 1),  (-1, -2),             'MIDDLE'),
        ('TOPPADDING',    (0, 1),  (-1, -2),             3),
        ('BOTTOMPADDING', (0, 1),  (-1, -2),             3),
        ('ALIGN',         (0, 1),  (0,  -2),             'CENTER'),
        ('ALIGN',         (1, 1),  (1,  -2),             'CENTER'),
        ('ALIGN',         (2, 1),  (-1, -2),             'RIGHT'),
        *[('BACKGROUND', (0, i), (-1, i), colors.HexColor('#f8f9fa'))
          for i in range(2, total_rows - 1, 2)],
        ('GRID',          (0, 0),  (-1, -2),             0.4, colors.grey),
        ('BOX',           (0, 0),  (-1, -1),             0.8, colors.black),
        ('LINEABOVE',     (0, -1), (-1, -1),             1.2, colors.black),
        ('BACKGROUND',    (0, -1), (-1, -1),             colors.HexColor('#e9ecef')),
        ('FONTNAME',      (0, -1), (-1, -1),             'Helvetica-Bold'),
        ('FONTSIZE',      (0, -1), (-1, -1),             9),
        ('ALIGN',         (1, -1), (1,  -1),             'RIGHT'),
        ('ALIGN',         (2, -1), (-1, -1),             'RIGHT'),
        ('TOPPADDING',    (0, -1), (-1, -1),             4),
        ('BOTTOMPADDING', (0, -1), (-1, -1),             4),
    ]))

    elements.append(table)
    doc.build(elements)
    buf.seek(0)

    base = "DayWise_Report"
    if from_date and to_date:
        base += f"_{from_date}_to_{to_date}"

    path = save_to_downloads(buf.read(), base_name=base, ext=".pdf")
    return jsonify({"status": "ok", "path": path})

# ============================================================
# DAY BOOK
# ============================================================
@app.route("/day-book", methods=["GET"])
@login_required
def day_book():
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    selected_date = request.args.get("date", "").strip()
    from_date     = request.args.get("from_date", "").strip()
    to_date       = request.args.get("to_date", "").strip()

    if selected_date:
        cursor.execute("""
            SELECT weight, awb_no, destination, total_amount
            FROM bookings WHERE booking_date = %s
            ORDER BY weight ASC, awb_no ASC
        """, (selected_date,))
    elif from_date and to_date:
        cursor.execute("""
            SELECT weight, awb_no, destination, total_amount
            FROM bookings WHERE booking_date BETWEEN %s AND %s
            ORDER BY weight ASC, awb_no ASC
        """, (from_date, to_date))
    elif from_date:
        cursor.execute("""
            SELECT weight, awb_no, destination, total_amount
            FROM bookings WHERE booking_date >= %s
            ORDER BY weight ASC, awb_no ASC
        """, (from_date,))
    else:
        cursor.execute("""
            SELECT weight, awb_no, destination, total_amount
            FROM bookings ORDER BY weight ASC, awb_no ASC
        """)

    rows = cursor.fetchall()
    db.close()

    groups      = []
    grand_total = 0.0
    for weight_val, grp in groupby(rows, key=lambda r: float(r["weight"] or 0)):
        grp_list = list(grp)
        subtotal = sum(float(r["total_amount"] or 0) for r in grp_list)
        grand_total += subtotal
        groups.append({"weight": f"{weight_val:.3f}", "rows": grp_list, "subtotal": subtotal})

    return render_template("day_book.html",
                           groups=groups, grand_total=grand_total,
                           selected_date=selected_date,
                           from_date=from_date, to_date=to_date,
                           role=session.get("role"))


# ============================================================
# DAY BOOK PDF
# ============================================================
@app.route("/day-book-pdf", methods=["POST"])
@login_required
def day_book_pdf():
    from datetime import datetime
 
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
 
    selected_date = request.form.get("date", "").strip()
    from_date     = request.form.get("from_date", "").strip()
    to_date       = request.form.get("to_date", "").strip()
    code          = request.form.get("code", "").strip()
    weight        = request.form.get("weight", "").strip()
    awb_no        = request.form.get("awb_no", "").strip()
    destination   = request.form.get("destination", "").strip()
    courier       = request.form.get("courier", "").strip()
 
    query  = """SELECT code, code_fullform, booking_date, awb_no, destination,
                       weight, courier, total_amount
                FROM bookings WHERE 1=1"""
    params = []
 
    if selected_date:
        query += " AND booking_date = %s"
        params.append(selected_date)
    elif from_date and to_date:
        query += " AND booking_date BETWEEN %s AND %s"
        params.extend([from_date, to_date])
    elif from_date:
        query += " AND booking_date >= %s"
        params.append(from_date)
    elif to_date:
        query += " AND booking_date <= %s"
        params.append(to_date)
 
    if code:
        query += " AND (code LIKE %s OR code_fullform LIKE %s)"
        params.append(f"%{code}%")
        params.append(f"%{code}%")
    if weight:
        query += " AND weight = %s"
        params.append(float(weight))
    if awb_no:
        query += " AND awb_no LIKE %s"
        params.append(f"%{awb_no}%")
    if destination:
        query += " AND destination LIKE %s"
        params.append(f"%{destination}%")
    if courier:
        query += " AND courier LIKE %s"
        params.append(f"%{courier}%")
 
    query += " ORDER BY booking_date ASC, awb_no ASC"
 
    cursor.execute(query, params)
    rows = cursor.fetchall()
    db.close()
 
    if not rows:
        return jsonify({"status": "error", "message": "No records found"})
 
    distinct_couriers = sorted({
        (r["courier"] or "").strip() for r in rows if (r["courier"] or "").strip()
    })
    if not courier:
        courier_display = "ALL"
    elif len(distinct_couriers) == 1:
        courier_display = distinct_couriers[0]
    elif len(distinct_couriers) > 1:
        courier_display = ", ".join(distinct_couriers)
    else:
        courier_display = courier.upper()
 
    def fmt_date(d):
        if not d:
            return ""
        if hasattr(d, "strftime"):
            return d.strftime("%d-%m-%Y")
        try:
            return datetime.strptime(str(d), "%Y-%m-%d").strftime("%d-%m-%Y")
        except Exception:
            return str(d)
 
    if selected_date:
        date_range_display = fmt_date(selected_date)
    elif from_date and to_date:
        date_range_display = f"{fmt_date(from_date)}  to  {fmt_date(to_date)}"
    elif from_date:
        date_range_display = f"From {fmt_date(from_date)}"
    else:
        date_range_display = "All Dates"
 
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=landscape(A4),
                            leftMargin=10*mm, rightMargin=10*mm,
                            topMargin=12*mm, bottomMargin=12*mm)
 
    title_style = ParagraphStyle('title', fontSize=14, alignment=TA_CENTER,
                                 spaceAfter=2, fontName='Helvetica-Bold')
    sub_style   = ParagraphStyle('sub',   fontSize=9,  alignment=TA_CENTER,
                                 spaceAfter=8, textColor=colors.grey)
 
    elements = []
    elements.append(Paragraph("Professional Couriers", title_style))
    elements.append(Paragraph("Day Book", title_style))
    elements.append(Paragraph(
        f"Date: {date_range_display} &nbsp;&nbsp;|&nbsp;&nbsp; Courier: <b>{courier_display}</b>",
        sub_style
    ))
    elements.append(Spacer(1, 3*mm))
 
    header = ['S NO.', 'Code', 'Company Name', 'Date', 'AWB Number',
              'Destination', 'Weight', 'Courier', 'Total Amount']
    col_widths = [12*mm, 18*mm, 58*mm, 22*mm, 32*mm,
                  44*mm, 18*mm, 34*mm, 24*mm]
 
    data        = [header]
    style_cmds  = []
    row_idx     = 1
    sno         = 0
    sum_weight  = 0.0
    sum_total   = 0.0
 
    for r in rows:
        sno   += 1
        wt     = float(r["weight"] or 0)
        total  = float(r["total_amount"] or 0)
        sum_weight += wt
        sum_total  += total
 
        bg = colors.white if (row_idx % 2 == 1) else colors.HexColor('#f8f9fa')
        style_cmds.append(('BACKGROUND', (0, row_idx), (-1, row_idx), bg))
 
        data.append([
            str(sno),
            str(r["code"] or ""),
            str(r["code_fullform"] or ""),
            fmt_date(r["booking_date"]),
            str(r["awb_no"] or ""),
            str(r["destination"] or ""),
            f"{wt:.3f}",
            str(r["courier"] or ""),
            f"{total:.2f}",
        ])
        row_idx += 1
 
    data.append(['', '', '', '', '', 'Grand Total', f"{sum_weight:.3f}", '',
                 f"{sum_total:.2f}"])
    total_rows = len(data)
 
    base_cmds = [
        ('BACKGROUND',    (0, 0),  (-1, 0),              colors.HexColor('#212529')),
        ('TEXTCOLOR',     (0, 0),  (-1, 0),              colors.white),
        ('FONTNAME',      (0, 0),  (-1, 0),              'Helvetica-Bold'),
        ('FONTSIZE',      (0, 0),  (-1, 0),              8),
        ('ALIGN',         (0, 0),  (-1, 0),              'CENTER'),
        ('VALIGN',        (0, 0),  (-1, 0),              'MIDDLE'),
        ('TOPPADDING',    (0, 0),  (-1, 0),              5),
        ('BOTTOMPADDING', (0, 0),  (-1, 0),              5),
        ('FONTNAME',      (0, 1),  (-1, -2),             'Helvetica'),
        ('FONTSIZE',      (0, 1),  (-1, -2),             8),
        ('VALIGN',        (0, 1),  (-1, -2),             'MIDDLE'),
        ('TOPPADDING',    (0, 1),  (-1, -2),             3),
        ('BOTTOMPADDING', (0, 1),  (-1, -2),             3),
        ('ALIGN',         (0, 1),  (1,  -2),             'CENTER'),
        ('ALIGN',         (3, 1),  (3,  -2),             'CENTER'),
        ('ALIGN',         (6, 1),  (6,  -2),             'RIGHT'),
        ('ALIGN',         (7, 1),  (7,  -2),             'CENTER'),
        ('ALIGN',         (8, 1),  (8,  -2),             'RIGHT'),
        ('GRID',          (0, 0),  (-1, -2),             0.4, colors.grey),
        ('BOX',           (0, 0),  (-1, -1),             0.8, colors.black),
        ('LINEABOVE',     (0, -1), (-1, -1),             1.2, colors.black),
        ('BACKGROUND',    (0, -1), (-1, -1),             colors.HexColor('#e9ecef')),
        ('FONTNAME',      (0, -1), (-1, -1),             'Helvetica-Bold'),
        ('FONTSIZE',      (0, -1), (-1, -1),             8),
        ('ALIGN',         (5, -1), (5,  -1),             'RIGHT'),
        ('ALIGN',         (6, -1), (6,  -1),             'RIGHT'),
        ('ALIGN',         (8, -1), (8,  -1),             'RIGHT'),
        ('TOPPADDING',    (0, -1), (-1, -1),             4),
        ('BOTTOMPADDING', (0, -1), (-1, -1),             4),
        *[('BACKGROUND', (0, i), (-1, i), colors.HexColor('#f8f9fa'))
          for i in range(2, total_rows - 1, 2)],
    ]
 
    table = Table(data, colWidths=col_widths, rowHeights=None, repeatRows=1)
    table.setStyle(TableStyle(base_cmds + style_cmds))
 
    elements.append(table)
    doc.build(elements)
    buf.seek(0)
 
    base = "DayBook"
    if selected_date:
        try:
            base += f"_{datetime.strptime(selected_date, '%Y-%m-%d').strftime('%d-%m-%Y')}"
        except Exception:
            base += f"_{selected_date}"
    elif from_date and to_date:
        base += f"_{from_date}_to_{to_date}"
    if courier:
        base += f"_{courier_display.replace(' ', '_')}"
 
    path = save_to_downloads(buf.read(), base_name=base, ext=".pdf")
    return jsonify({"status": "ok", "path": path})

# ============================================================
# EXPORTS  (all now save to Downloads with a clean filename)
# ============================================================
@app.route("/invoice-export", methods=["POST"])
@login_required
def invoice_export():
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    try:    fuel_rate = float(request.form.get("fuel_rate", 0))
    except: fuel_rate = 0.0

    from_date = request.form.get("from_date", "")
    to_date   = request.form.get("to_date", "")
    code      = request.form.get("code", "ALL")

    fuel_rate = _resolve_fuel_rate(cursor, code, fuel_rate)

    query  = """SELECT booking_date AS `DATE`, destination AS `DESTINATION`,
                       awb_no AS `AWB NO`, weight AS `WEIGHT`,
                       total_amount AS `Total`
                FROM bookings WHERE 1=1"""
    params = []
    if from_date and to_date:
        query += " AND booking_date BETWEEN %s AND %s"
        params.extend([from_date, to_date])
    if code and code != "ALL":
        query += " AND code=%s"
        params.append(code)
    query += " ORDER BY booking_date ASC, awb_no ASC"
    cursor.execute(query, params)
    df = pd.DataFrame(cursor.fetchall())

    if fuel_rate > 0 and not df.empty:
        df['Fuel']        = (df['Total'].astype(float) * fuel_rate / 100).round(2)
        df['Grand Total'] = (df['Total'].astype(float) + df['Fuel']).round(2)

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        info_df = pd.DataFrame([
            ["Company Name:",       "Professional Couriers"],
            ["Transaction Period:", f"{from_date} to {to_date}"],
            ["Customer Code:",      code],
            ["", ""],
        ])
        info_df.to_excel(writer, index=False, header=False, sheet_name="Invoice", startrow=0)
        df.to_excel(writer, index=False, sheet_name="Invoice", startrow=5)
    output.seek(0)
    db.close()

    base = f"Invoice_{code}_{from_date}_to_{to_date}" if (from_date and to_date) else f"Invoice_{code}"
    path = save_to_downloads(output.read(), base_name=base)
    return jsonify({"status": "ok", "path": path})


@app.route("/zone-export")
@admin_required
def zone_export():
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    cursor.execute("SELECT district, rate_zone FROM zones ORDER BY id")
    df = pd.DataFrame(cursor.fetchall())
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)
    db.close()
    path = save_to_downloads(output.read(), base_name="ZoneExport")
    return jsonify({"status": "ok", "path": path})


@app.route("/rate-export")
@admin_required
def rate_export():
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT code AS `CODE`, code_fullform AS `CODE FULL FORM`,
               place AS `PLACE`, zone AS `ZONE`,
               rate_250g AS `250 G Dx`, rate_500g AS `0.500 g`,
               rate_500g_1 AS `0.500 g 1`, rate_1_to_3kg AS `Add 1 to 3 Kg`,
               rate_3_to_10kg AS `Above 3-10 Kg`, rate_above_10kg AS `Above 10 Kg`,
               fuel AS `Fuel`
        FROM rates ORDER BY zone, code, id
    """)
    df = pd.DataFrame(cursor.fetchall())
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)
    db.close()
    path = save_to_downloads(output.read(), base_name="RateExport")
    return jsonify({"status": "ok", "path": path})


@app.route("/booking-export")
@login_required
def booking_export():
    from datetime import datetime

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    cursor.execute("""
        SELECT code, booking_date, awb_no, destination, weight, zone, auto_amount
        FROM bookings ORDER BY id DESC
    """)
    rows = cursor.fetchall()
    db.close()

    def fmt_date(d):
        if not d: return ""
        if hasattr(d, "strftime"): return d.strftime("%d-%m-%Y")
        try:
            return datetime.strptime(str(d), "%Y-%m-%d").strftime("%d-%m-%Y")
        except Exception:
            return str(d)

    records = []
    for i, r in enumerate(rows, start=1):
        records.append({
            "S NO.":       i,
            "Code":        r["code"] or "",
            "Date":        fmt_date(r["booking_date"]),
            "AWB Number":  str(r["awb_no"] or ""),
            "Destination": r["destination"] or "",
            "Weight":      round(float(r["weight"] or 0), 3),
            "Zone":        r["zone"] or "",
            "Amount":      int(round(float(r["auto_amount"] or 0))),
        })

    df = pd.DataFrame(records, columns=[
        "S NO.", "Code", "Date", "AWB Number",
        "Destination", "Weight", "Zone", "Amount"
    ])

    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Bookings")
        ws = writer.sheets["Bookings"]
        for cell in ws["F"][1:]:
            cell.number_format = "0.000"
        for cell in ws["D"][1:]:
            cell.number_format = "@"
    output.seek(0)

    today_str = date.today().strftime("%d-%m-%Y")
    path = save_to_downloads(output.read(), base_name=f"Bookings_{today_str}")
    return jsonify({"status": "ok", "path": path})


@app.route("/sales-export", methods=["POST"])
@login_required
def sales_export():
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    query  = """SELECT code AS `Client Code`, code_fullform AS `Company Name`,
                       awb_no AS `AWB No`, destination AS `Destination`,
                       COUNT(*) AS `Sum`, SUM(total_amount) AS `Amount`
                FROM bookings WHERE 1=1"""
    params = []
    if request.form.get("client_code"):
        query += " AND (code LIKE %s OR code_fullform LIKE %s)"
        params.append(f"%{request.form['client_code']}%")
        params.append(f"%{request.form['client_code']}%")
    if request.form.get("awb_no"):
        query += " AND awb_no LIKE %s"
        params.append(f"%{request.form['awb_no']}%")
    if request.form.get("destination"):
        query += " AND destination LIKE %s"
        params.append(f"%{request.form['destination']}%")
    if request.form.get("date"):
        query += " AND booking_date = %s"
        params.append(request.form["date"])
    query += " GROUP BY code, code_fullform, awb_no, destination ORDER BY MAX(id) DESC"
    cursor.execute(query, params)
    df = pd.DataFrame(cursor.fetchall())
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)
    db.close()
    path = save_to_downloads(output.read(), base_name="SalesChecking")
    return jsonify({"status": "ok", "path": path})


@app.route("/day-wise-export", methods=["POST"])
@login_required
def day_wise_export():
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
    query = """
        SELECT booking_date AS `DATE`,
               SUM(weight)       AS `Total Weight`,
               SUM(total_amount) AS `Total Sales Amount`
        FROM bookings WHERE 1=1
    """
    params = []
    fd = request.form.get("from_date", "")
    td = request.form.get("to_date", "")
    if fd and td:
        query += " AND booking_date BETWEEN %s AND %s"
        params.extend([fd, td])
    query += " GROUP BY booking_date ORDER BY booking_date"
    cursor.execute(query, params)
    df = pd.DataFrame(cursor.fetchall())
    output = io.BytesIO()
    df.to_excel(output, index=False)
    output.seek(0)
    db.close()
    base = f"DayWise_{fd}_to_{td}" if (fd and td) else "DayWise"
    path = save_to_downloads(output.read(), base_name=base)
    return jsonify({"status": "ok", "path": path})


# ============================================================
# DAY BOOK EXPORT  (fixed: includes Auto Amount, Fuel, Total Amount
#                   so re-import produces identical totals)
# ============================================================
@app.route("/day-book-export", methods=["POST"])
@admin_required
def day_book_export():
    from datetime import datetime
 
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
 
    query = """
        SELECT code, code_fullform, booking_date, awb_no, destination,
               weight, courier, zone, auto_amount, fuel, total_amount
        FROM bookings WHERE 1=1
    """
    params = []
 
    from_date = request.form.get("from_date", "").strip()
    to_date   = request.form.get("to_date", "").strip()
 
    if from_date and to_date:
        query += " AND booking_date BETWEEN %s AND %s"
        params.extend([from_date, to_date])
    elif from_date:
        query += " AND booking_date >= %s"
        params.append(from_date)
    elif to_date:
        query += " AND booking_date <= %s"
        params.append(to_date)
 
    if request.form.get("code"):
        query += " AND (code LIKE %s OR code_fullform LIKE %s)"
        code_like = f"%{request.form['code']}%"
        params.append(code_like)
        params.append(code_like)
    if request.form.get("weight"):
        query += " AND weight = %s"
        params.append(float(request.form["weight"]))
    if request.form.get("awb_no"):
        query += " AND awb_no LIKE %s"
        params.append(f"%{request.form['awb_no']}%")
    if request.form.get("destination"):
        query += " AND destination LIKE %s"
        params.append(f"%{request.form['destination']}%")
    if request.form.get("courier"):
        query += " AND courier LIKE %s"
        params.append(f"%{request.form['courier']}%")
 
    query += " ORDER BY booking_date ASC, awb_no ASC"
 
    cursor.execute(query, params)
    rows = cursor.fetchall()
    db.close()
 
    def fmt_date(d):
        if not d: return ""
        if hasattr(d, "strftime"): return d.strftime("%d-%m-%Y")
        try:
            return datetime.strptime(str(d), "%Y-%m-%d").strftime("%d-%m-%Y")
        except Exception:
            return str(d)
 
    records = []
    for i, r in enumerate(rows, start=1):
        records.append({
            "S NO.":        i,
            "Code":         r["code"] or "",
            "Company Name": r["code_fullform"] or "",
            "Date":         fmt_date(r["booking_date"]),
            "AWB Number":   str(r["awb_no"] or ""),
            "Destination":  r["destination"] or "",
            "Weight":       round(float(r["weight"] or 0), 3),
            "Zone":         r["zone"] or "",
            "Courier":      r["courier"] or "ST COURIER",
            "Auto Amount":  round(float(r["auto_amount"] or 0), 2),
            "Fuel":         round(float(r["fuel"] or 0), 2),
            "Total Amount": round(float(r["total_amount"] or 0), 2),
        })
 
    df = pd.DataFrame(records, columns=[
        "S NO.", "Code", "Company Name", "Date", "AWB Number",
        "Destination", "Weight", "Zone", "Courier",
        "Auto Amount", "Fuel", "Total Amount"
    ])
 
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Day Book")
        ws = writer.sheets["Day Book"]
        for cell in ws["G"][1:]:
            cell.number_format = "0.000"
        for cell in ws["E"][1:]:
            cell.number_format = "@"
        for col in ["J", "K", "L"]:
            for cell in ws[col][1:]:
                cell.number_format = "#,##0.00"
    output.seek(0)
 
    base = f"DayBook_{from_date}_to_{to_date}" if (from_date and to_date) else "DayBook"
    path = save_to_downloads(output.read(), base_name=base)
    return jsonify({"status": "ok", "path": path})


# ============================================================
# INVOICE PDF
# ============================================================
class WatermarkDocTemplate(SimpleDocTemplate):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.watermark_text = "AIKONIX"

    def afterPage(self):
        self.canv.saveState()
        self.canv.setFont('Helvetica', 80)
        self.canv.setFillColor(colors.Color(red=0.5, green=0.5, blue=0.5, alpha=0.25))
        pw, ph = A4
        self.canv.translate(pw / 2, ph / 2)
        self.canv.rotate(45)
        self.canv.drawCentredString(0, 0, self.watermark_text)
        self.canv.restoreState()


@app.route("/invoice-pdf", methods=["GET", "POST"])
@login_required
def invoice_pdf():
    if request.method == "GET":
        return redirect(url_for("invoice"))

    def s(v):
        return '' if v is None else str(v)

    from datetime import datetime
    from itertools import groupby as _groupby

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    from_date    = request.form.get("from_date", "").strip()
    to_date      = request.form.get("to_date",   "").strip()
    code         = request.form.get("code", "").strip()
    try:
        manual_fuel_percent = float(request.form.get("fuel_rate", 0) or 0)
    except:
        manual_fuel_percent = 0.0

    filter_code = None if (not code or code == "ALL") else code

    query  = """SELECT code, code_fullform, booking_date, destination, awb_no,
                       weight, auto_amount, fuel, total_amount
                FROM bookings WHERE booking_date IS NOT NULL"""
    params = []
    if from_date and to_date:
        query += " AND booking_date BETWEEN %s AND %s"
        params.extend([from_date, to_date])
    if filter_code:
        query += " AND code = %s"
        params.append(filter_code)
    query += " ORDER BY code ASC, booking_date ASC, awb_no ASC"
    cursor.execute(query, params)
    rows = cursor.fetchall()

    if not rows:
        db.close()
        return jsonify({"status": "error", "message": "No records found"})

    def fmt_date(d):
        if not d: return ""
        try:
            if hasattr(d, 'strftime'): return d.strftime('%d/%m/%Y')
            return datetime.strptime(str(d), "%Y-%m-%d").strftime('%d/%m/%Y')
        except: return str(d)

    from_display = fmt_date(from_date)
    to_display   = fmt_date(to_date)
    month_str    = ""
    if from_date:
        try:
            month_str = datetime.strptime(from_date, "%Y-%m-%d").strftime("%B %Y")
        except: pass

    buf = io.BytesIO()
    doc = WatermarkDocTemplate(buf, pagesize=A4,
                               leftMargin=12*mm, rightMargin=12*mm,
                               topMargin=12*mm, bottomMargin=12*mm)

    logo_style = ParagraphStyle(
        'logo', fontSize=16, fontName='Helvetica-Bold',
        textColor=colors.HexColor('#0d6efd')
    )
    stmt_sub_style = ParagraphStyle(
        'stmtsub', fontSize=8, fontName='Helvetica',
        alignment=TA_CENTER, spaceAfter=0,
        leading=14
    )

    usable_w = A4[0] - 24*mm
    left_w   = 48*mm
    right_w  = usable_w - left_w

    col_widths  = [14*mm, 26*mm, 52*mm, 52*mm, 22*mm, 22*mm]
    header_row  = ['SNO', 'DATE', 'DESTINATION', 'AWB NO', 'WEIGHT', 'Total']
    ROW_H       = 6*mm
    HEADER_H    = 8*mm

    def build_group_elements(group_code, group_rows, is_first_group):
        group_elements = []
        if not is_first_group:
            group_elements.append(PageBreak())

        company_name = (group_rows[0].get("code_fullform") or "").strip()

        fuel_percent = _resolve_fuel_rate(cursor, group_code or "ALL", manual_fuel_percent)

        company_display = company_name or group_code or 'ALL'
        if len(company_display) > 25:
            company_display = company_display[:25] + '...'

        stmt_para = Paragraph(
            f"<b>STATEMENT OF TRANSACTION</b>"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;"
            f"BETWEEN &nbsp;<b>{from_display}</b>&nbsp; AND &nbsp;<b>{to_display}</b>"
            f"<br/>"
            f"CUSTOMER : <b>{company_display}</b>"
            f"&nbsp;&nbsp;|&nbsp;&nbsp;"
            f"MONTH : <b>{month_str}</b>",
            stmt_sub_style
        )

        inner_table = Table([[stmt_para]], colWidths=[right_w])
        inner_table.setStyle(TableStyle([
            ('ALIGN',         (0,0), (0,0), 'CENTER'),
            ('VALIGN',        (0,0), (0,0), 'MIDDLE'),
            ('LEFTPADDING',   (0,0), (0,0), 0),
            ('RIGHTPADDING',  (0,0), (0,0), 0),
            ('TOPPADDING',    (0,0), (0,0), 0),
            ('BOTTOMPADDING', (0,0), (0,0), 0),
        ]))

        header_data = [[ Paragraph("Z XPRESS", logo_style), inner_table ]]
        hdr_table   = Table(header_data, colWidths=[left_w, right_w])
        hdr_table.setStyle(TableStyle([
            ('VALIGN',        (0,0), (-1,-1), 'MIDDLE'),
            ('ALIGN',         (0,0), (-1,-1), 'CENTER'),
            ('LEFTPADDING',   (0,0), (0,0),   0),
            ('RIGHTPADDING',  (0,0), (0,0),   0),
            ('TOPPADDING',    (0,0), (-1,-1), 0),
            ('BOTTOMPADDING', (0,0), (-1,-1), 0),
        ]))

        group_elements.append(hdr_table)
        group_elements.append(Spacer(1, 6*mm))

        data        = [header_row]
        row_heights = [HEADER_H]
        style_cmds  = []
        row_idx     = 1
        sno         = 0
        sum_courier = 0.0
        sum_fuel    = 0.0
        prev_date   = None

        for r in group_rows:
            sno        += 1
            auto_amt    = float(r.get("auto_amount") or 0)
            fuel_amt    = float(r.get("fuel")        or 0)
            sum_courier += auto_amt
            sum_fuel    += fuel_amt
            date_str  = fmt_date(r["booking_date"])
            show_date = date_str if date_str != prev_date else ""
            prev_date = date_str
            bg = colors.white if (row_idx % 2 == 1) else colors.HexColor('#f5f5f5')
            style_cmds.append(('BACKGROUND', (0, row_idx), (-1, row_idx), bg))
            data.append([str(sno), show_date, s(r["destination"]), s(r["awb_no"]),
                         f"{float(r['weight'] or 0):.3f}", f"{auto_amt:.2f}"])
            row_heights.append(ROW_H)
            row_idx += 1

        data.append(['', '', '', '', 'Courier Charges Total :', f"{sum_courier:.2f}"])
        row_heights.append(ROW_H + 1*mm)
        courier_row = row_idx; row_idx += 1

        fsc_row     = None
        grand_total = sum_courier

        if fuel_percent > 0:
            fuel_charge = round(sum_courier * fuel_percent / 100, 2)
            grand_total = round(sum_courier + fuel_charge, 2)
            data.append(['', '', '', f"Add @ {fuel_percent:.0f}% on {sum_courier:.2f}",
                         'Fuel Charges :', f"{fuel_charge:.2f}"])
            row_heights.append(ROW_H)
            fsc_row = row_idx; row_idx += 1
        elif sum_fuel > 0:
            grand_total = round(sum_courier + sum_fuel, 2)
            data.append(['', '', '', '', 'Fuel Charges :', f"{sum_fuel:.2f}"])
            row_heights.append(ROW_H)
            fsc_row = row_idx; row_idx += 1

        data.append(['', '', '', '', 'Total Gross Amount :', f"{grand_total:.2f}"])
        row_heights.append(ROW_H + 2*mm)
        grand_row  = row_idx
        total_rows = len(data)

        base_cmds = [
            ('BACKGROUND',    (0,0),  (-1,0),  colors.HexColor('#1a1a1a')),
            ('TEXTCOLOR',     (0,0),  (-1,0),  colors.white),
            ('FONTNAME',      (0,0),  (-1,0),  'Helvetica-Bold'),
            ('FONTSIZE',      (0,0),  (-1,0),  9),
            ('ALIGN',         (0,0),  (-1,0),  'CENTER'),
            ('VALIGN',        (0,0),  (-1,0),  'MIDDLE'),
            ('TOPPADDING',    (0,0),  (-1,0),  6),
            ('BOTTOMPADDING', (0,0),  (-1,0),  6),
            ('FONTSIZE',      (0,1),  (-1,-1), 9),
            ('FONTNAME',      (0,1),  (-1,-1), 'Helvetica'),
            ('VALIGN',        (0,1),  (-1,-1), 'MIDDLE'),
            ('TOPPADDING',    (0,1),  (-1,-1), 3),
            ('BOTTOMPADDING', (0,1),  (-1,-1), 3),
            ('ALIGN', (0,1),  (0,-1),  'CENTER'),
            ('ALIGN', (1,1),  (1,-1),  'CENTER'),
            ('ALIGN', (2,1),  (3,-1),  'LEFT'),
            ('ALIGN', (4,1),  (-1,-1), 'RIGHT'),
            ('GRID',  (0,0),  (-1, courier_row-1), 0.5, colors.HexColor('#cccccc')),
            ('BOX',   (0,0),  (-1,-1), 1.0, colors.black),
            ('LINEABOVE',  (0, courier_row), (-1, courier_row), 1.5, colors.black),
            ('BACKGROUND', (0, courier_row), (-1, courier_row), colors.HexColor('#e8e8e8')),
            ('FONTNAME',   (0, courier_row), (-1, courier_row), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, courier_row), (-1, courier_row), 9),
            ('ALIGN',      (0, courier_row), (-1, courier_row), 'RIGHT'),
            ('BACKGROUND', (0, grand_row), (-1, grand_row), colors.HexColor('#1a1a1a')),
            ('TEXTCOLOR',  (0, grand_row), (-1, grand_row), colors.white),
            ('FONTNAME',   (0, grand_row), (-1, grand_row), 'Helvetica-Bold'),
            ('FONTSIZE',   (0, grand_row), (-1, grand_row), 10),
            ('ALIGN',      (0, grand_row), (-1, grand_row), 'RIGHT'),
            ('TOPPADDING', (0, grand_row), (-1, grand_row), 6),
            ('BOTTOMPADDING', (0, grand_row), (-1, grand_row), 6),
        ]

        if fsc_row is not None:
            base_cmds += [
                ('BACKGROUND', (0, fsc_row), (-1, fsc_row), colors.HexColor('#fff8e1')),
                ('FONTNAME',   (0, fsc_row), (-1, fsc_row), 'Helvetica-Bold'),
                ('TEXTCOLOR',  (0, fsc_row), (-1, fsc_row), colors.HexColor('#7a5c00')),
                ('ALIGN',      (0, fsc_row), (-1, fsc_row), 'RIGHT'),
            ]

        t = Table(data, colWidths=col_widths, rowHeights=row_heights, repeatRows=1)
        t.setStyle(TableStyle(base_cmds + style_cmds))
        group_elements.append(t)
        return group_elements

    elements = []

    if filter_code:
        elements.extend(build_group_elements(filter_code, rows, is_first_group=True))
    else:
        first = True
        for group_code, group_iter in _groupby(rows, key=lambda r: r["code"]):
            group_rows = list(group_iter)
            elements.extend(build_group_elements(group_code, group_rows, is_first_group=first))
            first = False

    db.close()

    doc.build(elements)
    buf.seek(0)

    base = f"Invoice_{filter_code or 'ALL'}"
    if from_date and to_date:
        base += f"_{from_date}_to_{to_date}"
    elif month_str:
        base += f"_{month_str.replace(' ', '_')}"

    path = save_to_downloads(buf.read(), base_name=base, ext=".pdf")
    return jsonify({"status": "ok", "path": path})


# ============================================================
# SALES PDF
# ============================================================
@app.route("/sales-pdf", methods=["GET", "POST"])
@login_required
def sales_pdf():
    if request.method == "GET":
        return redirect(url_for("sales_checking"))

    def s(v):
        return '' if v is None else str(v)

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    client_code = request.form.get("client_code", "").strip()
    awb_no      = request.form.get("awb_no",      "").strip()
    destination = request.form.get("destination", "").strip()
    single_date = request.form.get("date",        "").strip()

    query = "SELECT code, code_fullform, awb_no, destination, weight, total_amount FROM bookings WHERE 1=1"
    params = []
    if client_code:
        query += " AND (code LIKE %s OR code_fullform LIKE %s)"
        params += [f"%{client_code}%", f"%{client_code}%"]
    if awb_no:
        query += " AND awb_no LIKE %s"
        params.append(f"%{awb_no}%")
    if destination:
        query += " AND destination LIKE %s"
        params.append(f"%{destination}%")
    if single_date:
        query += " AND booking_date = %s"
        params.append(single_date)
    query += " ORDER BY id DESC"
    cursor.execute(query, params)
    rows = cursor.fetchall()
    db.close()

    if not rows:
        return jsonify({"status": "error", "message": "No records found"})

    buf = io.BytesIO()
    doc = WatermarkDocTemplate(buf, pagesize=A4,
                               leftMargin=10*mm, rightMargin=10*mm,
                               topMargin=12*mm, bottomMargin=12*mm)

    title_style   = ParagraphStyle('title',   fontSize=13, alignment=TA_CENTER,
                                   spaceAfter=4, fontName='Helvetica-Bold')
    between_style = ParagraphStyle('between', fontSize=10, alignment=TA_CENTER,
                                   spaceAfter=6, fontName='Helvetica-Bold',
                                   textColor=colors.HexColor('#212529'))
    cell_style    = ParagraphStyle('cell', fontSize=8, leading=10)

    elements = []
    elements.append(Paragraph("Z XPRESS", title_style))

    if single_date:
        try:
            from datetime import datetime
            display_date = datetime.strptime(single_date, "%Y-%m-%d").strftime("%d/%m/%Y")
        except:
            display_date = single_date
        between_text = f"SALES CHECKING REPORT &nbsp; — &nbsp; {display_date}"
    else:
        between_text = "SALES CHECKING REPORT"
    elements.append(Paragraph(between_text, between_style))
    elements.append(Spacer(1, 4*mm))

    header     = ['SNO', 'CLIENT\nCODE', 'COMPANY NAME', 'AWB NO', 'DESTINATION', 'WEIGHT', 'AMOUNT']
    col_widths = [12*mm, 20*mm, 52*mm, 38*mm, 38*mm, 18*mm, 22*mm]
    ROW_H      = 6*mm
    HEADER_H   = 8*mm

    data        = [header]
    row_heights = [HEADER_H]
    style_cmds  = []
    row_idx     = 1
    sno         = 0
    sum_total   = 0.0

    for r in rows:
        sno      += 1
        amount    = float(r["total_amount"] or 0)
        sum_total += amount
        bg = colors.white if (row_idx % 2 == 1) else colors.HexColor('#f8f9fa')
        style_cmds.append(('BACKGROUND', (0, row_idx), (-1, row_idx), bg))

        company_name_display = s(r["code_fullform"])
        if len(company_name_display) > 25:
            company_name_display = company_name_display[:25] + '...'

        data.append([str(sno), s(r["code"]),
                     Paragraph(company_name_display, cell_style),
                     s(r["awb_no"]), s(r["destination"]),
                     f"{float(r['weight'] or 0):.3f}", f"{amount:.2f}"])
        row_heights.append(ROW_H)
        row_idx += 1

    data.append(['', '', '', '', '', 'Total:', f"{sum_total:.2f}"])
    row_heights.append(ROW_H)
    total_rows = len(data)

    base_cmds = [
        ('BACKGROUND',    (0,0),  (-1,0),  colors.HexColor('#212529')),
        ('TEXTCOLOR',     (0,0),  (-1,0),  colors.white),
        ('FONTNAME',      (0,0),  (-1,0),  'Helvetica-Bold'),
        ('FONTSIZE',      (0,0),  (-1,0),  8),
        ('ALIGN',         (0,0),  (-1,0),  'CENTER'),
        ('VALIGN',        (0,0),  (-1,0),  'MIDDLE'),
        ('TOPPADDING',    (0,0),  (-1,0),  4),
        ('BOTTOMPADDING', (0,0),  (-1,0),  4),
        ('FONTSIZE',      (0,1),  (-1,-1), 8),
        ('FONTNAME',      (0,1),  (-1,-1), 'Helvetica'),
        ('VALIGN',        (0,1),  (-1,-1), 'MIDDLE'),
        ('TOPPADDING',    (0,1),  (-1,-1), 2),
        ('BOTTOMPADDING', (0,1),  (-1,-1), 2),
        ('ALIGN', (0,1),  (0,-1),  'CENTER'),
        ('ALIGN', (1,1),  (1,-1),  'CENTER'),
        ('ALIGN', (2,1),  (2,-1),  'LEFT'),
        ('ALIGN', (3,1),  (3,-1),  'CENTER'),
        ('ALIGN', (4,1),  (4,-1),  'LEFT'),
        ('ALIGN', (5,1),  (6,-1),  'RIGHT'),
        ('GRID',  (0,0),  (-1, total_rows-2), 0.4, colors.grey),
        ('BOX',   (0,0),  (-1,-1), 0.8, colors.black),
        ('LINEABOVE',  (0, total_rows-1), (-1, total_rows-1), 1.2, colors.black),
        ('BACKGROUND', (0, total_rows-1), (-1, total_rows-1), colors.HexColor('#e9ecef')),
        ('FONTNAME',   (0, total_rows-1), (-1, total_rows-1), 'Helvetica-Bold'),
        ('ALIGN',      (5, total_rows-1), (6,  total_rows-1), 'RIGHT'),
    ]

    t = Table(data, colWidths=col_widths, rowHeights=row_heights, repeatRows=1)
    t.setStyle(TableStyle(base_cmds + style_cmds))
    elements.append(t)
    doc.build(elements)
    buf.seek(0)

    base = "Sales_Checking"
    if single_date:
        try:
            from datetime import datetime as _dt
            base += f"_{_dt.strptime(single_date, '%Y-%m-%d').strftime('%d-%m-%Y')}"
        except Exception:
            base += f"_{single_date}"
    if client_code:
        base += f"_{client_code}"

    path = save_to_downloads(buf.read(), base_name=base, ext=".pdf")
    return jsonify({"status": "ok", "path": path})


# ============================================================
# AJAX — AUTOCOMPLETE & LOOKUP ENDPOINTS
# ============================================================
@app.route("/day-book/filter")
@login_required
def day_book_filter():
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)
 
    from_date   = request.args.get("from_date", "").strip()
    to_date     = request.args.get("to_date", "").strip()
    entry_date  = request.args.get("entry_date", "").strip()
    code        = request.args.get("code", "").strip()
    weight      = request.args.get("weight", "")
    awb_no      = request.args.get("awb_no", "")
    destination = request.args.get("destination", "")
    courier     = request.args.get("courier", "").strip()
 
    query  = """SELECT id, code, code_fullform, booking_date, weight, awb_no, destination,
                       total_amount, courier, zone, auto_amount
                FROM bookings WHERE 1=1"""
    params = []
 
    if entry_date:
        query += " AND booking_date = %s";              params.append(entry_date)
    elif from_date and to_date:
        query += " AND booking_date BETWEEN %s AND %s"; params.extend([from_date, to_date])
    elif from_date:
        query += " AND booking_date >= %s";             params.append(from_date)
    elif to_date:
        query += " AND booking_date <= %s";             params.append(to_date)
 
    if code:
        query += " AND (code LIKE %s OR code_fullform LIKE %s)"
        params.append(f"%{code}%")
        params.append(f"%{code}%")
    if weight:      query += " AND weight = %s";         params.append(float(weight))
    if awb_no:      query += " AND awb_no LIKE %s";      params.append(f"%{awb_no}%")
    if destination: query += " AND destination LIKE %s"; params.append(f"%{destination}%")
    if courier:     query += " AND courier LIKE %s";     params.append(f"%{courier}%")
    query += " ORDER BY booking_date ASC, awb_no ASC"
 
    cursor.execute(query, params)
    rows      = cursor.fetchall()
    total_sum = sum(row["total_amount"] for row in rows) if rows else 0
    db.close()
 
    for r in rows:
        if hasattr(r["booking_date"], "isoformat"):
            r["booking_date"] = r["booking_date"].isoformat()
        else:
            r["booking_date"] = str(r["booking_date"]) if r["booking_date"] else ""
 
    return jsonify({"rows": rows, "total_sum": float(total_sum)})


@app.route("/day-book/delete/<int:booking_id>", methods=["POST"])
@admin_required
def day_book_delete(booking_id):
    db = get_db_connection()
    cursor = db.cursor(dictionary=True)

    cursor.execute("DELETE FROM bookings WHERE id=%s", (booking_id,))
    db.commit()
    db.close()

    return jsonify({"success": True})


@app.route("/day-book/update/<int:booking_id>", methods=["POST"])
@admin_required
def day_book_update(booking_id):
    data = request.get_json() or {}

    def txt(v):
        return str(v).strip() if v is not None else ""

    def num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    code          = txt(data.get("code")).upper()
    code_fullform = txt(data.get("code_fullform"))
    booking_date  = txt(data.get("booking_date")) or None
    awb_no        = txt(data.get("awb_no")).upper()
    destination   = txt(data.get("destination")).upper()
    weight        = num(data.get("weight"))
    courier       = txt(data.get("courier"))
    zone          = txt(data.get("zone")) or "5"
    auto_amount   = num(data.get("auto_amount"))

    if not code or not awb_no or not booking_date:
        return jsonify({"success": False, "message": "Code, AWB No and Date are required"}), 400

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    fuel         = 0.0
    total_amount = round(auto_amount, 2)

    cursor.execute("""
        UPDATE bookings SET
            code=%s, code_fullform=%s, booking_date=%s, awb_no=%s, destination=%s,
            weight=%s, courier=%s, zone=%s, auto_amount=%s, fuel=%s, total_amount=%s
        WHERE id=%s
    """, (code, code_fullform, booking_date, awb_no, destination, weight,
          courier, zone, auto_amount, fuel, total_amount, booking_id))
    db.commit()
    db.close()

    return jsonify({"success": True, "total_amount": total_amount})


@app.route("/api/ac/code")
def api_ac_code():
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute(
        "SELECT DISTINCT code, code_fullform FROM rates WHERE code LIKE %s OR code_fullform LIKE %s ORDER BY code, code_fullform LIMIT 10",
        (f"{q}%", f"%{q}%")
    )
    rows = cur.fetchall()
    db.close()
    return jsonify(rows)


@app.route("/api/ac/place")
def api_ac_place():
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT DISTINCT place FROM rates WHERE place LIKE %s ORDER BY place LIMIT 12", (f"{q}%",))
    rows = cur.fetchall()
    db.close()
    return jsonify([r["place"] for r in rows])


@app.route("/api/ac/zone")
def api_ac_zone():
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT DISTINCT place FROM rates WHERE place LIKE %s ORDER BY place LIMIT 12", (f"{q}%",))
    rows = cur.fetchall()
    db.close()
    return jsonify([r["place"] for r in rows])


@app.route("/api/ac/destination")
def api_ac_destination():
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    db  = get_db_connection()
    cur = db.cursor(dictionary=True)
    like = f"{q}%"
    cur.execute("""
        SELECT place AS destination FROM place_zones WHERE place LIKE %s
        UNION
        SELECT DISTINCT destination FROM bookings WHERE destination LIKE %s
        ORDER BY destination
        LIMIT 15
    """, (like, like))
    rows = cur.fetchall()
    db.close()
    return jsonify([r["destination"] for r in rows if r["destination"]])


@app.route("/api/ac/client")
def api_ac_client():
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT DISTINCT client_name FROM bookings WHERE client_name LIKE %s LIMIT 10", (f"%{q}%",))
    rows = cur.fetchall()
    db.close()
    return jsonify([r["client_name"] for r in rows if r["client_name"]])


@app.route("/api/rate/lookup")
def api_rate_lookup():
    code     = request.args.get("code", "").strip().upper()
    zone_str = request.args.get("zone", "").strip()
    if not code or not zone_str: return jsonify({})
    try:    zone = int(zone_str)
    except: return jsonify({})
    db = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT * FROM rates WHERE UPPER(code)=%s AND zone=%s LIMIT 1", (code, zone))
    row = cur.fetchone()
    db.close()
    return jsonify(row if row else {})


@app.route("/api/ac/awb_check")
def api_awb_check():
    awb     = request.args.get("awb", "").strip()
    edit_id = request.args.get("edit_id", "").strip()
    if not awb: return jsonify({"exists": False})
    db  = get_db_connection()
    cur = db.cursor(dictionary=True)
    if edit_id:
        cur.execute(
            "SELECT id, code, booking_date, destination FROM bookings WHERE awb_no=%s AND id!=%s LIMIT 1",
            (awb, edit_id)
        )
    else:
        cur.execute(
            "SELECT id, code, booking_date, destination FROM bookings WHERE awb_no=%s LIMIT 1",
            (awb,)
        )
    row = cur.fetchone()
    db.close()
    if row:
        return jsonify({"exists": True, "code": row["code"],
                        "date": str(row["booking_date"]), "dest": row["destination"]})
    return jsonify({"exists": False})


@app.route("/api/place/save-zone", methods=["POST"])
@login_required
def api_place_save_zone():
    data = request.get_json() or {}
    place = (data.get("place") or "").strip().upper()
    try:
        zone = int(data.get("zone") or 5)
    except (TypeError, ValueError):
        zone = 5
    place_code = (data.get("place_code") or "").strip().upper() or None

    if not place:
        return jsonify({"ok": False})

    db  = get_db_connection()
    cur = db.cursor()
    upsert_place_zone(cur, place, zone, place_code)
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/place/get-zone")
def api_place_get_zone():
    place = request.args.get("place", "").strip().upper()
    if not place: return jsonify({})
    db  = get_db_connection()
    cur = db.cursor(dictionary=True)
    cur.execute("SELECT zone FROM place_zones WHERE UPPER(place)=%s LIMIT 1", (place,))
    row = cur.fetchone()
    db.close()
    return jsonify(row if row else {})


@app.route("/api/ac/district")
def api_ac_district():
    q = request.args.get("q", "").strip()
    if not q: return jsonify([])
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    cursor.execute(
        "SELECT DISTINCT district FROM zones WHERE district LIKE %s ORDER BY district LIMIT 10",
        (f"{q}%",)
    )
    rows = cursor.fetchall()
    db.close()
    return jsonify([r["district"] for r in rows])


@app.route("/api/dest/zone-lookup")
def api_dest_zone_lookup():
    dest = request.args.get("dest", "").strip().upper()
    code = request.args.get("code", "").strip().upper()
    if not dest: return jsonify({})
    db  = get_db_connection()
    cur = db.cursor(dictionary=True)

    cur.execute("SELECT place, zone FROM place_zones WHERE UPPER(place_code)=%s LIMIT 1", (dest,))
    row = cur.fetchone()
    if row: db.close(); return jsonify({"place": row["place"], "zone": row["zone"]})

    cur.execute("SELECT zone FROM place_zones WHERE UPPER(place)=%s LIMIT 1", (dest,))
    pz = cur.fetchone()
    if pz: db.close(); return jsonify({"place": dest, "zone": pz["zone"]})

    if code:
        cur.execute("SELECT place, zone FROM rates WHERE UPPER(code)=%s AND UPPER(place)=%s LIMIT 1", (code, dest))
        row = cur.fetchone()
        if row: db.close(); return jsonify(row)

        cur.execute("SELECT place, zone FROM rates WHERE UPPER(code)=%s ORDER BY zone ASC", (code,))
        all_places = cur.fetchall()
        for p in all_places:
            pu = p["place"].upper()
            if dest in pu or pu in dest:
                db.close()
                return jsonify({"place": p["place"], "zone": p["zone"]})

    db.close()
    return jsonify({})


# ============================================================
# SMART ZONE API
# ============================================================
@app.route("/api/smart-zone")
def api_smart_zone():
    dest = request.args.get("dest", "").strip().upper()
    code = request.args.get("code", "").strip().upper()
    if not dest: return jsonify({})
    db  = get_db_connection()
    cur = db.cursor(dictionary=True)

    cur.execute("SELECT place, zone FROM place_zones WHERE UPPER(place_code)=%s LIMIT 1", (dest,))
    row = cur.fetchone()
    if row:
        db.close()
        return jsonify({**row, "source": "saved", "label": _zone_label(row["zone"])})

    cur.execute("SELECT zone FROM place_zones WHERE UPPER(place)=%s LIMIT 1", (dest,))
    row = cur.fetchone()
    if row:
        db.close()
        return jsonify({"place": dest, "zone": row["zone"], "source": "saved", "label": _zone_label(row["zone"])})

    if code:
        cur.execute("SELECT place, zone FROM rates WHERE UPPER(code)=%s AND UPPER(place)=%s LIMIT 1", (code, dest))
        row = cur.fetchone()
        if row:
            db.close()
            return jsonify({**row, "source": "rate_exact", "label": _zone_label(row["zone"])})

        cur.execute("SELECT place, zone FROM rates WHERE UPPER(code)=%s ORDER BY zone", (code,))
        for p in cur.fetchall():
            if dest in p["place"].upper() or p["place"].upper() in dest:
                db.close()
                return jsonify({"place": p["place"], "zone": p["zone"], "source": "rate_partial",
                                "label": _zone_label(p["zone"])})

    db.close()
    return jsonify({})


def _zone_label(z):
    return {1: "Chennai", 2: "Tamil Nadu", 3: "South India", 4: "North Metro", 5: "ROI"}.get(int(z), "ROI")

# ============================================================
# BACKUP / DELETE / RESTORE
# ============================================================
BOOKING_BACKUP_COLS = [
    "id", "code", "code_fullform", "booking_date", "awb_no", "destination",
    "weight", "courier", "zone", "auto_amount", "fuel", "total_amount",
    "client_name", "inv_no", "inv_date"
]

RATE_BACKUP_COLS = [
    "id", "code", "code_fullform", "place", "zone",
    "rate_250g", "rate_500g", "rate_500g_1",
    "rate_1_to_3kg", "rate_3_to_10kg", "rate_above_10kg", "fuel"
]

BOOKING_BACKUP_FILE = "Booking_Entry_Backup.xlsx"
RATE_BACKUP_FILE    = "Rate_Entry_Backup.xlsx"


def _backups_dir():
    try:
        if getattr(sys, "frozen", False):
            base = os.path.dirname(os.path.abspath(sys.executable))
        else:
            base = os.path.dirname(os.path.abspath(__file__))
    except Exception:
        base = "."
    folder = Path(base) / "backups"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def _safe_backup_subdir(name):
    root  = _backups_dir().resolve()
    safe  = os.path.basename(str(name))
    if not safe:
        return None
    target = (root / safe).resolve()
    if root not in target.parents and target != root:
        return None
    return target


def _fmt_12h(dt):
    """14-10-2026, 5:46:29 PM  (12-hour clock, no leading zero on the hour)."""
    return f"{dt:%d-%m-%Y}, {int(dt.strftime('%I'))}:{dt:%M:%S} {dt:%p}"


@app.route("/backup", methods=["GET"])
@login_required
def backup_page():
    from datetime import datetime

    root    = _backups_dir()
    today   = date.today().strftime("%Y-%m-%d")
    entries = []

    for d in sorted(root.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
        if not d.is_dir():
            continue
        files = []
        for fname in (BOOKING_BACKUP_FILE, RATE_BACKUP_FILE):
            fpath = d / fname
            if fpath.exists():
                files.append({
                    "name":    fname,
                    "size_kb": round(fpath.stat().st_size / 1024, 1),
                })
        stat = d.stat()
        entries.append({
            "name":    d.name,
            "created": _fmt_12h(datetime.fromtimestamp(stat.st_mtime)),
            "files":   files,
        })

    return render_template(
        "backup.html", folders=entries, role=session.get("role"),
        today=today
    )


@app.route("/backup/create", methods=["POST"])
@login_required
def backup_create():
    today = date.today().strftime("%Y-%m-%d")

    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)

    cursor.execute(
        f"SELECT {', '.join(BOOKING_BACKUP_COLS)} FROM bookings "
        f"ORDER BY booking_date ASC, id ASC"
    )
    booking_rows = cursor.fetchall()

    cursor.execute(f"SELECT {', '.join(RATE_BACKUP_COLS)} FROM rates ORDER BY id ASC")
    rate_rows = cursor.fetchall()
    db.close()

    booking_df = pd.DataFrame(booking_rows, columns=BOOKING_BACKUP_COLS)
    for col in ("booking_date", "inv_date"):
        booking_df[col] = booking_df[col].apply(
            lambda v: v.isoformat() if hasattr(v, "isoformat") else (v or "")
        )

    rate_df = pd.DataFrame(rate_rows, columns=RATE_BACKUP_COLS)

    root = _backups_dir()
    base_folder_name = f"AIKONIX Backup {today}"
    folder = root / base_folder_name
    n = 1
    while folder.exists():
        folder = root / f"{base_folder_name} ({n})"
        n += 1
    folder.mkdir(parents=True)

    with pd.ExcelWriter(folder / BOOKING_BACKUP_FILE, engine="openpyxl") as writer:
        booking_df.to_excel(writer, index=False, sheet_name="Bookings")
        ws = writer.sheets["Bookings"]
        for cell in ws["E"][1:]:
            cell.number_format = "@"

    with pd.ExcelWriter(folder / RATE_BACKUP_FILE, engine="openpyxl") as writer:
        rate_df.to_excel(writer, index=False, sheet_name="Rates")

    flash(f"Backup created successfully: '{folder.name}' "
          f"({len(booking_df)} booking rows, {len(rate_df)} rate rows).")
    return redirect(url_for("backup_page"))


@app.route("/backup/delete", methods=["POST"])
@admin_required
def backup_delete():
    target = _safe_backup_subdir(request.form.get("folder", ""))
    if not target or not target.is_dir():
        flash("Clear failed: backup folder not found.")
        return redirect(url_for("backup_page"))

    db     = get_db_connection()
    cursor = db.cursor()

    cursor.execute("DELETE FROM bookings")
    cursor.execute("ALTER TABLE bookings AUTO_INCREMENT = 1")

    cursor.execute("DELETE FROM rates")
    cursor.execute("ALTER TABLE rates AUTO_INCREMENT = 1")

    db.commit()
    db.close()

    flash(f"All live data (Booking Entry and Rate Entry) has been cleared "
          f"from the database. Backup '{target.name}' remains on disk.")
    return redirect(url_for("backup_page"))


@app.route("/backup/restore", methods=["POST"])
@admin_required
def backup_restore():
    target = _safe_backup_subdir(request.form.get("folder", ""))
    if not target or not target.is_dir():
        flash("Restore failed: backup folder not found.")
        return redirect(url_for("backup_page"))

    booking_path = target / BOOKING_BACKUP_FILE
    rate_path    = target / RATE_BACKUP_FILE

    if not booking_path.exists() and not rate_path.exists():
        flash(f"Restore failed: no backup files found inside '{target.name}'.")
        return redirect(url_for("backup_page"))

    def g(row, col, default=""):
        val = row.get(col, default)
        try:
            if pd.isna(val):
                return default
        except Exception:
            pass
        return val

    db     = get_db_connection()
    cursor = db.cursor()

    booking_count = 0
    rate_count    = 0

    if booking_path.exists():
        bdf = pd.read_excel(booking_path)
        bdf.columns = [str(c).strip() for c in bdf.columns]

        cursor.execute("DELETE FROM bookings")

        def _int_or_none(v):
            try:
                return int(v) if v not in (None, "") and not pd.isna(v) else None
            except (TypeError, ValueError):
                return None

        # rows without an id get the next free one (Postgres rejects id = NULL)
        used_ids = [i for i in (_int_or_none(g(r, "id", None)) for _, r in bdf.iterrows()) if i is not None]
        next_free = (max(used_ids) if used_ids else 0) + 1

        b_params = []
        for _, r in bdf.iterrows():
            booking_date = g(r, "booking_date", None)
            booking_date = str(booking_date)[:10] if booking_date else None

            inv_date = g(r, "inv_date", None)
            inv_date = str(inv_date)[:10] if inv_date else None

            row_id = _int_or_none(g(r, "id", None))
            if row_id is None:
                row_id = next_free
                next_free += 1

            b_params.append((
                row_id,
                str(g(r, "code", "")),
                str(g(r, "code_fullform", "")),
                booking_date,
                str(g(r, "awb_no", "")),
                str(g(r, "destination", "")),
                float(g(r, "weight", 0) or 0),
                str(g(r, "courier", "")),
                str(g(r, "zone", "")),
                float(g(r, "auto_amount", 0) or 0),
                float(g(r, "fuel", 0) or 0),
                float(g(r, "total_amount", 0) or 0),
                str(g(r, "client_name", "")),
                str(g(r, "inv_no", "")),
                inv_date,
            ))
            booking_count += 1

        if b_params:
            cursor.executemany("""
                INSERT INTO bookings (
                    id, code, code_fullform, booking_date, awb_no, destination,
                    weight, courier, zone, auto_amount, fuel, total_amount,
                    client_name, inv_no, inv_date
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, b_params)

        cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM bookings")
        next_id = cursor.fetchone()[0]
        cursor.execute(f"ALTER TABLE bookings AUTO_INCREMENT = {int(next_id)}")

    if rate_path.exists():
        rdf = pd.read_excel(rate_path)
        rdf.columns = [str(c).strip() for c in rdf.columns]

        cursor.execute("DELETE FROM rates")

        def _rid(r):
            raw_id = g(r, "id", None)
            try:
                return int(raw_id) if raw_id not in (None, "") and not pd.isna(raw_id) else None
            except (TypeError, ValueError):
                return None

        used_rids = [i for i in (_rid(r) for _, r in rdf.iterrows()) if i is not None]
        next_rid  = (max(used_rids) if used_rids else 0) + 1

        r_params = []
        for _, r in rdf.iterrows():
            row_id = _rid(r)
            if row_id is None:
                row_id = next_rid
                next_rid += 1

            def rnum(col):
                v = g(r, col, None)
                try:
                    return None if v in (None, "") or pd.isna(v) else float(v)
                except Exception:
                    return None

            r_params.append((
                row_id,
                str(g(r, "code", "")),
                str(g(r, "code_fullform", "")),
                str(g(r, "place", "")),
                int(g(r, "zone", 5) or 5),
                rnum("rate_250g"), rnum("rate_500g"), rnum("rate_500g_1"),
                rnum("rate_1_to_3kg"), rnum("rate_3_to_10kg"), rnum("rate_above_10kg"),
                rnum("fuel"),
            ))
            rate_count += 1

        if r_params:
            cursor.executemany("""
                INSERT INTO rates (
                    id, code, code_fullform, place, zone,
                    rate_250g, rate_500g, rate_500g_1,
                    rate_1_to_3kg, rate_3_to_10kg, rate_above_10kg, fuel
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            """, r_params)

        cursor.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM rates")
        next_id = cursor.fetchone()[0]
        cursor.execute(f"ALTER TABLE rates AUTO_INCREMENT = {int(next_id)}")

    db.commit()
    db.close()

    flash(f"Restore complete — {booking_count} booking rows and {rate_count} rate "
          f"rows have been restored from backup '{target.name}'.")
    return redirect(url_for("backup_page"))


# ============================================================
# USER MANAGEMENT  (admin only — add / delete / reset password)
# ============================================================
@app.route("/user-management", methods=["GET", "POST"])
@admin_required
def user_management():
    db     = get_db_connection()
    cursor = db.cursor(dictionary=True)
    error   = None
    success = None

    if request.method == "POST":
        action = request.form.get("_action", "add")

        if action == "add":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "").strip()
            role     = request.form.get("role", "user").strip()

            if not username or not password:
                error = "Username and password are required."
            elif role not in ("admin", "user"):
                error = "Invalid role."
            else:
                cursor.execute("SELECT username FROM app_users WHERE username=%s", (username,))
                if cursor.fetchone():
                    error = f"Username '{username}' already exists."
                else:
                    cursor.execute(
                        "INSERT INTO app_users (username, password, role) VALUES (%s,%s,%s)",
                        (username, password, role)
                    )
                    db.commit()
                    success = f"User '{username}' created."

        elif action == "delete":
            del_username = request.form.get("del_username", "").strip()

            if del_username == session.get("username"):
                error = "You can't delete the account you're currently logged in as."
            else:
                cursor.execute("SELECT role FROM app_users WHERE username=%s", (del_username,))
                target = cursor.fetchone()
                if not target:
                    error = "User not found."
                elif target["role"] == "admin":
                    cursor.execute("SELECT COUNT(*) AS cnt FROM app_users WHERE role='admin'")
                    admin_count = cursor.fetchone()["cnt"]
                    if admin_count <= 1:
                        error = "Can't delete the last remaining admin account."
                if not error:
                    cursor.execute("DELETE FROM app_users WHERE username=%s", (del_username,))
                    db.commit()
                    success = f"User '{del_username}' deleted."

        elif action == "reset_password":
            target_username = request.form.get("target_username", "").strip()
            new_password     = request.form.get("new_password", "").strip()
            confirm_password = request.form.get("confirm_password", "").strip()

            if not target_username or not new_password:
                error = "Username and new password are required."
            elif new_password != confirm_password:
                error = "New password and confirmation do not match."
            elif len(new_password) < 4:
                error = "New password must be at least 4 characters."
            else:
                cursor.execute("SELECT username FROM app_users WHERE username=%s", (target_username,))
                if not cursor.fetchone():
                    error = "User not found."
                else:
                    cursor.execute(
                        "UPDATE app_users SET password=%s WHERE username=%s",
                        (new_password, target_username)
                    )
                    db.commit()
                    success = f"Password updated for '{target_username}'."

    cursor.execute("SELECT username, password, role FROM app_users ORDER BY role DESC, username ASC")
    users = cursor.fetchall()
    db.close()

    return render_template(
        "user_management.html",
        users=users, error=error, success=success,
        current_username=session.get("username"),
        role=session.get("role")
    )

# ============================================================
# DATABASE: settings, switch, copy, backups   (admin only)
# + RECOVERY screen (works with no database / no login)
# ============================================================
import db as _dbmod
import dbtools


def _local_only():
    return request.remote_addr in ("127.0.0.1", "::1")


@app.errorhandler(_dbmod.DatabaseUnavailable)
def _database_unavailable(e):
    """Active database unreachable -> recovery screen instead of a crash."""
    try:
        with open(_crash_log_path(), "a", encoding="utf-8") as f:
            f.write(f"\n[{__import__('datetime').datetime.now()}] DB unavailable "
                    f"on {request.method} {request.path}: {e}\n")
    except Exception:
        pass
    wants_json = (request.path.startswith("/api/") or request.path.startswith("/database/")
                  or request.is_json
                  or request.headers.get("X-Requested-With") == "XMLHttpRequest")
    if wants_json:
        return jsonify({"ok": False, "error": str(e), "recovery": "/recovery"}), 503
    return redirect(url_for("recovery", reason=str(e)[:400]))


def _mysql_info():
    mysqld = dbtools.find_mysqld()
    port = int(_dbmod.CONFIG["mysql"]["port"])
    return {"port": port, "mysqld_found": bool(mysqld), "mysqld_path": mysqld or "",
            "listening": dbtools.port_listening(port)}


def _status_payload():
    return {
        "active": _dbmod.BACKEND,
        "active_label": dbtools.LABEL[_dbmod.BACKEND],
        "mysql": _mysql_info(),
        "supabase_configured": "YOUR-PASSWORD" not in (_dbmod.CONFIG["postgres"].get("url") or "YOUR-PASSWORD").upper()
                               and bool(_dbmod.CONFIG["postgres"].get("url")),
        "summaries": {"mysql": dbtools.summary("mysql"), "postgres": dbtools.summary("postgres")},
    }


@app.route("/database")
@admin_required
def database_page():
    return render_template("database.html", role=session.get("role"),
                           active=_dbmod.BACKEND, active_label=dbtools.LABEL[_dbmod.BACKEND])


@app.route("/database/status")
@admin_required
def database_status():
    d = _status_payload()
    d["backups"] = dbtools.list_backups()[:15]
    d["job"] = dbtools.job_status()
    return jsonify(d)


@app.route("/database/preview")
@admin_required
def database_preview():
    direction = request.args.get("direction", "")
    if direction not in dbtools.DIRECTIONS:
        return jsonify({"ok": False, "error": "bad direction"}), 400
    return jsonify(dbtools.preview(direction))


@app.route("/database/job")
@admin_required
def database_job():
    return jsonify(dbtools.job_status())


@app.route("/database/switch", methods=["POST"])
@admin_required
def database_switch():
    to = (request.get_json(silent=True) or {}).get("to", "")
    if to not in ("mysql", "postgres"):
        return jsonify({"ok": False, "error": "bad target"}), 400
    if to == _dbmod.BACKEND:
        return jsonify({"ok": False, "error": "Already using that database."}), 400
    try:
        dbtools.start_job("switch", dbtools.switch_backend, to)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/database/transfer", methods=["POST"])
@admin_required
def database_transfer():
    data = request.get_json(silent=True) or {}
    direction = data.get("direction", "")
    if direction not in dbtools.DIRECTIONS:
        return jsonify({"ok": False, "error": "bad direction"}), 400
    if str(data.get("confirm", "")).strip().upper() != "REPLACE":
        return jsonify({"ok": False, "error": "Type REPLACE to confirm."}), 400
    try:
        dbtools.start_job("transfer", dbtools.transfer, direction, force=bool(data.get("force")))
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/database/backups")
@login_required
def database_backups():
    """Automatic database backups (shown on the Backup page)."""
    return jsonify({"backups": dbtools.list_backups(), "is_admin": session.get("role") == "admin",
                    "active": _dbmod.BACKEND})


@app.route("/database/restore", methods=["POST"])
@admin_required
def database_restore():
    """Restore a backup into the database it came from."""
    data = request.get_json(silent=True) or {}
    if str(data.get("confirm", "")).strip().upper() != "REPLACE":
        return jsonify({"ok": False, "error": "Type REPLACE to confirm."}), 400
    name = str(data.get("name", ""))
    try:
        target = data.get("target") or dbtools.backup_origin(name)
    except Exception:
        return jsonify({"ok": False, "error": "Backup not found."}), 404
    if target not in ("mysql", "postgres"):
        return jsonify({"ok": False, "error": "bad target"}), 400
    try:
        dbtools.start_job("restore", dbtools.restore_backup, name, target, force=bool(data.get("force")))
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/database/backup-preview")
@admin_required
def database_backup_preview():
    name = request.args.get("name", "")
    target = request.args.get("target") or None
    if target not in (None, "mysql", "postgres"):
        return jsonify({"ok": False, "error": "bad target"}), 400
    try:
        return jsonify(dbtools.backup_preview(name, target))
    except Exception:
        return jsonify({"ok": False, "error": "Backup not found."}), 404


@app.route("/database/backup-now", methods=["POST"])
@admin_required
def database_backup_now():
    backend = (request.get_json(silent=True) or {}).get("backend")
    if backend not in ("mysql", "postgres"):
        return jsonify({"ok": False, "error": "bad database"}), 400
    try:
        folder = dbtools.backup_now(backend)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]}), 200
    return jsonify({"ok": True, "folder": os.path.basename(folder)})


@app.route("/database/mysql/start", methods=["POST"])
@admin_required
def database_mysql_start():
    ok, msg, port = dbtools.ensure_mysql()
    return jsonify({"ok": ok, "message": msg, "port": port})


@app.route("/database/mysql/port", methods=["POST"])
@admin_required
def database_mysql_port():
    try:
        port = int((request.get_json(silent=True) or {}).get("port"))
        if not 1024 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Port must be a number from 1024 to 65535."}), 400
    ok, msg, port = dbtools.change_mysql_port(port)
    return jsonify({"ok": ok, "message": msg, "port": port})


@app.route("/database/keepalive/status")
@admin_required
def keepalive_page_status():
    return jsonify({"task": dbtools.keepalive_task_status(), "last": dbtools.keepalive_last()})


@app.route("/database/keepalive/ping", methods=["POST"])
@admin_required
def keepalive_ping():
    return jsonify(dbtools.keepalive())


@app.route("/database/keepalive/enable", methods=["POST"])
@admin_required
def keepalive_enable():
    try:
        return jsonify({"ok": True, "task": dbtools.keepalive_task_install()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:300]})


@app.route("/database/keepalive/disable", methods=["POST"])
@admin_required
def keepalive_disable():
    return jsonify({"ok": True, "task": dbtools.keepalive_task_remove()})


# ---------------- recovery (no login: the database may be the problem) ----------------
@app.route("/recovery")
def recovery():
    if not _local_only():
        return "Recovery is only available on the computer running ZXpress.", 403
    return render_template("recovery.html", reason=request.args.get("reason", ""),
                           active=_dbmod.BACKEND, active_label=dbtools.LABEL[_dbmod.BACKEND])


@app.route("/recovery/status")
def recovery_status():
    if not _local_only():
        return jsonify({"ok": False}), 403
    return jsonify(_status_payload())


@app.route("/recovery/start-mysql", methods=["POST"])
def recovery_start_mysql():
    if not _local_only():
        return jsonify({"ok": False}), 403
    ok, msg, port = dbtools.ensure_mysql()
    return jsonify({"ok": ok, "message": msg, "port": port})


@app.route("/recovery/port", methods=["POST"])
def recovery_port():
    if not _local_only():
        return jsonify({"ok": False}), 403
    try:
        port = int((request.get_json(silent=True) or {}).get("port"))
        if not 1024 <= port <= 65535:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"ok": False, "message": "Port must be a number from 1024 to 65535."}), 400
    ok, msg, port = dbtools.change_mysql_port(port)
    return jsonify({"ok": ok, "message": msg, "port": port})


@app.route("/recovery/switch", methods=["POST"])
def recovery_switch():
    """Switch database without being logged in.  The admin proves who they are
    against the database they are switching TO (it must be reachable anyway)."""
    if not _local_only():
        return jsonify({"ok": False}), 403
    d = request.get_json(silent=True) or {}
    to = d.get("to")
    if to not in ("mysql", "postgres"):
        return jsonify({"ok": False, "error": "bad target"}), 400
    try:
        if to == "mysql":
            ok, msg, _ = dbtools.ensure_mysql()
            if not ok:
                return jsonify({"ok": False, "error": msg}), 200
        conn = dbtools.raw_connect(to)
        try:
            cur = dbtools._cursor(conn, to)
            cur.execute("SELECT password, role FROM app_users WHERE LOWER(username) = LOWER(%s) LIMIT 1",
                        (str(d.get("username", "")).strip(),))
            row = cur.fetchone()
        finally:
            conn.close()
        row = dict(row) if row else None
        if not row or row["password"] != str(d.get("password", "")) or row["role"] != "admin":
            return jsonify({"ok": False, "error": "Admin username/password not accepted by "
                                                  f"{dbtools.LABEL[to]}."}), 200
        res = dbtools.switch_backend(to)
        return jsonify({"ok": True, "result": res})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 200


# ---------------- XAMPP data repair (admin page + recovery page) ----------------
def _xampp_diagnose_json():
    import xamppfix
    return jsonify(xamppfix.diagnose())


def xampp_repair_start():
    import xamppfix
    d = request.get_json(silent=True) or {}
    if str(d.get("confirm", "")).strip().upper() != "REPAIR":
        return jsonify({"ok": False, "error": "Type REPAIR to confirm."}), 400
    try:
        dbtools.start_job("repair", xamppfix.repair_xampp, force_deep=bool(d.get("deep")))
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True})


def xampp_rescue_start():
    import xamppfix
    try:
        dbtools.start_job("rescue", xamppfix.create_rescue_file)
    except RuntimeError as e:
        return jsonify({"ok": False, "error": str(e)}), 409
    return jsonify({"ok": True})


@app.route("/database/xampp/diagnose")
@admin_required
def database_xampp_diagnose():
    return _xampp_diagnose_json()


@app.route("/database/xampp/repair", methods=["POST"])
@admin_required
def database_xampp_repair():
    return xampp_repair_start()


@app.route("/database/xampp/rescue-file", methods=["POST"])
@admin_required
def database_xampp_rescue():
    return xampp_rescue_start()


@app.route("/recovery/xampp/diagnose")
def recovery_xampp_diagnose():
    if not _local_only():
        return jsonify({"ok": False}), 403
    return _xampp_diagnose_json()


@app.route("/recovery/xampp/repair", methods=["POST"])
def recovery_xampp_repair():
    if not _local_only():
        return jsonify({"ok": False}), 403
    return xampp_repair_start()


@app.route("/recovery/xampp/rescue-file", methods=["POST"])
def recovery_xampp_rescue():
    if not _local_only():
        return jsonify({"ok": False}), 403
    return xampp_rescue_start()


@app.route("/recovery/job")
def recovery_job():
    if not _local_only():
        return jsonify({"ok": False}), 403
    return jsonify(dbtools.job_status())


@app.route("/recovery/open-backups", methods=["POST"])
def recovery_open_backups():
    """Open the folder that holds the safety zip / rescue file in Explorer (this PC only)."""
    if not _local_only():
        return jsonify({"ok": False}), 403
    try:
        os.startfile(dbtools._backups_dir())
        return jsonify({"ok": True, "path": dbtools._backups_dir()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)[:200]})


# ============================================================
# RUN
# ============================================================
if __name__ == "__main__":
    app.run(debug=True, host='0.0.0.0')