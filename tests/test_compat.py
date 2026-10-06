"""Foxglove presents itself consistently as the desktop Chrome it is built on (Google sign-in, bot checks).

Everything is checked offline against a local server: the user agent, the Sec-CH-UA client hints and
navigator.userAgentData must agree, Accept-Language must be sent and match navigator.languages, and pages (frames and
pop-ups too) must see Chrome's window.chrome APIs - without any trace of Qt WebEngine.
"""
from __future__ import annotations

import json
import re
import sys

import pytest

from helpers import load, poll_js, run_js, run_js_async, wait_until

from PyQt6 import sip
from PyQt6.QtWebEngineCore import QWebEngineProfile, qWebEngineChromiumVersion

HIGH_ENTROPY = ("Sec-CH-UA-Full-Version-List, Sec-CH-UA-Full-Version, Sec-CH-UA-Platform-Version, Sec-CH-UA-Arch, "
                "Sec-CH-UA-Bitness, Sec-CH-UA-Model, Sec-CH-UA-WoW64")
CHROMIUM = qWebEngineChromiumVersion()
MAJOR = CHROMIUM.split(".")[0]
PLATFORM = {"darwin": "macOS", "win32": "Windows"}.get(sys.platform, "Linux")
UA_OS = {"darwin": "Macintosh; Intel Mac OS X 10_15_7", "win32": "Windows NT 10.0; Win64; x64"}.get(sys.platform, "X11; Linux")


def grease_brand(major: int) -> tuple[str, str]:
    """Chrome's GREASE brand for a major version (components/embedder_support/user_agent_utils.cc)."""
    chars = [" ", "(", ":", "-", ".", "/", ")", ";", "=", "?", "_"]
    return f"Not{chars[major % 11]}A{chars[(major + 1) % 11]}Brand", ["8", "99", "24"][major % 3]


def brand_list(header: str) -> list[dict]:
    return [{"brand": b, "version": v} for b, v in re.findall(r'"((?:[^"\\]|\\.)*)";v="([^"]*)"', header)]


def lower(headers: dict) -> dict:
    return {k.lower(): v for k, v in headers.items()}


def serve(server, path: str, body: str, headers: dict | None = None) -> str:
    server.routes[path] = (200, "text/html; charset=utf-8", body.encode(), headers or {})
    return server.url(path)


def test_user_agent_and_client_hints_agree(fg, harness, server):
    url = serve(server, "/compat/hints", "<!doctype html><title>hints</title>", {"Accept-CH": HIGH_ENTROPY})
    page = harness.page()
    assert load(page, url) and load(page, url)  # the second request carries the hints asked for by Accept-CH
    sent = lower(server.headers["/compat/hints"])
    ua = sent["user-agent"]
    assert "QtWebEngine" not in json.dumps(sent) and "Qt" not in ua
    assert ua == harness.user_agent == harness.profile.httpUserAgent()
    assert re.fullmatch(rf"Mozilla/5\.0 \({re.escape(UA_OS)}[^)]*\) AppleWebKit/537\.36 \(KHTML, like Gecko\) "
                        rf"Chrome/{MAJOR}\.0\.0\.0 Safari/537\.36", ua), ua

    grease, grease_version = grease_brand(int(MAJOR))
    brands = brand_list(sent["sec-ch-ua"])
    assert sorted(b["brand"] for b in brands) == sorted(["Google Chrome", "Chromium", grease])
    assert {b["brand"]: b["version"] for b in brands} == {"Google Chrome": MAJOR, "Chromium": MAJOR, grease: grease_version}
    full = brand_list(sent["sec-ch-ua-full-version-list"])
    assert [b["brand"] for b in full] == [b["brand"] for b in brands]  # same order in both lists
    assert {b["brand"]: b["version"] for b in full} == {"Google Chrome": CHROMIUM, "Chromium": CHROMIUM,
                                                        grease: f"{grease_version}.0.0.0"}
    assert sent["sec-ch-ua-full-version"] == f'"{CHROMIUM}"'
    assert sent["sec-ch-ua-platform"] == f'"{PLATFORM}"' and sent["sec-ch-ua-mobile"] == "?0"
    assert sent["sec-ch-ua-model"] == '""' and sent["sec-ch-ua-bitness"] == '"64"'

    seen = run_js_async(page, """const d = navigator.userAgentData;
      return {ua: navigator.userAgent, appVersion: navigator.appVersion, vendor: navigator.vendor, webdriver: navigator.webdriver,
              brands: d.brands, mobile: d.mobile, platform: d.platform,
              high: await d.getHighEntropyValues(["fullVersionList", "uaFullVersion", "platformVersion", "architecture"])};""")
    assert seen["ua"] == ua and seen["appVersion"] == ua.removeprefix("Mozilla/") and seen["vendor"] == "Google Inc."
    assert seen["webdriver"] is False and seen["mobile"] is False and seen["platform"] == PLATFORM
    assert seen["brands"] == brands and seen["high"]["fullVersionList"] == full  # JS and headers tell the same story
    assert seen["high"]["uaFullVersion"] == CHROMIUM
    assert f'"{seen["high"]["platformVersion"]}"' == sent["sec-ch-ua-platform-version"]
    assert f'"{seen["high"]["architecture"]}"' == sent["sec-ch-ua-arch"]

    # applying it again (e.g. a second window) changes nothing
    assert fg.apply_browser_identity(harness.profile) == ua
    assert len(harness.profile.scripts().find(fg.CHROME_SCRIPT)) == 1
    assert sorted(harness.profile.clientHints().fullVersionList()) == sorted(["Google Chrome", "Chromium", grease])


def test_accept_language_matches_navigator_languages(fg, harness, server):
    assert re.fullmatch(r"[a-z]{2,3}(-[A-Z]{2}|-\d{3})?(,[a-z]{2,3};q=0\.9)?", fg.chrome_languages())
    url = serve(server, "/compat/lang", "<!doctype html><title>lang</title>")
    page = harness.page()
    assert load(page, url)
    header = lower(server.headers["/compat/lang"]).get("accept-language")
    assert header, "every real browser sends Accept-Language"
    langs = run_js(page, "JSON.stringify([navigator.language, navigator.languages])")
    first, languages = json.loads(langs)
    assert [part.split(";")[0] for part in header.split(",")] == languages and first == languages[0]


CHROME_PROBE = """
const shape = (w) => {
  const c = w.chrome, str = (f) => Function.prototype.toString.call(f);
  return {keys: Object.keys(c), loadTimes: Object.keys(c.loadTimes()), csi: Object.keys(c.csi()), app: Object.keys(c.app),
          loadTimesStr: str(c.loadTimes), csiStr: str(c.csi), appStr: str(c.app.getDetails), name: c.loadTimes.name,
          ownStr: w.Function.prototype.toString.call(c.loadTimes),
          details: c.app.getDetails(), installed: c.app.getIsInstalled(), running: c.app.runningState(), isInstalled: c.app.isInstalled};
};
const frame = document.querySelector("iframe").contentWindow;
const blank = document.body.appendChild(document.createElement("iframe")).contentWindow;
let threw = "";
try { chrome.app.getDetails(1); } catch (e) { threw = e.constructor.name + ": " + e.message; }
let notFunction = "";
try { Function.prototype.toString.call({}); } catch (e) { notFunction = e.constructor.name; }
const state = await new Promise((resolve) => chrome.app.installState(resolve));
const lt = chrome.loadTimes(), csi = chrome.csi();
return {top: shape(window), frame: shape(frame), blank: typeof blank.chrome === "object" && typeof blank.chrome.loadTimes,
        threw, notFunction, state, toString: Function.prototype.toString.toString(),
        toStringOfToString: Function.prototype.toString.call(Function.prototype.toString),
        ownSource: (function sample(a) { return a + 1; }).toString(), nativeStr: String(Array.prototype.push),
        rtt: navigator.connection.rtt, rttGetter: String(Object.getOwnPropertyDescriptor(NetworkInformation.prototype, "rtt").get),
        lt: {proto: lt.connectionInfo, type: lt.navigationType, request: lt.requestTime, finish: lt.finishDocumentLoadTime},
        csi: {startE: csi.startE, onloadT: csi.onloadT, tran: csi.tran}, now: Date.now(),
        hop: performance.getEntriesByType("navigation")[0].nextHopProtocol,
        cross: [frame.Function.prototype.toString.call(Function.prototype.toString), blank.Function.prototype.toString.call(chrome.loadTimes),
                blank.Function.prototype.toString.call(Object.getOwnPropertyDescriptor(NetworkInformation.prototype, "rtt").get)]};
"""


def test_window_chrome_looks_like_chrome(harness, server):
    serve(server, "/compat/frame", "<!doctype html><title>frame</title>")
    url = serve(server, "/compat/chrome", "<!doctype html><title>chrome</title><iframe src='/compat/frame'></iframe>")
    page = harness.page()
    assert load(page, url)
    got = run_js_async(page, CHROME_PROBE)
    for where in ("top", "frame"):
        shape = got[where]
        assert shape["keys"][:3] == ["loadTimes", "csi", "app"], (where, shape["keys"])
        assert shape["loadTimes"] == ["requestTime", "startLoadTime", "commitLoadTime", "finishDocumentLoadTime",
                                      "finishLoadTime", "firstPaintTime", "firstPaintAfterLoadTime", "navigationType",
                                      "wasFetchedViaSpdy", "wasNpnNegotiated", "npnNegotiatedProtocol",
                                      "wasAlternateProtocolAvailable", "connectionInfo"]
        assert shape["csi"] == ["startE", "onloadT", "pageT", "tran"]
        assert shape["app"] == ["isInstalled", "getDetails", "getIsInstalled", "installState", "runningState",
                                "InstallState", "RunningState"]
        assert shape["loadTimesStr"] == "function() {  native function GetLoadTimes();  return GetLoadTimes();}"
        assert shape["csiStr"] == "function() {  native function GetCSI();  return GetCSI();}"
        assert shape["appStr"] == "function getDetails() { [native code] }" and shape["name"] == ""
        assert shape["ownStr"] == shape["loadTimesStr"]
        assert (shape["details"], shape["installed"], shape["running"], shape["isInstalled"]) == (None, False, "cannot_run", False)
    assert got["blank"] == "function", got["blank"]
    assert got["threw"] == "TypeError: Error in invocation of app.getDetails()" and got["notFunction"] == "TypeError"
    assert got["state"] == "not_installed"
    # Function.prototype.toString still behaves natively for everything else
    assert got["toString"] == got["toStringOfToString"] == "function toString() { [native code] }"
    assert got["ownSource"] == "function sample(a) { return a + 1; }"
    assert got["cross"] == ["function toString() { [native code] }", got["top"]["loadTimesStr"],
                            "function get rtt() { [native code] }"]  # the same answer from another frame's realm
    assert got["nativeStr"] == "function push() { [native code] }"
    assert got["rtt"] > 0 and got["rttGetter"] == "function get rtt() { [native code] }"
    assert got["lt"]["proto"] == got["hop"] and got["lt"]["type"] == "Other"
    assert abs(got["lt"]["request"] * 1000 - got["now"]) < 60_000 and got["lt"]["finish"] >= got["lt"]["request"]
    assert got["csi"]["tran"] == 15 and got["csi"]["onloadT"] >= got["csi"]["startE"] > 0


def test_internal_pages_untouched(harness):
    page = harness.page()
    assert load(page, "foxglove://newtab")
    assert run_js(page, "typeof window.chrome === 'object' && 'loadTimes' in window.chrome") is False
    assert run_js(page, "Function.prototype.toString.call(Function.prototype.toString)") == "function toString() { [native code] }"


def test_sign_in_popup_has_the_same_identity(fg, harness, server, window):
    """"Sign in with Google" buttons open a pop-up with window.open(): it must look exactly like the tab."""
    serve(server, "/compat/signin", "<!doctype html><title>Sign in</title><p>pop-up</p>")
    opener = serve(server, "/compat/opener", "<!doctype html><title>site</title><script>"
                   "window.open('/compat/signin', 'signin', 'popup,width=420,height=560');</script>")
    tab = window.current_tab()
    assert load(tab.page, opener)
    wait_until(lambda: "/compat/signin" in server.headers, 15, "the pop-up's request")
    popup = wait_until(lambda: window.findChildren(fg.PopupWindow), 10, "the pop-up window")[0]
    sent, tab_sent = lower(server.headers["/compat/signin"]), lower(server.headers["/compat/opener"])
    for name in ("user-agent", "sec-ch-ua", "sec-ch-ua-platform", "accept-language"):
        assert sent[name] == tab_sent[name], name
    assert '"Google Chrome"' in sent["sec-ch-ua"] and sent["user-agent"] == harness.user_agent
    poll_js(popup.page, "document.title", lambda title: title == "Sign in", what="pop-up loaded")
    seen = json.loads(run_js(popup.page, "JSON.stringify([typeof chrome.loadTimes, navigator.userAgentData.brands.map(b => b.brand), "
                                         "navigator.userAgent, !!window.opener])"))
    assert seen[0] == "function" and "Google Chrome" in seen[1] and seen[2] == harness.user_agent and seen[3] is True
    popup.close()


@pytest.mark.parametrize("os_token, platform", [("Macintosh; Intel Mac OS X 10_15_7", "macOS"),
                                                 ("Windows NT 10.0; Win64; x64", "Windows"), ("X11; Linux x86_64", "Linux")])
def test_platform_hint_follows_the_user_agent(fg, qapp, os_token, platform):
    """What Qt WebEngine gives on each OS: Sec-CH-UA-Platform must name the OS the user agent names."""
    profile = QWebEngineProfile()
    try:
        profile.setHttpUserAgent(f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 (KHTML, like Gecko) QtWebEngine/6.11.0 "
                                 f"Chrome/{MAJOR}.0.0.0 Safari/537.36")
        ua = fg.apply_browser_identity(profile)
        assert ua == f"Mozilla/5.0 ({os_token}) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/{MAJOR}.0.0.0 Safari/537.36"
        assert profile.clientHints().platform() == platform and "Google Chrome" in profile.clientHints().fullVersionList()
        assert profile.httpAcceptLanguage() == fg.chrome_languages()
    finally:
        sip.delete(profile)


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS only")
def test_macos_identity(harness):
    hints = harness.profile.clientHints()
    assert hints.platform() == "macOS" and "Macintosh" in harness.user_agent and hints.arch() in ("arm", "x86")
