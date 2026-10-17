# ZXpress on Supabase (temporary online database)

The app can run on **Supabase (PostgreSQL)** or on **XAMPP (MySQL)**. Which one is
chosen by `db_config.json` — it sits next to `ZXpress.exe`, so it can be changed on
the client PC with Notepad, **no rebuild**.

```json
{ "backend": "postgres", "postgres": { "url": "postgresql://..." }, "mysql": { ... } }
```

| `backend`   | Database used                          | Needs on client PC                  |
|-------------|----------------------------------------|-------------------------------------|
| `"postgres"`| Supabase (online)                      | internet only (HTTPS-style outbound)|
| `"mysql"`   | local XAMPP MySQL (the original setup) | XAMPP + MySQL on port 3306          |

`app.py` still contains the original MySQL-dialect SQL; `db.py` translates it for
Postgres at run time. That is why switching back is only a config change.

## One-time setup (done once, on your PC)

1. Supabase dashboard -> project `fqiyiarxocjhfbaopwyh` -> **Connect** -> **Session pooler**
   -> copy the URI, put your database password in it, paste into
   `db_config.json -> postgres.url`.
   (Use the *pooler* string — the "direct" one is IPv6-only and fails on many PCs.)
2. XAMPP MySQL running, then:
   ```
   python migrate_mysql_to_supabase.py
   ```
   It creates the tables from `supabase_schema.sql`, copies everything from MySQL
   (read-only), and prints a row-count + totals check. It refuses to touch a
   Supabase database that already has rows unless you pass `--replace`.
3. Check the connection: `python db.py`  ->  `Supabase (PostgreSQL) OK - N bookings`
4. Build the exe: `build_venv\Scripts\python -m PyInstaller ZXpress.spec`
   and copy `ZXpress.exe` (+ an edited `db_config.json`) to the client.

## Going back to XAMPP after the 2-3 weeks

1. Bring the newest data back to XAMPP: on the Supabase version open **Backup ->
   Create Backup** (Excel files of bookings + rates). Switch the config to
   `"mysql"`, start the exe, and use **Backup -> Restore** on that folder.
   (Backup/Restore was tested on both back-ends: restoring gives identical rows and
   the next new booking continues at the right id.)
   Other tables (users, zones, place_zones) are small - re-check them after switching.
2. Set `"backend": "mysql"` in `db_config.json` on the client PC. Done.

## Backups while on Supabase

The free plan has **no automatic backups** and pauses after ~1 week without traffic.
Use the app's Backup page regularly (it works on both back-ends), and keep the
original dump: `D:\ST courier\backup_mysql\booking_system_FULL_*.sql`.

## Safety notes

* Every table has Row Level Security **on** with no policies, so Supabase's public
  REST API (anon key) can read/write nothing. The app uses its own direct database
  connection, which is not affected by RLS.
* `db_config.json` contains the database password. Treat it like a password: do not
  email it or commit it anywhere public. Anyone with it has full access to the data.
* Rotate the database password in Supabase (Settings -> Database) when the 2-3 weeks
  are over if the file was copied around.

## Database page (admin) and recovery screen

**Sidebar -> Database** (admin only):

* shows both databases side by side (reachable?, bookings, rates, latest booking date, amount total)
* **Switch to XAMPP / Supabase** - backs up both databases first, refuses if the target is unreachable
* **Copy XAMPP -> Supabase** / **Copy Supabase -> XAMPP** - replaces the target completely.
  Both databases are backed up first, the write is one transaction (any error = nothing changed),
  counts + amount are verified before commit, and if the target holds bookings the source
  does not, the admin must tick an extra box. Type `REPLACE` to confirm.
* **Start MySQL** and **MySQL port** - starts XAMPP's `mysqld.exe` directly (no Control Panel,
  no Apache). If port 3306 is busy it starts on the next free port and remembers it.
* **Automatic backups** list with Restore buttons. Files are in `db_backups\` next to the exe.

**If the database cannot be reached** the app does not close: it opens `/recovery`
(local PC only, no login needed) with Start MySQL, change port, and Switch database
(admin username + password, checked against the database being switched to).
MySQL start is limited to ~35 s so the window always opens.

Settings changed in the app are saved to `db_config.json` next to the exe, or to
`%APPDATA%\ZXpress\db_config.json` if that folder is read-only.

## XAMPP won't start ("MySQL shutdown unexpectedly") - one-click repair

On the **Recovery page** (opens by itself when the database cannot be reached) and on
**Database -> XAMPP / MySQL**:

* **Repair XAMPP data** - stops MySQL, zips the whole `mysql\data` folder to
  `db_backups\xampp_data_before_repair_<time>.zip`, renames `data` to `data_old_<time>`
  (never deleted), rebuilds `data` from `mysql\backup`, copies the user databases and
  `ibdata1` back, starts MySQL and checks the tables open. Nothing works -> automatic rollback.
* **Deep rescue** (automatic second attempt): starts MySQL on a COPY of the damaged data in
  `innodb_force_recovery` mode, exports every database to `.sql`, rebuilds a clean folder and
  imports it. Reported as "partial": some rows may be missing - compare with Supabase/backups.
* **Create rescue file** - one zip (data folder + my.ini + error log + diagnosis) to send to support.
* **What is wrong?** - plain-English cause from the error log.

Not fixable by software: missing Visual C++ runtime / too-old Windows, full or failing disk,
data that no longer exists anywhere. The app says so and offers **Switch to Supabase**.
