"""
Scheduler — background daemon threads:
  • background PRE-WARM: keeps analysis & market data fresh on a schedule
    (configurable interval) so the dashboard is always instantly up-to-date
  • periodic alert checks (every N minutes, configurable)
  • scheduled daily PDF reports (configurable time, optional email delivery)
"""
import glob
import json
import logging
import os
import smtplib
import threading
import time
from datetime import datetime, timedelta

import alerts
import backups
import paths
import analysis
import market
import pdfexport
import store

log = logging.getLogger("scheduler")
REPORTS_DIR = paths.REPORTS_DIR

_last_alert_check = 0.0
_stop = threading.Event()

# --- pre-warm state ----------------------------------------------------------
PREWARM_BOOT_DELAY_S = 30      # first refresh shortly after startup
PREWARM_MIN_GAP_S = 300        # never hammer Yahoo more often than 5 minutes
OPEN_REFRESH_MIN = market.OPEN_MIN + 15      # 9:45 ET — today's first bar exists
CLOSE_REFRESH_MIN = market.CLOSE_MIN + 20    # 4:20 PM ET — picks up the final close
_prewarm = {
    "enabled": True,
    "interval_minutes": 15,
    "market_hours_only": True,
    "last_run_ts": None,        # epoch
    "next_run_ts": None,        # epoch
    "last_force_ts": 0.0,       # epoch of last forced Yahoo refresh
    "running": False,
    "last_duration_s": None,
    "tickers_refreshed": 0,
    "last_error": None,
}


def prewarm_status() -> dict:
    """Snapshot of pre-warm state for the UI."""
    with _prewarm_lock():
        p = dict(_prewarm)
    p["last_run"] = datetime.fromtimestamp(p["last_run_ts"]).strftime("%d %b %Y, %I:%M %p") \
        if p["last_run_ts"] else "—"
    if p["next_run_ts"]:
        secs = max(0, int(p["next_run_ts"] - time.time()))
        if secs >= 90 * 60:      # hours away (market closed) → show the local time
            p["next_run"] = datetime.fromtimestamp(p["next_run_ts"]).strftime("%a %d %b, %I:%M %p")
        else:
            p["next_run"] = f"in ~{round(secs / 60, 1):g} min" if secs >= 60 else "now"
    else:
        p["next_run"] = "soon"
    return p


def next_auto_run(now_ts: float, interval_minutes: int,
                  market_hours_only: bool = True) -> float:
    """When the next automatic refresh is due (epoch seconds).

    Prices only change while the US market is open, so with
    `market_hours_only` the refresh runs every `interval_minutes` during the
    session, once more at 4:20 PM ET for the final close, and then waits for
    9:45 AM ET on the next trading day — instead of calling Yahoo for every
    stock around the clock.
    """
    step = now_ts + interval_minutes * 60
    if not market_hours_only:
        return step
    now_et = datetime.fromtimestamp(now_ts, market.ET)
    today = now_et.date()
    mins = now_et.hour * 60 + now_et.minute

    def at(day, minute):
        return datetime(day.year, day.month, day.day, minute // 60, minute % 60,
                        tzinfo=market.ET).timestamp()

    if market.is_trading_day(today):
        if market.OPEN_MIN <= mins < market.CLOSE_MIN:
            return step if step < at(today, market.CLOSE_MIN) else at(today, CLOSE_REFRESH_MIN)
        if mins < market.OPEN_MIN:
            return at(today, OPEN_REFRESH_MIN)
        if mins < CLOSE_REFRESH_MIN:
            return at(today, CLOSE_REFRESH_MIN)
    nxt = today + timedelta(days=1)
    while not market.is_trading_day(nxt):
        nxt += timedelta(days=1)
    return at(nxt, OPEN_REFRESH_MIN)


_prewarm_lock_obj = threading.Lock()


def _prewarm_lock():
    return _prewarm_lock_obj


def _collect_all_tickers() -> list[str]:
    """Every ticker across the guest watchlist and all user accounts."""
    tickers = set(store.load_guest_watchlist())
    import sqlite3
    conn = sqlite3.connect(store.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        users = conn.execute("SELECT id FROM users").fetchall()
    finally:
        conn.close()
    for u in users:
        tickers.update(store.get_watchlist(u["id"]))
    return sorted(tickers)


def _do_prewarm() -> None:
    try:
        t0 = time.time()
        tickers = _collect_all_tickers()
        refreshed, failed = 0, 0
        if not tickers:
            log.info("prewarm: no tickers to refresh")
        else:
            with _prewarm_lock():
                use_force = (time.time() - _prewarm["last_force_ts"]) >= PREWARM_MIN_GAP_S
            # force a fresh market fetch + recompute so both the data cache and
            # the shared analysis cache are warm for every user
            for t in tickers:
                try:
                    analysis.analyze_one(t, force=use_force)
                    refreshed += 1
                except Exception as e:  # noqa: BLE001
                    failed += 1
                    log.info("prewarm skip %s: %s", t, str(e)[:80])
            if use_force:
                with _prewarm_lock():
                    _prewarm["last_force_ts"] = time.time()
            with _prewarm_lock():
                _prewarm["last_duration_s"] = round(time.time() - t0, 1)
                _prewarm["tickers_refreshed"] = refreshed
                _prewarm["last_error"] = None if failed == 0 else f"{failed} ticker(s) skipped"
            log.info("prewarm done: %d/%d tickers in %.1fs",
                     refreshed, len(tickers), _prewarm["last_duration_s"])
    except Exception as e:  # noqa: BLE001
        log.error("prewarm failed: %s", e)
        with _prewarm_lock():
            _prewarm["last_error"] = str(e)[:200]
    finally:
        with _prewarm_lock():
            _prewarm["running"] = False
            _prewarm["last_run_ts"] = time.time()
            _prewarm["next_run_ts"] = next_auto_run(time.time(), _prewarm["interval_minutes"],
                                                    _prewarm["market_hours_only"])


def arm_prewarm(now_plus: float = 60, interval_minutes: int | None = None,
                market_hours_only: bool | None = None) -> None:
    """Schedule the next pre-warm run shortly from now (used after settings change)."""
    with _prewarm_lock():
        if interval_minutes:
            _prewarm["interval_minutes"] = interval_minutes
        if market_hours_only is not None:
            _prewarm["market_hours_only"] = bool(market_hours_only)
        _prewarm["next_run_ts"] = time.time() + now_plus


def run_prewarm_now() -> bool:
    """Kick off a pre-warm immediately (manual 'Refresh now' button).

    Starts the worker right away — only moving `next_run_ts` meant waiting for
    the next 30-second scheduler tick, after the UI had already said "done".
    Returns False when a pre-warm is already running.
    """
    with _prewarm_lock():
        if _prewarm["running"]:
            return False
        _prewarm["running"] = True   # claimed before the thread starts
    threading.Thread(target=_do_prewarm, name="prewarm", daemon=True).start()
    return True


def _maybe_prewarm() -> None:
    cfg = store.load_config()
    pw = cfg.get("prewarm") or {}
    enabled = pw.get("enabled", True)
    try:
        interval = max(5, min(int(pw.get("interval_minutes") or 15), 1440))
    except (TypeError, ValueError):
        interval = 15
    now = time.time()

    with _prewarm_lock():
        _prewarm["enabled"] = enabled
        _prewarm["interval_minutes"] = interval
        _prewarm["market_hours_only"] = bool(pw.get("market_hours_only", True))
        if _prewarm["next_run_ts"] is None:
            _prewarm["next_run_ts"] = now + PREWARM_BOOT_DELAY_S
        due = now >= _prewarm["next_run_ts"]
        running = _prewarm["running"]
        if due and running:
            _prewarm["next_run_ts"] = now + 60
        if not due or running or not enabled:
            return
        _prewarm["running"] = True   # claimed before the thread starts

    threading.Thread(target=_do_prewarm, name="prewarm", daemon=True).start()


def _tick():
    global _last_alert_check
    cfg = store.load_config()

    # --- automatic daily backup (backups/backup_YYYY-MM-DD.zip, last 7) ------
    try:
        backups.ensure_today()
    except Exception as e:  # noqa: BLE001
        log.error("daily backup failed: %s", e)

    # --- report deliveries that failed earlier (no network at the time) ------
    try:
        retry_pending_deliveries()
    except Exception as e:  # noqa: BLE001
        log.error("delivery retry failed: %s", e)

    # --- background pre-warm (auto-refresh) ----------------------------------
    try:
        _maybe_prewarm()
    except Exception as e:  # noqa: BLE001
        log.error("prewarm tick error: %s", e)

    # --- scheduled daily reports -------------------------------------------
    sched = cfg.get("schedule") or {}
    if sched.get("enabled"):
        now = datetime.now()
        today = now.strftime("%Y-%m-%d")
        # Catch-up logic: run as soon as we are AT or PAST the scheduled time and
        # no report has been produced today. Matching the exact minute (the old
        # behaviour) meant a report was silently skipped for the whole day when
        # the PC was asleep/off at that minute, or the scheduler was busy.
        hh, mm = 18, 0
        try:
            parts = str(sched.get("time") or "18:00").split(":")
            hh, mm = int(parts[0]), int(parts[1])
        except Exception:  # noqa: BLE001 — a bad setting must not stop the loop
            pass
        if sched.get("last_run_date") != today and (now.hour, now.minute) >= (hh, mm):
            try:
                log.info("daily report starting (scheduled %02d:%02d, now %02d:%02d)",
                         hh, mm, now.hour, now.minute)
                result = run_daily_reports() or {}
                # reload first: the reports can take minutes, and saving the
                # copy loaded before them would undo any settings the user
                # saved in the meantime
                fresh = store.load_config()
                fresh.setdefault("schedule", {})["last_run_date"] = today
                fresh["schedule"]["last_result"] = describe_run(result, fresh, now)
                store.save_config(fresh)
            except Exception as e:  # noqa: BLE001
                log.error("daily report failed: %s", e)

    # --- periodic alert checks ----------------------------------------------
    minutes = int(cfg.get("alert_check_minutes") or 10)
    if time.time() - _last_alert_check >= minutes * 60:
        _last_alert_check = time.time()
        try:
            fired = alerts.check_active_alerts()
            if fired:
                log.info("background alert check: %d alert(s) fired", fired)
        except Exception as e:  # noqa: BLE001
            log.error("alert check failed: %s", e)


def _loop():
    log.info("scheduler started")
    while not _stop.wait(30):
        try:
            _tick()
        except Exception as e:  # noqa: BLE001
            log.error("scheduler tick error: %s", e)


def start():
    threading.Thread(target=_loop, name="scheduler", daemon=True).start()


# ---------------------------------------------------------------------------
# Daily reports
# ---------------------------------------------------------------------------

def _email_pdf(cfg, to_addr: str, path: str) -> None:
    """Email the report PDF using the same resilient delivery path as alerts
    (automatic port 587 ⇄ 465 fallback and plain-English error messages)."""
    with open(path, "rb") as f:
        payload = f.read()
    alerts.send_email(
        cfg["email"],
        f"📊 Dr. Shah's Daily Analysis Report — {datetime.now():%d %b %Y}",
        "Attached is your daily technical analysis report from Dr. Shah's "
        "US Stocks Analysis.\n\n— Automated report",
        to_addr,
        attachment=(os.path.basename(path), payload, "pdf"),
    )


def _report_caption(payload: dict, who: str) -> str:
    """Short HTML summary caption that accompanies the PDF on Telegram."""
    res = payload.get("results") or {}
    errs = payload.get("errors") or {}
    try:
        from collections import Counter
        rc = Counter(r["rating"] for r in res.values())
        avg = sum(r["score"] for r in res.values()) / len(res) if res else 0
        top = max(res.values(), key=lambda r: r["score"]) if res else None
        gainers = sorted(res.values(), key=lambda r: r["change_pct"], reverse=True)[:2]
        losers = sorted(res.values(), key=lambda r: r["change_pct"])[:2]
    except Exception:  # noqa: BLE001
        return f"📊 Daily Analysis Report — {who}"
    lines = [
        f"<b>📊 Daily Analysis Report — {who}</b>",
        f"📅 {datetime.now():%d %b %Y} · {len(res)} stock(s)",
        f"<b>Average score: {avg:.1f}/100</b>",
        # best rating first (it used to be in whatever order the stocks came)
        " · ".join(f"{k}: {rc[k]}" for k in ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell")
                   if rc.get(k)),
    ]
    if top:
        lines.append(f"🏆 Top pick: <b>{top['ticker']} {top['score']:.1f}</b> ({top['rating']})")
    if gainers:
        lines.append("📈 " + " · ".join(f"{r['ticker']} {r['change_pct']:+.1f}%" for r in gainers))
    if losers:
        lines.append("📉 " + " · ".join(f"{r['ticker']} {r['change_pct']:+.1f}%" for r in losers))
    if errs:
        lines.append(f"⚠️ Skipped: {', '.join(errs)}")
    lines.append("📄 Full PDF attached 👇")
    return "\n".join(lines)


def delivery_problems(cfg: dict, only_if_scheduled: bool = True) -> list[str]:
    """Why the scheduled report would NOT reach the user, in plain words
    (empty when everything needed is set, or delivery is not requested).
    `only_if_scheduled=False` checks the channels even when the daily
    schedule is off — "Generate reports now" delivers too."""
    sched = cfg.get("schedule") or {}
    em = cfg.get("email") or {}
    tg = cfg.get("telegram") or {}
    if only_if_scheduled and not sched.get("enabled"):
        return []
    out = []
    if sched.get("email_delivery"):
        missing = []
        if not em.get("enabled"):
            missing.append("the email channel is switched off")
        if not str(em.get("smtp_host") or "").strip():
            missing.append("no SMTP host")
        if not str(em.get("smtp_user") or "").strip():
            missing.append("no username")
        if not str(em.get("smtp_pass") or "").strip("•").strip():
            missing.append("no password")
        if not (em.get("to_addr") or em.get("from_addr")):
            missing.append("no 'To' address (only accounts with an email address would get theirs)")
        if missing:
            out.append("Reports will NOT be emailed: " + ", ".join(missing) + ".")
    if sched.get("telegram_delivery"):
        missing = []
        if not tg.get("enabled"):
            missing.append("the Telegram channel is switched off")
        token = str(tg.get("bot_token") or "").strip()
        if not token:
            missing.append("no bot token")
        elif ":" not in token:
            missing.append("the bot token is incomplete (it must look like 123456789:AA…, "
                           "copied whole from @BotFather)")
        if not str(tg.get("chat_id") or "").strip():
            missing.append("no chat ID")
        if missing:
            out.append("Reports will NOT be sent to Telegram: " + ", ".join(missing) + ".")
    return out


def delivery_working(cfg: dict, only_if_scheduled: bool = True) -> list[str]:
    """The requested delivery channels that ARE fully set up ("email",
    "telegram") — so the warning can say "email is not set up" instead of
    "the report won't be delivered" when Telegram works fine."""
    sched = cfg.get("schedule") or {}
    if only_if_scheduled and not sched.get("enabled"):
        return []
    problems = " ".join(delivery_problems(cfg, only_if_scheduled))
    out = []
    if sched.get("email_delivery") and "NOT be emailed" not in problems:
        out.append("email")
    if sched.get("telegram_delivery") and "NOT be sent to Telegram" not in problems:
        out.append("telegram")
    return out


def _deliver_report(cfg, sched, who: str, path: str, payload: dict,
                    user_email: str = "") -> dict:
    """Send the generated report PDF via email and/or Telegram (Option C).
    Returns what happened: {"email": True/False/None, "telegram": …}
    (None = not requested or not possible with the current settings)."""
    done = {"email": None, "telegram": None}
    # a channel that is not fully set up is not tried at all
    working = delivery_working(cfg, only_if_scheduled=False)
    caption = _report_caption(payload, who)
    retry = []                           # channels that failed for a passing reason
    to_addr = cfg["email"].get("to_addr") or user_email or ""
    if "email" in working and to_addr:
        try:
            _email_pdf(cfg, to_addr, path)
            done["email"] = True
            log.info("report emailed to %s", to_addr)
        except Exception as e:  # noqa: BLE001
            done["email"] = False
            log.error("report email failed for %s: %s", who, e)
            # a wrong password or refused address will not fix itself
            if not isinstance(e.__cause__, (smtplib.SMTPAuthenticationError,
                                            smtplib.SMTPRecipientsRefused)):
                retry.append("email")
    if "telegram" in working:
        try:
            with open(path, "rb") as f:
                doc = f.read()
            alerts.send_telegram_document(cfg["telegram"], doc, os.path.basename(path), caption)
            done["telegram"] = True
            log.info("report sent to Telegram (%s)", who)
        except ValueError as e:          # Telegram itself said no (token / chat ID)
            done["telegram"] = False
            log.error("report telegram failed for %s: %s", who, e)
        except Exception as e:  # noqa: BLE001 — network: keep trying later
            done["telegram"] = False
            log.error("report telegram failed for %s: %s — will retry", who, e)
            retry.append("telegram")
    if retry:
        _queue_retry(who, path, caption, to_addr, retry)
    return done


# ---------------------------------------------------------------------------
# Deliveries that failed for a passing reason are tried again
# ---------------------------------------------------------------------------
# (On 4 Oct 2026 the report ran 40 s after the PC started, the Wi-Fi was not
#  up yet, Telegram failed once — and that day's report was simply never sent.)

RETRY_EVERY_S = 300            # try again every 5 minutes …
RETRY_FOR_S = 12 * 3600        # … for up to 12 hours
_pending_lock = threading.Lock()


def _pending_path() -> str:
    return os.path.join(REPORTS_DIR, "pending_delivery.json")


def _load_pending() -> list[dict]:
    try:
        with open(_pending_path(), encoding="utf-8") as f:
            items = json.load(f)
        return [i for i in items if isinstance(i, dict)] if isinstance(items, list) else []
    except (OSError, ValueError):
        return []


def _save_pending(items: list[dict]) -> None:
    try:
        if not items:
            if os.path.exists(_pending_path()):
                os.remove(_pending_path())
            return
        os.makedirs(REPORTS_DIR, exist_ok=True)
        with open(_pending_path(), "w", encoding="utf-8") as f:
            json.dump(items, f, indent=1)
    except OSError as e:
        log.error("could not save the delivery retry list: %s", e)


def _queue_retry(who: str, path: str, caption: str, email_to: str, channels: list[str]) -> None:
    with _pending_lock:
        items = [i for i in _load_pending() if i.get("path") != path]
        items.append({"who": who, "path": path, "caption": caption, "email_to": email_to,
                      "channels": list(channels), "created": time.time(),
                      "next_try": time.time() + RETRY_EVERY_S, "tries": 0})
        _save_pending(items)


def retry_pending_deliveries(now: float | None = None) -> int:
    """Called on every scheduler tick; returns how many deliveries succeeded."""
    now = time.time() if now is None else now
    with _pending_lock:
        items = _load_pending()
        if not items:
            return 0
        cfg = store.load_config()
        keep, delivered = [], []
        for it in items:
            if now - float(it.get("created") or 0) > RETRY_FOR_S or not os.path.exists(it.get("path", "")):
                log.warning("giving up on delivering the report for %s", it.get("who"))
                continue
            if now < float(it.get("next_try") or 0):
                keep.append(it)
                continue
            left = []
            for ch in it.get("channels") or []:
                try:
                    if ch == "telegram":
                        with open(it["path"], "rb") as f:
                            doc = f.read()
                        alerts.send_telegram_document(cfg["telegram"], doc,
                                                      os.path.basename(it["path"]),
                                                      it.get("caption") or "")
                    elif ch == "email":
                        _email_pdf(cfg, it.get("email_to") or "", it["path"])
                    delivered.append(ch)
                    log.info("report for %s delivered by %s on retry %d",
                             it.get("who"), ch, int(it.get("tries") or 0) + 1)
                except ValueError as e:
                    log.error("report retry (%s) refused: %s", ch, e)      # settings problem
                except Exception as e:  # noqa: BLE001
                    left.append(ch)
                    log.warning("report retry (%s) for %s failed: %s", ch, it.get("who"), e)
            if left:
                it.update(channels=left, tries=int(it.get("tries") or 0) + 1,
                          next_try=now + RETRY_EVERY_S)
                keep.append(it)
        _save_pending(keep)
    if delivered:
        try:                              # show it in Settings → "Last run"
            fresh = store.load_config()
            sched = fresh.setdefault("schedule", {})
            names = " and ".join(sorted({"Telegram" if c == "telegram" else c for c in delivered}))
            sched["last_result"] = (str(sched.get("last_result") or "").rstrip()
                                    + f" · delivered by {names} on a later try at "
                                    + datetime.now().strftime("%I:%M %p"))
            store.save_config(fresh)
        except Exception as e:  # noqa: BLE001
            log.error("could not record the late delivery: %s", e)
    return len(delivered)


def describe_run(result: dict, cfg: dict, when: datetime | None = None) -> str:
    """One line for Settings: what the last daily report run achieved."""
    when = when or datetime.now()
    sched = cfg.get("schedule") or {}
    count = int(result.get("count") or 0)
    due = int(result.get("to_deliver") or count)
    parts = [f"{when:%d %b %Y, %I:%M %p} — {count} report(s) saved"]
    if sched.get("email_delivery"):
        parts.append(f"emailed {result.get('emailed', 0)} of {due}")
    if sched.get("telegram_delivery"):
        parts.append(f"sent to Telegram {result.get('telegram', 0)} of {due}")
    line = "; ".join(parts)
    problems = delivery_problems(cfg)
    return line + (" — " + " ".join(problems) if problems else "")


def run_daily_reports() -> dict:
    """Generate a dated report PDF for every user + guest; deliver via
    email &/or Telegram when enabled; prune old files."""
    os.makedirs(REPORTS_DIR, exist_ok=True)
    cfg = store.load_config()
    sched = cfg.get("schedule") or {}
    stamp = datetime.now().strftime("%Y%m%d_%H%M")
    generated = []
    sent = {"email": 0, "telegram": 0}
    for problem in delivery_problems(cfg):
        log.warning("daily report: %s", problem)

    def _count(done: dict) -> None:
        for k in sent:
            if done.get(k):
                sent[k] += 1

    # registered users
    import sqlite3
    conn = sqlite3.connect(store.DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    try:
        users = conn.execute("SELECT id, username, email FROM users").fetchall()
    finally:
        conn.close()

    for u in users:
        try:
            wl = store.get_watchlist(u["id"])
            payload = analysis.analyze_watchlist(wl)
            data = pdfexport.build_report_pdf(payload["results"], payload["errors"])
            path = os.path.join(REPORTS_DIR, f"report_{stamp}_{u['username']}.pdf")
            with open(path, "wb") as f:
                f.write(data)
            generated.append(path)
            _count(_deliver_report(cfg, sched, u["username"], path, payload, u["email"] or ""))
        except Exception as e:  # noqa: BLE001
            log.error("report failed for %s: %s", u["username"], e)

    # guest watchlist
    try:
        wl = store.load_guest_watchlist()
        payload = analysis.analyze_watchlist(wl)
        data = pdfexport.build_report_pdf(payload["results"], payload["errors"])
        path = os.path.join(REPORTS_DIR, f"report_{stamp}_guest.pdf")
        with open(path, "wb") as f:
            f.write(data)
        generated.append(path)
        # The guest watchlist is only DELIVERED when nobody has an account —
        # otherwise every account holder would also get a second PDF each day
        # for a list that is not theirs. It is always saved in reports/.
        if not users:
            _count(_deliver_report(cfg, sched, "Guest Watchlist", path, payload))
        else:
            log.info("guest report saved (not delivered: accounts exist)")
    except Exception as e:  # noqa: BLE001
        log.error("guest report failed: %s", e)

    # prune old reports
    # at least 1 day: a 0/negative value (e.g. from an edited backup) would put
    # the cutoff in the future and delete the reports just generated
    try:
        keep_days = max(1, int(sched.get("keep_days") or 30))
    except (TypeError, ValueError):
        keep_days = 30
    cutoff = time.time() - keep_days * 86400
    for old in glob.glob(os.path.join(REPORTS_DIR, "report_*.pdf")):
        try:
            if os.path.getmtime(old) < cutoff:
                os.remove(old)
        except OSError:
            pass

    return {"generated": generated, "count": len(generated),
            "to_deliver": len(users) or 1,       # reports meant to be sent
            "emailed": sent["email"], "telegram": sent["telegram"]}
