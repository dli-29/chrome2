"""pytest fixtures for driving Foxglove's real extension code (Qt WebEngine, offscreen)."""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import _env  # noqa: E402

ROOT = _env.prepare()  # before anything imports PyQt6

import pytest  # noqa: E402

import helpers  # noqa: E402


@pytest.fixture(scope="session")
def fg():
    return helpers.load_foxglove()


@pytest.fixture(scope="session")
def qapp(fg):
    from PyQt6.QtWidgets import QApplication
    helpers.register_schemes()
    helpers.install_slot_error_hook()
    app = QApplication.instance() or QApplication([sys.argv[0]])
    app.setStyle("Fusion")
    app.setStyleSheet(fg.build_stylesheet())  # BrowserWindow widgets size themselves from the style sheet
    fg.THROBBER = fg.Throbber()  # main() normally creates it; tab labels need it
    return app


def pytest_sessionfinish(session, exitstatus):
    if not os.environ.get("FOXGLOVE_TEST_KEEP"):  # set it to look at the profiles after a run
        shutil.rmtree(ROOT, ignore_errors=True)


@pytest.fixture(scope="session")
def server():
    srv = helpers.LocalServer()
    yield srv
    srv.close()


@pytest.fixture(autouse=True)
def _no_slot_errors():
    """Fail the test if Foxglove raised inside a Qt callback (PyQt would otherwise abort or swallow it)."""
    helpers.SLOT_ERRORS.clear()
    yield
    errors, helpers.SLOT_ERRORS[:] = list(helpers.SLOT_ERRORS), []
    if errors:
        pytest.fail("exception(s) inside Qt callbacks:\n" + "\n".join(errors), pytrace=False)


@pytest.fixture(autouse=True)
def _no_modal_dialogs(fg, monkeypatch):
    """A modal dialog nobody answers would hang the run: tests that expect one replace ask_question themselves."""
    def unexpected(*args, **_kwargs):
        raise AssertionError(f"unexpected modal dialog: {args[1:3]}")
    monkeypatch.setattr(fg, "ask_question", unexpected)


@pytest.fixture
def harness(qapp):
    h = helpers.Harness(ROOT / "data")
    yield h
    h.close()


@pytest.fixture
def window(harness):
    return harness.window()
