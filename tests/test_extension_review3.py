"""Third review round: install IDs that skip the checks, request matching a web page can stall, registered content
scripts that break (or reload) their extension, session-only scripts, storage changes from closing pages, onStartup
after a Foxglove upgrade - and the smaller ones (activeTab, match patterns, event duplicates, updates, the bridge)."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from PyQt6.QtCore import QUrl

import extbuilder as eb
from helpers import run_js, run_js_async, spin, wait_attr, wait_until
from test_extension_review import ext_page, simple_ext


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-1: a manifest key is read as Chrome reads it, and an installed extension's ID is never taken over
# ══════════════════════════════════════════════════════════════════════════════════════════
def _pem(der: bytes) -> str:
    import base64
    body = base64.b64encode(der).decode("ascii")
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(body[i:i + 64] for i in range(0, len(body), 64)) + "\n-----END PUBLIC KEY-----\n"


def test_manifest_keys_parse_like_chrome(fg):
    key = eb.new_key()
    assert fg.manifest_key_bytes(key.manifest_key) == key.public_der
    assert fg.manifest_key_bytes(_pem(key.public_der)) == key.public_der
    assert fg.manifest_key_bytes(" " + key.manifest_key[:40] + "\n" + key.manifest_key[40:] + "\n") == key.public_der
    for bad in ("", "   ", "not base64!", key.manifest_key[:-2], "-----BEGIN PUBLIC KEY-----\nAAAA", 42, None):
        with pytest.raises(ValueError):
            fg.manifest_key_bytes(bad)


def _victim(harness, tmp_path):
    key = eb.new_key()
    victim = harness.install_ok(eb.write_crx3(simple_ext(tmp_path / "v", "Victim"), tmp_path / "v.crx", key), "Victim")
    page = ext_page(harness, victim)
    run_js_async(page, "await chrome.storage.local.set({secret: 'hunter2'}); return true")
    harness.drop_page(page)
    return key, victim, json.loads(json.dumps(harness.controller.registry[victim.id]))


def _victim_untouched(harness, victim, state) -> None:
    c = harness.controller
    assert {k: c.registry[victim.id].get(k) for k in ("source", "source_path", "unpacked")} == \
           {k: state.get(k) for k in ("source", "source_path", "unpacked")}
    entry = harness.wait_state(victim.id, True)
    assert [i.path() for i in c._infos() if i.id() == victim.id] == [victim.path]
    assert entry.name == "Victim"
    page = ext_page(harness, entry)
    assert run_js_async(page, "return (await chrome.storage.local.get('secret')).secret") == "hunter2"


@pytest.mark.parametrize("form", ["pem", "plain-after-pem"])
def test_a_pem_wrapped_key_cant_skip_the_replace_question(harness, tmp_path, form):
    key, victim, state = _victim(harness, tmp_path)
    attacker = simple_ext(tmp_path / "a", "Attacker", key=_pem(key.public_der))
    kind, text = harness.install(attacker)  # no window to ask in: refused, as for a plain copied key
    assert kind == "error" and text == "“Victim” is already installed from somewhere else. Remove it first to install this copy.", text
    if form == "plain-after-pem":  # the same folder again, plain key: still not "the same source" - it never was installed
        manifest = json.loads((attacker / "manifest.json").read_text())
        (attacker / "manifest.json").write_text(json.dumps({**manifest, "key": key.manifest_key}))
        kind, text = harness.install(attacker)
        assert kind == "error" and "already installed from somewhere else" in text, text
    _victim_untouched(harness, victim, state)
    assert harness.leftover_staging() == []


def test_an_invalid_manifest_key_is_refused(harness, tmp_path):
    kind, text = harness.install(simple_ext(tmp_path / "x", "Bad Key", key="-----BEGIN PUBLIC KEY-----\nnot*base64\n-----END PUBLIC KEY-----"))
    assert kind == "error" and text == "“Bad Key” can't be installed: its manifest.json has an invalid “key”.", text
    assert harness.entries() == [] and harness.leftover_staging() == []


def test_a_second_copy_under_an_installed_id_is_never_kept(harness, tmp_path, fg, monkeypatch):
    """Even when something gets past the checks before installing, the result is checked again."""
    key, victim, state = _victim(harness, tmp_path)
    monkeypatch.setattr(fg.ExtensionsController, "_on_staged", lambda self, job: self._install_staged(job))
    kind, text = harness.install(simple_ext(tmp_path / "a", "Attacker", key=key.manifest_key))
    assert kind == "error" and text == "Couldn't install “Attacker”: an extension with the same ID is already installed.", text
    folder = Path(victim.path).parent
    wait_until(lambda: [p.name for p in folder.iterdir()] == [Path(victim.path).name], 10, "the refused copy's folder to go")
    _victim_untouched(harness, victim, state)


def test_a_new_install_doesnt_inherit_a_stale_registry_entry(harness, tmp_path, fg):
    key = eb.new_key()
    c = harness.controller
    c.registry[key.ext_id] = {"granted": {"origins": ["<all_urls>"], "permissions": []}, "file_access": True,
                              "dnr": [{"id": 1, "action": {"type": "block"}, "condition": {"urlFilter": "x"}}]}
    entry = harness.install_ok(simple_ext(tmp_path / "n", "Newcomer", key=key.manifest_key), "Newcomer")
    state = c.registry[entry.id]
    assert entry.id == key.ext_id and not {"granted", "file_access", "dnr"} & set(state), state


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-2: no URL a web page makes up keeps the browser busy matching declarativeNetRequest rules
# ══════════════════════════════════════════════════════════════════════════════════════════
def _timed(fn):
    start = time.monotonic()
    value = fn()
    return value, time.monotonic() - start


def test_url_filters_match_in_linear_time(fg):
    """uBlock Origin Lite's "/site=*/viewid=*/size=": one 17 kB image URL used to freeze the browser for 15 s."""
    rule = fg.NetRule({"id": 1, "action": {"type": "block"}, "condition": {"urlFilter": "/site=*/viewid=*/size="}}, "r")
    net, page = fg.NetRules(None), QUrl("http://127.0.0.1/")
    request = lambda url: net._request(QUrl(url), "image", "get", page, page, None)
    for repeat in (600, 1200, 150_000):  # 8 kB, 17 kB, 2 MB
        hit, took = _timed(lambda: rule.matches(request("http://127.0.0.1/x.png?" + "/site=/viewid=" * repeat)))
        assert hit is False and took < 0.3, (repeat, took)
    assert rule.matches(request("http://127.0.0.1/a/site=1/viewid=2/size=3")) is True
    end = fg.NetRule({"id": 2, "action": {"type": "block"}, "condition": {"urlFilter": "*.js|"}}, "r")
    assert end.matches(request("http://127.0.0.1/a.js")) is True
    assert end.matches(request("http://127.0.0.1/" + "x" * 9000 + ".js")) is False  # past what rules look at: no end to match


@pytest.mark.parametrize("pattern, url, hit", [
    ("||b.com^", "https://a.b.com/", True), ("||b.com^", "https://user@b.com/", True), ("||b.com", "https://ab.com/", False),
    ("||b.com/x|", "https://b.com/x", True), ("||b.com/x|", "https://b.com/xy", False), ("|http*://*.js|", "http://h/a.js", True),
    ("*", "http://h/", True), ("a*b*c", "http://h/acb", False), ("a*b*c", "http://h/abbc", True), ("ad^", "http://h/ad", True),
    ("ad^^", "http://h/ad", True), ("ad^x", "http://h/ad", False), ("^ad^*|", "http://h/ad", True), ("AD", "http://h/ad", True)])
def test_url_filter_semantics(fg, pattern, url, hit):
    assert fg.UrlFilter(pattern).search(url) is hit
    assert fg.UrlFilter("AD", case=True).search("http://h/ad") is False


def test_regex_filters_like_chrome(fg, monkeypatch):
    for bad in (r"a(?=b)", r"(?<!x)y", r"(a)\1", "x" * 3000):  # RE2 (Chrome) refuses them: no rule
        rule = fg.NetRule({"id": 1, "action": {"type": "block"}, "condition": {"regexFilter": bad}}, "r")
        assert rule.matches(fg.NetRules(None)._request(QUrl("http://h/ab"), "image", "get", QUrl(), QUrl(), None)) is False
        assert rule.types == frozenset()
    assert fg.compile_regex_filter(r"\\1x").search("\\1x")  # an escaped backslash is no back-reference
    monkeypatch.setattr(fg, "_re2", None)  # Python's re: only the start of a long URL
    rule = fg.NetRule({"id": 1, "action": {"type": "block"},
                       "condition": {"regexFilter": r"^.*\/gambling\/main\/default\/.*?\/index\.html\?.*"}}, "r")
    net, page = fg.NetRules(None), QUrl("http://127.0.0.1/")
    hit, took = _timed(lambda: rule.matches(net._request(QUrl("http://h/" + "/gambling/main/default/" * 2000 + "/index.html"),
                                                         "image", "get", page, page, None)))
    assert hit is False and took < 0.3, took
    assert rule.matches(net._request(QUrl("http://h/gambling/main/default/a/index.html?x"), "image", "get", page, page, None))


def test_match_patterns_match_in_linear_time(fg):
    hit, took = _timed(lambda: fg.match_pattern("*://*/*a*a*b", "http://h/" + "a" * 3000))
    assert hit is False and took < 0.3, took
    assert fg.match_pattern("*://*/*a*a*b", "http://h/xaYab") and not fg.match_pattern("*://*/*a*a*b", "http://h/ab")
    assert fg.wildcard_match("a*b*", "ab") and fg.wildcard_match("*", "") and not fg.wildcard_match("a*a", "a")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC5: runtime.onStartup still fires on the first start after a Foxglove upgrade
#  LC8: a new version of an extension doesn't keep what the old one set at run time
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_onstartup_fires_after_a_foxglove_upgrade(server, tmp_path):
    from test_extensions import _run_phase
    sw = ("chrome.runtime.onStartup.addListener(() => chrome.storage.local.set({'ev:startup': Date.now()}));\n"
          "chrome.runtime.onInstalled.addListener((d) => chrome.storage.local.set({'ev:installed': d.reason}));\n")
    path = simple_ext(tmp_path / "st", "Starter", {"cs.js": "document.documentElement.setAttribute('data-fg-st', 'ran');"}, sw=sw,
                      content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}])
    args = {"root": str(tmp_path / "run"), "profile": "startup", "page_url": server.url("/page?startup"),
            "exts": [{"name": "Starter", "tag": "st", "path": str(path)}], "storage": {"Starter": "x"},
            "records": ["startup", "installed"], "enabled": ["Starter"], "upgrade": True}
    _run_phase("install", args)  # (installing fires onInstalled, not onStartup; the phase then forgets both records)
    after = _run_phase("report", args)  # a newer polyfill: re-wired, as after a Foxglove update
    assert "wait_error" not in after, after
    assert after["shimmed"] == {"Starter": True}
    assert after["records"]["Starter"]["installed"] == "chrome_update"
    assert after["records"]["Starter"]["startup"] is not None, "no runtime.onStartup on the first start after the upgrade"


V1_SW = """chrome.runtime.onMessage.addListener((m, s, reply) => {
  if (m !== 'setup') return;
  Promise.all([chrome.action.setPopup({popup: 'gone.html'}), chrome.action.setBadgeText({text: 'v1'}), chrome.action.disable(),
    chrome.declarativeNetRequest.updateSessionRules({addRules: [{id: 1, priority: 1, action: {type: 'block'}, condition: {urlFilter: 'blockme'}}]})])
    .then(() => reply('ok'), (e) => reply('ERR ' + e.message));
  return true;
});"""


def test_an_update_drops_the_old_versions_run_time_state(harness, server, tmp_path):
    import shutil
    src = tmp_path / "u"
    kw = dict(permissions=["storage", "declarativeNetRequest"], host_permissions=["http://127.0.0.1/*"], action={"default_popup": "popup.html"})
    entry = harness.install_ok(simple_ext(src, "Upd", {"gone.html": "<p>gone</p>", "popup.html": "<p>p</p>"}, sw=V1_SW, **kw), "Upd")
    page = ext_page(harness, entry)
    assert run_js_async(page, "return await chrome.runtime.sendMessage('setup')") == "ok"
    harness.drop_page(page)
    bridge = harness.controller.bridge
    assert bridge.action_value(entry.id, "enabled") is False and bridge._session_rules.get(entry.id)
    server.add("/blockme", b"ok", "text/plain")
    web = harness.fresh_page(server.page_url())
    fetch = "try { return 'status ' + (await fetch('/blockme')).status; } catch (e) { return 'blocked'; }"
    assert run_js_async(web, fetch) == "blocked"
    shutil.rmtree(src)  # version 2: no gone.html, no session rules
    simple_ext(src, "Upd", {"popup.html": "<p>p2</p>"}, version="2.0", **kw)
    assert harness.install(src) == ("success", "“Upd” was updated.")
    harness.wait_state(entry.id, True, 20)
    assert not bridge.action.get(entry.id) and not bridge._session_rules.get(entry.id)
    assert bridge.action_value(entry.id, "enabled") is None and bridge.action_value(entry.id, "badgeText") is None
    assert run_js_async(web, fetch) == "status 200"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC1: a registered content script Qt can't load is refused up front - and one that still fails to load is dropped
# ══════════════════════════════════════════════════════════════════════════════════════════
REG = {"reg.js": "document.documentElement.setAttribute('data-reg', 'yes');\n",
       "reg2.js": "document.documentElement.setAttribute('data-reg2', 'yes');\n", "latin1.js": b"// caf\xe9\n"}


def _registrar(harness, tmp_path, name="Registrar", **extra):
    return harness.install_ok(simple_ext(tmp_path / name, name, REG, permissions=["storage", "scripting"],
                                         host_permissions=["<all_urls>"], **extra), name)


def _settled(harness, entry, timeout: float = 30) -> None:
    c = harness.controller
    wait_until(lambda: entry.id not in c._reloads and entry.id not in c._updates, timeout, "the reload")
    harness.wait_state(entry.id, True, 20)


def _register(page, scripts) -> str:
    return run_js_async(page, f"try {{ await chrome.scripting.registerContentScripts({json.dumps(scripts)}); return 'ok'; }}"
                              " catch (e) { return 'ERR ' + e.message; }")


@pytest.mark.parametrize("bad, error", [
    ({"matches": ["ws://127.0.0.1/*"], "js": ["reg.js"]}, "invalid match pattern: 'ws://127.0.0.1/*'"),
    ({"matches": ["http://127.0.0.1/*"], "excludeMatches": ["wss://*/*"], "js": ["reg.js"]}, "invalid match pattern: 'wss://*/*'"),
    ({"matches": ["http://127.0.0.1/page*"], "js": ["reg.js"], "matchOriginAsFallback": True}, "must be '*' when 'matchOriginAsFallback'"),
    ({"matches": ["http://127.0.0.1/*"], "js": ["latin1.js"]}, "isn't UTF-8 encoded"),
], ids=["ws", "wss-exclude", "fallback-with-path", "not-utf8"])
def test_registrations_qt_cant_load_are_refused(harness, tmp_path, bad, error):
    entry = _registrar(harness, tmp_path)
    page = ext_page(harness, entry)
    result = _register(page, [{"id": "bad", **bad}])
    assert result.startswith("ERR ") and error in result, result
    assert harness.controller.registry[entry.id].get("scripts") == [] and entry.id not in harness.controller._reloads
    assert _register(page, [{"id": "ok", "matches": ["https://*/*"], "js": ["reg.js"], "matchOriginAsFallback": True}]) == "ok"


def test_a_registration_that_doesnt_load_is_dropped_again(harness, server, tmp_path, fg, monkeypatch):
    """Should one still get through, the failed reload takes it out of the registered scripts too - so it can't
    spoil every later registration (each would be written beside it, fail to load and be reverted)."""
    entry = _registrar(harness, tmp_path)
    page = ext_page(harness, entry)
    checked = fg.ExtensionBridge._script_entry
    monkeypatch.setattr(fg.ExtensionBridge, "_script_entry", lambda self, ext_id, raw, old=None: {**raw, "persistAcrossSessions": True})
    assert _register(page, [{"id": "bad", "matches": ["ws://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    monkeypatch.setattr(fg.ExtensionBridge, "_script_entry", checked)
    start = len(harness.messages)
    _settled(harness, entry)
    assert any(k == "error" and "couldn't be used" in t for k, t in harness.messages[start:]), harness.messages[start:]
    assert harness.controller.registry[entry.id]["scripts"] == []
    page = ext_page(harness, entry)
    assert run_js_async(page, "return (await chrome.scripting.getRegisteredContentScripts()).map(s => s.id)") == []
    assert _register(page, [{"id": "good", "matches": ["http://127.0.0.1/*"], "js": ["reg2.js"]}]) == "ok"
    _settled(harness, entry)
    assert wait_attr(harness.fresh_page(server.page_url()), "data-reg2") == "yes"


def test_a_worker_registering_a_bad_script_at_every_start_doesnt_loop(harness, tmp_path):
    sw = """(async () => {
      const {starts = 0} = await chrome.storage.local.get('starts');
      await chrome.storage.local.set({starts: starts + 1});
      await chrome.scripting.unregisterContentScripts();
      await chrome.scripting.registerContentScripts([{id: 'bad', matches: ['http://127.0.0.1/page*'], js: ['reg.js'], matchOriginAsFallback: true}]);
    })();"""
    c = harness.controller
    unloads = []
    unload = c.manager.unloadExtension
    c.manager.unloadExtension = lambda info: (unloads.append(info.id()), unload(info))[1]
    entry = harness.install_ok(simple_ext(tmp_path / "l", "Looper", REG, sw=sw, permissions=["storage", "scripting"],
                                          host_permissions=["<all_urls>"]), "Looper")
    spin(6)
    assert unloads == [] and entry.id not in c._reloads, f"{len(unloads)} reloads"
    page = ext_page(harness, entry)
    assert run_js_async(page, "return (await chrome.storage.local.get('starts')).starts") == 1


def test_a_failed_load_with_registered_scripts_loads_without_them(harness, tmp_path, fg):
    """A bad script written into the manifest (from before this check, or a quit before the reload) would keep the
    extension from loading at the next start: it loads without its registered scripts instead."""
    entry = _registrar(harness, tmp_path)
    c, path = harness.controller, entry.path
    c.registry[entry.id]["scripts"] = [{"id": "bad", "matches": ["ws://127.0.0.1/*"], "js": ["reg.js"]}]
    manifest = json.loads((Path(path) / "manifest.json").read_text())
    manifest["content_scripts"] = [{"matches": ["ws://127.0.0.1/*"], "js": [fg.SHIM_FILE, "reg.js"]}]
    c.manager.unloadExtension(c._info(entry.id))
    wait_until(lambda: c._info(entry.id) is None or not c._info(entry.id).isLoaded(), 10, "unload")
    (Path(path) / "manifest.json").write_text(json.dumps(manifest))
    c.manager.loadExtension(path)  # as at a start
    entry = harness.wait_state(entry.id, True, 20)
    assert "content_scripts" not in json.loads((Path(path) / "manifest.json").read_text())
    assert c.registry[entry.id].get("scripts") in (None, [])


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC2: a reload for registered content scripts keeps storage.session and leaves open extension pages alone
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_registration_reload_keeps_session_storage(harness, server, tmp_path):
    entry = _registrar(harness, tmp_path)
    page = ext_page(harness, entry)
    run_js_async(page, "await chrome.storage.session.set({token: 'abc', n: {deep: [1, 2]}}); return true")
    assert _register(page, [{"id": "x", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    reloads = []
    unload = harness.manager.unloadExtension
    harness.manager.unloadExtension = lambda info: (reloads.append(info.id()), unload(info))[1]
    _settled(harness, entry)
    assert reloads == [entry.id]
    page = ext_page(harness, entry)
    got = run_js_async(page, "return await chrome.storage.session.get(null)")
    assert got == {"token": "abc", "n": {"deep": [1, 2]}}
    assert wait_attr(harness.fresh_page(server.page_url()), "data-reg") == "yes"


def test_registration_reload_waits_for_open_extension_pages(window, harness, server, tmp_path):
    from test_extension_apis import load_tab
    entry = _registrar(harness, tmp_path)
    c = harness.controller
    tab = window.current_tab()
    load_tab(tab, entry.options_url.toString())
    assert wait_attr(tab.page, "data-options") == "ready"
    run_js(tab.page, "window.unsaved = 'typed text'")
    assert _register(tab.page, [{"id": "x", "matches": ["http://127.0.0.1/*"], "js": ["reg.js"]}]) == "ok"
    spin(3)
    assert run_js(tab.page, "String(window.unsaved)") == "typed text" and entry.id in c._reloads
    load_tab(tab, server.page_url())  # the page is gone: now it reloads
    _settled(harness, entry)
    assert wait_attr(harness.fresh_page(server.page_url()), "data-reg") == "yes"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC3: persistAcrossSessions: false scripts end with the session
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_session_only_scripts_end_at_a_restart(server, tmp_path):
    from test_extensions import _run_phase
    sw = """const rec = (k, v) => chrome.storage.local.set({['ev:' + k]: v});
const reg = async (why, scripts) => {
  try { await chrome.scripting.registerContentScripts(scripts); await rec(why, 'ok'); } catch (e) { await rec(why, 'ERR ' + e.message); }
};
const np = {id: 'np', matches: ['http://127.0.0.1/*'], js: ['reg.js'], persistAcrossSessions: false};
chrome.runtime.onInstalled.addListener(() => reg('installed', [np, {id: 'keep', matches: ['http://127.0.0.1/*'], js: ['reg2.js']}]));
chrome.runtime.onStartup.addListener(() => reg('startup', [np]));
"""
    path = simple_ext(tmp_path / "np", "Sessions", {**REG, "cs.js": "document.documentElement.setAttribute('data-fg-np', 'ran');"}, sw=sw,
                      permissions=["storage", "scripting"], host_permissions=["http://127.0.0.1/*"],
                      content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}])
    args = {"root": str(tmp_path / "run"), "profile": "sessions", "page_url": server.url("/page?sessions"),
            "exts": [{"name": "Sessions", "tag": "np", "path": str(path)}], "storage": {"Sessions": "x"},
            "records": ["startup"], "enabled": ["Sessions"], "settle": 4}
    before = _run_phase("install", args)
    ext_id = before["entries"]["Sessions"]["id"]
    assert [s["id"] for s in before["registry"][ext_id]["scripts"]] == ["np", "keep"]
    after = _run_phase("report", args)
    assert "wait_error" not in after, after
    assert after["records"]["Sessions"]["startup"] == "ok"  # not "Duplicate script ID 'np'"
    assert sorted(s["id"] for s in after["registry"][ext_id]["scripts"]) == ["keep", "np"]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  LC4: a pop-up that writes and closes at once still reaches the worker and content scripts
#  LC7: a burst of writes reaches a running worker once each
# ══════════════════════════════════════════════════════════════════════════════════════════
CLOSE_SW = """chrome.storage.onChanged.addListener((changes, area) => {
  if (changes.flag) chrome.storage.local.set({seen: changes.flag.newValue});
});"""
CLOSE_CS = """chrome.storage.onChanged.addListener((changes) => { if (changes.flag) document.documentElement.setAttribute('data-flag', String(changes.flag.newValue)); });
document.documentElement.setAttribute('data-cs', 'ran');"""
POP_HTML = """<!doctype html><html><head><meta charset="utf-8"></head><body style="width:100px;height:50px">x<script src="popup.js"></script></body></html>"""


def test_a_popup_that_writes_and_closes_at_once_is_heard(window, harness, server, tmp_path):
    """The pop-up's write is the extension's first use of its storage (slow): the pop-up is gone before it could tell."""
    from test_extension_apis import load_tab
    pop_js = "chrome.storage.local.set({flag: 'v' + Date.now()}); window.close();"
    entry = harness.install_ok(simple_ext(tmp_path / "p", "Closer", {"popup.html": POP_HTML, "popup.js": pop_js, "cs.js": CLOSE_CS},
                                          sw=CLOSE_SW, action={"default_popup": "popup.html"}, host_permissions=["http://127.0.0.1/*"],
                                          content_scripts=[{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"]}]), "Closer")
    c = harness.controller
    wait_until(lambda: "storage.onChanged" in (c.registry.get(entry.id) or {}).get("listeners", []), 10, "the worker's listener")
    tab = window.current_tab()
    load_tab(tab, server.page_url())
    assert wait_attr(tab.page, "data-cs") == "ran"
    spin(1)
    window.open_extension(entry.id)
    spin(4)
    page = ext_page(harness, entry)
    got = run_js_async(page, "return await chrome.storage.local.get(null)")
    assert got.get("flag", "").startswith("v"), got
    assert wait_until(lambda: run_js_async(page, "return (await chrome.storage.local.get(null)).seen ?? ''") == got["flag"], 10, "the worker")
    assert wait_until(lambda: run_js(tab.page, "document.documentElement.getAttribute('data-flag')") == got["flag"], 10, "the tab")


def test_early_storage_notices(harness, tmp_path, fg, monkeypatch):
    """What Foxglove does with a write announced before it's made: nothing if the writer announces it as usual,
    the change (without old values) to the worker, pages and content scripts if it never does - and only once."""
    entry = harness.install_ok(simple_ext(tmp_path / "e", "Early", sw="chrome.storage.onChanged.addListener(() => {});"), "Early")
    bridge, sent = harness.controller.bridge, []
    wait_until(lambda: bridge.listens(entry.id, "storage.onChanged"), 10, "the worker's listener")
    page = ext_page(harness, entry)
    run_js_async(page, "await chrome.storage.local.set({a: 2}); return 1")  # (what the notices below say is written)
    spin(0.5)
    monkeypatch.setattr(bridge, "emit", lambda ext_id, name, args, eid=None, worker_only=False: sent.append((name, args, eid, worker_only)))
    monkeypatch.setattr(bridge, "EARLY_STORAGE_MS", 200)
    ctx = {"from": "chrome-extension://x/popup.html", "cs": False, "tab": None}
    bridge.api_storage_pending(entry.id, {"area": "local", "src": "s", "eid": "s1", "set": {"a": 1}}, ctx)
    bridge.api_storage_changed(entry.id, {"area": "local", "src": "s", "eid": "s1", "changes": {"a": {"newValue": 1}}}, ctx)
    spin(0.5)
    assert sent == [("storage.changed", ["local", {"a": {"newValue": 1}}, "s"], "s1", True)]  # the usual way, once
    sent.clear()
    bridge.api_storage_pending(entry.id, {"area": "local", "src": "s", "eid": "s2", "set": {"a": 2}}, ctx)
    bridge.api_storage_pending(entry.id, {"area": "sync", "src": "s", "eid": "s3", "remove": ["b"]}, ctx)
    bridge.api_storage_pending(entry.id, {"area": "local", "src": "s", "eid": "s4", "set": {"c": 3}}, ctx)
    bridge.api_storage_changed(entry.id, {"area": "local", "src": "s", "eid": "s4", "changes": {}}, ctx)  # no change after all
    spin(1.5)
    assert sorted(sent, key=lambda e: e[2]) == [("storage.changed", ["local", {"a": {"newValue": 2}}, "s"], "s2", False),
                                                ("storage.changed", ["sync", {"b": {}}, "s"], "s3", False)]
    bridge.api_storage_changed(entry.id, {"area": "local", "src": "s", "eid": "s2", "changes": {"a": {"newValue": 2}}}, ctx)  # late
    spin(0.3)
    assert len(sent) == 2


BURST_SW = """const seen = [];
chrome.storage.onChanged.addListener((changes, area) => { for (const k of Object.keys(changes)) if (k.startsWith('b')) seen.push(k); });
chrome.runtime.onMessage.addListener((m, s, reply) => { if (m === 'seen') reply(seen); });
"""


def test_a_burst_of_writes_reaches_a_running_worker_once_each(harness, tmp_path):
    entry = harness.install_ok(simple_ext(tmp_path / "w", "Burst", sw=BURST_SW), "Burst")
    page = ext_page(harness, entry)
    c = harness.controller
    wait_until(lambda: "storage.onChanged" in (c.registry.get(entry.id) or {}).get("listeners", []), 10, "listener")
    run_js_async(page, "return await chrome.runtime.sendMessage('seen')")  # the worker runs
    run_js_async(page, "await Promise.all(Array.from({length: 100}, (_, i) => chrome.storage.local.set({['b' + i]: i}))); return true", timeout=60)
    spin(5)
    seen = run_js_async(page, "return await chrome.runtime.sendMessage('seen')")
    assert len(set(seen)) == 100 and len(seen) == 100, f"{len(seen) - len(set(seen))} duplicates"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-3: activeTab ends when the tab leaves the origin - it doesn't come back with it
#  SEC-4: "https:///*" (no host) grants nothing
# ══════════════════════════════════════════════════════════════════════════════════════════
BANK = b"<!doctype html><title>Bank</title><body>secret-xyz</body>"


def _read_tab(page, tab) -> str:
    return run_js_async(page, f"try {{ return (await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, "
                              "func: () => document.body.innerText}))[0].result; } catch (e) { return 'ERR ' + e.message; }")


def test_active_tab_ends_when_the_tab_leaves_the_origin(window, harness, server, tmp_path):
    from test_extension_apis import load_tab
    server.add("/bank", BANK, "text/html")
    entry = harness.install_ok(simple_ext(tmp_path / "e", "Act", permissions=["scripting", "activeTab"], action={"default_title": "Act"}), "Act")
    page = ext_page(harness, entry)
    tab = window.current_tab()
    load_tab(tab, server.url("/bank"))
    harness.controller.bridge.grant_active_tab(entry.id, tab)  # what clicking its toolbar button does
    assert _read_tab(page, tab) == "secret-xyz"
    load_tab(tab, server.url("/bank?same-origin"))  # still the same origin: still granted
    assert _read_tab(page, tab) == "secret-xyz"
    load_tab(tab, f"http://localhost:{server.port}/page?elsewhere")
    assert _read_tab(page, tab).startswith("ERR Cannot access")
    load_tab(tab, server.url("/bank?later"))  # back again, without the user invoking it: no access
    assert _read_tab(page, tab).startswith("ERR Cannot access")


def test_patterns_without_a_host_grant_nothing(window, harness, server, tmp_path, fg):
    from test_extension_apis import load_tab
    assert not fg.match_pattern("http:///*", "http://127.0.0.1/bank") and not fg.match_pattern("https:///*", "https://example.com/")
    assert fg.match_pattern("file:///*", "file:///tmp/x.html") and fg.match_pattern("*://*/*", "https://example.com/")
    server.add("/bank", BANK, "text/html")
    entry = harness.install_ok(simple_ext(tmp_path / "e", "Empty", permissions=["scripting"], host_permissions=["http:///*"],
                                          optional_host_permissions=["https:///*"]), "Empty")
    page = ext_page(harness, entry)
    tab = window.current_tab()
    load_tab(tab, server.url("/bank"))
    assert _read_tab(page, tab).startswith("ERR Cannot access")
    with pytest.raises(fg.ApiError, match=r"Invalid value for origin pattern https:///\*"):  # (past the user-gesture check)
        harness.controller.bridge.api_permissions_request(entry.id, {"origins": ["https:///*"]}, {"from": "", "cs": False, "tab": None})


# ══════════════════════════════════════════════════════════════════════════════════════════
#  R1: foxglove-ext:// refusals end like network errors (web pages can't tell Foxglove apart; nothing hangs)
#  R2: with no extensions, Foxglove filters no requests, puts nothing in pages and keeps no cookie copy
# ══════════════════════════════════════════════════════════════════════════════════════════
PROBE = """const c = new AbortController(); setTimeout(() => c.abort(), 4000);
  try { const r = await fetch(URL, {method: METHOD, body: BODY, signal: c.signal}); return 'resolved ' + r.status + ' ' + (await r.text()).length; }
  catch (e) { return e.name + ': ' + e.message; }"""


def _probe(page, url: str, method: str = "POST", body: str = "{}") -> str:
    script = PROBE.replace("URL", json.dumps(url)).replace("METHOD", json.dumps(method)).replace("BODY", "null" if method == "GET" else json.dumps(body))
    return run_js_async(page, script, timeout=15)


def test_bridge_refusals_are_network_errors(harness, server, tmp_path):
    web = harness.fresh_page(server.page_url())
    unknown = _probe(web, "foxglove-ext://bridge/call")  # no extensions: as in any browser
    assert unknown == "TypeError: Failed to fetch", unknown
    entry = harness.install_ok(simple_ext(tmp_path / "x", "Bridged", {"secret.js": "1"}), "Bridged")
    web = harness.fresh_page(server.page_url())
    for url, method in (("foxglove-ext://bridge/call", "POST"), (f"foxglove-ext://{entry.id}/secret.js", "GET"),
                        (f"foxglove-ext://{'a' * 32}/x.js", "GET")):
        assert _probe(web, url, method) == unknown, url  # the same as an unknown scheme - and nothing left hanging
    page = ext_page(harness, entry)
    assert _probe(page, "foxglove-ext://bridge/call", body="not json") == unknown


def test_no_extensions_no_request_filtering(window, harness, server, tmp_path, fg, monkeypatch):
    from test_extension_apis import load_tab
    calls = []
    apply = fg.NetRules.apply
    monkeypatch.setattr(fg.NetRules, "apply", lambda self, info, tab: (calls.append(info.requestUrl().toString()), apply(self, info, tab))[1])
    c, tab = harness.controller, window.current_tab()
    load_tab(tab, server.page_url())
    assert calls == [] and not c.filtering and not c.bridge._cookies_watched
    assert tab.page.scripts().find(c.TAB_SCRIPT) == []
    entry = harness.install_ok(simple_ext(tmp_path / "plain", "Plain"), "Plain")  # no rules, no cookies: still nothing
    load_tab(tab, server.page_url())
    assert calls == [] and not c.filtering and not c.bridge._cookies_watched
    assert len(tab.page.scripts().find(c.TAB_SCRIPT)) == 1  # (it may have content scripts that ask for their tab)
    server.add("/blocked.png", eb.tiny_png(), "image/png")
    rules = [{"id": 1, "priority": 1, "action": {"type": "block"}, "condition": {"urlFilter": "blocked.png"}}]
    dnr = harness.install_ok(simple_ext(tmp_path / "dnr", "Rules", {"rules.json": json.dumps(rules)}, permissions=["declarativeNetRequest"],
                                        declarative_net_request={"rule_resources": [{"id": "r", "enabled": True, "path": "rules.json"}]}), "Rules")
    assert wait_until(lambda: c.filtering, 5, "request filtering")
    load_tab(tab, server.page_url())
    assert calls
    status = run_js_async(tab.page, "try { await fetch('/blocked.png'); return 'loaded'; } catch (e) { return 'blocked'; }")
    assert status == "blocked"
    c.set_enabled(dnr.id, False)
    assert wait_until(lambda: not c.filtering, 5, "no more filtering")
    assert run_js_async(tab.page, "try { await fetch('/blocked.png'); return 'loaded'; } catch (e) { return 'blocked'; }") == "loaded"
    jar = harness.install_ok(simple_ext(tmp_path / "jar", "Jar", permissions=["cookies"]), "Jar")
    assert c.bridge._cookies_watched and entry.enabled and jar.enabled
