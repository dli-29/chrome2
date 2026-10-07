"""Review round 2 of Chrome 2: Claude's clicks and typing land where they were aimed, no pasting, secrets hidden from
Claude in addresses and screenshots, form state of autofilled pages kept off disk, the suggestion list and same-site
logins, privacy screens on dialogs, other apps' links while Claude works, and a few small fixes."""
from __future__ import annotations

import base64
import json
import os
import re
import stat
import sys
import time
from urllib.parse import quote, quote_plus

import keyring
import pytest
from PyQt6 import sip
from PyQt6.QtCore import QObject, QPoint, QPointF, Qt, QTimer, QUrl, pyqtSlot
from PyQt6.QtGui import QColor, QCursor, QDesktopServices, QEnterEvent, QGuiApplication, QImage
from PyQt6.QtWidgets import QToolButton

from helpers import load, poll_js, run_js, spin, wait_until
from test_autofill import LOGIN, MemoryKeyring, click, key, keys, popup, show, value, wait_popup

INACTIVE, ACTIVE = Qt.ApplicationState.ApplicationInactive, Qt.ApplicationState.ApplicationActive


@pytest.fixture
def vault(fg, monkeypatch):
    ring = MemoryKeyring()
    old = keyring.get_keyring()
    keyring.set_keyring(ring)
    monkeypatch.setattr(fg, "_secret_store", None)
    yield ring
    keyring.set_keyring(old)


def tool(fg, window, name: str, **arguments) -> dict:
    browser = getattr(window, "_test_agent_browser", None) or fg.AgentBrowser(window)
    window._test_agent_browser = browser
    box: dict = {}
    browser.run(name, arguments, lambda content, error=False, log_line="": box.update(c=content, e=error, l=log_line))
    wait_until(lambda: box, 40, f"the {name} tool")
    return box


def label_of(page_text: str, name: str) -> int:
    match = re.search(r'\[(\d+)\] [a-z ]+ "' + re.escape(name) + '"', page_text)
    assert match, f"no element {name!r} in:\n{page_text}"
    return int(match.group(1))


def settled_load(tab, url: str) -> None:
    """Load, and give the fresh page a moment: offscreen, input sent the instant a page appears can be dropped."""
    assert load(tab.page, url)
    spin(1.0)


def fill_secret(window, tab, secret: str) -> None:
    """What autofill records when it fills a password or card number into the tab's page."""
    window.autofill.state(tab.page).filled.append(secret)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-A1: Claude's clicks and typing must land on the element they were aimed at
# ══════════════════════════════════════════════════════════════════════════════════════════
MOVE_ON_HOVER = b"""<!doctype html><html><head><title>Blog</title></head><body style="margin:0">
<button id=more style="position:absolute;left:40px;top:40px;width:160px;height:40px">Read more</button>
<button id=danger aria-hidden=true style="position:absolute;left:-500px;top:40px;width:160px;height:40px;opacity:0.01"
  onclick="document.title='DANGER clicked'">x</button>
<script>
document.getElementById('more').onclick = () => { document.title = 'more clicked'; };
let moved = false;
addEventListener('mousemove', (e) => { if (e.isTrusted && !moved) { moved = true; document.getElementById('danger').style.left = '40px'; } }, true);
</script></body></html>"""

VICTIM = b"""<!doctype html><html><body style="margin:0">
<button style="width:100%;height:100vh" onclick="fetch('/victim-clicked')">Confirm transfer</button></body></html>"""


def blog_with_frame(victim_url: str) -> bytes:
    return f"""<!doctype html><html><head><title>Blog</title></head><body style="margin:0">
<div style="height:1500px">Long article</div>
<button id=next style="width:200px;height:50px">Next page</button>
<div style="height:1500px"></div>
<iframe id=f src="{victim_url}" style="position:absolute;left:-2000px;top:0;width:300px;height:120px;border:0"></iframe>
<script>
document.getElementById('next').onclick = () => {{ document.title = 'next clicked'; }};
addEventListener('scroll', () => {{
  const r = document.getElementById('next').getBoundingClientRect(), f = document.getElementById('f');
  f.style.left = (r.left + scrollX - 50) + 'px'; f.style.top = (r.top + scrollY - 35) + 'px';
}});
</script></body></html>""".encode()


def test_click_refuses_when_something_moves_over_the_target(fg, window, server):
    server.add("/r2-hover", MOVE_ON_HOVER, "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/r2-hover"))
    page_text = tool(fg, window, "read_page")["c"]
    result = tool(fg, window, "click", label=label_of(page_text, "Read more"))
    spin(0.5)
    assert run_js(tab.page, "document.title") != "DANGER clicked"
    assert result["e"] and "Read more" in result["c"] and "Clicked [" not in result["c"][:9]


def test_click_into_another_sites_frame_is_caught(fg, window, server):
    victim = f"http://localhost:{server.port}/r2-victim"
    server.add("/r2-victim", VICTIM, "text/html; charset=utf-8")
    server.add("/r2-blog", blog_with_frame(victim), "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/r2-blog"))
    wait_until(lambda: tab.page.mainFrame().children(), 10, "the frame")
    spin(0.5)
    page_text = tool(fg, window, "read_page")["c"]
    before = len(server.requests)
    result = tool(fg, window, "click", label=label_of(page_text, "Next page"))
    spin(0.6)
    assert "/r2-victim-clicked" not in server.requests[before:] and "/victim-clicked" not in server.requests[before:]
    assert result["e"], result["c"]


def test_click_that_lands_elsewhere_at_the_last_moment_is_reported(fg, window, server, monkeypatch):
    """The page moves another site's frame over the target between the last check and the press."""
    victim = f"http://localhost:{server.port}/r2-victim"
    server.add("/r2-victim", VICTIM, "text/html; charset=utf-8")
    server.add("/r2-late", f"""<!doctype html><html><head><title>Late</title></head><body style="margin:0">
<button id=next style="position:absolute;left:40px;top:40px;width:200px;height:50px">Next page</button>
<iframe id=f src="{victim}" style="position:absolute;left:-2000px;top:0;width:300px;height:120px;border:0"></iframe>
<script>window.cover = () => {{ const f = document.getElementById('f'); f.style.left = '0px'; f.style.top = '0px'; return 1; }};</script>
</body></html>""".encode(), "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/r2-late"))
    wait_until(lambda: tab.page.mainFrame().children(), 10, "the frame")
    spin(0.5)
    original = fg.AgentBrowser._mouse

    def late(self, tab_, x, y, press=True, move=True):
        if press and not move:  # (the press comes once the page has moved its frame)
            tab_.page.runJavaScript("cover()", 0, lambda _r: QTimer.singleShot(150, lambda: original(self, tab_, x, y, press, move)))
            return
        original(self, tab_, x, y, press, move)
    monkeypatch.setattr(fg.AgentBrowser, "_mouse", late)
    page_text = tool(fg, window, "read_page")["c"]
    result = tool(fg, window, "click", label=label_of(page_text, "Next page"))
    assert result["e"] and "trick" in result["c"], result["c"]
    assert run_js(tab.page, "document.title") != "next clicked"


FOCUS_THIEF = b"""<!doctype html><html><head><title>Search</title></head><body>
<form id=a onsubmit="document.title='a submitted'; return false"><input id=q aria-label="Search"></form>
<form id=b onsubmit="document.title='b submitted'; return false"><input id=evil aria-label="Other"></form>
<input id=t aria-label="Steal on focus">
<script>
document.getElementById('t').addEventListener('focus', () => setTimeout(() => document.getElementById('evil').focus(), 0));
let first = true;
document.getElementById('q').addEventListener('keydown', (e) => {
  if (e.isTrusted && first) { first = false; setTimeout(() => document.getElementById('evil').focus(), 0); }
});
</script></body></html>"""


def test_typing_stops_when_the_page_moves_the_focus(fg, window, server):
    server.add("/r2-type", FOCUS_THIEF, "text/html; charset=utf-8")
    tab = window.current_tab()
    settled_load(tab, server.url("/r2-type"))
    page_text = tool(fg, window, "read_page")["c"]
    # the focus is taken away right after the field gets it: nothing is typed anywhere
    result = tool(fg, window, "type_text", label=label_of(page_text, "Steal on focus"), text="hello")
    assert result["e"] and "Nothing was typed" in result["c"], result["c"]
    assert run_js(tab.page, "document.getElementById('evil').value + document.getElementById('t').value") == ""
    # ... or as typing starts: Enter isn't pressed into whatever has the focus now
    page_text = tool(fg, window, "read_page")["c"]
    result = tool(fg, window, "type_text", label=label_of(page_text, "Search"), text="cats", submit=True)
    spin(0.5)
    assert result["e"] and "Didn't press Enter" in result["c"], result["c"]
    assert run_js(tab.page, "document.title") == "Search"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-A2 / R1-3: no pasting, whatever the key is called
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_the_paste_key_is_refused(fg, window, server):
    server.add("/r2-paste", b"<!doctype html><title>Paste</title><textarea id=t aria-label=Notes></textarea>",
               "text/html; charset=utf-8")
    tab = window.current_tab()
    settled_load(tab, server.url("/r2-paste"))
    QGuiApplication.clipboard().setText("CLIPBOARD-SECRET")
    page_text = tool(fg, window, "read_page")["c"]
    clicked = tool(fg, window, "click", label=label_of(page_text, "Notes"))
    assert not clicked["e"], clicked["c"]
    for spec in ("Paste", "Ctrl+V", "Shift+Insert", "Ctrl+Shift+V", "Meta+Shift+V"):
        result = tool(fg, window, "press_key", key=spec)
        assert result["e"] and "Pasting isn't available" in result["c"], spec
    spin(0.3)
    assert run_js(tab.page, "document.getElementById('t').value") == ""
    for spec in ("Ctrl+A", "Shift+V", "v", "Enter"):  # (not pastes)
        assert not fg.agent_pastes(*fg.agent_key(spec)), spec
    QGuiApplication.clipboard().clear()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-A3 / R1-2: filled secrets in addresses, and the browser-state line
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_url_encoded_secrets_are_redacted(fg):
    for secret in ("p@ss word&99", "Pa55 word-secret!&x", "ünï cødé!"):
        for encoded in (quote(secret, safe=""), quote_plus(secret), re.sub("%..", lambda m: m.group().lower(), quote(secret)),
                        bytes(QUrl.toPercentEncoding(secret)).decode()):
            text = f"URL: http://x/welcome?username=alice&password={encoded}&next=1"
            assert fg.agent_redact(text, [secret]) == "URL: http://x/welcome?username=alice&password=[redacted]&next=1"


def test_secrets_in_the_address_never_reach_claude(fg, window, server):
    from test_agent import FakeClient, message, open_panel, results_of, text, use
    secret = "p@ss word&99"
    server.add("/welcome", b"<!doctype html><title>Welcome</title><p>Hi</p>", "text/html; charset=utf-8")
    tab = window.current_tab()
    url = server.url("/welcome?username=alice&password=" + quote_plus(secret))
    assert load(tab.page, url)
    fill_secret(window, tab, secret)
    client = FakeClient(message(use("read_page"), use("list_tabs")), message(text("ok"), stop="end_turn"))
    panel = open_panel(window, client)
    panel.input.setPlainText("What's on this page?")
    panel.submit()
    wait_until(lambda: not panel.session.running, 60, "Claude to finish")
    everything = json.dumps(client.calls[0]["messages"]) + json.dumps([results_of(c) for c in client.calls[1:]])
    assert quote_plus(secret) not in everything and "p%40ss" not in everything.lower()
    assert "password=[redacted]" in client.calls[0]["messages"][0]["content"][1]["text"]
    assert "password=[redacted]" in everything


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-A4 / R1-1: screenshots paint over filled secrets
# ══════════════════════════════════════════════════════════════════════════════════════════
CARD = "4111111111111111"
SHOWING = f"""<!doctype html><html><head><title>Checkout</title></head><body style="margin:0;background:#fff">
<input id=cc aria-label="Card" value="{CARD}" style="position:absolute;left:20px;top:20px;width:260px;height:30px;font-size:20px">
<p id=echo style="position:absolute;left:20px;top:80px;margin:0;font-size:20px">Your password is Tr4p-Secret!</p>
</body></html>""".encode()


def shot_image(result) -> QImage:
    assert not result["e"], result["c"]
    image = QImage.fromData(base64.b64decode(result["c"][1]["source"]["data"]), "PNG")
    assert not image.isNull()
    return image


def test_screenshots_cover_filled_secrets(fg, window, server):
    server.add("/r2-shot", SHOWING, "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/r2-shot"))
    fill_secret(window, tab, CARD)
    fill_secret(window, tab, "Tr4p-Secret!")
    window._test_agent_browser = None
    image = shot_image(tool(fg, window, "screenshot"))
    factor = image.width() / tab.view.width()
    mask = QColor("#3c4043")
    for x, y in ((40, 35), (150, 35), (260, 35), (60, 92), (220, 92)):  # the card field and the echoed password
        assert image.pixelColor(round(x * factor), round(y * factor)) == mask, (x, y)
    assert image.pixelColor(round(400 * factor), round(200 * factor)) != mask  # (the rest of the page shows)


def test_screenshots_cover_another_sites_frame_that_shows_a_secret(fg, window, server):
    server.add("/r2-pay", f"""<!doctype html><body style="margin:0"><input value="{CARD}" style="width:250px"></body>""".encode(),
               "text/html; charset=utf-8")
    server.add("/r2-shop", f"""<!doctype html><html><head><title>Shop</title></head><body style="margin:0;background:#fff">
<iframe src="http://localhost:{server.port}/r2-pay" style="position:absolute;left:20px;top:20px;width:300px;height:60px;border:0"></iframe>
<p style="position:absolute;left:20px;top:200px">Order total</p></body></html>""".encode(), "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/r2-shop"))
    wait_until(lambda: tab.page.mainFrame().children(), 10, "the frame")
    spin(0.5)
    fill_secret(window, tab, CARD)
    image = shot_image(tool(fg, window, "screenshot"))
    factor = image.width() / tab.view.width()
    mask = QColor("#3c4043")
    assert image.pixelColor(round(300 * factor), round(70 * factor)) == mask  # the whole frame
    # a secret split over several text nodes can't be covered: no screenshot at all
    run_js(tab.page, f"document.body.insertAdjacentHTML('beforeend', '<p><b>{CARD[:8]}</b>{CARD[8:]}</p>'); 1")
    result = tool(fg, window, "screenshot")
    assert result["e"] and "No screenshot" in result["c"]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-F1: no form state of autofilled pages in session.json (which is private)
# ══════════════════════════════════════════════════════════════════════════════════════════
CHECKOUT = f"""<!doctype html><title>Checkout</title><form action="/r2-thanks">
<input id=cc name=cardnumber autocomplete=cc-number><button>Pay</button></form>
<script>document.getElementById('cc').value = '{CARD}';</script>""".encode()


def test_session_keeps_no_form_state_of_pages_with_filled_secrets(fg, window, server):
    server.add("/r2-checkout", CHECKOUT, "text/html; charset=utf-8")
    server.add("/r2-thanks", b"<!doctype html><title>Thanks</title>", "text/html; charset=utf-8")
    plain, filled = window.current_tab(), window.new_tab(QUrl("about:blank"))
    for tab in (plain, filled):
        assert load(tab.page, server.url("/r2-checkout"))
        run_js(tab.page, f"document.getElementById('cc').value = '{CARD}'; 1")
    fill_secret(window, filled, CARD)
    for tab in (plain, filled):
        assert load(tab.page, server.url("/r2-thanks"))
    spin(0.5)
    blob = base64.b64decode(plain.session_entry()["history"])
    assert CARD.encode("utf-16-le") in blob  # (the form state that would be saved: the mechanism is real)
    entry = filled.session_entry()
    assert "history" not in entry and entry["url"].endswith("/r2-thanks")
    window.save_session()
    saved = window.session_path.read_text(encoding="utf-8")
    entries = json.loads(saved)["tabs"]
    assert not any("history" in e for e in entries if e["uid"] == filled.uid)
    assert stat.S_IMODE(os.stat(window.session_path).st_mode) == 0o600
    window.close_tab(filled)  # ... nor among the closed tabs
    spin(0.5)
    assert window.closed_tabs and "history" not in window.closed_tabs[-1], window.closed_tabs


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-F2: the suggestion list can't be popped up under a resting pointer and picked by the page's next Enter
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_suggestion_list_needs_the_user_to_go_to_the_field(fg, harness, vault, server):
    win = harness.window()
    server.add("/r2-login", (LOGIN + """<script>
document.addEventListener('keydown', (e) => { if (e.key === 'a') setTimeout(() => document.getElementById('user').focus(), 50); });
</script>""").encode(), "text/html; charset=utf-8")
    origin = f"http://127.0.0.1:{server.port}"
    win.autofill.data.add_login(origin, "alice", "Trap-Pa55!")
    tab = show(win, server.url("/r2-login"))
    run_js(tab.page, "document.activeElement.blur(); 1")
    key(tab, Qt.Key.Key_A)  # a trusted key press somewhere else: the page focuses the field on it
    wait_until(lambda: run_js(tab.page, "document.activeElement.id") == "user", 5, "the page to move the focus")
    spin(1.0)
    pop = popup(fg, win)
    assert pop is None or not pop.isVisible()
    # the arrow key (the user, at the field) opens it
    key(tab, Qt.Key.Key_Down)
    pop = wait_popup(fg, win)
    row = pop.widgets[0]
    point = QPointF(row.rect().center())
    global_point = QPointF(row.mapToGlobal(row.rect().center()))
    QCursor.setPos(pop.rest)  # the pointer hasn't moved since the list appeared: a hover isn't a choice
    row.enterEvent(QEnterEvent(point, point, global_point))
    assert pop.selected == -1
    pop.shown_at = time.monotonic()  # (just appeared)
    QCursor.setPos(pop.rest + QPoint(3, 3))  # moved onto the row: selected, but an Enter this soon isn't taken
    row.enterEvent(QEnterEvent(point, point, global_point))
    assert pop.selected == 0 and pop.by_mouse
    key(tab, Qt.Key.Key_Return)
    spin(0.3)
    assert "Trap-Pa55!" not in fg.Autofill.filled_secrets([tab.page])
    QCursor.setPos(QPoint(0, 0))


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-F3: logins aren't offered across a public suffix the list doesn't know
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_same_site_logins_only_from_the_sites_own_host(fg):
    same_site = fg.AutofillData.same_site
    for victim, evil in (("alice.me.uk", "mallory.me.uk"),
                         ("victim.s3-website-us-east-1.amazonaws.com", "evil.s3-website-us-east-1.amazonaws.com"),
                         ("victim.ts.net", "evil.ts.net"), ("victim.synology.me", "evil.synology.me"),
                         ("victim.ddns.net", "evil.ddns.net"), ("victim.unknownhost.example", "evil.unknownhost.example"),
                         ("login.example.com", "evil.example.com")):
        assert not same_site(f"https://{victim}", f"https://{evil}"), (victim, evil)
    assert same_site("https://www.example.com", "https://login.example.com")
    assert same_site("https://example.co.uk", "https://shop.example.co.uk")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-P1 / R1-4 / SEC-F4: dialogs get privacy screens; shown and copied passwords don't stay
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_dialogs_are_covered_and_shown_passwords_hidden_in_the_background(fg, harness, vault, qapp, monkeypatch):
    win = harness.window()
    win.autofill.data.add_login("https://example.com", "alice", "hunter2-secret")
    win.show_autofill_settings()
    dialog = win._dialogs["autofill"]
    spin(0.2)
    dialog.password_list.setCurrentItem(dialog.password_list.topLevelItem(0))
    dialog.toggle_password()
    assert "hunter2-secret" in dialog.revealed.values()
    monkeypatch.setattr(fg.AutofillDialog, "CLIPBOARD_MS", 300)
    dialog.copy_password()
    assert QGuiApplication.clipboard().text() == "hunter2-secret"
    try:
        qapp.applicationStateChanged.emit(INACTIVE)
        screen = dialog.findChild(fg.PrivacyScreen, options=Qt.FindChildOption.FindDirectChildrenOnly)
        assert screen is not None
        wait_until(lambda: screen.covering and screen.isVisible(), 5, "the dialog to be covered")
        assert screen.geometry() == dialog.rect()
        assert not dialog.revealed
        texts = [dialog.password_list.topLevelItem(0).text(c) for c in range(dialog.password_list.columnCount())]
        assert "hunter2-secret" not in texts
        # other dialogs too (History, Settings...)
        win.show_history()
        history = win._dialogs["history"]
        history_screen = history.findChild(fg.PrivacyScreen, options=Qt.FindChildOption.FindDirectChildrenOnly)
        assert history_screen is not None
    finally:
        qapp.applicationStateChanged.emit(ACTIVE)
    assert not screen.covering
    wait_until(lambda: QGuiApplication.clipboard().text() == "", 5, "the copied password to be cleared")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  SEC-A5: Claude's clicks don't open other apps
# ══════════════════════════════════════════════════════════════════════════════════════════
class SchemeCatcher(QObject):
    def __init__(self):
        super().__init__()
        self.urls: list[str] = []

    @pyqtSlot(QUrl)
    def handle(self, url: QUrl) -> None:
        self.urls.append(url.toString())


def test_claudes_clicks_dont_open_other_apps(fg, window, server):
    catcher = SchemeCatcher()
    QDesktopServices.setUrlHandler("fgtest-ext", catcher, "handle")
    try:
        server.add("/r2-ext", b"""<!doctype html><title>Ext</title>
<button onclick="location.href='fgtest-ext://run?cmd=open'">Continue</button>""", "text/html; charset=utf-8")
        tab = window.current_tab()
        assert load(tab.page, server.url("/r2-ext"))
        page_text = tool(fg, window, "read_page")["c"]
        tool(fg, window, "click", label=label_of(page_text, "Continue"))
        spin(1.5)
        assert catcher.urls == []
        window._test_agent_browser.release()  # (once Claude is done, the user's own clicks work as before)
        policy = tab.page.settings().unknownUrlSchemePolicy()
        assert policy == window.profile.settings().unknownUrlSchemePolicy()
    finally:
        QDesktopServices.unsetUrlHandler("fgtest-ext")


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Small fixes: SEC-N1, SEC-I1, R1-5, R1-6, R1-7
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_clearing_history_forgets_hidden_new_tab_sites(window):
    window.settings.set("ntp_hidden", ["https://very-private.example/"])
    window.clear_history_traces()
    assert window.settings.get("ntp_hidden") == []


def parse_exec(value: str) -> list[str]:
    """A .desktop Exec value as a launcher reads it (desktop entry spec): string escapes, then quoting, then field codes."""
    unescaped, i = "", 0
    while i < len(value):
        if value[i] == "\\":
            nxt = value[i + 1]
            unescaped += {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}[nxt]  # (anything else: invalid)
            i += 2
        else:
            unescaped += value[i]
            i += 1
    args, current, quoted, i = [], None, False, 0
    while i < len(unescaped):
        c = unescaped[i]
        if quoted:
            if c == "\\":
                assert unescaped[i + 1] in '"`$\\', f"bad escape in {unescaped!r}"
                current += unescaped[i + 1]
                i += 1
            elif c == '"':
                quoted = False
            else:
                current += c
        elif c == '"':
            quoted, current = True, current or ""
        elif c == " ":
            if current is not None:
                args.append(current)
            current = None
        else:
            current = (current or "") + c
        i += 1
    if current is not None:
        args.append(current)
    out = []
    for arg in args:
        if arg in ("%U", "%u", "%F", "%f"):
            continue
        out.append(re.sub(r"%(.)", lambda m: "%" if m.group(1) == "%" else "", arg))
    return out


def test_linux_launcher_quotes_any_path(fg, tmp_path, monkeypatch, qapp):
    monkeypatch.setattr(fg, "IS_MAC", False)
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    for python in ("/opt/a 100%f dir/py", '/opt/q"x/py', "/opt/b\\s$HOME`id`/py"):
        monkeypatch.setattr(sys, "executable", python)
        assert fg.install_app(home=tmp_path) == 0
        desktop = (tmp_path / "share" / "applications" / "chrome-2.desktop").read_text()
        line = next(x for x in desktop.splitlines() if x.startswith("Exec="))
        argv = parse_exec(line[5:])
        assert argv[-2:] == [python, os.path.abspath(fg.__file__)], (line, argv)
    with pytest.raises(ValueError):
        fg.desktop_exec(["/opt/a\nb/py"])


def test_hidden_address_bar_actions_show_no_button(window):
    bar = window.url_bar
    spin(0.2)
    for action in (bar.autofill_action, bar.zoom_action):
        assert not action.isVisible()
        buttons = [b for b in bar.findChildren(QToolButton) if b.defaultAction() is action]
        assert buttons and not any(b.isVisible() for b in buttons)


class FullscreenRequest:
    def __init__(self, on: bool):
        self.on = on

    def toggleOn(self) -> bool:
        return self.on

    def accept(self) -> None:
        pass

    def reject(self) -> None:
        pass


def test_page_full_screen_hides_claudes_panel(window):
    window.toggle_agent_panel(True)
    panel = window.agent_panel
    assert panel.isVisible()
    tab = window.current_tab()
    window._on_fullscreen_request(tab, FullscreenRequest(True))
    assert not panel.isVisible()
    window._on_fullscreen_request(tab, FullscreenRequest(False))
    assert panel.isVisible()
    window.toggle_agent_panel(False)  # (closed before: stays closed)
    window._on_fullscreen_request(tab, FullscreenRequest(True))
    window._on_fullscreen_request(tab, FullscreenRequest(False))
    assert not panel.isVisible()


def test_new_tab_page_can_restore_default_shortcuts(fg, window, harness):
    harness.profile.removeUrlSchemeHandler(harness.pages_handler)
    service = fg.NewTabPage(window.settings, window.history, window.favicons, harness.profile)
    harness.profile.installUrlSchemeHandler(b"foxglove", fg.InternalPages(service, harness.profile))
    settings = window.settings
    settings.set("ntp_shortcuts", [{"title": "A", "url": "https://a.example/"}, {"title": "B", "url": "https://b.example/"}])
    settings.set("ntp_shortcuts_edited", True)
    tab = window.current_tab()
    assert load(tab.page, "foxglove://newtab")
    page = tab.page
    poll_js(page, "document.querySelectorAll('#tiles a.tile').length === 2")
    run_js(page, "document.querySelectorAll('#tiles a.tile .tile-action')[1].click(); 1")
    run_js(page, "document.getElementById('menu-remove').click(); 1")
    wait_until(lambda: len(settings.get("ntp_shortcuts")) == 1)
    assert poll_js(page, "!document.getElementById('toast-restore').hidden")
    assert run_js(page, "document.getElementById('toast-restore').textContent") == "Restore default shortcuts"
    run_js(page, "document.getElementById('toast-restore').click(); 1")
    wait_until(lambda: settings.get("ntp_shortcuts_edited") is False)
    assert settings.get("ntp_shortcuts") == []


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Review round 3
# ══════════════════════════════════════════════════════════════════════════════════════════
def frame_ready(tab) -> None:
    wait_until(lambda: tab.page.mainFrame().children(), 10, "the frame")
    spin(0.5)


def canvas_page(victim_url: str, trap: bool) -> bytes:
    script = """addEventListener('mousemove', (e) => {
  if (!e.isTrusted) return;
  const f = document.getElementById('f');
  f.style.left = (e.clientX - 80) + 'px'; f.style.top = (e.clientY - 40) + 'px';
}, true);""" if trap else ""
    return f"""<!doctype html><html><head><title>Map</title></head><body style="margin:0">
<canvas id=c width=400 height=300 style="position:absolute;left:0;top:0;background:#cde"></canvas>
<iframe id=f src="{victim_url}" style="position:absolute;left:-2000px;top:0;width:160px;height:80px;border:0;opacity:0.02;z-index:5"></iframe>
<script>document.getElementById('c').onclick = () => {{ document.title = 'canvas clicked'; }};
{script}</script></body></html>""".encode()


# SEC-R3-1: click_at checks what's at the point once the mouse is there, and where the press went
def test_click_at_refuses_frame_moved_under_the_pointer(fg, window, server):
    server.add("/r3-victim", VICTIM.replace(b"/victim-clicked", b"/r3-victim-clicked"), "text/html; charset=utf-8")
    victim = f"http://localhost:{server.port}/r3-victim"
    server.add("/r3-map-trap", canvas_page(victim, True), "text/html; charset=utf-8")
    server.add("/r3-map", canvas_page(victim, False), "text/html; charset=utf-8")
    tab = window.current_tab()
    settled_load(tab, server.url("/r3-map-trap"))
    frame_ready(tab)
    shot_image(tool(fg, window, "screenshot"))
    factor = window._test_agent_browser.shot["factor"]
    before = len(server.requests)
    result = tool(fg, window, "click_at", x=200 * factor, y=150 * factor)
    spin(0.8)
    assert "/r3-victim-clicked" not in server.requests[before:]
    assert result["e"] and "Nothing was clicked" in result["c"], result["c"]
    assert run_js(tab.page, "document.title") != "canvas clicked"
    # (without the trap: the canvas gets the click; the near-invisible frame is off to the side)
    settled_load(tab, server.url("/r3-map"))
    frame_ready(tab)
    shot_image(tool(fg, window, "screenshot"))
    result = tool(fg, window, "click_at", x=200 * factor, y=150 * factor)
    assert not result["e"], result["c"]
    assert run_js(tab.page, "document.title") == "canvas clicked"
    # ... but a near-invisible frame of another site over it isn't clicked into
    run_js(tab.page, "const f = document.getElementById('f'); f.style.left = '120px'; f.style.top = '110px'; 1")
    shot_image(tool(fg, window, "screenshot"))
    before = len(server.requests)
    result = tool(fg, window, "click_at", x=200 * factor, y=150 * factor)
    spin(0.8)
    assert result["e"] and "invisible" in result["c"], result["c"]
    assert "/r3-victim-clicked" not in server.requests[before:]


# SEC-R3-2: a press that went unseen into another site's frame is reported, not silently retried
def test_press_into_foreign_frame_is_not_retried(fg, window, server, monkeypatch):
    server.add("/r3-victim", VICTIM.replace(b"/victim-clicked", b"/r3-victim-clicked"), "text/html; charset=utf-8")
    victim = f"http://localhost:{server.port}/r3-victim"
    server.add("/r3-late", f"""<!doctype html><html><head><title>Late</title></head><body style="margin:0">
<button id=next style="position:absolute;left:40px;top:40px;width:200px;height:50px">Next page</button>
<iframe id=f src="{victim}" style="position:absolute;left:-2000px;top:0;width:300px;height:120px;border:0"></iframe>
<script>
document.getElementById('next').onclick = () => {{ document.title = 'next clicked'; }};
window.cover = () => {{ const f = document.getElementById('f'); f.style.left = '0px'; f.style.top = '0px'; return 1; }};
addEventListener('blur', () => setTimeout(() => {{ const f = document.getElementById('f'); if (f) f.remove(); }}, 150));
</script></body></html>""".encode(), "text/html; charset=utf-8")
    tab = window.current_tab()
    settled_load(tab, server.url("/r3-late"))
    frame_ready(tab)
    original, presses = fg.AgentBrowser._mouse, []

    def late(self, tab_, x, y, press=True, move=True):
        if press and not move:
            presses.append((x, y))
            if len(presses) == 1:  # (the first press comes once the page has moved its frame there)
                tab_.page.runJavaScript("cover()", 0, lambda _r: QTimer.singleShot(150, lambda: original(self, tab_, x, y, press, move)))
                return
        original(self, tab_, x, y, press, move)
    monkeypatch.setattr(fg.AgentBrowser, "_mouse", late)
    page_text = tool(fg, window, "read_page")["c"]
    result = tool(fg, window, "click", label=label_of(page_text, "Next page"))
    spin(0.5)
    assert result["e"] and "didn't reach" in result["c"], result["c"]
    assert len(presses) == 1 and run_js(tab.page, "document.title") != "next clicked"


# SEC-R3-3: autofill never fills fields the user can't see
NEWSLETTER = """<!doctype html><html><head><title>Newsletter</title></head><body style="margin:20px">
<form id=f><input id=name autocomplete=name style="width:220px;height:24px"><br>
<input id=email type=email autocomplete=email style="width:220px;height:24px"><br>
<div style="display:none"><input id=street autocomplete=street-address><input id=city autocomplete=address-level2>
  <input id=zip autocomplete=postal-code></div>
<input id=tel autocomplete=tel style="visibility:hidden">
<input id=org autocomplete=organization style="opacity:0">
<input id=far autocomplete=address-line2 style="position:absolute;left:-5000px">
<button>Subscribe</button></form></body></html>"""


def test_hidden_address_fields_are_left_alone(fg, vault, harness, server):
    win = harness.window()
    win.autofill.data.add_address({"name": "Ada Lovelace", "line1": "12 Analytical Way", "line2": "Floor 3", "city": "Springfield",
                                   "state": "CA", "zip": "90210", "phone": "+1 555 0100", "email": "ada@example.com",
                                   "organization": "Engines Ltd"})
    server.add("/r3-news", NEWSLETTER.encode(), "text/html; charset=utf-8")
    tab = show(win, server.url("/r3-news"))
    click(tab, "#name")
    assert wait_popup(fg, win).widgets[0].text.text() == "Ada Lovelace"
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.getElementById('name').value", lambda v: v == "Ada Lovelace", what="the name")
    spin(0.3)
    assert value(tab, "#email") == "ada@example.com"
    hidden = json.loads(run_js(tab.page, "JSON.stringify(['street','city','zip','tel','org','far'].map(id => document.getElementById(id).value))"))
    assert hidden == [""] * 6, hidden


def test_hidden_card_number_is_left_alone(fg, vault, harness, server):
    win = harness.window()
    win.autofill.data.add_card("4111111111111111", "Ada Lovelace", "11", "2031", "")
    server.add("/r3-pay", b"""<!doctype html><html><head><title>Pay</title></head><body style="margin:20px">
<form><input id=ccname autocomplete=cc-name style="width:220px;height:24px">
<div style="display:none"><input id=cc autocomplete=cc-number></div><button>Pay</button></form></body></html>""",
               "text/html; charset=utf-8")
    tab = show(win, server.url("/r3-pay"))
    click(tab, "#ccname")
    wait_popup(fg, win)
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.getElementById('ccname').value", lambda v: v == "Ada Lovelace", what="the name on the card")
    spin(0.3)
    assert value(tab, "#cc") == ""


# SEC-R3-4: what autofill filled anywhere is hidden from Claude everywhere
def test_secret_filled_in_another_window_is_redacted(fg, vault, harness, server):
    secret = "Hunter2-Very-Secret"
    first = harness.window()
    server.add("/r3-login", LOGIN.encode(), "text/html; charset=utf-8")
    first.autofill.data.add_login(f"http://127.0.0.1:{server.port}", "alice", secret)
    tab = show(first, server.url("/r3-login"))
    click(tab, "#user")
    wait_popup(fg, first)
    key(tab, Qt.Key.Key_Down)
    key(tab, Qt.Key.Key_Return)
    poll_js(tab.page, "document.getElementById('pass').value", lambda v: v == secret, what="the password")
    first.close_tab(tab)  # (and that tab is gone now)
    spin(0.3)
    second = harness.window()
    server.add("/r3-echo", f"<!doctype html><title>Profile</title><p>Your password: {secret}</p>".encode(),
               "text/html; charset=utf-8")
    settled_load(second.current_tab(), server.url("/r3-echo"))
    result = tool(fg, second, "read_page")
    assert secret not in result["c"] and "[redacted]" in result["c"], result["c"]


# SEC-R3-5: a typed password stays [redacted] after "show password"
def test_shown_password_stays_hidden(fg, window, server):
    server.add("/r3-show", b"""<!doctype html><html><head><title>Sign in</title></head><body style="margin:20px">
<input id=u aria-label="User" style="width:200px;height:24px"><br>
<input id=p type=password aria-label="Password" style="width:200px;height:24px">
<button id=show onclick="const p = document.getElementById('p'); p.type = p.type === 'password' ? 'text' : 'password'">Show</button>
</body></html>""", "text/html; charset=utf-8")
    tab = window.current_tab()
    settled_load(tab, server.url("/r3-show"))
    window.activateWindow()
    click(tab, "#p")
    keys(tab, "TypedByUser-Secret9")
    poll_js(tab.page, "document.getElementById('p').value", lambda v: v == "TypedByUser-Secret9", what="the typing")
    click(tab, "#show")
    poll_js(tab.page, "document.getElementById('p').type", lambda v: v == "text", what="the password to show")
    result = tool(fg, window, "read_page")
    assert "TypedByUser-Secret9" not in result["c"] and "value=[redacted]" in result["c"], result["c"]
    # ... and a screenshot covers it (no password was filled in by autofill here)
    image = shot_image(tool(fg, window, "screenshot"))
    factor = window._test_agent_browser.shot["factor"]
    r = json.loads(run_js(tab.page, "JSON.stringify(document.getElementById('p').getBoundingClientRect())"))
    mask = QColor("#3c4043")
    for x in (r["left"] + 10, r["left"] + r["width"] / 2):
        assert image.pixelColor(round(x * factor), round((r["top"] + r["height"] / 2) * factor)) == mask, x
    r = json.loads(run_js(tab.page, "JSON.stringify(document.getElementById('u').getBoundingClientRect())"))
    assert image.pixelColor(round((r["left"] + r["width"] / 2) * factor), round((r["top"] + r["height"] / 2) * factor)) != mask


# F1: Stop stops a tool call under way: no more keys or clicks reach the page
STOP_PAGE = b"""<!doctype html><html><head><title>Message</title></head><body style="margin:20px">
<form onsubmit="fetch('/r3-submitted'); document.title = 'submitted'; return false">
<input id=name aria-label="Name" style="width:200px;height:24px"></form>
<button id=send style="width:120px;height:30px" onclick="fetch('/r3-sent'); document.title = 'sent'">Send</button>
<script>window.keys = []; addEventListener('keydown', (e) => { if (e.isTrusted) keys.push(e.key); }, true);</script>
</body></html>"""


def test_stop_cancels_the_tool_call_under_way(fg, window, server):
    from test_agent import FakeClient, message, open_panel, use
    server.add("/r3-stop", STOP_PAGE, "text/html; charset=utf-8")
    tab = window.current_tab()
    settled_load(tab, server.url("/r3-stop"))
    page_text = tool(fg, window, "read_page")["c"]
    name, send = label_of(page_text, "Name"), label_of(page_text, "Send")
    for doing, step in (("Typing", use("type_text", label=name, text="hello", submit=True)), ("Clicking", use("click", label=send))):
        client = FakeClient(message(step))
        panel = open_panel(window, client)
        stop = lambda kind, text, doing=doing: QTimer.singleShot(0, panel.session.stop) if (kind, text) == ("doing", doing) else None
        panel.session.transcript.connect(stop)
        before = len(server.requests)
        panel.input.setPlainText("Go")
        panel.submit()
        wait_until(lambda: not panel.session.running, 30, "Claude to stop")
        spin(2.5)
        panel.session.transcript.disconnect(stop)
        assert run_js(tab.page, "document.getElementById('name').value") == ""
        assert run_js(tab.page, "keys.length") == 0
        assert run_js(tab.page, "document.title") == "Message"
        assert not {"/r3-submitted", "/r3-sent"} & set(server.requests[before:])


# F2: while Claude works on a page, its alert() is closed at once and told to Claude; a confirm() is the user's to
# answer, and Claude is told about it at once (not after a time-out)
def message_boxes():
    from PyQt6.QtWidgets import QApplication, QMessageBox
    return [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]


def test_page_dialogs_while_claude_works(fg, window, server):
    server.add("/r3-alert", b"""<!doctype html><html><head><title>Alert</title></head><body>
<button onclick="alert('Saved!'); document.title = 'after alert'">Go</button>
<button onclick="document.title = confirm('Delete it?') ? 'deleted' : 'kept'">Delete</button></body></html>""",
               "text/html; charset=utf-8")
    tab = window.current_tab()
    settled_load(tab, server.url("/r3-alert"))
    page_text = tool(fg, window, "read_page")["c"]
    browser, box, seen = window._test_agent_browser, {}, []
    QTimer.singleShot(1500, lambda: seen.append(len(message_boxes())))
    t0 = time.monotonic()
    browser.run("click", {"label": label_of(page_text, "Go")},
                lambda c, e=False, log="": box.setdefault("click", (c, e, log, time.monotonic() - t0)))
    wait_until(lambda: "click" in box and seen, 30, "the click")
    content, error, log_line, took = box["click"]
    assert not error and "Saved!" in content and "alert" in content and "alert" in log_line and took < 3, (content, took)
    assert seen == [0]  # (no dialog was shown)
    assert run_js(tab.page, "document.title") == "after alert"
    # a confirm(): the user answers it; Claude hears of it at once, and the page waits
    box.clear()

    def later_read() -> None:
        t1 = time.monotonic()
        browser.run("read_page", {}, lambda c, e=False, log="": box.setdefault("read", (c, e, time.monotonic() - t1)))

    def answer() -> None:
        boxes = message_boxes()
        box["boxes"] = len(boxes)
        for w in boxes:
            w.done(0)
    QTimer.singleShot(3000, later_read)  # (these run in the dialog's own event loop)
    QTimer.singleShot(5000, answer)
    t0 = time.monotonic()
    browser.run("click", {"label": label_of(page_text, "Delete")},
                lambda c, e=False, log="": box.setdefault("click", (c, e, time.monotonic() - t0)))
    wait_until(lambda: {"click", "read", "boxes"} <= set(box), 30, "the dialog to be answered")
    content, error, took = box["click"]
    assert box["boxes"] == 1
    assert error and "Delete it?" in content and "confirm" in content and took < 3, (content, took)
    content, error, took = box["read"]
    assert error and "Delete it?" in content and took < 1, (content, took)
    result = tool(fg, window, "read_page")  # (answered: the page works again)
    assert not result["e"] and "Delete" in result["c"]
    assert run_js(tab.page, "document.title") == "kept"
    # Claude done: the page's alerts are the user's again
    browser.cancel()
    seen.clear()

    def look() -> None:
        seen.append(len(message_boxes()))
        for w in message_boxes():
            w.done(0)
    QTimer.singleShot(1500, look)
    run_js(tab.page, "setTimeout(() => alert('for the user'), 0); 1")
    wait_until(lambda: seen, 20, "the alert")
    assert seen == [1]


# F3: dialogs that open while the app is in the background are covered at once - a page's alert() too
def test_dialogs_opened_in_the_background_are_covered(fg, window, server, qapp):
    from PyQt6.QtWidgets import QApplication, QMessageBox
    server.add("/r3-bg-alert", b"<!doctype html><title>Timer</title><p>hi</p>", "text/html; charset=utf-8")
    tab = window.current_tab()
    settled_load(tab, server.url("/r3-bg-alert"))
    seen = {}

    def look() -> None:
        boxes = [w for w in QApplication.topLevelWidgets() if isinstance(w, QMessageBox) and w.isVisible()]
        screens = [w.findChild(fg.PrivacyScreen, options=Qt.FindChildOption.FindDirectChildrenOnly) for w in boxes]
        seen["alert"] = [(s is not None and s.covering and s.isVisible()) for s in screens]
        for w in boxes:
            w.done(0)
    try:
        qapp.applicationStateChanged.emit(INACTIVE)
        spin(0.2)
        window.show_history()
        history = window._dialogs["history"]
        QGuiApplication.processEvents()
        screen = history.findChild(fg.PrivacyScreen, options=Qt.FindChildOption.FindDirectChildrenOnly)
        assert screen is not None and screen.covering and screen.isVisible() and screen.opacity == 1.0
        QTimer.singleShot(1500, look)
        run_js(tab.page, "setTimeout(() => alert('secret page text'), 0); 1")
        wait_until(lambda: "alert" in seen, 20, "the alert")
        assert seen["alert"] == [True]
    finally:
        qapp.applicationStateChanged.emit(ACTIVE)


# F4: quitting with a password manager edit dialog open
def test_quit_with_an_edit_dialog_open(fg, harness, vault):
    win = harness.window()
    win.show_autofill_settings()
    dialog = win._dialogs["autofill"]
    spin(0.2)
    QTimer.singleShot(300, dialog.close)  # (what closing the window does to it; it deletes itself, and the edit dialog)
    dialog.add_password()  # (modal, until then: no RuntimeError once it returns)
    spin(0.3)
    assert sip.isdeleted(dialog)


# F5: a filled-in password doesn't cost the tab its back/forward list
def test_history_is_kept_after_a_password_was_filled(fg, window, server):
    secret = "Pa55-Filled-In!"
    server.add("/r3-signin", LOGIN.encode(), "text/html; charset=utf-8")
    server.add("/r3-a", b"<!doctype html><title>A</title>", "text/html; charset=utf-8")
    server.add("/r3-b", b"<!doctype html><title>B</title>", "text/html; charset=utf-8")
    tab = window.current_tab()
    assert load(tab.page, server.url("/r3-signin"))
    run_js(tab.page, f"document.getElementById('user').value = 'alice'; document.getElementById('pass').value = {json.dumps(secret)}; 1")
    fill_secret(window, tab, secret)
    for path in ("/r3-a", "/r3-b"):
        assert load(tab.page, server.url(path))
    spin(0.5)
    entry = tab.session_entry()
    assert "history" in entry
    blob = base64.b64decode(entry["history"])
    assert secret.encode("utf-16-le") not in blob and secret.encode() not in blob
    # ... unless the password is in it after all (a "show password" button made the field a text field)
    assert load(tab.page, server.url("/r3-signin"))
    run_js(tab.page, f"const p = document.getElementById('pass'); p.type = 'text'; p.value = {json.dumps(secret)}; 1")
    assert load(tab.page, server.url("/r3-a"))
    spin(0.5)
    if secret.encode("utf-16-le") in bytes(base64.b64decode(tab_history(tab))):
        assert "history" not in tab.session_entry()


def tab_history(tab) -> bytes:
    from PyQt6.QtCore import QByteArray, QDataStream, QIODevice
    data = QByteArray()
    stream = QDataStream(data, QIODevice.OpenModeFlag.WriteOnly)
    stream << tab.page.history()
    return bytes(data.toBase64())
