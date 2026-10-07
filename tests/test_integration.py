"""The features working together: the privacy screen over Claude's panel and both sides of a split view, Claude on
the focused side of a split view, Claude and autofill (no suggestions or filling while Claude works, autofilled
passwords and card numbers hidden from it, no pasting), and the API key in the same keychain store as passwords."""
from __future__ import annotations

import json
import threading
from types import SimpleNamespace
from urllib.parse import quote

import keyring
import pytest
from PyQt6.QtCore import QPoint, Qt, QUrl
from PyQt6.QtWidgets import QLabel, QWidget

from helpers import load, poll_js, run_js, spin, wait_until
from test_autofill import LOGIN, PASSWORD, FormServer, MemoryKeyring, click, key, popup, show, wait_popup

INACTIVE, ACTIVE = Qt.ApplicationState.ApplicationInactive, Qt.ApplicationState.ApplicationActive


@pytest.fixture
def vault(fg, monkeypatch):
    ring = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(ring)
    monkeypatch.setattr(fg, "_secret_store", None)  # the app's SecretStore finds this keychain
    yield ring
    keyring.set_keyring(old)


@pytest.fixture
def forms():
    srv = FormServer()
    yield srv
    srv.close()


@pytest.fixture
def win(vault, harness):
    return harness.window()


def data_page(text: str) -> QUrl:
    return QUrl("data:text/html," + quote(f"<!doctype html><title>{text}</title><body style='background:#fff'>"
                                          f"<p>{text}</p><button id=b onclick=\"document.title='clicked {text}'\">"
                                          f"Press {text}</button></body>"))


def two_sides(window):
    """A split view of two loaded pages, "left" and "right"; the right side has the focus."""
    left = window.current_tab()
    right = window.new_tab(None, background=True)
    for tab, text in ((left, "left"), (right, "right")):
        tab.load(data_page(text))
        wait_until(lambda t=tab, x=text: t.pending is None and not t.loading and t.title() == x, 20, f"the {text} page")
    window.focus_tab(left)
    split = window.add_tab_to_split(right)
    assert split is not None and window.visible_tabs() == [left, right] and window.current_tab() is right
    return left, right


def tool(browser, name: str, **args) -> SimpleNamespace:
    """Run one of Claude's browser tools; its answer."""
    box: dict = {}
    browser.run(name, args, lambda content, error, log: box.update(content=content, error=error, log=log))
    wait_until(lambda: "content" in box, 40, f"the {name} tool")
    content = box["content"]
    text = content if isinstance(content, str) else "\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return SimpleNamespace(text=text, error=box["error"], log=box["log"])


class GateStream:
    """A reply that never comes until it's closed: Claude stays at work."""

    def __init__(self) -> None:
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.closed.set()

    def __iter__(self):
        self.closed.wait(60)
        raise RuntimeError("stream closed")

    def close(self) -> None:
        self.closed.set()


class GateClient:
    def __init__(self) -> None:
        self.streams: list[GateStream] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(stream=self._stream))
        self.messages = self.beta.messages

    def _stream(self, **_params) -> GateStream:
        self.streams.append(GateStream())
        return self.streams[-1]


def claude_at_work(window) -> object:
    """Claude's panel with a prompt sent and its reply pending (session.running until stopped)."""
    window.toggle_agent_panel(True)
    panel = window.agent_panel
    client = GateClient()
    panel.session.client_factory = lambda: client
    panel.input.setPlainText("Do something on this page")
    panel.submit()
    wait_until(lambda: panel.session.running and client.streams, 10, "Claude to start")
    assert window.agent_running()
    return panel


# ══════════════════════════════════════════════════════════════════════════════════════════
#  The privacy screen over Claude's panel and a split view
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_privacy_screen_covers_claudes_panel_and_both_sides_of_a_split_view(fg, window, qapp):
    left, right = two_sides(window)
    window.toggle_agent_panel(True)
    panel = window.agent_panel
    spin(0.5)
    assert panel.isVisible() and left.view.isVisible() and right.view.isVisible()
    shield = window.privacy_screen

    def corners() -> list[QPoint]:  # a point just inside each part (away from the logo in the middle)
        return [w.mapTo(window, QPoint(12, 12)) for w in (left.view, right.view, panel)]

    before = window.grab().toImage()
    grey = fg.PrivacyScreen.COLOR
    assert all(before.pixelColor(p).name() != grey for p in corners())
    try:
        qapp.applicationStateChanged.emit(INACTIVE)
        wait_until(lambda: shield.covering and shield.opacity == 1.0, 5, "the window to be covered")
        assert [c for c in window.children() if isinstance(c, QWidget) and c.isVisible()][-1] is shield
        image = window.grab().toImage()
        assert [image.pixelColor(p).name() for p in corners()] == [grey] * 3
        # a panel shown meanwhile stays under it
        window.toggle_agent_panel(False)
        window.toggle_agent_panel(True)
        spin(0.2)
        assert window.grab().toImage().pixelColor(corners()[2]).name() == grey
    finally:
        qapp.applicationStateChanged.emit(ACTIVE)
    assert not shield.covering and not shield.isVisible()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Claude and split view / pinned tabs
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_claude_acts_on_the_focused_side_of_a_split_view(fg, window):
    left, right = two_sides(window)
    browser = fg.AgentBrowser(window)
    read = tool(browser, "read_page")
    assert not read.error and "Title: right" in read.text and "Press right" in read.text

    window.focus_tab(left)  # the other side gets the focus: Claude follows it
    assert window.current_tab() is left and window.visible_tabs() == [left, right]
    read = tool(browser, "read_page")
    assert "Title: left" in read.text and "Press left" in read.text
    n = int(read.text.split('button "Press left"')[0].rsplit("[", 1)[1].split("]")[0])
    clicked = tool(browser, "click", label=n)
    assert not clicked.error
    wait_until(lambda: left.title() == "clicked left", 10, "the click on the left side")
    assert right.title() == "right"  # (not the other side)
    shot = tool(browser, "screenshot")
    assert not shot.error and browser.shot["width"] <= left.view.width() + 2  # just this side

    window.set_pinned(window.new_tab(data_page("pinned"), background=True), True)
    listing = tool(browser, "list_tabs")
    lines = listing.text.splitlines()[1:]
    assert len(lines) == 3 and "(pinned)" in lines[0]
    assert "(current, left side of a split view with [2])" in lines[1] and "(right side of a split view with [1])" in lines[2]
    switched = tool(browser, "switch_tab", index=2)
    assert not switched.error and window.current_tab() is right and window.visible_tabs() == [left, right]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Claude and autofill
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_api_key_lives_in_the_same_keychain_store_as_passwords(fg, win, vault, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    win.toggle_agent_panel(True)
    panel = win.agent_panel
    assert panel.keys.secrets is win.autofill.store is fg.secret_store()
    panel.key_field.setText("sk-ant-shared")
    panel._save_key()
    assert vault.items[("Chrome 2", "anthropic_api_key")] == "sk-ant-shared"
    assert not panel.keys.file.exists() and "keychain" in panel.key_status.text()
    assert panel.keyring_note.isHidden()
    panel._remove_key()
    assert ("Chrome 2", "anthropic_api_key") not in vault.items


def test_no_suggestions_or_filling_while_claude_works(fg, win, vault, forms):
    url = forms.add("/login", LOGIN)
    entry = win.autofill.data.add_login(forms.origin, "alice", PASSWORD)
    tab = show(win, url)
    click(tab, "#user")
    wait_popup(fg, win)
    panel = claude_at_work(win)
    assert not popup(fg, win).isVisible()  # an open list goes when Claude starts
    assert win.autofill.held(tab.page)
    tab.view.setFocus()
    click(tab, "#pass")
    click(tab, "#user")  # (what Claude's clicks look like to the page: real clicks)
    key(tab, Qt.Key.Key_Down)
    spin(1.5)
    assert popup(fg, win) is None or not popup(fg, win).isVisible()
    target = fg.AutofillTarget(tab.page, tab.page.mainFrame(), (), forms.origin, 1, "login", "username")
    win.autofill.choose(target, ("login", entry["id"]))  # (even a pick that got through fills nothing)
    spin(1.0)
    assert run_js(tab.page, "document.querySelector('#pass').value") == ""
    assert win.autofill.filled_secrets([tab.page]) == []

    panel.session.stop()
    wait_until(lambda: not panel.session.running, 10, "Claude to stop")
    assert not win.autofill.held(tab.page)
    click(tab, "#pass")
    click(tab, "#user")
    assert wait_popup(fg, win).widgets[0].text.text() == "alice"  # back to normal


EXPOSING_LOGIN = LOGIN.replace("</form>", """</form><p id=echo></p>
<script>
  // a page that shows the password once it's in: a "show password" switch, and a copy of it in the text
  document.getElementById("pass").addEventListener("input", (e) => {
    e.target.type = "text";
    document.getElementById("echo").textContent = "You entered: " + e.target.value;
  });
</script>""")

EXPOSING_CHECKOUT = """<!doctype html><html><head><title>Checkout</title></head><body style="margin:20px">
<form id="f" method="post" action="/thanks">
  <input id="ccname" autocomplete="cc-name" style="width:220px;height:22px"><br>
  <input id="cc" name="card" autocomplete="cc-number" style="width:220px;height:22px"><br>
  <input id="exp" autocomplete="cc-exp" placeholder="MM/YY" style="width:220px;height:22px"><br>
</form><p id=echo></p>
<script>
  document.getElementById("cc").addEventListener("input", (e) =>
    document.getElementById("echo").textContent = "Paying with " + e.target.value.replace(/(\\d{4})(?=\\d)/g, "$1 "));
</script></body></html>"""


def test_claude_never_sees_autofilled_passwords_or_card_numbers(fg, win, vault, forms):
    win.autofill.data.add_login(forms.origin, "alice", PASSWORD)
    tab = show(win, forms.add("/login", EXPOSING_LOGIN))
    click(tab, "#user")
    wait_popup(fg, win)
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.querySelector('#echo').textContent", lambda v: PASSWORD in (v or ""), what="the page's copy")
    assert run_js(tab.page, "document.querySelector('#pass').type") == "text"  # no longer a password field
    assert win.autofill.filled_secrets([tab.page]) == [PASSWORD]
    browser = fg.AgentBrowser(win)
    read = tool(browser, "read_page")
    assert PASSWORD not in read.text and "You entered: [redacted]" in read.text and "alice" in read.text
    assert 'value=[redacted]' in read.text or 'value="[redacted]"' in read.text

    assert win.autofill.data.add_card("4111111111111111", "Ada Lovelace", "11", "2031") is not None
    tab = show(win, forms.add("/checkout", EXPOSING_CHECKOUT))
    click(tab, "#cc")
    wait_popup(fg, win)
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.querySelector('#echo').textContent", lambda v: "4111 1111" in (v or ""), what="the card")
    read = tool(browser, "read_page")
    assert "4111" not in read.text and "Paying with [redacted]" in read.text and "Ada Lovelace" in read.text


def test_redaction_helper(fg):
    secrets = ['pa"ss\\word 1', "4111111111111111", "abc"]
    text = 'a value="pa\\"ss\\\\word 1" here; Card 4111 1111-1111 1111 and abc stays; pa"ss\\word 1'
    assert fg.agent_redact(text, secrets) == 'a value="[redacted]" here; Card [redacted] and abc stays; [redacted]'
    blocks = [{"type": "text", "text": "x 4111111111111111"}, {"type": "image", "source": {}}]
    assert fg.agent_redact(blocks, secrets) == [{"type": "text", "text": "x [redacted]"}, {"type": "image", "source": {}}]
    assert fg.agent_redact("nothing", []) == "nothing"


def test_claude_cannot_paste(fg, window):
    tab = window.current_tab()
    assert load(tab.page, data_page("paste"))
    browser = fg.AgentBrowser(window)
    for combo in ("Ctrl+V", "Meta+V", "Shift+Insert", "Ctrl+Shift+V"):
        result = tool(browser, "press_key", key=combo)
        assert result.error and "clipboard" in result.text
    assert not tool(browser, "press_key", key="Ctrl+A").error


def test_user_visible_text_says_chrome_2(fg, win):
    win.toggle_agent_panel(True)
    win.show_autofill_settings("passwords")
    spin(0.3)
    dialog = win._dialogs["autofill"]
    texts = [w.text() for root in (win.agent_panel, dialog, win) for w in root.findChildren(QLabel)]
    texts += [a.text() for a in win.findChildren(type(win.act_agent))]
    assert not [t for t in texts if "Foxglove" in t]
    dialog.close()
    assert json.dumps(fg.KEYCHAIN_SERVICE) == '"Chrome 2"' and fg.APP_NAME == "Chrome 2"
