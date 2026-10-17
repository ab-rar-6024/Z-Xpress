# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['launcher.pyw'],
    pathex=[],
    binaries=[],
    datas=[('templates', 'templates'), ('static', 'static'), ('schema.sql', '.'), ('logo.ico', '.'), ('db_config.json', '.'), ('supabase_schema.sql', '.'), ('mysql_schema.sql', '.')],
    hiddenimports=['pymysql', 'pymysql.cursors', 'psycopg2', 'psycopg2.extras', 'dbtools', 'xamppfix', 'webview'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='ZXpress',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['logo.ico'],
)
