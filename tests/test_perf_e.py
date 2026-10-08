"""Disk I/O: a page whose title keeps changing no longer rewrites session.json or commits to history without end,
write_json/write_private_json go through json.dumps (same bytes, the C encoder), the history database runs WAL with
synchronous=NORMAL and deletes in one transaction, and the History window searches after a pause in typing."""
from __future__ import annotations

import json
import stat

from PyQt6 import sip
from PyQt6.QtCore import QUrl

from helpers import run_js, spin, wait_until

TICK_PAGE = b"""<!doctype html><html><head><meta charset="utf-8"><title>tick 0</title><script>
function tick(count) {
  let n = 0;
  const timer = setInterval(() => { n += 1; document.title = "tick " + n; if (n >= count) clearInterval(timer); }, 100);
}
</script></head><body><p>ticking</p></body></html>"""

UNICODE_DATA = {"name": "Chrome 2 éè ☃", "list": [1, 2.5, None, True, False, {"nested": "  line \u0000 nul"}],
                "empty": {}, "none": [], "big": 2 ** 70, "neg": -0.0, "quote": 'say "hi" \\ back', "key ü": "v",
                "tabs": [{"url": "https://example.com/?q=ü", "title": "Título", "history": "QUJD" * 2000}]}


# ── JSON files ────────────────────────────────────────────────────────────────────────────
def test_json_writers_match_json_dumps(fg, tmp_path):
    expected = json.dumps(UNICODE_DATA, ensure_ascii=False, indent=1)
    public, private = tmp_path / "public.json", tmp_path / "private.json"
    assert fg.write_json(public, UNICODE_DATA) and public.read_text(encoding="utf-8") == expected
    assert fg.write_private_json(private, UNICODE_DATA) and private.read_text(encoding="utf-8") == expected
    assert stat.S_IMODE(private.stat().st_mode) == 0o600
    assert json.loads(public.read_text(encoding="utf-8")) == UNICODE_DATA
    # Unserializable data: False, the old file untouched and no temp file left (dumps runs before it is created).
    for writer, path in ((fg.write_json, public), (fg.write_private_json, private)):
        assert writer(path, {"x": object()}) is False
        assert path.read_text(encoding="utf-8") == expected
        assert not list(tmp_path.glob(".*.tmp"))
    assert fg.write_json(public, {"v": 2}, keep_backup=True)
    assert public.read_text(encoding="utf-8") == '{\n "v": 2\n}'
    assert public.with_name("public.json.bak").read_text(encoding="utf-8") == expected


# ── history database ──────────────────────────────────────────────────────────────────────
def test_history_store_wal_normal_and_one_transaction_delete(fg, tmp_path):
    store = fg.HistoryStore(tmp_path / "history.sqlite")
    assert store.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.db.execute("PRAGMA synchronous").fetchone()[0] == 1  # NORMAL (2 = FULL: an fsync per commit)
    urls = [f"https://site{i}.example.com/p" for i in range(1500)]
    for i, url in enumerate(urls):
        store.add_visit(url, f"Page {i}")
    store.set_title(urls[0], "Page 0 (renamed)")
    statements: list[str] = []
    store.db.set_trace_callback(statements.append)
    store.delete(urls[:1000])
    store.db.set_trace_callback(None)
    assert sum(s.upper().startswith("COMMIT") for s in statements) == 1, statements[-3:]
    assert not store.db.in_transaction
    assert {r[0] for r in store.db.execute("SELECT url FROM places")} == set(urls[1000:])
    assert store.search("site1499") == [("https://site1499.example.com/p", "Page 1499")]
    assert len(store.recent("", 5000)) == 500 and store.recent("site1000")[0][1] == "Page 1000"
    store.delete([])
    assert len(store.recent("", 5000)) == 500
    store.clear()
    assert store.recent("", 5000) == []
    store.close()
    store.delete(urls)  # a closed store: nothing to do, no error


# ── a ticking title ───────────────────────────────────────────────────────────────────────
def test_ticking_title_leaves_session_and_history_alone(fg, window, server, monkeypatch):
    server.add("/tick", TICK_PAGE, "text/html; charset=utf-8")
    writes, titles = [], []
    real_write, real_set_title = fg.write_private_json, window.history.set_title

    def counting_write(path, data):
        writes.append(path)
        return real_write(path, data)

    def counting_set_title(url, title):
        titles.append((url, title))
        real_set_title(url, title)

    monkeypatch.setattr(fg, "write_private_json", counting_write)
    monkeypatch.setattr(window.history, "set_title", counting_set_title)
    window._autosave.stop()  # (its 15 s tick is a write of its own; this test counts the title's)
    tab, url = window.current_tab(), server.url("/tick")
    tab.load(QUrl(url))
    wait_until(lambda: not tab.loading and tab.page.url().toString() == url, 20, "the ticking page to load")
    wait_until(lambda: not window._session_timer.isActive(), 5, "the load's own session save to run")
    writes.clear()
    run_js(tab.page, "tick(20); true")
    wait_until(lambda: tab.title() == "tick 20", 10, "the page's last title")
    spin(1.5)  # (a title-triggered save would have fired 1 s after the last change)
    assert writes == [], f"{len(writes)} session.json writes for 20 title changes"
    assert not window._session_timer.isActive()
    assert 1 <= len(titles) <= 5, f"{len(titles)} history title updates for one document"
    assert all(u == url for u, _t in titles) and tab.title_writes == 5
    # The title is not lost: it is in the live session data, and a quit (save_session) writes it.
    index = window.tabs().index(tab)
    assert window.session_data()["tabs"][index]["title"] == "tick 20"
    window.save_session()
    assert writes == [window.session_path]
    assert json.loads(window.session_path.read_text(encoding="utf-8"))["tabs"][index]["title"] == "tick 20"
    # A navigation (here pushState) starts the count over for the new document, as in Chrome.
    run_js(tab.page, "history.pushState({}, '', '/tick?spa=1'); true")
    wait_until(lambda: tab.page.url().query() == "spa=1", 10, "the pushState to reach the browser")
    assert tab.title_writes == 0
    titles.clear()
    run_js(tab.page, "document.title = 'spa title'; true")
    wait_until(lambda: tab.title() == "spa title", 10, "the new title")
    assert titles == [(url + "?spa=1", "spa title")]


# ── the History window's search box ───────────────────────────────────────────────────────
def test_history_search_waits_for_a_pause_in_typing(fg, window, monkeypatch):
    for i in range(30):
        window.history.add_visit(f"https://abc{i}.example.com/", f"Page {i}")
    window.history.add_visit("https://other.example.com/", "Other page")
    queries: list[str] = []
    real_recent = window.history.recent

    def counting_recent(text: str = "", limit: int = 2000):
        queries.append(text)
        return real_recent(text, limit)

    monkeypatch.setattr(window.history, "recent", counting_recent)
    dialog = fg.HistoryDialog(window)
    try:
        assert queries == [""] and dialog.tree.topLevelItemCount() == 31
        for text in ("a", "ab", "abc"):
            dialog.search.setText(text)
        assert queries == [""]  # (nothing per keystroke)
        spin(0.4)
        assert queries == ["", "abc"]
        rows = [dialog.tree.topLevelItem(i) for i in range(dialog.tree.topLevelItemCount())]
        assert len(rows) == 30 and all("abc" in row.text(1) for row in rows)
        # A direct reload (what Delete and Clear do) covers a pending search instead of running it again after.
        dialog.search.setText("other")
        dialog.reload()
        assert queries == ["", "abc", "other"] and dialog.tree.topLevelItemCount() == 1
        spin(0.4)
        assert queries == ["", "abc", "other"]
    finally:
        dialog.close()
        sip.delete(dialog)
