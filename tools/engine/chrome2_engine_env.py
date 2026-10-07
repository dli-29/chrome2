"""Chrome 2 engine wiring: makes this Python use the bundled PyQt6 + Qt WebEngine (built with H.264/AAC).

The installer puts a chrome2-engine.pth file into the virtual environment (~/chrome2-env) that adds this folder
to sys.path and imports this module when Python starts - before anything imports PyQt6. Every path is computed
from where this file is, so the engine folder isn't tied to the place it was built in.

The bundle is laid out so that Qt's own idea of its install prefix (it walks up from QtCore.framework the way
Homebrew's build does) is the engine folder itself: QLibraryInfo's plugin, library, data and translation paths
all stay inside it, in this process and in Chromium's QtWebEngineProcess helpers. On top of that this module:
  * puts the engine's site folder (with PyQt6) ahead of site-packages; the bundled PyQt6 package doesn't merge
    in other PyQt6 folders, so a pip-installed PyQt6 in this environment is ignored (with a warning)
  * QT_PLUGIN_PATH -> only the engine's Qt plugins (a QT_PLUGIN_PATH from elsewhere could load a second Qt)
  * QTWEBENGINEPROCESS_PATH -> the engine's QtWebEngineProcess helper app
Nothing here runs unless the interpreter is CPython 3.14 on arm64 macOS (what the engine is built for).
"""
import os
import sys

# Where the Qt frameworks and plugins are inside the engine folder (written by build_bundle.py).
QT_LIB_REL = "qt/6/macos/lib"
PLUGINS_REL = "share/qt/plugins"

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
QT_LIB = os.path.join(ROOT, *QT_LIB_REL.split("/"))
PLUGINS = os.path.join(ROOT, *PLUGINS_REL.split("/"))
_FRAMEWORK = os.path.join(QT_LIB, "QtWebEngineCore.framework")
_HELPER_IN_APP = os.path.join("QtWebEngineProcess.app", "Contents", "MacOS", "QtWebEngineProcess")
HELPER = next((path for path in (os.path.join(_FRAMEWORK, "Versions", "A", "Helpers", _HELPER_IN_APP),
                                 os.path.join(_FRAMEWORK, "Helpers", _HELPER_IN_APP)) if os.path.isfile(path)), "")


def _usable() -> bool:
    if sys.platform != "darwin" or sys.version_info[:2] != (3, 14) or sys.implementation.name != "cpython":
        return False
    try:
        return os.uname().machine == "arm64"
    except AttributeError:
        return False


def _foreign_pyqt6() -> str:
    """A PyQt6 folder in site-packages (pip's), which the engine's PyQt6 shadows."""
    for entry in sys.path:
        if entry != SITE and entry.rstrip("/").endswith("site-packages"):
            candidate = os.path.join(entry, "PyQt6")
            if os.path.isdir(candidate):
                return candidate
    return ""


def _setup() -> None:
    if not _usable():
        sys.stderr.write(f"chrome2-engine: not used - it needs CPython 3.14 on an Apple Silicon Mac "
                         f"(this is {sys.version.split()[0]} on {sys.platform})\n")
        return
    while SITE in sys.path:
        sys.path.remove(SITE)
    index = next((i for i, entry in enumerate(sys.path) if entry.rstrip("/").endswith("site-packages")),
                 len(sys.path))
    sys.path.insert(index, SITE)
    os.environ["QT_PLUGIN_PATH"] = PLUGINS
    if HELPER:
        os.environ["QTWEBENGINEPROCESS_PATH"] = HELPER
    os.environ["CHROME2_ENGINE"] = ROOT
    foreign = _foreign_pyqt6()
    if foreign:
        sys.stderr.write(f"chrome2-engine: ignoring the pip-installed PyQt6 in {foreign} - the engine brings its "
                         f"own. Remove it with: {sys.executable} -m pip uninstall -y PyQt6 PyQt6-Qt6 PyQt6-sip "
                         f"PyQt6-WebEngine PyQt6-WebEngine-Qt6\n")


_setup()
