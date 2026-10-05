"""Fourth review round: what an extension's sandboxed pages can reach of the polyfill, early storage notices that
aren't true, storage changes for tabs without the extension's scripts - and registered content scripts that keep
an extension reloading, get lost in a reload, or wait for ever on an open extension page."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

import extbuilder as eb
from helpers import poll_js, run_js, run_js_async, spin, wait_attr, wait_until
from test_extension_review import ext_page, simple_ext


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC2-1: the polyfill's secrets stay with the extension's own pages, worker and content scripts
# ══════════════════════════════════════════════════════════════════════════════════════════
SANDBOX_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body><script src="sandbox.js"></script></body></html>"""
SANDBOX_JS = r"""
(async () => {
  const get = async (path) => { try { const r = await fetch(path); return r.ok ? 'read ' + (await r.text()).length : 'status ' + r.status; }
                                catch (e) { return 'refused'; } };
  parent.postMessage(JSON.stringify({origin: self.origin, runtime: typeof (self.chrome && chrome.runtime && chrome.runtime.id),
    shim: await get('/foxglove-shim.js'), bridge: await get('/foxglove-bridge.html'), own: await get('/sandbox.js')}), '*');
})();
"""


def _frame_report(page, frame_js: str) -> dict:
    run_js(page, f"""window.__s = []; window.addEventListener('message', (e) => window.__s.push(String(e.data)));
      {frame_js}; 1""")
    seen = poll_js(page, "window.__s.length ? window.__s[0] : ''", bool, 15, "the frame's report")
    return json.loads(seen)


def test_sandboxed_pages_cant_read_the_polyfill(harness, tmp_path, fg):
    src = simple_ext(tmp_path / "s", "Sandboxer", {"sandbox.html": SANDBOX_HTML, "sandbox.js": SANDBOX_JS, "cs.js": "1;\n"},
                     sandbox={"pages": ["sandbox.html"]}, content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}])
    ext = harness.install_ok(src, "Sandboxer")
    assert fg.SHIM_TAG not in (Path(ext.path) / "sandbox.html").read_text()  # nothing it could run there
    page = ext_page(harness, ext)
    # the manifest's sandboxed page: an opaque origin without extension APIs - and without the polyfill's files
    got = _frame_report(page, "const f = document.createElement('iframe'); f.src = 'sandbox.html'; document.body.appendChild(f)")
    assert got["origin"] == "null" and got["runtime"] == "undefined", got
    assert got["shim"] == "refused" and got["bridge"] == "refused", got
    assert got["own"].startswith("read "), got  # its own files load as before
    # a sandboxed frame an extension page makes for content from anywhere
    untrusted = "<script>" + SANDBOX_JS.replace("/sandbox.js", "/options.js").replace("'/foxglove", f"'chrome-extension://{ext.id}/foxglove") \
        .replace("'/options.js'", f"'chrome-extension://{ext.id}/options.js'") + "<\\/script>"
    got = _frame_report(page, f"const f = document.createElement('iframe'); f.sandbox = 'allow-scripts'; "
                              f"f.srcdoc = {json.dumps(untrusted)}; document.body.appendChild(f)")
    assert got["shim"] == "refused" and got["bridge"] == "refused", got
    # the extension's own pages still have it
    assert run_js_async(page, "return (await (await fetch('/foxglove-shim.js')).text()).startsWith('/* foxglove-shim')") is True
    assert run_js_async(page, "await chrome.storage.local.set({x: 1}); return (await chrome.storage.local.get('x')).x") == 1


def test_rewiring_takes_the_polyfill_out_of_sandboxed_pages(tmp_path, fg):
    folder = eb.write_tree(tmp_path / "x", {"manifest_version": 3, "name": "X", "version": "1",
                                            "sandbox": {"pages": ["/sb/*.html"]}},
                           {"sb/a.html": f"<html><head>{fg.SHIM_TAG}</head></html>", "sb/b.html": "<html><head></head></html>",
                            "page.html": "<html><head></head></html>"})
    fg.inject_shim(folder, json.loads((folder / "manifest.json").read_text()), "0" * 32, "en_US")
    assert fg.SHIM_TAG not in (folder / "sb/a.html").read_text()
    assert fg.SHIM_TAG not in (folder / "sb/b.html").read_text()
    assert fg.SHIM_TAG in (folder / "page.html").read_text()


def test_early_notices_pass_on_only_what_is_stored(harness, tmp_path, fg, monkeypatch):
    """A notice is what its sender says: one that isn't true (made up, or a write that failed) changes nothing."""
    entry = harness.install_ok(simple_ext(tmp_path / "e", "Early", sw="chrome.storage.onChanged.addListener(() => {});"), "Early")
    bridge, sent = harness.controller.bridge, []
    wait_until(lambda: bridge.listens(entry.id, "storage.onChanged"), 10, "the worker's listener")
    page = ext_page(harness, entry)
    run_js_async(page, "await chrome.storage.local.set({blockingEnabled: true, a: 2}); await chrome.storage.sync.set({s: 'x'}); return 1")
    spin(0.5)
    monkeypatch.setattr(bridge, "emit", lambda ext_id, name, args, eid=None, worker_only=False: sent.append((name, args, eid)))
    monkeypatch.setattr(bridge, "EARLY_STORAGE_MS", 200)
    ctx = {"from": "", "cs": True, "tab": None}
    bridge.api_storage_pending(entry.id, {"area": "local", "src": "z", "eid": "f1", "set": {"blockingEnabled": False}}, ctx)
    bridge.api_storage_pending(entry.id, {"area": "sync", "src": "z", "eid": "f2", "remove": ["s"]}, ctx)
    bridge.api_storage_pending(entry.id, {"area": "local", "src": "z", "eid": "ok1", "set": {"a": 2, "blockingEnabled": False}}, ctx)
    bridge.api_storage_pending(entry.id, {"area": "sync", "src": "z", "eid": "ok2", "remove": ["gone"]}, ctx)
    spin(2)
    assert sorted(sent, key=lambda e: e[2]) == [("storage.changed", ["local", {"a": {"newValue": 2}}, "z"], "ok1"),
                                                ("storage.changed", ["sync", {"gone": {}}, "z"], "ok2")], sent


SCRIPT_CS = """chrome.storage.onChanged.addListener((c) => { if (c.vault) document.documentElement.setAttribute('data-vault', c.vault.newValue); });
document.documentElement.setAttribute('data-cs', '1');"""


def test_storage_changes_go_only_to_tabs_with_the_extensions_scripts(window, harness, server, tmp_path):
    from test_extension_apis import load_tab
    ext = harness.install_ok(simple_ext(tmp_path / "v", "Vault", {"cs.js": SCRIPT_CS},
                                        content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}]), "Vault")
    relay = harness.controller.shim_config(ext.id)["relay"]
    other, mine = window.current_tab(), window.new_tab(background=True)
    load_tab(other, server.page_url().replace("127.0.0.1", "localhost"))
    load_tab(mine, server.page_url())
    assert wait_attr(mine.page, "data-cs") == "1"
    run_js(other.page, f"window.__got = []; document.addEventListener({json.dumps(relay)}, (e) => window.__got.push(e.detail)); 1")
    page = ext_page(harness, ext)
    run_js_async(page, "await chrome.storage.local.set({vault: 'hunter2'}); return 1")
    assert wait_attr(mine.page, "data-vault") == "hunter2"  # the content script hears it...
    spin(1)
    assert run_js(other.page, "JSON.stringify(window.__got)") == "[]"  # ... a page without it doesn't


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC2-1/2/3: a registered content script Qt can't load never keeps the extension reloading, and only it is dropped
# ══════════════════════════════════════════════════════════════════════════════════════════
from test_extension_review3 import REG, _register, _registrar, _settled  # noqa: E402

BAD_PORT = {"matches": ["http://127.0.0.1:99999/*"], "js": ["reg.js"]}


def _ids(harness, entry) -> list:
    return [s["id"] for s in harness.controller.registry[entry.id].get("scripts") or []]


def _manifest_scripts(entry) -> list:
    return [e["js"][-1] for e in json.loads((Path(entry.path) / "manifest.json").read_text()).get("content_scripts") or []]


def _age_polyfill(fg, entry) -> None:
    """As if an older Foxglove had wired the extension: the next start (here: _apply_enabled) re-wires it."""
    shim = Path(entry.path) / fg.SHIM_FILE
    text = shim.read_text(encoding="utf-8")
    shim.write_text("/* foxglove-shim v0-old\n" + text.split("\n", 1)[1], encoding="utf-8")


def _unchecked(fg, monkeypatch):
    """Registrations as if Foxglove couldn't tell they won't load (what Qt refuses that isn't checked up front)."""
    monkeypatch.setattr(fg.ExtensionBridge, "_script_entry", lambda self, ext_id, raw, old=None: {**raw, "persistAcrossSessions": True})


@pytest.mark.parametrize("bad, error", [
    ({"matches": ["http://127.0.0.1:65536/*"], "js": ["reg.js"]}, "invalid match pattern: 'http://127.0.0.1:65536/*'"),
    ({"matches": ["http://127.0.0.1/*"], "excludeMatches": ["*://*:99999/*"], "js": ["reg.js"]}, "invalid match pattern"),
    ({"matches": ["http://127.0.0.1/*"], "js": ["nonchar.js"]}, "isn't UTF-8 encoded"),
    ({"matches": ["http://127.0.0.1/*"], "css": ["nonchar2.css"]}, "isn't UTF-8 encoded"),
], ids=["port", "exclude-port", "noncharacter", "astral-noncharacter-css"])
def test_ports_and_noncharacters_are_refused_up_front(harness, tmp_path, fg, bad, error):
    files = {"nonchar.js": "// ￿\n1;\n", "nonchar2.css": "/* \U0010fffe */\n"}
    entry = harness.install_ok(simple_ext(tmp_path / "r", "Registrar", {**REG, **files}, permissions=["storage", "scripting"],
                                          host_permissions=["<all_urls>"]), "Registrar")
    page = ext_page(harness, entry)
    result = _register(page, [{"id": "bad", **bad}])
    assert result.startswith("ERR ") and error in result, result
    assert harness.controller.registry[entry.id].get("scripts") == [] and entry.id not in harness.controller._reloads
    assert fg.valid_match_pattern("http://127.0.0.1:65535/*") and fg.valid_match_pattern("*://*:0/*")
    assert _register(page, [{"id": "ok", "matches": ["http://127.0.0.1:8080/*"], "js": ["reg.js"]}]) == "ok"


def test_a_script_qt_refused_isnt_registered_again(harness, tmp_path, fg, monkeypatch):
    """A worker that registers it at every start: refused from the second time on - not a reload after reload."""
    sw = """(async () => {
      const {starts = 0} = await chrome.storage.local.get('starts');
      await chrome.storage.local.set({starts: starts + 1});
      await chrome.scripting.unregisterContentScripts();
      try { await chrome.scripting.registerContentScripts([{id: 'bad', matches: ['http://127.0.0.1:99999/*'], js: ['reg.js']}]);
            await chrome.storage.local.set({last: 'ok'}); }
      catch (e) { await chrome.storage.local.set({last: 'ERR ' + e.message}); }
    })();"""
    _unchecked(fg, monkeypatch)
    c = harness.controller
    unloads = []
    unload = c.manager.unloadExtension
    c.manager.unloadExtension = lambda info: (unloads.append(info.id()), unload(info))[1]
    start = len(harness.messages)
    entry = harness.install_ok(simple_ext(tmp_path / "l", "Looper", REG, sw=sw, permissions=["storage", "scripting"],
                                          host_permissions=["<all_urls>"]), "Looper")
    spin(3)
    _settled(harness, entry)
    spin(4)
    _settled(harness, entry)
    assert len(unloads) == 1, f"{len(unloads)} reloads"
    assert len([m for m in harness.messages[start:] if m[0] == "error"]) == 1
    page = ext_page(harness, entry)
    got = run_js_async(page, "return await chrome.storage.local.get(null)")
    assert got["starts"] == 2 and got["last"].startswith("ERR Script with ID 'bad' couldn't be loaded: Invalid value"), got
    assert _ids(harness, entry) == [] and entry.id not in c._reloads


def test_a_registration_during_a_reload_is_dropped_alone(harness, server, tmp_path, fg, monkeypatch):
    """Registered while the reload for an earlier one is under way: written by the next reload - and when that one
    fails, the registered scripts are what the manifest has (not the bad one, which would come back with every later
    registration, and at the next Foxglove upgrade)."""
    entry = _registrar(harness, tmp_path)
    c = harness.controller
    page = ext_page(harness, entry)
    assert _register(page, [{"id": "A", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    harness.drop_page(page)
    wait_until(lambda: entry.id in c._updates, 10, "the reload to start", interval=5)
    _unchecked(fg, monkeypatch)
    c.bridge.api_scripting_register(entry.id, {"scripts": [{"id": "B", **BAD_PORT}]}, {"cs": False, "from": "", "tab": None})
    start = len(harness.messages)
    for _ in range(3):
        _settled(harness, entry)
        spin(1.5)
    assert any(k == "error" and "Invalid port" in t for k, t in harness.messages[start:]), harness.messages[start:]
    assert _ids(harness, entry) == ["A"] and _manifest_scripts(entry) == ["reg.js"]
    page = ext_page(harness, entry)
    assert run_js_async(page, "return (await chrome.scripting.getRegisteredContentScripts()).map(s => s.id)") == ["A"]
    assert _register(page, [{"id": "C", "matches": ["http://127.0.0.1/*"], "js": ["reg2.js"]}]) == "ok"
    _settled(harness, entry)
    web = harness.fresh_page(server.page_url())
    assert wait_attr(web, "data-reg") == "yes" and wait_attr(web, "data-reg2") == "yes"
    # an upgrade of Foxglove re-wires it with the registered scripts: those load
    _age_polyfill(fg, entry)
    c._apply_enabled(entry.id)
    wait_until(lambda: entry.id not in c._updates, 30, "the re-wiring")
    harness.wait_state(entry.id, True, 20)
    assert entry.id not in c._reshim_failed and not c._needs_shim(c._info(entry.id))


def test_rewiring_drops_a_registered_script_that_doesnt_load(harness, tmp_path, fg):
    """One already in the registry (from before the checks): the re-wiring after an upgrade goes ahead without it."""
    entry = _registrar(harness, tmp_path)
    c = harness.controller
    c.registry[entry.id]["scripts"] = [{"id": "A", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}, {"id": "B", **BAD_PORT}]
    _age_polyfill(fg, entry)
    start = len(harness.messages)
    c._apply_enabled(entry.id)
    wait_until(lambda: entry.id not in c._updates and not c._needs_shim(c._info(entry.id)) or entry.id in c._reshim_failed, 40, "the re-wiring")
    harness.wait_state(entry.id, True, 20)
    assert entry.id not in c._reshim_failed and not c._needs_shim(c._info(entry.id))
    assert _ids(harness, entry) == ["A"] and _manifest_scripts(entry) == ["reg.js"]
    assert any(k == "error" and "Invalid port" in t for k, t in harness.messages[start:]), harness.messages[start:]


def test_a_failed_reload_keeps_the_other_changes(harness, server, tmp_path, fg, monkeypatch):
    """In the same second as the bad registration: a good one stays registered, an unregistered one stays gone."""
    entry = _registrar(harness, tmp_path)
    page = ext_page(harness, entry)
    assert _register(page, [{"id": "X", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    _settled(harness, entry)
    page = ext_page(harness, entry)
    _unchecked(fg, monkeypatch)
    assert run_js_async(page, "await chrome.scripting.unregisterContentScripts({ids: ['X']}); return 1") == 1
    assert _register(page, [{"id": "B", **BAD_PORT}]) == "ok"
    assert _register(page, [{"id": "C", "matches": ["http://127.0.0.1/*"], "js": ["reg2.js"]}]) == "ok"
    for _ in range(3):
        _settled(harness, entry)
        spin(1.5)
    assert _ids(harness, entry) == ["C"] and _manifest_scripts(entry) == ["reg2.js"]
    web = harness.fresh_page(server.page_url())
    assert wait_attr(web, "data-reg2") == "yes" and run_js(web, "document.documentElement.getAttribute('data-reg')") is None


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC2-4: an extension page the user isn't looking at holds a registration reload back only briefly
# ══════════════════════════════════════════════════════════════════════════════════════════
NEWTAB = """<!doctype html><html><head><meta charset="utf-8"><title>NT</title></head><body>new tab<script src="options.js"></script></body></html>"""


@pytest.mark.parametrize("where", ["newtab-page", "background-tab"])
def test_open_extension_pages_hold_a_reload_back_briefly(window, harness, server, tmp_path, monkeypatch, where):
    from test_extension_apis import load_tab
    entry = harness.install_ok(simple_ext(tmp_path / "n", "NewTabber", {**REG, "nt.html": NEWTAB}, permissions=["storage", "scripting"],
                                          host_permissions=["http://127.0.0.1/*"], chrome_url_overrides={"newtab": "nt.html"}), "NewTabber")
    c = harness.controller
    monkeypatch.setattr(type(c), "RELOAD_WAIT", 2.0, raising=False)
    if where == "newtab-page":
        tab = window.new_tab(window._home_url())  # the current tab
    else:
        tab = window.new_tab(background=True)
        load_tab(tab, entry.options_url.toString())
    assert wait_attr(tab.page, "data-options") == "ready"
    assert tab.url().host() == entry.id
    assert _register(tab.page, [{"id": "x", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    spin(1.5)
    assert entry.id in c._reloads  # (a moment for the page)
    _settled(harness, entry, timeout=15)
    assert wait_attr(harness.fresh_page(server.page_url()), "data-reg") == "yes"
    # the page is opened again: it works as before
    wait_until(lambda: run_js(tab.page, "!!(window.chrome && chrome.runtime && chrome.runtime.id) && "
                                        "document.documentElement.getAttribute('data-options') === 'ready'"), 10, "the page again")
    assert run_js_async(tab.page, "await chrome.storage.local.set({k: 1}); return (await chrome.storage.local.get('k')).k") == 1


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC2-2..5: host access, Foxglove's own messages, optional permissions, pages through foxglove-ext://
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_content_script_matches_give_no_api_host_access(window, harness, server, tmp_path):
    from PyQt6.QtCore import QUrl
    from PyQt6.QtNetwork import QNetworkCookie
    from test_extension_apis import load_tab
    ext = harness.install_ok(simple_ext(tmp_path / "c", "CS Only", {"cs.js": "document.documentElement.setAttribute('data-cso', '1');"},
                                        permissions=["cookies", "scripting"],
                                        content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}]), "CS Only")
    bridge = harness.controller.bridge
    wait_until(lambda: bridge._cookies_ready, 5, "the cookie store")
    cookie = QNetworkCookie(b"session", b"s3cr3t-httponly")
    cookie.setHttpOnly(True)
    cookie.setPath("/")
    harness.profile.cookieStore().setCookie(cookie, QUrl(server.url("/")))
    spin(0.5)
    tab = window.current_tab()
    load_tab(tab, server.page_url() + "?account=42")
    assert wait_attr(tab.page, "data-cso") == "1"  # its content script runs there...
    page = ext_page(harness, ext)
    assert run_js_async(page, "return await chrome.cookies.getAll({})") == []  # ... which gives the extension no host access
    got = run_js_async(page, f"return await chrome.tabs.get({tab.tab_id})")
    assert "url" not in got and "title" not in got, got
    result = run_js_async(page, f"try {{ await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, func: () => location.href}});"
                                " return 'ran'; } catch (e) { return 'ERR ' + e.message; }")
    assert result.startswith("ERR Cannot access contents of the page"), result
    assert bridge.covers(ext.id, "http://127.0.0.1/*") and not bridge.can_access(ext.id, "http://127.0.0.1/")


RELAY_SW = r"""
chrome.runtime.onInstalled.addListener(() => chrome.storage.local.get('installs').then(({installs = 0}) => chrome.storage.local.set({installs: installs + 1})));
chrome.runtime.onMessage.addListener((m) => { chrome.storage.local.set({lastMsg: JSON.stringify(m)}); });
"""


def test_an_object_passed_on_is_never_taken_for_foxgloves_own_message(harness, tmp_path):
    ext = harness.install_ok(simple_ext(tmp_path / "r", "Relay", sw=RELAY_SW), "Relay")
    page = ext_page(harness, ext)
    wait_until(lambda: run_js_async(page, "return (await chrome.storage.local.get('installs')).installs ?? 0") == 1, 10, "onInstalled")
    for msg in ({"__fg": "event", "name": "runtime.onInstalled", "args": [{"reason": "install"}]},
                {"__fg": "cs-msg", "msg": "hi", "tab": 7, "frame": 0, "title": "t"}):
        run_js_async(page, f"await chrome.runtime.sendMessage({json.dumps(msg)}).catch(() => 0); return 1")
        assert wait_until(lambda: run_js_async(page, "return (await chrome.storage.local.get('lastMsg')).lastMsg ?? ''") == json.dumps(msg, separators=(",", ":")),
                          10, "the worker to get the message as it is")
    assert run_js_async(page, "return (await chrome.storage.local.get('installs')).installs") == 1


def test_permission_patterns_compare_like_chrome(window, harness, tmp_path, fg, monkeypatch):
    entry = harness.install_ok(simple_ext(tmp_path / "o", "Optional", optional_host_permissions=["https://example.com/*"],
                                          host_permissions=["https://shop.example/*"]), "Optional")
    bridge, ctx, asked = harness.controller.bridge, {"from": "", "cs": False, "tab": None}, []
    monkeypatch.setattr(fg, "ask_question", lambda *a, **k: asked.append(a[2]) or True)
    with pytest.raises(fg.ApiError, match="Only permissions specified in the manifest"):
        bridge.api_permissions_request(entry.id, {"origins": ["https://*.example.com/*"]}, ctx)
    assert asked == [] and not bridge.can_access(entry.id, "https://mail.example.com/inbox")
    assert bridge.api_permissions_contains(entry.id, {"origins": ["https://*.shop.example/*"]}, ctx) is False
    assert bridge.api_permissions_contains(entry.id, {"origins": ["https://shop.example/cart/*"]}, ctx) is True
    replies = []
    later = bridge.api_permissions_request(entry.id, {"origins": ["https://example.com/path/*"]}, ctx)  # within what it lists
    later(lambda value=None, error=None: replies.append((value, error)))
    wait_until(lambda: replies, 5, "the answer")
    assert replies == [(True, None)] and bridge.can_access(entry.id, "https://example.com/path/x")
    for outer, inner, inside in [("https://*.example.com/*", "https://example.com/*", True), ("*://*/*", "https://*.x.org/*", True),
                                 ("https://*.example.com/*", "https://*.a.example.com/*", True), ("<all_urls>", "file:///*", True),
                                 ("https://example.com/*", "https://*.example.com/*", False), ("*://*/*", "file:///*", False),
                                 ("http://h/*", "http://h:8080/*", True), ("http://h:80/*", "http://h/*", False)]:
        assert fg.pattern_contains(outer, inner) is inside, (outer, inner)


WAR_HTML = """<!doctype html><meta charset="utf-8"><body><script>parent.postMessage('inline ran', '*');</script>
<script src="war.js"></script></body>"""


def test_web_accessible_pages_keep_the_extensions_csp(window, harness, server, tmp_path):
    from test_extension_apis import load_tab
    ext = harness.install_ok(simple_ext(tmp_path / "w", "Warry", {"view.html": WAR_HTML, "war.js": "parent.postMessage('file ran', '*');"},
                                        web_accessible_resources=[{"resources": ["view.html", "war.js"], "matches": ["<all_urls>"]}]), "Warry")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    run_js(tab.page, f"""window.__s = []; window.addEventListener('message', (e) => window.__s.push(String(e.data)));
      const f = document.createElement('iframe'); f.src = 'foxglove-ext://{ext.id}/view.html'; document.body.appendChild(f); 1""")
    poll_js(tab.page, "JSON.stringify(window.__s)", lambda v: "file ran" in v, 10, "the page's own script")
    spin(0.5)
    assert run_js(tab.page, "JSON.stringify(window.__s)") == '["file ran"]'  # its inline script was refused, as in Chrome


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC2-5..7: a failed write, a dialog when the browser quits, user actions during Foxglove's own reload
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_write_that_fails_is_never_announced(harness, tmp_path):
    sw = "chrome.storage.onChanged.addListener((c, area) => { if (c.big) chrome.storage.local.set({heard: area}); });"
    entry = harness.install_ok(simple_ext(tmp_path / "f", "Failer", sw=sw), "Failer")
    bridge = harness.controller.bridge
    wait_until(lambda: bridge.listens(entry.id, "storage.onChanged"), 10, "the worker's listener")
    page = ext_page(harness, entry)
    run_js_async(page, "await chrome.storage.session.set({fill: 'y'.repeat(10400000)}); return 1", timeout=60)
    spin(2)
    done = {k for k in bridge._storage_done if k.startswith(entry.id)}
    result = run_js_async(page, "try { await chrome.storage.session.set({big: 'z'.repeat(200000)}); return 'ok'; }"
                                " catch (e) { return 'ERR ' + e.message; }", timeout=60)
    assert result.startswith("ERR ") and "quota" in result.lower(), result
    wait_until(lambda: {k for k in bridge._storage_done if k.startswith(entry.id)} - done
               and not [k for k in bridge._early_storage if k.startswith(entry.id)], 1.0, "the notice taken back")
    spin(2.5)
    assert run_js_async(page, "return (await chrome.storage.local.get('heard')).heard ?? null") is None
    assert run_js_async(page, "return (await chrome.storage.session.get('big')).big ?? null") is None


def test_a_question_whose_window_goes_away_is_no(qapp, fg, monkeypatch):
    from PyQt6 import sip
    from PyQt6.QtCore import QTimer
    from PyQt6.QtWidgets import QWidget
    monkeypatch.undo()  # the real ask_question (tests otherwise refuse dialogs)
    parent = QWidget()
    QTimer.singleShot(300, lambda: sip.delete(parent))  # the browser quits while it's open
    assert fg.ask_question(parent, "Remove Extension", "Remove “X”?", "Remove") is False


def test_user_actions_during_foxgloves_own_reload(harness, server, tmp_path):
    """An update asked for while Foxglove reloads the extension for its scripts follows it; a removal goes ahead."""
    c = harness.controller
    src = tmp_path / "Racer"
    entry = harness.install_ok(simple_ext(src, "Racer", REG, permissions=["storage", "scripting"], host_permissions=["<all_urls>"]), "Racer")
    page = ext_page(harness, entry)
    assert _register(page, [{"id": "x", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    harness.drop_page(page)
    wait_until(lambda: entry.id in c._updates, 10, "the reload to start", interval=5)
    simple_ext(src, "Racer", REG, permissions=["storage", "scripting"], host_permissions=["<all_urls>"], version="2.0")
    start = len(harness.messages)
    c.install_from_path(str(src))
    wait_until(lambda: ("success", "“Racer” was updated.") in harness.messages[start:], 30, "the update")
    assert harness.messages[start:] == [("success", "“Racer” was updated.")], harness.messages[start:]
    assert harness.wait_state(entry.id, True, 20).version == "2.0"
    # a removal while a reload is about to start: the reload isn't needed any more
    page = ext_page(harness, entry)
    assert _register(page, [{"id": "y", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    harness.drop_page(page)
    wait_until(lambda: entry.id in c._updates, 10, "the reload to start", interval=5)
    start = len(harness.messages)
    c.uninstall(entry.id)
    wait_until(lambda: ("success", "“Racer” was removed.") in harness.messages[start:], 20, "the removal")
    assert harness.messages[start:] == [("success", "“Racer” was removed.")] and harness.entries() == []
    assert entry.id not in harness.registry_on_disk()


def test_a_removal_asked_for_during_an_update_survives_a_restart(harness, tmp_path):
    c = harness.controller
    entry = harness.install_ok(simple_ext(tmp_path / "q", "Quitter"), "Quitter")
    c._updates[entry.id] = job = {"silent": True, "name": "Quitter", "reshim": True}  # (Foxglove re-wiring it)
    c.uninstall(entry.id)
    assert harness.messages[-1] == ("info", "“Quitter” will be removed in a moment.")
    assert harness.registry_on_disk()[entry.id].get("remove") is True and job["remove_after"]
    c._updates.pop(entry.id)  # ... and Foxglove quits before it's through: at the next start
    start = len(harness.messages)
    c._apply_enabled(entry.id)
    wait_until(lambda: ("success", "“Quitter” was removed.") in harness.messages[start:], 20, "the removal")
    assert harness.entries() == [] and entry.id not in harness.registry_on_disk()
