"""
Alert engine — evaluates user alerts against analysis results and delivers
notifications through the configured channels (in-app, email, Telegram).

Alert kinds:
  price_above / price_below   : price crosses a target level
  rsi_above  / rsi_below      : RSI(14) crosses a level
  score_above/ score_below    : technical score crosses a level
  rating_is                   : rating becomes a specific value
  breakout / breakdown        : price breaks nearest resistance / support
"""
import json
import logging
import re
import smtplib
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

import store

log = logging.getLogger("alerts")

KIND_LABELS = {
    "price_above": "Price ≥ target",
    "price_below": "Price ≤ target",
    "rsi_above": "RSI ≥ target",
    "rsi_below": "RSI ≤ target",
    "score_above": "Score ≥ target",
    "score_below": "Score ≤ target",
    "rating_is": "Rating is…",
    "rating_change": "Rating changes (up or down)",
    "breakout": "New 1-month high (breakout)",
    "breakdown": "New 1-month low (breakdown)",
}


def condition_text(alert: dict) -> str:
    v = alert.get("value") or ""
    label = KIND_LABELS.get(alert["kind"], alert["kind"])
    if alert["kind"] in ("breakout", "breakdown", "rating_change"):
        return label
    if alert["kind"] == "rating_is":
        return f"Rating = {v}"
    unit = "%" if alert["kind"].startswith(("rsi", "score")) else "$"
    return f"{label.replace(' target', '')} {v}{unit}"


def _num(x):
    """float(x) or None — never raises, and maps NaN to None."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if f != f else f


def evaluate(alert: dict, r: dict) -> tuple[bool, str]:
    """Returns (triggered, human-readable description). NEVER raises.

    A single malformed alert (missing target value) or a market-data gap
    (e.g. no resistance level above an all-time high) must not break the
    caller — it would otherwise stop every other alert from being checked, or
    return HTTP 500 for the whole /api/analyze request.
    """
    try:
        return _evaluate(alert, r)
    except Exception as e:  # noqa: BLE001
        log.error("alert evaluation failed (kind=%s id=%s): %s",
                  alert.get("kind"), alert.get("id"), e)
        return False, f"Could not evaluate this alert ({type(e).__name__})."


def _rating_rank(rating: str) -> int:
    return {"Strong Buy": 5, "Buy": 4, "Hold": 3, "Sell": 2, "Strong Sell": 1}.get(rating, 0)


def _evaluate(alert: dict, r: dict) -> tuple[bool, str]:
    ticker = str(r.get("ticker") or alert.get("ticker") or "?")
    price = _num(r.get("price"))
    ind = r.get("indicators") or {}
    sr = r.get("support_resistance") or {}
    lv = r.get("levels") or {}
    kind = alert.get("kind")
    val = alert.get("value")
    fval = _num(val)

    if kind in ("price_above", "price_below", "rsi_above", "rsi_below",
                "score_above", "score_below") and fval is None:
        return False, f"{ticker}: this alert has no target value set."
    if kind in ("price_above", "price_below") and price is None:
        return False, f"{ticker}: no price available yet."

    if kind == "price_above":
        return price >= fval, f"{ticker} price ${price:,.2f} reached ${fval:,.2f}"
    if kind == "price_below":
        return price <= fval, f"{ticker} price ${price:,.2f} fell to ${fval:,.2f}"

    if kind == "rsi_above":
        rsi = _num(ind.get("rsi"))
        if rsi is None:
            return False, f"{ticker}: RSI not available yet."
        return rsi >= fval, f"{ticker} RSI(14) {rsi:.1f} rose to {fval:g}"
    if kind == "rsi_below":
        rsi = _num(ind.get("rsi"))
        if rsi is None:
            return False, f"{ticker}: RSI not available yet."
        return rsi <= fval, f"{ticker} RSI(14) {rsi:.1f} fell to {fval:g}"

    if kind == "score_above":
        score = _num(r.get("score"))
        if score is None:
            return False, f"{ticker}: score not available yet."
        return score >= fval, f"{ticker} score {score:.1f} rose to {fval:g}"
    if kind == "score_below":
        score = _num(r.get("score"))
        if score is None:
            return False, f"{ticker}: score not available yet."
        return score <= fval, f"{ticker} score {score:.1f} fell to {fval:g}"

    if kind == "rating_is":
        return r.get("rating") == val, f"{ticker} rating is now {r.get('rating')}"

    if kind == "rating_change":
        # compared with the previous session's CLOSING rating (score history)
        prev = (r.get("score_trend") or {}).get("prev_rating")
        now_r = r.get("rating")
        if not prev or not now_r:
            return False, f"{ticker}: no earlier rating to compare with yet."
        direction = ("upgraded" if _rating_rank(now_r) > _rating_rank(prev)
                     else "downgraded")
        return prev != now_r, f"{ticker} {direction}: {prev} → {now_r}"

    # ---- breakout / breakdown -------------------------------------------------
    # A level ABOVE the price can never be "crossed" (that is why the old
    # resistance test never fired), so we compare against the highest high /
    # lowest low of the PREVIOUS 20 sessions — a genuine 1-month breakout —
    # and also accept a new 52-week high. Older snapshots without the 'levels'
    # block fall back to the support/resistance levels.
    if kind == "breakout":
        if price is None:
            return False, f"{ticker}: no price available yet."
        for name, lvl in (("1-month high", _num(lv.get("high_20d"))),
                          ("52-week high", _num(lv.get("high_252d")))):
            if lvl is not None and price >= lvl:
                return True, (f"{ticker} broke out — new {name} "
                              f"${lvl:,.2f} (price ${price:,.2f})")
        if lv.get("high_20d") is None and lv.get("high_252d") is None:
            res = _num(sr.get("nearest_resistance"))
            if res is not None and price >= res:
                return True, f"{ticker} broke above resistance ${res:,.2f}"
        return False, f"{ticker} has not broken out yet"

    if kind == "breakdown":
        if price is None:
            return False, f"{ticker}: no price available yet."
        lvl = _num(lv.get("low_20d"))
        if lvl is not None and price <= lvl:
            return True, (f"{ticker} broke down — new 1-month low "
                          f"${lvl:,.2f} (price ${price:,.2f})")
        if lvl is None:
            sup = _num(sr.get("nearest_support"))
            if sup is not None and price <= sup:
                return True, f"{ticker} broke below support ${sup:,.2f}"
        return False, f"{ticker} has not broken down yet"

    return False, "unknown condition"


REPEAT_COOLDOWN_H = 12   # a repeating alert fires at most every 12 hours


def _in_cooldown(alert: dict, now: datetime | None = None) -> bool:
    last = alert.get("triggered_at")
    if not last:
        return False
    try:
        fired = datetime.strptime(str(last)[:19], "%Y-%m-%d %H:%M:%S")   # SQLite UTC
    except ValueError:
        return False
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)
    return now - fired < timedelta(hours=REPEAT_COOLDOWN_H)


def trigger(user_id: int, alert: dict, r: dict) -> bool:
    """Mark alert triggered, create in-app notification, deliver via channels.

    A fired alert stays quiet. A REPEATING one re-arms as soon as its
    condition is no longer true, so it can fire again the next time it
    becomes true (but not within REPEAT_COOLDOWN_H hours of the last time).
    """
    ok, desc = evaluate(alert, r)
    if alert.get("triggered"):
        if alert.get("repeat") and not ok:
            store.rearm_alert(alert["id"])
        return False
    if not ok:
        return False
    if alert.get("repeat") and _in_cooldown(alert):
        return False

    store.mark_alert_triggered(alert["id"], f"{r['price']:.2f}")
    body = (f"{desc}\n"
            f"Price: ${r['price']:,.2f} ({r['change_pct']:+.2f}%)\n"
            f"Score: {r['score']:.1f}/100 — {r['rating']}\n"
            f"RSI: {r['indicators'].get('rsi')} · Pattern: {r['pattern']}")
    store.add_notification(user_id, f"🚨 Alert fired: {r['ticker']}",
                           body, r["ticker"])

    channels = set(json.loads(alert.get("channels") or '["app"]'))
    cfg = store.load_config()
    user = store.get_user(user_id)

    if "email" in channels and cfg["email"].get("enabled"):
        to_addr = cfg["email"].get("to_addr") or (user or {}).get("email") or ""
        if to_addr:
            try:
                send_email(cfg["email"], f"🚨 Dr. Shah's Alert: {r['ticker']}",
                           body + "\n\n— Dr. Shah's US Stocks Analysis", to_addr)
            except Exception as e:  # noqa: BLE001
                log.error("email delivery failed: %s", e)

    if "telegram" in channels and cfg["telegram"].get("enabled"):
        try:
            send_telegram(cfg["telegram"], f"<b>🚨 {r['ticker']}</b>\n<pre>{body}</pre>")
        except Exception as e:  # noqa: BLE001
            log.error("telegram delivery failed: %s", e)

    log.info("alert %s fired for user %s (%s)", alert["id"], user_id, desc)
    return True


def evaluate_results(results: dict, user_id) -> int:
    """Check all active alerts of one user against fresh analysis results."""
    if not user_id:
        return 0
    fired = 0
    for alert in store.get_alerts(user_id, active_only=True):
        if alert["triggered"] and not alert.get("repeat"):
            continue                      # fired once, waits for a manual re-arm
        r = results.get(alert["ticker"].upper())
        if r is None:
            continue
        try:
            if trigger(user_id, alert, r):
                fired += 1
        except Exception as e:  # noqa: BLE001
            # one bad alert must never break the analysis request
            log.error("alert %s failed for user %s: %s", alert.get("id"), user_id, e)
    try:
        fired += check_rating_watch(int(user_id), results)
    except Exception as e:  # noqa: BLE001
        log.error("rating watch failed for user %s: %s", user_id, e)
    return fired


def check_rating_watch(user_id: int, results: dict) -> int:
    """Whole-watchlist rating watch: report every stock whose rating differs
    from the previous session's close — once per stock per day, new watchlist
    stocks included automatically. One in-app notification per stock and ONE
    combined Telegram / email message per check. Returns how many changed."""
    prefs = store.get_prefs(user_id).get("rating_watch") or {}
    if not prefs.get("enabled"):
        return 0
    changes = []
    for ticker, r in (results or {}).items():
        try:
            prev = (r.get("score_trend") or {}).get("prev_rating")
            now_r = r.get("rating")
            day = r.get("data_as_of") or r.get("last_updated")
            if not prev or not now_r or prev == now_r or not day:
                continue
            if not store.claim_rating_notice(user_id, ticker, str(day)):
                continue                      # already reported today
            up = _rating_rank(now_r) > _rating_rank(prev)
            prev_score = (r.get("score_trend") or {}).get("prev_score")
            changes.append({"ticker": ticker, "up": up, "prev": prev, "now": now_r,
                            "score": r.get("score"), "prev_score": prev_score,
                            "price": r.get("price"), "chg": r.get("change_pct")})
        except Exception as e:  # noqa: BLE001 — one odd result must not stop the rest
            log.error("rating watch skipped %s: %s", ticker, e)
    if not changes:
        return 0
    changes.sort(key=lambda c: (not c["up"], c["ticker"]))        # upgrades first

    def line(c, html=False):
        name = f"<b>{c['ticker']}</b>" if html else c["ticker"]
        txt = f"{'⬆' if c['up'] else '⬇'} {name}: {c['prev']} → {c['now']}"
        if isinstance(c["score"], (int, float)):
            txt += f" · score {c['score']:.1f}"
            if isinstance(c["prev_score"], (int, float)):
                txt += f" (was {c['prev_score']:.1f})"
        if isinstance(c["price"], (int, float)):
            txt += f" · ${c['price']:,.2f}"
            if isinstance(c["chg"], (int, float)):
                txt += f" ({c['chg']:+.2f}%)"
        return txt

    for c in changes:
        store.add_notification(
            user_id, f"{'⬆ Upgraded' if c['up'] else '⬇ Downgraded'}: {c['ticker']}",
            line(c), c["ticker"])

    channels = set(prefs.get("channels") or [])
    cfg = store.load_config()
    title = f"Rating changes in your watchlist ({len(changes)})"
    if "telegram" in channels and cfg["telegram"].get("enabled"):
        try:
            send_telegram(cfg["telegram"], f"<b>📊 {title}</b>\n"
                          + "\n".join(line(c, html=True) for c in changes))
        except Exception as e:  # noqa: BLE001
            log.error("rating watch telegram failed: %s", e)
    if "email" in channels and cfg["email"].get("enabled"):
        user = store.get_user(user_id)
        to_addr = cfg["email"].get("to_addr") or (user or {}).get("email") or ""
        if to_addr:
            try:
                send_email(cfg["email"], f"📊 Dr. Shah's — {title}",
                           "\n".join(line(c) for c in changes)
                           + "\n\n— Dr. Shah's US Stocks Analysis", to_addr)
            except Exception as e:  # noqa: BLE001
                log.error("rating watch email failed: %s", e)
    log.info("rating watch: %d change(s) reported to user %s", len(changes), user_id)
    return len(changes)


def check_watchlist_rating_changes() -> int:
    """Background: run the rating watch for every account that switched it on."""
    total = 0
    for uid in store.rating_watch_users():
        results = {}
        for t in store.get_watchlist(uid):
            try:
                results[t.upper()] = analyze_one_import(t)
            except Exception as e:  # noqa: BLE001
                log.info("rating watch skip %s: %s", t, e)
        try:
            total += check_rating_watch(uid, results)
        except Exception as e:  # noqa: BLE001
            log.error("rating watch failed for user %s: %s", uid, e)
    return total


def check_active_alerts() -> int:
    """Background check: all users' active alerts vs. latest data (uses cache)."""
    try:
        watch_fired = check_watchlist_rating_changes()
    except Exception as e:  # noqa: BLE001
        log.error("rating watch check failed: %s", e)
        watch_fired = 0
    alerts = store.all_active_alerts()
    if not alerts:
        return watch_fired
    by_ticker: dict[str, list[dict]] = {}
    for a in alerts:
        by_ticker.setdefault(a["ticker"].upper(), []).append(a)

    fired = 0
    for ticker, alist in by_ticker.items():
        try:
            r = analyze_one_import(ticker)
        except Exception as e:  # noqa: BLE001
            log.info("alert check skip %s: %s", ticker, e)
            continue
        for alert in alist:
            try:
                if trigger(alert["user_id"], alert, r):
                    fired += 1
            except Exception as e:  # noqa: BLE001
                log.error("alert %s evaluation failed: %s", alert["id"], e)
    return fired + watch_fired


def analyze_one_import(ticker):
    """Late import to avoid a circular import at module load time."""
    import analysis
    return analysis.analyze_one(ticker, force=False)


# ---------------------------------------------------------------------------
# Delivery channels
# ---------------------------------------------------------------------------

def smtp_error_hint(e: Exception) -> str:
    """Turn a raw SMTP failure into something a non-technical user can act on."""
    text = str(e)
    if isinstance(e, smtplib.SMTPAuthenticationError) or "535" in text or "BadCredentials" in text:
        return ("The mail server rejected the password. For Gmail you must use a "
                "16-letter App Password (myaccount.google.com/apppasswords) — a normal "
                "Gmail password never works. Create a new App Password and paste it in "
                "Settings → Email.")
    if isinstance(e, (smtplib.SMTPServerDisconnected, ConnectionError, OSError)) \
            or "unexpectedly closed" in text.lower():
        return ("The mail server closed the connection before login. That normally means "
                "your internet provider, office network or antivirus is blocking outgoing "
                "mail (SMTP). The app already retried port 465 automatically.")
    if "timed out" in text.lower() or "timeout" in text.lower():
        return ("The mail server did not answer in time — it is blocked or offline. "
                "Try another network, or use Telegram alerts instead.")
    return text


def _send_via(host: str, port: int, user: str, password: str,
              msg, use_ssl: bool = False) -> None:
    """One delivery attempt (SSL on 465, STARTTLS on 587/25)."""
    if use_ssl:
        with smtplib.SMTP_SSL(host, port, timeout=30) as s:
            if user:
                s.login(user, password)
            s.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            if user:
                s.login(user, password)
            s.send_message(msg)


def send_email(cfg: dict, subject: str, body: str, to_addr: str,
               attachment: tuple | None = None) -> None:
    """Send a mail (optionally with a PDF attachment). Tries the configured port
    first, then falls back to the other common port (587 ⇄ 465) so a network that
    blocks one still delivers. Bad credentials are never retried."""
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.get("from_addr") or cfg.get("smtp_user")
    msg["To"] = to_addr
    msg.set_content(body)
    if attachment:
        fname, blob, subtype = attachment
        msg.add_attachment(blob, maintype="application", subtype=subtype,
                           filename=fname)

    host = str(cfg.get("smtp_host") or "").strip()
    if not host:
        # an empty host used to be tried anyway (it means "this computer") and
        # the failure was then blamed on the internet provider
        raise RuntimeError("The SMTP host is empty — fill it in under Settings → Email "
                           "(for a Gmail address it is smtp.gmail.com).")
    port = int(cfg.get("smtp_port") or 587)
    user = str(cfg.get("smtp_user") or "").strip()
    password = cfg.get("smtp_pass") or ""

    attempts = [(port, False), (465, True)] if port != 465 else [(465, True), (587, False)]
    problems = []
    for p, use_ssl in attempts:
        try:
            _send_via(host, p, user, password, msg, use_ssl=use_ssl)
            return
        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError(smtp_error_hint(e)) from e      # credentials — stop
        except smtplib.SMTPRecipientsRefused as e:
            raise RuntimeError(f"The mail server refused the recipient address "
                               f"'{to_addr}'.") from e
        except Exception as e:  # noqa: BLE001 — network layer, try the other port
            problems.append(f"port {p}: {type(e).__name__}: {str(e)[:110]}")
    raise RuntimeError(smtp_error_hint(Exception("; ".join(problems)))
                       + "  [tried " + " and ".join(f"port {p}" for p, _ in attempts)
                       + f" → {problems[-1]}]")


_BOT_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")


def valid_bot_token(token) -> bool:
    """Shape of a real Telegram bot token: <bot number>:<secret of 30+ chars>."""
    return bool(_BOT_TOKEN_RE.match(str(token or "").strip()))


def _tg_token(cfg: dict) -> str:
    token = str(cfg.get("bot_token") or "").strip()
    # users often paste the token WITH the "bot" URL prefix or stray whitespace —
    # clean it before building the API URL (real tokens always start with digits)
    if token.lower().startswith("bot"):
        token = token[3:]
    if not token or ":" not in token:
        raise ValueError("Bot token looks empty or malformed — copy the full token from @BotFather.")
    return token


def _tg_chat(cfg: dict) -> str:
    chat = str(cfg.get("chat_id") or "").strip()
    if not chat:
        raise ValueError("Telegram Chat ID is empty — add it in Settings (✈️ Telegram Alerts).")
    return chat


def _tg_raise(e) -> None:  # pragma: no cover — exercised live
    if isinstance(e, urllib.error.HTTPError):
        if e.code == 404:
            raise ValueError(
                "Telegram says: bot not found (HTTP 404). The bot token is wrong or "
                "incomplete. Open @BotFather in Telegram, tap your bot, send /token "
                "and copy the FULL code (numbers:letters) — no spaces, no 'bot' in front.") from e
        if e.code == 401:
            raise ValueError(
                "Telegram says: unauthorized (HTTP 401). The bot token is incorrect — "
                "regenerate it in @BotFather (/revoke then /token) and paste the new one.") from e
        if e.code == 400:
            raise ValueError(
                "Telegram says: bad request (HTTP 400) — usually the Chat ID is wrong. "
                "Open your bot in Telegram, press Start, then check your Chat ID "
                "(@userinfobot shows it as 'Id:').") from e
        raise ValueError(f"Telegram error {e.code}: {e.reason}") from e
    raise e


TG_RETRY_WAITS = (4, 12)      # seconds between attempts after a network error


def _tg_post(req, timeout: int) -> None:
    """POST to Telegram, trying again after a NETWORK error (connection reset,
    no route, time-out — e.g. Wi-Fi still connecting right after the PC
    starts). An answer from Telegram itself (wrong token / chat) is final."""
    for wait in (*TG_RETRY_WAITS, None):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp.read()
            return
        except urllib.error.HTTPError as e:
            _tg_raise(e)
        except (urllib.error.URLError, ConnectionError, TimeoutError, OSError) as e:
            if wait is None:
                raise
            log.warning("telegram network error (%s) — trying again in %ss", e, wait)
            time.sleep(wait)


def send_telegram(cfg: dict, text: str) -> None:
    token = _tg_token(cfg)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": _tg_chat(cfg), "text": text, "parse_mode": "HTML"}).encode()
    _tg_post(urllib.request.Request(url, data=payload), timeout=25)


def _multipart_body(fields: dict, file_field: str, filename: str,
                    file_bytes: bytes, content_type: str = "application/pdf"):
    """Minimal multipart/form-data body (no external deps needed)."""
    import uuid
    boundary = uuid.uuid4().hex
    body = b""
    for k, v in fields.items():
        body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{k}\"\r\n\r\n"
                 f"{v}\r\n").encode()
    body += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
             f"filename=\"{filename}\"\r\nContent-Type: {content_type}\r\n\r\n").encode()
    body += file_bytes
    body += f"\r\n--{boundary}--\r\n".encode()
    return boundary, body


def send_telegram_document(cfg: dict, document: bytes, filename: str,
                           caption: str = "") -> None:
    """Send a document (e.g. the daily PDF report) with a text caption."""
    token = _tg_token(cfg)
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    fields = {"chat_id": _tg_chat(cfg)}
    if caption:
        fields["caption"] = caption[:1024]      # Telegram caption limit
        fields["parse_mode"] = "HTML"
    boundary, body = _multipart_body(fields, "document", filename, document)
    req = urllib.request.Request(url, data=body, headers={
        "Content-Type": f"multipart/form-data; boundary={boundary}"})
    _tg_post(req, timeout=60)
