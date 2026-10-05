"""Second review round: extensions must not reach each other (IDs, pages, worlds, tab ids, events, files) and real
extensions' needs (big calls, storage before closing, session access, frames, fonts, cookies, registered scripts,
promise replies, upgrades of the polyfill, sizing of pop-ups)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QSize, QUrl
from PyQt6.QtNetwork import QNetworkCookie
from PyQt6.QtWidgets import QCheckBox

import extbuilder as eb
from helpers import poll_js, run_js, run_js_async, spin, wait_attr, wait_until
from test_extension_apis import install_sink, load_tab
from test_extensions import _run_phase

OPT_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>%s</title></head>
<body><h1>%s</h1><script src="options.js"></script></body></html>"""
OPT_JS = "document.documentElement.setAttribute('data-options', 'ready');\n"


def simple_ext(dest: Path, name: str, files: dict | None = None, sw: str = "", **extra) -> Path:
    manifest = {"manifest_version": 3, "name": name, "version": "1.0", "permissions": ["storage"],
                "background": {"service_worker": "sw.js"}, "options_page": "options.html", **extra}
    return eb.write_tree(dest, manifest, {"options.html": OPT_HTML % (name, name), "options.js": OPT_JS,
                                          "sw.js": sw or "self.addEventListener('fetch', () => {});\n", **(files or {})})


def ext_page(harness, entry):
    page = harness.fresh_page(entry.options_url.toString())
    assert wait_attr(page, "data-options") == "ready"
    return page


# ══════════════════════════════════════════════════════════════════════════════════════════
#  A package can't take an installed extension's ID (and with it its data)
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("packed", [False, True], ids=["folder-with-its-own-key", "signed-crx"])
def test_a_packaged_sidecar_cant_claim_another_id(harness, tmp_path, fg, packed):
    victim_key, attacker_key = eb.new_key(), eb.new_key()
    victim = harness.install_ok(eb.write_crx3(simple_ext(tmp_path / "v", "Victim"), tmp_path / "v.crx", victim_key), "Victim")
    page = ext_page(harness, victim)
    run_js_async(page, "await chrome.storage.local.set({secret: 'hunter2'}); return true")
    harness.drop_page(page)
    src = simple_ext(tmp_path / "a", "Attacker", **({} if packed else {"key": attacker_key.manifest_key}))
    sidecar = {**json.loads((src / "manifest.json").read_text()), "key": victim_key.manifest_key}
    (src / fg.SHIM_ORIGINAL).write_text(json.dumps(sidecar))  # what an installed (wired) copy keeps beside manifest.json
    kind, text = harness.install(eb.write_crx3(src, tmp_path / "a.crx", attacker_key) if packed else src)
    assert kind == "success", text
    wait_until(lambda: (e := harness.entry(attacker_key.ext_id)) is not None and e.enabled, 15, "the new extension, under its own ID")
    victim = harness.wait_state(victim_key.ext_id, True)
    assert victim.name == "Victim"
    page = ext_page(harness, victim)
    assert run_js_async(page, "return (await chrome.storage.local.get('secret')).secret") == "hunter2"


def test_an_install_that_loads_under_another_id_is_refused(harness, tmp_path, fg, monkeypatch):
    install = fg.ExtensionsController._install_staged
    monkeypatch.setattr(fg.ExtensionsController, "_install_staged", lambda self, job: install(self, {**job, "id": "a" * 32}))
    kind, text = harness.install(simple_ext(tmp_path / "odd", "Odd One"))
    assert kind == "error" and "isn't what it claims" in text
    wait_until(lambda: not harness.by_name("Odd One"), 15, "the refused copy to go")
    spin(0.5)
    assert not [m for m in harness.messages if "removed" in m[1]]  # taken out quietly
    assert harness.controller.registry.get("a" * 32) is None


def test_inject_shim_keeps_the_checked_key(fg, tmp_path):
    root = simple_ext(tmp_path / "x", "Keyed", key="CHECKED")
    (root / fg.SHIM_ORIGINAL).write_text(json.dumps({"manifest_version": 3, "name": "Keyed", "version": "1.0", "key": "OTHER"}))
    fg.inject_shim(root, fg.load_manifest(root), "0" * 32, "en_US")
    assert json.loads((root / "manifest.json").read_text())["key"] == "CHECKED"
    root = simple_ext(tmp_path / "y", "Keyless")
    (root / fg.SHIM_ORIGINAL).write_text(json.dumps({"manifest_version": 3, "name": "Keyless", "version": "1.0", "key": "OTHER"}))
    fg.inject_shim(root, fg.load_manifest(root), "0" * 32, "en_US")
    assert "key" not in json.loads((root / "manifest.json").read_text())


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Host access: web pages only - not other extensions' pages, not files without the user's consent
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_scripting_cant_reach_extension_pages(window, harness, tmp_path):
    victim = harness.install_ok(simple_ext(tmp_path / "v", "Victim"), "Victim")
    attacker = harness.install_ok(simple_ext(tmp_path / "a", "Attacker", permissions=["scripting", "tabs"],
                                             host_permissions=["chrome-extension://*/*", "<all_urls>"]), "Attacker")
    page = ext_page(harness, attacker)
    info = run_js_async(page, f"return await chrome.tabs.create({{url: 'chrome-extension://{victim.id}/options.html'}})")
    tab = wait_until(lambda: next((t for t in window.tabs() if t.tab_id == info["id"]), None))
    wait_until(lambda: tab.url().host() == victim.id and not tab.loading, 20, "the victim's page in a tab")
    for call in (f"chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, world: 'MAIN', func: () => 1}})",
                 f"chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, func: () => 1}})",
                 f"chrome.scripting.insertCSS({{target: {{tabId: {tab.tab_id}}}, css: 'body {{ color: red; }}'}})"):
        with pytest.raises(AssertionError, match="Cannot access contents"):
            run_js_async(page, f"return await {call}")
    bridge = harness.controller.bridge
    assert not bridge.can_access(attacker.id, f"chrome-extension://{victim.id}/options.html")
    assert not bridge.can_access(attacker.id, "foxglove://newtab")
    assert bridge.can_access(attacker.id, "https://example.com/")


def test_file_pages_need_the_users_consent(window, harness, tmp_path, fg):
    secret = tmp_path / "taxes.html"
    secret.write_text("<html><body>SSN 123-45-6789</body></html>")
    ext = harness.install_ok(simple_ext(tmp_path / "x", "All Urls", permissions=["scripting"], host_permissions=["<all_urls>"]), "All Urls")
    page = ext_page(harness, ext)
    tab = window.current_tab()
    load_tab(tab, QUrl.fromLocalFile(str(secret)).toString())
    read = f"return (await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, func: () => document.body.innerText}}))[0].result"
    with pytest.raises(AssertionError, match="Cannot access contents"):
        run_js_async(page, read)
    assert "url" not in run_js_async(page, f"return await chrome.tabs.get({tab.tab_id})")
    controller = harness.controller
    assert controller.wants_file_access(ext.id) and not controller.file_access(ext.id)
    row = fg.ExtensionRow(controller.entry(ext.id), window)
    box = next(b for b in row.findChildren(QCheckBox) if b.text() == "Allow access to file URLs")
    assert not box.isChecked()
    box.setChecked(True)  # the user allows it
    assert controller.file_access(ext.id)
    assert run_js_async(page, read) == "SSN 123-45-6789"
    row.deleteLater()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Content scripts: other extensions can't listen in on, or fake, what Foxglove tells them
# ══════════════════════════════════════════════════════════════════════════════════════════
B_CS = r"""(() => {
  const root = document.documentElement;
  chrome.runtime.onMessage.addListener((msg, sender, reply) => { root.setAttribute('data-got', JSON.stringify(msg)); reply('ok'); });
  document.addEventListener('probe-send', () => chrome.runtime.sendMessage({type: 'who'}).then((r) => root.setAttribute('data-who', JSON.stringify(r))));
  root.setAttribute('data-incognito', String(chrome.extension && chrome.extension.inIncognitoContext));
  root.setAttribute('data-b', 'ready');
})();
"""
B_SW = "chrome.runtime.onMessage.addListener((m, s, reply) => { if (m && m.type === 'who') reply({tab: s.tab ? s.tab.id : null}); });\n"


def two_extensions(harness, tmp_path):
    b = harness.install_ok(simple_ext(tmp_path / "b", "Bee", {"cs.js": B_CS}, sw=B_SW, permissions=["storage", "scripting"],
                                      host_permissions=["http://127.0.0.1/*"],
                                      content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}]), "Bee")
    a = harness.install_ok(simple_ext(tmp_path / "a", "Ant", permissions=["scripting"], host_permissions=["http://127.0.0.1/*"]), "Ant")
    return a, b


def execute(page, tab, func: str, args: str = "[]", world: str = "ISOLATED"):
    return run_js_async(page, f"return (await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, world: '{world}',"
                              f" args: {args}, func: {func}}}))[0].result")


def test_injected_code_runs_in_a_world_of_its_own(window, harness, server, tmp_path):
    a, b = two_extensions(harness, tmp_path)
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    assert wait_attr(tab.page, "data-b") == "ready"
    apage, bpage = ext_page(harness, a), ext_page(harness, b)
    execute(apage, tab, """() => { window.__cap = []; window.__mine = 'ant'; const orig = document.dispatchEvent;
      document.dispatchEvent = function (e) { window.__cap.push(e.type + ' ' + e.detail); return orig.call(this, e); }; }""")
    assert run_js_async(bpage, f"return await chrome.tabs.sendMessage({tab.tab_id}, {{password: 'pw123'}})") == "ok"
    assert json.loads(wait_attr(tab.page, "data-got")) == {"password": "pw123"}
    assert execute(apage, tab, "() => JSON.stringify(window.__cap)") == "[]"  # Foxglove's world isn't Ant's
    assert execute(apage, tab, "() => window.__mine") == "ant"
    assert execute(bpage, tab, "() => window.__mine === undefined") is True  # nor is Bee's
    assert run_js(tab.page, "window.__mine === undefined") is True  # nor the page's


def test_tab_ids_cant_be_faked(window, harness, server, tmp_path):
    a, b = two_extensions(harness, tmp_path)
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    assert wait_attr(tab.page, "data-b") == "ready"
    assert attr_of(tab, "data-incognito") == "false"  # chrome.extension in content scripts, as in Chrome
    apage = ext_page(harness, a)
    own = run_js_async(apage, """const t = await (await fetch(chrome.runtime.getURL('foxglove-shim.js'))).text();
      const s = t.indexOf('/*FOXGLOVE_CFG*/') + 16; return JSON.parse(t.slice(s, t.indexOf('/*END*/', s)))""")
    bee = harness.controller.shim_config(b.id)
    assert own["tabAnswer"] != bee["tabAnswer"] and own["tabQuery"] != bee["tabQuery"]  # names per extension
    execute(apage, tab, "(n) => { document.dispatchEvent(new CustomEvent(n, {detail: '4242'})); }", json.dumps([own["tabAnswer"]]))
    run_js(tab.page, f"document.dispatchEvent(new CustomEvent({json.dumps(bee['tabAnswer'])}, {{detail: '4343'}}))")  # unasked: ignored
    run_js(tab.page, "document.dispatchEvent(new CustomEvent('probe-send'))")
    assert json.loads(wait_attr(tab.page, "data-who")) == {"tab": tab.tab_id}


def attr_of(tab, name: str):
    return run_js(tab.page, f"document.documentElement.getAttribute({json.dumps(name)})")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Events that report browsing need their permission
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_navigation_events_need_the_webnavigation_permission(window, harness, server, tmp_path):
    spy = harness.install_ok(simple_ext(tmp_path / "spy", "Spy", permissions=[]), "Spy")
    nav = harness.install_ok(simple_ext(tmp_path / "nav", "Nav", permissions=["webNavigation"]), "Nav")
    spy_page, nav_page = ext_page(harness, spy), ext_page(harness, nav)
    run_js(spy_page, "window.__seen = []; new BroadcastChannel('__foxglove').onmessage = (e) => window.__seen.push(e.data); 1")
    run_js_async(spy_page, """const r = await fetch('foxglove-ext://bridge/call', {method: 'POST', body: JSON.stringify(
        {api: 'events.listen', args: {name: 'webNavigation.onCompleted', worker: false}, from: location.href})}); return (await r.json()).ok""")
    run_js(nav_page, "window.__seen = []; chrome.webNavigation.onCompleted.addListener((d) => window.__seen.push(d.url)); 1")
    spin(0.5)
    url = server.url("/page?secret-statement")
    load_tab(window.current_tab(), url)
    poll_js(nav_page, "JSON.stringify(window.__seen)", lambda v: url in v, 10, "the onCompleted event")
    spin(1.0)
    assert "secret-statement" not in run_js(spy_page, "JSON.stringify(window.__seen)")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Web-accessible files: opaque origins, and an extension frame's own files
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_web_accessible_files_from_opaque_origins_and_own_frames(window, harness, server, tmp_path):
    files = {"img/a.png": eb.tiny_png(), "frame.html": '<!doctype html><meta charset="utf-8"><script src="frame.js"></script>',
             "frame.js": "parent.postMessage('frame.js ran', '*');\n"}
    entry = harness.install_ok(simple_ext(tmp_path / "w", "Warry", files, web_accessible_resources=[
        {"resources": ["img/*", "frame.html", "frame.js"], "matches": ["http://127.0.0.1/*"]}]), "Warry")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    png = f"foxglove-ext://{entry.id}/img/a.png"
    assert run_js_async(tab.page, f"return (await fetch({json.dumps(png)})).status") == 200
    run_js(tab.page, f"""window.__s = []; window.addEventListener('message', (e) => window.__s.push(e.data));
      const f = document.createElement('iframe'); f.sandbox = 'allow-scripts';
      f.srcdoc = '<script>fetch({json.dumps(png)}).then((r) => parent.postMessage("sandboxed " + r.status, "*"), () => parent.postMessage("sandboxed refused", "*"))<\\/script>';
      document.body.appendChild(f);
      const g = document.createElement('iframe'); g.src = 'foxglove-ext://{entry.id}/frame.html'; document.body.appendChild(g); 1""")
    seen = poll_js(tab.page, "JSON.stringify(window.__s)", lambda v: v.count('"') >= 4, 10, "both frames to report")
    assert sorted(json.loads(seen))[0] == "frame.js ran"  # the extension's frame gets its own files
    assert sorted(json.loads(seen))[1] in ("sandboxed 404", "sandboxed refused")  # an opaque origin isn't 127.0.0.1


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Calls bigger than Qt passes in one request (uBlock Origin Lite's rule updates)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_big_calls_arrive_whole(harness, tmp_path):
    _entry, page = install_sink(harness, tmp_path)
    got = run_js_async(page, """
      const rules = Array.from({length: 400}, (_, i) => ({id: i + 1, priority: 1, action: {type: 'block'},
        condition: {urlFilter: 'ads-\\u00fc\\ud83d\\ude00-' + i + '-' + 'x'.repeat(150)}}));
      await chrome.declarativeNetRequest.updateDynamicRules({addRules: rules});
      const got = await chrome.declarativeNetRequest.getDynamicRules();
      return [got.length, got[399].condition.urlFilter, JSON.stringify(rules).length];""", timeout=30)
    assert got[0] == 400 and got[1] == "ads-ü\U0001F600-399-" + "x" * 150 and got[2] > 60000
    refused = run_js_async(page, "const r = await fetch('foxglove-ext://bridge/call?part=bad', {method: 'POST', body: '{}'})"
                                 ".catch(() => null); return r ? r.status : 'refused'")
    assert refused in ("refused", 403)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Storage written right before a page closes (pop-ups saving their settings)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_storage_written_just_before_closing_is_kept(window, harness, tmp_path, fg):
    files = eb.probe_files("Saver", "saver", popup_js="chrome.storage.local.set({closeL: 1}); chrome.storage.sync.set({closeS: 2});"
                                                      " chrome.storage.local.remove('gone'); window.close();\n")
    entry = harness.install_ok(eb.write_tree(tmp_path / "saver", eb.probe_manifest("Saver", "saver"), files), "Saver")
    page = harness.fresh_page(entry.options_url.toString())
    run_js_async(page, "await chrome.storage.local.set({gone: 1}); return true")
    window.open_extension(entry.id)
    stored = lambda: run_js_async(page, "return [(await chrome.storage.local.get(null)), (await chrome.storage.sync.get(null))]")
    wait_until(lambda: stored() == [{"closeL": 1}, {"closeS": 2}], 10, "what the pop-up saved")
    wait_until(lambda: not [p for p in window.findChildren(fg.ExtensionPopup) if not sip.isdeleted(p) and p.isVisible()], 10,
               "the pop-up to close itself")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Vimium's needs: storage.session for content scripts, the tab's frames, sender.tab from its pages
# ══════════════════════════════════════════════════════════════════════════════════════════
SESSION_SW = """chrome.storage.session.setAccessLevel({accessLevel: 'TRUSTED_AND_UNTRUSTED_CONTEXTS'})
  .then(() => chrome.storage.session.set({hello: 'from the worker'}));
"""
SESSION_CS = """(async () => {
  const root = document.documentElement;
  for (let i = 0; i < 40; i++) {
    try { const v = await chrome.storage.session.get('hello'); if (v.hello) { root.setAttribute('data-session', v.hello); return; } }
    catch (e) { root.setAttribute('data-session-error', e.message); }
    await new Promise((r) => setTimeout(r, 250));
  }
})();
"""


def test_content_scripts_reach_session_storage_when_allowed(harness, server, tmp_path):
    harness.install_ok(simple_ext(tmp_path / "s", "Sessions", {"cs.js": SESSION_CS}, sw=SESSION_SW,
                                  content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}]), "Sessions")
    page = harness.fresh_page(server.page_url())
    assert wait_attr(page, "data-session", 15) == "from the worker"


def test_webnavigation_frames(window, harness, server, tmp_path):
    entry, page = install_sink(harness, tmp_path)
    tab = window.current_tab()
    url = server.url("/page?frames")
    load_tab(tab, url)
    frames = run_js_async(page, f"return await chrome.webNavigation.getAllFrames({{tabId: {tab.tab_id}}})")
    assert [(f["frameId"], f["parentFrameId"], f["url"]) for f in frames] == [(0, -1, url)]
    assert run_js_async(page, f"return (await chrome.webNavigation.getFrame({{tabId: {tab.tab_id}, frameId: 0}})).url") == url
    assert run_js_async(page, f"return await chrome.webNavigation.getFrame({{tabId: {tab.tab_id}, frameId: 7}})") is None
    other = harness.install_ok(simple_ext(tmp_path / "o", "No Nav"), "No Nav")
    other_page = ext_page(harness, other)
    with pytest.raises(AssertionError, match="webNavigation"):  # the polyfill hides it; the bridge refuses it too
        run_js_async(other_page, f"""return await (await fetch('foxglove-ext://bridge/call', {{method: 'POST', body: JSON.stringify(
            {{api: 'webNavigation.getAllFrames', args: {{tabId: {tab.tab_id}}}, from: location.href}})}})).json().then((r) => {{
            if (!r.ok) throw new Error(r.error); return r.value; }})""")


def test_extension_pages_in_a_tab_send_their_tab(window, harness, tmp_path):
    entry, page = install_sink(harness, tmp_path)
    assert run_js_async(page, "return await chrome.runtime.sendMessage({type: 'hello'})")["tab"] is None  # not in a tab
    window.open_url(entry.options_url, "tab")
    tab = wait_until(lambda: next((t for t in window.tabs() if t.url() == entry.options_url and not t.loading), None), 15)
    assert wait_attr(tab.page, "data-options") == "ready"
    assert run_js_async(tab.page, "return await chrome.runtime.sendMessage({type: 'hello'})") == {"tab": tab.tab_id, "frame": 0}
    assert run_js_async(tab.page, "return (await chrome.tabs.getCurrent()).id") == tab.tab_id


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Worker listeners answering with a promise (newer Chrome), without upsetting older ones
# ══════════════════════════════════════════════════════════════════════════════════════════
PROMISE_SW = """chrome.runtime.onMessage.addListener(async (m) => { if (m.t === 'p') return {got: m.v}; });
chrome.runtime.onMessage.addListener((m, s, reply) => { if (m.t === 'late') { setTimeout(() => reply('late answer'), 300); return true; } });
"""


def test_promise_replies(harness, tmp_path):
    entry = harness.install_ok(simple_ext(tmp_path / "p", "Promiser", sw=PROMISE_SW), "Promiser")
    page = ext_page(harness, entry)
    assert run_js_async(page, "return await chrome.runtime.sendMessage({t: 'p', v: 5})") == {"got": 5}
    assert run_js_async(page, "return await chrome.runtime.sendMessage({t: 'late'})") == "late answer"
    assert run_js_async(page, "return (await chrome.runtime.sendMessage({t: 'none'})) ?? 'nothing'", timeout=5) == "nothing"


def test_extensions_for_a_newer_chrome_install_with_a_warning(harness, tmp_path):
    kind, text = harness.install(simple_ext(tmp_path / "n", "Newer", minimum_chrome_version="999.0"))
    assert kind == "success" and "made for Chrome 999.0 or newer" in text
    kind, text = harness.install(simple_ext(tmp_path / "o", "Older", minimum_chrome_version="100"))
    assert kind == "success" and "Chrome" not in text


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Dark Reader (fontSettings), Old Reddit Redirect (cookies)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_font_list(harness, tmp_path):
    entry = harness.install_ok(simple_ext(tmp_path / "f", "Fonts", permissions=["fontSettings"]), "Fonts")
    page = ext_page(harness, entry)
    fonts = run_js_async(page, "return await new Promise((r) => chrome.fontSettings.getFontList(r))")
    assert isinstance(fonts, list) and all(set(f) == {"fontId", "displayName"} for f in fonts)
    assert run_js_async(page, "return await chrome.fontSettings.getSomethingList()") == []  # other lists: empty, not undefined


def test_cookies_api(window, harness, server, tmp_path):
    entry = harness.install_ok(simple_ext(tmp_path / "c", "Cookie Jar", permissions=["cookies"], host_permissions=["http://127.0.0.1/*"]),
                               "Cookie Jar")
    page = ext_page(harness, entry)
    bridge = harness.controller.bridge
    wait_until(lambda: bridge._cookies_ready, 5)
    run_js(page, "window.__ev = []; chrome.cookies.onChanged.addListener((c) => window.__ev.push([c.removed, c.cause, c.cookie.value])); 1")
    spin(0.3)
    url = server.url("/")
    cookie = run_js_async(page, f"return await chrome.cookies.set({{url: {json.dumps(url)}, name: 'fg', value: '1'}})")
    assert {k: cookie[k] for k in ("name", "value", "domain", "hostOnly", "path", "session")} == \
        {"name": "fg", "value": "1", "domain": "127.0.0.1", "hostOnly": True, "path": "/", "session": True}
    assert run_js_async(page, f"return (await chrome.cookies.get({{url: {json.dumps(url)}, name: 'fg'}})).value") == "1"
    web = harness.fresh_page(server.page_url())
    assert "fg=1" in run_js(web, "document.cookie")
    run_js_async(page, f"return await chrome.cookies.set({{url: {json.dumps(url)}, name: 'fg', value: '2'}})")
    poll_js(page, "JSON.stringify(window.__ev)", lambda v: v.count("[") >= 4, 10, "cookie events")
    assert json.loads(run_js(page, "JSON.stringify(window.__ev)"))[:3] == \
        [[False, "explicit", "1"], [True, "overwrite", "1"], [False, "explicit", "2"]]
    harness.profile.cookieStore().setCookie(_cookie(b"elsewhere", ".example.com"), QUrl("https://www.example.com/"))
    spin(0.5)
    names = [c["name"] for c in run_js_async(page, "return await chrome.cookies.getAll({})")]
    assert "fg" in names and "elsewhere" not in names  # no host access to example.com
    assert run_js_async(page, "return await chrome.cookies.getAllCookieStores()")[0]["id"] == "0"
    with pytest.raises(AssertionError, match="No host permissions"):
        run_js_async(page, "return await chrome.cookies.set({url: 'https://www.example.com/', name: 'x', value: 'y'})")
    run_js_async(page, f"return await chrome.cookies.remove({{url: {json.dumps(url)}, name: 'fg'}})")
    poll_js(page, "JSON.stringify(window.__ev)", lambda v: '[true,"explicit","2"]' in v, 10, "the removal event")
    assert run_js_async(page, f"return await chrome.cookies.get({{url: {json.dumps(url)}, name: 'fg'}})") is None


def _cookie(name: bytes, domain: str) -> QNetworkCookie:
    cookie = QNetworkCookie(name, b"v")
    cookie.setDomain(domain)
    cookie.setPath("/")
    return cookie


# ══════════════════════════════════════════════════════════════════════════════════════════
#  chrome.scripting.registerContentScripts (uBlock Origin Lite's cosmetic filters)
# ══════════════════════════════════════════════════════════════════════════════════════════
REG_JS = "document.documentElement.setAttribute('data-reg', typeof chrome.runtime.sendMessage);\n"


def test_registered_content_scripts(window, harness, server, tmp_path, fg):
    entry = harness.install_ok(simple_ext(tmp_path / "r", "Registrar", {"reg.js": REG_JS, "reg.css": "html { outline: 3px solid rgb(1, 2, 3); }"},
                                          permissions=["storage", "scripting"], host_permissions=["http://127.0.0.1/*"]), "Registrar")
    page = ext_page(harness, entry)
    register = lambda scripts: run_js_async(page, f"await chrome.scripting.registerContentScripts({json.dumps(scripts)}); return true")
    register([{"id": "reg", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"], "css": ["reg.css"], "runAt": "document_end"},
              {"id": "far", "matches": ["https://example.com/*"], "js": ["reg.js"]}])
    got = run_js_async(page, "return await chrome.scripting.getRegisteredContentScripts({ids: ['reg']})")
    assert got == [{"id": "reg", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"], "css": ["reg.css"], "runAt": "document_end",
                    "allFrames": False, "persistAcrossSessions": True, "matchOriginAsFallback": False, "world": "ISOLATED"}]
    for scripts, error in (([{"id": "reg", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}], "Duplicate script ID"),
                           ([{"id": "x", "matches": ["http://127.0.0.1/*"], "js": ["missing.js"]}], "Could not load javascript"),
                           ([{"id": "y", "matches": ["http://a.*/*"], "js": ["reg.js"]}], "invalid match pattern"),
                           ([{"id": "_z", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}], "must not start with"),
                           ([{"id": "w", "js": ["reg.js"]}], "must specify 'matches'")):
        with pytest.raises(AssertionError, match=error):
            register(scripts)
    with pytest.raises(AssertionError, match="Nonexistent script ID"):
        run_js_async(page, "await chrome.scripting.unregisterContentScripts({ids: ['nope']}); return true")
    installed = json.loads((Path(entry.path) / "manifest.json").read_text())
    assert installed["content_scripts"] == [{"matches": ["http://127.0.0.1/*"], "js": ["foxglove-shim.js", "reg.js"],
                                             "css": ["reg.css"], "run_at": "document_end"}]  # example.com: no host access
    controller = harness.controller  # Qt reads content scripts when it loads an extension: Foxglove reloads it
    wait_until(lambda: entry.id not in controller._reloads and entry.id not in controller._updates, 20, "the reload")
    harness.wait_state(entry.id, True)
    web = harness.fresh_page(server.page_url())
    assert wait_attr(web, "data-reg") == "function"  # with the polyfill and chrome.*
    assert run_js(web, "getComputedStyle(document.documentElement).outlineColor") == "rgb(1, 2, 3)"
    page = ext_page(harness, entry)
    run_js_async(page, "await chrome.scripting.unregisterContentScripts(); return true")
    assert run_js_async(page, "return await chrome.scripting.getRegisteredContentScripts()") == []
    assert "content_scripts" not in json.loads((Path(controller.entry(entry.id).path) / "manifest.json").read_text())
    wait_until(lambda: entry.id not in controller._reloads and entry.id not in controller._updates, 20, "the reload")
    harness.wait_state(entry.id, True)


def test_what_a_polyfill_update_keeps(harness, tmp_path):
    entry = harness.install_ok(simple_ext(tmp_path / "k", "Keeper"), "Keeper")
    bridge, registry = harness.controller.bridge, harness.controller.registry
    state = registry[entry.id]
    state.update(menus=[{"id": "m"}], alarms={"a": {}}, scripts=[{"id": "s"}], listeners=["tabs.onUpdated"])
    bridge.installed(entry.id, {"reason": "chrome_update"})  # a newer Foxglove: like a Chrome update
    assert (state.get("menus"), state.get("alarms"), state.get("scripts"), state.get("listeners")) == \
        ([{"id": "m"}], {"a": {}}, [{"id": "s"}], ["tabs.onUpdated"])  # the same worker code: the same listeners
    bridge.installed(entry.id, {"reason": "update", "previousVersion": "0.9"})  # a new version of the extension
    assert not any(k in state for k in ("menus", "alarms", "scripts", "listeners"))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  A newer polyfill reaches the service worker too (after a Foxglove update)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_newer_polyfill_reaches_the_worker(server, tmp_path):
    exts = [{"name": n, "tag": t, "path": str(eb.api_extension(tmp_path / t, name=n))} for n, t in (("Sink A", "sa"), ("Sink B", "sb"))]
    args = {"root": str(tmp_path / "run"), "profile": "upgrade", "page_url": server.url("/page?upgrade"), "exts": exts,
            "disable": ["Sink B"], "enabled": ["Sink A"], "upgrade": True, "marks": ["Sink A"], "enable": ["Sink B"]}
    _run_phase("install", args)
    after = _run_phase("report", args)
    assert "wait_error" not in after, after
    assert after["marks"] == {"Sink A": {"page": "upgraded", "worker": "upgraded"}, "Sink B": {"page": "upgraded", "worker": "upgraded"}}
    assert after["shimmed"] == {"Sink A": True, "Sink B": True}


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Pop-ups: a last child's margin that collapses through the body
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_popup_height_includes_a_collapsed_bottom_margin(window, harness, tmp_path, fg):
    files = eb.probe_files("Margins", "margins")
    files["popup.html"] = ('<!doctype html><html><head><meta charset="utf-8"></head><body>'
                           '<div style="width: 200px; height: 100px; margin-bottom: 15px">list</div>'
                           '<script src="popup.js"></script></body></html>')
    entry = harness.install_ok(eb.write_tree(tmp_path / "margins", eb.probe_manifest("Margins", "margins"), files), "Margins")
    window.open_extension(entry.id)
    popup = wait_until(lambda: next((p for p in window.findChildren(fg.ExtensionPopup) if p.isVisible()), None))
    wait_until(lambda: popup.view.size() == QSize(216, 123), 10, f"216x123 (is {popup.view.size()})")  # 8 + 100 + max(8, 15)
    assert run_js(popup.page, "document.documentElement.scrollHeight <= innerHeight")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Pure helpers
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("pattern, ok", [
    ("<all_urls>", True), ("*://*/*", True), ("*://*.example.com/*", True), ("http://127.0.0.1:8080/*", True),
    ("file:///*", True), ("http://a.*/*", False), ("*://*", False), ("chrome-extension://*/*", False), ("", False), (5, False)])
def test_valid_match_patterns(fg, pattern, ok):
    assert fg.valid_match_pattern(pattern) is ok


def test_registered_scripts_in_manifest_form(fg):
    assert fg.registered_as_manifest([{"id": "a", "matches": ["<all_urls>"], "excludeMatches": [], "js": ["a.js"], "allFrames": True,
                                       "runAt": "document_start", "world": "MAIN", "persistAcrossSessions": True}]) == \
        [{"matches": ["<all_urls>"], "js": ["a.js"], "all_frames": True, "run_at": "document_start", "world": "MAIN"}]
    assert fg.registered_as_manifest([{"id": "b", "matches": ["<all_urls>"], "css": ["b.css"], "world": "ISOLATED"}]) == \
        [{"matches": ["<all_urls>"], "css": ["b.css"]}]
    assert fg.wire_content_scripts([{"js": ["a.js"], "matches": ["<all_urls>"]}, {"js": ["b.js"], "world": "MAIN"}, {"css": ["c.css"]}]) == \
        [{"js": ["foxglove-shim.js", "a.js"], "matches": ["<all_urls>"]}, {"js": ["b.js"], "world": "MAIN"}, {"css": ["c.css"]}]
