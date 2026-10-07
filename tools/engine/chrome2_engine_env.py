"""Chrome 2 engine wiring: makes this Python use the bundled PyQt6 + Qt WebEngine (built with H.264/AAC).

The installer puts a chrome2-engine.pth file into the virtual environment (~/chrome2-env) that adds this folder
to sys.path and imports this module when Python starts - before anything imports PyQt6. Every path is computed
from where this file is, so the engine folder isn't tied to the place it was built in.

What it does:
  * puts the engine's site folder (with PyQt6) ahead of site-packages, so a pip-installed PyQt6 can't shadow it
  * QT_PLUGIN_PATH -> the engine's Qt plugins (platforms/cocoa, imageformats, tls, ...): Homebrew's Qt would
    otherwise look for them under the Homebrew prefix it was built for
  * QTWEBENGINEPROCESS_PATH -> the engine's QtWebEngineProcess helper app
Nothing here runs unless the interpreter is CPython 3.14 on arm64 macOS (what the engine is built for).
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SITE = os.path.join(ROOT, "site")
PLUGINS = os.path.join(ROOT, "qt", "plugins")
_FRAMEWORK = os.path.join(ROOT, "qt", "lib", "QtWebEngineCore.framework")
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
    paths = [PLUGINS] + [p for p in os.environ.get("QT_PLUGIN_PATH", "").split(os.pathsep) if p and p != PLUGINS]
    os.environ["QT_PLUGIN_PATH"] = os.pathsep.join(paths)
    if HELPER:
        os.environ["QTWEBENGINEPROCESS_PATH"] = HELPER
    os.environ["CHROME2_ENGINE"] = ROOT


_setup()
