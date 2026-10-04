"""
One place that decides where the app keeps its files.

Two ways this app can run:

1. From source (start.bat / python app.py)
      code, data and assets all live in the same folder.

2. As a packaged .exe (PyInstaller --onefile)
      • the code and the web assets (templates/, static/) are unpacked into a
        TEMPORARY folder (_MEIPASS) that disappears when the app closes
      • therefore the database, settings and reports MUST be written next to
        the .exe instead, or your watchlist/alerts/cache would vanish on
        every restart.

`paths.py` gives the rest of the code the right folder for each purpose, so
nothing else has to care whether it is running frozen or not.
"""
import os
import sys

APP_NAME = "DrShah's US Stocks Analysis"


def is_frozen() -> bool:
    """True when running from a PyInstaller bundle (.exe)."""
    return bool(getattr(sys, "frozen", False))


def _script_dir() -> str:
    """Folder of the running script (source mode)."""
    return os.path.dirname(os.path.abspath(__file__))


def _exe_dir() -> str:
    """Folder that contains the running .exe (frozen mode).

    sys.executable is the .exe path when frozen; sys.argv[0] is used as a
    fallback for unusual launchers. `sys._MEIPASS` is deliberately NOT used
    here — that is a temp folder that is deleted on exit.
    """
    for candidate in (sys.executable, sys.argv[0] if sys.argv else ""):
        if candidate and candidate.lower().endswith(".exe"):
            return os.path.dirname(os.path.abspath(candidate))
    return os.path.dirname(os.path.abspath(sys.executable or sys.argv[0] or "."))


def _bundle_dir() -> str:
    """Read-only folder holding the bundled assets.

    Frozen  → the PyInstaller temp extraction folder (_MEIPASS).
    Source  → this folder, exactly as before.
    """
    return getattr(sys, "_MEIPASS", None) or _script_dir()


def _writable_dir() -> str:
    """Where user data goes: right next to the .exe, else the source folder.

    If that folder happens to be read-only (e.g. the user "installed" the exe
    into C:\\Program Files), fall back to a per-user data folder so the app
    still works instead of crashing on the first save.
    """
    target = _exe_dir() if is_frozen() else _script_dir()
    try:
        os.makedirs(target, exist_ok=True)
        probe = os.path.join(target, ".write_test")
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return target
    except Exception:  # noqa: BLE001 — read-only location
        fallback = os.path.join(
            os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
            or os.path.expanduser("~"), "DrShahsUSStocksAnalysis")
        os.makedirs(fallback, exist_ok=True)
        return fallback


# --- public paths ----------------------------------------------------------
BASE = _script_dir()                       # backwards compatibility
DATA_DIR = _writable_dir()                 # app.db, config.json, reports/, caches
BUNDLE_DIR = _bundle_dir()                 # templates/, static/ (read-only)
TEMPLATES_DIR = os.path.join(BUNDLE_DIR, "templates")
STATIC_DIR = os.path.join(BUNDLE_DIR, "static")

# individual data files (kept here so every module agrees on one location)
DB_PATH = os.path.join(DATA_DIR, "app.db")
CONFIG_PATH = os.path.join(DATA_DIR, "config.json")
GUEST_FILE = os.path.join(DATA_DIR, "watchlist.json")
CACHE_DB_PATH = os.path.join(DATA_DIR, "data_cache.db")
REPORTS_DIR = os.path.join(DATA_DIR, "reports")
LOGS_DIR = os.path.join(DATA_DIR, "logs")          # app.log (rotating)
BACKUPS_DIR = os.path.join(DATA_DIR, "backups")    # automatic daily backups
LOGO_PATH = os.path.join(STATIC_DIR, "logo.png")


def describe() -> dict:
    """Small diagnostic block (shown in the console at startup)."""
    return {
        "mode": "packaged .exe" if is_frozen() else "python source",
        "data_folder": DATA_DIR,
        "assets_folder": BUNDLE_DIR,
        "writable": DATA_DIR == (_exe_dir() if is_frozen() else _script_dir()),
    }


# Files that should come across from the bundle on the FIRST run of a packaged
# app (so the .exe starts with the same watchlist you built it with). Anything
# the user already has is left untouched, and credentials are never copied.
SEED_FILES = ("watchlist.json",)


def seed_first_run() -> list[str]:
    """Copy bundled starter files next to the .exe if they are missing.

    Returns the list of files that were copied. Safe to call on every start —
    it never overwrites existing user data.
    """
    copied = []
    if DATA_DIR == BUNDLE_DIR:
        return copied                      # source mode: nothing to do
    for name in SEED_FILES:
        src = os.path.join(BUNDLE_DIR, name)
        dst = os.path.join(DATA_DIR, name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                with open(src, "rb") as f:
                    blob = f.read()
                with open(dst, "wb") as f:
                    f.write(blob)
                copied.append(name)
            except Exception:  # noqa: BLE001 — never block startup
                pass
    return copied


if __name__ == "__main__":          # python paths.py → quick self-check
    import json
    print(json.dumps(describe(), indent=2))
    for name in ("templates/index.html", "static/logo.png", "static/favicon.png"):
        full = os.path.join(BUNDLE_DIR, name)
        print(f"  {'OK ' if os.path.exists(full) else 'MISSING'} {full}")
