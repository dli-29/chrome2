"""Starting from cached bytecode: the macOS app launcher and VPN restarts boot foxglove.py through importlib
(boot_command), so the 18k-line file is compiled once and read from the cache after that, not on every start.

Checked with real subprocesses: the boot leaves __main__ exactly as `python script.py` does (name, file, argv,
sys.path), works for any file name, keeps the cache away from the script, and just compiles when it can't write."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from _env import REPO

PROBE = """\
import json, os, sys
from dataclasses import dataclass


@dataclass
class Point:  # @dataclass looks __main__ up in sys.modules
    x: int = 1


print(json.dumps({"name": __name__, "file": __file__, "argv": sys.argv, "path0": sys.path[0],
                  "empty_in_path": "" in sys.path, "cwd_in_path": os.getcwd() in sys.path,
                  "is_main": sys.modules["__main__"] is sys.modules[__name__], "prefix": sys.pycache_prefix,
                  "point": Point().x}))
"""


def run(argv: list[str], cwd: Path | str, **env: str) -> subprocess.CompletedProcess:
    clean = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120, env={**clean, **env})


def pycs(folder: Path) -> list[Path]:
    return sorted(folder.rglob("*.pyc"))


@pytest.fixture
def cache(fg, tmp_path, monkeypatch) -> Path:
    folder = tmp_path / "cache"
    monkeypatch.setattr(fg, "bytecode_cache", lambda: str(folder))
    return folder


@pytest.mark.parametrize("flags", [[], ["-P"]])  # -P: Python already leaves the current directory off sys.path
def test_boot_command_sets_up_main_like_a_script(fg, tmp_path, cache, flags):
    script = tmp_path / "my script (1).py"  # no module name needed: spaces, parentheses
    script.write_text(PROBE, encoding="utf-8")
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    (cwd / "json.py").write_text("raise ImportError('the current directory is on sys.path')\n")
    direct = run([sys.executable, str(script), "a", "b c"], cwd)
    assert direct.returncode == 0, direct.stderr
    booted = run([sys.executable, *flags, "-c", fg.boot_command(str(script)), "a", "b c"], cwd)
    assert booted.returncode == 0, booted.stderr
    seen = json.loads(booted.stdout)
    assert seen == json.loads(direct.stdout)
    assert seen["name"] == "__main__" and seen["file"] == str(script) and seen["argv"] == [str(script), "a", "b c"]
    assert seen["path0"] == str(tmp_path) and not seen["empty_in_path"] and not seen["cwd_in_path"]
    assert seen["is_main"] and seen["prefix"] is None and seen["point"] == 1
    assert [p.name for p in pycs(cache)] == [f"my script (1).{sys.implementation.cache_tag}.pyc"]
    assert not (tmp_path / "__pycache__").exists()


def test_boot_command_reads_foxglove_from_the_cache(fg, tmp_path, cache):
    copy = tmp_path / "foxglove.v2 (1).py"  # a dotted stem: `python -m` could never run it
    shutil.copy(REPO / "foxglove.py", copy)
    argv = [sys.executable, "-c", fg.boot_command(str(copy)), "--help"]
    first = run(argv, tmp_path)
    assert first.returncode == 0 and "usage: foxglove.py" in first.stdout, first.stderr
    (pyc,) = pycs(cache)
    assert pyc.name == f"foxglove.v2 (1).{sys.implementation.cache_tag}.pyc"
    written = (pyc.stat().st_ino, pyc.stat().st_mtime_ns)
    second = run(argv, tmp_path)
    assert second.returncode == 0 and second.stdout == first.stdout, second.stderr
    assert (pyc.stat().st_ino, pyc.stat().st_mtime_ns) == written  # read, not compiled and rewritten
    assert not (tmp_path / "__pycache__").exists()
    copy.write_text(copy.read_text(encoding="utf-8") + "\n# edited\n", encoding="utf-8")  # the user replaced the file
    third = run(argv, tmp_path)
    assert third.returncode == 0 and third.stdout == first.stdout, third.stderr
    assert pycs(cache) == [pyc] and (pyc.stat().st_ino, pyc.stat().st_mtime_ns) != written  # stale: compiled again


def test_boot_command_compiles_when_the_cache_cannot_be_written(fg, tmp_path, monkeypatch):
    (tmp_path / "blocker").write_text("not a folder")
    monkeypatch.setattr(fg, "bytecode_cache", lambda: str(tmp_path / "blocker" / "pycache"))
    script = tmp_path / "probe.py"
    script.write_text(PROBE, encoding="utf-8")
    booted = run([sys.executable, "-c", fg.boot_command(str(script)), "x"], tmp_path)
    assert booted.returncode == 0 and not booted.stderr, booted.stderr
    assert json.loads(booted.stdout)["argv"] == [str(script), "x"]
    assert not pycs(tmp_path)


def test_restart_process_boots_from_cached_bytecode(fg, monkeypatch):
    calls = []
    monkeypatch.setattr(fg.os, "execv", lambda *args: calls.append(args))
    monkeypatch.setattr(fg, "_icon_factory", None)  # (its folder would be deleted for the restart)
    fg.restart_process("work", os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS"))
    [(python, argv)] = calls
    assert python == sys.executable
    assert argv == [sys.executable, "-c", fg.boot_command(fg.__file__), "--profile", "work"]
    assert os.path.abspath(fg.__file__) in argv[2] and fg.bytecode_cache() in argv[2]


def test_app_launcher_starts_from_cached_bytecode(fg, tmp_path, cache, monkeypatch, qapp):
    real_which = shutil.which
    monkeypatch.setattr(fg.shutil, "which", lambda name, *a, **k: None if name == "iconutil" else real_which(name, *a, **k))
    script = tmp_path / "it's here" / "foxglove.py"
    script.parent.mkdir()
    shutil.copy(REPO / "foxglove.py", script)
    bundle = fg.make_app_bundle(tmp_path / "Applications" / "Chrome 2.app", sys.executable, str(script))
    launcher = bundle / "Contents" / "MacOS" / fg.APP_EXECUTABLE
    text = launcher.read_text()
    assert " -c " in text and "importlib" in text and f"{sys.executable} {script}" not in text
    home = tmp_path / "home"
    home.mkdir()
    for _ in range(2):  # the second start reads the cache the first one wrote
        started = run([str(launcher), "--help"], tmp_path, HOME=str(home))
        assert started.returncode == 0, started.stderr
        assert "usage: foxglove.py" in (home / "Library" / "Logs" / "Chrome 2.log").read_text()
        assert [p.name for p in pycs(cache)] == [f"foxglove.{sys.implementation.cache_tag}.pyc"]
    assert not (script.parent / "__pycache__").exists()


def test_bytecode_cache_location(fg, monkeypatch):
    monkeypatch.setattr(sys, "pycache_prefix", None)
    monkeypatch.setattr(fg, "IS_MAC", True)
    assert fg.bytecode_cache() == str(Path.home() / "Library" / "Caches" / "Chrome 2" / "pycache")
    monkeypatch.setattr(fg, "IS_MAC", False)
    monkeypatch.setenv("XDG_CACHE_HOME", "/tmp/xdg cache")
    assert fg.bytecode_cache() == "/tmp/xdg cache/Foxglove/pycache"
    monkeypatch.delenv("XDG_CACHE_HOME")
    assert fg.bytecode_cache() == str(Path.home() / ".cache" / "Foxglove" / "pycache")
    monkeypatch.setattr(sys, "pycache_prefix", "/already/chosen")  # -X pycache_prefix / PYTHONPYCACHEPREFIX wins
    assert fg.bytecode_cache() == "/already/chosen"
