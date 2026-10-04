"""
Dr. SHAH'S US STOCKS ANALYSIS — Flask application
===================================================
Endpoints:
  GET   /                              → dashboard
  Auth: /api/auth/me | register | login | logout        (session cookies)
  Watchlist: /api/watchlist | add | remove | import | save   (per-user)
  Analysis: /api/analyze | /api/stock/<ticker>
  Alerts: GET/POST /api/alerts | toggle | delete
  Notifications: /api/notifications | unread | read
  Settings: /api/settings | test-email | test-telegram
  Exports: watchlist.pdf | report.pdf | indicators.csv | indicators.xlsx
  Schedule: /api/schedule/run-now
"""
import gzip
import json
import logging
import os
import re
import sys
import threading
import traceback
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import instance

if __name__ == "__main__":
    # FIRST thing, before the slow imports: only one copy per port. A second
    # copy (e.g. opened by hand while "Start with Windows" is still starting
    # the first) waits for the running one and shows its dashboard instead.
    _instance_lock = instance.InstanceLock(int(os.environ.get("PORT", 5000)))
    if not _instance_lock.acquire():
        instance.hand_over(int(os.environ.get("PORT", 5000)),
                           open_browser="--background" not in sys.argv[1:])
        raise SystemExit(0)

import applog
import paths

if __name__ == "__main__":
    applog.setup()          # logs/app.log from the very first line (server runs only)

from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory, session)

import alerts as alerts_mod
import analysis
import analyzer
import autostart
import backtest
import backups
import data as data_layer
import indices
import market
import pdfexport
import scheduler
import store
import tray

BASE = paths.BUNDLE_DIR          # templates/ static/ (works in the .exe too)
TICKER_RE = re.compile(r"^[A-Z][A-Z0-9.\-]{0,9}$")
MAX_WATCHLIST = 60

app = Flask(__name__,
              template_folder=paths.TEMPLATES_DIR,
              static_folder=paths.STATIC_DIR)
app.secret_key = store.secret_key()
app.config.update(
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_NAME="drshah_session",
    PERMANENT_SESSION_LIFETIME=timedelta(days=365),   # "stay signed in" after a login
    SEND_FILE_MAX_AGE_DEFAULT=604800,   # cache /static/* in the browser for 7 days
    MAX_CONTENT_LENGTH=8 * 1024 * 1024,  # reject oversized uploads instead of choking
)

_seeded = paths.seed_first_run()      # .exe only: copy the starter watchlist across
if _seeded:
    print(f"[setup] First run — created {', '.join(_seeded)} next to the program.")

store.init_db()
if __name__ != "__main__":
    # imported (tests / another WSGI host). When run directly the scheduler is
    # started further down, once this copy knows it is the only one running —
    # a second copy must never run reports, alerts and backups a second time.
    scheduler.start()
analysis.load_saved_snapshot()   # seed cache from last results → instant first render


def _uid():
    """Current user id from session (None = guest)."""
    return session.get("user_id")


LOOPBACK = ("127.0.0.1", "::1", "::ffff:127.0.0.1")


def _is_this_computer() -> bool:
    """The request comes from a browser on the PC the app runs on."""
    return (request.remote_addr or "") in LOOPBACK


def _sign_in(user_id: int) -> None:
    session.clear()
    session["user_id"] = int(user_id)
    session.permanent = True          # survives closing the browser (1 year)


@app.before_request
def _guard_and_auto_sign_in():
    # 1) Another website must not be able to push buttons in this app: a
    #    browser always sends Origin on a cross-site POST, and it must be us.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("Origin")
        if origin and urlsplit(origin).netloc != request.host:
            return jsonify({"ok": False, "error": "Cross-site request refused."}), 403

    # 2) Automatic sign-in — only for a browser on THIS computer, only when it
    #    has been switched on for an account, and not right after "Sign out"
    #    (that lasts until the browser is closed).
    if not (request.path.startswith("/static/") or session.get("user_id")
            or session.get("signed_out") or not _is_this_computer()):
        auto = store.load_config().get("auto_login") or {}
        if auto.get("enabled") and auto.get("username"):
            user = store.get_user_by_username(str(auto["username"]))
            if user:
                _sign_in(user["id"])

    # 3) App-wide settings, saved passwords, backups and jobs: this computer,
    #    or the owner account signed in from another device. Anyone else on the
    #    network (a guest, or an account they registered themselves) is refused.
    if request.path in ADMIN_PATHS and not _is_admin():
        return jsonify({"ok": False, "error": ADMIN_ONLY_MESSAGE}), 403
    return None


ADMIN_PATHS = frozenset({
    "/api/settings", "/api/settings/test-email", "/api/settings/test-telegram",
    "/api/backups", "/api/backups/run", "/api/autostart",
    "/api/schedule/run-now", "/api/prewarm/run-now",
})
ADMIN_ONLY_MESSAGE = ("Settings, backups and reports can only be managed on the computer the "
                      "app runs on — or from another device after signing in with the owner "
                      "account (the first account that was created).")


def _is_admin() -> bool:
    """May this request see or change app-wide settings, secrets and backups?"""
    if _is_this_computer():
        return True
    uid = _uid()
    return bool(uid) and int(uid) == store.first_user_id()


# ---------------------------------------------------------------------------
# Performance: gzip compression + browser cache headers
# ---------------------------------------------------------------------------

@app.after_request
def _performance_headers(resp):
    # browser caching: static assets are long-lived (they rarely change);
    # everything else (HTML page, JSON APIs, exports) must never be cached
    if request.path.startswith("/static/"):
        resp.headers.setdefault("Cache-Control", "public, max-age=604800")
    else:
        resp.headers["Cache-Control"] = "no-store"

    # gzip-compress text / json responses when the client accepts it
    try:
        if resp.direct_passthrough:
            return resp
        data = resp.get_data()
        if len(data) < 512:                     # tiny responses not worth the CPU
            return resp
        ct = resp.content_type or ""
        # whitelist — only text/json benefits from gzip; PDF/XLSX/PNG are already
        # compressed binary formats and would waste CPU for no gain
        compressible = (ct.startswith("text/")
                        or ct in ("application/json", "application/javascript")
                        or ct.endswith("+json"))
        if not compressible:
            return resp
        if "gzip" not in (request.headers.get("Accept-Encoding") or ""):
            return resp
        compressed = gzip.compress(data, compresslevel=6)
        if len(compressed) >= len(data):        # no gain → serve as-is
            return resp
        resp.set_data(compressed)
        resp.headers["Content-Encoding"] = "gzip"
        resp.headers["Vary"] = "Accept-Encoding"
    except Exception:  # noqa: BLE001 — compression must never break a response
        pass
    return resp


def _normalize_ticker(raw) -> str:
    """Upper-case a typed symbol and use Yahoo's share-class form.

    Yahoo only knows class shares with a dash: "BRK.B" returns no data, "BRK-B"
    does. A dotted one-letter class becomes a dash; any other dotted suffix
    (an exchange code such as "AAPL.US") is dropped.
    """
    t = str(raw).strip().upper()
    base, dot, suffix = t.partition(".")
    if not dot:
        return t
    return f"{base}-{suffix}" if len(suffix) == 1 and suffix.isalpha() else base


def _require_user():
    uid = _uid()
    if not uid:
        return None, (jsonify({"ok": False, "error": "Please sign in to use this feature."}), 401)
    return int(uid), None


def _json_body() -> dict:
    """Always return a dict for a JSON request body.

    Read the raw JSON body safely. Using `request.get_json(silent=True) or {}`
    is unsafe on its own: a body that is valid JSON but not an object
    (e.g. `[1,2]`, `12345`, `"text"`) is returned as-is and the later `.get(...)`
    call raises AttributeError → HTTP 500. This normalises every shape
    (None, list, number, string, malformed) to an empty dict.
    """
    body = request.get_json(silent=True)
    return body if isinstance(body, dict) else {}


@app.errorhandler(Exception)
def _unexpected_error(e):
    """Never show a user an HTML stack-trace page: API calls get JSON, pages get
    a short friendly message. The full traceback still goes to the console."""
    from werkzeug.exceptions import HTTPException
    if isinstance(e, HTTPException):
        if request.path.startswith("/api/"):
            return jsonify({"ok": False, "error": e.description or e.name,
                            "status": e.code}), e.code
        return e
    traceback.print_exc()
    if request.path.startswith("/api/"):
        return jsonify({"ok": False,
                        "error": f"Something went wrong ({type(e).__name__}). "
                                 f"Please try again."}), 500
    return ("<h3>Something went wrong</h3><p>Please restart the app "
            "(run start.bat) and try again.</p>", 500)


@app.errorhandler(404)
def _not_found(e):
    if request.path.startswith("/api/"):
        return jsonify({"ok": False, "error": "Unknown API endpoint."}), 404
    return e


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/favicon.ico")
def favicon():
    """Browsers request this on their own — answer with the app icon instead of
    a 404 in the console/logs."""
    return send_from_directory(paths.STATIC_DIR, "favicon.png",
                               mimetype="image/png")


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------

@app.route("/api/auth/me")
def api_auth_me():
    uid = _uid()
    local = _is_this_computer()
    if uid:
        user = store.get_user(int(uid))
        if user:
            auto = store.load_config().get("auto_login") or {}
            return jsonify({"logged_in": True, "username": user["username"],
                            "email": user["email"],
                            # automatic sign-in on this computer (Settings)
                            "auto_login": bool(auto.get("enabled")
                                               and auto.get("username") == user["username"]),
                            "auto_login_available": local,
                            "can_admin": _is_admin()})   # Settings / backups allowed?
        session.pop("user_id", None)          # account no longer exists
    return jsonify({"logged_in": False, "auto_login_available": local,
                    "can_admin": _is_admin()})


@app.route("/api/auth/auto-login", methods=["POST"])
def api_auth_auto_login():
    """Switch "sign me in automatically on this computer" on/off for the
    signed-in account. Only a browser on the PC itself may change it."""
    uid, err = _require_user()
    if err:
        return err
    if not _is_this_computer():
        return jsonify({"ok": False, "error": "Automatic sign-in can only be changed on the "
                        "computer the app runs on."}), 403
    user = store.get_user(uid)
    enabled = bool(_json_body().get("enabled"))
    cfg = store.load_config()
    cfg["auto_login"] = {"enabled": enabled, "username": user["username"] if enabled else ""}
    store.save_config(cfg)
    return jsonify({"ok": True, "auto_login": enabled, "username": user["username"]})


@app.route("/api/auth/register", methods=["POST"])
def api_auth_register():
    body = _json_body()
    username = str(body.get("username", "")).strip()
    email = str(body.get("email", "")).strip()
    password = str(body.get("password", ""))
    if not re.match(r"^[A-Za-z0-9_]{3,20}$", username):
        return jsonify({"ok": False, "error": "Username must be 3–20 letters, numbers or underscores."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Password must be at least 6 characters."}), 400
    if len(password) > 128:
        return jsonify({"ok": False, "error": "Password is too long (maximum 128 characters)."}), 400
    if email and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        return jsonify({"ok": False, "error": "That email address looks invalid."}), 400
    if store.get_user_by_username(username):
        return jsonify({"ok": False, "error": "That username is already taken."}), 409
    uid = store.create_user(username, email, password)
    _sign_in(uid)
    return jsonify({"ok": True, "username": username})


@app.route("/api/auth/login", methods=["POST"])
def api_auth_login():
    body = _json_body()
    username = str(body.get("username", "")).strip()
    password = str(body.get("password", ""))
    user = store.verify_user(username, password)
    if not user:
        return jsonify({"ok": False, "error": "Invalid username or password."}), 401
    _sign_in(user["id"])
    return jsonify({"ok": True, "username": user["username"], "email": user["email"]})


@app.route("/api/auth/logout", methods=["POST"])
def api_auth_logout():
    session.clear()
    # stay signed out even if automatic sign-in is on — until the browser is
    # closed (this flag lives in a session cookie, not a permanent one)
    session["signed_out"] = True
    session.permanent = False
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Watchlist (per-user; guest uses watchlist.json)
# ---------------------------------------------------------------------------

@app.route("/api/watchlist")
def api_watchlist():
    return jsonify({"tickers": store.get_watchlist(_uid())})


@app.route("/api/watchlist/add", methods=["POST"])
def api_watchlist_add():
    body = _json_body()
    ticker = _normalize_ticker(body.get("ticker", ""))
    if not TICKER_RE.match(ticker):
        return jsonify({"ok": False, "error": f"'{ticker}' is not a valid US ticker symbol."}), 400
    wl = store.get_watchlist(_uid())
    if ticker in wl:
        return jsonify({"ok": False, "error": f"{ticker} is already in the watchlist."}), 409
    if len(wl) >= MAX_WATCHLIST:
        return jsonify({"ok": False, "error": f"Watchlist limit reached ({MAX_WATCHLIST} stocks)."}), 400
    try:
        df = data_layer.fetch_history(ticker, period="3y")
        if len(df) < 5:
            raise ValueError("too little data")
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Cannot add {ticker} — {str(e)[:120]}"}), 400
    wl.append(ticker)
    store.set_watchlist(_uid(), wl)
    return jsonify({"ok": True, "tickers": wl})


@app.route("/api/watchlist/remove", methods=["POST"])
def api_watchlist_remove():
    body = _json_body()
    ticker = str(body.get("ticker", "")).strip().upper()
    wl = store.get_watchlist(_uid())
    if ticker in wl:
        wl.remove(ticker)
        store.set_watchlist(_uid(), wl)
        analysis.ANALYSIS_CACHE.pop(ticker, None)
    return jsonify({"ok": True, "tickers": wl})


@app.route("/api/watchlist/import", methods=["POST"])
def api_watchlist_import():
    body = _json_body()
    raw = body.get("tickers", [])
    if isinstance(raw, str):
        raw = re.split(r"[\s,;]+", raw)
    if not isinstance(raw, (list, tuple)):
        return jsonify({"ok": False,
                        "error": "Send the symbols as a list, e.g. "
                                 "{\"tickers\": [\"AAPL\", \"MSFT\"]} or a "
                                 "comma-separated text line."}), 400
    seen = set(store.get_watchlist(_uid()))
    added, rejected = [], []
    for item in raw:
        t = _normalize_ticker(item)       # "BRK.B" → "BRK-B" (was cut to "BRK")
        if not t or not TICKER_RE.match(t) or t in seen:
            continue
        seen.add(t)
        added.append(t)
    if len(seen) > MAX_WATCHLIST:
        return jsonify({"ok": False, "error": f"Import would exceed the {MAX_WATCHLIST}-stock limit."}), 400
    for t in list(added):
        try:
            df = data_layer.fetch_history(t, period="3y")
            if len(df) < 5:
                raise ValueError("too little data")
        except Exception as e:  # noqa: BLE001
            seen.discard(t)
            rejected.append({"ticker": t, "reason": str(e)[:100]})
            added.remove(t)
    wl = store.get_watchlist(_uid())
    for t in added:
        if t not in wl:
            wl.append(t)
    store.set_watchlist(_uid(), wl)
    return jsonify({"ok": True, "added": added, "rejected": rejected, "tickers": wl})


@app.route("/api/watchlist/save", methods=["POST"])
def api_watchlist_save():
    store.set_watchlist(_uid(), store.get_watchlist(_uid()))
    return jsonify({"ok": True, "message": "Watchlist saved."})


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

@app.route("/api/analyze")
def api_analyze():
    force = request.args.get("force", "0") == "1"
    cached = request.args.get("cached", "0") == "1"
    uid = _uid()
    # --- instant path: serve the last saved results (stale-while-revalidate) --
    if cached and not force:
        try:
            tickers = store.get_watchlist(uid)
            snap = analysis.load_saved_snapshot()
            results = {t: snap[t] for t in tickers if t in snap}
            cached_at = max((r.get("computed_at") or 0) for r in results.values()) if results else None
            if results:
                return jsonify({"results": results, "errors": {},
                                "from_cache": True, "cached_at": cached_at,
                                "data": analysis.summarize_freshness(results)})
        except Exception:  # noqa: BLE001 — fall through to a full analysis
            pass
    try:
        payload = analysis.analyze_watchlist(store.get_watchlist(uid), force=force)
        payload["data"] = analysis.summarize_freshness(payload["results"])
        if uid:
            alerts_mod.evaluate_results(payload["results"], int(uid))
        return jsonify(payload)
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/api/stock/<ticker>")
def api_stock(ticker):
    t = ticker.strip().upper()
    try:
        force = request.args.get("force") == "1"
        df = data_layer.fetch_history(t, force=force)
        bench = analysis.benchmark_close(force=force)
        result = analyzer.analyze(df, benchmark=bench)
        analysis.decorate(t, df, result, bench, backfill_now=True)
        result["history"] = analyzer.build_detail_history(df)
        result["score_history"] = analysis.score_history(t, df, bench)
        return jsonify(result)
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"No data for {t} — {str(e)[:120]}"}), 404


# ---------------------------------------------------------------------------
# Alerts (login required)
# ---------------------------------------------------------------------------

@app.route("/api/alerts")
def api_alerts():
    uid, err = _require_user()
    if err:
        return err
    return jsonify({"ok": True, "alerts": store.get_alerts(uid),
                    "rating_watch": _clean_rating_watch(store.get_prefs(uid).get("rating_watch"))})


def _clean_rating_watch(raw) -> dict:
    """{"enabled": bool, "channels": [...]} — the whole-watchlist rating watch."""
    raw = raw if isinstance(raw, dict) else {}
    chans = raw.get("channels")
    chans = [c for c in chans if c in ("app", "email", "telegram")] if isinstance(chans, list) else []
    if "app" not in chans:
        chans.insert(0, "app")                # always shown in the bell
    return {"enabled": bool(raw.get("enabled")), "channels": chans}


@app.route("/api/alerts/rating-watch", methods=["POST"])
def api_alerts_rating_watch():
    """Tell me when ANY stock in my watchlist changes rating (one switch
    instead of one alert per stock; new stocks are covered automatically)."""
    uid, err = _require_user()
    if err:
        return err
    watch = _clean_rating_watch(_json_body())
    store.set_pref(uid, "rating_watch", watch)
    return jsonify({"ok": True, "rating_watch": watch})


@app.route("/api/alerts", methods=["POST"])
def api_alerts_create():
    uid, err = _require_user()
    if err:
        return err
    body = _json_body()
    ticker = str(body.get("ticker", "")).strip().upper()
    kind = str(body.get("kind", ""))
    value = body.get("value")
    channels = body.get("channels") or ["app"]
    if not TICKER_RE.match(ticker):
        return jsonify({"ok": False, "error": "Invalid ticker symbol."}), 400
    if kind not in VALID_ALERT_KINDS:
        return jsonify({"ok": False, "error": "Unknown alert condition."}), 400
    if kind not in NO_VALUE_KINDS and value in (None, ""):
        return jsonify({"ok": False, "error": "Please set a target value."}), 400
    if kind == "rating_is" and value not in ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell"):
        return jsonify({"ok": False, "error": "Please choose a rating."}), 400
    if not isinstance(channels, list) or not channels:
        channels = ["app"]
    aid = store.add_alert(uid, ticker, kind, value, channels, repeat=bool(body.get("repeat")))
    return jsonify({"ok": True, "id": aid})


@app.route("/api/alerts/<int:alert_id>/toggle", methods=["POST"])
def api_alerts_toggle(alert_id):
    uid, err = _require_user()
    if err:
        return err
    if not store.toggle_alert(alert_id, uid):
        return jsonify({"ok": False, "error": "Alert not found."}), 404
    return jsonify({"ok": True})


@app.route("/api/alerts/<int:alert_id>/delete", methods=["POST"])
def api_alerts_delete(alert_id):
    uid, err = _require_user()
    if err:
        return err
    if not store.delete_alert(alert_id, uid):
        return jsonify({"ok": False, "error": "Alert not found."}), 404
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Notifications (login required)
# ---------------------------------------------------------------------------

@app.route("/api/notifications")
def api_notifications():
    uid, err = _require_user()
    if err:
        return err
    return jsonify({"ok": True, "notifications": store.get_notifications(uid)})


@app.route("/api/notifications/unread")
def api_notifications_unread():
    uid, err = _require_user()
    if err:
        return err
    notifs = store.get_notifications(uid, limit=5)
    latest = notifs[0] if notifs else None
    return jsonify({"ok": True, "count": store.unread_count(uid), "latest": latest})


@app.route("/api/notifications/read", methods=["POST"])
def api_notifications_read():
    uid, err = _require_user()
    if err:
        return err
    body = _json_body()
    store.mark_notifications_read(uid, body.get("id"))
    return jsonify({"ok": True})


# ---------------------------------------------------------------------------
# Settings (app-level: alert channels + schedule)
# ---------------------------------------------------------------------------

@app.route("/api/settings")
def api_settings():
    cfg = store.load_config()
    safe = json.loads(json.dumps(cfg))
    safe["email"]["smtp_pass"] = "••••••" if safe["email"].get("smtp_pass") else ""
    safe["telegram"]["bot_token"] = "••••••" if safe["telegram"].get("bot_token") else ""
    safe["secret_key"] = None
    safe.pop("auto_login", None)          # per-account: served by /api/auth/me
    # why a scheduled report would not be delivered (shown in Settings and once
    # on the dashboard) — the report is still saved in reports/ either way
    safe["delivery_warnings"] = scheduler.delivery_problems(cfg)
    safe["delivery_working"] = scheduler.delivery_working(cfg)   # channels that DO work
    safe["lan_url"] = _lan_url()          # address for a phone on the same network
    safe["lan_active"] = LISTEN_HOST != "127.0.0.1"   # what the running app does now
    return jsonify(safe)


LISTEN_HOST = "0.0.0.0"       # set at start-up from Settings → network.lan_access


def _lan_url() -> str | None:
    """http://<this PC's address on the local network>:<port>, if any."""
    import socket
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))          # no packet is sent
            ip = s.getsockname()[0]
    except OSError:
        return None
    if not ip or ip.startswith("127."):
        return None
    return f"http://{ip}:{int(os.environ.get('PORT', 5000))}"


@app.route("/api/prewarm/status")
def api_prewarm_status():
    return jsonify(scheduler.prewarm_status())


@app.route("/api/market-status")
def api_market_status():
    return jsonify(market.market_status())


@app.route("/api/indices")
def api_indices():
    force = request.args.get("force", "0") == "1"
    return jsonify(indices.fetch_indices(force=force))


@app.route("/api/data-status")
def api_data_status():
    """Which trading session are the displayed prices from?

    Read-only and network-free: it reports the newest bar we already hold for
    every watchlist symbol against the newest expected NYSE session, so the
    header can show "Data as of <date>" — and warn if a bar is missing.
    """
    try:
        tickers = store.get_watchlist(_uid())
    except Exception:  # noqa: BLE001
        tickers = []
    items = []
    for t in tickers:
        try:
            with analysis._lock:
                hit = analysis.ANALYSIS_CACHE.get(t)
            df = hit[1].get("data_status") if hit else None
            if isinstance(df, dict) and df.get("as_of"):
                items.append(df)
            else:
                items.append(data_layer.freshness(t))
        except Exception:  # noqa: BLE001 — one bad symbol must not break the header
            continue
    # on a completely cold start nothing has a date yet — never raise here,
    # the header simply shows "Data as of —" until the first analysis finishes
    as_of = max((i["as_of"] for i in items if i.get("as_of")), default=None)
    if as_of is None:
        # fall back to whatever the disk cache already holds
        try:
            disk_days = [data_layer._disk_latest_date(t) for t in tickers]
            disk_days = [d for d in disk_days if d]
            if disk_days:
                as_of = max(disk_days).isoformat()
        except Exception:  # noqa: BLE001
            as_of = None
    stale = [i["ticker"] for i in items if i.get("current") is False]
    return jsonify({
        "expected": data_layer.latest_expected_session().isoformat(),
        "as_of": as_of,
        "current": (not stale) if as_of else None,
        "stale": stale,
        "checked": len(items),
        "stats": dict(data_layer.STATS),
    })


@app.route("/api/prewarm/run-now", methods=["POST"])
def api_prewarm_run_now():
    try:
        started = scheduler.run_prewarm_now()
        return jsonify({"ok": True, "started": started,
                        "message": "Refresh started in the background." if started
                        else "A refresh is already running."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


def _valid_hhmm(value) -> bool:
    """A 24-hour "HH:MM" time. Anything else (e.g. "25:00") would make the
    daily report silently never run."""
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", str(value).strip())
    return bool(m) and int(m.group(1)) < 24 and int(m.group(2)) < 60


# ---------------------------------------------------------------------------
# "Does the score work?" test, backups, start with Windows
# ---------------------------------------------------------------------------

@app.route("/api/backtest")
def api_backtest():
    return jsonify(backtest.status())


@app.route("/api/backtest/run", methods=["POST"])
def api_backtest_run():
    tickers = store.get_watchlist(_uid())
    if not tickers:
        return jsonify({"ok": False, "error": "Your watchlist is empty — add stocks first."}), 400
    started = backtest.start(tickers)
    return jsonify({"ok": True, "started": started, "total": len(tickers),
                    "message": f"Testing the score on {len(tickers)} stocks…" if started
                    else "A test is already running."})


@app.route("/api/backups")
def api_backups():
    return jsonify({"folder": backups.BACKUPS_DIR, "keep": backups.KEEP,
                    "backups": backups.list_backups(), "log_file": applog.LOG_FILE,
                    "data_folder": paths.DATA_DIR})


@app.route("/api/backups/run", methods=["POST"])
def api_backups_run():
    try:
        path = backups.make_backup()
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Backup failed: {str(e)[:150]}"}), 500
    return jsonify({"ok": True, "message": f"Backup saved: {os.path.basename(path)}",
                    "backups": backups.list_backups()})


@app.route("/api/autostart")
def api_autostart():
    supported = autostart.supported()
    return jsonify({"supported": supported,
                    "enabled": autostart.is_enabled() if supported else False})


@app.route("/api/autostart", methods=["POST"])
def api_autostart_set():
    if not autostart.supported():
        return jsonify({"ok": False, "error": "Start with Windows is available in the "
                        "Windows .exe version only."}), 400
    try:
        enabled = autostart.set_enabled(bool(_json_body().get("enabled")))
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Could not change it: {str(e)[:150]}"}), 500
    return jsonify({"ok": True, "enabled": enabled})


@app.route("/api/settings", methods=["POST"])
def api_settings_save():
    body = _json_body()
    cfg = store.load_config()
    warnings = []                 # things that were NOT saved, and why

    if "alert_check_minutes" in body:
        try:
            cfg["alert_check_minutes"] = max(1, min(int(body["alert_check_minutes"]), 1440))
        except (TypeError, ValueError):
            pass

    em = body.get("email")
    if isinstance(em, dict):
        cfg["email"]["enabled"] = bool(em.get("enabled"))
        for k in ("smtp_host", "smtp_user", "from_addr", "to_addr"):
            if em.get(k) is not None:
                cfg["email"][k] = str(em[k]).strip()
        if em.get("smtp_port") is not None:
            try:
                cfg["email"]["smtp_port"] = int(em["smtp_port"]) or 587
            except (TypeError, ValueError):
                pass
        pw = str(em.get("smtp_pass") or "")
        if pw and pw.replace("•", "").strip():
            cfg["email"]["smtp_pass"] = pw.strip()

    tg = body.get("telegram")
    if isinstance(tg, dict):
        cfg["telegram"]["enabled"] = bool(tg.get("enabled"))
        if tg.get("chat_id") is not None:
            cfg["telegram"]["chat_id"] = str(tg["chat_id"]).strip()
        tok = tg.get("bot_token")
        if tok and tok != "••••••":
            cleaned = str(tok).strip()
            if cleaned.lower().startswith("bot"):
                cleaned = cleaned[3:]
            # A real token is "<bot number>:<35-character secret>". Saving half
            # of it (or any other text) used to replace a working token
            # silently, and every Telegram message then failed.
            if alerts_mod.valid_bot_token(cleaned):
                cfg["telegram"]["bot_token"] = cleaned
            else:
                warnings.append("The Telegram bot token was NOT saved — it does not look like a "
                                "token. Copy the whole code from @BotFather, e.g. "
                                "123456789:AAExampleExampleExampleExampleExample "
                                "(the bot number, a colon, then the secret part).")

    sc = body.get("schedule")
    if isinstance(sc, dict):
        cfg["schedule"]["enabled"] = bool(sc.get("enabled"))
        if sc.get("time") and _valid_hhmm(sc["time"]):
            cfg["schedule"]["time"] = str(sc["time"]).strip()
        cfg["schedule"]["email_delivery"] = bool(sc.get("email_delivery"))
        cfg["schedule"]["telegram_delivery"] = bool(sc.get("telegram_delivery"))
        try:
            cfg["schedule"]["keep_days"] = max(1, int(sc.get("keep_days") or 30))
        except (TypeError, ValueError):
            pass

    pw = body.get("prewarm")
    if isinstance(pw, dict):
        cfg["prewarm"]["enabled"] = bool(pw.get("enabled"))
        try:
            cfg["prewarm"]["interval_minutes"] = max(5, min(int(pw.get("interval_minutes") or 15), 1440))
        except (TypeError, ValueError):
            pass
        if "market_hours_only" in pw:
            cfg["prewarm"]["market_hours_only"] = bool(pw.get("market_hours_only"))
        # changing the interval re-arms the next run promptly and updates
        # the scheduler state immediately (no waiting for the next tick)
        scheduler.arm_prewarm(now_plus=60,
                              interval_minutes=cfg["prewarm"]["interval_minutes"],
                              market_hours_only=cfg["prewarm"].get("market_hours_only", True))

    net = body.get("network")
    if isinstance(net, dict) and "lan_access" in net:
        cfg.setdefault("network", {})["lan_access"] = bool(net.get("lan_access"))

    store.save_config(cfg)
    return jsonify({"ok": True, "message": "Settings saved.", "warnings": warnings})


@app.route("/api/settings/test-email", methods=["POST"])
def api_settings_test_email():
    cfg = store.load_config()
    em = cfg["email"]
    # --- precise diagnostics: say exactly WHAT is missing ------------------
    missing = []
    if not em.get("enabled"):
        missing.append("the 'Enable email channel' checkbox is not ticked")
    if not em.get("smtp_host"):
        missing.append("SMTP host (e.g. smtp.gmail.com)")
    if not em.get("smtp_user"):
        missing.append("Username (your full email address)")
    if not (em.get("smtp_pass") or "").strip("•").strip():
        missing.append("Password / app password")
    if missing:
        return jsonify({"ok": False, "error": "Email not configured yet — missing: "
                        + "; ".join(missing) + ".",
                        "missing": missing}), 400
    to_addr = em.get("to_addr") or em.get("from_addr") or ""
    uid = _uid()
    if not to_addr and uid:
        u = store.get_user(int(uid))
        to_addr = (u or {}).get("email") or ""
    if not to_addr:
        return jsonify({"ok": False, "error": "No recipient: set the 'To' address "
                        "(or 'From' address — we will send the test to yourself)."}), 400
    try:
        alerts_mod.send_email(em, "✅ Dr. Shah's — Test email",
                              "If you can read this, email alerts are working!\n\n— Dr. Shah's US Stocks Analysis",
                              to_addr)
        return jsonify({"ok": True, "message": f"Test email sent to {to_addr}."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Email failed: {str(e)[:200]}"}), 400


@app.route("/api/settings/test-telegram", methods=["POST"])
def api_settings_test_telegram():
    cfg = store.load_config()
    tg = cfg["telegram"]
    if not tg.get("enabled") or not tg.get("bot_token") or not tg.get("chat_id"):
        return jsonify({"ok": False, "error": "Telegram channel is not configured."}), 400
    try:
        alerts_mod.send_telegram(tg, "<b>✅ Dr. Shah's</b> — test message from your alert bot")
        return jsonify({"ok": True, "message": "Test message sent to Telegram."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": f"Telegram failed: {str(e)[:200]}"}), 400


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

def _export_payload():
    return analysis.analyze_watchlist(store.get_watchlist(_uid()))


def _send_bytes(data: bytes, mimetype: str, name: str, attach: bool = False):
    """Serve generated bytes. Built with a plain Response (not send_file) so the
    gzip after-request hook can also compress text exports (e.g. CSV)."""
    resp = Response(data, mimetype=mimetype)
    disp = "attachment" if attach else "inline"
    resp.headers["Content-Disposition"] = f"{disp}; filename={name}"
    return resp


@app.route("/api/export/watchlist.pdf")
def api_export_watchlist_pdf():
    payload = _export_payload()
    return _send_bytes(pdfexport.build_watchlist_pdf(payload["results"], payload["errors"]),
                       "application/pdf", "watchlist_analysis.pdf")


@app.route("/api/export/report.pdf")
def api_export_report_pdf():
    payload = _export_payload()
    return _send_bytes(pdfexport.build_report_pdf(payload["results"], payload["errors"]),
                       "application/pdf", "analysis_report.pdf")


@app.route("/api/export/indicators.csv")
def api_export_indicators_csv():
    payload = _export_payload()
    return _send_bytes(pdfexport.build_indicators_csv(payload["results"]),
                       "text/csv", "indicators_export.csv", attach=True)


@app.route("/api/export/indicators.xlsx")
def api_export_indicators_xlsx():
    payload = _export_payload()
    return _send_bytes(pdfexport.build_indicators_xlsx(payload["results"]),
                       "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                       "indicators_export.xlsx", attach=True)


# ---------------------------------------------------------------------------
# Schedule
# ---------------------------------------------------------------------------

@app.route("/api/schedule/run-now", methods=["POST"])
def api_schedule_run_now():
    try:
        result = scheduler.run_daily_reports()
        return jsonify({"ok": True, "count": result["count"],
                        "message": f"Generated {result['count']} report(s) into /reports."})
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 500


def _should_open_browser() -> bool:
    """Open the dashboard in the browser when the server starts?

    • packaged .exe → yes: double-clicking it is the whole launcher, and nothing
      sets AUTO_OPEN_BROWSER there (that is why the .exe never opened it)
    • from source   → only when start.bat / start.sh / run_server.bat ask
    AUTO_OPEN_BROWSER=1 / =0 always forces it on / off.
    """
    flag = os.environ.get("AUTO_OPEN_BROWSER", "").strip()
    if flag in ("0", "1"):
        return flag == "1"
    return paths.is_frozen()


def _open_url(url: str) -> bool:
    """Open `url` in the default browser; on Windows fall back to the shell."""
    return instance.open_url(url)


def _maybe_open_browser(port: int) -> None:
    """Waits until the server actually responds, then opens the default browser."""
    import threading

    if not _should_open_browser():
        return

    def _wait_and_open():
        import time
        import urllib.request

        url = f"http://localhost:{port}"
        for _ in range(60):
            try:
                with urllib.request.urlopen(url, timeout=2):
                    break
            except Exception:
                time.sleep(1)
        time.sleep(0.5)
        if _open_url(url):
            print(f"[browser] Opened {url} in your web browser.")
        else:
            print(f"[browser] Could not open the browser by itself - "
                  f"type {url} into your browser.")

    threading.Thread(target=_wait_and_open, daemon=True).start()


# ---------------------------------------------------------------------------
# Backup / restore
# ---------------------------------------------------------------------------

VALID_ALERT_KINDS = {"price_above", "price_below", "rsi_above", "rsi_below",
                     "score_above", "score_below", "rating_is", "rating_change",
                     "breakout", "breakdown"}
NO_VALUE_KINDS = ("breakout", "breakdown", "rating_change")   # need no target value
VALID_CHANNELS = {"app", "email", "telegram"}
SETTINGS_KEYS = ("alert_check_minutes", "email", "telegram", "schedule", "prewarm")


def _sanitize_tickers(items) -> list[str]:
    out = []
    for it in items or []:
        t = str(it).strip().upper()
        if TICKER_RE.match(t) and t not in out and len(out) < MAX_WATCHLIST:
            out.append(t)
    return out


def _sanitize_alerts(items) -> list[dict]:
    out = []
    for a in items or []:
        if not isinstance(a, dict):
            continue
        tk = str(a.get("ticker", "")).strip().upper()
        kind = str(a.get("kind", ""))
        if not TICKER_RE.match(tk) or kind not in VALID_ALERT_KINDS:
            continue
        value = a.get("value")
        if kind not in NO_VALUE_KINDS and value in (None, ""):
            continue
        chans = a.get("channels") or ["app"]
        if isinstance(chans, str):
            chans = [chans]
        chans = [c for c in chans if c in VALID_CHANNELS] or ["app"]
        out.append({"ticker": tk, "kind": kind,
                    "value": str(value) if value is not None else "",
                    "channels": chans, "active": bool(a.get("active", True)),
                    "repeat": bool(a.get("repeat"))})
    return out


def _sanitize_settings(backup_settings) -> dict:
    cfg = store.load_config()
    if not isinstance(backup_settings, dict):
        return cfg
    for key in SETTINGS_KEYS:
        val = backup_settings.get(key)
        if val is None:
            continue
        if key == "alert_check_minutes":
            try:
                cfg[key] = max(1, min(int(val), 1440))
            except (TypeError, ValueError):
                pass
        elif isinstance(val, dict) and isinstance(cfg.get(key), dict):
            for k2, v2 in val.items():
                if k2 == "secret_key":
                    continue
                if isinstance(v2, bool):
                    cfg[key][k2] = v2
                elif isinstance(v2, (int, float)) and not isinstance(v2, bool):
                    try:
                        cfg[key][k2] = int(v2) if k2 in ("smtp_port", "keep_days") else v2
                    except (TypeError, ValueError):
                        pass
                elif isinstance(v2, str):
                    # never overwrite a stored secret with the masked placeholder
                    # ("••••••") that the settings screen shows
                    if v2.strip("••").strip() == "" and k2 in ("smtp_pass", "bot_token"):
                        continue
                    if key == "schedule" and k2 == "time" and not _valid_hhmm(v2):
                        continue
                    cfg[key][k2] = v2
    return cfg


@app.route("/api/backup")
def api_backup():
    uid = _uid()
    payload = {
        "app": "Dr. Shah's US Stocks Analysis",
        "version": 1,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "watchlist": store.get_watchlist(uid),
        "alerts": store.get_alerts(uid) if uid else [],
        "rating_watch": store.get_prefs(uid).get("rating_watch") if uid else None,
    }
    if _is_admin():
        # App settings hold the email / Telegram passwords, so they are only
        # given to this computer or the owner account — a visitor on the
        # network gets a backup of their own watchlist and alerts only.
        settings = store.load_config()
        # the session-signing key must never leave the app: anyone holding it
        # can forge a login cookie for ANY account (restore ignores it anyway)
        settings.pop("secret_key", None)
        settings.pop("auto_login", None)      # belongs to this installation
        payload["settings"] = settings
    data = json.dumps(payload, indent=2).encode("utf-8")
    return _send_bytes(data, "application/json", "drshah_backup.json", attach=True)


@app.route("/api/backup/restore", methods=["POST"])
def api_backup_restore():
    uid = _uid()
    body = _json_body()
    # Accept BOTH shapes so the file can be restored exactly as downloaded:
    #   { "data": {backup…} }        (what the web UI sends)
    #   { "watchlist": […], … }      (the backup file's own top-level object)
    data = body.get("data")
    if not isinstance(data, dict):
        data = body if "watchlist" in body else None
    if not isinstance(data, dict) or "watchlist" not in data:
        return jsonify({"ok": False, "error": "That does not look like a Dr. Shah's "
                        "backup file. Choose the drshah_backup.json you downloaded."}), 400
    try:
        if not isinstance(data.get("watchlist"), (list, tuple)):
            return jsonify({"ok": False, "error": "Invalid backup file — the watchlist "
                            "section is missing."}), 400
        tickers = _sanitize_tickers(data.get("watchlist", []))
        alerts_in = _sanitize_alerts(data.get("alerts", []))
        store.set_watchlist(uid, tickers)
        if uid:
            store.replace_alerts(uid, alerts_in)
            rw = data.get("rating_watch")
            if isinstance(rw, dict):
                store.set_pref(uid, "rating_watch", _clean_rating_watch(rw))
        # app settings are only restored by this computer / the owner account
        admin = _is_admin()
        if admin:
            store.save_config(_sanitize_settings(data.get("settings")))
        analysis.ANALYSIS_CACHE.clear()
        return jsonify({
            "ok": True,
            "message": f"Restored {len(tickers)} stocks, {len(alerts_in)} alert(s)"
                       + (" and settings." if admin else ". (App settings can only be restored "
                          "on the computer the app runs on.)"),
            "watchlist": tickers})
    except Exception as e:  # noqa: BLE001
        traceback.print_exc()
        return jsonify({"ok": False, "error": f"Restore failed: {str(e)[:150]}"}), 400


def _port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Is something already serving HTTP on this port?"""
    return instance.port_in_use(port, host)


def _banner(port: int, in_tray: bool = False) -> None:
    info = paths.describe()
    line = "=" * 66
    print(line)
    print(f"  {paths.APP_NAME}")
    print(f"  Running as : {info['mode']}")
    print(f"  Your data  : {info['data_folder']}")
    print(f"  Log file   : {applog.LOG_FILE}")
    print(f"  Address    : http://localhost:{port}")
    print(line)
    print("  The dashboard should open in your browser automatically.")
    print("  If it does not, type  http://localhost:%d  in your browser." % port)
    if in_tray:
        print("  Running in the system tray: click the icon to open the dashboard,")
        print("  right-click it -> Quit to stop the app.")
    else:
        print("  KEEP THIS WINDOW OPEN while you use the app.")
        print("  Close this window (or press Ctrl+C) to stop the app.")
    print(line)
    print()


def _use_tray() -> bool:
    """The packaged Windows app runs as a tray icon (no console window).
    DRSHAH_TRAY=1 / =0 forces it on / off (e.g. to try it from source)."""
    flag = os.environ.get("DRSHAH_TRAY", "").strip()
    wanted = (flag == "1") if flag in ("0", "1") else (paths.is_frozen() and sys.platform == "win32")
    return wanted and tray.available()


def _fatal(message: str) -> None:
    """Report a start-up failure where the user can see it — in the console,
    or as a Windows message box when the app has no console (tray mode)."""
    logging.getLogger("app").error(message.replace("\n", " "))
    if sys.__stdin__ is not None and sys.__stdout__ is not None:
        print("\n" + message)
        try:
            input("\nPress Enter to close…")
        except Exception:  # noqa: BLE001
            pass
    elif os.name == "nt":
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(None, message, paths.APP_NAME, 0x10)
        except Exception:  # noqa: BLE001
            pass
    raise SystemExit(1)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # Settings → "Allow other devices on my network": off = this computer only
    _net = store.load_config().get("network") or {}
    host = LISTEN_HOST = "0.0.0.0" if _net.get("lan_access", True) else "127.0.0.1"
    url = f"http://localhost:{port}"
    background = autostart.BACKGROUND_FLAG in sys.argv[1:]   # started with Windows

    # This copy holds the single-instance lock (taken at the very top), so
    # anything answering on the port is NOT another copy started the normal
    # way — e.g. an older version of this app. Show it rather than fight it.
    if _port_in_use(port):
        print(f"[server] A copy of Dr. Shah's app is ALREADY RUNNING on port {port}.")
        print(f"         Opening {url} in your browser…")
        print("         (Quit the other copy first if you want to restart it.)")
        if not background:
            _open_url(url)
        raise SystemExit(0)

    scheduler.start()        # this copy holds the lock and the port is free
    in_tray = _use_tray()
    _banner(port, in_tray)
    if not background:                       # at Windows login: no browser tab
        _maybe_open_browser(port)
    try:
        # Production WSGI server (Waitress) — multi-threaded, stable,
        # production-grade. Fall back to the Flask dev server only if
        # waitress is not installed.
        import waitress
    except ImportError:
        print("[server] Waitress not installed — using the Flask development "
              "server (fine for local use; `pip install waitress` for production).")
        app.run(host=host, port=port, debug=False, threaded=True)
        raise SystemExit(0)
    try:
        server = waitress.create_server(app, host=host, port=port, threads=12,
                                        channel_timeout=120,
                                        max_request_body_size=2 * 1024 * 1024)
    except OSError as e:
        _fatal(f"Could not start on port {port}: {e}\n\n"
               "Another program is using that port. Set a different one, "
               "e.g.  set PORT=5050, then start the app again.")
    try:
        from importlib.metadata import version as _pkg_version
        wver = " " + _pkg_version("waitress")
    except Exception:  # noqa: BLE001
        wver = ""
    print(f"[server] Waitress{wver} (production web server) listening on http://{host}:{port}"
          + ("" if host == "0.0.0.0" else "  (this computer only)"))

    if in_tray:
        threading.Thread(target=server.run, name="web-server", daemon=True).start()
        try:
            autostart.sync_path()            # folder moved? keep start-with-Windows working
        except Exception as e:  # noqa: BLE001
            logging.getLogger("app").warning("start-with-Windows check failed: %s", e)
        tray.run(open_dashboard=lambda: _open_url(url), on_quit=server.close,
                 welcome=not background)
        print("[server] Stopped from the tray icon. Goodbye!")
    else:
        try:
            server.run()
        except KeyboardInterrupt:
            print("\n[server] Stopped. Goodbye!")
