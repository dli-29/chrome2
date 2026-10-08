"""Chrome 2's look: the name, the logo, Chrome's New Tab page (and its channel to the browser, which nothing else may
use), the privacy screen and the macOS app made by --install-app."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QPoint, QRect, QStandardPaths, Qt, QUrl
from PyQt6.QtGui import QColor, QImage
from PyQt6.QtWidgets import QAbstractButton, QLabel, QWidget

import extbuilder as eb
from helpers import load, poll_js, run_js, run_js_async, spin, stays_absent, wait_until
from test_extension_review import ext_page, simple_ext

NTP = "foxglove://newtab"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The name: "Chrome 2" wherever people see it - data stays where it was
# ══════════════════════════════════════════════════════════════════════════════════════════
def _texts(widget: QWidget) -> list[str]:
    found = [widget.windowTitle()]
    for child in widget.findChildren(QWidget):
        if isinstance(child, (QLabel, QAbstractButton)):
            found.append(child.text() + " " + child.toolTip())
        found += [action.text() + " " + action.toolTip() for action in child.actions()]
    found += [action.text() for action in widget.actions()]
    return found


def test_the_browser_is_called_chrome_2(fg, window):
    assert fg.APP_NAME == "Chrome 2" and fg.DATA_NAME == "Foxglove"
    assert fg.__doc__.lstrip().startswith("Chrome 2 - ")
    assert window.windowTitle() == "Chrome 2"  # the New Tab page
    assert window.act_about.text() == "About Chrome 2"
    texts = _texts(window)
    for factory in (fg.SettingsDialog, fg.ExtensionsDialog):
        dialog = factory(window)
        texts += _texts(dialog)
        dialog.deleteLater()
    assert not [t for t in texts if "Foxglove" in t]
    joined = "\n".join(texts)
    assert "Open previous tabs when Chrome 2 starts" in joined and "Hide pages when Chrome 2 isn't in focus" in joined
    assert "Add to Chrome 2" in joined  # the Extensions dialog's how-to
    assert "Chrome 2" in fg.EXTENSION_SHIM_JS and "Foxglove couldn't" not in fg.EXTENSION_SHIM_JS


def test_the_data_folder_and_extension_ids_dont_move(fg, harness, tmp_path):
    root = fg.set_application_names()
    assert QCoreApplication.applicationName() == "Foxglove"
    assert root == Path(os.environ["XDG_DATA_HOME"]) / "Foxglove"  # where the browser kept it as "Foxglove"
    assert root == Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation))
    assert "Foxglove" in Path(harness.profile.persistentStoragePath()).parts  # Qt WebEngine's cookies, extensions...
    # a profile written by the browser before the rename (hard-coded old place) is the one it opens now
    old = Path(os.environ["XDG_DATA_HOME"]) / "Foxglove" / "Profiles" / "renamed-profile"
    old.mkdir(parents=True, exist_ok=True)
    (old / "settings.json").write_text(json.dumps({"search_engine": "Bing", "website_appearance": "light"}))
    (old / "extensions.json").write_text(json.dumps({"abcdefghijklmnopabcdefghijklmnop": {"enabled": True}}))
    profile_dir = fg.set_application_names() / "Profiles" / "renamed-profile"
    settings = fg.Settings(profile_dir / "settings.json")
    assert settings.get("search_engine") == "Bing" and settings.get("privacy_screen") is True  # (new keys: defaults)
    assert fg.read_json(profile_dir / "extensions.json", {}) == {"abcdefghijklmnopabcdefghijklmnop": {"enabled": True}}
    # an unpacked extension keeps the ID it was given as "Foxglove:<folder>" (and with it its data)
    src = simple_ext(tmp_path / "unpacked", "Same ID")
    entry = harness.install_ok(src, "Same ID")
    key = hashlib.sha256(f"Foxglove:{src.resolve()}".encode()).digest()
    assert entry.id == fg.extension_id_from_key(key)


def test_the_logo_is_chromes(fg):
    image = fg.logo_image(480)
    def color(x, y):
        return image.pixelColor(x, y).name()
    def near(name, expected, tolerance=40):
        a, b = QColor(name), QColor(expected)
        return all(abs(getattr(a, c)() - getattr(b, c)()) <= tolerance for c in ("red", "green", "blue"))
    assert near(color(240, 240), "#1a73e8")                               # blue centre
    assert near(color(240, 240 - 107), "#ffffff")                         # white ring (radius 9.5..12 of 24)
    assert near(color(240, 20), "#ea4335")                                # red at the top
    assert near(color(240 + 180, 240 + 60), "#fbbc04")                    # yellow, lower right
    assert near(color(240 - 180, 240 + 60), "#34a853")                    # green, lower left
    assert image.pixelColor(2, 2).alpha() == 0                            # round: clear corners
    # the pinwheel: red/yellow meet along the tangent at the top of the white ring (y = 120), running right to the rim;
    # left of the ring's top, red goes on down
    assert near(color(300, 125), "#fbbc04") and near(color(300, 115), "#ea4335") and near(color(180, 125), "#ea4335")
    assert fg.icons().app_icon().availableSizes()  # the Dock / taskbar icon


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Chrome's New Tab page
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def ntp(fg, window, harness):
    """The real New Tab page on the window's profile (the harness serves a stub)."""
    harness.profile.removeUrlSchemeHandler(harness.pages_handler)
    service = fg.NewTabPage(window.settings, window.history, window.favicons, harness.profile)
    harness.profile.installUrlSchemeHandler(b"foxglove", fg.InternalPages(service, harness.profile))
    window.resize(1280, 900)
    return service


def open_ntp(window):
    tab = window.current_tab()
    assert load(tab.page, NTP)
    poll_js(tab.page, "!!document.getElementById('tiles') && document.readyState === 'complete'")
    return tab.page


def tiles(page) -> list[list[str]]:
    return run_js(page, "[...document.querySelectorAll('#tiles a.tile')].map((a) => "
                        "[a.querySelector('.tile-title span').textContent, a.getAttribute('href')])")


def style(page, selector: str, prop: str) -> str:
    return run_js(page, f"getComputedStyle(document.querySelector({json.dumps(selector)}))[{json.dumps(prop)}]")


def fill_dialog(page, name: str, url: str) -> None:
    run_js(page, f"""(() => {{
  document.getElementById('name').value = {json.dumps(name)};
  const u = document.getElementById('url'); u.value = {json.dumps(url)}; u.dispatchEvent(new Event('input'));
  document.getElementById('edit-form').requestSubmit(); return 1; }})()""")


def test_new_tab_page_looks_like_chromes(fg, window, ntp, server):
    for i, path in enumerate(("/page?a", "/page?b", "/page?c")):
        for _ in range(3 - i):
            window.history.add_visit(server.url(path), f"Site {path[-1]}")
    page = open_ntp(window)
    assert run_js(page, "document.title") == "New Tab" and window.windowTitle() == "Chrome 2"
    assert window.url_bar.text() == ""
    assert run_js(page, "!!document.querySelector('#logo svg.glogo[aria-label=Google]')")
    assert run_js(page, "document.getElementById('q').placeholder") == "Search Google or type a URL"
    assert run_js(page, "[...document.querySelectorAll('#ogb .link')].map((a) => a.textContent)") == ["Gmail", "Images"]
    assert run_js(page, "document.getElementById('apps').title + '|' + document.getElementById('avatar').title") == \
        "Google apps|Google Account"
    assert run_js(page, "document.querySelector('#customize span').textContent") == "Customize Chrome"
    assert style(page, "#q", "width") == "561px" and style(page, "#q", "height") == "48px"
    assert style(page, "#q", "borderTopLeftRadius") == "24px"
    assert style(page, ".tile", "width") == "112px" and style(page, ".tile-icon", "borderTopLeftRadius") == "50%"
    assert tiles(page) == [["Site a", server.url("/page?a")], ["Site b", server.url("/page?b")],
                           ["Site c", server.url("/page?c")]]  # My shortcuts: the most visited until edited
    assert run_js(page, "document.querySelector('#tiles .tile.add .tile-title span').textContent") == "Add shortcut"
    # dark (the default appearance): Chrome's dark colours and a white logo
    assert style(page, "body", "backgroundColor") == "rgb(32, 33, 36)"
    assert style(page, ".glogo path", "fill") == "rgb(255, 255, 255)"
    # the Google apps menu
    run_js(page, "document.getElementById('apps').click(); 1")
    assert poll_js(page, "!document.getElementById('apps-menu').hidden")
    names = run_js(page, "[...document.querySelectorAll('#apps-menu .app span')].map((s) => s.textContent)")
    assert len(names) == 18 and {"Gmail", "Drive", "YouTube", "Maps", "Calendar"} <= set(names)
    # light: white, with the coloured logo
    window.settings.set("website_appearance", "light")
    page = open_ntp(window)
    assert style(page, "body", "backgroundColor") == "rgb(255, 255, 255)"
    assert style(page, ".glogo path", "fill") == "rgb(234, 67, 53)"  # the red "o"
    # another search engine: its name instead of Google's logo, no Google bar
    window.settings.set("search_engine", "DuckDuckGo")
    page = open_ntp(window)
    assert run_js(page, "document.querySelector('#logo .wordmark').textContent") == "DuckDuckGo"
    assert run_js(page, "document.getElementById('ogb').hidden") is True
    assert run_js(page, "document.getElementById('q').placeholder") == "Search DuckDuckGo or type a URL"


def test_shortcut_favicons_come_from_the_browsers_cache(fg, window, ntp, server):
    window.history.add_visit(server.url("/page?f"), "Icon site")
    window.favicons.store_data_uri(server.url("/"), "data:image/png;base64," +
                                   __import__("base64").b64encode(eb.tiny_png(32, (200, 30, 30))).decode())
    page = open_ntp(window)
    src = run_js(page, "document.querySelector('#tiles a.tile img').getAttribute('src')")
    assert src.startswith("/favicon?host=127.0.0.1&t=")
    assert poll_js(page, "document.querySelector('#tiles a.tile img').naturalWidth") == 32
    # the same file without the page's token isn't served (nor to anyone else: see below)
    size = "return (await (await fetch(%s)).arrayBuffer()).byteLength"
    assert run_js_async(page, size % json.dumps(src)) > 0
    assert run_js_async(page, size % json.dumps("/favicon?host=127.0.0.1&t=nope")) == 0


def test_my_shortcuts_add_edit_remove_undo(fg, window, ntp):
    page = open_ntp(window)
    assert tiles(page) == []
    settings = window.settings
    run_js(page, "document.querySelector('#tiles .tile.add').click(); 1")
    assert poll_js(page, "document.getElementById('edit').open && document.getElementById('edit-title').textContent") == "Add shortcut"
    fill_dialog(page, "Example", "example.com")
    wait_until(lambda: settings.get("ntp_shortcuts") == [{"title": "Example", "url": "https://example.com"}])
    assert poll_js(page, "!document.getElementById('edit').open")
    assert poll_js(page, "document.getElementById('toast-text').textContent") == "Shortcut added"
    assert tiles(page) == [["Example", "https://example.com"]]
    # the same address again, and things that aren't web addresses, are refused in the dialog
    for url, error in (("https://example.com", "Shortcut already exists"), ("javascript:alert(1)", "Type a valid URL"),
                       ("chrome-extension://abc/x.html", "Type a valid URL")):
        run_js(page, "document.querySelector('#tiles .tile.add').click(); 1")
        fill_dialog(page, "Bad", url)
        assert poll_js(page, "document.getElementById('url-error').textContent") == error
        run_js(page, "document.getElementById('cancel').click(); 1")
    assert len(settings.get("ntp_shortcuts")) == 1
    # a name is text, never markup
    run_js(page, "document.querySelector('#tiles .tile.add').click(); 1")
    fill_dialog(page, "<img src=x onerror=\"document.title='pwned'\">", "http://127.0.0.1:9/second")
    wait_until(lambda: len(settings.get("ntp_shortcuts")) == 2)
    poll_js(page, "document.querySelectorAll('#tiles a.tile').length === 2")
    assert run_js(page, "document.querySelectorAll('#tiles .tile-title img').length") == 0
    assert tiles(page)[1][0].startswith("<img src=x")
    # Edit, from the tile's menu
    run_js(page, "document.querySelector('#tiles a.tile .tile-action').click(); 1")
    assert poll_js(page, "!document.getElementById('tile-menu').hidden")
    run_js(page, "document.getElementById('menu-edit').click(); 1")
    assert poll_js(page, "document.getElementById('edit-title').textContent") == "Edit shortcut"
    assert run_js(page, "document.getElementById('name').value + '|' + document.getElementById('url').value") == \
        "Example|https://example.com"
    fill_dialog(page, "Example Org", "https://example.org/")
    wait_until(lambda: settings.get("ntp_shortcuts")[0] == {"title": "Example Org", "url": "https://example.org/"})
    # moving (what dragging a tile does), through the page's own channel
    token = run_js(page, "JSON.parse(document.getElementById('state').textContent).token")
    run_js_async(page, f"""const r = await fetch('/api', {{method: 'POST', headers: {{'X-NTP-Token': {json.dumps(token)}}},
      body: JSON.stringify({{action: 'move', index: 1, to: 0}})}}); return (await r.json()).tiles.length""")
    assert [s["url"] for s in settings.get("ntp_shortcuts")] == ["http://127.0.0.1:9/second", "https://example.org/"]
    page = open_ntp(window)  # they stay
    assert [t[1] for t in tiles(page)] == ["http://127.0.0.1:9/second", "https://example.org/"]
    # Remove, then Undo
    run_js(page, "document.querySelectorAll('#tiles a.tile .tile-action')[1].click(); 1")
    run_js(page, "document.getElementById('menu-remove').click(); 1")
    wait_until(lambda: len(settings.get("ntp_shortcuts")) == 1)
    assert poll_js(page, "document.getElementById('toast-text').textContent") == "Shortcut removed"
    run_js(page, "document.getElementById('toast-undo').click(); 1")
    wait_until(lambda: len(settings.get("ntp_shortcuts")) == 2)
    assert poll_js(page, "document.querySelectorAll('#tiles a.tile').length") == 2
    assert run_js(page, "document.title") == "New Tab"


def test_most_visited_sites_mode(fg, window, ntp, server):
    urls = [server.url(f"/page?mv{i}") for i in range(10)]
    for i, url in enumerate(urls):
        for _ in range(12 - i):
            window.history.add_visit(url, f"Visited {i}")
    settings = window.settings
    page = open_ntp(window)
    run_js(page, "document.getElementById('customize').click(); 1")
    assert poll_js(page, "document.getElementById('panel').classList.contains('open')")
    run_js(page, "document.querySelector('input[name=mode][value=most_visited]').click(); 1")
    wait_until(lambda: settings.get("ntp_shortcut_mode") == "most_visited")
    poll_js(page, "!document.querySelector('#tiles .tile.add')")
    assert [t[1] for t in tiles(page)] == urls[:fg.NTP_MAX_MOST_VISITED]  # Chrome's 8, most visited first
    assert run_js(page, "getComputedStyle(document.getElementById('tiles')).gridTemplateColumns") == "112px " * 3 + "112px"
    # removing one (the tile's X) hides it; Restore all brings it back
    run_js(page, "document.querySelector('#tiles a.tile .tile-action').click(); 1")
    wait_until(lambda: settings.get("ntp_hidden") == [urls[0]])
    poll_js(page, f"document.querySelector('#tiles a.tile').getAttribute('href') === {json.dumps(urls[1])}")
    assert [t[1] for t in tiles(page)] == urls[1:fg.NTP_MAX_MOST_VISITED + 1]
    assert poll_js(page, "!document.getElementById('toast-restore').hidden")
    run_js(page, "document.getElementById('toast-restore').click(); 1")
    wait_until(lambda: settings.get("ntp_hidden") == [])
    # hiding the shortcuts, and a colour theme
    run_js(page, "document.getElementById('customize').click(); 1")
    poll_js(page, "document.getElementById('panel').classList.contains('open')")
    run_js(page, "document.getElementById('show').click(); 1")
    wait_until(lambda: settings.get("ntp_show_shortcuts") is False)
    assert poll_js(page, "document.getElementById('tiles').hidden")
    run_js(page, "document.querySelector('.chip[data-theme=green]').click(); 1")
    wait_until(lambda: settings.get("ntp_theme") == "green")
    assert poll_js(page, "document.documentElement.dataset.logo") == "single"
    assert style(page, "body", "backgroundColor") != "rgb(32, 33, 36)"
    run_js(page, "document.querySelector('.chip[data-theme=\"\"]').click(); 1")
    wait_until(lambda: settings.get("ntp_theme") == "")
    assert poll_js(page, "getComputedStyle(document.body).backgroundColor") == "rgb(32, 33, 36)"


def test_search_box_searches_or_opens_addresses(fg, window, ntp, server, monkeypatch):
    monkeypatch.setitem(fg.SEARCH_ENGINES, "Google", server.url("/page?q={}"))
    tab = window.current_tab()
    for text, expected in (("hello world", server.url("/page?q=hello+world")),
                           (f"127.0.0.1:{server.port}/page?n=typed", server.url("/page?n=typed")),
                           ("javascript:alert(1)", server.url("/page?q=javascript%3Aalert%281%29"))):
        page = open_ntp(window)
        run_js(page, f"document.getElementById('q').value = {json.dumps(text)}; "
                     "document.getElementById('searchbox').requestSubmit(); 1")
        wait_until(lambda: tab.url().toString() == expected and not tab.loading, 20, f"{text!r} -> {expected}")


# ── only the New Tab page can use its channel ──
def spy(monkeypatch, ntp) -> dict:
    calls = {"api": 0, "favicon": 0}
    for name in calls:
        real = getattr(ntp, name)

        def counted(job, _real=real, _name=name):
            calls[_name] += 1
            return _real(job)
        monkeypatch.setattr(ntp, name, counted)
    return calls


REACH_NTP = """
const out = {}, token = %s;
for (const [name, url, init] of [["api", "foxglove://newtab/api", {method: "POST", headers: {"X-NTP-Token": token},
                                   body: JSON.stringify({action: "add", title: "evil", url: "https://evil.example/"})}],
                                 ["page", "foxglove://newtab/", {}], ["favicon", "foxglove://newtab/favicon?host=x&t=" + token, {}]]) {
  try { const r = await fetch(url, init); out[name] = "read " + r.status + " " + (await r.text()).slice(0, 40); }
  catch (e) { out[name] = "refused"; }
}
out.xhr = await new Promise((done) => {
  const x = new XMLHttpRequest();
  try { x.open("GET", "foxglove://newtab/"); x.onload = () => done("read"); x.onerror = () => done("refused"); x.send(); }
  catch (e) { done("refused"); }
});
return out;"""


def test_web_pages_cant_use_the_new_tab_channel(fg, window, ntp, server, harness, monkeypatch):
    calls = spy(monkeypatch, ntp)
    page = open_ntp(window)
    token = run_js(page, "JSON.parse(document.getElementById('state').textContent).token")
    web = harness.fresh_page(server.page_url())
    out = run_js_async(web, REACH_NTP % json.dumps(token))  # even with the page's token
    assert out == {"api": "refused", "page": "refused", "favicon": "refused", "xhr": "refused"}, out
    run_js(web, "const f = document.createElement('iframe'); f.src = 'foxglove://newtab/'; document.body.append(f); 1")
    spin(1.0)
    assert run_js(web, "(() => { try { return document.querySelector('iframe').contentDocument.getElementById('state') ? "
                       "'read' : 'empty'; } catch (e) { return 'blocked'; } })()") in ("empty", "blocked")
    assert calls == {"api": 0, "favicon": 0} and window.settings.get("ntp_shortcuts") == []
    # the page itself: only with its token, and only POST
    add = json.dumps(json.dumps({"action": "add", "title": "x", "url": "https://x.example/"}))
    for init in (f"{{method: 'POST', headers: {{'X-NTP-Token': 'guess'}}, body: {add}}}", f"{{method: 'POST', body: {add}}}",
                 f"{{headers: {{'X-NTP-Token': {json.dumps(token)}}}}}"):
        assert run_js_async(page, f"return await (await fetch('/api', {init})).json()") == {"error": "Not allowed"}
    assert calls["api"] == 3 and window.settings.get("ntp_shortcuts") == []


class _Job:
    def __init__(self, initiator: str, token: str = "", method: bytes = b"POST"):
        self._initiator, self._token, self._method = initiator, token, method

    def initiator(self):
        return QUrl(self._initiator)

    def requestHeaders(self):
        return {b"X-NTP-Token": self._token.encode()} if self._token else {}

    def requestMethod(self):
        return self._method


def test_only_the_new_tab_origin_with_its_token_is_trusted(fg, window, ntp):
    assert ntp.trusted(_Job("foxglove://newtab", ntp.token))
    for job in (_Job("https://evil.example", ntp.token), _Job("chrome-extension://abcdefghijklmnopabcdefghijklmnop", ntp.token),
                _Job("foxglove://other", ntp.token), _Job("", ntp.token), _Job("foxglove://newtab", "x" + ntp.token),
                _Job("foxglove://newtab")):
        assert not ntp.trusted(job)
        assert ntp.api(job) is None
    assert ntp.api(_Job("foxglove://newtab", ntp.token, b"GET")) is None


NTP_PROBE_CS = "document.documentElement.setAttribute('data-ntp-probe', location.href);\n"


def test_extensions_cant_use_or_read_the_new_tab_page(fg, window, ntp, harness, tmp_path, monkeypatch):
    calls = spy(monkeypatch, ntp)
    ext = harness.install_ok(simple_ext(tmp_path / "x", "Snoop", {"cs.js": NTP_PROBE_CS},
                                        permissions=["storage", "scripting", "tabs"], host_permissions=["<all_urls>"],
                                        content_scripts=[{"matches": ["<all_urls>"], "js": ["cs.js"], "all_frames": True}]),
                             "Snoop")
    page = open_ntp(window)
    assert stays_absent(page, "data-ntp-probe", 2.5)  # no content script in it
    token = run_js(page, "JSON.parse(document.getElementById('state').textContent).token")
    epage = ext_page(harness, ext)
    out = run_js_async(epage, REACH_NTP % json.dumps(token))
    assert out == {"api": "refused", "page": "refused", "favicon": "refused", "xhr": "refused"}, out
    tab = window.current_tab()
    with pytest.raises(AssertionError, match="Cannot access contents"):
        run_js_async(epage, f"return await chrome.scripting.executeScript({{target: {{tabId: {tab.tab_id}}}, "
                            "func: () => document.getElementById('state').textContent})")
    assert calls == {"api": 0, "favicon": 0} and window.settings.get("ntp_shortcuts") == []
    assert run_js(page, "document.querySelectorAll('#tiles a.tile').length") == 0


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The privacy screen
# ══════════════════════════════════════════════════════════════════════════════════════════
def set_app_state(qapp, state) -> None:
    qapp.applicationStateChanged.emit(state)


INACTIVE, ACTIVE = Qt.ApplicationState.ApplicationInactive, Qt.ApplicationState.ApplicationActive


def test_privacy_screen_covers_windows_while_the_app_is_in_the_background(fg, window, qapp):
    shield = window.privacy_screen
    assert not shield.covering and not shield.isVisible()
    assert shield.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert shield.focusPolicy() == Qt.FocusPolicy.NoFocus
    try:
        set_app_state(qapp, INACTIVE)
        wait_until(lambda: shield.covering and shield.isVisible() and shield.opacity == 1.0, 5, "the window to be covered")
        assert shield.geometry() == window.rect()  # toolbars and all
        assert [c for c in window.children() if isinstance(c, QWidget) and c.isVisible()][-1] is shield  # on top
        image = window.grab().toImage()
        center = QPoint(image.width() // 2, image.height() // 2)
        assert image.pixelColor(5, image.height() - 5).name() == fg.PrivacyScreen.COLOR  # grey
        assert image.pixelColor(5, 5).name() == fg.PrivacyScreen.COLOR                    # over the tab strip too
        assert image.pixelColor(center).name() == "#1a73e8"                                # the logo's blue centre
        assert window.childAt(window.rect().center()) is not shield  # never takes a click
        window.resize(window.width() + 40, window.height() + 30)
        wait_until(lambda: shield.geometry() == window.rect(), 5, "the cover to follow the window's size")
        set_app_state(qapp, ACTIVE)
        assert not shield.covering and not shield.isVisible()  # at once
        # a moment in the background (a full-screen switch, a prompt) doesn't flash it
        set_app_state(qapp, INACTIVE)
        set_app_state(qapp, ACTIVE)
        spin(0.3)
        assert not shield.covering
        # full screen while the app is active: nothing
        window.showFullScreen()
        spin(0.3)
        assert not shield.covering
        window.showNormal()
        # pop-up windows too
        popup = fg.PopupWindow(window, QRect())
        popup.show()
        set_app_state(qapp, INACTIVE)
        wait_until(lambda: popup.privacy_screen.covering and shield.covering, 5, "the pop-up to be covered")
        assert popup.privacy_screen.geometry() == popup.rect()
        popup.close()  # closing while covered is fine
        spin(0.2)
        set_app_state(qapp, ACTIVE)
        assert not shield.covering
    finally:
        set_app_state(qapp, ACTIVE)


def test_privacy_screen_setting(fg, window, qapp):
    shield, settings = window.privacy_screen, window.settings
    assert settings.get("privacy_screen") is True
    dialog = fg.SettingsDialog(window)
    box = dialog.privacy_screen
    assert box.text() == "Hide pages when Chrome 2 isn't in focus" and box.isChecked()
    try:
        box.setChecked(False)
        assert settings.get("privacy_screen") is False
        set_app_state(qapp, INACTIVE)
        spin(0.4)
        assert not shield.covering and not shield.isVisible()
        set_app_state(qapp, ACTIVE)
        box.setChecked(True)
        set_app_state(qapp, INACTIVE)
        wait_until(lambda: shield.covering, 5, "the window to be covered")
        box.setChecked(False)  # turned off while covered: gone at once
        assert not shield.covering and not shield.isVisible()
        box.setChecked(True)
        set_app_state(qapp, INACTIVE)
        wait_until(lambda: shield.covering, 5, "the window to be covered")
        window.close_now()  # quitting while covered
        spin(0.2)
    finally:
        if not sip.isdeleted(dialog):  # (closing the window took it along)
            dialog.deleteLater()
        set_app_state(qapp, ACTIVE)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  --install-app
# ══════════════════════════════════════════════════════════════════════════════════════════
def _icns_chunks(data: bytes) -> dict[bytes, bytes]:
    assert data[:4] == b"icns" and int.from_bytes(data[4:8], "big") == len(data)
    chunks, pos = {}, 8
    while pos < len(data):
        kind, size = data[pos:pos + 4], int.from_bytes(data[pos + 4:pos + 8], "big")
        chunks[kind] = data[pos + 8:pos + size]
        pos += size
    return chunks


def test_install_app_makes_a_macos_app_bundle(fg, tmp_path, monkeypatch, qapp):
    real_which = shutil.which
    monkeypatch.setattr(fg.shutil, "which", lambda name, *a, **k: None if name == "iconutil" else real_which(name, *a, **k))
    log_home = tmp_path / "home"
    recorder = tmp_path / "My Venv" / "bin" / "python3"  # stands in for the venv's Python: records how it was started
    recorder.parent.mkdir(parents=True)
    recorder.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$HOME/started-with"\necho started\n')
    recorder.chmod(0o755)
    script = tmp_path / "it's here" / "foxglove.py"
    bundle = fg.make_app_bundle(tmp_path / "Applications" / "Chrome 2.app", str(recorder), str(script))
    contents = bundle / "Contents"
    info = plistlib.loads((contents / "Info.plist").read_bytes())
    assert info["CFBundleName"] == info["CFBundleDisplayName"] == "Chrome 2"
    assert info["CFBundleIdentifier"] == fg.APP_BUNDLE_ID and info["CFBundlePackageType"] == "APPL"
    assert info["CFBundleIconFile"] == "icon.icns"
    launcher = contents / "MacOS" / info["CFBundleExecutable"]
    assert launcher.stat().st_mode & stat.S_IXUSR and launcher.read_text().startswith("#!/bin/sh\n")
    assert "Library/Logs/Chrome 2.log" in launcher.read_text()
    assert (contents / "PkgInfo").read_text() == "APPL????"
    assert not list(bundle.parent.glob("*.part"))
    # the launcher starts this script with that Python (quoting and all) from cached bytecode (python -c boot_command),
    # passing arguments on, output to the log
    log_home.mkdir()
    subprocess.run([str(launcher), "https://example.com/"], env={**os.environ, "HOME": str(log_home)}, check=True, timeout=30)
    started_with = (log_home / "started-with").read_text().splitlines()
    assert started_with == ["-c", *fg.boot_command(str(script)).splitlines(), "https://example.com/"]
    assert "started" in (log_home / "Library" / "Logs" / "Chrome 2.log").read_text()
    # no iconutil (not macOS): the icon is written directly, every size a PNG of the logo
    chunks = _icns_chunks((contents / "Resources" / "icon.icns").read_bytes())
    assert set(chunks) == set(fg.ICNS_TYPES.values())
    big = QImage.fromData(chunks[b"ic10"], "PNG")
    assert big.width() == 1024 and big.pixelColor(512, 512).name() == "#1a73e8"
    assert big.pixelColor(20, 20).alpha() == 0  # the margin app icons have
    # again (an update): replaces the old bundle
    fg.make_app_bundle(bundle, sys.executable, str(script))
    assert sys.executable in launcher.read_text()


def test_install_app_uses_iconutil_with_every_iconset_size(fg, tmp_path, monkeypatch, qapp):
    fake = tmp_path / "bin" / "iconutil"
    fake.parent.mkdir()
    fake.write_text('#!/bin/sh\n# iconutil -c icns <iconset> -o <file>\nls "$3" > "$5.list"\nprintf icns > "$5"\n')
    fake.chmod(0o755)
    real_which = shutil.which
    monkeypatch.setattr(fg.shutil, "which", lambda name, *a, **k: str(fake) if name == "iconutil" else real_which(name, *a, **k))
    target = tmp_path / "icon.icns"
    assert fg.write_icns(target) == "iconutil"
    assert sorted(Path(str(target) + ".list").read_text().split()) == sorted(
        f"icon_{s}x{s}{'@2x' if k == 2 else ''}.png" for s, k in fg.ICONSET_SIZES)


def test_install_app_command(fg, tmp_path, monkeypatch, capsys, qapp):
    monkeypatch.setattr(fg, "IS_MAC", True)
    assert fg.install_app(home=tmp_path) == 0
    bundle = tmp_path / "Applications" / "Chrome 2.app"
    assert (bundle / "Contents" / "Info.plist").exists() and (bundle / "Contents" / "Resources" / "icon.icns").exists()
    assert os.path.abspath(fg.__file__) in (bundle / "Contents" / "MacOS" / fg.APP_EXECUTABLE).read_text()
    out = capsys.readouterr().out
    assert "Keep in Dock" in out and str(bundle) in out
    # Linux: an app-menu entry instead
    monkeypatch.setattr(fg, "IS_MAC", False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert fg.install_app(home=tmp_path) == 0
    desktop = (tmp_path / "share" / "applications" / "chrome-2.desktop").read_text()
    assert "Name=Chrome 2" in desktop and f'Exec="{sys.executable}" "{os.path.abspath(fg.__file__)}" %U' in desktop
    assert QImage(str(tmp_path / "share" / "icons" / "hicolor" / "256x256" / "apps" / "chrome-2.png")).width() == 256
    # main() hands --install-app over before starting any browser
    monkeypatch.setattr(fg, "install_app", lambda: 7)
    assert fg.main(["foxglove.py", "--install-app"]) == 7
