"""
Only ONE copy of the app per port.

The old check ("is the port answering?") left a gap of several seconds while
the first copy was still starting: a second copy started in that gap ran in
full — two schedulers, every daily report built (and delivered) twice, two
tray icons, both writing the same files. Windows even lets both listen on the
same port. That happened whenever the app was opened by hand a moment after
"Start with Windows" had launched it.

Now the very first thing a copy does is take an exclusive lock on a small file
in the user's temp folder (one per port, so it also covers copies in different
folders). The operating system releases it when the process ends, however it
ends. A copy that cannot get the lock waits for the running one to answer and
opens its dashboard instead.

Standard library only: this runs before the slow imports.
"""
import os
import socket
import tempfile
import time


def open_url(url: str) -> bool:
    """Open `url` in the default browser; on Windows fall back to the shell."""
    import webbrowser
    try:
        if webbrowser.open(url):
            return True
    except Exception:  # noqa: BLE001
        pass
    if os.name == "nt":
        try:
            os.startfile(url)
            return True
        except Exception:  # noqa: BLE001
            pass
    return False


def port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Is something already accepting connections on this port?"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.6)
        return s.connect_ex((host, port)) == 0


class InstanceLock:
    def __init__(self, port: int, folder: str | None = None):
        self.path = os.path.join(folder or tempfile.gettempdir(),
                                 f"DrShahsUSStocksAnalysis-{port}.lock")
        self._fh = None

    def acquire(self) -> bool:
        """True = this is the only copy. False = another copy holds the lock."""
        try:
            fh = open(self.path, "a+b")
        except OSError:
            return True          # no usable temp folder: never block the app over it
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            fh.close()
            return False
        self._fh = fh            # kept open for the life of the process
        return True

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            finally:
                self._fh = None


def hand_over(port: int, open_browser: bool = True, wait_s: float = 60.0) -> bool:
    """Another copy owns the port: wait until it answers (it may still be
    starting), then show its dashboard. Returns True if it answered."""
    deadline = time.time() + wait_s
    up = port_in_use(port)
    while not up and time.time() < deadline:
        time.sleep(0.5)
        up = port_in_use(port)
    if open_browser:
        open_url(f"http://localhost:{port}")
    return up
