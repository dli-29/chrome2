"""Pinned tabs and split view (like Chrome's): ordering, looks, dragging, closing/reopening, the session, and what
the focused side of a split view drives (address bar, find, zoom, extension APIs, full screen)."""
from __future__ import annotations

import re
from urllib.parse import quote, unquote

from PyQt6.QtCore import QEvent, QPointF, Qt, QUrl
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication, QTabBar

from helpers import run_js, spin, wait_until

FAKE_EXT = "a" * 32  # an extension id nobody installed: tab_info as an extension without "tabs" sees it


def page(text: str) -> QUrl:
    return QUrl("data:text/html," + quote(f"<!doctype html><title>{text}</title><body><p>{text}</p></body>"))


def shows(tab) -> str:
    """The text of the data: page a tab is on (from its address)."""
    match = re.search(r"<p>(.*?)</p>", unquote(tab.url().toString()))
    return match.group(1) if match else ""


def loaded(window, tab, url: QUrl):
    """Load *url* in *tab* and wait until it shows it."""
    tab.load(url)
    target = url.toString()
    wait_until(lambda: tab.pending is None and not tab.loading and tab.page.url().toString() == target, 20,
               f"{shows(tab) or 'the tab'} to load {unquote(target)[-40:]}")
    return tab


def settled(tab) -> None:
    wait_until(lambda: tab.pending is None and not tab.loading and tab.page.url().scheme() == "data", 20,
               "the tab to load its page")


def open_tabs(window, *texts: str) -> list:
    """The window's first tab plus one per text, all loaded (the first shows texts[0])."""
    tabs = [window.current_tab()] + [window.new_tab(None, background=True) for _ in texts[1:]]
    for tab, text in zip(tabs, texts):
        loaded(window, tab, page(text))
    return tabs


def order(window) -> list:
    return window.tabs()


def mouse(widget, kind: QEvent.Type, x: float, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton):
    pos = QPointF(x, widget.height() / 2)
    event = QMouseEvent(kind, pos, widget.mapToGlobal(pos), button, buttons, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, event)


def drag(bar, index: int, to_x: float, steps: int = 12, check=None) -> None:
    """Press on the tab at *index*, move to *to_x* in steps (calling *check* after each), release."""
    start = bar.tabRect(index).center().x()
    mouse(bar, QEvent.Type.MouseButtonPress, start)
    for i in range(1, steps + 1):
        mouse(bar, QEvent.Type.MouseMove, start + (to_x - start) * i / steps, Qt.MouseButton.NoButton)
        QApplication.processEvents()
        if check is not None:
            check()
    mouse(bar, QEvent.Type.MouseButtonRelease, to_x, buttons=Qt.MouseButton.NoButton)
    spin(0.45)  # Qt's drop animation, then the strip's clean-up


class FakeFullScreenRequest:
    def __init__(self, on: bool):
        self.on, self.answer = on, None

    def toggleOn(self) -> bool:
        return self.on

    def accept(self) -> None:
        self.answer = "accepted"

    def reject(self) -> None:
        self.answer = "rejected"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Pinned tabs
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_pinning_moves_tabs_left_icon_only(window, fg):
    a, b, c, d = open_tabs(window, "a", "b", "c", "d")
    bar = window.tab_bar
    window.set_pinned(c, True)
    assert order(window) == [c, a, b, d] and c.pinned
    window.set_pinned(d, True)  # goes to the end of the pinned group
    assert order(window) == [c, d, a, b]
    spin(0.1)
    for i in (0, 1):  # compact, fixed width, no close button, the title only as a tooltip
        assert bar.tabRect(i).width() == fg.PINNED_TAB_WIDTH
        assert bar.tabButton(i, QTabBar.ButtonPosition.RightSide) is None
        assert bar.label(i).pinned
    assert bar.tabToolTip(0).startswith("c")
    assert bar.tabRect(2).width() > fg.PINNED_TAB_WIDTH and bar.tabButton(2, QTabBar.ButtonPosition.RightSide) is not None
    label = bar.label(0)  # the icon is centred in the tab
    icon_centre = label.mapTo(bar, label.icon_rect().center()).x()
    assert abs(icon_centre - bar.tabRect(0).center().x()) <= 2
    assert window.extensions.bridge.tab_info(FAKE_EXT, c)["pinned"] is True
    assert window.extensions.bridge.tab_info(FAKE_EXT, a)["pinned"] is False

    window.set_pinned(c, False)  # unpinned: first after the pinned tabs, with its close button back
    assert order(window) == [d, c, a, b] and not c.pinned
    spin(0.1)
    assert bar.tabButton(1, QTabBar.ButtonPosition.RightSide) is not None
    assert bar.tabRect(1).width() > fg.PINNED_TAB_WIDTH and not bar.label(1).pinned


def test_new_tabs_go_after_the_pinned_ones(window):
    a, b = open_tabs(window, "a", "b")
    window.set_pinned(b, True)
    assert order(window) == [b, a]
    first = window.new_tab(page("x"), index=0)  # an unpinned tab can't go in front of pinned ones
    assert window.index_of(first) == 1
    window.focus_tab(b)
    window.open_url(page("from pinned 1"), "background", opener=b)
    window.open_url(page("from pinned 2"), "background", opener=b)
    assert order(window)[0] is b  # right of the pinned group, in order
    assert [shows(t) for t in order(window)[1:]] == ["from pinned 1", "from pinned 2", "x", "a"]


def test_tab_menu_pins_and_unpins(window):
    a, b = open_tabs(window, "a", "b")
    menu = window.tab_menu(b)
    actions = {action.text(): action for action in menu.actions()}
    assert "Pin Tab" in actions and actions["Add Tab to New Split View"].isEnabled()
    actions["Pin Tab"].trigger()
    assert b.pinned and order(window) == [b, a]
    menu = window.tab_menu(b)
    actions = {action.text(): action for action in menu.actions()}
    assert "Unpin Tab" in actions
    assert not actions["Add Tab to New Split View"].isEnabled()  # pinned tabs can't be in a split view
    assert actions["Close Other Tabs"].isEnabled()  # (it closes a, never other pinned tabs)
    actions["Unpin Tab"].trigger()
    assert not b.pinned


def test_dragging_never_mixes_pinned_and_other_tabs(window):
    a, b, c, d = open_tabs(window, "a", "b", "c", "d")
    window.set_pinned(a, True)
    window.set_pinned(b, True)
    bar = window.tab_bar
    spin(0.1)
    def apart():  # (all through the drag, not just after it)
        assert [t.pinned for t in order(window)] == [True, True, False, False]

    drag(bar, 0, bar.width() - 5, check=apart)  # a pinned tab dragged far right stays the last pinned one
    assert order(window)[:2] == [b, a] and order(window)[2:] == [c, d]
    drag(bar, 3, 2, check=apart)  # an unpinned tab dragged far left stops right after the pinned ones
    assert order(window) == [b, a, d, c]
    assert [t.pinned for t in order(window)] == [True, True, False, False]


def test_pinned_tabs_close_and_reopen_pinned(window):
    a, b, c = open_tabs(window, "a", "b", "c")
    window.set_pinned(c, True)
    window.set_pinned(b, True)
    window.focus_tab(b)
    assert window.current_tab() is b
    window.act_close_tab.trigger()  # Ctrl+W closes a pinned tab too
    wait_until(lambda: window.index_of(b) < 0, message="the pinned tab to close")
    assert order(window) == [c, a]
    window.reopen_closed_tab()  # comes back pinned, with the pinned tabs
    reopened = order(window)[1]
    assert reopened.pinned and reopened is not a and order(window)[2] is a
    settled(reopened)
    assert shows(reopened) == "b"
    bar = window.tab_bar  # middle-click closes a pinned tab as well
    x = bar.tabRect(0).center().x()
    mouse(bar, QEvent.Type.MouseButtonPress, x, Qt.MouseButton.MiddleButton, Qt.MouseButton.MiddleButton)
    mouse(bar, QEvent.Type.MouseButtonRelease, x, Qt.MouseButton.MiddleButton, Qt.MouseButton.NoButton)
    wait_until(lambda: window.index_of(c) < 0, message="middle-click to close the pinned tab")
    assert order(window) == [reopened, a]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Split view
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_split_view_shows_both_and_the_focused_side_drives_the_toolbar(window):
    a, b, c = open_tabs(window, "alpha", "beta", "gamma")
    window.focus_tab(a)
    split = window.add_tab_to_split(c)  # the clicked tab joins the current one, next to it in the strip
    assert split is not None and a.split is split and c.split is split
    assert split.tabs == [a, c] and order(window) == [a, c, b]
    assert window.current_tab() is c and window.visible_tabs() == [a, c]
    assert window.stack.currentWidget() is split
    assert window.split_button.isVisible()
    spin(0.3)
    assert a.isVisible() and c.isVisible() and a.view.width() > 100 and c.view.width() > 100
    assert abs(a.view.width() - c.view.width()) < 20
    assert run_js(a.page, "document.body.textContent") == "alpha"  # both live
    assert run_js(c.page, "document.body.textContent") == "gamma"
    assert "gamma" in window.url_bar.text()
    assert window.windowTitle().startswith("gamma")

    window.activateWindow()
    a.view.setFocus()  # clicking into the other side focuses it
    wait_until(lambda: window.current_tab() is a, message="the left side to become current")
    assert "alpha" in window.url_bar.text() and window.windowTitle().startswith("alpha")
    assert window.visible_tabs() == [a, c] and window.stack.currentWidget() is split
    assert split.panes[0].property("focused") and not split.panes[1].property("focused")

    window.focus_tab(b)  # another tab: the split view goes away (but stays a split view)
    assert window.visible_tabs() == [b] and window.stack.currentWidget() is b and not window.split_button.isVisible()
    assert a.split is split
    window.focus_tab(c)  # and comes back with either of its tabs
    assert window.stack.currentWidget() is split and window.visible_tabs() == [a, c]


def test_back_reload_and_navigation_act_on_the_focused_side(window):
    a, b = open_tabs(window, "one", "two")
    window.focus_tab(a)
    window.add_tab_to_split(b)
    loaded(window, b, page("two-next"))
    assert window.back_button.isEnabled()  # b has history; a doesn't
    window.focus_tab(a)
    assert not window.back_button.isEnabled()
    window.focus_tab(b)
    window.act_back.trigger()
    wait_until(lambda: shows(b) == "two", message="b to go back")
    assert shows(a) == "one"
    window._navigate_from_url_bar(page("typed").toString())  # what Enter in the address bar does
    wait_until(lambda: shows(b) == "typed", message="b to navigate")
    assert shows(a) == "one"


def test_add_split_for_the_current_tab_opens_a_new_tab_beside_it(window, fg):
    a, b = open_tabs(window, "a", "b")
    window.focus_tab(b)
    split = window.add_tab_to_split(b)
    new = window.current_tab()
    assert split.tabs == [b, new] and new not in (a, b) and fg.is_newtab(new.url())
    assert order(window) == [a, b, new]


def test_reverse_and_separate_views(window, fg):
    a, b, c = open_tabs(window, "a", "b", "c")
    window.focus_tab(a)
    split = window.add_tab_to_split(b)
    split.set_ratio(0.3)
    window.reverse_split(split)
    assert split.tabs == [b, a] and order(window) == [b, a, c]
    assert abs(split.ratio - 0.7) < 0.01
    assert window.current_tab() is b and window.visible_tabs() == [b, a]
    menu_texts = [action.text() for action in window.tab_menu(a).actions()]
    assert "Reverse Views" in menu_texts and "Separate Views" in menu_texts
    menu = fg.Menu("", window)
    window._fill_split_menu(menu)  # the split view button's menu
    assert [action.text() for action in menu.actions() if action.text()] == \
        ["Reverse Views", "Separate Views", "Close Left View", "Close Right View"]
    window._update_tab_actions()  # (the macOS menu bar's Window menu)
    assert window.act_split_view.text() == "Separate Views" and window.act_pin_tab.text() == "Pin Tab"
    next(action for action in menu.actions() if action.text() == "Separate Views").trigger()
    assert a.split is None and b.split is None and order(window) == [b, a, c]
    assert window.current_tab() is b and window.visible_tabs() == [b]
    assert window.stack.currentWidget() is b and not window.split_button.isVisible()
    spin(0.2)
    assert b.isVisible() and not a.isVisible()
    window.focus_tab(a)
    assert window.stack.currentWidget() is a and a.isVisible()


def test_closing_one_side_ends_the_split_and_reopening_restores_it(window):
    a, b, c = open_tabs(window, "a", "b", "c")
    window.focus_tab(a)
    window.add_tab_to_split(b)
    a.split.set_ratio(0.35)
    window.focus_tab(a)
    window.close_tab(a)  # the focused side: the other one stays, on its own
    wait_until(lambda: window.index_of(a) < 0, message="the left side to close")
    assert b.split is None and window.current_tab() is b and window.visible_tabs() == [b]
    assert window.stack.currentWidget() is b and b.isVisible()
    window.reopen_closed_tab()
    back = window.current_tab()
    assert back is not b and back.split is b.split and back.split is not None
    assert back.split.tabs == [back, b] and order(window) == [back, b, c]
    assert abs(back.split.ratio - 0.35) < 0.01
    settled(back)
    assert shows(back) == "a"
    window.close_tab(b)  # the other side, not focused
    wait_until(lambda: window.index_of(b) < 0, message="the right side to close")
    assert back.split is None and window.current_tab() is back


def test_pinned_tabs_and_split_views_dont_mix(window):
    a, b, c = open_tabs(window, "a", "b", "c")
    window.set_pinned(a, True)
    toasts = []
    window.toast = lambda text, kind="success": toasts.append(text)
    assert window.add_tab_to_split(a) is None and a.split is None and toasts
    window.focus_tab(a)  # the current tab is pinned: the clicked tab gets a New Tab beside it instead
    split = window.add_tab_to_split(b)
    assert split is not None and b in split.tabs and a not in split.tabs and not any(t.pinned for t in split.tabs)
    window.set_pinned(b, True)  # pinning a tab of a split view ends the split view first
    assert b.pinned and b.split is None and all(t.split is None for t in order(window))
    assert order(window)[:2] == [a, b]


def test_strip_keeps_split_views_together(window):
    t0, t1, t2, t3 = open_tabs(window, "t0", "t1", "t2", "t3")
    window.focus_tab(t1)
    window.add_tab_to_split(t2)
    bar = window.tab_bar
    assert bar.split_pairs() == [(1, 2)]
    spin(0.1)
    def together():
        assert window.index_of(t2) == window.index_of(t1) + 1

    drag(bar, 1, bar.tabRect(3).right() + 30, check=together)  # dragging one half moves both, all the way
    assert order(window) == [t0, t3, t1, t2]
    drag(bar, 3, 2)  # (from the right half too, to the far left)
    assert order(window) == [t1, t2, t0, t3]
    drag(bar, 3, bar.tabRect(0).left() + 5)  # another tab dragged onto the pair goes past it, not between
    assert order(window) in ([t3, t1, t2, t0], [t1, t2, t3, t0])
    assert bar.split_pairs() and t1.split.tabs == [t1, t2]
    # an extension moving one half moves the pair; new tabs never land between the halves
    window.extensions.bridge.api_tabs_move(FAKE_EXT, {"tabIds": [t2.tab_id], "index": 0}, {})
    assert window.index_of(t2) == window.index_of(t1) + 1 and bar.split_pairs()
    middle = window.new_tab(page("middle"), index=window.index_of(t2))
    assert window.index_of(middle) == window.index_of(t2) + 1 and window.index_of(t2) == window.index_of(t1) + 1


def test_new_tabs_from_a_split_view_open_after_it(window):
    a, b, c = open_tabs(window, "a", "b", "c")
    window.focus_tab(a)
    window.add_tab_to_split(b)
    window.open_url(page("child"), "background", opener=a)
    assert order(window)[:2] == [a, b] and shows(order(window)[2]) == "child"
    dup = window.duplicate_tab(a)
    assert dup.split is None and window.index_of(dup) == 2 and dup.uid != a.uid


def test_find_bar_and_zoom_act_on_the_focused_side(window):
    a, b = open_tabs(window, "apple pie", "banana split")
    window.focus_tab(a)
    window.add_tab_to_split(b)
    window.focus_tab(a)
    window.find_bar.open()
    window.find_bar.edit.setText("apple")
    wait_until(lambda: window.find_bar.status.text() == "1 of 1 match", message="the left side's match")
    window.focus_tab(b)  # the find bar stays and searches the side that has the focus
    assert window.find_bar.isVisible()
    wait_until(lambda: window.find_bar.status.text() == "Phrase not found", message="no match on the right")
    window.find_bar.edit.setText("banana")
    wait_until(lambda: window.find_bar.status.text() == "1 of 1 match", message="the right side's match")
    window.find_bar.close_bar()
    window.zoom_in()
    assert b.view.zoomFactor() > 1.05 and abs(a.view.zoomFactor() - 1.0) < 0.001
    assert window.url_bar.zoom_action.isVisible()
    window.focus_tab(a)
    assert not window.url_bar.zoom_action.isVisible()
    window.zoom_out()
    assert a.view.zoomFactor() < 0.95 and b.view.zoomFactor() > 1.05


def test_extensions_see_the_focused_side_as_the_active_tab(window):
    a, b = open_tabs(window, "a", "b")
    window.focus_tab(a)
    window.add_tab_to_split(b)
    bridge = window.extensions.bridge
    active = bridge.api_tabs_query(FAKE_EXT, {"active": True, "currentWindow": True}, {})
    assert [t["id"] for t in active] == [b.tab_id]
    window.focus_tab(a)
    active = bridge.api_tabs_query(FAKE_EXT, {"active": True}, {})
    assert [t["id"] for t in active] == [a.tab_id]
    assert bridge._tab(None, current=True) is a
    bridge.api_tabs_update(FAKE_EXT, {"tabId": b.tab_id, "active": True}, {})  # activating the other side
    assert window.current_tab() is b and window.visible_tabs() == [a, b]
    bridge.api_tabs_update(FAKE_EXT, {"tabId": a.tab_id, "pinned": True}, {})  # pinning via the API ends the split
    assert a.pinned and a.split is None and b.split is None and order(window)[0] is a
    created = bridge.api_tabs_create(FAKE_EXT, {"url": "about:blank", "pinned": True, "active": False}, {})
    assert created["pinned"] is True and window.index_of(next(t for t in window.tabs() if t.tab_id == created["id"])) == 1


def test_only_the_full_screen_side_fills_the_window(window):
    a, b = open_tabs(window, "a", "b")
    window.focus_tab(a)
    split = window.add_tab_to_split(b)
    request = FakeFullScreenRequest(True)
    window._on_fullscreen_request(a, request)  # not the focused side: refused
    assert request.answer == "rejected"
    request = FakeFullScreenRequest(True)
    window._on_fullscreen_request(b, request)
    assert request.answer == "accepted" and window._fullscreen_tab is b
    spin(0.2)
    assert not split.panes[0].isVisible() and split.panes[1].isVisible()
    window._on_fullscreen_request(b, FakeFullScreenRequest(False))
    spin(0.2)
    assert window._fullscreen_tab is None and split.panes[0].isVisible() and split.panes[1].isVisible()
    assert window.tab_strip.isVisible() and window.split_button.isVisible()


def test_divider_ratio_is_kept(window):
    a, b = open_tabs(window, "a", "b")
    window.focus_tab(a)
    split = window.add_tab_to_split(b)
    spin(0.2)
    total = sum(split.splitter.sizes())
    split.splitter.moveSplitter(int(total * 0.25), 1)  # what dragging the divider does
    assert abs(split.ratio - 0.25) < 0.05
    assert window.session_data()["splits"] == [[0, 1, round(split.ratio, 4)]]


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Session
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_pins_and_split_views_survive_a_restart(harness):
    first = harness.window()
    a, b, c, d, e = open_tabs(first, "a", "b", "c", "d", "e")
    first.set_pinned(b, True)
    first.focus_tab(c)
    first.add_tab_to_split(e)
    c.split.set_ratio(0.3)
    first.focus_tab(d)
    first.add_tab_to_split(a)  # (a goes next to d)
    assert [shows(t) for t in order(first)] == ["b", "c", "e", "d", "a"]
    first.close_tab(a)  # the closed tab remembers its split view
    wait_until(lambda: first.index_of(a) < 0, message="a to close")
    first.focus_tab(c)  # the left side of the c|e split view is the current tab
    first.close_now()
    QApplication.processEvents()

    second = harness.window()
    tabs = order(second)
    assert [shows(t) for t in tabs] == ["b", "c", "e", "d"]
    assert [t.pinned for t in tabs] == [True, False, False, False]
    spin(0.1)
    assert second.tab_bar.tabButton(0, QTabBar.ButtonPosition.RightSide) is None and second.tab_bar.label(0).pinned
    current = second.current_tab()
    assert shows(current) == "c" and current.split is not None and current.split.tabs == tabs[1:3]
    assert abs(current.split.ratio - 0.3) < 0.01
    assert second.visible_tabs() == tabs[1:3] and second.stack.currentWidget() is current.split
    wait_until(lambda: all(t.pending is None for t in tabs[1:3]), message="both sides to load")
    assert tabs[0].pending is not None and tabs[3].pending is not None  # the others wait until you open them
    second.focus_tab(tabs[3])
    second.reopen_closed_tab()  # the closed side finds its partner again, after the restart too
    back = second.current_tab()
    assert shows(back) == "a" and back.split is not None and back.split.tabs == [tabs[3], back]


def test_pinned_tabs_come_back_even_without_session_restore(harness, fg):
    first = harness.window()
    a, b, c = open_tabs(first, "a", "b", "c")
    first.set_pinned(c, True)
    first.focus_tab(a)
    first.add_tab_to_split(b)
    first.settings.set("restore_session", False)
    first.close_now()
    QApplication.processEvents()

    second = harness.window()  # like Chrome: the pinned tabs, and a New Tab
    tabs = order(second)
    assert len(tabs) == 2 and tabs[0].pinned and shows(tabs[0]) == "c"
    assert fg.is_newtab(tabs[1].url()) and not tabs[1].pinned and second.current_tab() is tabs[1]
    assert all(t.split is None for t in tabs)
