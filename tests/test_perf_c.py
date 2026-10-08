"""Extension bookkeeping that used to grow with extensions x tabs: the 'changed' storm at start-up is one emission per
event-loop pass (the user's own switch still shows at once), the extension list is listed once per pass and forgotten
when Qt changes it, chrome.tabs.query builds only the tabs an active query can match, a storage.onChanged relay is
encoded once for all tabs, and tabs.sendMessage / cookies.set free their one-off timers."""
from __future__ import annotations

import json
import time
from types import SimpleNamespace

from PyQt6.QtCore import QRect, QTimer, QUrl

import extbuilder as eb
from helpers import spin, wait_attr, wait_until
from test_extensions import toolbar_buttons


def install_probes(harness, tmp_path, count: int, **manifest_extra) -> list:
    return [harness.install_ok(eb.probe_extension(tmp_path / f"p{i}", f"Probe {i}", f"p{i}", **manifest_extra), f"Probe {i}")
            for i in range(count)]


def settled(harness) -> None:
    """Past the controller's own start-up syncs (0 and 2500 ms) and the installs' follow-ups."""
    spin(max(0.2, 2.8 - (time.monotonic() - harness.created)))


def count_changed(controller) -> list:
    emits: list = []
    controller.changed.connect(lambda: emits.append(1))
    return emits


def blank_tabs(window, count: int) -> list:
    tabs = [window.new_tab(QUrl("about:blank"), background=True) for _ in range(count)]
    wait_until(lambda: all(not t.loading and t.pending is None for t in tabs), 10, "blank tabs")
    return tabs


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The 'changed' storm
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_startup_sync_emits_changed_once_and_wires_each_tab_once(window, harness, tmp_path, monkeypatch):
    c = harness.controller
    entries = install_probes(harness, tmp_path, 3)
    tabs = blank_tabs(window, 10)
    settled(harness)
    emits = count_changed(c)
    wired: list = []
    real_wire = c.wire_tab
    monkeypatch.setattr(c, "wire_tab", lambda page, tab_id, pairs=None: wired.append(tab_id) or real_wire(page, tab_id, pairs))
    c._sync_enabled()
    assert emits == []  # one pass later, once for all three (each emission rewires every tab)
    spin(0.1)
    assert len(emits) == 1
    assert sorted(wired) == sorted(t.tab_id for t in window.tabs())
    assert len(toolbar_buttons(window)) == 3
    names = [c.shim_config(e.id)["tabQuery"] for e in entries]
    for tab in tabs:
        found = tab.page.scripts().find(c.TAB_SCRIPT)
        assert len(found) == 1
        assert f"'{tab.tab_id}'" in found[0].sourceCode() and all(n in found[0].sourceCode() for n in names)
    # Switched off at start-up (the registry says so): its request rules go at once, the toolbar a pass later.
    invalidated: list = []
    monkeypatch.setattr(c.net, "invalidate", lambda: invalidated.append(1))
    emits.clear()
    c.registry[entries[0].id]["enabled"] = False
    c._apply_enabled(entries[0].id)
    assert invalidated == [1] and not c._info(entries[0].id).isEnabled()
    assert emits == [] and len(toolbar_buttons(window)) == 3
    spin(0.05)
    assert len(emits) == 1 and len(toolbar_buttons(window)) == 2
    # The user's own switch: the toolbar (and the extensions dialog) follow before set_enabled returns.
    c.set_enabled(entries[0].id, True)
    assert len(emits) == 2 and len(toolbar_buttons(window)) == 3 and c._info(entries[0].id).isEnabled()
    spin(0.05)
    assert len(emits) == 2


def test_extensions_loaded_in_one_pass_are_switched_on_with_one_changed(window, harness, tmp_path):
    c = harness.controller
    entries = install_probes(harness, tmp_path, 2)
    settled(harness)
    infos = [c._info(e.id) for e in entries]
    for info in infos:
        c.manager.setExtensionEnabled(info, False)  # as Qt loads them at start-up: off
    assert not any(i.isEnabled() for i in infos)
    emits = count_changed(c)
    for info in infos:
        c._on_load_finished(info)
    assert c._loaded_ids == [e.id for e in entries] and not any(i.isEnabled() for i in infos)  # (a pass later, together)
    spin(0.2)
    assert len(emits) == 1 and all(i.isEnabled() for i in infos) and c._loaded_ids == []
    assert len(toolbar_buttons(window)) == 2


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The extension list: once per pass
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_extension_list_is_listed_once_per_pass_and_forgotten_when_qt_changes_it(window, harness, tmp_path, monkeypatch):
    c = harness.controller
    entries = install_probes(harness, tmp_path, 3)
    for e in entries:
        c.registry[e.id]["listeners"] = ["tabs.onUpdated"]
    heard: list = []
    monkeypatch.setattr(c.bridge, "_run_in_page", lambda ext_id, script: heard.append(ext_id) or True)
    settled(harness)
    listed: list = []
    real = c.manager.extensions
    monkeypatch.setattr(c.manager, "extensions", lambda: listed.append(1) or real())
    assert c._info_memo is None
    window._tab_updated(window.current_tab(), {"status": "complete"})  # three listeners, each asking about itself a few times
    assert len(listed) == 1 and sorted(heard) == sorted(e.id for e in entries)
    assert c._infos() is c._infos() and c._info_memo is not None
    spin(0.05)
    assert c._info_memo is None  # forgotten by the next pass
    # The infos are live: a switch within the pass shows at once.
    c.set_enabled(entries[0].id, False)
    assert c._info(entries[0].id) is not None and not c._info(entries[0].id).isEnabled()
    assert set(c.bridge.listeners("tabs.onUpdated")) == {e.id for e in entries[1:]}
    # Qt's signals forget the list before anything else (an install's check, an uninstall), not a pass later.
    fake = SimpleNamespace(id=lambda: "none", path=lambda: "/nowhere", isLoaded=lambda: False, isInstalled=lambda: False,
                           error=lambda: "")
    c._rejected.add("/nowhere")
    for handler in (c._on_load_finished, c._on_install_finished, c._on_uninstall_finished, c._on_unload_finished):
        c._infos()
        assert c._info_memo is not None
        handler(fake)
        assert c._info_memo is None
    c._rejected.discard("/nowhere")
    # Two listed under one id (an update's moment): the first, as before.
    first = SimpleNamespace(id=lambda: "dup", isInstalled=lambda: True, path=lambda: "/a")
    second = SimpleNamespace(id=lambda: "dup", isInstalled=lambda: True, path=lambda: "/b")
    monkeypatch.setattr(c.manager, "extensions", lambda: [first, second])
    c._forget_infos()
    assert c._info("dup") is first and c._infos() == [first, second] and c._info("") is None
    monkeypatch.setattr(c.manager, "extensions", real)
    c._forget_infos()
    gone = entries[2].id
    c.uninstall(gone)
    wait_until(lambda: c._info(gone) is None, 15, "the extension to go")
    assert gone not in c.bridge.listeners("tabs.onUpdated")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  chrome.tabs.query
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_tabs_query_builds_only_the_tabs_an_active_query_can_match(window, harness, tmp_path, fg, monkeypatch):
    c = harness.controller
    entry = install_probes(harness, tmp_path, 1, permissions=["storage", "tabs"])[0]
    bridge = c.bridge
    tabs = blank_tabs(window, 4)
    window.focus_tab(tabs[1])
    popup = fg.PopupWindow(window, QRect(0, 0, 300, 200))  # active in its own window, like a pop-up's tab in Chrome
    settled(harness)
    real = bridge.tab_info

    def reference(q: dict) -> list:
        return [info for tab in bridge.tabs() for info in [real(entry.id, tab)] if bridge._matches_query(q, tab, info)]

    built: list = []
    monkeypatch.setattr(bridge, "tab_info", lambda ext_id, tab: built.append(tab) or real(ext_id, tab))
    for q in ({"active": True}, {"active": True, "currentWindow": True}, {"highlighted": True}, {"active": False},
              {"active": 1}, {"windowType": "popup"}, {"active": True, "windowId": popup.window_id}, {}):
        assert bridge.api_tabs_query(entry.id, q, {}) == reference(q), q
    built.clear()
    assert [t["id"] for t in bridge.api_tabs_query(entry.id, {"active": True, "currentWindow": True}, {})] == [tabs[1].tab_id]
    assert len(built) == 2 and tabs[1] in built and popup in built  # not the 5 other tabs
    built.clear()
    assert [t["id"] for t in bridge.api_tabs_query(entry.id, {"active": True}, {})] == [tabs[1].tab_id, popup.tab_id]
    assert len(built) == 2
    built.clear()
    assert len(bridge.api_tabs_query(entry.id, {}, {})) == len(bridge.tabs()) == len(built) == 6
    popup.close()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  storage.onChanged to content scripts
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_storage_change_is_encoded_once_for_all_the_tabs(window, harness, server, tmp_path, fg, monkeypatch):
    c = harness.controller
    entry = harness.install_ok(eb.api_extension(tmp_path / "sink"), "Kitchen Sink")
    tabs = [window.new_tab(QUrl(server.page_url()), background=True) for _ in range(3)]
    for tab in tabs:
        assert wait_attr(tab.page, "data-sink-ran") == "yes"
    bridge, changes = c.bridge, {"theme": {"newValue": "dark"}}
    assert len(bridge.script_tabs(entry.id)) == 3
    encoded: list = []
    real = fg.json.dumps
    monkeypatch.setattr(fg.json, "dumps", lambda *a, **k: encoded.append(a[0]) or real(*a, **k))
    bridge._deliver_storage(entry.id, "sync", changes, None, None, {"cs": False, "from": ""})
    monkeypatch.setattr(fg.json, "dumps", real)
    assert len(encoded) == 3  # the event's name, the payload and its string - not again for each tab
    assert any(isinstance(e, dict) and e.get("args") == ["sync", changes, None] for e in encoded)
    for tab in tabs:
        assert json.loads(wait_attr(tab.page, "data-sink-sync")) == changes


# ══════════════════════════════════════════════════════════════════════════════════════════
#  One-off timers
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_send_message_and_cookies_set_free_their_timers(window, harness, server, tmp_path):
    c = harness.controller
    entry = install_probes(harness, tmp_path, 1, permissions=["storage", "cookies"], host_permissions=["http://127.0.0.1/*"])[0]
    bridge = c.bridge
    tab = blank_tabs(window, 1)[0]
    settled(harness)
    bridge.extension_disabled(entry.id)  # (closes its bridge page: no page timers to count)
    spin(0.1)

    def timers() -> int:
        return len(bridge.findChildren(QTimer)) - len(bridge._pages)

    base = timers()
    answers: list = []
    for i in range(30):
        later = bridge.api_tabs_sendMessage(entry.id, {"tabId": tab.tab_id, "msg": {"n": i}}, {"from": ""})
        later(lambda value=None, error=None, i=i: answers.append((i, value, error)))
        if i % 3 == 0:  # answered by a content script
            bridge.api_tabs_reply(entry.id, {"callId": list(bridge._replies)[-1], "value": i}, {})
        elif i % 3 == 1:  # switched off while waiting
            bridge.extension_disabled(entry.id)
        # otherwise: nothing in the page receives it (about:blank)
    wait_until(lambda: len(answers) == 30, 10, "every message settled")
    assert [v for i, v, _e in answers if i % 3 == 0] == list(range(0, 30, 3))
    assert all(e == bridge.NO_RECEIVER for i, _v, e in answers if i % 3)
    assert bridge._replies == {}
    spin(0.2)
    assert timers() == base
    wait_until(lambda: bridge._cookies_ready, 5, "cookie watching")
    base = timers()
    cookies: list = []
    for i in range(10):
        later = bridge.api_cookies_set(entry.id, {"url": server.url("/"), "name": f"fg{i}", "value": "1"}, {})
        later(lambda value=None, error=None: cookies.append((value, error)))
    wait_until(lambda: len(cookies) == 10, 10, "the cookies set")
    assert all(e is None and v["name"].startswith("fg") for v, e in cookies)
    later = bridge.api_cookies_set(entry.id, {"url": server.url("/"), "name": "late", "value": "1"}, {})
    later(lambda value=None, error=None: cookies.append((value, error)))
    timer = next(w[1] for waiters in bridge._cookie_waiters.values() for w in waiters)
    timer.stop()
    timer.timeout.emit()  # the store never confirmed it
    assert cookies[-1] == (None, 'Failed to parse or set cookie named "late".')
    spin(0.3)
    assert not any(bridge._cookie_waiters.values()) and timers() == base
