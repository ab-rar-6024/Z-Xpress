# Z-Xpress (ZXpress)

Courier **booking, rate and invoice system** for a small courier business. It runs as a Windows desktop
app (`ZXpress.exe`) that opens its own window, or as a normal Flask web app in a browser.

It can store its data in **XAMPP (MySQL/MariaDB)** on the same PC **or** in **Supabase (PostgreSQL)** in the
cloud, and an administrator can switch between the two, copy data both ways, back up and restore - from
inside the app.

> **Private repository.** It contains no passwords or customer data. See [Security](#security).

---

## Features

**Business pages**
- Dashboard (today / month / total, trends, top clients, zone mix)
- Zone Entry, Rate Entry, Booking Data (add / edit / delete / Excel import)
- Invoice (PDF + Excel), Sales Checking, Day Wise, Day Book (PDF + Excel exports)
- User management with admin / user roles

**Database control (admin) - `Database` and `Backup` pages**
- Live view of both databases side by side (counts, latest booking, in sync or not)
- **Switch** between XAMPP and Supabase
- **Copy** all data either way (replaces the target; both sides are backed up first, all-or-nothing)
- **Automatic backups** before every switch / copy / restore, with one-click **Restore**
- Start MySQL, change the MySQL port safely, Supabase **keep-alive** (stops the free plan pausing)

**When XAMPP breaks**
- The app never becomes a dead window: it opens a **Recovery page** (no login needed, local PC only)
- **Repair XAMPP data** - fixes "MySQL shutdown unexpectedly": safety zip, old folder kept, automatic rollback,
  deep rescue fallback (`innodb_force_recovery` dump and re-import)
- **Create rescue file** - one zip (data folder + `my.ini` + error log + diagnosis) to send to support

---

## Requirements

- Windows 10 / 11 (also works on older PCs; XAMPP itself is optional)
- Python **3.11** (only to run from source or to build the exe)
- Packages: see `requirements.txt` (`flask`, `pymysql`, `psycopg2-binary`, `pandas`, `openpyxl`,
  `reportlab`, `pywebview`, `mysql-connector-python`)
- Optional: **XAMPP** (MariaDB 10.4+) for the local database. Apache is **not** needed.

---

## Quick start (from source)

```powershell
cd project
python -m venv venv
.\venv\Scripts\pip install -r requirements.txt

copy db_config.example.json db_config.json     # then edit it (see Configuration)
.\venv\Scripts\python app.py                   # http://127.0.0.1:5000
```

The packaged exe uses port **8877** and opens its own window instead of a browser tab.

### Database setup
| Using | Do this once |
|---|---|
| **XAMPP** | Start MySQL, create the database `booking_system`, import `schema.sql` (tables + starter accounts). |
| **Supabase** | Run `supabase_schema.sql` in the Supabase SQL editor, then load your data with `migrate_mysql_to_supabase.py`, or use **Backup -> Copy** inside the app. |

Change the starter accounts created by `schema.sql` before real use.

---

## Configuration

Copy `db_config.example.json` to `db_config.json` (next to `app.py` or next to `ZXpress.exe`):

```json
{
  "backend": "mysql",
  "postgres": { "url": "postgresql://postgres.<PROJECT-REF>:<PASSWORD>@aws-1-<region>.pooler.supabase.com:5432/postgres" },
  "mysql":    { "host": "localhost", "port": 3306, "user": "root", "password": "", "database": "booking_system" }
}
```

- `backend`: `"mysql"` (XAMPP) or `"postgres"` (Supabase). Admins can also switch it from the **Database** page.
- Use Supabase's **Session pooler** connection string (the direct one is IPv6-only).
- If the password contains spaces or symbols, URL-encode them (`space` -> `%20`).
- Settings changed inside the app are saved back to this file (or to `%APPDATA%\ZXpress\db_config.json`
  when the folder is read-only).
- Environment overrides for testing: `ZX_DB_BACKEND`, `ZX_DB_URL`, `ZX_MYSQL_DB`, `ZX_CONFIG_DIR`.

---

## Build the exe

```powershell
cd project
python -m venv build_venv
.\build_venv\Scripts\pip install -r requirements.txt pyinstaller
.\build_venv\Scripts\python -m PyInstaller ZXpress.spec --noconfirm --distpath dist_new
copy db_config.json dist_new\          # the exe reads db_config.json next to itself
```

Result: a single `ZXpress.exe` (about 48 MB). Close any running copy first - Windows will not overwrite
a running exe. The window and taskbar icon come from `logo.ico`.

---

## Project layout

```
app.py            Flask app - all pages, exports, admin routes, recovery routes
db.py             database layer: XAMPP (MySQL) or Supabase (PostgreSQL); SQL translated for Postgres
dbtools.py        start MySQL / change port, summaries, backup, copy, restore, switch, Supabase keep-alive
xamppfix.py       XAMPP data repair, deep rescue, rescue file, diagnosis
launcher.pyw      desktop launcher (starts MySQL if needed, then Flask, then the app window)
templates/        HTML pages (Bootstrap)      static/   CSS, JS, images
schema.sql  mysql_schema.sql  supabase_schema.sql      database definitions
migrate_mysql_to_supabase.py                           one-off data migration
ZXpress.spec  logo.ico  requirements.txt               build files
SUPABASE_README.md                                     notes on switching, backups and repair
```

`app.py` is written in MySQL dialect on purpose; `db.py` translates statements for PostgreSQL, so switching
back to XAMPP is only a configuration change.

---

## Daily use and safety

- Switching or copying never changes data until you confirm (type `REPLACE`); both databases are backed up first.
- Backups are written to `db_backups\` next to the exe and listed on the **Backup** page.
- The free Supabase plan pauses after about a week of no use - turn on **Keep Supabase awake**
  (Database page), or upgrade the plan.
- Never switch the PC off while MySQL is running; if XAMPP will not start, use **Repair XAMPP data**.

---

## Security

- `db_config.json`, backups, exes, logs and saved logins are **git-ignored** and must never be committed.
- The exe has `db_config.json` (including the Supabase password) built in. Keep the repository and the
  exe **private**.
- Change the starter accounts from `schema.sql` and rotate the Supabase database password if it is ever shared.
- Before each commit run `git status` and check that `db_config.json` is not listed.

---

&copy; AIKONIK. All rights reserved.
