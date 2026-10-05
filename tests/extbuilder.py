"""Builds throw-away Chrome extensions for the tests: unpacked folders, .zip files and signed .crx (v2/v3) packages.

Pure Python (plus `cryptography` for signing) - nothing here imports Qt or foxglove.py, so the expected
extension IDs are computed independently of the code under test.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
import zipfile
import zlib
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

# ── extension sources ─────────────────────────────────────────────────────────────────────

# Content script: marks the page's <html> element, then talks to the service worker three ways
# (one-shot message, long-lived port, and asks the worker to push a message back via chrome.tabs).
CONTENT_JS = """(() => {
  const root = document.documentElement, tag = %(tag)s;
  const mark = (suffix, value) => root.setAttribute("data-fg-" + tag + suffix, String(value));
  mark("", "ran");
  chrome.runtime.onMessage.addListener((msg, _sender, reply) => {
    if (msg && msg.type === "pushed") { mark("-push", msg.value); reply({ok: true}); }
  });
  try {
    chrome.runtime.sendMessage({type: "ping", tag}, (resp) => {
      const err = chrome.runtime.lastError;
      mark("-sw", resp ? resp.pong : "no-response: " + (err && err.message));
    });
  } catch (e) { mark("-sw", "throw: " + e); }
  try {
    const port = chrome.runtime.connect({name: "fg-" + tag});
    port.onMessage.addListener((m) => mark("-port", m.echo));
    port.postMessage({value: "hi"});
  } catch (e) { mark("-port", "throw: " + e); }
  try {
    chrome.runtime.sendMessage({type: "push", tag}, (resp) => {
      const err = chrome.runtime.lastError;
      mark("-push-status", resp ? JSON.stringify(resp) : "no-response: " + (err && err.message));
    });
  } catch (e) { mark("-push-status", "throw: " + e); }
})();
"""

WORKER_JS = """const TAG = %(tag)s;
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg) return;
  if (msg.type === "ping") { reply({pong: "pong-" + TAG}); return; }
  if (msg.type === "count") {
    chrome.storage.local.get("count").then(({count = 0}) =>
      chrome.storage.local.set({count: count + 1}).then(() => reply({count: count + 1})));
    return true;
  }
  if (msg.type === "push") {
    const tabId = sender && sender.tab && sender.tab.id;
    if (tabId === undefined || !chrome.tabs || !chrome.tabs.sendMessage) {
      reply({ok: false, why: "no sender.tab / chrome.tabs.sendMessage", hasTabs: !!chrome.tabs});
      return;
    }
    reply({ok: true, tabId});
    setTimeout(() => chrome.tabs.sendMessage(tabId, {type: "pushed", value: "pushed-" + TAG}).catch(() => {}), 50);
  }
});
chrome.runtime.onConnect.addListener((port) => {
  port.onMessage.addListener((m) => port.postMessage({echo: m.value + "-" + TAG}));
});
"""

POPUP_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>%(name)s Popup</title>
<style>html, body { margin: 0; } body { width: %(w)dpx; height: %(h)dpx; background: #eee; }</style>
</head><body>%(body)s<div id="popup">%(name)s pop-up</div><script src="popup.js"></script></body></html>
"""
POPUP_JS = 'document.documentElement.setAttribute("data-popup-ready", "yes");\n'
OPTIONS_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>%(name)s Options</title></head>
<body><h1 id="options">Options for %(name)s</h1></body></html>
"""
POPUP_SIZE = (250, 222)  # the pop-up page's body size; Foxglove's ExtensionPopup should shrink-wrap to it


def tiny_png(size: int = 16, rgb: tuple[int, int, int] = (155, 127, 245)) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    row = b"\x00" + bytes(rgb) * size
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(row * size)) + chunk(b"IEND", b""))


def probe_manifest(name: str = "Probe", tag: str = "probe", version: str = "1.0", popup: bool = True,
                   options: str | None = "options_ui", **extra) -> dict:
    """An MV3 manifest using everything Foxglove cares about: worker, content script, pop-up, options, icons."""
    manifest: dict = {
        "manifest_version": 3, "name": name, "version": version, "description": f"Test extension {tag}",
        "permissions": ["storage"],
        "background": {"service_worker": "sw.js"},
        "content_scripts": [{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"], "run_at": "document_end"}],
        "icons": {"16": "icon16.png", "48": "icon48.png"},
        "action": {"default_title": name, "default_icon": {"16": "icon16.png"}},
    }
    if popup:
        manifest["action"]["default_popup"] = "popup.html"
    if options == "options_ui":
        manifest["options_ui"] = {"page": "options.html", "open_in_tab": True}
    elif options == "options_page":
        manifest["options_page"] = "options.html"
    manifest.update(extra)
    return manifest


def probe_files(name: str, tag: str, popup_size: tuple[int, int] = POPUP_SIZE, popup_js: str = "",
                popup_body: str = "") -> dict[str, str | bytes]:
    tag_js = json.dumps(tag)
    return {
        "cs.js": CONTENT_JS % {"tag": tag_js}, "sw.js": WORKER_JS % {"tag": tag_js},
        "popup.html": POPUP_HTML % {"name": name, "w": popup_size[0], "h": popup_size[1], "body": popup_body},
        "popup.js": POPUP_JS + popup_js,
        "options.html": OPTIONS_HTML % {"name": name},
        "icon16.png": tiny_png(16), "icon48.png": tiny_png(48),
    }


def write_tree(dest: Path, manifest: dict | str, files: dict[str, str | bytes] | None = None) -> Path:
    """Write an unpacked extension: *manifest* (dict -> JSON, str -> verbatim text) plus *files*."""
    dest.mkdir(parents=True, exist_ok=True)
    for rel, body in (files or {}).items():
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    text = manifest if isinstance(manifest, str) else json.dumps(manifest, indent=2)
    (dest / "manifest.json").write_text(text, encoding="utf-8")
    return dest


def probe_extension(dest: Path, name: str = "Probe", tag: str = "probe", version: str = "1.0", **manifest_extra) -> Path:
    return write_tree(dest, probe_manifest(name, tag, version, **manifest_extra), probe_files(name, tag))


def mv2_extension(dest: Path, name: str = "Old Timer") -> Path:
    return write_tree(dest, {"manifest_version": 2, "name": name, "version": "1.0",
                             "browser_action": {"default_title": name}, "background": {"scripts": ["bg.js"]}},
                      {"bg.js": "// MV2 background page\n"})


def broken_manifest_extension(dest: Path) -> Path:
    return write_tree(dest, '{ "manifest_version": 3, "name": "Broken", "version": ', {})


def commented_manifest_extension(dest: Path, name: str = "Commented", tag: str = "commented",
                                 trailing_commas: bool = False) -> Path:
    """manifest.json with // and /* */ comments (which Chrome accepts) and optionally trailing commas."""
    comma = "," if trailing_commas else ""
    text = f"""{{
  // line comment
  "manifest_version": 3, /* block comment */
  "name": "{name}",
  "version": "1.0",
  "description": "a // that is not a comment, and a /* that isn't one either */",
  "content_scripts": [{{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"], "run_at": "document_end"{comma}}}{comma}],
  "background": {{"service_worker": "sw.js"{comma}}}{comma}
}}
"""
    files = probe_files(name, tag)
    return write_tree(dest, text, {"cs.js": files["cs.js"], "sw.js": files["sw.js"]})


def localized_extension(dest: Path, message: str = "Localized Probe", tag: str = "l10n") -> Path:
    manifest = probe_manifest("__MSG_extName__", tag, popup=False, options=None, default_locale="en",
                              description="__MSG_extDesc__")
    files = probe_files(message, tag)
    files["_locales/en/messages.json"] = json.dumps({"extName": {"message": message},
                                                     "extDesc": {"message": f"{message} description"}})
    return write_tree(dest, manifest, files)


# ── packaging ─────────────────────────────────────────────────────────────────────────────
def zip_bytes(src: Path, prefix: str = "", extra: dict[str, bytes] | None = None) -> bytes:
    """Zip the folder *src*; *prefix* nests everything one folder deep (as GitHub release zips do)."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path in sorted(src.rglob("*")):
            if path.is_file():
                zf.write(path, prefix + path.relative_to(src).as_posix())
        for name, body in (extra or {}).items():
            zf.writestr(name, body)
    return buf.getvalue()


def write_zip(src: Path, out: Path, prefix: str = "", extra: dict[str, bytes] | None = None) -> Path:
    out.write_bytes(zip_bytes(src, prefix, extra))
    return out


def _varint(value: int) -> bytes:
    out = bytearray()
    while True:
        byte, value = value & 0x7F, value >> 7
        out.append(byte | (0x80 if value else 0))
        if not value:
            return bytes(out)


def _field(number: int, payload: bytes) -> bytes:  # a length-delimited protobuf field
    return _varint(number << 3 | 2) + _varint(len(payload)) + payload


@dataclass
class SigningKey:
    private: rsa.RSAPrivateKey
    public_der: bytes

    @property
    def ext_id(self) -> str:
        return extension_id(self.public_der)

    @property
    def manifest_key(self) -> str:
        return base64.b64encode(self.public_der).decode("ascii")


def new_key() -> SigningKey:
    private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    der = private.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return SigningKey(private, der)


def extension_id(public_der: bytes) -> str:
    """Chrome's rule: first 128 bits of SHA-256(public key), hex digits 0-f mapped to letters a-p."""
    return hashlib.sha256(public_der).hexdigest()[:32].translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))


def crx3_bytes(archive: bytes, key: SigningKey) -> bytes:
    signed_data = _field(1, hashlib.sha256(key.public_der).digest()[:16])  # SignedData { crx_id = 1 }
    message = b"CRX3 SignedData\x00" + struct.pack("<I", len(signed_data)) + signed_data + archive
    signature = key.private.sign(message, padding.PKCS1v15(), hashes.SHA256())
    proof = _field(1, key.public_der) + _field(2, signature)  # AsymmetricKeyProof
    header = _field(2, proof) + _field(10000, signed_data)   # CrxFileHeader { sha256_with_rsa, signed_header_data }
    return b"Cr24" + struct.pack("<II", 3, len(header)) + header + archive


def crx2_bytes(archive: bytes, key: SigningKey) -> bytes:
    signature = key.private.sign(archive, padding.PKCS1v15(), hashes.SHA1())
    return b"Cr24" + struct.pack("<III", 2, len(key.public_der), len(signature)) + key.public_der + signature + archive


def write_crx3(src: Path, out: Path, key: SigningKey, prefix: str = "", extra: dict[str, bytes] | None = None) -> Path:
    out.write_bytes(crx3_bytes(zip_bytes(src, prefix, extra), key))
    return out


def write_crx2(src: Path, out: Path, key: SigningKey) -> Path:
    out.write_bytes(crx2_bytes(zip_bytes(src), key))
    return out


# ── an extension that uses the chrome.* APIs Qt WebEngine lacks (Foxglove's polyfill provides them) ──
API_WORKER_JS = r"""%(imports)s
const record = (kind, value) => chrome.storage.local.set({["ev:" + kind]: value === undefined ? null : value});
record("start", {util: self.UTIL_LOADED === true, greet: chrome.i18n.getMessage("greet", ["Ann"]),
  name: chrome.i18n.getMessage("extName"), dollars: chrome.i18n.getMessage("price"), ui: chrome.i18n.getUILanguage(),
  idOk: chrome.i18n.getMessage("@@extension_id") === chrome.runtime.id});
// Top-level calls to APIs Qt WebEngine doesn't have: without the polyfill the first one throws and kills the worker.
chrome.action.setBadgeText({text: "7"});
chrome.action.setBadgeBackgroundColor({color: [0, 128, 0, 255]});
chrome.action.setTitle({title: "Seven things"});
chrome.contextMenus.removeAll(() => {
  chrome.contextMenus.create({id: "look", title: "Look up “%%s”", contexts: ["selection"]});
  chrome.contextMenus.create({id: "page-item", title: "Sink page item", contexts: ["page"]});
  chrome.contextMenus.create({id: "look", title: "dup"}, () => record("dup", chrome.runtime.lastError ? chrome.runtime.lastError.message : "no error"));
});
chrome.alarms.create("tick", {delayInMinutes: 0.01});
chrome.alarms.onAlarm.addListener((alarm) => record("alarm", alarm.name));
chrome.runtime.onInstalled.addListener((details) => record("installed", details));
chrome.contextMenus.onClicked.addListener((info, tab) => record("menu", {id: info.menuItemId, selection: info.selectionText || null, tab: tab ? tab.id : null}));
chrome.notifications.onClicked.addListener((id) => record("note", id));
chrome.commands.onCommand.addListener((name, tab) => record("command", {name, tab: tab ? tab.id : null}));
chrome.storage.onChanged.addListener((changes, area) => { if (area === "sync") record("sync", changes); });
chrome.tabs.onUpdated.addListener((tabId, change, tab) => { if (change.status === "complete" && tab.url && tab.url.includes("tabevent")) record("updated", {tabId, url: tab.url}); });
chrome.action.onClicked.addListener(async (tab) => {
  const out = {tab: tab.id, url: tab.url};
  out.exec = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: (a) => document.title + "|" + a, args: ["x"]})
    .then((r) => r[0].result, (e) => "ERR " + e.message);
  out.asyncExec = await chrome.scripting.executeScript({target: {tabId: tab.id}, func: async () => { await new Promise((r) => setTimeout(r, 50)); return 42; }})
    .then((r) => r[0].result, (e) => "ERR " + e.message);
  await chrome.scripting.insertCSS({target: {tabId: tab.id}, css: "body { border-top: 5px solid rgb(1, 2, 3); }"});
  out.reply = await chrome.tabs.sendMessage(tab.id, {q: "ping"}).catch((e) => "ERR " + e.message);
  out.missing = await chrome.tabs.sendMessage(999999, {q: "ping"}).then(() => "no error", (e) => e.message);
  record("clicked", out);
});
chrome.runtime.onMessage.addListener((msg, sender, reply) => {
  if (!msg || msg.__fg) { record("leak", msg); return; }
  if (msg.type === "hello") { reply({tab: sender.tab ? sender.tab.id : null, frame: sender.frameId}); return; }
  if (msg.type === "mark") { reply(self.__fgMark === undefined ? null : self.__fgMark); return; }  // which polyfill runs here
  if (msg.type === "notify") { chrome.notifications.create("n1", {type: "basic", iconUrl: "icon16.png", title: "Hi", message: "There"}).then(reply); return true; }
  if (msg.type === "dnr") {
    chrome.declarativeNetRequest.updateSessionRules({addRules: [{id: 1, priority: 1, action: {type: "block"}, condition: {urlFilter: "ads"}}]})
      .then(() => chrome.declarativeNetRequest.getSessionRules()).then(reply, (e) => reply("ERR " + e.message));
    return true;
  }
  if (msg.type === "offscreen") {
    chrome.offscreen.createDocument({url: "offscreen.html", reasons: ["DOM_PARSER"], justification: "test"})
      .then(async () => reply({has: await chrome.offscreen.hasDocument(),
        contexts: (await chrome.runtime.getContexts({contextTypes: ["OFFSCREEN_DOCUMENT"]})).length}), (e) => reply("ERR " + e.message));
    return true;
  }
  if (msg.type === "from-offscreen") { record("offscreen", msg.text); return; }
  if (msg.type === "stubs") {
    Promise.all([chrome.cookies.getAll({}), chrome.bookmarks.getTree(), chrome.webNavigation.getAllFrames({tabId: 1})])
      .then((r) => reply(r.map((x) => Array.isArray(x))), (e) => reply("ERR " + e.message));
    return true;
  }
});
"""

API_CONTENT_JS = r"""(() => {
  const root = document.documentElement, mark = (k, v) => root.setAttribute("data-sink-" + k, typeof v === "string" ? v : JSON.stringify(v));
  mark("ran", "yes");
  mark("i18n", chrome.i18n.getMessage("greet", ["Cy"]));
  chrome.runtime.onMessage.addListener((msg, sender, reply) => { reply({pong: msg.q, title: document.title}); });
  chrome.runtime.sendMessage({type: "hello"}).then((r) => mark("hello", r), (e) => mark("hello", "ERR " + e.message));
  chrome.storage.onChanged.addListener((changes, area) => { if (area === "sync") mark("sync", changes); });
  const img = new Image();
  img.onload = () => mark("war", img.naturalWidth + " " + img.src.split(":")[0]);
  img.onerror = () => mark("war", "error " + img.src);
  img.src = chrome.runtime.getURL("img/pic.png");
  const badge = document.createElement("span");
  badge.className = "sink-badge";
  (document.body || root).appendChild(badge);
  mark("css", getComputedStyle(badge).backgroundImage);
})();
"""

API_PAGE_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Sink Options</title></head>
<body><h1 id="options">Sink options</h1><script src="options.js"></script></body></html>
"""
OFFSCREEN_HTML = """<!doctype html><html><head><meta charset="utf-8"><title>Offscreen</title></head>
<body><script src="offscreen.js"></script></body></html>
"""
OFFSCREEN_JS = 'chrome.runtime.sendMessage({type: "from-offscreen", text: chrome.i18n.getMessage("extName")});\n'


def api_manifest(name: str = "__MSG_extName__", version: str = "1.0", module: bool = False, **extra) -> dict:
    manifest: dict = {
        "manifest_version": 3, "name": name, "version": version, "default_locale": "en", "description": "__MSG_desc__",
        "permissions": ["storage", "contextMenus", "alarms", "notifications", "scripting", "tabs", "offscreen",
                        "declarativeNetRequest", "webNavigation", "cookies", "bookmarks"],
        "host_permissions": ["http://127.0.0.1/*"],
        "background": {"service_worker": "js/sw.js", **({"type": "module"} if module else {})},
        "content_scripts": [{"matches": ["http://127.0.0.1/*"], "js": ["cs.js"], "css": ["cs.css"], "run_at": "document_end"}],
        "action": {"default_title": "Sink", "default_icon": {"16": "icon16.png"}},
        "commands": {"_execute_action": {"suggested_key": {"default": "Alt+Shift+K"}},
                     "toggle-thing": {"suggested_key": {"default": "Ctrl+Shift+U", "mac": "Command+Shift+U"}, "description": "Toggle"}},
        "icons": {"16": "icon16.png", "48": "icon48.png"},
        "options_page": "options.html",
        "web_accessible_resources": [{"resources": ["img/*.png"], "matches": ["http://127.0.0.1/*"]}],
        "content_security_policy": {"extension_pages": "script-src 'self'; object-src 'self'; connect-src 'self'"},
    }
    manifest.update(extra)
    return manifest


def api_extension(dest: Path, name: str = "Kitchen Sink", version: str = "1.0", module: bool = False,
                  manifest_extra: dict | None = None, files: dict | None = None) -> Path:
    """Uses action, contextMenus, alarms, notifications, scripting, tabs, offscreen, DNR, i18n, storage.sync,
    web-accessible resources - at the worker's top level, like real extensions do."""
    worker = API_WORKER_JS % {"imports": 'import "./util.js";' if module else 'importScripts("util.js");'}
    tree = {
        "js/sw.js": worker, "js/util.js": "self.UTIL_LOADED = true;\n", "cs.js": API_CONTENT_JS,
        "cs.css": ".sink-badge { background-image: url(chrome-extension://__MSG_@@extension_id__/img/pic.png); }\n",
        "options.html": API_PAGE_HTML, "options.js": "document.documentElement.setAttribute('data-options', 'ready');\n",
        "offscreen.html": OFFSCREEN_HTML, "offscreen.js": OFFSCREEN_JS,
        "icon16.png": tiny_png(16), "icon48.png": tiny_png(48), "img/pic.png": tiny_png(20, (1, 2, 3)),
        "_locales/en/messages.json": json.dumps({
            "extName": {"message": name}, "desc": {"message": "Uses everything"},
            "greet": {"message": "Hello $who$, costs $$5", "placeholders": {"who": {"content": "$1"}}},
            "price": {"message": "$$9.99"}}),
        "_locales/de/messages.json": json.dumps({"extName": {"message": name + " (de)"}}),
        **(files or {}),
    }
    return write_tree(dest, api_manifest("__MSG_extName__", version, module, **(manifest_extra or {})), tree)
