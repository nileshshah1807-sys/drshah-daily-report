"""
Log file — everything the app reports is also written to logs/app.log next to
the program, so a problem can be looked at after the window (or the tray
app) has closed.

  • rotating: 2 MB per file, the last 5 files kept (≈ 12 MB at most)
  • captures logging messages AND anything printed / any traceback, because
    parts of the app (and its libraries) print instead of logging
  • the console (when there is one) keeps showing the same text as before
"""
import logging
import os
import sys
from logging.handlers import RotatingFileHandler

import paths

LOG_FILE = os.path.join(paths.LOGS_DIR, "app.log")
_FMT = "%(asctime)s  %(levelname)-7s %(name)s: %(message)s"
_configured = False


class _StreamToLog:
    """Stand-in for sys.stdout / sys.stderr: passes text through to the
    original stream (if any) and writes each complete line to the log."""

    def __init__(self, original, logger: logging.Logger, level: int):
        self._orig = original
        self._log = logger
        self._level = level
        self._buf = ""

    def write(self, text):
        if self._orig is not None:
            try:
                self._orig.write(text)
            except Exception:  # noqa: BLE001 — a closed console must not break the app
                pass
        self._buf += str(text)
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self._log.log(self._level, line.rstrip())
        return len(text)

    def flush(self):
        if self._orig is not None:
            try:
                self._orig.flush()
            except Exception:  # noqa: BLE001
                pass

    def isatty(self):
        return bool(self._orig is not None and getattr(self._orig, "isatty", lambda: False)())

    @property
    def encoding(self):
        return getattr(self._orig, "encoding", "utf-8") or "utf-8"


def setup(console_level: int = logging.WARNING) -> str:
    """Start writing logs/app.log. Safe to call more than once. Returns the path."""
    global _configured
    if _configured:
        return LOG_FILE
    os.makedirs(paths.LOGS_DIR, exist_ok=True)
    file_handler = RotatingFileHandler(LOG_FILE, maxBytes=2 * 1024 * 1024,
                                       backupCount=5, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(_FMT, "%Y-%m-%d %H:%M:%S"))
    file_handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(file_handler)
    if sys.__stderr__ is not None:           # a console window exists
        console = logging.StreamHandler(sys.__stderr__)
        console.setLevel(console_level)
        console.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(console)
    for noisy in ("urllib3", "peewee", "yfinance"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # printed text and tracebacks → log file too. The console already shows
    # them as printed, so this logger writes to the file only (no duplicates).
    printed = logging.getLogger("console")
    printed.propagate = False
    printed.setLevel(logging.INFO)
    printed.addHandler(file_handler)
    sys.stdout = _StreamToLog(sys.stdout, printed, logging.INFO)
    sys.stderr = _StreamToLog(sys.stderr, printed, logging.ERROR)
    _configured = True
    logging.getLogger("app").info("===== %s starting (%s) =====",
                                  paths.APP_NAME, paths.describe()["mode"])
    return LOG_FILE
