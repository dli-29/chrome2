"""Third review round. Web pages can't read the polyfill's files or fill Foxglove's buffers; only the user grants
activeTab and answers permission prompts; packages can't write outside their folder. Extensions work as in Chrome:
declarativeNetRequest rules are enforced, registered content scripts apply at once, a page's storage change reaches
the worker, titles are localized, insertCSS works under a strict CSP, an extension's frames in pages get their files."""
from __future__ import annotations

import json
import shutil
import time
import uuid
from pathlib import Path

import pytest
from PyQt6.QtCore import QPoint, Qt, QUrl
from PyQt6.QtTest import QTest

import extbuilder as eb
from helpers import TEST_PAGE, run_js, run_js_async, spin, wait_attr, wait_until
from test_extension_apis import install_sink, load_tab
from test_extension_review import OPT_HTML, OPT_JS, ext_page, simple_ext


def fetch_result(page, url: str) -> str:
    return run_js_async(page, f"""const c = new AbortController(); setTimeout(() => c.abort(), 5000);
      try {{ const r = await fetch({json.dumps(url)}, {{signal: c.signal}}); const t = await r.text();
             return String(r.status) + (t.includes('FOXGLOVE_CFG') ? ' LEAK' : ''); }}
      catch (e) {{ return 'refused'; }}""", timeout=10)


def wait_url(tab, url: str, timeout: float = 20.0) -> None:
    wait_until(lambda: tab.url().toString() == url and not tab.loading, timeout, f"the tab at {url} (is {tab.url().toString()})")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Web pages: no way to the polyfill's files (its token, its event names) or into Foxglove's buffers
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_encoded_dots_dont_leave_web_accessible_files(window, harness, server, tmp_path):
    files = {"img/a.png": eb.tiny_png(), "secret.js": "var SECRET = 's3cr3t';\n"}
    entry = harness.install_ok(simple_ext(tmp_path / "w", "Warry", files, web_accessible_resources=[
        {"resources": ["img/*"], "matches": ["<all_urls>"]}]), "Warry")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    base = f"foxglove-ext://{entry.id}/"
    assert fetch_result(tab.page, base + "img/a.png") == "200"
    for path in ("foxglove-shim.js", "FOXGLOVE-SHIM.JS", "img/..%2Ffoxglove-shim.js", "img/..%2ffoxglove-manifest.json",
                 "img/..%2Fsecret.js", "img/%2E%2E%2Fsecret.js", "img/..%5Csecret.js", "img/%2e%2e/secret.js",
                 "img/..%2F..%2F..%2F..%2Fetc%2Fpasswd", "secret.js", "img/%00a.png"):
        assert fetch_result(tab.page, base + path) in ("refused", "404"), path


def test_web_pages_cant_hold_call_parts(window, harness, server, tmp_path):
    _entry, page = install_sink(harness, tmp_path)
    web = harness.fresh_page(server.page_url())
    statuses = run_js_async(web, """return await Promise.all(Array.from({length: 70}, async (_, i) => {
        const c = new AbortController(); setTimeout(() => c.abort(), 5000);
        try { return (await fetch('foxglove-ext://bridge/call?part=attacker' + String(i).padStart(4, '0') + '.0.700',
                                  {method: 'POST', body: '\\nx', signal: c.signal})).status; }
        catch (e) { return e.name; } }));""", timeout=20)
    assert 200 not in statuses and "AbortError" not in statuses, statuses  # refused at once
    assert not harness.controller.bridge._parts
    started = time.monotonic()
    count = run_js_async(page, """const rules = Array.from({length: 100}, (_, i) => ({id: i + 1, priority: 1, action: {type: 'block'},
        condition: {urlFilter: 'ads-' + i + '-' + 'x'.repeat(150)}}));
      await chrome.declarativeNetRequest.updateDynamicRules({addRules: rules});
      return (await chrome.declarativeNetRequest.getDynamicRules()).length;""", timeout=15)
    assert count == 100 and time.monotonic() - started < 5


BIG_CS = """chrome.runtime.onMessage.addListener((m, s, reply) => { if (m.big) reply('x'.repeat(m.big)); });
document.documentElement.setAttribute('data-big', 'ready');
"""


def test_content_scripts_send_big_calls_in_parts(window, harness, server, tmp_path):
    entry = harness.install_ok(simple_ext(tmp_path / "b", "Bigmouth", {"cs.js": BIG_CS}, host_permissions=["http://127.0.0.1/*"],
                                          content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}]), "Bigmouth")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    assert wait_attr(tab.page, "data-big") == "ready"
    page = ext_page(harness, entry)
    assert run_js_async(page, f"return (await chrome.tabs.sendMessage({tab.tab_id}, {{big: 50000}})).length", timeout=15) == 50000


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Packages: the worker's path can't make Foxglove write outside the extension
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("worker", ["../../escaped/deeper/sw.js", "/../escaped/sw.js", "a/../../escaped/sw.js"])
def test_worker_path_stays_inside_the_extension(fg, tmp_path, worker):
    root = simple_ext(tmp_path / "stage" / "ext", "Escaper")
    manifest = fg.load_manifest(root)
    manifest["background"] = {"service_worker": worker}
    with pytest.raises(fg.InstallError, match="outside the extension"):
        fg.inject_shim(root, manifest, "0" * 32, "en_US")
    assert not list(tmp_path.rglob(fg.SHIM_WORKER)) and not (tmp_path / "escaped").exists()


def test_install_refuses_an_escaping_worker(harness, tmp_path):
    src = simple_ext(tmp_path / "x", "Escaper")
    manifest = json.loads((src / "manifest.json").read_text())
    manifest["background"] = {"service_worker": "../../escaped/sw.js"}
    (src / "manifest.json").write_text(json.dumps(manifest))
    kind, text = harness.install(src)
    assert kind == "error" and "outside the extension" in text
    assert not list(harness.dir.rglob("escaped")) and harness.leftover_staging() == []


# ══════════════════════════════════════════════════════════════════════════════════════════
#  activeTab and permission prompts come from the user alone
# ══════════════════════════════════════════════════════════════════════════════════════════
CLICK_SW = "chrome.action.onClicked.addListener((tab) => chrome.storage.local.set({clicked: tab.id}));\n"


def test_open_popup_is_not_the_user(window, harness, server, tmp_path, fg):
    files = {"popup.html": '<!doctype html><meta charset="utf-8"><p>pop</p>'}
    popper = harness.install_ok(simple_ext(tmp_path / "p", "Popper", files, permissions=["activeTab", "scripting"],
                                           action={"default_popup": "popup.html"}), "Popper")
    clicker = harness.install_ok(simple_ext(tmp_path / "c", "Clicker", sw=CLICK_SW, permissions=["activeTab", "scripting", "storage"],
                                            action={}), "Clicker")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    read = f"return (await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, func: () => document.title}}))[0].result"
    page, other = ext_page(harness, popper), ext_page(harness, clicker)
    with pytest.raises(AssertionError, match="Cannot access contents"):
        run_js_async(page, read)
    run_js_async(page, "await chrome.action.openPopup(); return true")
    wait_until(lambda: any(p.isVisible() and p.ext_id == popper.id for p in window.findChildren(fg.ExtensionPopup)), 10, "the pop-up")
    with pytest.raises(AssertionError, match="Cannot access contents"):
        run_js_async(page, read)  # the pop-up is open, but the user didn't ask for it
    window.close_extension_popups()
    window.open_extension(popper.id)  # the toolbar button
    assert run_js_async(page, read) == "Foxglove test page"
    window.close_extension_popups()
    with pytest.raises(AssertionError, match="does not have a popup"):
        run_js_async(other, "await chrome.action.openPopup(); return true")
    spin(0.5)
    assert run_js_async(other, "return (await chrome.storage.local.get('clicked')).clicked ?? null") is None  # no fake click
    with pytest.raises(AssertionError, match="Cannot access contents"):
        run_js_async(other, read)


ASK_JS = OPT_JS + """document.addEventListener('click', () => chrome.permissions.request({permissions: ['downloads']}).then(
  (v) => document.documentElement.setAttribute('data-r', String(v)), (e) => document.documentElement.setAttribute('data-r', 'ERR ' + e.message)));
"""


def test_permission_prompts_need_a_gesture_and_dont_repeat(window, harness, tmp_path, fg, monkeypatch):
    entry = harness.install_ok(simple_ext(tmp_path / "a", "Asker", {"options.js": ASK_JS}, optional_permissions=["downloads"]), "Asker")
    page = ext_page(harness, entry)
    with pytest.raises(AssertionError, match="user gesture"):
        run_js_async(page, "return await chrome.permissions.request({permissions: ['downloads']})")
    asked = []
    monkeypatch.setattr(fg, "ask_question", lambda *a, **k: asked.append(a[2]) and False)
    window.open_url(entry.options_url, "tab")
    tab = wait_until(lambda: next((t for t in window.tabs() if t.url() == entry.options_url and not t.loading), None), 15)
    assert wait_attr(tab.page, "data-options") == "ready"

    def click() -> str:
        run_js(tab.page, "document.documentElement.removeAttribute('data-r')")
        QTest.mouseClick(tab.view.focusProxy(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(30, 30))
        return wait_attr(tab.page, "data-r")

    wait_until(lambda: tab.view.focusProxy() is not None, 5, "the page's widget")
    assert click() == "false" and len(asked) == 1  # asked, and the user said no
    assert click() == "false" and len(asked) == 1  # not asked again right away
    bridge = harness.controller.bridge
    bridge._refused.clear()
    bridge._asking.add(entry.id)  # a question is open
    with pytest.raises(fg.ApiError, match="already in progress"):
        bridge.api_permissions_request(entry.id, {"permissions": ["downloads"]}, {"from": "", "cs": False, "tab": None})
    bridge._asking.clear()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  chrome.declarativeNetRequest: rule sets, dynamic and session rules are enforced
# ══════════════════════════════════════════════════════════════════════════════════════════
DNR_RULES = [
    {"id": 1, "priority": 1, "action": {"type": "block"}, "condition": {"urlFilter": "/ads/", "resourceTypes": ["image", "script"]}},
    {"id": 2, "priority": 2, "action": {"type": "allow"}, "condition": {"urlFilter": "/ads/allowed", "resourceTypes": ["image"]}},
    {"id": 3, "action": {"type": "redirect", "redirect": {"transform": {"path": "/page", "query": "?moved=1"}}},
     "condition": {"urlFilter": "/old-page", "resourceTypes": ["main_frame"]}},
    {"id": 4, "action": {"type": "redirect", "redirect": {"extensionPath": "/surrogate.js"}},
     "condition": {"urlFilter": "/tracker.js", "resourceTypes": ["script"]}},
    {"id": 5, "action": {"type": "modifyHeaders", "requestHeaders": [{"header": "X-Foxglove", "operation": "set", "value": "ruled"}]},
     "condition": {"urlFilter": "/echo", "resourceTypes": ["xmlhttprequest"]}},
    {"id": 6, "action": {"type": "redirect", "redirect": {"transform": {"queryTransform": {"removeParams": ["utm_source"]}}}},
     "condition": {"urlFilter": "utm_source=", "resourceTypes": ["main_frame"]}},
    {"id": 7, "action": {"type": "upgradeScheme"}, "condition": {"urlFilter": "/upgrade-me", "resourceTypes": ["image"]}},
    {"id": 8, "action": {"type": "redirect", "redirect": {"regexSubstitution": "\\1/page?from=\\2"}},
     "condition": {"regexFilter": "^(http://127\\.0\\.0\\.1:\\d+)/regex-(\\w+)$", "resourceTypes": ["main_frame"]}},
]
DNR_PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>dnr</title></head><body>
<script>const n = location.search.slice(1);
for (const p of ['/ads/banner.png', '/ads/allowed.png', '/ok.png', '/dyn.png', '/sess.png', '/extra.png', '/upgrade-me.png', '/nohost.png']) {
  const i = new Image(); i.src = p + '?' + n; document.body.appendChild(i); }
fetch('/echo?' + n).then((r) => document.documentElement.setAttribute('data-echo', String(r.status)));</script>
<script src="/tracker.js"></script></body></html>"""
IMAGES = ("/ads/banner.png", "/ads/allowed.png", "/ok.png", "/dyn.png", "/sess.png", "/extra.png", "/upgrade-me.png", "/nohost.png")


def dnr_page(tab, server) -> set[str]:
    """Load the test page in *tab*: which of its images reached the server."""
    n = uuid.uuid4().hex[:8]
    load_tab(tab, server.url(f"/dnr?{n}"))
    assert wait_attr(tab.page, "data-echo") == "404"
    spin(0.5)
    return {p for p in IMAGES if f"{p}?{n}" in server.requests}


def test_declarative_net_request_rules_are_enforced(window, harness, server, tmp_path):
    server.add("/dnr", DNR_PAGE, "text/html; charset=utf-8")
    server.add("/tracker.js", b"document.documentElement.setAttribute('data-tracker', 'real');\n", "text/javascript")
    files = {"rules/main.json": json.dumps(DNR_RULES), "surrogate.js": "document.documentElement.setAttribute('data-tracker', 'surrogate');\n",
             "rules/extra.json": json.dumps([{"id": 1, "action": {"type": "block"}, "condition": {"urlFilter": "/extra.png"}}])}
    entry = harness.install_ok(simple_ext(tmp_path / "d", "Blocky", files, permissions=["storage", "declarativeNetRequest"],
                                          host_permissions=["http://127.0.0.1/*"],
                                          declarative_net_request={"rule_resources": [
                                              {"id": "main", "enabled": True, "path": "rules/main.json"},
                                              {"id": "extra", "enabled": False, "path": "/rules/extra.json"}]},
                                          web_accessible_resources=[{"resources": ["surrogate.js"], "matches": ["<all_urls>"]}]), "Blocky")
    nohost = harness.install_ok(simple_ext(tmp_path / "n", "No Hosts", permissions=["declarativeNetRequest"]), "No Hosts")
    tab = window.current_tab()
    got = dnr_page(tab, server)
    assert got == {"/ads/allowed.png", "/ok.png", "/dyn.png", "/sess.png", "/extra.png", "/nohost.png"}  # blocked; allowed; upgraded
    assert run_js(tab.page, "document.documentElement.getAttribute('data-tracker')") == "surrogate"  # redirected to the extension
    assert not [r for r in server.requests if r.startswith("/tracker.js")]
    assert next(v for k, v in server.headers.items() if k.startswith("/echo")).get("X-Foxglove") == "ruled"

    page, other = ext_page(harness, entry), ext_page(harness, nohost)
    run_js_async(page, f"""await chrome.declarativeNetRequest.updateDynamicRules({{addRules: [
        {{id: 1, action: {{type: 'block'}}, condition: {{urlFilter: '/dyn.png'}}}}]}});
      await chrome.declarativeNetRequest.updateSessionRules({{addRules: [
        {{id: 1, action: {{type: 'block'}}, condition: {{urlFilter: '/sess.png', tabIds: [{tab.tab_id}]}}}}]}});
      await chrome.declarativeNetRequest.updateEnabledRulesets({{enableRulesetIds: ['extra']}}); return true""")
    run_js_async(other, f"""await chrome.declarativeNetRequest.updateDynamicRules({{addRules: [
        {{id: 1, action: {{type: 'block'}}, condition: {{urlFilter: '/nohost.png'}}}},
        {{id: 2, action: {{type: 'redirect', redirect: {{url: {json.dumps(server.url('/page?stolen'))}}}}},
          condition: {{urlFilter: '/nohost-redirect', resourceTypes: ['main_frame']}}}}]}}); return true""")
    assert dnr_page(tab, server) == {"/ads/allowed.png", "/ok.png"}
    other_tab = window.new_tab(QUrl("about:blank"))
    assert dnr_page(other_tab, server) == {"/ads/allowed.png", "/ok.png", "/sess.png"}  # the session rule is for the first tab

    for path, landed in (("/old-page?x=1", "/page?moved=1"), ("/page?utm_source=mail&keep=1", "/page?keep=1"),
                         ("/regex-abc", "/page?from=abc"), ("/nohost-redirect", "/nohost-redirect")):  # redirects need host access
        tab.load(QUrl(server.url(path)))
        wait_url(tab, server.url(landed))
    harness.controller.set_enabled(entry.id, False)
    harness.controller.set_enabled(nohost.id, False)
    assert dnr_page(tab, server) == set(IMAGES)  # nothing applies any more


def test_rule_precedence(fg):
    def ext(rules):
        return fg.NetExtension("e" * 32, [fg.RuleIndex(rules, "set")], ["<all_urls>"], False)

    net = fg.NetRules(None)
    page = QUrl("https://site.example/")

    def outcome(rules, url="https://cdn.example/ads/x.js", kind="script", initiator=page):
        req = net._request(QUrl(url), kind, "get", initiator, page, None)
        decided = net.decide(ext(rules), req, page)
        return decided if decided is None else (decided[0].kind if decided[0] else None, [r.id for r in decided[1]])

    block = {"id": 1, "action": {"type": "block"}, "condition": {"urlFilter": "/ads/"}}
    assert outcome([block]) == ("block", [])
    assert outcome([block, {"id": 2, "priority": 2, "action": {"type": "allow"}, "condition": {"urlFilter": "x.js"}}]) == ("allow", [])
    assert outcome([block, {"id": 2, "action": {"type": "redirect", "redirect": {"url": "https://a/"}}, "condition": {"urlFilter": "x.js"}}])[0] == "block"
    assert outcome([block], kind="main_frame") == (None, [])  # main frames only when a rule names them
    assert outcome([{**block, "condition": {"urlFilter": "/ads/", "domainType": "firstParty"}}]) == (None, [])
    assert outcome([{**block, "condition": {"urlFilter": "/ads/", "initiatorDomains": ["example"]}}]) == ("block", [])
    assert outcome([{**block, "condition": {"urlFilter": "/ads/", "excludedInitiatorDomains": ["site.example"]}}]) == (None, [])
    assert outcome([{**block, "condition": {"urlFilter": "/ads/", "requestMethods": ["post"]}}]) == (None, [])
    assert outcome([{**block, "condition": {"urlFilter": "/ads/", "tabIds": [3]}}]) is None  # needs the tab
    allow_page = {"id": 3, "priority": 5, "action": {"type": "allowAllRequests"},
                  "condition": {"requestDomains": ["site.example"], "resourceTypes": ["main_frame"]}}
    headers = {"id": 4, "priority": 9, "action": {"type": "modifyHeaders", "requestHeaders": [{"header": "a", "operation": "set", "value": "b"}]},
               "condition": {"urlFilter": "x.js"}}
    assert outcome([block, allow_page, headers]) == (None, [4])  # the page is allowed; a higher rule still changes headers


@pytest.mark.parametrize("pattern, url, hit", [
    ("||example.com^", "https://example.com/x", True), ("||example.com^", "https://a.example.com:8080/", True),
    ("||example.com^", "https://notexample.com/", False), ("||example.com^", "https://example.com.evil.net/", False),
    ("|https://a.com/x|", "https://a.com/x", True), ("|https://a.com/x|", "https://a.com/xy", False),
    ("/ads/*banner", "http://h/ads/big-banner.png", True), ("^ad^", "http://h/x/ad/y", True), ("^ad^", "http://h/x/adx", False),
    ("^ad^", "http://h/x/ad", True), ("ADS", "http://h/ads", True)])
def test_url_filters(fg, pattern, url, hit):
    assert fg.UrlFilter(pattern).search(url) is hit


@pytest.mark.parametrize("pattern, keys", [
    ("||example.com^", ["d:example.com"]), ("||example.com/path", ["d:example.com"]), ("||example.com", ["t:example"]),
    ("/ads/*banner", ["t:ads"]), ("|https://*", []), ("ads", []), ("||go.*.com/smartpop/", ["t:smartpop"]),
    ("&adslot=", ["t:adslot"])])
def test_rule_index_keys(fg, pattern, keys):
    assert fg._filter_keys(pattern) == keys


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Registered content scripts work without a restart
# ══════════════════════════════════════════════════════════════════════════════════════════
REG_JS = "document.documentElement.setAttribute('data-reg', typeof chrome.runtime.sendMessage);\n"


def test_registered_content_scripts_apply_at_once(window, harness, server, tmp_path, monkeypatch):
    entry = harness.install_ok(simple_ext(tmp_path / "r", "Registrar", {"reg.js": REG_JS}, permissions=["storage", "scripting"],
                                          host_permissions=["http://127.0.0.1/*"]), "Registrar")
    page = ext_page(harness, entry)
    run_js_async(page, "await chrome.scripting.registerContentScripts([{id: 'reg', matches: ['http://127.0.0.1/*'], js: ['reg.js']}]); return true")
    controller = harness.controller
    wait_until(lambda: entry.id not in controller._reloads and entry.id not in controller._updates, 20, "the extension to reload")
    harness.wait_state(entry.id, True)
    web = harness.fresh_page(server.page_url())
    assert wait_attr(web, "data-reg") == "function"
    page = ext_page(harness, entry)
    assert [s["id"] for s in run_js_async(page, "return await chrome.scripting.getRegisteredContentScripts()")] == ["reg"]
    reloads = []
    monkeypatch.setattr(controller.bridge, "extension_disabled", lambda ext_id: reloads.append(ext_id))
    run_js_async(page, """await chrome.scripting.unregisterContentScripts();  // as some extensions do at every start
      await chrome.scripting.registerContentScripts([{id: 'reg', matches: ['http://127.0.0.1/*'], js: ['reg.js']}]); return true""")
    spin(2.0)
    assert reloads == [] and entry.id not in controller._reloads  # the same scripts as loaded: no reload (no loop)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  A page's storage change reaches the worker (Foxglove wakes a stopped one) - once
# ══════════════════════════════════════════════════════════════════════════════════════════
WAKE_SW = """let seen = 0;
chrome.storage.onChanged.addListener((changes) => { if (changes.flag) seen++; });
chrome.runtime.onMessage.addListener((m, s, reply) => { if (m === 'seen') reply(seen); });
"""


def test_page_storage_changes_reach_the_worker_once(harness, tmp_path, monkeypatch):
    entry = harness.install_ok(simple_ext(tmp_path / "w", "Waker", sw=WAKE_SW), "Waker")
    page = ext_page(harness, entry)
    controller = harness.controller
    wait_until(lambda: "storage.onChanged" in (controller.registry.get(entry.id) or {}).get("listeners", []), 10, "the worker's listener")
    bridge, sent = controller.bridge, []
    emit = bridge.emit
    monkeypatch.setattr(bridge, "emit", lambda *a, **k: (sent.append(k), emit(*a, **k))[1])
    run_js_async(page, "await chrome.storage.local.set({flag: 1}); return true")
    wait_until(lambda: any(k.get("worker_only") and k.get("eid") for k in sent), 10, "the change sent to the worker")
    spin(1.0)
    assert run_js_async(page, "return await chrome.runtime.sendMessage('seen')") == 1  # the same event id: not twice


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Titles, command descriptions in the user's language
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_localized_toolbar_title_and_commands(window, harness, tmp_path):
    src = eb.localized_extension(tmp_path / "l")
    manifest = json.loads((src / "manifest.json").read_text())
    manifest.update(commands={"toggle": {"description": "__MSG_cmdToggle__", "suggested_key": {"default": "Alt+Shift+Y"}}},
                    options_page="options.html")
    (src / "manifest.json").write_text(json.dumps(manifest))
    messages = src / "_locales" / "en" / "messages.json"
    messages.write_text(json.dumps({**json.loads(messages.read_text()), "cmdToggle": {"message": "Toggle it"}}))
    entry = harness.install_ok(src, "Localized Probe")
    button = wait_until(lambda: window.extension_button(entry.id), 10, "the toolbar button")
    wait_until(lambda: button.toolTip() == "Localized Probe", 5, f"the localized title (is {button.toolTip()!r})")
    page = harness.fresh_page(entry.options_url.toString())
    assert run_js_async(page, "return await chrome.action.getTitle({})") == "Localized Probe"
    assert run_js_async(page, "return (await chrome.commands.getAll()).map((c) => c.description)") == ["Toggle it"]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  insertCSS under a strict CSP; an extension's frame in a page (Vimium's bars)
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_insert_css_ignores_the_pages_csp(window, harness, server, tmp_path):
    server.routes["/csp-page"] = (200, "text/html; charset=utf-8", TEST_PAGE, {"Content-Security-Policy": "style-src 'self'"})
    entry = harness.install_ok(simple_ext(tmp_path / "s", "Styler", permissions=["scripting"], host_permissions=["http://127.0.0.1/*"]), "Styler")
    page = ext_page(harness, entry)
    tab = window.current_tab()
    load_tab(tab, server.url("/csp-page"))
    color = lambda: run_js(tab.page, "getComputedStyle(document.getElementById('content')).color")
    css = f"{{target: {{tabId: {tab.tab_id}}}, css: '#content {{ color: rgb(1, 2, 3) !important; }}'}}"
    run_js_async(page, f"await chrome.scripting.insertCSS({css}); return 1")
    wait_until(lambda: color() == "rgb(1, 2, 3)", 5, "the inserted CSS")
    run_js_async(page, f"await chrome.scripting.removeCSS({css}); return 1")
    wait_until(lambda: color() == "rgb(0, 0, 0)", 5, "the CSS to go")


FRAME_CS = r"""(() => {
  const root = document.documentElement;
  window.addEventListener('message', (e) => { if (e.data && e.data.from === 'frame') root.setAttribute('data-frame', e.data.text); });
  const f = document.createElement('iframe');
  f.src = chrome.runtime.getURL('ui/frame.html');
  f.addEventListener('load', () => f.contentWindow.postMessage('hello', chrome.runtime.getURL('')));
  document.body.appendChild(f);
})();
"""
FRAME_FILES = {"cs.js": FRAME_CS, "ui/frame.css": "html { color: rgb(4, 5, 6); }\n",
               "ui/frame.html": '<!doctype html><meta charset="utf-8"><link rel="stylesheet" href="frame.css"><script src="frame.js"></script>',
               "ui/frame.js": "addEventListener('message', (e) => parent.postMessage({from: 'frame', text: e.data + ' '"
                              " + getComputedStyle(document.documentElement).color}, '*'));\n"}


def test_an_extension_frame_in_a_page_gets_its_files_and_messages(window, harness, server, tmp_path):
    harness.install_ok(simple_ext(tmp_path / "f", "Framer", FRAME_FILES, host_permissions=["http://127.0.0.1/*"],
                                  content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"], "run_at": "document_end"}],
                                  web_accessible_resources=[{"resources": ["ui/frame.html"], "matches": ["http://127.0.0.1/*"]}]), "Framer")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    assert wait_attr(tab.page, "data-frame", 15) == "hello rgb(4, 5, 6)"  # its own (not web-accessible) files, the message


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Alarms: Chrome's 30-second minimum for packed extensions
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_alarm_minimum_for_packed_extensions(harness, tmp_path):
    controller = harness.controller
    folder = harness.install_ok(simple_ext(tmp_path / "u", "Unpacked"), "Unpacked")
    key = eb.new_key()
    packed = harness.install_ok(eb.write_crx3(simple_ext(tmp_path / "p", "Packed"), tmp_path / "p.crx", key), "Packed")
    assert controller.registry[folder.id]["unpacked"] is True and controller.registry[packed.id]["unpacked"] is False
    now, bridge = time.time() * 1000, controller.bridge
    for ext_id in (folder.id, packed.id):
        bridge.api_alarms_create(ext_id, {"name": "t", "delayInMinutes": 0.001, "periodInMinutes": 0.001}, {})
    p, u = controller.registry[packed.id]["alarms"]["t"], controller.registry[folder.id]["alarms"]["t"]
    assert p["periodInMinutes"] == 0.5 and p["scheduledTime"] >= now + 29_000
    assert u["periodInMinutes"] == 0.001 and u["scheduledTime"] < now + 5000
    for ext_id in (folder.id, packed.id):
        bridge.api_alarms_clearAll(ext_id, {}, {})


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Updates: a crash while the old version is being deleted never brings it back
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_a_crash_while_deleting_the_old_version_keeps_the_new_one(harness, tmp_path, fg, monkeypatch):
    src = eb.probe_extension(tmp_path / "c", "Crashy", "crashy", version="1.0")
    entry = harness.install_ok(src, "Crashy")
    eb.write_tree(src, eb.probe_manifest("Crashy", "crashy", version="2.0"))
    real, seen = shutil.rmtree, {}

    def killed_midway(path, *args, **kwargs):  # the process dies after deleting part of the old version
        p = Path(path)
        if p.name.startswith("previous-") and not seen:
            seen["note"] = Path(str(p) + ".restore").exists()
            (p / "manifest.json").unlink()
            return None
        return real(path, *args, **kwargs)

    monkeypatch.setattr(shutil, "rmtree", killed_midway)
    kind, text = harness.install(src)
    assert kind == "success" and "updated" in text
    monkeypatch.setattr(shutil, "rmtree", real)
    assert seen == {"note": False}  # its note was gone before the backup started to go
    assert harness.controller._recover_interrupted_updates() == []  # the next start leaves the new version alone
    assert json.loads((Path(entry.path) / fg.SHIM_ORIGINAL).read_text())["version"] == "2.0"
