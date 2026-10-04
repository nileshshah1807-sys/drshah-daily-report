"""
Automatic daily backup of the user's own data.

Once a day the scheduler writes backups/backup_YYYY-MM-DD.zip next to the
program with:
  • app.db          — accounts, watchlists, alerts, notifications
  • config.json     — settings (includes the email / Telegram credentials)
  • watchlist.json  — the guest watchlist
The last 7 backups are kept. The market-data cache is not included: it is
downloaded again automatically.
"""
import glob
import logging
import os
import sqlite3
import tempfile
import zipfile
from datetime import datetime

import paths
import store

BACKUPS_DIR = paths.BACKUPS_DIR
KEEP = 7
log = logging.getLogger("backup")

_RESTORE_TXT = """Dr. Shah's US Stocks Analysis - automatic backup
=================================================
To restore this backup:
  1. Quit the app (tray icon -> Quit, or close its window).
  2. Copy app.db, config.json and watchlist.json from this zip into the
     app's folder (the folder that holds DrShahsUSStocksAnalysis.exe),
     replacing the files there.
  3. Start the app again.
config.json contains your email / Telegram passwords - keep this file private.
"""


def _zip_path(day: str) -> str:
    return os.path.join(BACKUPS_DIR, f"backup_{day}.zip")


def list_backups() -> list[dict]:
    """Newest first."""
    out = []
    for p in sorted(glob.glob(os.path.join(BACKUPS_DIR, "backup_*.zip")), reverse=True):
        try:
            out.append({"file": os.path.basename(p), "size": os.path.getsize(p),
                        "date": os.path.basename(p)[7:17]})
        except OSError:
            continue
    return out


def _copy_database(src: str, dst: str) -> None:
    """SQLite's online backup: consistent even while the app is writing."""
    source = sqlite3.connect(src, timeout=20)
    target = sqlite3.connect(dst)
    try:
        with target:
            source.backup(target)
    finally:
        target.close()
        source.close()


def make_backup(day: str | None = None) -> str:
    """Write today's backup zip (replacing one from earlier today) and prune."""
    day = day or datetime.now().strftime("%Y-%m-%d")
    os.makedirs(BACKUPS_DIR, exist_ok=True)
    target = _zip_path(day)
    tmp = target + ".tmp"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        if os.path.exists(store.DB_PATH):
            fd, snap = tempfile.mkstemp(suffix=".db")
            os.close(fd)
            try:
                _copy_database(store.DB_PATH, snap)
                z.write(snap, "app.db")
            finally:
                os.remove(snap)
        with store._lock:                     # never read config.json mid-write
            for path, name in ((store.CONFIG_PATH, "config.json"),
                               (store.GUEST_FILE, "watchlist.json")):
                if os.path.exists(path):
                    with open(path, "rb") as f:
                        z.writestr(name, f.read())
        z.writestr("HOW-TO-RESTORE.txt", _RESTORE_TXT)
    os.replace(tmp, target)
    prune()
    log.info("backup written: %s", target)
    return target


def prune(keep: int = KEEP) -> list[str]:
    """Delete all but the newest `keep` backups; returns what was removed."""
    removed = []
    for old in [b["file"] for b in list_backups()][keep:]:
        try:
            os.remove(os.path.join(BACKUPS_DIR, old))
            removed.append(old)
        except OSError:
            pass
    return removed


def ensure_today() -> str | None:
    """Called by the scheduler: make today's backup if it does not exist yet."""
    day = datetime.now().strftime("%Y-%m-%d")
    if os.path.exists(_zip_path(day)):
        return None
    return make_backup(day)
