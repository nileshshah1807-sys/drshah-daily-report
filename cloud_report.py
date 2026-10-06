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
  python cloud_report.py --again     deliver it even if today's report has already gone out
  python cloud_report.py --plan      only decide whether this run has anything to do

GitHub starts scheduled runs late, and some days not at all, so the workflow
tries several times a day. Each channel the report reaches is recorded for the
day (.sent/channels, carried between runs by the workflow), and a later try
sends only to the channels still missing: one report a day per channel.
"""
import json
import logging
import os
import sqlite3
import sys
import time
from datetime import datetime, timedelta

import paths

NO_SEND = "--no-send" in sys.argv[1:]
AGAIN = "--again" in sys.argv[1:]
PLAN = "--plan" in sys.argv[1:]
MIN_SHARE_ANALYSED = 0.5        # fewer stocks than this: the data source is failing
BACKFILL_WAIT_S = 6 * 60        # score-history rebuild on a fresh cache
DELIVERY_RETRIES = 4
DELIVERY_RETRY_PAUSE_S = 45
SEND_AT = (9, 0)                # the report's time of day; the workflow sets the time zone (India)
BUILD_LEAD_S = 2 * 60           # installing and building take about this long
MAX_WAIT_S = 15 * 60            # the longest a run that started early waits for SEND_AT
CHANNEL_SECRET = {"email": "SMTP_PASS", "telegram": "TELEGRAM_BOT_TOKEN"}
SENT_MARK = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".sent", "channels")
NOT_SET_UP = ("Not set up yet: no email or Telegram password has been added to this repository's "
              "secrets, so there is nothing to deliver with. Add SMTP_USER, SMTP_PASS, EMAIL_TO, "
              "TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID under Settings > Secrets and variables > Actions.")

log = logging.getLogger("cloud")


def _env(name: str, default: str = "") -> str:
    return (os.environ.get(name) or default).strip()


def configured() -> list[str]:
    """The channels whose password is in the repository's secrets."""
    return [ch for ch, secret in CHANNEL_SECRET.items() if _env(secret)]


def sent_today() -> set[str]:
    """The channels today's report has already reached, as recorded by an earlier run."""
    try:
        with open(SENT_MARK, encoding="utf-8") as f:
            mark = json.load(f)
        if mark.get("date") == f"{datetime.now():%Y-%m-%d}":
            return {str(ch) for ch in mark.get("channels") or []}
    except (OSError, ValueError, AttributeError):
        pass
    return set()


def record_sent(channels: set[str]) -> None:
    os.makedirs(os.path.dirname(SENT_MARK), exist_ok=True)
    with open(SENT_MARK, "w", encoding="utf-8") as f:
        json.dump({"date": f"{datetime.now():%Y-%m-%d}", "channels": sorted(channels)}, f)


def channels_due() -> list[str]:
    """The set-up channels this run should deliver to."""
    if NO_SEND:
        return []
    have = configured()
    return have if AGAIN else [ch for ch in have if ch not in sent_today()]


def output(**values) -> None:
    """Hand values to the later steps of the workflow."""
    target = os.environ.get("GITHUB_OUTPUT")
    if target:
        with open(target, "a", encoding="utf-8") as f:
            f.writelines(f"{k}={v}\n" for k, v in values.items())


def write_config(due: list[str]) -> None:
    """config.json for this run, in the shape the app's own settings use.
    Only the channels in `due` are switched on."""
    smtp_user = _env("SMTP_USER")
    email, telegram = "email" in due, "telegram" in due
    cfg = {
        "secret_key": "cloud-run",       # only the web app uses it; set so nothing is generated
        "email": {
            "enabled": email,
            "smtp_host": _env("SMTP_HOST", "smtp.gmail.com"),
            "smtp_port": int(_env("SMTP_PORT", "587")),
            "smtp_user": smtp_user,
            "smtp_pass": _env("SMTP_PASS"),
            "from_addr": _env("EMAIL_FROM", smtp_user),
            "to_addr": _env("EMAIL_TO"),
        },
        "telegram": {
            "enabled": telegram,
            "bot_token": _env("TELEGRAM_BOT_TOKEN"),
            "chat_id": _env("TELEGRAM_CHAT_ID"),
        },
        # a channel with no password is "not set up", not a failed delivery
        "schedule": {"enabled": True, "time": "09:00", "email_delivery": email,
                     "telegram_delivery": telegram, "keep_days": 30},
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


def nothing_to_do(have: list[str], due: list[str]) -> bool:
    """True, with the reason logged, when this run has no report to deliver."""
    if NO_SEND:
        return False
    # Until the repository's secrets are added there is nothing to deliver with.
    # That is "not set up yet", not a failure: say so and stop, instead of
    # failing (and emailing a failure notice) every morning.
    if not have:
        log.warning(NOT_SET_UP)
        summary(["### Daily report: not set up yet", NOT_SET_UP])
        return True
    if not due:
        msg = f"Today's report has already been sent ({', '.join(have)}). Nothing to do."
        log.info(msg)
        summary(["### Daily report: already sent today", msg])
        return True
    return False


def plan() -> int:
    """The workflow's first step, before anything is installed: is there a report
    to send, and should this run wait for 09:00 first? Every try after the one
    that delivered stops here, within seconds."""
    have, due = configured(), channels_due()
    run, wait = not nothing_to_do(have, due), 0
    if NO_SEND:
        log.info("test run: the report will be built but not sent")
    elif run:
        log.info("to send: %s", ", ".join(due))
        if os.environ.get("GITHUB_EVENT_NAME") == "schedule":
            now = datetime.now()
            start = (now.replace(hour=SEND_AT[0], minute=SEND_AT[1], second=0, microsecond=0)
                     - timedelta(seconds=BUILD_LEAD_S))
            wait = int(min(MAX_WAIT_S, max(0.0, (start - now).total_seconds())))
            if wait:
                log.info("started early: waiting %d min %02d s so the report goes out at %02d:%02d",
                         wait // 60, wait % 60, *SEND_AT)
    output(run=str(run).lower(), wait=wait)
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s  %(levelname)-7s %(name)s: %(message)s",
                        datefmt="%H:%M:%S")
    for noisy in ("urllib3", "peewee", "yfinance"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    if PLAN:
        return plan()
    have, due = configured(), channels_due()
    if nothing_to_do(have, due):
        return 0

    write_config(due)
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

    outcome, failed, reached = [], bool(problems), set()
    for ch in CHANNEL_SECRET:
        if ch not in have:
            outcome.append(f"- {ch}: not set up")
        elif ch not in due:
            outcome.append(f"- {ch}: already sent earlier today")
        elif ch not in wanted:
            outcome.append(f"- {ch}: FAILED (its settings are incomplete)")
        else:
            ok = done.get(ch) is True or (ch in queued and ch not in still_failing)
            outcome.append(f"- {ch}: {'sent' if ok else 'FAILED'}")
            failed = failed or not ok
            if ok:
                reached.add(ch)
    for line in outcome:
        log.info(line)
    if reached:
        # so that today's later tries do not send it to these channels again
        record_sent(sent_today() | reached)
        output(recorded="true")
    summary([f"### Daily report: {'delivery problem' if failed else 'sent'}",
             f"{len(results)} of {len(tickers)} stocks analysed.", *outcome,
             *(f"- skipped {t}: {why}" for t, why in errors.items())])
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
