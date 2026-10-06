"""The bookmarks toolbar hides itself while it has nothing on it."""
from __future__ import annotations


def test_bookmarks_bar_shows_only_with_bookmarks(window):
    window.show()
    bar, store = window.bookmarks_bar, window.bookmarks
    for node in list(store.children("toolbar")):
        store.remove(node["id"])
    assert window.settings.get("show_bookmarks_bar")
    assert not bar.isVisible()  # empty: hidden although the setting is on

    node = store.add_bookmark("Example", "https://example.com/")
    assert bar.isVisible()

    window.act_bookmarks_bar.trigger()  # the user hides it
    assert not bar.isVisible() and not window.settings.get("show_bookmarks_bar")
    window.act_bookmarks_bar.trigger()  # and shows it again
    assert bar.isVisible() and window.settings.get("show_bookmarks_bar")

    store.remove(node["id"])
    assert not bar.isVisible()
    assert window.settings.get("show_bookmarks_bar")  # comes back with the next bookmark
    store.add_bookmark("Example", "https://example.com/")
    assert bar.isVisible()
