"""
Persistence layer — SQLite (users, per-user watchlists, alerts, notifications),
app config (config.json) and the guest watchlist (watchlist.json).
"""
import json
import os
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager

from werkzeug.security import check_password_hash, generate_password_hash

import paths
BASE = paths.DATA_DIR
DB_PATH = paths.DB_PATH
CONFIG_PATH = paths.CONFIG_PATH
GUEST_FILE = paths.GUEST_FILE
MAX_WATCHLIST = 60          # keep in sync with app.py
DEFAULT_WATCHLIST = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META",
                     "TSLA", "JPM", "AMD", "NFLX", "COST", "V"]

DEFAULT_CONFIG = {
    "secret_key": None,
    "alert_check_minutes": 10,
    "email": {"enabled": False, "smtp_host": "", "smtp_port": 587,
              "smtp_user": "", "smtp_pass": "", "from_addr": "", "to_addr": ""},
    "telegram": {"enabled": False, "bot_token": "", "chat_id": ""},
    "schedule": {"enabled": False, "time": "18:00", "email_delivery": False,
                 "telegram_delivery": False,
                 "keep_days": 30, "last_run_date": None},
    "prewarm": {"enabled": True, "interval_minutes": 15, "market_hours_only": True},
    # sign this account in automatically for a browser on this computer
    "auto_login": {"enabled": False, "username": ""},
    # may phones / other PCs on the same network open the app? (needs a restart)
    "network": {"lan_access": True},
}

_lock = threading.Lock()


def _connect():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def db():
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with _lock, db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          username TEXT UNIQUE NOT NULL,
          email TEXT DEFAULT '',
          password_hash TEXT NOT NULL,
          created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS watchlists(
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          tickers TEXT NOT NULL DEFAULT '[]'
        );
        CREATE TABLE IF NOT EXISTS alerts(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          ticker TEXT NOT NULL,
          kind TEXT NOT NULL,
          value TEXT DEFAULT '',
          channels TEXT NOT NULL DEFAULT '["app"]',
          active INTEGER NOT NULL DEFAULT 1,
          triggered INTEGER NOT NULL DEFAULT 0,
          triggered_at TEXT,
          last_value TEXT,
          created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS notifications(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
          ticker TEXT DEFAULT '',
          title TEXT NOT NULL,
          body TEXT DEFAULT '',
          read INTEGER NOT NULL DEFAULT 0,
          created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE INDEX IF NOT EXISTS idx_alerts_user ON alerts(user_id);
        CREATE INDEX IF NOT EXISTS idx_notif_user ON notifications(user_id);
        """)
        # added 25 Sep 2026: repeating alerts re-arm once their condition clears
        cols = {r["name"] for r in c.execute("PRAGMA table_info(alerts)")}
        if "repeat" not in cols:
            c.execute("ALTER TABLE alerts ADD COLUMN repeat INTEGER NOT NULL DEFAULT 0")
        # added 4 Oct 2026: per-account preferences (e.g. the whole-watchlist
        # rating watch) and what that watch has already reported
        c.executescript("""
        CREATE TABLE IF NOT EXISTS user_prefs(
          user_id INTEGER PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
          prefs TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS rating_notices(
          user_id INTEGER NOT NULL,
          ticker TEXT NOT NULL,
          date TEXT NOT NULL,
          PRIMARY KEY (user_id, ticker, date)
        );
        """)


# ---------------------------------------------------------------------------
# Users / auth
# ---------------------------------------------------------------------------

def create_user(username: str, email: str, password: str) -> int:
    with _lock, db() as c:
        cur = c.execute("INSERT INTO users (username, email, password_hash) VALUES (?,?,?)",
                        (username, email or "", generate_password_hash(password)))
        uid = cur.lastrowid
        c.execute("INSERT INTO watchlists (user_id, tickers) VALUES (?,?)",
                  (uid, json.dumps(load_guest_watchlist())))
        return uid


def get_user_by_username(username: str):
    with _lock, db() as c:
        row = c.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        return dict(row) if row else None


def get_user(user_id: int):
    with _lock, db() as c:
        row = c.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
        return dict(row) if row else None


def first_user_id() -> int | None:
    """The owner: the first account created on this installation."""
    with _lock, db() as c:
        row = c.execute("SELECT MIN(id) AS id FROM users").fetchone()
        return int(row["id"]) if row and row["id"] is not None else None


def verify_user(username: str, password: str):
    user = get_user_by_username(username)
    if user and check_password_hash(user["password_hash"], password):
        return user
    return None


# ---------------------------------------------------------------------------
# Watchlists (guest = watchlist.json, logged-in = DB)
# ---------------------------------------------------------------------------

def load_guest_watchlist() -> list[str]:
    """The guest watchlist from watchlist.json.

    A MISSING or unreadable file means "fresh install" → start with the default
    list. An EMPTY file (the user removed every stock on purpose) is honoured as
    an empty watchlist — it must NOT silently refill with the defaults.
    """
    if not os.path.exists(GUEST_FILE):
        return list(DEFAULT_WATCHLIST)
    try:
        with open(GUEST_FILE) as f:
            data = json.load(f)
    except Exception:
        return list(DEFAULT_WATCHLIST)
    if not isinstance(data, list):
        return list(DEFAULT_WATCHLIST)
    tickers = [str(t).strip().upper() for t in data if str(t).strip()]
    return tickers[:MAX_WATCHLIST]      # a hand-edited file can never explode the app


def save_guest_watchlist(tickers: list[str]) -> None:
    with open(GUEST_FILE, "w") as f:
        json.dump(tickers, f, indent=2)


def get_watchlist(user_id) -> list[str]:
    if not user_id:
        return load_guest_watchlist()
    with _lock, db() as c:
        row = c.execute("SELECT tickers FROM watchlists WHERE user_id = ?", (user_id,)).fetchone()
        if not row:
            return list(DEFAULT_WATCHLIST)
        try:
            return json.loads(row["tickers"])
        except Exception:
            return list(DEFAULT_WATCHLIST)


def set_watchlist(user_id, tickers: list[str]) -> None:
    if not user_id:
        save_guest_watchlist(tickers)
        return
    with _lock, db() as c:
        c.execute(
            "INSERT INTO watchlists (user_id, tickers) VALUES (?,?) "
            "ON CONFLICT(user_id) DO UPDATE SET tickers = excluded.tickers",
            (user_id, json.dumps(tickers)))


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def get_alerts(user_id, active_only: bool = False) -> list[dict]:
    with _lock, db() as c:
        q = "SELECT * FROM alerts WHERE user_id = ?"
        if active_only:
            q += " AND active = 1"
        q += " ORDER BY created_at DESC"
        rows = c.execute(q, (user_id,)).fetchall()
        return [dict(r) for r in rows]


def add_alert(user_id, ticker: str, kind: str, value, channels: list[str],
              repeat: bool = False) -> int:
    with _lock, db() as c:
        cur = c.execute(
            "INSERT INTO alerts (user_id, ticker, kind, value, channels, repeat) "
            "VALUES (?,?,?,?,?,?)",
            (user_id, ticker.upper(), kind, str(value if value is not None else ""),
             json.dumps(channels), 1 if repeat else 0))
        return cur.lastrowid


def toggle_alert(alert_id: int, user_id: int) -> bool:
    with _lock, db() as c:
        row = c.execute("SELECT * FROM alerts WHERE id = ? AND user_id = ?",
                        (alert_id, user_id)).fetchone()
        if not row:
            return False
        # re-arming resets the triggered flag
        new_active = 0 if row["active"] else 1
        c.execute("UPDATE alerts SET active = ?, triggered = 0 WHERE id = ?",
                  (new_active, alert_id))
        return True


def delete_alert(alert_id: int, user_id: int) -> bool:
    with _lock, db() as c:
        cur = c.execute("DELETE FROM alerts WHERE id = ? AND user_id = ?", (alert_id, user_id))
        return cur.rowcount > 0


def replace_alerts(user_id: int, alerts: list[dict]) -> int:
    """Delete all alerts for a user and insert the given (sanitized) ones.
    Returns the number inserted (used by backup restore)."""
    with _lock, db() as c:
        c.execute("DELETE FROM alerts WHERE user_id = ?", (user_id,))
        n = 0
        for a in alerts:
            c.execute(
                "INSERT INTO alerts (user_id, ticker, kind, value, channels, active, repeat) "
                "VALUES (?,?,?,?,?,?,?)",
                (user_id, a["ticker"], a["kind"], str(a.get("value") or ""),
                 json.dumps(a.get("channels") or ["app"]),
                 1 if a.get("active", True) else 0, 1 if a.get("repeat") else 0))
            n += 1
        return n


def mark_alert_triggered(alert_id: int, last_value: str) -> None:
    with _lock, db() as c:
        c.execute("UPDATE alerts SET triggered = 1, triggered_at = datetime('now'), "
                  "last_value = ? WHERE id = ?", (last_value, alert_id))


def rearm_alert(alert_id: int) -> None:
    """A repeating alert whose condition has cleared can fire again."""
    with _lock, db() as c:
        c.execute("UPDATE alerts SET triggered = 0 WHERE id = ?", (alert_id,))


def all_active_alerts() -> list[dict]:
    """Alerts to check: not yet fired, plus fired repeating ones (to re-arm)."""
    with _lock, db() as c:
        rows = c.execute("SELECT * FROM alerts WHERE active = 1 "
                         "AND (triggered = 0 OR repeat = 1)").fetchall()
        return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Per-account preferences + the whole-watchlist rating watch
# ---------------------------------------------------------------------------

def get_prefs(user_id) -> dict:
    if not user_id:
        return {}
    with _lock, db() as c:
        row = c.execute("SELECT prefs FROM user_prefs WHERE user_id = ?", (user_id,)).fetchone()
    try:
        prefs = json.loads(row["prefs"]) if row else {}
    except ValueError:
        prefs = {}
    return prefs if isinstance(prefs, dict) else {}


def set_pref(user_id: int, key: str, value) -> None:
    prefs = get_prefs(user_id)
    prefs[key] = value
    with _lock, db() as c:
        c.execute("INSERT INTO user_prefs (user_id, prefs) VALUES (?,?) "
                  "ON CONFLICT(user_id) DO UPDATE SET prefs = excluded.prefs",
                  (user_id, json.dumps(prefs)))


def rating_watch_users() -> list[int]:
    """Accounts that asked to hear about every rating change in their watchlist."""
    with _lock, db() as c:
        rows = c.execute("SELECT user_id, prefs FROM user_prefs").fetchall()
    out = []
    for r in rows:
        try:
            if (json.loads(r["prefs"]).get("rating_watch") or {}).get("enabled"):
                out.append(int(r["user_id"]))
        except (ValueError, AttributeError):
            continue
    return out


def claim_rating_notice(user_id: int, ticker: str, day: str) -> bool:
    """True the FIRST time a rating change of `ticker` on `day` is reported to
    this account (so it is reported once, not at every check)."""
    with _lock, db() as c:
        cur = c.execute("INSERT OR IGNORE INTO rating_notices (user_id, ticker, date) "
                        "VALUES (?,?,?)", (user_id, ticker.upper(), str(day)))
        c.execute("DELETE FROM rating_notices WHERE date < date('now', '-45 days')")
        return cur.rowcount > 0


# ---------------------------------------------------------------------------
# Notifications (in-app)
# ---------------------------------------------------------------------------

def add_notification(user_id, title: str, body: str = "", ticker: str = "") -> None:
    with _lock, db() as c:
        c.execute("INSERT INTO notifications (user_id, title, body, ticker) VALUES (?,?,?,?)",
                  (user_id, title, body, ticker))


def get_notifications(user_id, limit: int = 60) -> list[dict]:
    with _lock, db() as c:
        rows = c.execute("SELECT * FROM notifications WHERE user_id = ? "
                         "ORDER BY id DESC LIMIT ?", (user_id, limit)).fetchall()
        return [dict(r) for r in rows]


def unread_count(user_id) -> int:
    with _lock, db() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM notifications "
                        "WHERE user_id = ? AND read = 0", (user_id,)).fetchone()
        return int(row["n"])


def mark_notifications_read(user_id, nid=None) -> None:
    with _lock, db() as c:
        if nid:
            c.execute("UPDATE notifications SET read = 1 WHERE id = ? AND user_id = ?",
                      (nid, user_id))
        else:
            c.execute("UPDATE notifications SET read = 1 WHERE user_id = ?", (user_id,))


# ---------------------------------------------------------------------------
# App config (config.json) — app-level settings for alert channels & schedule
# ---------------------------------------------------------------------------

def _read_saved_config():
    """The saved config.json object, or None if there is no readable file.

    Read under the same lock save_config() writes under. Without it a read
    that landed mid-write saw a half-written file, fell back to the defaults
    and then SAVED them (new secret key) — wiping the email/Telegram settings.
    """
    for _attempt in range(3):
        with _lock:
            if not os.path.exists(CONFIG_PATH):
                return None
            try:
                with open(CONFIG_PATH) as f:
                    return json.load(f)
            except Exception:
                pass
        time.sleep(0.05)      # antivirus / OneDrive may hold the file briefly
    return None


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    saved = _read_saved_config()
    if isinstance(saved, dict):
        for k, v in saved.items():
            if isinstance(cfg.get(k), dict):
                # a hand-edited section that is not an object keeps its defaults
                if isinstance(v, dict):
                    cfg[k].update(v)
            else:
                cfg[k] = v
    if not cfg.get("secret_key"):
        cfg["secret_key"] = uuid.uuid4().hex
        save_config(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    # write a temp file and swap it in, so a crash mid-write can never leave a
    # truncated config.json behind
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
        try:
            os.replace(tmp, CONFIG_PATH)
        except OSError:
            # Windows: another program is holding config.json — write in place
            with open(CONFIG_PATH, "w") as f:
                json.dump(cfg, f, indent=2)
            try:
                os.remove(tmp)
            except OSError:
                pass


def secret_key() -> str:
    return load_config()["secret_key"]
