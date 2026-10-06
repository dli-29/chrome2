"""Site data: persistence across every way of quitting, site settings, the cookie index and clearing."""
from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent

SET_PAGE = b"""<!doctype html><title>set</title><body>set<script>
(async () => {
  document.cookie = "js=1; path=/";
  localStorage.setItem("ls", "1");
  await new Promise((ok, fail) => {
    const r = indexedDB.open("db", 1);
    r.onupgradeneeded = () => r.result.createObjectStore("s");
    r.onsuccess = () => { const t = r.result.transaction("s", "readwrite"); t.objectStore("s").put("1", "k");
                          t.oncomplete = ok; t.onerror = fail; };
    r.onerror = fail;
  });
  const cache = await caches.open("c");
  await cache.put("/cached", new Response("1"));
  await navigator.serviceWorker.register("/sw.js");
  await navigator.serviceWorker.ready;
  window.__done = true;
})().catch(e => { document.title = "error " + e; });
</script>"""

CHECK_PAGE = b"""<!doctype html><title>check</title><body>check<script>
(async () => {
  const out = {cookie: document.cookie, ls: localStorage.getItem("ls")};
  out.idb = await new Promise(ok => {
    const r = indexedDB.open("db");
    r.onsuccess = () => { const db = r.result;
      if (!db.objectStoreNames.contains("s")) { ok(null); return; }
      const g = db.transaction("s").objectStore("s").get("k"); g.onsuccess = () => ok(g.result ?? null); };
    r.onerror = () => ok(null);
  });
  out.cache = (await caches.has("c")) ? await (await (await caches.open("c")).match("/cached"))?.text() ?? null : null;
  out.sw = (await navigator.serviceWorker.getRegistrations()).length;
  window.__result = JSON.stringify(out);
})().catch(e => { window.__result = JSON.stringify({error: String(e)}); });
</script>"""


class SiteServer:
    """127.0.0.1:<free port> serving the pages above; /set stores data only the first time it is loaded."""

    def __init__(self) -> None:
        self.hits: dict[str, int] = {}
        self.cookies: dict[str, str] = {}  # path -> the Cookie header it last came with
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def do_GET(self) -> None:
                path, _, query = self.path.partition("?")
                server.hits[path] = server.hits.get(path, 0) + 1
                server.cookies[path] = self.headers.get("Cookie", "")
                headers: list[tuple[str, str]] = []
                if path == "/store" or (path == "/set" and server.hits[path] == 1):  # /set: only the first time
                    body, ctype = SET_PAGE, "text/html"
                    headers = [("Set-Cookie", "p=1; Max-Age=31536000; Path=/"), ("Set-Cookie", "s=1; Path=/"),
                               ("Set-Cookie", "h=1; Path=/; HttpOnly")]
                elif path == "/cookies":  # /cookies?name=value&...: session cookies (persistent if the name starts with p)
                    body, ctype = b"<!doctype html><title>cookies</title>", "text/html"
                    headers = [("Set-Cookie", f"{pair}; Path=/{'; Max-Age=3600' if pair.startswith('p') else ''}")
                               for pair in query.split("&") if "=" in pair]
                elif path == "/check":
                    body, ctype = CHECK_PAGE, "text/html"
                elif path == "/sw.js":
                    body, ctype = b"self.addEventListener('fetch', () => {});", "text/javascript"
                elif path == "/slow-download":
                    self.send_response(200)
                    self.send_header("Content-Type", "application/octet-stream")
                    self.send_header("Content-Disposition", "attachment; filename=big.bin")
                    self.send_header("Content-Length", str(50 * 1024 * 1024))
                    self.end_headers()
                    try:
                        for _ in range(500):
                            self.wfile.write(b"\0" * 4096)
                            self.wfile.flush()
                            time.sleep(0.05)
                    except OSError:
                        pass
                    return
                else:
                    body, ctype = b"<!doctype html><title>blank</title>", "text/html"
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for name, value in headers:
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(body)

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


def run_driver(root: Path, *args: str) -> subprocess.Popen:
    env = dict(os.environ, FOXGLOVE_TEST_ROOT=str(root))
    return subprocess.Popen([sys.executable, str(HERE / "site_driver.py"), *args], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)


def finish(proc: subprocess.Popen, timeout: float = 60) -> str:
    try:
        _out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        _out, err = proc.communicate()
        pytest.fail(f"Foxglove didn't quit:\n{err[-3000:]}")
    return err


def store_and_check(how: str) -> dict:
    """Store site data, quit Foxglove the way *how* says, start it again and report what survived."""
    srv = SiteServer()
    root = Path(tempfile.mkdtemp(prefix="sitedata-", dir=os.environ.get("FOXGLOVE_TEST_ROOT")))
    try:
        proc = run_driver(root, "set", srv.url, how)
        if how == "restart":  # the restarted process (same pid) opens the restored tab, then is told to quit
            deadline = time.monotonic() + 45
            while srv.hits.get("/set", 0) < 2 and time.monotonic() < deadline and proc.poll() is None:
                time.sleep(0.2)
            assert srv.hits.get("/set", 0) >= 2, "Foxglove didn't come back after restarting"
            time.sleep(1.5)
            proc.send_signal(signal.SIGTERM)
        err = finish(proc)
        assert proc.returncode == 0, f"quitting via {how} failed ({proc.returncode}):\n{err[-3000:]}"
        out = root / "check.json"
        err = finish(run_driver(root, "check", srv.url, str(out)))
        assert out.exists(), f"check run failed:\n{err[-3000:]}"
        result = json.loads(out.read_text())
        result["sent"] = srv.cookies.get("/check", "")
        return result
    finally:
        srv.close()
        if not os.environ.get("FOXGLOVE_TEST_KEEP"):
            shutil.rmtree(root, ignore_errors=True)


# the window's close button, Quit (Cmd+Q), the app being told to quit (Dock, logging out), Ctrl+C / kill, the VPN's
# "Apply & Restart", and closing the window while a download runs and a pop-up is open
QUIT_PATHS = ["close", "quit_action", "app_quit", "sigterm", "sigint", "restart", "busy_close"]


@pytest.mark.parametrize("how", QUIT_PATHS)
def test_site_data_survives_quitting(how):
    result = store_and_check(how)
    sent = dict(part.strip().split("=", 1) for part in result["sent"].split(";") if "=" in part)
    survived = {"persistent cookie": sent.get("p") == "1", "session cookie": sent.get("s") == "1",
                "HttpOnly session cookie": sent.get("h") == "1", "script session cookie": sent.get("js") == "1",
                "localStorage": result.get("ls") == "1", "IndexedDB": result.get("idb") == "1",
                "Cache Storage": result.get("cache") == "1", "service worker": result.get("sw") == 1,
                "granted permission": result.get("notifications") == "Granted",
                "blocked permission": result.get("geolocation") == "Denied",
                "cookie list (site settings)": result.get("index") == ["h", "js", "p", "s"]}
    print(f"AUDIT {how}: {json.dumps(survived)}")
    assert all(survived.values()), f"lost after quitting via {how}: {[k for k, v in survived.items() if not v]}"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  In the browser: cookie index, site settings, clearing
# ══════════════════════════════════════════════════════════════════════════════════════════
from PyQt6.QtCore import QUrl  # noqa: E402

from helpers import load, poll_js, run_js, wait_until  # noqa: E402


@pytest.fixture(scope="module")
def site():
    srv = SiteServer()
    srv.port = int(srv.url.rsplit(":", 1)[1])
    yield srv
    srv.close()


def load_tab(tab, url: str) -> None:
    tab.load(QUrl(url))
    wait_until(lambda: tab.url().toString() == url and not tab.loading, 20, f"{url} to load in the tab")


def site_cookies(fg, profile, name: str) -> list[str]:
    return sorted(bytes(c.name()).decode() for c in fg.CookieIndex.of(profile).for_site(name))


def stored(harness, url: str) -> dict:
    """What /check finds in a fresh page of *url*'s origin (and the cookies it was sent)."""
    page = harness.page()
    assert load(page, url + "/check")
    result = json.loads(poll_js(page, "window.__result || ''", timeout=15, what="the check page"))
    harness.drop_page(page)
    return result


def test_site_helpers(fg):
    assert fg.site_of("accounts.google.com") == "google.com" and fg.site_of(".google.com") == "google.com"
    assert fg.site_of("www.bbc.co.uk") == "bbc.co.uk" and fg.site_of("example.com") == "example.com"
    assert fg.site_of("127.0.0.1") == "127.0.0.1" and fg.site_of("localhost") == "localhost"
    assert fg.origin_of(QUrl("https://user:pw@Example.com:443/a?b#c")) == "https://example.com"
    assert fg.origin_of(QUrl("http://127.0.0.1:8080/x")) == "http://127.0.0.1:8080"
    assert fg.origin_of(QUrl("http://[::1]:81/")) == "http://[::1]:81"
    assert fg.origin_of(QUrl("file:///tmp/x")) == "" and fg.origin_of(QUrl("foxglove://newtab")) == ""


def test_cookie_list_and_per_site_delete(window, harness, site, fg):
    profile = window.profile
    page = harness.page()
    assert load(page, f"{site.url}/cookies?a=1&b=2&pc=3")  # 127.0.0.1: two session cookies, one persistent
    assert load(page, f"http://localhost:{site.port}/cookies?c=3")  # another site
    index = fg.CookieIndex.of(profile)
    assert fg.CookieIndex.of(profile) is index  # one per profile
    wait_until(lambda: index.ready and site_cookies(fg, profile, "127.0.0.1") == ["a", "b", "pc"]
               and site_cookies(fg, profile, "localhost") == ["c"], 10, "the cookie index")
    window.show_site_settings(QUrl(site.url + "/"))
    dialog = window._dialogs["sites"]
    assert dialog.isVisible() and dialog.stack.currentIndex() == 1 and dialog.site == "127.0.0.1"
    assert dialog.origins.itemText(0) == site.url
    wait_until(lambda: dialog.cookie_list.topLevelItemCount() == 3, 5, "the site's cookies listed")
    assert dialog.cookie_heading.text() == "Cookies (3)"
    items = {dialog.cookie_list.topLevelItem(i).text(0): dialog.cookie_list.topLevelItem(i) for i in range(3)}
    assert items["a"].text(2) == "Session" and items["pc"].text(2)[:2] == "20"  # session vs persistent (a date)
    items["a"].setSelected(True)
    dialog.delete_cookies(selected=True)
    wait_until(lambda: site_cookies(fg, profile, "127.0.0.1") == ["b", "pc"], 5, "cookie a deleted")
    wait_until(lambda: dialog.cookie_list.topLevelItemCount() == 2, 5, "the list to follow")
    assert load(page, f"{site.url}/blank?1")
    assert sorted(site.cookies["/blank"].split("; ")) == ["b=2", "pc=3"]  # gone from the browser, not just the list
    dialog.delete_cookies()
    wait_until(lambda: site_cookies(fg, profile, "127.0.0.1") == [], 5, "all of the site's cookies deleted")
    assert site_cookies(fg, profile, "localhost") == ["c"]  # other sites keep theirs
    assert load(page, f"{site.url}/blank?2") and site.cookies["/blank"] == ""


def test_permissions_allow_block_ask(window, harness, site, fg):
    PT, origin = fg.QWebEnginePermission.PermissionType, site.url
    profile, tab = window.profile, window.current_tab()
    load_tab(tab, f"{site.url}/blank?perm")
    window.show_site_settings()  # the main menu's action: the current tab's site
    dialog = window._dialogs["sites"]
    assert dialog.site == "127.0.0.1" and dialog.origin == origin
    notify, camera, mic, screen = (dialog.choices[k] for k in (PT.Notifications, PT.MediaVideoCapture, PT.MediaAudioCapture,
                                                              PT.DesktopVideoCapture))
    assert [screen.itemData(i) for i in range(screen.count())] == ["ask", "block"]  # never "always share my screen"
    state = lambda kind: profile.queryPermission(QUrl(origin), kind).state().name  # noqa: E731
    for n, (choice, expected, seen) in enumerate((("allow", "Granted", "granted"), ("block", "Denied", "denied"),
                                                   ("ask", "Ask", "default"))):
        notify.setCurrentIndex(notify.findData(choice))
        assert state(PT.Notifications) == expected and window.permission_state(origin, PT.Notifications) == choice
        load_tab(tab, f"{site.url}/blank?perm{n}")  # what the site sees
        assert run_js(tab.page, "Notification.permission") == seen
    dialog.close()
    tab.page.runJavaScript("Notification.requestPermission()")
    wait_until(lambda: tab.permission_bars, 10, "the site to be asked again")
    for bar in list(tab.permission_bars):
        bar.dismiss()
    # camera and microphone: Qt asks every time, Foxglove keeps the answer
    window.show_site_settings()
    dialog = window._dialogs["sites"]
    camera, mic, screen = (dialog.choices[k] for k in (PT.MediaVideoCapture, PT.MediaAudioCapture, PT.DesktopVideoCapture))
    query = lambda kind: profile.queryPermission(QUrl(origin), kind)  # noqa: E731
    camera.setCurrentIndex(camera.findData("allow"))
    assert window.settings.get("site_permissions")[origin] == {"MediaVideoCapture": "allow"}
    assert window.remembered_decision(query(PT.MediaVideoCapture)) is True
    assert window.remembered_decision(query(PT.MediaAudioVideoCapture)) is None  # microphone still asks
    mic.setCurrentIndex(mic.findData("allow"))
    assert window.remembered_decision(query(PT.MediaAudioVideoCapture)) is True
    camera.setCurrentIndex(camera.findData("block"))
    assert window.remembered_decision(query(PT.MediaAudioVideoCapture)) is False
    screen.setCurrentIndex(screen.findData("block"))
    assert window.remembered_decision(query(PT.DesktopVideoCapture)) is False
    window.set_permission_state(origin, PT.DesktopVideoCapture, "allow")  # refused: screen sharing always asks
    assert window.permission_state(origin, PT.DesktopVideoCapture) == "ask"
    # answers given in the bar are kept too - not a dismissal (no camera here: the requests are made up)
    window.reset_site_permissions("127.0.0.1")
    assert window.settings.get("site_permissions") == {} and window.permission_decisions() == []
    bars = len(tab.permission_bars)
    window._on_permission(tab, query(PT.MouseLock))
    tab.permission_bars[-1].dismiss()
    assert window.permission_state(origin, PT.MouseLock) == "ask"
    window._on_permission(tab, query(PT.MediaAudioVideoCapture))
    assert len(tab.permission_bars) == bars + 2
    next(b for b in tab.permission_bars[-1].findChildren(fg.QPushButton) if b.text() == "Allow").click()
    assert window.permission_state(origin, PT.MediaVideoCapture) == window.permission_state(origin, PT.MediaAudioCapture) == "allow"
    window._on_permission(tab, query(PT.MediaVideoCapture))  # answered from what was kept: no bar
    assert len(tab.permission_bars) == bars + 2
    dialog._show_permissions(origin)
    assert camera.currentData() == mic.currentData() == "allow"
    # the padlock panel lists the decisions and leads to the site settings
    dialog.close()
    window._show_site_info()
    panel = wait_until(lambda: window.findChild(fg.SiteInfoPanel), 5, "the site panel")
    texts = [w.text() for w in panel.findChildren(fg.QLabel)]
    assert "Allowed to use your camera" in texts and "Allowed to use your microphone" in texts
    assert any(t.endswith("from 127.0.0.1") for t in texts)
    button = next(b for b in panel.findChildren(fg.QPushButton) if b.text() == "Site settings…")
    button.click()
    dialog = wait_until(lambda: (d := window._dialogs.get("sites")) and not fg.sip.isdeleted(d) and d.isVisible() and d, 5, "dialog")
    assert dialog.site == "127.0.0.1" and dialog.stack.currentIndex() == 1
    window.reset_site_permissions()


def test_all_sites_view_search_and_remove(window, harness, site, fg, monkeypatch):
    PT, profile = fg.QWebEnginePermission.PermissionType, window.profile
    page = harness.page()
    assert load(page, f"{site.url}/cookies?x=1")
    assert load(page, f"http://localhost:{site.port}/cookies?y=1&z=2")
    window.set_permission_state("https://example.org", PT.Geolocation, "block")
    window.set_permission_state("https://maps.example.org", PT.MediaVideoCapture, "allow")
    index = fg.CookieIndex.of(profile)
    wait_until(lambda: index.ready and site_cookies(fg, profile, "localhost") == ["y", "z"], 10, "cookies")
    window.show_site_settings(QUrl())  # Settings › Site Settings and Cookies: the list of every site
    dialog = window._dialogs["sites"]
    assert dialog.stack.currentIndex() == 0

    def rows() -> dict:
        return {dialog.sites.topLevelItem(i).text(0): (dialog.sites.topLevelItem(i).text(1), dialog.sites.topLevelItem(i).text(2))
                for i in range(dialog.sites.topLevelItemCount())}
    wait_until(lambda: rows().get("localhost") == ("2", ""), 5, "the list of sites")
    assert rows()["127.0.0.1"] == ("1", "") and rows()["example.org"] == ("0", "2")
    dialog.search.setText("LOCAL")
    assert list(rows()) == ["localhost"]
    dialog.search.setText("")
    dialog.remove_site("example.org")
    assert window.permission_state("https://example.org", PT.Geolocation) == "ask"
    assert window.permission_state("https://maps.example.org", PT.MediaVideoCapture) == "ask"
    wait_until(lambda: "example.org" not in rows(), 5, "example.org gone from the list")
    dialog.remove_site("localhost")
    wait_until(lambda: "localhost" not in rows() and site_cookies(fg, profile, "localhost") == [], 15, "localhost removed")
    assert site_cookies(fg, profile, "127.0.0.1") == ["x"]
    monkeypatch.setattr(fg, "ask_question", lambda *a, **k: True)
    dialog.remove_all()
    wait_until(lambda: not index.cookies and not rows(), 15, "everything removed")


def test_clear_site_data_removes_storage_of_that_site_only(window, harness, site, fg):
    tab = window.current_tab()
    other = f"http://localhost:{site.port}"
    for origin in (site.url, other):  # cookies, localStorage, IndexedDB, Cache Storage and a service worker on both
        load_tab(tab, f"{origin}/store")
        poll_js(tab.page, "window.__done === true", timeout=15, what="the data to be stored")
    load_tab(tab, f"{site.url}/blank?open")  # a tab of the site stays open
    before = stored(harness, site.url)
    assert (before["ls"], before["idb"], before["cache"], before["sw"]) == ("1", "1", "1", 1)
    finished = []
    cleaner = window.clear_site_data("127.0.0.1", lambda: finished.append(True))
    wait_until(lambda: finished, 40, "the site's data to be cleared")
    assert site.url in cleaner.cleared
    after = stored(harness, site.url)
    assert after == {"cookie": "", "ls": None, "idb": None, "cache": None, "sw": 0}
    assert site.cookies["/check"] == ""
    kept = stored(harness, other)
    assert (kept["ls"], kept["idb"], kept["cache"], kept["sw"]) == ("1", "1", "1", 1) and "p=1" in kept["cookie"]


def test_clear_browsing_data_clears_cookies_and_site_data(window, harness, site, fg, monkeypatch):
    tab = window.current_tab()
    load_tab(tab, f"{site.url}/store")
    poll_js(tab.page, "window.__done === true", timeout=15, what="the data to be stored")
    load_tab(tab, f"{site.url}/blank?cleared")
    dialog = fg.ClearDataDialog(window)
    assert dialog.cookies.text().startswith("Cookies and site data")
    dialog.history.setChecked(True)
    dialog.cookies.setChecked(True)
    cleaners = []
    clear = window.clear_site_data
    monkeypatch.setattr(window, "clear_site_data", lambda *a: cleaners.append(clear(*a)) or cleaners[-1])
    dialog._clear()
    assert cleaners and site.url in cleaners[0].origins  # found through history (cleared after this)
    assert not cleaners[0].thorough
    wait_until(lambda: fg.sip.isdeleted(cleaners[0]) or not cleaners[0]._workers, 40, "the clearing")
    wait_until(lambda: not fg.CookieIndex.of(window.profile).cookies, 5, "no cookies")
    after = stored(harness, site.url)
    assert (after["cookie"], after["ls"], after["idb"], after["cache"]) == ("", None, None, None)
