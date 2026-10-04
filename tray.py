"""
System-tray icon for the packaged Windows app.

The .exe runs without a console window; this icon (bottom-right, next to the
clock) is how you reach it:
  • click                → open the dashboard in the browser
  • right-click menu     → Open dashboard · Start with Windows (tick) ·
                           Open data folder · Open log file · Quit
"""
import logging
import os

import applog
import autostart
import paths

log = logging.getLogger("tray")
TITLE = "Dr. Shah's US Stocks Analysis"


def available() -> bool:
    """pystray + Pillow importable (bundled with the .exe)."""
    try:
        import pystray  # noqa: F401
        from PIL import Image  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


def _startfile(path: str) -> None:
    try:
        os.startfile(path)            # Windows: opens Explorer / Notepad
    except Exception as e:  # noqa: BLE001
        log.error("could not open %s: %s", path, e)


def build_menu(pystray, open_dashboard, on_quit):
    """The right-click menu (separate so it can be tested without a tray)."""

    def toggle_autostart(icon, _item):
        try:
            autostart.set_enabled(not autostart.is_enabled())
        except Exception as e:  # noqa: BLE001
            log.error("start-with-Windows toggle failed: %s", e)
            try:
                icon.notify(f"Could not change 'Start with Windows': {e}", TITLE)
            except Exception:  # noqa: BLE001
                pass

    def quit_app(icon, _item):
        log.info("quit from the tray menu")
        icon.stop()
        on_quit()

    return pystray.Menu(
        pystray.MenuItem("Open dashboard", lambda icon, item: open_dashboard(), default=True),
        pystray.MenuItem("Start with Windows", toggle_autostart,
                         checked=lambda item: autostart.is_enabled(),
                         enabled=autostart.supported()),
        pystray.MenuItem("Open data folder", lambda icon, item: _startfile(paths.DATA_DIR)),
        pystray.MenuItem("Open log file", lambda icon, item: _startfile(applog.LOG_FILE)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit Dr. Shah's", quit_app),
    )


def run(open_dashboard, on_quit, welcome: bool = True) -> None:
    """Show the icon and block until Quit is chosen (call from the main thread)."""
    import pystray
    from PIL import Image

    image = Image.open(paths.LOGO_PATH)
    icon = pystray.Icon("DrShahsUSStocksAnalysis", image, TITLE,
                        build_menu(pystray, open_dashboard, on_quit))

    def setup(ic):
        ic.visible = True
        if welcome:
            try:
                ic.notify("Running in the background. Click this icon to open the "
                          "dashboard; right-click it to quit.", TITLE)
            except Exception:  # noqa: BLE001 — notifications are optional
                pass

    log.info("tray icon shown")
    icon.run(setup=setup)
