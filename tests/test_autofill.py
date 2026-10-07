"""Autofill and passwords: saving logins, addresses and cards (secrets in the keychain only), Chrome's native suggestion
list, filling through the isolated world, and what pages can't see."""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import keyring
import keyring.backend
import keyring.errors
import pytest
from PyQt6.QtCore import QPoint, Qt
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QLabel, QPushButton

from helpers import load, poll_js, run_js, spin, wait_until

PASSWORD = "s3cret-Pa55word!"


class MemoryKeyring(keyring.backend.KeyringBackend):
    """A keychain in memory (this machine has no OS keychain)."""
    priority = 1

    def __init__(self):
        super().__init__()
        self.items: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.items.get((service, username))

    def set_password(self, service, username, password):
        self.items[(service, username)] = password

    def delete_password(self, service, username):
        if self.items.pop((service, username), None) is None:
            raise keyring.errors.PasswordDeleteError(username)


class FormServer:
    """GET and POST on 127.0.0.1:<free port>; every path in *routes* answers with its HTML."""

    def __init__(self) -> None:
        self.routes: dict[str, bytes] = {}
        self.posts: list[str] = []
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                pass

            def _answer(self) -> None:
                body = server.routes.get(self.path.split("?")[0], b"<!doctype html><title>404</title>not found")
                self.send_response(200 if self.path.split("?")[0] in server.routes else 404)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                self._answer()

            def do_POST(self) -> None:
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                server.posts.append(self.path)
                self._answer()

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.httpd.daemon_threads = True
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def url(self, path: str) -> str:
        return self.origin + path

    def add(self, path: str, html: str) -> str:
        self.routes[path] = html.encode()
        return self.url(path)

    def close(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()


LOGIN = """<!doctype html><html><head><title>Sign in</title></head><body style="margin:20px">
<form id="f" method="post" action="/welcome">
  <input id="user" name="username" autocomplete="username" style="width:220px;height:24px"><br><br>
  <input id="pass" name="password" type="password" style="width:220px;height:24px"><br><br>
  <button id="go" type="submit">Sign in</button>
</form></body></html>"""
WELCOME = "<!doctype html><title>Welcome</title><p id=hi>Signed in</p>"

# React's controlled inputs: the framework tracks the value it last set (a setter on the element itself, in the page's
# own world) and only takes a change it didn't make; then it renders its state back into the field.
REACT_LOGIN = """<!doctype html><html><head><title>React sign in</title></head><body style="margin:20px">
<div id="root">
  <input id="user" placeholder="Email or username" style="width:220px;height:24px"><br><br>
  <input id="pass" type="password" placeholder="Password" style="width:220px;height:24px"><br><br>
  <div id="go" role="button">Sign in</div>
</div>
<script>
const state = {user: "", pass: ""};
window.reactState = state;
window.spy = [];
const proto = Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value");
for (const [id, key] of [["user", "user"], ["pass", "pass"]]) {
  const el = document.getElementById(id);
  let tracked = el.value;
  Object.defineProperty(el, "value", {configurable: true, get() { return proto.get.call(this); },
                                       set(v) { tracked = String(v); proto.set.call(this, v); }});
  el.addEventListener("input", () => {
    const now = proto.get.call(el);
    if (now !== tracked) { tracked = now; state[key] = now; }
    el.value = state[key];
  });
}
for (const type of ["focus", "focusin", "input", "change", "message", "keydown", "keyup", "click", "mousedown"])
  window.addEventListener(type, (e) => spy.push([type, String(e.data || ""), String(e.detail || "")]), true);
new MutationObserver((records) => records.forEach((r) => spy.push(["mutation", r.attributeName || "", String(r.target.outerHTML || "").slice(0, 200)])))
  .observe(document, {subtree: true, attributes: true, childList: true, characterData: true});
document.getElementById("go").addEventListener("click", () => { if (state.pass) location.href = "/welcome"; });
</script></body></html>"""

CHECKOUT = """<!doctype html><html><head><title>Checkout</title></head><body style="margin:20px">
<form id="f" method="post" action="/thanks">
  <input id="name" autocomplete="name" style="width:220px;height:22px"><br>
  <input id="street" autocomplete="address-line1" style="width:220px;height:22px"><br>
  <input id="city" autocomplete="address-level2" style="width:220px;height:22px"><br>
  <select id="state" autocomplete="address-level1"><option value="">-</option><option value="CA">California</option>
    <option value="NY">New York</option></select><br>
  <input id="zip" autocomplete="postal-code" style="width:220px;height:22px"><br>
  <input id="email" type="email" name="email" style="width:220px;height:22px"><br>
  <input id="cc" name="cardnumber" autocomplete="cc-number" style="width:220px;height:22px"><br>
  <input id="exp" autocomplete="cc-exp" placeholder="MM/YY" style="width:220px;height:22px"><br>
  <input id="cvc" autocomplete="cc-csc" style="width:220px;height:22px"><br>
  <input id="ccname" autocomplete="cc-name" style="width:220px;height:22px"><br>
  <button type="submit">Pay</button>
</form></body></html>"""


@pytest.fixture
def vault(fg, monkeypatch):
    ring = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(ring)
    monkeypatch.setattr(fg, "_secret_store", None)  # a fresh SecretStore finds this keychain
    yield ring
    keyring.set_keyring(old)


@pytest.fixture
def forms():
    srv = FormServer()
    srv.add("/welcome", WELCOME)
    srv.add("/thanks", "<!doctype html><title>Thanks</title><p>Order placed</p>")
    yield srv
    srv.close()


@pytest.fixture
def win(vault, harness):
    return harness.window()


# ── helpers ───────────────────────────────────────────────────────────────────────────────
def show(win, url: str):
    tab = win.current_tab()
    assert load(tab.page, url)
    win.activateWindow()
    tab.view.setFocus()
    spin(0.3)
    return tab


def click_at(tab, x: float, y: float) -> None:
    """A real click (user input) at CSS pixel (x, y) of the page."""
    zoom = tab.page.zoomFactor()
    point = QPoint(int(x * zoom), int(y * zoom))
    QTest.mouseMove(tab.view.focusProxy(), point)  # (frames from other sites find their events by the pointer's place)
    spin(0.15)
    QTest.mouseClick(tab.view.focusProxy(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, point)


def click(tab, selector: str) -> None:
    rect = json.loads(run_js(tab.page, f"JSON.stringify(document.querySelector({json.dumps(selector)}).getBoundingClientRect())"))
    click_at(tab, rect["x"] + 8, rect["y"] + rect["height"] / 2)


def keys(tab, text: str) -> None:
    QTest.keyClicks(tab.view.focusProxy(), text)


def key(tab, which) -> None:
    QTest.keyClick(tab.view.focusProxy(), which)


def popup(fg, win):
    return win.findChild(fg.AutofillPopup)


def wait_popup(fg, win, timeout: float = 10):
    return wait_until(lambda: (p := popup(fg, win)) is not None and p.isVisible() and p, timeout, "the suggestion list")


def no_popup(fg, win, seconds: float = 1.5) -> bool:
    spin(seconds)
    p = popup(fg, win)
    return p is None or not p.isVisible()


def bubble(fg, win, kind: str | None = None, timeout: float = 10):
    def found():
        for b in win.findChildren(fg.AutofillBubble):
            if b.isVisible() and (kind is None or b.kind == kind):
                return b
        return None
    return wait_until(found, timeout, f"the {kind or 'autofill'} bubble")


def press(widget, text: str) -> None:
    button = next(b for b in widget.findChildren(QPushButton) if b.text() == text)
    button.click()


def value(tab, selector: str):
    return run_js(tab.page, f"document.querySelector({json.dumps(selector)}).value")


def frame_js(frame, script: str, world: int = 0):
    box = {}
    frame.runJavaScript(script, world, lambda r: box.setdefault("r", r))
    wait_until(lambda: "r" in box, 10, f"frame JS {script[:60]!r}")
    return box["r"]


def sign_in(tab, username: str, password: str) -> None:
    click(tab, "#user")
    keys(tab, username)
    click(tab, "#pass")
    keys(tab, password)
    key(tab, Qt.Key.Key_Return)


def secrets_on_disk(directory) -> str:
    text = ""
    for name in ("autofill.json", "settings.json", "session.json", "bookmarks.json"):
        path = directory / name
        if path.exists():
            text += path.read_text(encoding="utf-8")
    return text


# ── passwords ─────────────────────────────────────────────────────────────────────────────
def test_login_is_offered_saved_to_keychain_and_filled_on_revisit(fg, win, vault, forms, harness):
    url = forms.add("/login", LOGIN)
    tab = show(win, url)
    sign_in(tab, "alice", PASSWORD)
    wait_until(lambda: "/welcome" in tab.page.url().toString(), 10, "the signed-in page")
    offer = bubble(fg, win, "save-password")
    assert offer.username.text() == "alice" and offer.password.text() == PASSWORD
    assert offer.password.echoMode() == offer.password.EchoMode.Password  # masked until you ask
    assert win.url_bar.autofill_action.isVisible()
    press(offer, "Save")
    data = win.autofill.data
    assert [(e["origin"], e["username"]) for e in data.logins] == [(forms.origin, "alice")]
    entry_id = data.logins[0]["id"]
    assert vault.items == {("Chrome 2", f"password:{entry_id}"): PASSWORD}  # the keychain, under the fixed service name
    win.save_session()
    win.settings.save()
    assert PASSWORD not in secrets_on_disk(harness.dir)
    path = harness.dir / "autofill.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert "alice" in path.read_text()

    # back on the sign-in page: the browser's own list offers the username (never the password)
    tab = show(win, url)
    click(tab, "#user")
    pop = wait_popup(fg, win)
    texts = [w.text.text() for w in pop.widgets]
    assert texts[0] == "alice" and not any(PASSWORD in t for t in texts)
    assert value(tab, "#user") == "" and value(tab, "#pass") == ""  # nothing reached the page yet
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.querySelector('#pass').value", lambda v: v == PASSWORD, what="the filled password")
    assert value(tab, "#user") == "alice"
    assert not pop.isVisible()


def test_fill_reaches_react_controlled_inputs_and_update_password(fg, win, vault, forms):
    url = forms.add("/react", REACT_LOGIN)
    win.autofill.data.add_login(forms.origin, "alice", "old-pass-1")
    tab = show(win, url)
    click(tab, "#user")
    pop = wait_popup(fg, win)
    assert [w.text.text() for w in pop.widgets][:1] == ["alice"]
    spin(0.6)  # (clicks right after the list appears are ignored)
    pop.clicked(0)
    poll_js(tab.page, "JSON.stringify(window.reactState)", lambda v: json.loads(v) == {"user": "alice", "pass": "old-pass-1"},
            what="React's state")
    assert value(tab, "#pass") == "old-pass-1"

    # a new password typed for the same user: "Update password?"
    run_js(tab.page, "document.querySelector('#pass').select()")
    click(tab, "#pass")
    run_js(tab.page, "document.querySelector('#pass').select()")
    keys(tab, "new-pass-2")
    poll_js(tab.page, "window.reactState.pass", lambda v: v == "new-pass-2", what="the typed password")
    click(tab, "#go")
    wait_until(lambda: "/welcome" in tab.page.url().toString(), 10, "the signed-in page")
    offer = bubble(fg, win, "update-password")
    assert offer.password.text() == "new-pass-2"
    press(offer, "Update")
    entry = win.autofill.data.logins[0]
    assert len(win.autofill.data.logins) == 1 and vault.items[("Chrome 2", f"password:{entry['id']}")] == "new-pass-2"


def test_never_for_this_site(fg, win, vault, forms):
    url = forms.add("/login", LOGIN)
    tab = show(win, url)
    sign_in(tab, "bob", PASSWORD)
    offer = bubble(fg, win, "save-password")
    press(offer, "Never")
    data = win.autofill.data
    assert data.never == [forms.origin] and data.logins == [] and vault.items == {}
    tab = show(win, url)
    sign_in(tab, "bob", PASSWORD)
    wait_until(lambda: "/welcome" in tab.page.url().toString(), 10, "the signed-in page")
    spin(2.0)
    assert not any(b.isVisible() for b in win.findChildren(fg.AutofillBubble))
    # the Password Manager lists the site, and taking it off the list lets the offer come back
    win.show_autofill_settings("passwords")
    dialog = win._dialogs["autofill"]
    assert dialog.never_list.topLevelItemCount() == 1
    dialog.never_list.setCurrentItem(dialog.never_list.topLevelItem(0))
    dialog.never_remove.click()
    assert data.never == []


def test_username_first_then_password_page(fg, win, vault, forms):
    forms.add("/step1", """<!doctype html><body style="margin:20px"><form method="post" action="/step2">
        <input id="user" type="email" name="identifier" style="width:220px;height:24px"><button>Next</button></form></body>""")
    forms.add("/step2", """<!doctype html><body style="margin:20px"><form method="post" action="/welcome">
        <input id="pass" type="password" name="password" style="width:220px;height:24px"><button>Sign in</button></form></body>""")
    tab = show(win, forms.url("/step1"))
    click(tab, "#user")
    keys(tab, "mia@example.com")
    key(tab, Qt.Key.Key_Return)
    wait_until(lambda: "/step2" in tab.page.url().toString(), 10, "the password step")
    spin(0.3)
    click(tab, "#pass")
    keys(tab, PASSWORD)
    key(tab, Qt.Key.Key_Return)
    offer = bubble(fg, win, "save-password")
    assert offer.username.text() == "mia@example.com"  # remembered from the step before
    press(offer, "Save")
    # next time the password step alone is filled from the one saved login
    tab = show(win, forms.url("/step2"))
    click(tab, "#pass")
    wait_popup(fg, win)
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.querySelector('#pass').value", lambda v: v == PASSWORD, what="the password")


def test_change_password_form_offers_update(fg, win, vault, forms):
    win.autofill.data.add_login(forms.origin, "nora", "old-pw")
    forms.add("/change", """<!doctype html><body style="margin:20px"><form method="post" action="/welcome">
        <input id="cur" type="password" autocomplete="current-password"><br>
        <input id="new" type="password" autocomplete="new-password"><br>
        <input id="again" type="password" autocomplete="new-password"><br><button>Change</button></form></body>""")
    tab = show(win, forms.url("/change"))
    for selector, text in (("#cur", "old-pw"), ("#new", "brand-new-pw"), ("#again", "brand-new-pw")):
        click(tab, selector)
        keys(tab, text)
    key(tab, Qt.Key.Key_Return)
    offer = bubble(fg, win, "update-password")
    assert offer.prompt["username"] == "nora" and offer.password.text() == "brand-new-pw"
    press(offer, "Update")
    entry = win.autofill.data.logins[0]
    assert vault.items[("Chrome 2", f"password:{entry['id']}")] == "brand-new-pw"


def test_typing_filters_and_the_list_follows_scrolling(fg, win, vault, forms):
    for name in ("olga", "oscar", "paul"):
        win.autofill.data.add_login(forms.origin, name, "pw-" + name)
    url = forms.add("/long", LOGIN.replace("<form", '<div style="height:200px"></div><form') + "<div style='height:3000px'></div>")
    tab = show(win, url)
    click(tab, "#user")
    pop = wait_popup(fg, win)
    assert [w.text.text() for w in pop.widgets[:3]] == ["olga", "oscar", "paul"]
    keys(tab, "os")
    wait_until(lambda: [w.text.text() for w in pop.widgets][:1] == ["oscar"] and len(pop.widgets) == 2, 10, "the filtered list")
    top = pop.y()
    run_js(tab.page, "window.scrollBy(0, 120); true")
    wait_until(lambda: pop.isVisible() and abs(pop.y() - (top - 120)) <= 2, 10, f"the list to follow its field (was at {top})")
    run_js(tab.page, "window.scrollBy(0, 2000); true")
    wait_until(lambda: not pop.isVisible(), 10, "the list to close with its field out of sight")


def test_sign_in_popup_that_closes_itself_is_offered_in_the_window(fg, win, vault, forms):
    forms.add("/popup-login", LOGIN.replace('action="/welcome"', 'action="/done"'))
    forms.add("/done", "<!doctype html><title>Done</title><script>window.close()</script>")
    url = forms.add("/opener", """<!doctype html><body style="margin:20px"><button id="b" style="width:120px;height:30px"
        onclick="window.open('/popup-login', 'login', 'width=420,height=500')">Sign in</button></body>""")
    tab = show(win, url)
    click(tab, "#b")
    popup_window = wait_until(lambda: next(iter(win.popups), None), 10, "the sign-in pop-up")
    wait_until(lambda: "/popup-login" in popup_window.page.url().toString() and not popup_window.loading, 10, "its page")
    popup_window.activateWindow()
    popup_window.view.setFocus()
    spin(0.3)
    sign_in(popup_window, "pia", PASSWORD)
    wait_until(lambda: not win.popups, 10, "the pop-up to close itself")
    offer = bubble(fg, win, "save-password")
    assert offer.username.text() == "pia"
    press(offer, "Save")
    assert [(e["origin"], e["username"]) for e in win.autofill.data.logins] == [(forms.origin, "pia")]


def test_failed_login_is_not_offered(fg, win, vault, forms):
    forms.add("/again", LOGIN.replace('action="/welcome"', 'action="/again"'))
    tab = show(win, forms.url("/again"))
    sign_in(tab, "carol", "wrong-password")
    wait_until(lambda: len(forms.posts) == 1, 10, "the post")
    spin(2.0)
    assert not any(b.isVisible() for b in win.findChildren(fg.AutofillBubble))


def test_cross_origin_iframe_is_not_filled(fg, win, vault, forms):
    other = FormServer()
    try:
        other.add("/frame", """<!doctype html><body style="margin:0">
            <input id="user" autocomplete="username" style="position:absolute;left:10px;top:10px;width:200px;height:24px">
            <input id="pass" type="password" style="position:absolute;left:10px;top:50px;width:200px;height:24px"></body>""")
        url = forms.add("/top", f"""<!doctype html><body style="margin:0">
            <iframe id="f" src="{other.url('/frame')}" style="position:absolute;left:40px;top:60px;width:300px;height:120px;border:0"></iframe>
            </body>""")
        win.autofill.data.add_login(forms.origin, "top-user", "top-secret")  # the page's own origin, not the frame's
        tab = show(win, url)
        wait_until(lambda: tab.page.mainFrame().children(), 10, "the frame")
        frame = tab.page.mainFrame().children()[0]
        wait_until(lambda: frame_js(frame, "!!document.querySelector('#user')"), 10, "the frame's form")
        click_at(tab, 40 + 20, 60 + 22)
        wait_until(lambda: frame_js(frame, "document.activeElement && document.activeElement.id") == "user", 10, "focus in the frame")
        assert no_popup(fg, win)
        assert frame_js(frame, "document.querySelector('#user').value + document.querySelector('#pass').value") == ""

        # a login saved for the frame's own origin is offered there (and only goes into that frame)
        win.autofill.data.add_login(other.origin, "frame-user", "frame-secret")
        click_at(tab, 40 + 20, 60 + 22)
        pop = wait_popup(fg, win)
        assert [w.text.text() for w in pop.widgets][0] == "frame-user"
        assert pop.y() > 60  # under the field inside the frame
        key(tab, Qt.Key.Key_Down)
        key(tab, Qt.Key.Key_Return)
        wait_until(lambda: frame_js(frame, "document.querySelector('#pass').value") == "frame-secret", 10, "the frame filled")
        assert frame_js(frame, "document.querySelector('#user').value") == "frame-user"
    finally:
        other.close()


def test_page_cannot_read_saved_data_before_it_is_picked(fg, win, vault, forms):
    url = forms.add("/react", REACT_LOGIN)
    win.autofill.data.add_login(forms.origin, "alice", PASSWORD)
    tab = show(win, url)
    click(tab, "#user")
    wait_popup(fg, win)
    spin(0.5)
    page_view = run_js(tab.page, """JSON.stringify({
        html: document.documentElement.outerHTML, user: document.querySelector('#user').value,
        pass: document.querySelector('#pass').value, state: window.reactState, spy: window.spy,
        api: typeof __fgAutofill, qt: typeof qt, globals: Object.keys(window).filter(k => /autofill|fg/i.test(k)),
        storage: JSON.stringify(localStorage) + JSON.stringify(sessionStorage) + document.cookie})""")
    assert PASSWORD not in page_view and "alice" not in page_view
    seen = json.loads(page_view)
    assert seen["api"] == "undefined" and seen["qt"] == "undefined" and seen["user"] == "" and seen["pass"] == ""
    # the isolated world's API isn't reachable from the page, and forged pokes do nothing
    run_js(tab.page, f"console.debug({json.dumps(fg.AUTOFILL_POKE)} + 'guess:' + location.href); "
                     "document.dispatchEvent(new FocusEvent('focusin', {bubbles: true})); true")
    spin(0.5)
    assert PASSWORD not in run_js(tab.page, "JSON.stringify(window.spy) + document.documentElement.outerHTML")
    # picked: now it's the page's
    pop = wait_popup(fg, win)
    spin(0.5)
    pop.clicked(0)
    poll_js(tab.page, "window.reactState.pass", lambda v: v == PASSWORD, what="the filled password")


def test_suggestions_need_a_user_gesture_and_ignore_early_clicks(fg, win, vault, forms):
    url = forms.add("/login", LOGIN)
    win.autofill.data.add_login(forms.origin, "alice", PASSWORD)
    tab = show(win, url)
    run_js(tab.page, "document.querySelector('#user').focus(); true")  # the page moving the focus by itself
    assert no_popup(fg, win)
    key(tab, Qt.Key.Key_Down)  # ... the arrow key opens the list, like Chrome
    pop = wait_popup(fg, win)
    pop.clicked(0)  # at once: ignored (it may have popped up under the mouse)
    spin(0.3)
    assert value(tab, "#pass") == ""
    key(tab, Qt.Key.Key_Escape)
    assert not pop.isVisible()


def test_http_warning_and_card_filling_off_on_insecure_pages(fg, win, vault, monkeypatch):
    autofill = win.autofill
    autofill.data.add_login("http://example.com", "dave", "pw")
    autofill.data.add_card("4111111111111111", "Dave", "12", "2030")
    target = fg.AutofillTarget(win.current_tab().page, None, (), "http://example.com", 1, "login", "username")
    rows = autofill.suggestions(target)
    assert rows[0].get("note") and "isn't secure" in rows[0]["text"] and rows[1]["text"] == "dave"
    card = fg.AutofillTarget(win.current_tab().page, None, (), "http://example.com", 1, "card", type="cc-number")
    rows = autofill.suggestions(card)
    assert len(rows) == 1 and rows[0].get("note") and "pick" not in rows[0]
    secure = fg.AutofillTarget(win.current_tab().page, None, (), "https://example.com", 1, "card", type="cc-number")
    assert autofill.suggestions(secure)[0]["text"] == "Visa •••• 1111"
    assert fg.secure_origin("http://127.0.0.1:8000") and fg.secure_origin("http://localhost") and not fg.secure_origin("http://10.0.0.2")


def test_same_site_matches_are_offered_with_their_domain(fg, win, vault):
    data = win.autofill.data
    data.add_login("https://www.example.com", "erin", "pw1")
    data.add_login("https://login.example.com", "frank", "pw2")
    data.add_login("https://alice.github.io", "gina", "pw3")
    data.add_login("http://login.example.com", "hank", "pw4")
    found = [(e["username"], exact) for e, exact in data.logins_for("https://login.example.com")]
    assert found == [("frank", True), ("erin", False)]  # never another scheme, never across a public suffix
    assert data.logins_for("https://bob.github.io") == []
    assert data.logins_for("http://127.0.0.1:9") == [] and not data.same_site("http://127.0.0.1:1", "http://127.0.0.1:2")
    target = fg.AutofillTarget(win.current_tab().page, None, (), "https://login.example.com", 1, "login", "password")
    rows = win.autofill.suggestions(target)
    assert [(r["text"], r["sub"]) for r in rows[:2]] == [("frank", "••••••••"), ("erin", "www.example.com")]


# ── addresses and cards ───────────────────────────────────────────────────────────────────
def test_address_and_card_are_offered_saved_and_filled_never_the_cvc(fg, win, vault, forms, harness):
    url = forms.add("/checkout", CHECKOUT)
    tab = show(win, url)
    run_js(tab.page, """(() => { const set = (id, v) => document.getElementById(id).value = v;
        set('name', 'Ada Lovelace'); set('street', '12 Analytical Way'); set('city', 'Springfield'); set('state', 'CA');
        set('zip', '90210'); set('email', 'ada@example.com'); set('cc', '4111 1111 1111 1111'); set('exp', '11/31');
        set('cvc', '987'); set('ccname', 'Ada Lovelace');
        document.getElementById('f').requestSubmit(); return true; })()""")
    card_offer = bubble(fg, win, "save-card")
    assert card_offer.prompt["card"]["month"] == "11" and card_offer.prompt["card"]["year"] == "2031"
    assert sorted(card_offer.prompt["card"]) == ["month", "name", "network", "number", "year"]  # (no security code)
    assert "987" not in card_offer.prompt["card"].values()
    press(card_offer, "Save")
    address_offer = bubble(fg, win, "save-address")  # (the next offer follows)
    assert address_offer.prompt["address"]["city"] == "Springfield"
    press(address_offer, "Save")
    data = win.autofill.data
    assert [(c["network"], c["last4"], c["month"], c["year"], c["name"]) for c in data.cards] == \
        [("Visa", "1111", "11", "2031", "Ada Lovelace")]
    assert vault.items == {("Chrome 2", f"card:{data.cards[0]['id']}"): "4111111111111111"}  # the number, nothing else
    saved = (harness.dir / "autofill.json").read_text()
    records = json.loads(saved)["cards"] + json.loads(saved)["addresses"]
    assert "4111111111111111" not in saved and "csc" not in saved and "cvc" not in saved
    assert all("987" not in record.values() for record in records)
    assert [(a["name"], a["line1"], a["city"], a["state"], a["zip"], a["email"]) for a in data.addresses] == \
        [("Ada Lovelace", "12 Analytical Way", "Springfield", "CA", "90210", "ada@example.com")]

    # a fresh checkout: the address goes in with one pick, then the card (never a CVC)
    tab = show(win, url)
    click(tab, "#name")
    pop = wait_popup(fg, win)
    assert pop.widgets[0].text.text() == "Ada Lovelace"
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.getElementById('zip').value", lambda v: v == "90210", what="the address")
    filled = json.loads(run_js(tab.page, "JSON.stringify(['street','city','state','email','cc'].map(id => document.getElementById(id).value))"))
    assert filled == ["12 Analytical Way", "Springfield", "CA", "ada@example.com", ""]
    click(tab, "#cc")
    pop = wait_popup(fg, win)
    assert pop.widgets[0].text.text() == "Visa •••• 1111"
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.getElementById('cc').value", lambda v: v == "4111111111111111", what="the card")
    assert value(tab, "#exp") == "11/31" and value(tab, "#ccname") == "Ada Lovelace" and value(tab, "#cvc") == ""


def test_autocomplete_off_addresses_are_left_alone(fg, win, vault, forms):
    win.autofill.data.add_address({"name": "Ada Lovelace", "line1": "12 Analytical Way", "city": "Springfield",
                                   "state": "CA", "zip": "90210"})
    plain = """<!doctype html><body style="margin:20px"><form>
      <input name="full_name" placeholder="Full name" OFF><br><input name="address1" placeholder="Street address" OFF><br>
      <input name="city" placeholder="City" OFF><br><input name="zip" placeholder="ZIP code" OFF><br>
      <button>Ship here</button></form></body>"""  # (no autocomplete types: the names say what the fields are)
    tab = show(win, forms.add("/off", plain.replace("OFF", 'autocomplete="off"')))
    click(tab, "input[name=city]")
    assert no_popup(fg, win)
    tab = show(win, forms.add("/on", plain.replace("OFF", "")))
    click(tab, "input[name=city]")
    pop = wait_popup(fg, win)
    assert pop.widgets[0].text.text() == "Springfield"
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.querySelector('input[name=address1]').value", lambda v: v == "12 Analytical Way", what="the street")
    assert value(tab, "input[name=full_name]") == "Ada Lovelace" and value(tab, "input[name=zip]") == "90210"
    # a form-level autocomplete="off" doesn't override the fields' own autocomplete types
    tab = show(win, forms.add("/typed", CHECKOUT.replace('<form id="f"', '<form id="f" autocomplete="off"')))
    click(tab, "#city")
    assert wait_popup(fg, win).widgets[0].text.text() == "Springfield"


# ── the manager ───────────────────────────────────────────────────────────────────────────
def test_manager_shows_copies_edits_and_deletes(fg, win, vault, monkeypatch):
    data = win.autofill.data
    entry = data.add_login("https://example.com", "ivy", "first-pw")
    win.show_autofill_settings("passwords")
    dialog = win._dialogs["autofill"]
    tree = dialog.password_list
    assert tree.topLevelItemCount() == 1 and tree.topLevelItem(0).text(1) == "ivy"
    assert tree.topLevelItem(0).text(2) == dialog.MASK
    tree.setCurrentItem(tree.topLevelItem(0))
    dialog.show_button.click()
    assert tree.topLevelItem(0).text(2) == "first-pw"
    dialog.show_button.click()
    assert tree.topLevelItem(0).text(2) == dialog.MASK
    dialog.copy_button.click()
    assert QGuiApplication.clipboard().text() == "first-pw"
    QGuiApplication.clipboard().clear()
    dialog.search.setText("nothing-like-it")
    assert tree.topLevelItemCount() == 0
    dialog.search.setText("exam")
    tree.setCurrentItem(tree.topLevelItem(0))

    def edit(dialog_):
        if isinstance(dialog_, fg.LoginEditDialog):
            dialog_.username.setText("ivy2")
            dialog_.password.setText("second-pw")
        elif isinstance(dialog_, fg.CardEditDialog):
            dialog_.number.setText("5555 5555 5555 4444")
            dialog_.name.setText("Ivy")
            dialog_.month.setCurrentText("03")
        elif isinstance(dialog_, fg.AddressEditDialog):
            dialog_.fields["name"].setText("Ivy Example")
            dialog_.fields["city"].setText("Townsville")
        dialog_.deleteLater()
        return True
    monkeypatch.setattr(fg, "run_dialog", edit)
    dialog.edit_button.click()
    assert (data.login(entry["id"])["username"], vault.items[("Chrome 2", f"password:{entry['id']}")]) == ("ivy2", "second-pw")
    tree.setCurrentItem(tree.topLevelItem(0))
    dialog.delete_button.click()
    assert data.logins == [] and vault.items == {}

    dialog.show_section("payments")
    dialog.add_card()
    assert [(c["network"], c["last4"], c["month"], c["name"]) for c in data.cards] == [("Mastercard", "4444", "03", "Ivy")]
    dialog.card_list.setCurrentItem(dialog.card_list.topLevelItem(0))
    dialog.delete_card()
    assert data.cards == [] and vault.items == {}

    dialog.show_section("addresses")
    dialog.add_address()
    assert [(a["name"], a["city"]) for a in data.addresses] == [("Ivy Example", "Townsville")]
    dialog.address_list.setCurrentItem(dialog.address_list.topLevelItem(0))
    dialog.edit_address()
    dialog.address_list.setCurrentItem(dialog.address_list.topLevelItem(0))
    dialog.delete_address()
    assert data.addresses == []


def test_switches_turn_offers_and_suggestions_off(fg, win, vault, forms):
    win.settings.set("offer_to_save_passwords", False)
    url = forms.add("/login", LOGIN)
    tab = show(win, url)
    sign_in(tab, "jay", PASSWORD)
    wait_until(lambda: "/welcome" in tab.page.url().toString(), 10, "the signed-in page")
    spin(2.0)
    assert not any(b.isVisible() for b in win.findChildren(fg.AutofillBubble)) and vault.items == {}
    win.settings.set("autofill_addresses", False)
    win.autofill.data.add_address({"name": "Jay", "line1": "1 Road", "city": "Springfield", "zip": "1"})
    target = fg.AutofillTarget(tab.page, None, (), forms.origin, 1, "address", type="city")
    assert win.autofill.suggestions(target) == []


def test_clearing_site_data_keeps_passwords(fg, win, vault, forms):
    win.autofill.data.add_login(forms.origin, "kim", PASSWORD)
    done = []
    win.clear_site_data(None, lambda: done.append(True))
    wait_until(lambda: done, 30, "clearing to finish")
    assert [e["username"] for e in win.autofill.data.logins] == ["kim"] and list(vault.items.values()) == [PASSWORD]


# ── without a keychain ────────────────────────────────────────────────────────────────────
def test_without_keyring_passwords_are_off_and_addresses_still_fill(fg, harness, forms, monkeypatch):
    monkeypatch.setitem(sys.modules, "keyring", None)  # "import keyring" fails, as when it isn't installed
    monkeypatch.setattr(fg, "_secret_store", None)
    win = harness.window()
    store = win.autofill.store
    assert not store.available() and "pip install keyring" in store.problem()
    assert store.get("x") is None and not store.set("x", "y")
    tab = show(win, forms.add("/login", LOGIN))
    sign_in(tab, "lee", PASSWORD)
    note = bubble(fg, win, "no-keychain")
    assert any("pip install keyring" in label.text() for label in note.findChildren(QLabel))
    press(note, "OK")
    assert win.autofill.data.logins == [] and PASSWORD not in secrets_on_disk(harness.dir)
    assert win.autofill.data.add_login(forms.origin, "lee", PASSWORD) is None
    assert win.autofill.data.add_card("4111111111111111") is None

    win.autofill.data.add_address({"name": "Lee Example", "line1": "5 Main St", "city": "Springfield", "state": "NY", "zip": "10001"})
    tab = show(win, forms.add("/checkout", CHECKOUT))
    click(tab, "#street")
    pop = wait_popup(fg, win)
    assert pop.widgets[0].text.text() == "5 Main St"
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.getElementById('state').value", lambda v: v == "NY", what="the address")

    win.show_autofill_settings("passwords")
    dialog = win._dialogs["autofill"]
    assert not dialog.add_password_button.isEnabled() and not dialog.add_card_button.isEnabled()


# ── small pieces ──────────────────────────────────────────────────────────────────────────
def test_card_and_expiry_helpers(fg):
    assert fg.luhn_ok("4111111111111111") and not fg.luhn_ok("4111111111111112") and not fg.luhn_ok("1234")
    assert fg.card_network("378282246310005") == "American Express" and fg.card_network("5555555555554444") == "Mastercard"
    assert fg.parse_expiry(both="12/30") == ("12", "2030") and fg.parse_expiry(both="07 / 2031") == ("07", "2031")
    assert fg.parse_expiry("3", "29") == ("03", "2029") and fg.parse_expiry(both="13/30") == ("", "")
    assert fg.parse_expiry(both="0532") == ("05", "2032")
    values = fg.address_fill_values({"name": "Ada King Lovelace", "state": "California", "country": "US"})
    assert values["given"] == "Ada King" and values["family"] == "Lovelace" and "CA" in values["states"]
    assert "United States" in values["countries"]
