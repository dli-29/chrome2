"""One isolated world per frame: autofill, Claude's password watch and the extension tab-id script share PAGE_WORLD
(every extra world is a V8 context in every frame of every page), the shared script is only injected into http(s) and
file documents (about:blank, srcdoc and internal frames get no context at all) - and pages, extensions and Claude see
exactly what they did before: a shown password stays [redacted], the world stays out of the page's reach, content
scripts in frames still learn their tab."""
from __future__ import annotations

import json

from PyQt6.QtCore import QUrl
from PyQt6.QtWebEngineCore import QWebEngineScript

import extbuilder as eb
from helpers import load, poll_js, run_js, wait_attr, wait_until

PROBE = "typeof __fgAutofill + ',' + !!window.__claudeEverPassword"  # what autofill and the watch leave in a world
MIXED = b"""<!doctype html><title>Mixed frames</title><body><p>top</p>
<iframe id=web src="/page?inner=1"></iframe><iframe id=blank></iframe><iframe id=doc srcdoc="<p>inline</p>"></iframe>
</body></html>"""
SHOWN = b"""<!doctype html><title>Shown password</title><body>
<label>Password <input id=pw type=password></label><button id=show type=button>Show</button>
<script>document.getElementById('show').addEventListener('click', () => { document.getElementById('pw').type = 'text'; });</script>
</body></html>"""
FRAMED = b"""<!doctype html><title>Framed</title><body><p>outer</p><iframe id=inner src="/page?inner=2"></iframe></body></html>"""


def world_js(target, script: str, world: int):
    """*script* in *world* of a page or a frame."""
    box: dict = {}
    target.runJavaScript(script, world, lambda r: box.setdefault("r", r))
    wait_until(lambda: "r" in box, 10, f"JS in world {world}: {script[:60]!r}")
    return box["r"]


def frames_by_url(page) -> dict:
    return {child.url().toString(): child for child in page.mainFrame().children()}


def test_one_world_for_all_of_the_browsers_page_scripts(window, harness, server, tmp_path, fg):
    assert fg.AGENT_WORLD == fg.AUTOFILL_WORLD == fg.PAGE_WORLD and 0 < fg.PAGE_WORLD < fg.FIRST_EXTENSION_WORLD
    profile = window.profile.scripts().toList()
    assert {s.worldId() for s in profile} <= {0, fg.PAGE_WORLD}
    assert not window.profile.scripts().find("chrome2-agent-watch")  # the watch no longer has a script (world) of its own
    (script,) = window.profile.scripts().find(fg.AUTOFILL_SCRIPT)
    assert script.worldId() == fg.PAGE_WORLD and script.runsOnSubFrames()
    assert script.injectionPoint() == QWebEngineScript.InjectionPoint.DocumentCreation
    source = script.sourceCode()
    assert source.startswith("// ==UserScript==\n") and source.index("// ==/UserScript==") < source.index("(() => {")
    header = source.split("// ==/UserScript==")[0]
    assert all(f"// @match {m}" in header for m in ("http://*/*", "https://*/*", "file:///*"))
    assert "// @run-at document-start" in header
    assert "__claudeEverPassword" in source and "__fgAutofill" in source  # the watch rides along with autofill
    # with an extension installed, the tab-id script joins them instead of opening a world of its own
    harness.install_ok(eb.probe_extension(tmp_path / "probe", "Probe", "probe"), "Probe")
    controller, tab = harness.controller, window.current_tab()
    tab_script = wait_until(lambda: tab.page.scripts().find(controller.TAB_SCRIPT), 10, "the tab-id script")
    assert len(tab_script) == 1 and tab_script[0].worldId() == fg.PAGE_WORLD
    worlds = {s.worldId() for s in [*window.profile.scripts().toList(), *tab.page.scripts().toList()]} - {0}
    assert worlds == {fg.PAGE_WORLD}


def test_web_frames_share_the_world_and_other_frames_get_none(window, server, fg):
    server.add("/mixed", MIXED, "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/mixed"))
    frames = wait_until(lambda: len(f := frames_by_url(tab.page)) == 3 and f, 10, "three frames")
    web = next(frame for url, frame in frames.items() if url.startswith("http://"))
    assert world_js(tab.page, PROBE, fg.PAGE_WORLD) == "object,true"  # autofill and the watch, in one world
    assert world_js(web, PROBE, fg.PAGE_WORLD) == "object,true"
    assert world_js(frames["about:blank"], PROBE, fg.PAGE_WORLD) == "undefined,false"  # nothing was injected there
    assert world_js(frames["about:srcdoc"], PROBE, fg.PAGE_WORLD) == "undefined,false"
    # Claude's page code lands in the same world and picks up the watch's set
    assert world_js(tab.page, f"{fg.AGENT_JS}\ntypeof __fgAutofill + ',' + typeof window.__claudeAgent", fg.PAGE_WORLD) == "object,object"
    # the page sees none of it
    assert run_js(tab.page, "typeof __fgAutofill + typeof __claudeEverPassword + typeof __claudeAgent") == "undefinedundefinedundefined"
    assert run_js(tab.page, "Object.keys(window).filter(k => /autofill|claude|__fg/i.test(k)).length") == 0


def test_file_pages_keep_the_watch_and_internal_pages_get_nothing(window, tmp_path, fg):
    local = tmp_path / "local.html"
    local.write_text("<!doctype html><title>Local</title><input id=pw type=password>", encoding="utf-8")
    tab = window.current_tab()
    assert load(tab.page, QUrl.fromLocalFile(str(local)))
    assert world_js(tab.page, PROBE, fg.PAGE_WORLD) == "undefined,true"  # no autofill on file: pages, the watch yes
    assert load(tab.page, fg.NEWTAB)
    assert world_js(tab.page, PROBE, fg.PAGE_WORLD) == "undefined,false"


def test_shown_password_stays_redacted_from_claude(window, server, fg):
    server.add("/shown", SHOWN, "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/shown"))
    # typed, then shown (type=text) - all before Claude ever looks at the page: only the early watch can know
    run_js(tab.page, "const pw = document.getElementById('pw'); pw.focus(); pw.value = 'sw0rdfish-77'; "
                     "pw.dispatchEvent(new Event('input', {bubbles: true})); document.getElementById('show').click(); pw.type")
    assert run_js(tab.page, "document.getElementById('pw').type") == "text"
    browser, box = fg.AgentBrowser(window), {}
    browser.run("read_page", {}, lambda content, error=False, log_line="": box.update(c=content, e=error))
    wait_until(lambda: box, 30, "the read_page tool")
    assert not box["e"] and "sw0rdfish-77" not in box["c"] and 'textbox "Password" value=[redacted]' in box["c"]
    assert run_js(tab.page, "typeof window.__claudeAgent + typeof window.__claudeEverPassword") == "undefinedundefined"


def test_content_scripts_in_frames_learn_their_tab(window, harness, server, tmp_path):
    """The tab-id script answers in every frame from PAGE_WORLD (DOM events reach every world); the worker's push back
    through chrome.tabs.sendMessage (relay, PAGE_WORLD too) still arrives."""
    frames = [{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"], "run_at": "document_end", "all_frames": True}]
    harness.install_ok(eb.probe_extension(tmp_path / "af", "Framer", "af", content_scripts=frames), "Framer")
    server.add("/framed", FRAMED, "text/html; charset=utf-8")
    tab = window.current_tab()
    tab.load(QUrl(server.url("/framed")))
    assert json.loads(wait_attr(tab.page, "data-fg-af-push-status", 15)) == {"ok": True, "tabId": tab.tab_id}
    inner = "document.getElementById('inner').contentDocument.documentElement.getAttribute('data-fg-af-push-status')"
    assert json.loads(poll_js(tab.page, inner, timeout=15, what="the frame's sender.tab")) == {"ok": True, "tabId": tab.tab_id}
    assert wait_attr(tab.page, "data-fg-af-push") == "pushed-af"


def test_extensions_cannot_reach_the_shared_world(window, harness, server, tmp_path):
    manifest = {"manifest_version": 3, "name": "Peeker", "version": "1.0",
                "content_scripts": [{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"], "run_at": "document_end"}]}
    peek = ("document.documentElement.setAttribute('data-peek', typeof __fgAutofill + ',' + typeof __claudeEverPassword "
            "+ ',' + typeof __claudeAgent);")
    harness.install_ok(eb.write_tree(tmp_path / "peek", manifest, {"cs.js": peek}), "Peeker")
    tab = window.current_tab()
    tab.load(QUrl(server.page_url()))
    assert wait_attr(tab.page, "data-peek") == "undefined,undefined,undefined"
