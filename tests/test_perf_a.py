"""Memory Saver (like Chrome's): background tabs left unused for an hour, beyond the most recently used few, give back
their memory (discarded: the renderer ends) and reload when shown - never a tab in use (pinned, shown, in a call, typed
into, with notifications, devtools, full screen, Claude at work...)."""
from __future__ import annotations

import time

from PyQt6.QtCore import QUrl
from PyQt6.QtTest import QTest
from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEnginePermission

from helpers import run_js, run_js_async, spin, wait_until
from test_extension_review import ext_page, simple_ext

S = QWebEnginePage.LifecycleState
PT = QWebEnginePermission.PermissionType


def settle(tab, url: str) -> None:
    wait_until(lambda: tab.pending is None and not tab.loading and tab.page.url().toString() == url, 20, f"{url} to load")


def open_tabs(window, server, count: int, host: str = "127.0.0.1") -> list:
    """*count* loaded background tabs on test pages (oldest first)."""
    tabs = []
    for _ in range(count):
        url = server.page_url().replace("127.0.0.1", host)
        tabs.append(window.new_tab(QUrl(url), background=True))
        settle(tabs[-1], url)
    return tabs


def age(*tabs) -> None:
    """Out of sight for two hours, the first longest."""
    now = time.monotonic()
    for i, tab in enumerate(tabs):
        tab.hidden_since = now - 2 * 3600 - (len(tabs) - i)


def states(tabs) -> list:
    return [t.page.lifecycleState() for t in tabs]


def test_discards_the_least_recently_used_beyond_the_limit_after_the_idle_time(window, server, fg, monkeypatch):
    monkeypatch.setattr(fg, "TAB_LIVE_LIMIT", 2)
    shown = window.current_tab()
    tabs = open_tabs(window, server, 4)
    updates = []  # (what tabs.onUpdated tells extensions)
    monkeypatch.setattr(window, "_tab_updated", lambda tab, change: "discarded" in change and updates.append((tab, change)))
    assert all(t.hidden_since is not None for t in tabs) and shown.hidden_since is None  # (opened in the background)
    window._sleep_tabs()
    assert states(tabs) == [S.Active] * 4  # not unused long enough
    age(*tabs[:3])
    tabs[3].hidden_since = time.monotonic() - 60
    window._sleep_tabs()
    assert states(tabs) == [S.Discarded, S.Discarded, S.Active, S.Active]  # the two used longest ago go
    assert updates == [(tabs[0], {"discarded": True}), (tabs[1], {"discarded": True})]
    window._sleep_tabs()
    assert states(tabs) == [S.Discarded, S.Discarded, S.Active, S.Active]  # 2 left: the limit
    assert shown.page.lifecycleState() == S.Active
    for tab in tabs[:2]:
        assert fg.unloaded(tab) and tab.title() == "Foxglove test page" and tab.url().path() == "/page"
        assert "history" in tab.session_entry() and not tab.crashed
    window.focus_tab(tabs[1])
    wait_until(lambda: tabs[1].page.lifecycleState() == S.Active and not tabs[1].loading, 20, "the tab to come back")
    assert updates[2:] == [(tabs[1], {"discarded": False})]
    requests = len(server.requests)
    window.close_tab(tabs[0])  # (a discarded page has no "leave page?" check to run: it goes at once, never woken)
    assert tabs[0] not in window.tabs()
    spin(0.5)
    assert server.requests[requests:] == []


def test_a_discarded_tab_reloads_when_shown_with_its_back_list(window, server, fg, monkeypatch):
    monkeypatch.setattr(fg, "TAB_LIVE_LIMIT", 0)
    first, second = server.page_url(), server.page_url()
    tab = window.new_tab(QUrl(first), background=True)
    settle(tab, first)
    tab.load(QUrl(second))
    settle(tab, second)
    age(tab)
    window._sleep_tabs()
    assert tab.page.lifecycleState() == S.Discarded
    requests = len(server.requests)
    window.focus_tab(tab)
    wait_until(lambda: tab.page.lifecycleState() == S.Active and not tab.loading and tab.page.title() == "Foxglove test page",
               20, "the tab to come back")
    assert tab.url().toString() == second and tab.hidden_since is None and len(server.requests) > requests
    assert window.back_button.isEnabled()
    window.back_button.click()
    settle(tab, first)


def test_tabs_in_use_stay(window, server, fg, monkeypatch):
    monkeypatch.setattr(fg, "TAB_LIVE_LIMIT", 0)
    a, b = open_tabs(window, server, 2)
    window.focus_tab(a)
    window.add_tab_to_split(b)  # both sides of the split view are shown
    pinned, devtools, call, typed, plain = open_tabs(window, server, 5)
    window.set_pinned(pinned, True)
    devtools.open_devtools()
    notifying = open_tabs(window, server, 1, host="localhost")[0]
    window.profile.queryPermission(QUrl(fg.origin_of(notifying.url())), PT.Notifications).grant()
    origin = fg.origin_of(call.url())
    window.set_permission_state(origin, PT.MediaVideoCapture, "allow")  # a call: the camera was given (remembered)
    window._on_permission(call, window.profile.queryPermission(QUrl(origin), PT.MediaVideoCapture))
    assert call.keep_awake == origin
    window.focus_tab(typed)
    QTest.keyClicks(typed.view.focusProxy(), "draft")
    assert typed.typed
    window.focus_tab(a)
    data = window.new_tab(QUrl("data:text/html,<title>data</title>"), background=True)  # (not a website)
    wait_until(lambda: not data.loading, 10, "the data: page")
    kept = [a, b, pinned, devtools, call, typed, notifying, data]
    age(*kept, plain)
    monkeypatch.setattr(window, "agent_running", lambda: True)  # Claude at work: nothing goes
    window._sleep_tabs()
    assert states(kept + [plain]) == [S.Active] * 9
    monkeypatch.setattr(window, "agent_running", lambda: False)
    window.settings.set("memory_saver", False)  # turned off in Settings
    window._sleep_tabs()
    assert states(kept + [plain]) == [S.Active] * 9
    window.settings.set("memory_saver", True)
    window._sleep_tabs()
    assert states(kept) == [S.Active] * 8 and plain.page.lifecycleState() == S.Discarded
    call.load(QUrl(fg.NEWTAB))  # the call's site is gone: it may go
    wait_until(lambda: not call.keep_awake and not call.loading, 10, "the call to end")
    typed.load(QUrl(server.page_url()))  # a new page: nothing typed in it
    wait_until(lambda: not typed.typed and not typed.loading, 10, "the new page")
    age(call, typed)
    window._sleep_tabs()
    assert call.page.lifecycleState() == typed.page.lifecycleState() == S.Discarded


def test_an_opener_with_its_pop_up_open_stays(window, server, fg, monkeypatch):
    monkeypatch.setattr(fg, "TAB_LIVE_LIMIT", 0)
    opener = open_tabs(window, server, 1)[0]
    run_js(opener.page, f"window.open({server.page_url()!r}, 'pop', 'popup'); true")  # (a sign-in window, say)
    wait_until(lambda: window.popups, 10, "the pop-up")
    seen = []
    opener.page.lifecycleStateChanged.connect(seen.append)
    age(opener)
    before = time.monotonic()
    window._sleep_tabs()
    assert opener.page.lifecycleState() == S.Active  # (Qt: its pop-up may still talk to it)
    assert not seen and opener.hidden_since >= before  # Qt's answer came from a probe nobody saw; asked again in an hour


def test_clearing_site_data_leaves_discarded_tabs_asleep(window, server, fg, monkeypatch):
    monkeypatch.setattr(fg, "TAB_LIVE_LIMIT", 1)
    asleep, awake = open_tabs(window, server, 2)
    age(asleep, awake)
    window._sleep_tabs()
    assert states([asleep, awake]) == [S.Discarded, S.Active]
    requests, done = len(server.requests), []
    window.clear_site_data("127.0.0.1", lambda: done.append(True))
    wait_until(lambda: done, 40, "the site's data to be cleared")
    path = lambda tab: tab.url().path() + "?" + tab.url().query()  # noqa: E731
    wait_until(lambda: path(awake) in server.requests[requests:] and not awake.loading, 20, "the open tab to reload")
    assert asleep.page.lifecycleState() == S.Discarded  # (it reloads when shown: no proxy data spent on it now)
    assert path(asleep) not in server.requests[requests:]


def test_hidden_since_follows_the_tab_shown(window, server):
    a, b = open_tabs(window, server, 2)
    window.focus_tab(a)
    assert a.hidden_since is None and b.hidden_since is not None
    before = time.monotonic()
    window.focus_tab(b)
    assert b.hidden_since is None and a.hidden_since >= before


def test_settings_switch(window, fg):
    assert window.settings.get("memory_saver") is True  # on by default, like Chrome
    dialog = fg.SettingsDialog(window)
    assert dialog.memory_saver.isChecked()
    dialog.memory_saver.setChecked(False)
    assert window.settings.get("memory_saver") is False
    dialog.close()
    dialog.deleteLater()


CS = """chrome.runtime.onMessage.addListener((m, s, reply) => { reply('pong ' + m); });
document.documentElement.setAttribute('data-cs', 'ready');
"""


def test_extensions_see_a_discarded_tab_as_unloaded(window, harness, server, fg, monkeypatch, tmp_path):
    monkeypatch.setattr(fg, "TAB_LIVE_LIMIT", 0)
    entry = harness.install_ok(simple_ext(tmp_path / "ms", "Sleeper", {"cs.js": CS}, permissions=["storage", "scripting"],
                                          host_permissions=["http://127.0.0.1/*"],
                                          content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}]), "Sleeper")
    tab = open_tabs(window, server, 1)[0]
    wait_until(lambda: run_js(tab.page, "document.documentElement.getAttribute('data-cs')") == "ready", 10, "the content script")
    page = ext_page(harness, entry)
    send = f"return await chrome.tabs.sendMessage({tab.tab_id}, 'ping').then((r) => r, (e) => 'ERR ' + e.message)"
    assert run_js_async(page, send) == "pong ping"
    get = f"const t = await chrome.tabs.get({tab.tab_id}); return [t.status, t.discarded]"
    assert run_js_async(page, get) == ["complete", False]
    run = (f"return await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, func: () => 6 * 7}})"
           ".then((r) => r[0].result, (e) => 'ERR ' + e.message)")
    assert run_js_async(page, run) == 42
    age(tab)
    window._sleep_tabs()
    assert tab.page.lifecycleState() == S.Discarded
    assert run_js_async(page, get) == ["unloaded", True]
    assert run_js_async(page, send) == "ERR Could not establish connection. Receiving end does not exist."
    assert run_js_async(page, run) == "ERR Cannot access contents of a tab that hasn't been loaded yet."
    assert tab not in harness.controller.bridge.script_tabs(entry.id)
    window.focus_tab(tab)  # back: the content script runs again
    wait_until(lambda: tab.page.lifecycleState() == S.Active and not tab.loading, 20, "the tab to come back")
    wait_until(lambda: run_js(tab.page, "document.documentElement.getAttribute('data-cs')") == "ready", 10, "the content script")
    assert run_js_async(page, send) == "pong ping"
