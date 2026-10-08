"""The back/forward cache: Back/Forward show a page as it was left, at once, and Qt's report of such a restore
(loadFinished(False), then LoadFailedStatus with no error) still counts as a page that loaded: webNavigation events,
the history visit, the address bar, the throbber and tabs.onUpdated - while real failures, stops and 204s don't."""
from __future__ import annotations

import inspect
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineCore import QWebEngineLoadingInfo, QWebEnginePage, QWebEngineSettings

from helpers import run_js, spin, wait_until

STATUS, DOMAIN = QWebEngineLoadingInfo.LoadStatus, QWebEngineLoadingInfo.ErrorDomain


class CacheableServer:
    """Pages without Cache-Control (helpers.LocalServer sends no-store, which keeps pages out of the cache).
    /nocontent -> 204, /slow -> held until release is set."""

    def __init__(self) -> None:
        self.requests: list[str] = []
        self.release = threading.Event()
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                path = urlsplit(self.path).path
                server.requests.append(path)
                try:
                    if path == "/nocontent":
                        self.send_response(204)
                        self.end_headers()
                        return
                    if path == "/slow":
                        server.release.wait(15)
                    body = (f"<!doctype html><title>{path}</title><body><p>{path}</p><script>"
                            "addEventListener('pageshow', (e) => { window.persisted = e.persisted; });</script>").encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except OSError:  # the browser gave up on it (Stop)
                    pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="foxglove-test-bfcache", daemon=True)
        self.thread.start()

    def url(self, path: str) -> str:
        return f"http://127.0.0.1:{self.httpd.server_address[1]}{path}"

    def close(self) -> None:
        self.release.set()
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def cacheable():
    srv = CacheableServer()
    yield srv
    srv.close()


@pytest.fixture
def events(fg, monkeypatch):
    """(name, url) of every webNavigation event and {change} of every tabs.onUpdated the window sends."""
    seen: dict[str, list] = {"nav": [], "updated": []}
    nav, updated = fg.BrowserWindow._navigation_event, fg.BrowserWindow._tab_updated

    def navigation_event(self, tab, name: str, url: QUrl) -> None:
        seen["nav"].append((name, url.toString()))
        nav(self, tab, name, url)

    def tab_updated(self, tab, change: dict) -> None:
        seen["updated"].append(change)
        updated(self, tab, change)
    monkeypatch.setattr(fg.BrowserWindow, "_navigation_event", navigation_event)
    monkeypatch.setattr(fg.BrowserWindow, "_tab_updated", tab_updated)
    return seen


def visits(window, url: str) -> int:
    return next((row[3] for row in window.history.recent() if row[0] == url), 0)


def finished(tab, action, url: str = "") -> tuple[str, int]:
    """Run *action* and wait for the tab's load (of *url*, if given) to end: (status name, error code) from Qt."""
    box: list = []

    def changed(info) -> None:
        if info.status() != STATUS.LoadStartedStatus and (not url or info.url().toString() == url):
            box.append((info.status().name, info.errorCode()))
    tab.page.loadingChanged.connect(changed)
    try:
        action()
        wait_until(lambda: box, 20, "the load to end")
        spin(0.3)  # everything else the end of the load sets off
    finally:
        tab.page.loadingChanged.disconnect(changed)
    return box[0]


def load_tab(tab, url: str) -> None:
    assert finished(tab, lambda: tab.load(QUrl(url)), url)[0] == "LoadSucceededStatus"  # (not the New Tab page's)
    assert tab.page.url().toString() == url and not tab.loading


def test_back_forward_cache_is_on(fg, harness):
    attribute = QWebEngineSettings.WebAttribute
    assert harness.profile.settings().testAttribute(attribute.BackForwardCacheEnabled)  # the harness mirrors main()
    assert "attribute.BackForwardCacheEnabled, True" in inspect.getsource(fg.main)


def test_back_forward_cache_restore_counts_as_loaded(fg, window, cacheable, events):
    tab = window.current_tab()
    a, b = cacheable.url("/a"), cacheable.url("/b")
    load_tab(tab, a)
    load_tab(tab, b)
    assert visits(window, a) == 1 and visits(window, b) == 1
    events["nav"].clear()
    events["updated"].clear()

    assert finished(tab, window.act_back.trigger) == ("LoadFailedStatus", 0)  # how Qt reports a cache restore
    assert tab.page.url().toString() == a
    assert run_js(tab.page, "window.persisted") is True and cacheable.requests.count("/a") == 1  # not loaded again
    assert events["nav"].count(("onDOMContentLoaded", a)) == 1 and events["nav"].count(("onCompleted", a)) == 1
    assert {"status": "complete"} in events["updated"]
    assert visits(window, a) == 2  # one visit, as for any Back
    assert window.url_bar.text() == fg.display_url(QUrl(a)) and window.windowTitle().startswith("/a")
    label = window.tab_bar.label(window.index_of(tab))
    assert not tab.loading and tab.progress == 100 and not label.loading  # the throbber stopped
    assert not tab.crashed and tab.crash_bar is None and window.forward_button.isEnabled()

    events["nav"].clear()
    assert finished(tab, window.act_forward.trigger) == ("LoadFailedStatus", 0)
    assert tab.page.url().toString() == b and run_js(tab.page, "window.persisted") is True
    assert events["nav"].count(("onCompleted", b)) == 1 and visits(window, b) == 2
    assert window.url_bar.text() == fg.display_url(QUrl(b)) and not tab.loading
    # Memory Saver's "typed into" guard: a new document resets it, a restore keeps the document (and the draft)
    tab.typed = True
    assert finished(tab, window.act_back.trigger) == ("LoadFailedStatus", 0) and tab.typed is True
    load_tab(tab, cacheable.url("/c"))
    assert tab.typed is False


def test_204_stop_and_failed_loads_are_not_counted_as_loaded(fg, window, cacheable, events):
    tab = window.current_tab()
    a = cacheable.url("/a")
    load_tab(tab, a)
    events["nav"].clear()

    nocontent = cacheable.url("/nocontent")  # Chromium stays on the page
    assert finished(tab, lambda: tab.load(QUrl(nocontent)))[0] == "LoadStoppedStatus"
    slow = cacheable.url("/slow")

    def stop() -> None:
        tab.load(QUrl(slow))
        wait_until(lambda: "/slow" in cacheable.requests, 10, "the slow request")
        tab.page.triggerAction(QWebEnginePage.WebAction.Stop)
    assert finished(tab, stop)[0] == "LoadStoppedStatus"
    cacheable.release.set()
    assert tab.page.url().toString() == a and not tab.loading

    with socket.socket() as sock:  # a port nothing listens on
        sock.bind(("127.0.0.1", 0))
        refused = f"http://127.0.0.1:{sock.getsockname()[1]}/"
    status, code = finished(tab, lambda: tab.load(QUrl(refused)))
    assert status == "LoadFailedStatus" and code != 0
    assert not [e for e in events["nav"] if e[0] in ("onDOMContentLoaded", "onCompleted")], events["nav"]
    assert visits(window, a) == 1 and not any(visits(window, url) for url in (nocontent, slow, refused))


def test_only_the_restore_signature_counts_from_loading_changed(fg, window, monkeypatch):
    """_on_loading_changed calls a load a success only for LoadFailedStatus + code 0 + NoErrorDomain + no error page."""
    tab = window.current_tab()
    calls: list = []
    monkeypatch.setattr(fg.BrowserWindow, "_load_succeeded", lambda self, t: calls.append(t))
    cases = [(STATUS.LoadFailedStatus, 0, DOMAIN.NoErrorDomain, False, True),  # Qt's back/forward cache restore
             (STATUS.LoadFailedStatus, 0, DOMAIN.NoErrorDomain, True, False),
             (STATUS.LoadFailedStatus, 0, DOMAIN.InternalErrorDomain, False, False),
             (STATUS.LoadFailedStatus, -102, DOMAIN.ConnectionErrorDomain, True, False),
             (STATUS.LoadFailedStatus, -3, DOMAIN.InternalErrorDomain, False, False),
             (STATUS.LoadStoppedStatus, -3, DOMAIN.InternalErrorDomain, False, False),
             (STATUS.LoadStoppedStatus, 0, DOMAIN.NoErrorDomain, False, False),
             (STATUS.LoadSucceededStatus, 200, DOMAIN.HttpStatusCodeDomain, False, False)]  # loadFinished(True) did it
    for status, code, domain, error_page, counted in cases:
        calls.clear()
        target = "https://expired.test/"
        tab.back_after_error = target
        info = SimpleNamespace(status=lambda s=status: s, errorCode=lambda c=code: c, errorDomain=lambda d=domain: d,
                               isErrorPage=lambda e=error_page: e, url=lambda: QUrl("http://127.0.0.1:9/x"))
        window._on_loading_changed(tab, info)
        assert calls == ([tab] if counted else []), (status, code, domain, error_page)
        # a restore doesn't use up "step back past the certificate error page"; any other end of a load does
        assert tab.back_after_error == (target if counted else ""), (status, code, domain, error_page)
    tab.back_after_error = ""
    # a renderer that died mid-load ends with the very same signature: no success for a dead page
    calls.clear()
    tab.crashed = True
    info = SimpleNamespace(status=lambda: STATUS.LoadFailedStatus, errorCode=lambda: 0, errorDomain=lambda: DOMAIN.NoErrorDomain,
                           isErrorPage=lambda: False, url=lambda: QUrl("http://127.0.0.1:9/x"))
    window._on_loading_changed(tab, info)
    tab.crashed = False
    assert calls == []
