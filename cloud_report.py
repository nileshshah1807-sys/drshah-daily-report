"""
Cloud run of the daily report (GitHub Actions).

Builds the same PDF the desktop app builds at 09:00 and sends it by email and
Telegram, using the app's own analysis, PDF and delivery code. It runs once and
exits; nothing is left running.

The watchlist comes from cloud_watchlist.json. Passwords come from environment
variables (GitHub Secrets), never from a file in the repository:

  SMTP_USER, SMTP_PASS, EMAIL_TO            email
  SMTP_HOST, SMTP_PORT, EMAIL_FROM          optional (smtp.gmail.com, 587, SMTP_USER)
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID      Telegram

  python cloud_report.py             build the report and deliver it
  python cloud_report.py --no-send   build the PDF only (test)
"""
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime

import paths

NO_SEND = "--no-send" in sys.argv[1:]
MIN_SHARE_ANALYSED = 0.5        # fewer stocks than this: the data source is failing
BACKFILL_WAIT_S = 6 * 60        # score-history rebuild on a fresh cache
DELIVERY_RETRIES = 4
DELIVERY_RETRY_PAUSE_S = 45

log = logging.getLogger("cloud")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def write_config() -> None:
    """config.json for this run, in the shape the app's own settings use."""
    smtp_user = _env("SMTP_USER")
    send = not NO_SEND
    cfg = {
        "secret_key": "cloud-run",       # only the web app uses it; set so nothing is generated
        "email": {
            "enabled": send and bool(_env("SMTP_PASS")),
            "smtp_host": _env("SMTP_HOST", "smtp.gmail.com"),
            "smtp_port": int(_env("SMTP_PORT", "587")),
            "smtp_user": smtp_user,
            "smtp_pass": _env("SMTP_PASS"),
            "from_addr": _env("EMAIL_FROM", smtp_user),
            "to_addr": _env("EMAIL_TO"),
        },
        "telegram": {
            "enabled": send and bool(_env("TELEGRAM_BOT_TOKEN")),
            "bot_token": _env("TELEGRAM_BOT_TOKEN"),
            "chat_id": _env("TELEGRAM_CHAT_ID"),
        },
        "schedule": {"enabled": True, "time": "09:00", "email_delivery": send,
                     "telegram_delivery": send, "keep_days": 30},
        "prewarm": {"enabled": False, "interval_minutes": 15, "market_hours_only": True},
    }
    with open(paths.CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def load_watchlist() -> tuple[str, list[str]]:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "cloud_watchlist.json"), encoding="utf-8") as f:
        wl = json.load(f)
    tickers = [str(t).strip().upper() for t in wl.get("tickers") or [] if str(t).strip()]
    return str(wl.get("account") or "Watchlist"), tickers


def analyse(analysis, tickers: list[str]) -> dict:
    def progress(done, total, ticker):
        if done % 5 == 0 or done == total:
            log.info("analysed %d of %d", done, total)

    payload = analysis.analyze_watchlist(tickers, progress=progress)
    # Yahoo sometimes refuses a few requests in a burst: try those once more, slowly.
    if payload["errors"]:
        failed = list(payload["errors"])
        log.warning("%d stock(s) failed, trying them again: %s", len(failed), ", ".join(failed))
        time.sleep(20)
        for t in failed:
            try:
                payload["results"][t] = analysis.analyze_one(t, force=True)
                payload["errors"].pop(t, None)
            except Exception as e:  # noqa: BLE001
                payload["errors"][t] = str(e)[:200]
            time.sleep(2)
    return payload


def wait_for_score_history(analysis) -> None:
    """On a fresh cache the score trend is rebuilt in the background; wait for it
    so the PDF shows the same trend lines as the desktop report."""
    pending = getattr(analysis, "_backfill_pending", None)
    if pending is None:
        return
    deadline = time.time() + BACKFILL_WAIT_S
    while pending and time.time() < deadline:
        time.sleep(3)
    if pending:
        log.warning("score history still rebuilding for %d stock(s); continuing", len(pending))


def checkpoint_cache() -> None:
    """Fold the SQLite write-ahead log into data_cache.db so the saved cache is complete."""
    try:
        conn = sqlite3.connect(paths.CACHE_DB_PATH, timeout=20)
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()
    except Exception as e:  # noqa: BLE001 — the cache is an optimisation only
        log.warning("cache checkpoint skipped: %s", e)


def summary(lines: list[str]) -> None:
    """Show the outcome on the run's page in GitHub."""
    target = os.environ.get("GITHUB_STEP_SUMMARY")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("urllib3", "peewee", "yfinance"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    write_config()
    # imported after config.json exists: these modules read it
    import alerts
    import analysis
    import pdfexport
    import scheduler
    import store

    account, tickers = load_watchlist()
    if not tickers:
        log.error("cloud_watchlist.json has no tickers")
        return 2
    log.info("daily report for %s: %d stock(s), %s", account, len(tickers),
             datetime.now().strftime("%d %b %Y %H:%M %Z").strip())

    payload = analyse(analysis, tickers)
    results, errors = payload["results"], payload["errors"]
    log.info("analysed %d, failed %d", len(results), len(errors))
    for t, why in errors.items():
        log.warning("  %s: %s", t, why)

    cfg = store.load_config()
    if len(results) < max(1, len(tickers) * MIN_SHARE_ANALYSED):
        msg = (f"Cloud daily report was NOT sent: market data could be loaded for only "
               f"{len(results)} of {len(tickers)} stocks. The data source may be refusing "
               f"requests from the cloud server.")
        log.error(msg)
        summary(["### Daily report: not sent", msg])
        if not NO_SEND and cfg["telegram"].get("enabled"):
            try:
                alerts.send_telegram(cfg["telegram"], "⚠️ " + msg)
            except Exception as e:  # noqa: BLE001
                log.error("could not send the failure notice: %s", e)
        checkpoint_cache()
        return 2

    wait_for_score_history(analysis)

    os.makedirs(paths.REPORTS_DIR, exist_ok=True)
    path = os.path.join(paths.REPORTS_DIR, f"report_{datetime.now():%Y%m%d_%H%M}_{account}.pdf")
    with open(path, "wb") as f:
        f.write(pdfexport.build_report_pdf(results, errors))
    log.info("report saved: %s (%d KB)", os.path.basename(path), os.path.getsize(path) // 1024)
    checkpoint_cache()

    if NO_SEND:
        summary(["### Daily report: built, not sent (test)",
                 f"{len(results)} of {len(tickers)} stocks analysed."])
        return 0

    problems = scheduler.delivery_problems(cfg, only_if_scheduled=False)
    for p in problems:
        log.error(p)
    wanted = scheduler.delivery_working(cfg, only_if_scheduled=False)

    done = scheduler._deliver_report(cfg, cfg["schedule"], account, path, payload)
    # channels whose first try failed for a passing reason and were queued for retry
    queued = {ch for it in scheduler._load_pending() for ch in it.get("channels") or []}
    # The app retries a failed delivery every 5 minutes; this run is short-lived,
    # so make those retries now.
    for _ in range(DELIVERY_RETRIES):
        if not scheduler._load_pending():
            break
        time.sleep(DELIVERY_RETRY_PAUSE_S)
        scheduler.retry_pending_deliveries(now=time.time() + scheduler.RETRY_EVERY_S + 1)
    still_failing = {ch for it in scheduler._load_pending() for ch in it.get("channels") or []}

    outcome, failed = [], bool(problems)
    for ch in ("email", "telegram"):
        if ch not in wanted:
            outcome.append(f"- {ch}: not set up")
            continue
        ok = done.get(ch) is True or (ch in queued and ch not in still_failing)
        outcome.append(f"- {ch}: {'sent' if ok else 'FAILED'}")
        failed = failed or not ok
    for line in outcome:
        log.info(line)
    summary([f"### Daily report: {'delivery problem' if failed else 'sent'}",
             f"{len(results)} of {len(tickers)} stocks analysed.", *outcome,
             *(f"- skipped {t}: {why}" for t, why in errors.items())])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
