"""Process environment for the Foxglove tests. Import (and call prepare()) before anything imports PyQt6."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def prepare(root: Path | None = None) -> Path:
    """Point every per-user folder Qt/Chromium touches at a private *root* (a fresh temp dir by default).

    Other Chromium instances may run on this machine at the same time, so nothing may be shared with them:
    data, cache, config, NSS database (under $HOME) and the runtime dir all live below *root*.
    """
    if root is None:
        given = os.environ.get("FOXGLOVE_TEST_ROOT")
        root = Path(given) if given else Path(tempfile.mkdtemp(prefix="foxglove-tests-"))
    root.mkdir(parents=True, exist_ok=True)
    for var, sub in (("XDG_DATA_HOME", "data"), ("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"),
                     ("XDG_RUNTIME_DIR", "runtime"), ("HOME", "home")):
        folder = root / sub
        folder.mkdir(parents=True, exist_ok=True)
        os.environ[var] = str(folder)
    os.chmod(root / "runtime", 0o700)  # Qt ignores a runtime dir other users could read
    os.environ["FOXGLOVE_TEST_ROOT"] = str(root)
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if "--no-sandbox" not in flags:  # Chromium's sandbox refuses to start as root
        flags.append("--no-sandbox")
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags)
    os.environ["QTWEBENGINE_DISABLE_SANDBOX"] = "1"
    os.environ.setdefault("QT_LOGGING_RULES", "qt.qpa.*=false;qt.webenginecontext=false")
    for var in ("no_proxy", "NO_PROXY"):  # the local test servers must never go through a proxy
        hosts = [h for h in os.environ.get(var, "").split(",") if h]
        os.environ[var] = ",".join(dict.fromkeys(["127.0.0.1", "localhost", *hosts]))
    return root
