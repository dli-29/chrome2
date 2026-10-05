"""One run of "Foxglove" for the restart tests:  python ext_phase.py <install|report> '<json args>'

A profile can't be re-created inside one process (Chromium keeps it), so each phase is its own process
on the same data folders - exactly what quitting and relaunching the browser does. The phase builds the
real ExtensionsController (and, for "report", a BrowserWindow - in main()'s order) on a disk profile with
a fixed name, does its work, and prints one line "RESULT <json>".

args: root, profile, page_url,
      exts:     [{"name", "path", "tag"}]  extensions to install (install) / to check (report)
      disable:  [name]                      install: switch these off afterwards
      unpin:    [name]                      install: take these off the toolbar
      storage:  {name: value}               install: chrome.storage.local.set({persist: value}); report: read it
      enabled:  [name]                      report: names expected to come back enabled
      old:      true                        install: as an older Foxglove did (no polyfill wired in)
      records:  [kind]                      report: what each extension's worker stored under "ev:<kind>"
      upgrade:  true                        report: a newer polyfill than the one installed (as after a Foxglove update)
      marks:    [name]                      report: which polyfill the worker and pages of these extensions run
      enable:   [name]                      report: switch these on (after the rest of the report), then check marks
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _env  # noqa: E402

PHASE, ARGS = sys.argv[1], json.loads(sys.argv[2])
_env.prepare(Path(ARGS["root"]))

from PyQt6.QtWidgets import QApplication  # noqa: E402

import helpers  # noqa: E402


def snapshot(h: helpers.Harness) -> dict:
    return {
        "entries": {e.name: {"id": e.id, "enabled": e.enabled, "pinned": e.pinned, "version": e.version, "path": e.path}
                    for e in h.entries()},
        "registry": h.registry_on_disk(),
        "messages": h.messages,
    }


def by_name(h: helpers.Harness, name: str):
    return next((e for e in h.entries() if e.name == name), None)


def ext_page_url(entry) -> str:
    return (entry.popup_url if not entry.popup_url.isEmpty() else entry.options_url).toString()


def record(page, kind: str, timeout: float = 15.0):
    deadline = time.monotonic() + timeout
    while True:
        value = helpers.run_js_async(page, f"const r = await chrome.storage.local.get('ev:{kind}'); return r['ev:{kind}'] ?? null")
        if value is not None or time.monotonic() > deadline:
            return value
        helpers.spin(0.2)


def install(h: helpers.Harness) -> dict:
    for ext in ARGS["exts"]:
        h.install_ok(ext["path"], ext["name"])
    for name in ARGS.get("disable", []):
        entry = by_name(h, name)
        h.controller.set_enabled(entry.id, False)
        h.wait_state(entry.id, False)
    for name in ARGS.get("unpin", []):
        h.controller.set_pinned(by_name(h, name).id, False)
    for name, value in ARGS.get("storage", {}).items():
        page = h.fresh_page(ext_page_url(by_name(h, name)))
        helpers.run_js_async(page, f"await chrome.storage.local.set({{persist: {json.dumps(value)}}}); return true")
        for kind in ARGS.get("records", []):  # what the first start recorded: gone, so the next start's is fresh
            helpers.run_js_async(page, f"await chrome.storage.local.remove('ev:{kind}'); return true")
        h.drop_page(page)
    return snapshot(h)


def marks(h: helpers.Harness, name: str) -> dict:
    """The polyfill the extension's worker and a page of it run (an upgraded one sets __fgMark)."""
    entry = by_name(h, name)
    helpers.wait_until(lambda: entry.id not in h.controller._updates and (e := by_name(h, name)) is not None and e.enabled, 30,
                       f"{name} to settle")
    page = h.fresh_page(ext_page_url(entry))
    try:
        return {"page": helpers.run_js(page, "window.__fgMark ?? null"),
                "worker": helpers.run_js_async(page, "return await chrome.runtime.sendMessage({type: 'mark'})", timeout=15)}
    finally:
        h.drop_page(page)


def report(h: helpers.Harness) -> dict:
    out: dict = {}
    win = h.window()  # created right after the controller, as main() does
    expected = ARGS.get("enabled", [])
    try:  # Qt reloads the installed extensions (disabled); Foxglove should switch these back on
        helpers.wait_until(lambda: all((e := by_name(h, n)) is not None and e.enabled for n in expected), 20,
                           f"{expected} enabled")
    except helpers.WaitTimeout as exc:
        out["wait_error"] = str(exc)
    helpers.spin(max(0.0, 3.5 - (time.monotonic() - h.created)))  # past the controller's 2.5 s re-sync
    out.update(snapshot(h))
    layout = win.extension_buttons
    out["toolbar"] = [layout.itemAt(i).widget().toolTip() for i in range(layout.count()) if layout.itemAt(i).widget()]
    page = h.fresh_page(ARGS["page_url"])
    out["content_scripts"] = {}
    for ext in ARGS["exts"]:
        name, attr = ext["name"], f"data-fg-{ext['tag']}"
        if name in expected:
            try:
                out["content_scripts"][name] = helpers.wait_attr(page, attr, 8)
            except helpers.WaitTimeout:
                out["content_scripts"][name] = None
        else:
            out["content_scripts"][name] = None if helpers.stays_absent(page, attr, 2.0) else "ran"
    h.drop_page(page)
    out["storage"] = {}
    for name in ARGS.get("storage", {}):
        entry = by_name(h, name)
        if entry is None or not entry.enabled:
            out["storage"][name] = None
            continue
        page = h.fresh_page(ext_page_url(entry))
        out["storage"][name] = helpers.run_js_async(page, "return (await chrome.storage.local.get('persist')).persist ?? null")
        h.drop_page(page)
    out["marks"] = {name: marks(h, name) for name in ARGS.get("marks", []) if name in expected}
    for name in ARGS.get("enable", []):
        entry = by_name(h, name)
        h.controller.set_enabled(entry.id, True)
        h.wait_state(entry.id, True, 20)
        helpers.wait_until(lambda: entry.id not in h.controller._updates, 20, "its polyfill update")
        h.wait_state(entry.id, True, 20)
        out["marks"][name] = marks(h, name)
    out["shimmed"], out["records"] = {}, {}
    for ext in ARGS["exts"]:
        entry = by_name(h, ext["name"])
        info = h.info(entry.id) if entry is not None else None
        out["shimmed"][ext["name"]] = info is not None and not h.controller._needs_shim(info)
        if ARGS.get("records") and entry is not None and entry.enabled:
            page = h.fresh_page(ext_page_url(entry))
            out["records"][ext["name"]] = {kind: record(page, kind) for kind in ARGS["records"]}
            h.drop_page(page)
    return out


def main() -> int:
    helpers.register_schemes()
    helpers.install_slot_error_hook()
    app = QApplication([sys.argv[0]])
    fg = helpers.load_foxglove()
    app.setStyleSheet(fg.build_stylesheet())
    fg.THROBBER = fg.Throbber()
    if PHASE == "install" and ARGS.get("old"):  # what Foxglove did before it had the polyfill
        fg.inject_shim = lambda directory, manifest, *_a: (directory / "manifest.json").write_text(json.dumps(manifest)) or manifest
        fg.ExtensionsController._needs_shim = lambda self, info: False
    if PHASE == "report" and ARGS.get("upgrade"):  # a newer Foxglove: its polyfill differs from the installed one
        fg.EXTENSION_SHIM_JS = fg.EXTENSION_SHIM_JS.replace("const g = globalThis;", 'const g = globalThis; g.__fgMark = "upgraded";', 1)
    h = helpers.Harness(Path(ARGS["root"]) / "data", name=ARGS["profile"])
    try:
        result = {"install": install, "report": report}[PHASE](h)
    except Exception as exc:  # report, don't hang the parent
        result = {"error": f"{type(exc).__name__}: {exc}", "messages": h.messages}
    h.close()
    result["slot_errors"] = list(helpers.SLOT_ERRORS)
    print("RESULT " + json.dumps(result), flush=True)
    del app
    return 0


if __name__ == "__main__":
    sys.exit(main())
