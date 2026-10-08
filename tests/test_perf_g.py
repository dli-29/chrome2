"""Unit G_agent_lazy: opening Claude's panel no longer imports the anthropic SDK on the UI thread (about a second).
The panel only checks that the package is there (find_spec) and warms the import up on a background thread; the key
lookup stays synchronous, so the key status is right at once."""
from __future__ import annotations

import importlib.util
import sys
import threading
import time
from types import SimpleNamespace

import pytest

from helpers import spin, wait_until

anthropic = pytest.importorskip("anthropic")


def import_threads() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "anthropic-import"]


def errors(panel) -> list[str]:
    from PyQt6.QtWidgets import QLabel
    out = []
    for i in range(panel.column.count()):
        widget = panel.column.itemAt(i).widget()
        if isinstance(widget, QLabel) and widget.objectName() == "AgentError":
            out.append(widget.property("raw") or widget.text())
    return out


def test_agent_sdk_installed_looks_without_importing(fg, monkeypatch):
    monkeypatch.delitem(sys.modules, "anthropic", raising=False)  # a fresh process: installed, not imported yet
    assert fg.agent_sdk_installed() is True
    assert "anthropic" not in sys.modules  # find_spec only looked for it
    monkeypatch.setitem(sys.modules, "anthropic", None)  # import anthropic -> ImportError (not installed)
    assert fg.agent_sdk_installed() is False
    monkeypatch.setitem(sys.modules, "anthropic", SimpleNamespace())  # importable, but no __spec__ for find_spec
    assert fg.agent_sdk_installed() is True
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)
    assert fg.agent_sdk_installed() is True

    def broken(name):
        raise ValueError(name)
    monkeypatch.delitem(sys.modules, "anthropic")
    monkeypatch.setattr(importlib.util, "find_spec", broken)
    assert fg.agent_sdk_installed() is False


def test_panel_opens_without_waiting_for_the_sdk_import(window, fg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delitem(sys.modules, "anthropic")  # as in a fresh process: installed, not imported yet
    calls: list[str] = []
    done = threading.Event()

    def slow_sdk():  # the real import takes about a second
        calls.append(threading.current_thread().name)
        time.sleep(1.0)
        done.set()
        return anthropic

    monkeypatch.setattr(fg, "agent_sdk", slow_sdk)
    started = time.perf_counter()
    window.toggle_agent_panel(True)
    elapsed = time.perf_counter() - started
    panel = window.agent_panel
    assert elapsed < 0.5, f"opening the panel took {elapsed:.2f}s (the import must not run on the UI thread)"
    assert panel.isVisible() and panel.input.isEnabled() and panel.send_button.isEnabled()
    assert panel.hint is not None and "Ask Claude" in panel.hint.text()
    assert "First add your Anthropic API key" in panel.hint.text()
    assert panel._key_where == "" and "No key yet" in panel.key_status.text()  # the key was looked up at once
    assert done.wait(5)
    assert calls == ["anthropic-import"]  # imported once, on the background thread
    wait_until(lambda: not import_threads(), 5, "the import thread to finish")
    assert "anthropic" not in sys.modules  # (the fake never put it there - nothing else imported it either)
    spin(0.2)
    assert calls == ["anthropic-import"]


def test_panel_leaves_the_import_to_the_session(harness, fg, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    calls: list[str] = []

    def fake_sdk():
        calls.append(threading.current_thread().name)
        return sys.modules.get("anthropic")

    monkeypatch.setattr(fg, "agent_sdk", fake_sdk)
    # Already imported: nothing to warm up, and the panel itself never calls agent_sdk().
    monkeypatch.setitem(sys.modules, "anthropic", anthropic)
    window = harness.window()
    window.toggle_agent_panel(True)
    panel = window.agent_panel
    assert panel.input.isEnabled() and "Ask Claude" in panel.hint.text()
    spin(0.2)
    assert calls == [] and not import_threads()
    # Not installed: the install hint, no thread, and Send still asks the session (where the module is needed).
    monkeypatch.setitem(sys.modules, "anthropic", None)
    other = harness.window()
    other.toggle_agent_panel(True)
    panel = other.agent_panel
    assert "pip install anthropic" in panel.hint.text() and not panel.input.isEnabled()
    spin(0.2)
    assert calls == [] and not import_threads()
    panel.input.setPlainText("hello")
    panel.submit()
    assert panel._key_where is None  # no keychain lookup without the SDK
    assert calls == ["MainThread"] and any("pip install anthropic" in text for text in errors(panel))
