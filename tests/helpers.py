"""Shared machinery for the Foxglove extension tests: module loading, Qt event-loop waits, JS, a local web server.

_env.prepare() must have run before this module is imported (it imports PyQt6).
"""
from __future__ import annotations

import importlib.util
import json
import re
import sys
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlsplit

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings

from _env import REPO

# ── foxglove.py as a module ───────────────────────────────────────────────────────────────
_module = None


def load_foxglove():
    """Import /foxglove.py from the repo (it is a script, not a package)."""
    global _module
    if _module is None:
        spec = importlib.util.spec_from_file_location("foxglove", REPO / "foxglove.py")
        module = importlib.util.module_from_spec(spec)
        sys.modules["foxglove"] = module  # @dataclass looks the module up while the class body runs
        writes, sys.dont_write_bytecode = sys.dont_write_bytecode, True  # no __pycache__ next to foxglove.py
        try:
            spec.loader.exec_module(module)
        finally:
            sys.dont_write_bytecode = writes
        _module = module
    return _module


def register_schemes() -> None:
    """What main() does before QApplication exists: foxglove:// and foxglove-ext:// must be registered up front."""
    fg = load_foxglove()
    fg.register_url_schemes()
    QCoreApplication.setApplicationName(fg.APP_NAME)  # Qt's extension/profile folders are named after the app
    QCoreApplication.setOrganizationName("")


# ── errors raised inside Qt callbacks ─────────────────────────────────────────────────────
# PyQt calls sys.excepthook for exceptions in slots (and aborts the process if it is the default hook).
# Record them instead, so the test that triggered them fails with the real traceback.
SLOT_ERRORS: list[str] = []


def install_slot_error_hook() -> None:
    def hook(exc_type, exc, tb) -> None:
        if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
            sys.__excepthook__(exc_type, exc, tb)
            return
        SLOT_ERRORS.append("".join(traceback.format_exception(exc_type, exc, tb)))
        traceback.print_exception(exc_type, exc, tb, file=sys.stderr)

    sys.excepthook = hook


# ── waiting on the Qt event loop ──────────────────────────────────────────────────────────
class WaitTimeout(AssertionError):
    pass


def wait_until(predicate, timeout: float = 15.0, message: str = "condition", interval: int = 20):
    """Run the Qt event loop until predicate() is truthy (returns its value) or raise WaitTimeout."""
    deadline = time.monotonic() + timeout
    box: dict = {}
    loop = QEventLoop()

    def check() -> None:
        if box:
            return
        try:
            value = predicate()
        except Exception as exc:  # report it from wait_until, never from inside a Qt callback
            box["error"] = exc
            loop.quit()
            return
        if value:
            box["value"] = value
            loop.quit()
        elif time.monotonic() >= deadline:
            box["timeout"] = True
            loop.quit()

    timer = QTimer()
    timer.setInterval(interval)
    timer.timeout.connect(check)
    timer.start()
    QTimer.singleShot(0, check)
    loop.exec()
    timer.stop()
    if "error" in box:
        raise box["error"]
    if "value" not in box:
        raise WaitTimeout(f"timed out after {timeout:.1f}s waiting for {message}")
    return box["value"]


def spin(seconds: float) -> None:
    """Keep processing events for a while (used to prove that something does *not* happen)."""
    loop = QEventLoop()
    QTimer.singleShot(int(seconds * 1000), loop.quit)
    loop.exec()


# ── pages and JavaScript ──────────────────────────────────────────────────────────────────
def load(page: QWebEnginePage, url: str | QUrl, timeout: float = 20.0) -> bool:
    box: dict = {}

    def finished(ok: bool) -> None:
        box["ok"] = ok

    page.loadFinished.connect(finished)
    try:
        page.load(QUrl(url) if isinstance(url, str) else url)
        wait_until(lambda: "ok" in box, timeout, f"page load of {url}")
    finally:
        page.loadFinished.disconnect(finished)
    return box["ok"]


def run_js(page: QWebEnginePage, script: str, timeout: float = 10.0):
    box: dict = {}
    page.runJavaScript(script, lambda result: box.setdefault("result", result))
    wait_until(lambda: "result" in box, timeout, f"JavaScript result of {script[:80]!r}")
    return box["result"]


def poll_js(page: QWebEnginePage, script: str, accept=bool, timeout: float = 10.0, what: str = ""):
    """Re-evaluate *script* until accept(result); returns the accepted result."""
    state: dict = {"pending": False, "value": None, "done": False}

    def callback(result) -> None:
        state.update(pending=False, value=result, done=bool(accept(result)))

    def predicate() -> bool:
        if not state["done"] and not state["pending"]:
            state["pending"] = True
            page.runJavaScript(script, callback)
        return state["done"]

    try:
        wait_until(predicate, timeout, what or script[:80], interval=100)
    except WaitTimeout as exc:
        raise WaitTimeout(f"{exc}; last value: {state['value']!r}") from None
    return state["value"]


def run_js_async(page: QWebEnginePage, body: str, timeout: float = 10.0):
    """Run *body* as an async function in the page's main world and return its (JSON-able) result."""
    token = uuid.uuid4().hex
    run_js(page, f"""(() => {{
  window.__fg = window.__fg || {{}};
  (async () => {{ {body} }})().then(
    (v) => {{ window.__fg["{token}"] = JSON.stringify({{ok: v === undefined ? null : v}}); }},
    (e) => {{ window.__fg["{token}"] = JSON.stringify({{error: String(e && e.message || e)}}); }});
  return true;
}})()""")
    raw = poll_js(page, f'(window.__fg && window.__fg["{token}"]) || ""', timeout=timeout, what=f"async JS {body[:60]!r}")
    result = json.loads(raw)
    if "error" in result:
        raise AssertionError(f"JavaScript failed: {result['error']}")
    return result["ok"]


def attr(page: QWebEnginePage, name: str):
    return run_js(page, f"document.documentElement.getAttribute({json.dumps(name)})")


def wait_attr(page: QWebEnginePage, name: str, timeout: float = 10.0):
    return poll_js(page, f"document.documentElement.getAttribute({json.dumps(name)})", lambda v: v is not None,
                   timeout, f"attribute {name}")


def stays_absent(page: QWebEnginePage, name: str, seconds: float = 2.0) -> bool:
    """True if the attribute never shows up during *seconds* (content scripts run within ms of load)."""
    try:
        wait_attr(page, name, timeout=seconds)
    except WaitTimeout:
        return True
    return False


# ── local web server ──────────────────────────────────────────────────────────────────────
TEST_PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>Foxglove test page</title></head>
<body><p id="content">plain page</p></body></html>"""


class LocalServer:
    """A tiny threaded HTTP server on 127.0.0.1:<free port>.

    /page...                    -> TEST_PAGE (any query string)
    /service/update2/crx?x=...  -> 302 to /crx/<id>.crx, like the Web Store's update service
    anything added with add()   -> that body
    """

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, str, bytes, dict]] = {}
        self.requests: list[str] = []
        self.headers: dict[str, dict[str, str]] = {}  # path -> the request headers it last came with
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                server.requests.append(self.path)
                server.headers[self.path] = dict(self.headers.items())
                status, ctype, body, headers = server.respond(self.path)
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="foxglove-test-http", daemon=True)
        self.thread.start()

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self.port}{path}"

    def page_url(self) -> str:  # unique, so the browser never reuses a cached copy
        return self.url(f"/page?n={uuid.uuid4().hex[:8]}")

    def add(self, path: str, body: bytes, ctype: str = "application/octet-stream", status: int = 200) -> None:
        self.routes[path] = (status, ctype, body, {})

    def respond(self, raw_path: str) -> tuple[int, str, bytes, dict]:
        parts = urlsplit(raw_path)
        if raw_path in self.routes:
            return self.routes[raw_path]
        if parts.path in self.routes:
            return self.routes[parts.path]
        if parts.path.startswith("/page"):
            return 200, "text/html; charset=utf-8", TEST_PAGE, {}
        if parts.path == "/service/update2/crx":
            x = parse_qs(parts.query).get("x", [""])[0]  # "id=<id>&uc"
            ext_id = parse_qs(unquote(x)).get("id", [""])[0]
            return 302, "text/plain", b"", {"Location": f"/crx/{ext_id}.crx"}
        return 404, "text/plain", b"not found", {}

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


# ── one Foxglove profile + ExtensionsController (+ optional BrowserWindow) ────────────────
class Harness:
    """A fresh, uniquely named QWebEngineProfile with Foxglove's real ExtensionsController on it."""

    def __init__(self, root: Path, name: str | None = None):
        self.fg = fg = load_foxglove()
        self.name = name or f"t{uuid.uuid4().hex[:10]}"
        self.dir = root / "Profiles" / self.name  # Foxglove's own files (registry, staging, settings...)
        self.dir.mkdir(parents=True, exist_ok=True)
        # The same profile set-up as main() (foxglove.py ~5955-5973).
        self.profile = QWebEngineProfile(self.name)
        self.profile.setPersistentCookiesPolicy(QWebEngineProfile.PersistentCookiesPolicy.ForcePersistentCookies)
        self.profile.setPersistentPermissionsPolicy(QWebEngineProfile.PersistentPermissionsPolicy.StoreOnDisk)
        self.user_agent = re.sub(r"\s*QtWebEngine/\S+", "", self.profile.httpUserAgent())
        self.profile.setHttpUserAgent(self.user_agent)
        attribute = QWebEngineSettings.WebAttribute
        for name_, value in (("FullScreenSupportEnabled", True), ("JavascriptCanOpenWindows", True),
                             ("LocalStorageEnabled", True), ("PdfViewerEnabled", True)):
            self.profile.settings().setAttribute(getattr(attribute, name_), value)
        self.pages_handler = fg.InternalPages(lambda: "<!doctype html><title>New Tab</title>")
        self.profile.installUrlSchemeHandler(b"foxglove", self.pages_handler)
        self.registry_path = self.dir / "extensions.json"
        self.staging = self.dir / "extension-staging"
        self.created = time.monotonic()
        self.messages: list[tuple[str, str]] = []  # (kind, text) from ExtensionsController.message
        self.controller = self.new_controller()
        self.pages: list[QWebEnginePage] = []
        self.windows: list = []

    def new_controller(self):
        controller = self.fg.ExtensionsController(self.profile, self.registry_path, self.staging, self.user_agent)
        controller.message.connect(lambda text, kind: self.messages.append((kind, text)))
        return controller

    # extension manager views
    @property
    def manager(self):
        return self.controller.manager

    def entries(self) -> list:
        return self.controller.entries()

    def entry(self, ext_id: str):
        return self.controller.entry(ext_id)

    def by_name(self, name: str) -> list:
        return [e for e in self.entries() if e.name == name]

    def info(self, ext_id: str):
        return self.controller._info(ext_id)

    def registry_on_disk(self) -> dict:
        try:
            return json.loads(self.registry_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def leftover_staging(self) -> list[str]:
        return sorted(p.name for p in self.staging.glob("ext-*")) if self.staging.exists() else []

    # installing
    def wait_message(self, start: int, kinds=("success", "error"), timeout: float = 30.0) -> tuple[str, str]:
        """The first message after index *start* whose kind is in *kinds*."""
        def found():
            return next((m for m in self.messages[start:] if m[0] in kinds), None)
        return wait_until(found, timeout, f"an extension message of kind {kinds} (got {self.messages[start:]})")

    def install(self, path: Path | str, timeout: float = 30.0) -> tuple[str, str]:
        start = len(self.messages)
        self.controller.install_from_path(str(path))
        return self.wait_message(start, timeout=timeout)

    def install_ok(self, path: Path | str, name: str, timeout: float = 30.0):
        """Install and return the (enabled) entry called *name*."""
        kind, text = self.install(path, timeout)
        assert kind == "success", f"install of {path} failed: {text}"
        return self.wait_enabled_by_name(name)

    def wait_enabled_by_name(self, name: str, timeout: float = 15.0):
        return wait_until(lambda: next((e for e in self.by_name(name) if e.enabled), None), timeout,
                          f"extension {name!r} to be installed and enabled (entries: {[(e.name, e.enabled) for e in self.entries()]})")

    def wait_state(self, ext_id: str, enabled: bool, timeout: float = 15.0):
        return wait_until(lambda: (e := self.entry(ext_id)) is not None and e.enabled == enabled and e, timeout,
                          f"{ext_id} enabled={enabled}")

    # pages
    def page(self) -> QWebEnginePage:
        page = QWebEnginePage(self.profile)
        self.pages.append(page)
        return page

    def fresh_page(self, url: str) -> QWebEnginePage:
        page = self.page()
        assert load(page, url), f"couldn't load {url}"
        return page

    def drop_page(self, page: QWebEnginePage) -> None:
        if page in self.pages:
            self.pages.remove(page)
        if not sip.isdeleted(page):
            sip.delete(page)

    # a real browser window on this profile
    def window(self):
        fg, d = self.fg, self.dir
        settings = fg.Settings(d / "settings.json")
        favicons = fg.FaviconCache(d / "favicons")
        bookmarks = fg.BookmarkStore(d / "bookmarks.json", favicons)
        history = fg.HistoryStore(d / "history.sqlite")
        win = fg.BrowserWindow(self.profile, settings, bookmarks, history, favicons, self.controller,
                               d / "session.json", [])
        win.show()
        self.windows.append((win, history))
        return win

    def close(self) -> None:
        # Let Foxglove's own 0/500/2500 ms follow-up timers run while everything still exists.
        spin(max(0.6, 2.7 - (time.monotonic() - self.created)))
        for win, history in self.windows:
            if not sip.isdeleted(win):
                win.close_now()
                if not sip.isdeleted(win):
                    sip.delete(win)
            history.close()
        for page in self.pages:
            if not sip.isdeleted(page):
                sip.delete(page)
        QCoreApplication.processEvents()
        if not sip.isdeleted(self.controller):
            sip.delete(self.controller)
        if not sip.isdeleted(self.profile):
            sip.delete(self.profile)
        QCoreApplication.processEvents()
