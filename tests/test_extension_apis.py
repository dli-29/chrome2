"""Foxglove's chrome.* polyfill: the APIs Qt WebEngine lacks (or crashes on), driven through real extensions.

Every installed extension gets foxglove-shim.js wired in at install time; calls that need the browser reach
ExtensionBridge over foxglove-ext://. These tests install an extension that uses those APIs the way popular
extensions do (at the service worker's top level) and check what the user - and the extension - sees.
"""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtGui import QColor
from PyQt6.QtWebEngineCore import QWebEngineContextMenuRequest

import extbuilder as eb
from helpers import WaitTimeout, poll_js, run_js, run_js_async, spin, wait_attr, wait_until

NAME = "Kitchen Sink"


def wait_record(page, kind: str, timeout: float = 15.0, unlike=None):
    """What the test extension's worker stored under "ev:<kind>" (it records what it saw there)."""
    deadline = time.monotonic() + timeout
    while True:
        value = run_js_async(page, f"const r = await chrome.storage.local.get('ev:{kind}');"
                                   f" return r['ev:{kind}'] === undefined ? '__missing__' : r['ev:{kind}']")
        if value != "__missing__" and value != unlike:
            return value
        if time.monotonic() > deadline:
            raise WaitTimeout(f"the extension never recorded {kind!r} (last: {value!r})")
        spin(0.2)


def install_sink(harness, tmp_path, **kw):
    entry = harness.install_ok(eb.api_extension(tmp_path / "sink", **kw), NAME)
    page = harness.fresh_page(entry.options_url.toString())
    assert wait_attr(page, "data-options") == "ready"
    return entry, page


def load_tab(tab, url: str) -> None:
    tab.load(QUrl(url))
    wait_until(lambda: tab.url().toString() == url and not tab.loading, 20, f"{url} to load in the tab")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Service worker: top-level calls, i18n, menus, alarms, onInstalled
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("module", [False, True], ids=["classic-worker", "module-worker"])
def test_worker_survives_top_level_calls_to_missing_apis(harness, server, tmp_path, module):
    """chrome.action/contextMenus/alarms at the worker's top level used to throw and kill the worker."""
    entry, page = install_sink(harness, tmp_path, module=module)
    start = wait_record(page, "start")
    assert start == {"util": True, "greet": "Hello Ann, costs $5", "name": NAME, "dollars": "$9.99", "ui": "en-US", "idOk": True}
    assert wait_record(page, "installed") == {"reason": "install"}
    assert wait_record(page, "dup") == "Cannot create item with duplicate id look"
    bridge = harness.controller.bridge
    assert [m["id"] for m in bridge.menus(entry.id)] == ["look", "page-item"]
    assert bridge.action_value(entry.id, "badgeText") == "7" and bridge.action_value(entry.id, "title") == "Seven things"
    assert wait_record(page, "alarm", 20) == "tick"
    assert run_js_async(page, "return await chrome.alarms.getAll()") == []  # a one-off alarm is gone once it fired
    web = harness.fresh_page(server.page_url())
    assert wait_attr(web, "data-sink-ran") == "yes"
    assert json.loads(wait_attr(web, "data-sink-hello")) == {"tab": None}  # not a Foxglove tab: no tab id
    assert wait_attr(web, "data-sink-i18n") == "Hello Cy, costs $5"
    assert wait_attr(web, "data-sink-war") == "20 foxglove-ext"  # web_accessible_resources, served by Foxglove
    assert wait_attr(web, "data-sink-css") == f'url("foxglove-ext://{entry.id}/img/pic.png")'  # __MSG_@@extension_id__
    with pytest.raises(WaitTimeout):
        wait_record(page, "leak", 1)  # the polyfill's own messages never reach the extension's listeners
    assert run_js(page, "chrome.i18n.getMessage('extName')") == NAME  # extension pages too


def test_on_installed_reports_updates(harness, tmp_path):
    src = eb.api_extension(tmp_path / "sink")
    entry = harness.install_ok(src, NAME)
    page = harness.fresh_page(entry.options_url.toString())
    assert wait_record(page, "installed") == {"reason": "install"}
    harness.drop_page(page)
    eb.write_tree(src, eb.api_manifest(version="2.0"))
    kind, text = harness.install(src)
    assert kind == "success" and text == f"“{NAME}” was updated.", text
    wait_until(lambda: (e := harness.entry(entry.id)) is not None and e.version == "2.0" and e.enabled, message="v2")
    page = harness.fresh_page(entry.options_url.toString())
    assert wait_record(page, "installed", unlike={"reason": "install"}) == {"reason": "update", "previousVersion": "1.0"}


def test_storage_sync_and_change_events(window, harness, server, tmp_path):
    entry, page = install_sink(harness, tmp_path)
    wait_record(page, "start")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    assert wait_attr(tab.page, "data-sink-ran") == "yes"
    run_js_async(page, "await chrome.storage.sync.set({theme: 'dark'}); return true")
    assert run_js_async(page, "return await chrome.storage.sync.get('theme')") == {"theme": "dark"}
    assert "theme" not in run_js_async(page, "return await chrome.storage.local.get(null)")  # sync is kept apart
    assert wait_record(page, "sync") == {"theme": {"newValue": "dark"}}  # the worker heard it
    assert json.loads(wait_attr(tab.page, "data-sink-sync")) == {"theme": {"newValue": "dark"}}  # and the content script


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The browser side: toolbar badge, action.onClicked, scripting, tabs, context menus, notifications
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_badge_and_click_reach_the_toolbar_and_worker(window, harness, server, tmp_path):
    entry, page = install_sink(harness, tmp_path)
    button = wait_until(lambda: window.extension_button(entry.id), message="toolbar button")
    wait_until(lambda: button.badge == "7" and button.toolTip() == "Seven things", message="badge and title from chrome.action")
    assert button.badge_colors[0] == QColor(0, 128, 0)
    wait_until(lambda: harness.controller.bridge.listens(entry.id, "action.onClicked"), message="the worker's click listener")
    tab = window.current_tab()
    load_tab(tab, server.url("/page?click"))
    hello = json.loads(wait_attr(tab.page, "data-sink-hello"))
    assert hello == {"tab": tab.tab_id, "frame": 0}  # sender.tab, filled in by the polyfill
    button.click()  # no pop-up: chrome.action.onClicked
    out = wait_record(page, "clicked")
    assert out["tab"] == tab.tab_id and out["url"] == server.url("/page?click")
    assert out["exec"] == "Foxglove test page|x" and out["asyncExec"] == 42
    assert out["reply"] == {"pong": "ping", "title": "Foxglove test page"}  # tabs.sendMessage -> content script
    assert out["missing"] == "Could not establish connection. Receiving end does not exist."
    assert poll_js(tab.page, "getComputedStyle(document.body).borderTopColor", lambda v: v == "rgb(1, 2, 3)")  # insertCSS


def test_per_tab_badge_follows_the_current_tab(window, harness, tmp_path):
    entry, page = install_sink(harness, tmp_path)
    button = wait_until(lambda: window.extension_button(entry.id) if window.extension_button(entry.id) and
                        window.extension_button(entry.id).badge == "7" else None, message="badge")
    first = window.current_tab()
    second = window.new_tab(QUrl("about:blank"), background=True)
    run_js_async(page, f"await chrome.action.setBadgeText({{tabId: {second.tab_id}, text: 'tab'}}); return true")
    assert run_js_async(page, f"return await chrome.action.getBadgeText({{tabId: {second.tab_id}}})") == "tab"
    assert button.badge == "7"
    window.tab_bar.setCurrentIndex(window.index_of(second))
    wait_until(lambda: button.badge == "tab", message="the second tab's badge")
    window.tab_bar.setCurrentIndex(window.index_of(first))
    wait_until(lambda: button.badge == "7", message="the global badge again")


class FakeMenuRequest:
    """What BrowserWindow reads from a QWebEngineContextMenuRequest (Qt only makes real ones on a real right-click)."""

    def __init__(self, selection: str = "", link: str = ""):
        self.selection, self.link = selection, link

    def linkUrl(self): return QUrl(self.link)
    def mediaUrl(self): return QUrl()
    def selectedText(self): return self.selection
    def isContentEditable(self): return False
    def mediaType(self): return QWebEngineContextMenuRequest.MediaType.MediaTypeNone


def menu_texts(menu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


def test_context_menu_items_reach_the_worker(window, harness, server, tmp_path, fg):
    entry, page = install_sink(harness, tmp_path)
    wait_until(lambda: len(harness.controller.bridge.menus(entry.id)) == 2, message="menu items")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    menu = fg.Menu("", window)
    window._extension_menu_items(menu, tab, FakeMenuRequest())
    assert menu_texts(menu) == ["Sink page item"]  # "page" context only
    menu = fg.Menu("", window)
    window._extension_menu_items(menu, tab, FakeMenuRequest(selection="plain page"))
    assert menu_texts(menu) == ["Look up “plain page”"]  # %s -> the selection, "selection" context only
    next(a for a in menu.actions() if a.text() == "Look up “plain page”").trigger()
    assert wait_record(page, "menu") == {"id": "look", "selection": "plain page", "tab": tab.tab_id}


def test_notification_shows_and_its_click_reaches_the_worker(window, harness, tmp_path):
    entry, page = install_sink(harness, tmp_path)
    wait_record(page, "start")
    assert run_js_async(page, "return await chrome.runtime.sendMessage({type: 'notify'})") == "n1"
    toast = window.content.toast
    wait_until(lambda: toast.isVisible() and toast.label.text() == f"{NAME}: Hi — There", message="the notification")
    toast.mousePressEvent(None)
    assert wait_record(page, "note") == "n1"


def test_tabs_and_windows_from_an_extension_page(window, harness, server, tmp_path):
    entry, page = install_sink(harness, tmp_path)
    current = window.current_tab()
    active = run_js_async(page, "return await chrome.tabs.query({active: true, currentWindow: true})")
    assert [t["id"] for t in active] == [current.tab_id]
    url = server.url("/page?created")
    created = run_js_async(page, f"return await chrome.tabs.create({{url: {json.dumps(url)}, active: false}})")
    tab = wait_until(lambda: next((t for t in window.tabs() if t.tab_id == created["id"]), None), message="the new tab")
    assert window.current_tab() is current
    wait_until(lambda: tab.url().toString() == url, message="the new tab's page")
    found = run_js_async(page, "return await chrome.tabs.query({url: 'http://127.0.0.1/*created*'})")
    assert [t["id"] for t in found] == [tab.tab_id] and found[0]["title"] in ("Foxglove test page", url)
    run_js_async(page, f"await chrome.tabs.update({tab.tab_id}, {{active: true}}); return true")
    assert window.current_tab() is tab
    win = run_js_async(page, "return await chrome.windows.getCurrent({populate: true})")
    assert win["id"] == 1 and win["type"] == "normal" and tab.tab_id in [t["id"] for t in win["tabs"]]
    run_js_async(page, f"await chrome.tabs.remove({tab.tab_id}); return true")
    wait_until(lambda: tab not in window.tabs(), message="the tab to close")
    count = window.tab_bar.count()
    run_js_async(page, "await chrome.runtime.openOptionsPage(); return true")
    options = wait_until(lambda: window.tab_bar.count() == count + 1 and window.current_tab(), message="options tab")
    assert options.url() == entry.options_url
    with pytest.raises(AssertionError, match="No tab with id: 424242"):
        run_js_async(page, "return await chrome.tabs.get(424242)")


def test_keyboard_commands(window, harness, server, tmp_path, fg):
    entry, page = install_sink(harness, tmp_path)
    wait_until(lambda: harness.controller.bridge.listens(entry.id, "commands.onCommand"), message="the command listener")
    shortcuts = {a.shortcut().toString(): a for a in window._command_actions}
    assert set(shortcuts) == {"Alt+Shift+K", "Ctrl+Shift+U"}
    assert run_js_async(page, "return (await chrome.commands.getAll()).map((c) => c.name + '=' + c.shortcut)") == \
        ["_execute_action=Alt+Shift+K", "toggle-thing=Ctrl+Shift+U"]
    shortcuts["Ctrl+Shift+U"].trigger()
    assert wait_record(page, "command") == {"name": "toggle-thing", "tab": window.current_tab().tab_id}


def test_active_tab_access_comes_from_the_user(window, harness, server, tmp_path):
    """With "activeTab" (no host permissions) scripting works on a tab only after the user invoked the extension."""
    manifest = eb.probe_manifest("Clicker", "clicker", popup=False, options="options_page", permissions=["activeTab", "scripting"])
    del manifest["content_scripts"]
    entry = harness.install_ok(eb.write_tree(tmp_path / "clicker", manifest, eb.probe_files("Clicker", "clicker")), "Clicker")
    page = harness.fresh_page(entry.options_url.toString())
    tab = window.current_tab()
    load_tab(tab, server.url("/page?active-tab"))
    script = f"return (await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, func: () => document.title}}))[0].result"
    with pytest.raises(AssertionError, match="Cannot access contents of the page"):
        run_js_async(page, script)
    assert "url" not in run_js_async(page, f"return await chrome.tabs.get({tab.tab_id})")
    window.open_extension(entry.id)  # the toolbar button
    assert run_js_async(page, script) == "Foxglove test page"
    assert run_js_async(page, f"return (await chrome.tabs.get({tab.tab_id})).url") == server.url("/page?active-tab")
    with pytest.raises(AssertionError, match="needs the “downloads” permission"):
        run_js_async(page, "return await fetch('foxglove-ext://bridge/call', {method: 'POST', body: JSON.stringify("
                           "{api: 'downloads.download', args: {url: 'https://example.com/'}})}).then((r) => r.json())"
                           ".then((r) => { if (!r.ok) throw new Error(r.error); return r; })")


def test_tab_events_wake_the_worker(window, harness, server, tmp_path):
    entry, page = install_sink(harness, tmp_path)
    wait_until(lambda: harness.controller.bridge.listens(entry.id, "tabs.onUpdated"), message="the onUpdated listener")
    tab = window.current_tab()
    load_tab(tab, server.url("/page?tabevent"))
    assert wait_record(page, "updated") == {"tabId": tab.tab_id, "url": server.url("/page?tabevent")}


def test_popup_window_from_chrome_windows_create(window, harness, tmp_path, fg):
    entry, page = install_sink(harness, tmp_path)
    url = entry.options_url.toString()
    info = run_js_async(page, f"return await chrome.windows.create({{url: {json.dumps(url)}, type: 'popup', width: 420, height: 300}})")
    popup = wait_until(lambda: next((p for p in window.popups if p.window_id == info["id"]), None), message="the pop-up window")
    assert info["type"] == "popup" and info["tabs"][0]["id"] == popup.tab_id
    wait_until(lambda: popup.url().toString() == url, message="the extension page in it")
    assert poll_js(popup.page, "typeof chrome.storage.local.get") == "function"
    run_js_async(page, f"await chrome.windows.remove({info['id']}); return true")
    wait_until(lambda: popup not in window.popups, message="the pop-up window to close")


def test_offscreen_dnr_and_stand_ins_dont_crash(harness, tmp_path):
    """offscreen.createDocument and declarativeNetRequest.updateSessionRules crash Qt WebEngine 6.11 outright."""
    entry, page = install_sink(harness, tmp_path)
    wait_record(page, "start")
    assert run_js_async(page, "return await chrome.runtime.sendMessage({type: 'offscreen'})") == {"has": True, "contexts": 1}
    assert wait_record(page, "offscreen") == NAME  # the offscreen document runs with the extension's APIs
    rules = run_js_async(page, "return await chrome.runtime.sendMessage({type: 'dnr'})")
    assert [r["id"] for r in rules] == [1]
    # cookies and bookmarks: stand-ins / real lists; webNavigation is real: no window here, so no tab 1 (null, as in Chrome)
    assert run_js_async(page, "return await chrome.runtime.sendMessage({type: 'stubs'})") == [True, True, False]
    assert run_js(page, "chrome.runtime.getManifest().background.service_worker") == "js/sw.js"  # the original


def bridge_call(page, api: str, args: dict):
    """A call straight to Foxglove's bridge, past the polyfill's own checks (a user gesture, for one)."""
    return run_js_async(page, f"""const r = await (await fetch('foxglove-ext://bridge/call', {{method: 'POST', body: JSON.stringify(
        {{api: {json.dumps(api)}, args: {json.dumps(args)}, from: location.href}})}})).json();
      if (!r.ok) throw new Error(r.error); return r.value ?? null;""")


def test_permissions_api(window, harness, tmp_path, fg, monkeypatch):
    extra = {"optional_permissions": ["downloads"], "optional_host_permissions": ["https://example.com/*"]}
    entry, page = install_sink(harness, tmp_path, manifest_extra=extra)
    assert run_js_async(page, "return await chrome.permissions.contains({permissions: ['tabs']})") is True
    assert run_js_async(page, "return await chrome.permissions.contains({permissions: ['downloads']})") is False
    with pytest.raises(AssertionError, match="user gesture"):  # as in Chrome (test_extension_hardening clicks for one)
        run_js_async(page, "return await chrome.permissions.request({permissions: ['downloads']})")
    with pytest.raises(AssertionError, match="Only permissions specified in the manifest"):
        bridge_call(page, "permissions.request", {"permissions": ["history"]})
    asked = []
    monkeypatch.setattr(fg, "ask_question", lambda *a, **k: asked.append(a[2]) or True)
    assert bridge_call(page, "permissions.request", {"permissions": ["downloads"], "origins": ["https://example.com/*"]}) is True
    assert asked and "downloads" in asked[0]
    assert run_js_async(page, "return await chrome.permissions.contains({origins: ['https://example.com/*']})") is True
    assert harness.controller.bridge.can_access(entry.id, "https://example.com/x")


def test_web_pages_cant_use_the_bridge(harness, server, tmp_path):
    install_sink(harness, tmp_path)
    web = harness.fresh_page(server.page_url())
    result = run_js_async(web, "try { const r = await fetch('foxglove-ext://bridge/call', {method: 'POST', body: JSON.stringify("
                               "{api: 'tabs.query', args: {}}) }); return 'status ' + r.status; } catch (e) { return 'blocked'; }")
    assert result in ("blocked", "status 403"), result  # refused: a web page isn't an extension


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Installs from older Foxglove versions get the polyfill (same ID, same data)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_old_installs_get_the_polyfill_without_changing_id(server, tmp_path):
    from test_extensions import _run_phase  # the restart machinery (one process per browser start)
    src = eb.api_extension(tmp_path / "sink")
    args = {"root": str(tmp_path / "run"), "profile": "legacy", "page_url": server.url("/page?legacy"),
            "exts": [{"name": NAME, "tag": "sink", "path": str(src)}], "storage": {NAME: "kept"},
            "enabled": [NAME], "old": True, "records": ["installed", "start"]}
    before = _run_phase("install", args)
    path = Path(before["entries"][NAME]["path"])
    assert not (path / "foxglove-shim.js").exists()  # installed the way older Foxglove versions did
    after = _run_phase("report", args)
    assert "wait_error" not in after, after
    assert after["entries"][NAME]["id"] == before["entries"][NAME]["id"] and after["entries"][NAME]["path"] == str(path)
    assert (path / "foxglove-shim.js").exists() and after["shimmed"] == {NAME: True}
    assert after["storage"] == {NAME: "kept"}
    assert after["records"][NAME]["installed"] == {"reason": "chrome_update"}  # Qt never sent onInstalled before
    assert after["records"][NAME]["start"]["greet"] == "Hello Ann, costs $5"
    assert not [m for m in after["messages"] if m[0] == "error"], after["messages"]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Pure helpers
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("pattern, url, expected", [
    ("<all_urls>", "https://a.example/x", True),
    ("<all_urls>", "chrome-extension://abc/x", False),
    ("*://*/*", "http://a.example/", True),
    ("*://*/*", "file:///etc/hosts", False),
    ("https://*.example.com/*", "https://www.example.com/a", True),
    ("https://*.example.com/*", "https://example.com/", True),
    ("https://*.example.com/*", "https://badexample.com/", False),
    ("http://127.0.0.1/*", "http://127.0.0.1:8080/page?x", True),
    ("https://example.com/foo*", "https://example.com/foobar?q=1", True),
    ("https://example.com/foo*", "https://example.com/bar", False),
    ("file:///*", "file:///tmp/a.html", True),
    ("not a pattern", "https://example.com/", False),
])
def test_match_patterns(fg, pattern, url, expected):
    assert fg.match_pattern(pattern, url) is expected


def test_messages_follow_chromes_locale_order(fg, tmp_path):
    root = eb.api_extension(tmp_path / "sink")
    manifest = json.loads((root / "manifest.json").read_text())
    de = fg.resolve_messages(root, manifest, "de_DE")
    assert de["extname"] == f"{NAME} (de)" and de["greet"] == "Hello $1, costs $$5"  # missing in de: from en
    assert fg.resolve_messages(root, manifest, "en_US")["extname"] == NAME


def test_csp_lets_the_polyfill_reach_foxglove(fg):
    assert fg.allow_scheme_in_csp("script-src 'self'; connect-src 'self'", "foxglove-ext") == \
        "script-src 'self'; connect-src 'self' foxglove-ext:"
    assert fg.allow_scheme_in_csp("default-src 'none'", "foxglove-ext") == "default-src 'none'; connect-src foxglove-ext:"
    assert fg.allow_scheme_in_csp("script-src 'self'", "foxglove-ext") == "script-src 'self'"


def test_shim_wiring_is_idempotent(fg, tmp_path):
    root = eb.api_extension(tmp_path / "sink")
    shutil.copy(root / "options.html", root / "plain.html")
    for _ in range(2):  # an installed copy getting a newer shim goes through this again
        manifest = fg.load_manifest(root)
        fg.inject_shim(root, manifest, "0" * 32, "en_US")
    manifest = json.loads((root / "manifest.json").read_text())
    assert manifest["background"]["service_worker"] == "js/foxglove-worker.js"
    assert (root / "js" / "foxglove-worker.js").read_text() == 'importScripts("/foxglove-shim.js", "sw.js");\n'
    assert manifest["content_scripts"][0]["js"] == ["foxglove-shim.js", "cs.js"]
    assert manifest["content_security_policy"]["extension_pages"].endswith("connect-src 'self' foxglove-ext:")
    assert (root / "options.html").read_text().count('<script src="/foxglove-shim.js"></script>') == 1
    assert json.loads((root / "foxglove-manifest.json").read_text())["background"]["service_worker"] == "js/sw.js"
    config = fg.installed_shim_config(root)
    assert config["worker"] and config["cs"] and config["war"] == ["img/*.png"] and config["messages"]["extname"] == NAME
    assert (root / "foxglove-shim.js").read_text().startswith(f"/* foxglove-shim {fg.shim_stamp('0' * 32, 'en_US')}\n")


@pytest.mark.parametrize("value, expected", [
    ([255, 0, 0, 255], [255, 0, 0, 255]), ([1, 2, 3], [1, 2, 3, 255]), ("#00ff00", [0, 255, 0, 255]),
    ("rgba(10, 20, 30, 0.5)", [10, 20, 30, 127]), ("red", [255, 0, 0, 255]), ("not a colour", None)])
def test_badge_colours(fg, value, expected):
    assert fg._color(value) == expected
