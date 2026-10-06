"""Runs Foxglove's real main() in a subprocess and drives it, for the site-data persistence tests.

    python3 site_driver.py set <base url> <how to quit>    # store site data, then quit that way
    python3 site_driver.py check <base url> <out.json>      # report which site data is still there

The caller sets FOXGLOVE_TEST_ROOT (the profile lives under it). Nothing in foxglove.py is changed: the driver
only wraps BrowserWindow.__init__ to get hold of the window main() creates.
"""
from __future__ import annotations

import json
import os
import signal
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

_env.prepare(Path(os.environ["FOXGLOVE_TEST_ROOT"]))

from PyQt6.QtCore import QTimer, QUrl  # noqa: E402
from PyQt6.QtWidgets import QApplication, QMessageBox  # noqa: E402

import helpers  # noqa: E402

fg = helpers.load_foxglove()
mode, base = sys.argv[1], sys.argv[2].rstrip("/")
arg = sys.argv[3] if len(sys.argv) > 3 else ""
origin = QUrl(base)
NOTIFY = fg.QWebEnginePermission.PermissionType.Notifications
GEO = fg.QWebEnginePermission.PermissionType.Geolocation
deadline = time.monotonic() + 40


def poll(win, script: str, then) -> None:
    """Evaluate *script* in the current tab every 100 ms until it gives a truthy value, then call then(value)."""
    def tick() -> None:
        tab = win.current_tab()
        if time.monotonic() > deadline:
            print("driver: timed out", file=sys.stderr)
            os._exit(3)
        if tab is None or tab.page is None:
            QTimer.singleShot(100, tick)
            return
        tab.page.runJavaScript(script, 0, lambda value: then(value) if value else QTimer.singleShot(100, tick))
    QTimer.singleShot(100, tick)


def quit_now(win, how: str) -> None:
    print(f"driver: quitting via {how}", file=sys.stderr, flush=True)
    if how == "close":
        win.close()
    elif how == "quit_action":
        win.act_quit.trigger()
    elif how == "app_quit":
        QApplication.quit()
    elif how == "sigkill":  # not a way Foxglove quits: proves the checks notice lost data
        os.kill(os.getpid(), signal.SIGKILL)
    elif how in ("sigterm", "sigint"):
        os.kill(os.getpid(), signal.SIGTERM if how == "sigterm" else signal.SIGINT)
    elif how == "restart":
        win.restart_browser()  # os.execv()s foxglove.py; the test stops that process
    elif how == "busy_close":  # a download running and a pop-up open
        QMessageBox.question = staticmethod(lambda *a, **k: QMessageBox.StandardButton.Yes)
        win.current_tab().page.runJavaScript(
            "window.open('/blank?popup', 'p', 'popup,width=300,height=300');"
            "const a = document.createElement('a'); a.href = '/slow-download'; a.download = 'big.bin';"
            "document.body.appendChild(a); a.click(); 1")

        def ready() -> None:
            if time.monotonic() > deadline:
                os._exit(4)
            if win.popups and any(item.active for item in win.downloads):
                print(f"driver: {len(win.popups)} popup(s), downloads active", file=sys.stderr, flush=True)
                win.close()
            else:
                QTimer.singleShot(100, ready)
        QTimer.singleShot(100, ready)
    else:
        raise SystemExit(f"unknown quit path {how}")


def on_set(win) -> None:
    def stored(_value) -> None:
        win.profile.queryPermission(origin, NOTIFY).grant()   # what "Allow" in Foxglove's prompt does
        win.profile.queryPermission(origin, GEO).deny()
        quit_now(win, arg)
    poll(win, "window.__done === true", stored)


def on_check(win) -> None:
    def report(value) -> None:
        result = json.loads(value)
        result["notifications"] = win.profile.queryPermission(origin, NOTIFY).state().name
        result["geolocation"] = win.profile.queryPermission(origin, GEO).state().name
        result["index"] = sorted(bytes(c.name()).decode() for c in fg.CookieIndex.of(win.profile).for_site(origin.host()))
        Path(arg).write_text(json.dumps(result), encoding="utf-8")
        win.close_now()
    poll(win, "window.__result || ''", report)


original_init = fg.BrowserWindow.__init__


def init(self, *args, **kwargs) -> None:
    original_init(self, *args, **kwargs)
    QTimer.singleShot(0, lambda: (on_set if mode == "set" else on_check)(self))


fg.BrowserWindow.__init__ = init
sys.exit(fg.main(["foxglove.py", f"{base}/{mode}"]))
