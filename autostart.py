"""
"Start with Windows" — a value under
HKEY_CURRENT_USER\\Software\\Microsoft\\Windows\\CurrentVersion\\Run that starts
the packaged app at login with --background (tray icon only, no browser tab).

Per-user, no administrator rights needed; switching it off deletes the value.
Only offered for the packaged .exe (a source install has no single program
file to start).
"""
import os
import sys

import paths

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DrShahsUSStocksAnalysis"
BACKGROUND_FLAG = "--background"


def supported() -> bool:
    return sys.platform == "win32" and paths.is_frozen()


def command() -> str:
    return f'"{sys.executable}" {BACKGROUND_FLAG}'


def _winreg():
    import winreg  # Windows only
    return winreg


def registered_command() -> str | None:
    if sys.platform != "win32":
        return None
    winreg = _winreg()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _kind = winreg.QueryValueEx(key, VALUE_NAME)
            return str(value) or None
    except OSError:
        return None


def is_enabled() -> bool:
    return registered_command() is not None


def set_enabled(on: bool) -> bool:
    """Switch start-with-Windows on/off; returns the new state."""
    if not supported():
        raise RuntimeError("Start with Windows is available in the .exe version only.")
    winreg = _winreg()
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
    return is_enabled()


def sync_path() -> bool:
    """If start-with-Windows points at a program that no longer exists (the
    folder was moved or rebuilt elsewhere), point it at this copy instead.
    Returns True when the entry was updated."""
    if not supported():
        return False
    cmd = registered_command()
    if not cmd:
        return False
    target = cmd.split('"')[1] if cmd.startswith('"') else cmd.split(" ")[0]
    if os.path.exists(target):
        return False
    set_enabled(True)
    return True
