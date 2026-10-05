"""End-to-end tests for Foxglove's Chrome extension support.

They drive foxglove.py's real classes (ExtensionsController on a real QWebEngineProfile, BrowserWindow,
ExtensionPopup, ExtensionsDialog) on the offscreen Qt platform, with locally built extensions and a
local HTTP server standing in for web sites and the Chrome Web Store.
"""
from __future__ import annotations

import json
import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest
from PyQt6 import sip
from PyQt6.QtCore import QSize, QUrl
from PyQt6.QtWidgets import QCheckBox, QPushButton

import extbuilder as eb
from helpers import WaitTimeout, poll_js, run_js, run_js_async, spin, stays_absent, wait_attr, wait_until

UBO = "ddkjiahejlhfcafbddmgiahcphecmpfh"


def ext_url(ext_id: str, page: str) -> QUrl:
    return QUrl(f"chrome-extension://{ext_id}/{page}")


def content_script_runs(harness, server, tag: str, timeout: float = 10.0) -> bool:
    page = harness.fresh_page(server.page_url())
    try:
        return wait_attr(page, f"data-fg-{tag}", timeout) == "ran"
    finally:
        harness.drop_page(page)


def content_script_absent(harness, server, tag: str, seconds: float = 2.0) -> bool:
    page = harness.fresh_page(server.page_url())
    try:
        return stays_absent(page, f"data-fg-{tag}", seconds)
    finally:
        harness.drop_page(page)


def storage_get(harness, entry, key: str):
    page = harness.fresh_page(entry.popup_url.toString())
    try:
        return run_js_async(page, f"return (await chrome.storage.local.get({json.dumps(key)}))[{json.dumps(key)}] ?? null")
    finally:
        harness.drop_page(page)


def storage_set(harness, entry, key: str, value) -> None:
    page = harness.fresh_page(entry.popup_url.toString())
    try:
        run_js_async(page, f"await chrome.storage.local.set({{{json.dumps(key)}: {json.dumps(value)}}}); return true")
    finally:
        harness.drop_page(page)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Pure helpers (no browser profile needed)
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("url", [
    f"https://chromewebstore.google.com/detail/ublock-origin-lite/{UBO}",
    f"https://chromewebstore.google.com/detail/{UBO}",
    f"https://chromewebstore.google.com/detail/ublock-origin-lite/{UBO}?hl=en&authuser=0",
    f"https://chromewebstore.google.com/detail/ublock-origin-lite/{UBO}/reviews",
    f"https://chromewebstore.google.com/detail/%E6%B2%89%E6%B5%B8%E5%BC%8F%E7%BF%BB%E8%AF%91/{UBO}",
    f"https://chrome.google.com/webstore/detail/ublock-origin-lite/{UBO}",
    f"https://chrome.google.com/webstore/detail/{UBO}?hl=en-US",
    f"https://chrome.google.com/webstore/detail/ublock-origin-lite/{UBO}#reviews",
])
def test_webstore_regex_matches_both_url_formats(fg, url):
    match = fg.WEBSTORE_RE.match(QUrl(url).toString())  # Foxglove matches QUrl.toString(), as here
    assert match is not None and match.group(1) == UBO


@pytest.mark.parametrize("url", [
    "https://chromewebstore.google.com/",
    "https://chromewebstore.google.com/category/extensions",
    f"https://chromewebstore.google.com/detail/x/{UBO[:-1]}",           # 31 letters
    f"https://chromewebstore.google.com/detail/x/{UBO}x",               # 33 letters
    "https://chromewebstore.google.com/detail/x/" + "z" * 32,            # IDs only use a-p
    f"https://chromewebstore.google.com.evil.example/detail/x/{UBO}",
    f"https://evil.example/webstore/detail/x/{UBO}",
])
def test_webstore_regex_rejects_other_urls(fg, url):
    assert fg.WEBSTORE_RE.match(QUrl(url).toString()) is None


def test_crx_parsing_recovers_the_signing_key(fg, tmp_path):
    src = eb.probe_extension(tmp_path / "src")
    key = eb.new_key()
    archive = eb.zip_bytes(src)
    for packed in (eb.crx3_bytes(archive, key), eb.crx2_bytes(archive, key)):
        zip_part, public = fg.parse_crx(packed)
        assert zip_part == archive and public == key.public_der
        assert fg.extension_id_from_key(public) == key.ext_id
    assert fg.parse_crx(archive) == (archive, None)


def test_manifest_reader_tolerates_comments_but_not_trailing_commas(fg, tmp_path):
    """Chromium accepts comments in manifest.json but refuses trailing commas - Foxglove's check must agree."""
    manifest = fg.load_manifest(eb.commented_manifest_extension(tmp_path / "c"))
    assert manifest["name"] == "Commented" and manifest["manifest_version"] == 3
    assert manifest["description"] == "a // that is not a comment, and a /* that isn't one either */"
    with pytest.raises(ValueError):
        fg.load_manifest(eb.commented_manifest_extension(tmp_path / "t", trailing_commas=True))


def test_localized_manifest_strings(fg, tmp_path):
    root = eb.localized_extension(tmp_path / "l")
    manifest = fg.load_manifest(root)
    assert fg.localized(root, manifest, manifest["name"]) == "Localized Probe"
    assert fg.localized(root, manifest, "__MSG_missing__") == "__MSG_missing__"


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Installing
# ══════════════════════════════════════════════════════════════════════════════════════════
def _package(kind: str, tmp_path: Path, name: str, tag: str):
    """Build the probe extension in the requested form; returns (path to install, signing key or None)."""
    src = eb.probe_extension(tmp_path / f"{tag}-src", name, tag)
    key = eb.new_key() if "crx" in kind else None
    path = {
        "folder": lambda: src,
        "zip": lambda: eb.write_zip(src, tmp_path / f"{tag}.zip"),
        "nested-zip": lambda: eb.write_zip(src, tmp_path / f"{tag}-nested.zip", prefix=f"{tag}-1.0/"),
        "macos-zip": lambda: eb.write_zip(src, tmp_path / f"{tag}-mac.zip", extra={"__MACOSX/._manifest.json": b"\0\5\26\7"}),
        "crx3": lambda: eb.write_crx3(src, tmp_path / f"{tag}.crx", key),
        "nested-crx3": lambda: eb.write_crx3(src, tmp_path / f"{tag}-nested.crx", key, prefix="dist/"),
        "crx2": lambda: eb.write_crx2(src, tmp_path / f"{tag}-v2.crx", key),
    }[kind]()
    return path, key


@pytest.mark.parametrize("kind", ["folder", "zip", "nested-zip", "macos-zip", "crx3", "nested-crx3", "crx2"])
def test_install_from_each_package_kind(harness, server, tmp_path, kind):
    name, tag = f"Probe {kind}", kind.replace("-", "")
    path, key = _package(kind, tmp_path, name, tag)
    kind_, text = harness.install(path)
    assert kind_ == "success", text
    assert text == f"“{name}” was added to Foxglove."
    entry = harness.wait_enabled_by_name(name)
    assert entry.version == "1.0" and entry.description == f"Test extension {tag}"
    if key is not None:  # signed packages keep the developer key, so the ID is the key's ID
        assert entry.id == key.ext_id
        installed = json.loads((Path(entry.path) / "manifest.json").read_text(encoding="utf-8"))
        assert installed["key"] == key.manifest_key
    assert Path(entry.path).parent == Path(harness.manager.installPath())
    state = harness.registry_on_disk()[entry.id]
    assert state["source"] == "file" and state["source_path"] == str(Path(path).resolve())
    assert harness.leftover_staging() == []
    assert content_script_runs(harness, server, tag)


def test_extension_is_enabled_after_install(harness, tmp_path):
    seen: list[tuple[str, bool]] = []
    harness.manager.installFinished.connect(lambda info: seen.append((info.id(), info.isEnabled())))
    entry = harness.install_ok(eb.probe_extension(tmp_path / "e", "Enabler", "enabler"), "Enabler")
    assert seen == [(entry.id, False)], "premise: Qt reports a fresh install as disabled"
    info = harness.info(entry.id)
    assert info.isInstalled() and info.isLoaded() and info.isEnabled() and not info.error()
    state = harness.registry_on_disk()[entry.id]
    assert state["enabled"] is True and state["pinned"] is True


def _commented(tmp_path: Path, packaging: str, trailing_commas: bool) -> Path:
    src = eb.commented_manifest_extension(tmp_path / "c", trailing_commas=trailing_commas)
    return src if packaging == "folder" else eb.write_crx3(src, tmp_path / "c.crx", eb.new_key())


@pytest.mark.parametrize("packaging", ["folder", "crx3"])
def test_manifest_with_comments_installs(harness, server, tmp_path, packaging):
    entry = harness.install_ok(_commented(tmp_path, packaging, trailing_commas=False), "Commented")
    assert entry.version == "1.0"
    assert content_script_runs(harness, server, "commented")


@pytest.mark.parametrize("packaging", ["folder", "crx3"])
def test_manifest_with_trailing_commas_is_refused_the_same_way(harness, tmp_path, packaging):
    """Chromium refuses trailing commas; Foxglove says so up front, however the extension was packaged."""
    kind, text = harness.install(_commented(tmp_path, packaging, trailing_commas=True))
    assert kind == "error" and text.startswith("The extension's manifest.json can't be read: "), text
    assert harness.entries() == [] and harness.leftover_staging() == []


def test_localized_name_is_shown(harness, tmp_path):
    kind, text = harness.install(eb.localized_extension(tmp_path / "l10n"))
    assert kind == "success" and text == "“Localized Probe” was added to Foxglove."
    entry = harness.wait_enabled_by_name("Localized Probe")
    assert entry.description == "Localized Probe description"


@pytest.mark.parametrize("packaging", ["folder", "zip"])
def test_manifest_v2_is_rejected_with_a_clear_message(harness, tmp_path, packaging):
    src = eb.mv2_extension(tmp_path / "mv2", "Old Timer")
    path = src if packaging == "folder" else eb.write_zip(src, tmp_path / "mv2.zip")
    kind, text = harness.install(path)
    assert kind == "error"
    assert text.startswith("“Old Timer” uses Manifest V2.") and "only runs Manifest V3" in text
    assert harness.entries() == [] and harness.leftover_staging() == []


def _bad_packages(tmp_path: Path) -> dict[str, tuple[Path, str]]:
    good = eb.probe_extension(tmp_path / "good", "Good", "good")
    readme_only = eb.write_tree(tmp_path / "readme-only", "{}", {})
    (readme_only / "manifest.json").unlink()
    (readme_only / "README.md").write_text("no manifest here")
    twins = tmp_path / "twins"
    eb.probe_extension(twins / "one", "One", "one")
    eb.probe_extension(twins / "two", "Two", "two")
    key = eb.new_key()

    def write(name: str, data: bytes) -> Path:
        (tmp_path / name).write_bytes(data)
        return tmp_path / name

    return {
        "text file": (write("hello.zip", b"hello world"), "This file isn't a Chrome extension (.crx or .zip)."),
        "empty file": (write("empty.crx", b""), "This file isn't a Chrome extension (.crx or .zip)."),
        "truncated zip": (write("cut.zip", eb.zip_bytes(good)[:80]), "The extension package is damaged or isn't a Chrome extension."),
        "crx3 header garbage": (write("garbage.crx", b"Cr24" + struct.pack("<II", 3, 12) + b"\x12\xff\xff\xff\x0f" + bytes(7)),
                                "The extension file is damaged (truncated field)."),
        "crx3 bad payload": (write("payload.crx", eb.crx3_bytes(b"definitely not a zip", key)),
                             "The extension package is damaged or isn't a Chrome extension."),
        "crx version 4": (write("v4.crx", b"Cr24" + struct.pack("<III", 4, 0, 0)), "Unsupported .crx version (4)."),
        "zip without manifest": (eb.write_zip(readme_only, tmp_path / "nomanifest.zip"), "The package doesn't contain a manifest.json file."),
        "zip with two extensions": (eb.write_zip(twins, tmp_path / "twins.zip"),
                                    "The package contains several extensions (one, two); unpack it and install one folder at a time."),
        "zip slip": (eb.write_zip(good, tmp_path / "slip.zip", extra={"../../escaped.js": b"evil()"}),
                     "The extension archive contains unsafe file paths."),
        "missing file": (tmp_path / "nowhere.crx", "The extension file couldn't be found."),
        "folder without manifest": (readme_only, "That folder doesn't contain a manifest.json file, so it isn't an unpacked extension."),
        "broken manifest": (eb.broken_manifest_extension(tmp_path / "broken"), "The extension's manifest.json can't be read: "),
        "broken manifest in zip": (eb.write_zip(eb.broken_manifest_extension(tmp_path / "broken2"), tmp_path / "broken.zip"),
                                   "The extension's manifest.json can't be read: "),
    }


@pytest.mark.parametrize("case", ["text file", "empty file", "truncated zip", "crx3 header garbage", "crx3 bad payload",
                                  "crx version 4", "zip without manifest", "zip with two extensions", "zip slip",
                                  "missing file", "folder without manifest", "broken manifest", "broken manifest in zip"])
def test_bad_packages_are_reported(harness, tmp_path, case):
    path, expected = _bad_packages(tmp_path)[case]
    kind, text = harness.install(path)
    assert kind == "error"
    assert text.startswith(expected), text
    assert harness.entries() == [] and harness.leftover_staging() == []
    assert not list(harness.dir.rglob("escaped.js")) and not (harness.staging.parent / "escaped.js").exists()


def test_engine_rejection_is_reported(harness, tmp_path):
    """Foxglove's own checks pass (MV3, readable manifest), but Chromium refuses it: no "version"."""
    src = eb.write_tree(tmp_path / "noversion", {"manifest_version": 3, "name": "No Version"}, {})
    kind, text = harness.install(src)
    assert kind == "error" and text.startswith("Couldn't install “No Version”: "), text
    assert len(text) > len("Couldn't install “No Version”: ") and "unknown error" not in text
    assert harness.entries() == [] and harness.leftover_staging() == []


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Running: content scripts, service worker, enable/disable
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_content_script_runs_on_a_local_page(harness, server, tmp_path):
    harness.install_ok(eb.probe_extension(tmp_path / "cs", "Scripter", "cs"), "Scripter")
    page = harness.fresh_page(server.page_url())
    assert wait_attr(page, "data-fg-cs") == "ran"
    assert run_js(page, "document.getElementById('content').textContent") == "plain page"  # page itself intact


def test_content_script_and_service_worker_message_each_other(harness, server, tmp_path):
    harness.install_ok(eb.probe_extension(tmp_path / "msg", "Messenger", "msg"), "Messenger")
    page = harness.fresh_page(server.page_url())
    assert wait_attr(page, "data-fg-msg-sw") == "pong-msg"     # runtime.sendMessage + sendResponse
    assert wait_attr(page, "data-fg-msg-port") == "hi-msg"     # runtime.connect port, both directions


def test_service_worker_can_push_to_the_tab(window, harness, server, tmp_path):
    """Qt gives extensions only chrome.tabs.update and no sender.tab; Foxglove's polyfill adds both (in its tabs)."""
    harness.install_ok(eb.probe_extension(tmp_path / "push", "Pusher", "push"), "Pusher")
    tab = window.current_tab()
    tab.load(QUrl(server.page_url()))
    status = json.loads(wait_attr(tab.page, "data-fg-push-push-status", 15))
    assert status == {"ok": True, "tabId": tab.tab_id}, status
    assert wait_attr(tab.page, "data-fg-push-push") == "pushed-push"


def test_disable_stops_content_script_and_enable_restores_it(harness, server, tmp_path):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "tog", "Toggler", "tog"), "Toggler")
    assert content_script_runs(harness, server, "tog")
    harness.controller.set_enabled(entry.id, False)
    harness.wait_state(entry.id, False)
    assert harness.registry_on_disk()[entry.id]["enabled"] is False
    assert content_script_absent(harness, server, "tog")
    harness.controller.set_enabled(entry.id, True)
    harness.wait_state(entry.id, True)
    assert harness.registry_on_disk()[entry.id]["enabled"] is True
    assert content_script_runs(harness, server, "tog")


def test_extension_pages_have_storage(harness, tmp_path):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "st", "Storer", "st"), "Storer")
    storage_set(harness, entry, "answer", 42)
    assert storage_get(harness, entry, "answer") == 42


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Browser window: toolbar buttons, pop-ups, options pages, add-ons manager
# ══════════════════════════════════════════════════════════════════════════════════════════
def toolbar_buttons(win) -> list:
    layout = win.extension_buttons
    return [layout.itemAt(i).widget() for i in range(layout.count()) if layout.itemAt(i).widget() is not None]


def test_popup_opens_from_toolbar_and_sizes_itself(window, harness, tmp_path, fg):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "pop", "Popper", "pop"), "Popper")
    assert entry.popup_url == ext_url(entry.id, "popup.html")
    button = wait_until(lambda: len(toolbar_buttons(window)) == 1 and toolbar_buttons(window)[0], message="toolbar button")
    assert button.toolTip() == "Popper"
    button.click()
    popup = wait_until(lambda: next((p for p in window.findChildren(fg.ExtensionPopup) if p.isVisible()), None),
                       message="the extension pop-up")
    assert poll_js(popup.page, "document.documentElement.getAttribute('data-popup-ready')", timeout=15) == "yes"
    assert popup.page.url() == entry.popup_url
    wait_until(lambda: popup.view.size() == QSize(*eb.POPUP_SIZE), 10,
               f"pop-up to size itself to {eb.POPUP_SIZE} (is {popup.view.size()})")
    assert run_js(popup.page, "typeof chrome.storage.local.get") == "function"
    popup.close()


def open_popup(window, harness, tmp_path, fg, name: str, tag: str, **files):
    """Install an extension whose pop-up is customised by *files* (see extbuilder.probe_files) and open it."""
    src = eb.write_tree(tmp_path / tag, eb.probe_manifest(name, tag), eb.probe_files(name, tag, **files))
    entry = harness.install_ok(src, name)
    window.open_extension(entry.id)
    popup = wait_until(lambda: next((p for p in window.findChildren(fg.ExtensionPopup) if p.isVisible()), None),
                       message="the extension pop-up")
    assert poll_js(popup.page, "document.documentElement.getAttribute('data-popup-ready')", timeout=15) == "yes"
    return entry, popup


def popup_gone(popup) -> bool:
    return sip.isdeleted(popup) or not popup.isVisible()


def test_popup_size_is_capped_like_chrome(window, harness, tmp_path, fg):
    _entry, popup = open_popup(window, harness, tmp_path, fg, "Huge", "huge", popup_size=(1000, 900))
    wait_until(lambda: popup.view.size() == QSize(800, 600), 10, f"pop-up capped at 800x600 (is {popup.view.size()})")


def test_popup_follows_content_that_grows_later(window, harness, tmp_path, fg):
    """Chrome keeps resizing a pop-up to its content; pop-ups that render after loading data rely on it."""
    _entry, popup = open_popup(window, harness, tmp_path, fg, "Grower", "grow",
                               popup_js="setTimeout(() => { document.body.style.height = '420px'; }, 3000);\n")
    wait_until(lambda: popup.view.size() == QSize(eb.POPUP_SIZE[0], eb.POPUP_SIZE[1]), 10, "initial size")
    poll_js(popup.page, "document.body.getBoundingClientRect().height", lambda h: h == 420, 8, "content to grow")
    try:
        wait_until(lambda: popup.view.size() == QSize(eb.POPUP_SIZE[0], 420), 4, "pop-up to grow with its content")
    except WaitTimeout:
        pytest.fail(f"content is now {eb.POPUP_SIZE[0]}x420 but the pop-up stayed {popup.view.size().width()}x"
                    f"{popup.view.size().height()} (it is only measured 0-2 s after loading)", pytrace=False)


def test_popup_window_close_closes_it(window, harness, tmp_path, fg):
    _entry, popup = open_popup(window, harness, tmp_path, fg, "Closer", "closer")
    popup.page.runJavaScript("window.close()")
    wait_until(lambda: popup_gone(popup), 10, "pop-up to close itself")


def test_popup_links_open_in_a_new_tab(window, harness, server, tmp_path, fg):
    target = server.url("/page?from-popup-link")
    _entry, popup = open_popup(window, harness, tmp_path, fg, "Linker", "link",
                               popup_body=f'<a id="link" href="{target}" target="_blank">site</a>')
    count = window.tab_bar.count()
    popup.page.runJavaScript("document.getElementById('link').click()")
    tab = wait_until(lambda: window.tab_bar.count() == count + 1 and window.tabs()[-1], 10, "a new tab for the link")
    wait_until(lambda: tab.url().toString() == target, 10, "the link to load in the new tab")
    wait_until(lambda: popup_gone(popup), 5, "pop-up to close")


def test_popup_tabs_update_navigates_the_current_tab(window, harness, server, tmp_path, fg):
    """chrome.tabs.update({url}) from a pop-up navigates the active tab in Chrome and the pop-up closes."""
    target = server.url("/page?from-tabs-update")
    _entry, popup = open_popup(window, harness, tmp_path, fg, "Navigator", "nav")
    tab = window.current_tab()
    popup.page.runJavaScript(f"chrome.tabs.update({{url: {json.dumps(target)}}})")
    landed = wait_until(lambda: ("tab" if tab.url().toString() == target else
                                 "pop-up" if not sip.isdeleted(popup) and popup.page.url().toString() == target else None),
                        8, "the navigation to land somewhere")
    assert landed == "tab", "chrome.tabs.update() navigated the pop-up itself instead of the current tab"
    wait_until(lambda: popup_gone(popup), 5, "pop-up to close")


def test_popup_can_open_the_options_page(window, harness, tmp_path, fg):
    """Qt answers openOptionsPage() with "Could not create an options page."; the polyfill opens a tab."""
    entry, popup = open_popup(window, harness, tmp_path, fg, "Settings Gear", "gear")
    count = window.tab_bar.count()
    popup.page.runJavaScript("chrome.runtime.openOptionsPage()")
    tab = wait_until(lambda: window.tab_bar.count() == count + 1 and window.tabs()[-1], 8, "an options tab")
    wait_until(lambda: tab.url() == entry.options_url, 8, "the options page")
    wait_until(lambda: popup_gone(popup), 5, "pop-up to close")


def test_toolbar_icon_comes_from_the_extension(window, harness, tmp_path):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "ico", "Iconic", "ico"), "Iconic")
    sizes = entry.icon.availableSizes()
    assert QSize(48, 48) in sizes and QSize(16, 16) in sizes, sizes
    button = wait_until(lambda: toolbar_buttons(window) and toolbar_buttons(window)[0], message="toolbar button")
    assert button.icon().cacheKey() == entry.icon.cacheKey() or QSize(16, 16) in button.icon().availableSizes()


@pytest.mark.parametrize("options", ["options_ui", "options_page"])
def test_options_page_url_and_opening(window, harness, tmp_path, options):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "opt", "Optioned", "opt", options=options), "Optioned")
    assert entry.options_url == ext_url(entry.id, "options.html")
    count = window.tab_bar.count()
    window.open_url(entry.options_url, "tab")
    tab = wait_until(lambda: window.tab_bar.count() == count + 1 and window.current_tab(), message="options tab")
    wait_until(lambda: tab.page.title() == "Optioned Options", 15, "options page title")
    assert run_js(tab.page, "document.getElementById('options').textContent") == "Options for Optioned"


def test_no_options_page_means_empty_url(harness, tmp_path):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "noopt", "Plain", "plain", options=None), "Plain")
    assert entry.options_url.isEmpty()


def test_toolbar_click_without_popup_opens_options_or_explains(window, harness, tmp_path):
    with_options = harness.install_ok(eb.probe_extension(tmp_path / "a", "No Popup", "nopop", popup=False), "No Popup")
    assert with_options.popup_url.isEmpty()
    count = window.tab_bar.count()
    window.open_extension(with_options.id)
    tab = wait_until(lambda: window.tab_bar.count() == count + 1 and window.current_tab(), message="options tab")
    wait_until(lambda: tab.page.title() == "No Popup Options", 15, "options page title")
    bare = harness.install_ok(eb.probe_extension(tmp_path / "b", "Bare", "bare", popup=False, options=None), "Bare")
    window.open_extension(bare.id)
    assert window.content.toast.label.text() == "“Bare” has no pop-up — it works on web pages automatically."


def test_pin_and_unpin_control_toolbar_buttons(window, harness, tmp_path):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "pin", "Pinny", "pin"), "Pinny")
    wait_until(lambda: [b.toolTip() for b in toolbar_buttons(window)] == ["Pinny"], message="pinned button")
    harness.controller.set_pinned(entry.id, False)
    wait_until(lambda: toolbar_buttons(window) == [], message="button removed after unpin")
    assert harness.registry_on_disk()[entry.id]["pinned"] is False and harness.entry(entry.id).pinned is False
    harness.controller.set_pinned(entry.id, True)
    wait_until(lambda: len(toolbar_buttons(window)) == 1, message="button back after pin")
    harness.controller.set_enabled(entry.id, False)  # disabled extensions never get a button
    wait_until(lambda: toolbar_buttons(window) == [], message="button removed while disabled")
    harness.controller.set_enabled(entry.id, True)
    wait_until(lambda: len(toolbar_buttons(window)) == 1, message="button back after enabling")
    menu = window.extensions_button.menu()
    window._fill_extensions_menu(menu)
    assert "Pinny" in [a.text() for a in menu.actions()]


def test_extensions_dialog_toggles_and_opens_options(window, harness, tmp_path, fg):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "dlg", "Dialogged", "dlg"), "Dialogged")
    window.show_extensions()
    dialog = window._dialogs["extensions"]

    def rows():
        holder = dialog.area.widget()
        return holder.findChildren(fg.ExtensionRow) if holder is not None else []

    def box(row, text):
        return next(b for b in row.findChildren(QCheckBox) if b.text() == text)

    row = wait_until(lambda: len(rows()) == 1 and rows()[0], message="one extension row")
    assert box(row, "Enabled").isChecked() and box(row, "Show in toolbar").isChecked()
    box(row, "Enabled").click()
    harness.wait_state(entry.id, False)
    row = wait_until(lambda: len(rows()) == 1 and rows()[0] is not row and rows()[0], message="rebuilt row")
    assert not box(row, "Enabled").isChecked()
    wait_until(lambda: toolbar_buttons(window) == [], message="button gone while disabled")
    box(row, "Enabled").click()
    harness.wait_state(entry.id, True)
    row = wait_until(lambda: len(rows()) == 1 and box(rows()[0], "Enabled").isChecked() and rows()[0], message="re-enabled row")
    count = window.tab_bar.count()
    next(b for b in row.findChildren(QPushButton) if b.text() == "Options").click()
    tab = wait_until(lambda: window.tab_bar.count() == count + 1 and window.current_tab(), message="options tab")
    wait_until(lambda: tab.page.title() == "Dialogged Options", 15, "options page title")
    dialog.close()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Chrome Web Store
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.fixture
def local_store(fg, server, monkeypatch):
    """The Chrome Web Store's update service, played by the local server (Foxglove reads WEBSTORE_CRX_URL)."""
    monkeypatch.setattr(fg, "WEBSTORE_CRX_URL", server.url("/service/update2/crx?response=redirect&prodversion={version}"
                                                           "&acceptformat=crx2,crx3&x=id%3D{id}%26uc"))
    return server


def test_webstore_download_url(fg):
    url = QUrl(fg.WEBSTORE_CRX_URL.format(version="140.0.1.2", id=UBO))
    assert (url.scheme(), url.host(), url.path()) == ("https", "clients2.google.com", "/service/update2/crx")
    query = url.query(QUrl.ComponentFormattingOption.FullyEncoded)
    assert f"x=id%3D{UBO}%26uc" in query and "response=redirect" in query and "acceptformat=crx2,crx3" in query
    assert "prodversion=140.0.1.2" in query


def _store_crx(server, tmp_path: Path, name: str, tag: str, key=None, served_as: str | None = None):
    key = key or eb.new_key()
    src = eb.probe_extension(tmp_path / tag, name, tag)
    crx = eb.write_crx3(src, tmp_path / f"{tag}.crx", key, extra={"_metadata/verified_contents.json": b"[]"})
    server.add(f"/crx/{served_as or key.ext_id}.crx", crx.read_bytes(), "application/x-chrome-extension")
    return key


def test_webstore_install_through_local_update_service(harness, local_store, tmp_path, fg):
    server = local_store
    key = _store_crx(server, tmp_path, "Store Probe", "store")
    start = len(harness.messages)
    harness.controller.install_from_webstore(key.ext_id, "Store Probe")
    kind, text = harness.wait_message(start)
    assert kind == "success" and text == "“Store Probe” was added to Foxglove.", text
    assert ("info", "Downloading Store Probe from the Chrome Web Store…") in harness.messages[start:]
    asked = next(r for r in server.requests if r.startswith("/service/update2/crx"))
    assert f"x=id%3D{key.ext_id}%26uc" in asked and f"prodversion={fg.qWebEngineChromiumVersion()}" in asked
    entry = harness.wait_state(key.ext_id, True)
    assert entry.name == "Store Probe"
    assert not (Path(entry.path) / "_metadata").exists()
    assert harness.registry_on_disk()[key.ext_id]["source"] == "webstore"


def test_webstore_download_of_the_wrong_extension_is_refused(harness, local_store, tmp_path):
    server = local_store
    wanted = eb.new_key().ext_id
    _store_crx(server, tmp_path, "Impostor", "impostor", served_as=wanted)  # signed by another key
    start = len(harness.messages)
    harness.controller.install_from_webstore(wanted, "Wanted")
    kind, text = harness.wait_message(start)
    assert kind == "error" and text == "The downloaded file doesn't match the requested extension."
    assert harness.entries() == [] and harness.leftover_staging() == []


@pytest.mark.parametrize("response, expected", [
    (None, "Couldn't download Ghost: "),                                        # 404 from the store
    (b"", "The Chrome Web Store didn't provide Ghost (HTTP 200)."),              # empty answer
    (b"<!doctype html><title>Consent</title>", "This file isn't a Chrome extension (.crx or .zip)."),  # a web page
])
def test_webstore_download_failure_is_reported(harness, local_store, response, expected):
    server = local_store
    missing = eb.new_key().ext_id
    if response is not None:
        server.add(f"/crx/{missing}.crx", response, "text/html")
    start = len(harness.messages)
    harness.controller.install_from_webstore(missing, "Ghost")
    kind, text = harness.wait_message(start)
    assert kind == "error" and text.startswith(expected), text
    assert harness.entries() == []


def test_webstore_reinstall_updates_in_place(harness, local_store, tmp_path):
    server = local_store
    key = _store_crx(server, tmp_path, "Store Update", "storeupd")
    start = len(harness.messages)
    harness.controller.install_from_webstore(key.ext_id, "Store Update")
    assert harness.wait_message(start)[0] == "success"
    first = harness.wait_state(key.ext_id, True)
    storage_set(harness, first, "marker", "kept")
    src = eb.probe_extension(tmp_path / "storeupd-2", "Store Update", "storeupd", version="2.0")
    server.add(f"/crx/{key.ext_id}.crx", eb.crx3_bytes(eb.zip_bytes(src), key), "application/x-chrome-extension")
    start = len(harness.messages)
    harness.controller.install_from_webstore(key.ext_id, "Store Update")
    kind, text = harness.wait_message(start)
    assert kind == "success" and text == "“Store Update” was updated.", text
    after = wait_until(lambda: (e := harness.entry(key.ext_id)) is not None and e.version == "2.0" and e.enabled and e,
                       message="version 2.0 enabled")
    assert len(harness.entries()) == 1 and storage_get(harness, after, "marker") == "kept"


def test_webstore_info_bar_offers_install_and_installs(window, harness, local_store, tmp_path):
    server = local_store
    key = _store_crx(server, tmp_path, "Bar Probe", "barprobe")
    tab = window.current_tab()
    store_url = QUrl(f"https://chromewebstore.google.com/detail/bar-probe/{key.ext_id}?hl=en")
    window._update_webstore_bar(tab, store_url)  # what urlChanged does for a Web Store page
    bar = tab.webstore_bar
    assert bar is not None and bar.property("ext_id") == key.ext_id
    assert "Install this extension in Foxglove?" in bar.text.text()
    add = next(b for b in bar.findChildren(QPushButton) if b.text() == "Add to Foxglove")
    start = len(harness.messages)
    add.click()
    kind, text = harness.wait_message(start)
    assert kind == "success", text
    harness.wait_state(key.ext_id, True)
    window._update_webstore_bar(tab, store_url)
    bar = wait_until(lambda: tab.webstore_bar is not None and tab.webstore_bar.isVisible() and tab.webstore_bar,
                     message="info bar for the installed extension")
    assert bar.text.text() == "This extension is installed in Foxglove."
    assert [b.text() for b in bar.findChildren(QPushButton)] == ["Reinstall / Update"]
    window._update_webstore_bar(tab, QUrl("https://example.com/"))  # leaving the store page removes the bar
    assert tab.webstore_bar is None


def test_webstore_info_bar_on_real_navigation(window, harness):
    """Loads a real Web Store URL in a tab (the network may refuse it - the address still commits)."""
    tab = window.current_tab()
    tab.load(QUrl(f"https://chromewebstore.google.com/detail/ublock-origin-lite/{UBO}"))
    bar = wait_until(lambda: tab.webstore_bar, 30, "the Web Store info bar")
    assert bar.property("ext_id") == UBO


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Updating, removing, duplicates
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_reinstalling_a_folder_updates_in_place_and_keeps_data(harness, server, tmp_path):
    src = eb.probe_extension(tmp_path / "upd", "Updater", "upd", version="1.0")
    first = harness.install_ok(src, "Updater")
    storage_set(harness, first, "marker", "kept")
    eb.write_tree(src, eb.probe_manifest("Updater", "upd", version="2.0"))
    kind, text = harness.install(src)
    assert kind == "success" and text == "“Updater” was updated.", text
    after = wait_until(lambda: (e := harness.entry(first.id)) is not None and e.version == "2.0" and e.enabled and e,
                       message="version 2.0 enabled under the same ID")
    assert [e.id for e in harness.entries()] == [first.id]
    assert after.path == first.path
    assert storage_get(harness, after, "marker") == "kept"
    assert content_script_runs(harness, server, "upd")
    assert harness.leftover_staging() == [] and not list(harness.staging.glob("previous-*"))


def test_crx_with_the_same_key_updates_in_place(harness, tmp_path):
    key = eb.new_key()
    v1 = eb.write_crx3(eb.probe_extension(tmp_path / "v1", "Signed", "signed", version="1.0"), tmp_path / "v1.crx", key)
    v2 = eb.write_crx3(eb.probe_extension(tmp_path / "v2", "Signed", "signed", version="2.0"), tmp_path / "v2.crx", key)
    first = harness.install_ok(v1, "Signed")
    assert first.id == key.ext_id
    storage_set(harness, first, "marker", "kept")
    kind, text = harness.install(v2)
    assert kind == "success" and text == "“Signed” was updated.", text
    after = wait_until(lambda: (e := harness.entry(key.ext_id)) is not None and e.version == "2.0" and e.enabled and e,
                       message="version 2.0 enabled")
    assert len(harness.entries()) == 1
    assert storage_get(harness, after, "marker") == "kept"


def test_update_keeps_a_disabled_extension_disabled(harness, server, tmp_path):
    src = eb.probe_extension(tmp_path / "off", "Sleeper", "off")
    entry = harness.install_ok(src, "Sleeper")
    harness.controller.set_enabled(entry.id, False)
    harness.wait_state(entry.id, False)
    eb.write_tree(src, eb.probe_manifest("Sleeper", "off", version="1.1"))
    kind, text = harness.install(src)
    assert kind == "success" and text == "“Sleeper” was updated.", text
    wait_until(lambda: harness.entry(entry.id) is not None and harness.entry(entry.id).version == "1.1", message="update")
    spin(1.0)  # Foxglove re-applies the saved state 0 and 500 ms after the update
    assert harness.entry(entry.id).enabled is False
    assert content_script_absent(harness, server, "off")


def test_failed_update_keeps_the_previous_version(harness, server, tmp_path):
    src = eb.probe_extension(tmp_path / "keep", "Keeper", "keep")
    entry = harness.install_ok(src, "Keeper")
    manifest = eb.probe_manifest("Keeper", "keep", version="2.0")
    del manifest["version"]  # passes Foxglove's checks, Chromium refuses to load it
    eb.write_tree(src, manifest)
    kind, text = harness.install(src)
    assert kind == "error" and text.startswith("Couldn't update “Keeper”: ") and text.endswith("The previous version was kept."), text
    kept = harness.wait_state(entry.id, True)
    assert kept.version == "1.0" and kept.path == entry.path
    assert content_script_runs(harness, server, "keep")


def test_uninstall_removes_files_and_registry_entry(window, harness, server, tmp_path, fg, monkeypatch):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "rm", "Remover", "rm"), "Remover")
    wait_until(lambda: len(toolbar_buttons(window)) == 1, message="toolbar button")
    assert entry.id in harness.registry_on_disk()
    asked = []
    monkeypatch.setattr(fg, "ask_question", lambda *a, **_k: asked.append(a[1:3]) or True)
    start = len(harness.messages)
    window.confirm_remove_extension(entry.id, entry.name)
    assert asked == [("Remove Extension", "Remove “Remover” from Foxglove?")]
    kind, text = harness.wait_message(start)
    assert kind == "success" and text == "“Remover” was removed.", text
    assert harness.entry(entry.id) is None and harness.entries() == []
    assert entry.id not in harness.controller.registry and entry.id not in harness.registry_on_disk()
    wait_until(lambda: not Path(entry.path).exists(), 10, "the installed folder to be deleted")
    wait_until(lambda: toolbar_buttons(window) == [], message="toolbar button removed")
    assert content_script_absent(harness, server, "rm")


def test_concurrent_installs_of_one_folder_are_deduplicated(harness, tmp_path):
    src = eb.probe_extension(tmp_path / "dup", "Twice", "dup")
    start = len(harness.messages)
    harness.controller.install_from_path(str(src))
    harness.controller.install_from_path(str(src))
    harness.wait_message(start)
    harness.wait_enabled_by_name("Twice")
    spin(2.0)
    messages = harness.messages[start:]
    assert len(harness.by_name("Twice")) == 1, harness.entries()
    assert ("info", "“Twice” is already being installed.") in messages, messages
    assert [m for m in messages if m[0] != "info"] == [("success", "“Twice” was added to Foxglove.")], messages
    assert harness.leftover_staging() == []


def test_concurrent_installs_of_one_crx_are_deduplicated(harness, tmp_path):
    key = eb.new_key()
    crx = eb.write_crx3(eb.probe_extension(tmp_path / "dupcrx", "Twice Signed", "dupcrx"), tmp_path / "dup.crx", key)
    copy = tmp_path / "copy-of-dup.crx"  # same extension (same ID) from a different file
    copy.write_bytes(crx.read_bytes())
    start = len(harness.messages)
    harness.controller.install_from_path(str(crx))
    harness.controller.install_from_path(str(copy))
    harness.wait_message(start)
    harness.wait_state(key.ext_id, True)
    spin(2.0)
    messages = harness.messages[start:]
    assert [e.id for e in harness.entries()] == [key.ext_id]
    assert ("info", "“Twice Signed” is already being installed.") in messages, messages
    assert [m for m in messages if m[0] != "info"] == [("success", "“Twice Signed” was added to Foxglove.")], messages


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Restart
# ══════════════════════════════════════════════════════════════════════════════════════════
def _run_phase(phase: str, args: dict) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "FOXGLOVE_TEST_ROOT"}
    proc = subprocess.run([sys.executable, str(Path(__file__).with_name("ext_phase.py")), phase, json.dumps(args)],
                          env=env, capture_output=True, text=True, timeout=150)
    line = next((ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")), None)
    assert line is not None, f"phase {phase} printed no result (exit {proc.returncode}):\n{proc.stdout}\n{proc.stderr[-3000:]}"
    result = json.loads(line[len("RESULT "):])
    assert "error" not in result, f"phase {phase} failed: {result}"
    assert not result["slot_errors"], "\n".join(result["slot_errors"])
    return result


def _restart_args(server, tmp_path: Path) -> dict:
    exts = [{"name": f"Persist {c}", "tag": f"p{c.lower()}", "path": str(eb.probe_extension(tmp_path / c, f"Persist {c}", f"p{c.lower()}"))}
            for c in "ABC"]
    return {"root": str(tmp_path / "run"), "profile": "persist", "page_url": server.url("/page?restart"), "exts": exts,
            "disable": ["Persist B"], "unpin": ["Persist A"], "storage": {"Persist A": "yes"},
            "enabled": ["Persist A", "Persist C"]}


def test_state_survives_a_restart(server, tmp_path):
    args = _restart_args(server, tmp_path)
    before = _run_phase("install", args)
    assert [before["entries"][f"Persist {c}"]["enabled"] for c in "ABC"] == [True, False, True]
    after = _run_phase("report", args)
    assert "wait_error" not in after, after
    for c in "ABC":
        assert after["entries"][f"Persist {c}"]["id"] == before["entries"][f"Persist {c}"]["id"]
    state = {name: (e["enabled"], e["pinned"]) for name, e in after["entries"].items()}
    assert state == {"Persist A": (True, False), "Persist B": (False, True), "Persist C": (True, True)}
    assert after["toolbar"] == ["Persist C"]  # A is unpinned, B is disabled
    assert after["content_scripts"] == {"Persist A": "ran", "Persist B": None, "Persist C": "ran"}
    assert after["storage"] == {"Persist A": "yes"}


@pytest.mark.parametrize("state", ["marker", "note", "half-swapped", "note-only"])
def test_interrupted_update_is_rolled_back_on_restart(server, tmp_path, state):
    """Foxglove quit (or crashed) in the middle of swapping an extension's folder for an update: the next start
    must put the previous version back (_recover_interrupted_updates), whichever step it stopped at."""
    args = _restart_args(server, tmp_path)
    before = _run_phase("install", args)
    target = Path(before["entries"]["Persist A"]["path"])
    staging = tmp_path / "run" / "data" / "Profiles" / "persist" / "extension-staging"
    backup = staging / "previous-0123456789"
    staging.mkdir(parents=True, exist_ok=True)
    if state != "note-only":  # note-only: it stopped right after writing the note
        target.rename(backup)
    if state == "marker":  # how older Foxglove versions marked the backup
        (backup / ".foxglove-restore-to").write_text(str(target), encoding="utf-8")
    else:
        backup.with_name(backup.name + ".restore").write_text(str(target), encoding="utf-8")
    if state == "half-swapped":  # part of the new version had already been moved in
        eb.write_tree(target, '{"manifest_version": 3, "name": "Half"', {"junk.js": "//"})
    after = _run_phase("report", args)
    assert target.is_dir() and not backup.exists() and not list(staging.glob("previous-*"))
    assert "wait_error" not in after, after
    a = after["entries"].get("Persist A")
    assert a is not None and a["id"] == before["entries"]["Persist A"]["id"] and a["enabled"], after["entries"]
    assert after["content_scripts"]["Persist A"] == "ran"
    assert after["storage"] == {"Persist A": "yes"}
