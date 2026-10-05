"""Regression tests for the confirmed extension defects (signatures, replacing, updates, pop-ups, names, New Tab)."""
from __future__ import annotations

import hashlib
import io
import json
import struct
import zipfile
from pathlib import Path

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding
from PyQt6.QtCore import QSize, QUrl
from PyQt6.QtWidgets import QPushButton

import extbuilder as eb
from helpers import poll_js, run_js, spin, wait_until
from test_extensions import content_script_runs, open_popup, popup_gone, storage_get, storage_set


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Package signatures: an ID is only taken from a key whose signature verifies
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_signature_checks(fg):
    rsa_key, message = eb.new_key(), b"signed data" * 100
    good = rsa_key.private.sign(message, padding.PKCS1v15(), hashes.SHA256())
    assert fg.verify_signature(rsa_key.public_der, good, message)
    assert not fg.verify_signature(rsa_key.public_der, good, message + b"!")
    assert not fg.verify_signature(eb.new_key().public_der, good, message)
    assert fg.verify_signature(rsa_key.public_der, rsa_key.private.sign(message, padding.PKCS1v15(), hashes.SHA1()), message, "sha1")
    ec_key = ec.generate_private_key(ec.SECP256R1())
    ec_der = ec_key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    ec_sig = ec_key.sign(message, ec.ECDSA(hashes.SHA256()))
    assert fg.verify_signature(ec_der, ec_sig, message)
    assert not fg.verify_signature(ec_der, ec_sig, message[:-1])
    assert not fg.verify_signature(b"garbage", good, message)


def _crx3_with_proof(archive: bytes, claimed_der: bytes, signer, field: int = 2) -> bytes:
    """A CRX3 whose header names *claimed_der* as its key, signed by *signer* (RSA, or EC for field 3)."""
    signed = eb._field(1, hashlib.sha256(claimed_der).digest()[:16])
    message = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed)) + signed + archive
    signature = (signer.sign(message, ec.ECDSA(hashes.SHA256())) if field == 3
                 else signer.sign(message, padding.PKCS1v15(), hashes.SHA256()))
    header = eb._field(field, eb._field(1, claimed_der) + eb._field(2, signature)) + eb._field(10000, signed)
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive


@pytest.mark.parametrize("case", ["tampered archive", "someone else's key", "crx2 tampered"])
def test_packages_with_bad_signatures_are_refused(harness, tmp_path, case):
    archive = eb.zip_bytes(eb.probe_extension(tmp_path / "src", "Forged", "forged"))
    victim, attacker = eb.new_key(), eb.new_key()
    data = {
        "tampered archive": lambda: (lambda d: d[:-30] + bytes([d[-30] ^ 1]) + d[-29:])(eb.crx3_bytes(archive, victim)),
        "someone else's key": lambda: _crx3_with_proof(archive, victim.public_der, attacker.private),
        "crx2 tampered": lambda: (lambda d: d[:-30] + bytes([d[-30] ^ 1]) + d[-29:])(eb.crx2_bytes(archive, victim)),
    }[case]()
    (tmp_path / "forged.crx").write_bytes(data)
    kind, text = harness.install(tmp_path / "forged.crx")
    assert kind == "error" and text == "The extension file's signature doesn't verify, so it can't be installed.", text
    assert harness.entries() == []


def test_ecdsa_signed_package_installs(harness, tmp_path):
    key = ec.generate_private_key(ec.SECP256R1())
    der = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    archive = eb.zip_bytes(eb.probe_extension(tmp_path / "src", "Elliptic", "ecdsa"))
    (tmp_path / "ec.crx").write_bytes(_crx3_with_proof(archive, der, key, field=3))
    entry = harness.install_ok(tmp_path / "ec.crx", "Elliptic")
    assert entry.id == eb.extension_id(der)


def test_crx_key_wins_over_a_stale_manifest_key(harness, tmp_path):
    key, stale = eb.new_key(), eb.new_key()
    src = eb.probe_extension(tmp_path / "src", "Stale Key", "stale", key=stale.manifest_key)
    entry = harness.install_ok(eb.write_crx3(src, tmp_path / "s.crx", key), "Stale Key")
    assert entry.id == key.ext_id  # Chrome's rule: the signed key decides
    assert json.loads((Path(entry.path) / "manifest.json").read_text())["key"] == key.manifest_key


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Replacing an installed extension with a package that only *claims* its ID
# ══════════════════════════════════════════════════════════════════════════════════════════
def _keyed_pair(tmp_path: Path):
    key = eb.new_key()
    first = eb.probe_extension(tmp_path / "first", "Keyed", "keyed", key=key.manifest_key)
    other = eb.write_zip(eb.probe_extension(tmp_path / "other", "Keyed", "keyed", version="9.9", key=key.manifest_key),
                         tmp_path / "other.zip")
    return key, first, other


def test_copied_key_cant_silently_replace_an_extension(harness, tmp_path):
    key, first, other = _keyed_pair(tmp_path)
    entry = harness.install_ok(first, "Keyed")
    assert entry.id == key.ext_id
    storage_set(harness, entry, "secret", "mine")
    kind, text = harness.install(other)  # no window to ask in: refused
    assert kind == "error" and text == "“Keyed” is already installed from somewhere else. Remove it first to install this copy."
    assert harness.entry(entry.id).version == "1.0" and harness.leftover_staging() == []
    kind, text = harness.install(first)  # the same folder again: an ordinary update, no question
    assert kind == "success" and text == "“Keyed” was updated."


@pytest.mark.parametrize("answer", [False, True])
def test_replacing_asks_first(window, harness, tmp_path, fg, monkeypatch, answer):
    key, first, other = _keyed_pair(tmp_path)
    entry = harness.install_ok(first, "Keyed")
    storage_set(harness, entry, "secret", "mine")
    asked = []
    monkeypatch.setattr(fg, "ask_question", lambda *a, **_k: asked.append(a[1:3]) or answer)
    start = len(harness.messages)
    harness.controller.install_from_path(str(other))
    kind, text = harness.wait_message(start, kinds=("success", "info") if not answer else ("success",))
    assert asked and asked[0][0] == "Replace Extension" and str(other) in asked[0][1]
    assert "gets access to everything the installed one has saved" in asked[0][1]
    if answer:
        assert text == "“Keyed” was updated."
        updated = wait_until(lambda: (e := harness.entry(entry.id)) is not None and e.version == "9.9" and e.enabled and e)
        assert storage_get(harness, updated, "secret") == "mine"
    else:
        assert (kind, text) == ("info", "“Keyed” was left as it was.")
        assert harness.entry(entry.id).version == "1.0"
    assert harness.leftover_staging() == []


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Updates: rollback that can't put the old version back, removal while updating
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_failed_restore_keeps_the_previous_version_for_the_next_start(harness, tmp_path, fg, monkeypatch):
    src = eb.probe_extension(tmp_path / "keep", "Keeper", "keep")
    entry = harness.install_ok(src, "Keeper")
    manifest = eb.probe_manifest("Keeper", "keep", version="2.0")
    del manifest["version"]  # Chromium refuses to load it, so the update rolls back
    eb.write_tree(src, manifest)
    real_move = fg._move

    def move(source: Path, target: Path) -> None:
        if source.name.startswith("previous-"):
            raise OSError("the folder is in use")  # e.g. a file lock on Windows
        real_move(source, target)
    monkeypatch.setattr(fg, "_move", move)
    kind, text = harness.install(src)
    assert kind == "error" and "couldn't be put back yet - it will be when Foxglove restarts" in text, text
    backups = list(harness.staging.glob("previous-*"))
    assert len([b for b in backups if b.is_dir()]) == 1 and len([b for b in backups if b.suffix == ".restore"]) == 1
    harness.controller._clean_staging()  # what the next start does first: the backup must survive it
    assert len(list(harness.staging.glob("previous-*"))) == 2
    monkeypatch.setattr(fg, "_move", real_move)
    assert harness.controller._recover_interrupted_updates() == [entry.path]
    harness.manager.loadExtension(entry.path)
    restored = wait_until(lambda: (e := harness.entry(entry.id)) is not None and e.enabled and e, message="v1 back")
    assert restored.version == "1.0" and not list(harness.staging.glob("previous-*"))


def test_removing_during_an_update_happens_after_it(harness, tmp_path, monkeypatch):
    src = eb.probe_extension(tmp_path / "busy", "Busy", "busy")
    entry = harness.install_ok(src, "Busy")
    eb.write_tree(src, eb.probe_manifest("Busy", "busy", version="2.0"))
    start, controller = len(harness.messages), harness.controller
    real_start = controller._start_update

    def start_update(info, job) -> None:  # "Remove" clicked while the update is on its way
        real_start(info, job)
        controller.uninstall(entry.id)
    monkeypatch.setattr(controller, "_start_update", start_update)
    controller.install_from_path(str(src))
    wait_until(lambda: len(harness.messages) > start, message="the update")
    assert harness.messages[start] == ("info", "“Busy” will be removed once its update finishes.")
    wait_until(lambda: ("success", "“Busy” was removed.") in harness.messages[start:], 20, "the removal")
    assert ("success", "“Busy” was updated.") in harness.messages[start:]
    assert harness.entries() == [] and entry.id not in harness.registry_on_disk()


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Zip edge cases
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_zip_made_on_windows_installs(harness, server, tmp_path):
    src = eb.probe_extension(tmp_path / "win", "Windowsy", "winzip", icons={"16": "icons/16.png"})
    (src / "icons").mkdir()
    (src / "icons" / "16.png").write_bytes(eb.tiny_png(16))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.writestr(path.relative_to(src).as_posix().replace("/", "\\"), path.read_bytes())
    (tmp_path / "win.zip").write_bytes(buf.getvalue())
    entry = harness.install_ok(tmp_path / "win.zip", "Windowsy")
    assert (Path(entry.path) / "icons" / "16.png").is_file()
    assert content_script_runs(harness, server, "winzip")


def test_zip_bomb_is_refused(harness, tmp_path):
    src = eb.probe_extension(tmp_path / "bomb", "Bomb", "bomb")
    eb.write_zip(src, tmp_path / "bomb.zip", extra={"padding.bin": bytes(64 * 1024 * 1024)})  # compresses ~1000:1
    kind, text = harness.install(tmp_path / "bomb.zip")
    assert (kind, text) == ("error", "The extension package is too large to install.")
    assert harness.leftover_staging() == []


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Pages of a disabled extension, names that look like HTML, New Tab pages
# ══════════════════════════════════════════════════════════════════════════════════════════
def test_extension_pages_get_their_apis_back_when_switched_on(window, harness, tmp_path, fg):
    entry = harness.install_ok(eb.probe_extension(tmp_path / "opt", "Optional", "opt"), "Optional")
    harness.controller.set_enabled(entry.id, False)
    harness.wait_state(entry.id, False)
    row = fg.ExtensionRow(harness.entry(entry.id), window)
    options = next(b for b in row.findChildren(QPushButton) if b.text() == "Options")
    assert not options.isEnabled()  # its page would load without chrome.* APIs
    row.deleteLater()
    tab = window.new_tab(entry.options_url)
    wait_until(lambda: tab.page.title() == "Optional Options", 15, "the options page")
    assert run_js(tab.page, "typeof chrome.storage") == "undefined"
    harness.controller.set_enabled(entry.id, True)
    assert poll_js(tab.page, "typeof chrome.storage", lambda v: v == "object", 15) == "object"  # reloaded for us


def test_html_in_extension_names_stays_text(window, harness, tmp_path, fg):
    name = "<b>Bold</b> & <img src=x>"
    harness.install_ok(eb.probe_extension(tmp_path / "html", name, "html"), name)
    button = wait_until(lambda: window.findChildren(fg.ExtensionButton) and window.findChildren(fg.ExtensionButton)[0])
    assert button.toolTip() == "<span>&lt;b&gt;Bold&lt;/b&gt; &amp; &lt;img src=x&gt;</span>"
    assert fg.plain_tip("Tabs & Windows") == "Tabs & Windows"


def test_new_tab_extension_is_used_for_new_tabs(window, harness, tmp_path, fg):
    files = eb.probe_files("Fresh Tab", "ntp")
    files["newtab.html"] = '<!doctype html><html><head><meta charset="utf-8"><title>My New Tab</title></head><body>hi</body></html>'
    manifest = eb.probe_manifest("Fresh Tab", "ntp", chrome_url_overrides={"newtab": "newtab.html"})
    entry = harness.install_ok(eb.write_tree(tmp_path / "ntp", manifest, files), "Fresh Tab")
    assert entry.newtab_url == QUrl(f"chrome-extension://{entry.id}/newtab.html")
    window.open_new_tab()
    tab = window.current_tab()
    wait_until(lambda: tab.page.title() == "My New Tab", 15, "the extension's New Tab page")
    assert window.url_bar.text() == ""  # like Foxglove's own New Tab page
    assert poll_js(tab.page, "typeof chrome.storage") == "object"
    harness.controller.set_enabled(entry.id, False)
    harness.wait_state(entry.id, False)
    assert window._home_url() == QUrl(fg.NEWTAB)


# ══════════════════════════════════════════════════════════════════════════════════════════
#  Pop-ups: sizing, permissions, leaving the extension
# ══════════════════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("body, expected", [
    ('<div id="app" style="width: 520px">wide content</div>', 536),     # wider than the starting view: no clipping
    ('<div style="width: 150px">narrow</div>', 166),                     # shrink-wraps like Chrome
])
def test_popup_width_follows_content_without_creeping(window, harness, tmp_path, fg, body, expected):
    files = eb.probe_files("Sized", "sized")
    files["popup.html"] = (f'<!doctype html><html><head><meta charset="utf-8"></head><body>{body}'
                           '<script src="popup.js"></script></body></html>')
    entry = harness.install_ok(eb.write_tree(tmp_path / "sized", eb.probe_manifest("Sized", "sized"), files), "Sized")
    window.open_extension(entry.id)
    popup = wait_until(lambda: next((p for p in window.findChildren(fg.ExtensionPopup) if p.isVisible()), None))
    wait_until(lambda: popup.view.size().width() == expected, 10, f"width {expected} (is {popup.view.size()})")
    spin(1.5)  # the measurements keep coming: the width must not drift
    assert popup.view.size().width() == expected
    assert run_js(popup.page, "document.documentElement.scrollWidth <= innerWidth")  # nothing cut off


def test_popup_shrinks_with_its_content(window, harness, tmp_path, fg):
    _entry, popup = open_popup(window, harness, tmp_path, fg, "Shrinker", "shrink",
                               popup_js="setTimeout(() => { document.body.style.height = '100px'; }, 1500);\n")
    wait_until(lambda: popup.view.size() == QSize(*eb.POPUP_SIZE), 10, "initial size")
    wait_until(lambda: popup.view.size() == QSize(eb.POPUP_SIZE[0], 100), 6, f"the pop-up to shrink (is {popup.view.size()})")


def test_popup_permission_request_is_asked_in_the_window(window, harness, tmp_path, fg, monkeypatch):
    asked = []
    monkeypatch.setattr(window, "ask_permission_modal", lambda parent, permission: asked.append((parent, permission.permissionType())))
    _entry, popup = open_popup(window, harness, tmp_path, fg, "Asker", "ask")
    popup.page.runJavaScript("Notification.requestPermission()")
    wait_until(lambda: asked, 10, "the permission question")
    assert asked[0] == (window, fg.QWebEnginePermission.PermissionType.Notifications)
    wait_until(lambda: popup_gone(popup), 5, "the pop-up to close")


def test_popup_navigating_away_opens_in_the_tab(window, harness, server, tmp_path, fg):
    target = server.url("/page?left-popup")
    _entry, popup = open_popup(window, harness, tmp_path, fg, "Leaver", "leave")
    tab = window.current_tab()
    popup.page.runJavaScript(f"location.href = {json.dumps(target)}")
    wait_until(lambda: tab.url().toString() == target, 10, "the page to open in the current tab")
    wait_until(lambda: popup_gone(popup), 5, "the pop-up to close")
