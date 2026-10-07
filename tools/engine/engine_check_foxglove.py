#!/usr/bin/env python3
"""CI check: Chrome 2 (foxglove.py) itself runs on the installed engine.

    python3 engine_check_foxglove.py --python ~/chrome2-env/bin/python3 --script ~/Desktop/temp/foxglove.py \
        --home ~ [--seconds 20] [--platform cocoa]

1. imports foxglove.py with the venv's Python: HAS_EXTENSIONS must be True (Qt WebEngine 6.10+ extension API) and
   PyQt6 must come from the engine;
2. starts it like the user does (with HOME pointing at --home, so its data goes to
   <home>/Library/Application Support/Foxglove), lets it run, checks that neither the browser nor its
   QtWebEngineProcess helpers map anything from /opt/homebrew or /usr/local, quits it with SIGTERM (Chrome 2
   saves and closes cleanly on that) and fails on any Python traceback in its output.
Doesn't import Qt itself, so any Python 3 can run it.
"""
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

FORBIDDEN = ("/opt/homebrew", "/usr/local/")

IMPORT_CHECK = r"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.abspath(sys.argv[1])))
import foxglove
from PyQt6 import QtCore, QtWebEngineCore
print("HAS_EXTENSIONS", foxglove.HAS_EXTENSIONS)
print("QT", QtCore.QT_VERSION_STR, "CHROMIUM", QtWebEngineCore.qWebEngineChromiumVersion())
print("PYQT6", os.path.realpath(QtCore.__file__))
print("ENGINE", os.path.realpath(os.environ.get("CHROME2_ENGINE", "?")))
"""


def descendants(pid: int) -> list[tuple[int, str]]:
    table = subprocess.run(["ps", "-A", "-ww", "-o", "pid=,ppid=,command="], stdout=subprocess.PIPE, text=True).stdout
    children: dict[int, list[tuple[int, str]]] = {}
    for line in table.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            children.setdefault(int(parts[1]), []).append((int(parts[0]), parts[2] if len(parts) > 2 else ""))
    found, stack = [], [pid]
    while stack:
        for child in children.get(stack.pop(), []):
            found.append(child)
            stack.append(child[0])
    return found


def mapped_files(pid: int) -> list[str]:
    out = subprocess.run(["lsof", "-n", "-P", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
    return [line[1:] for line in out.splitlines() if line.startswith("n")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--python", required=True)
    parser.add_argument("--script", required=True)
    parser.add_argument("--home", required=True)
    parser.add_argument("--seconds", type=float, default=20.0)
    parser.add_argument("--platform", default="cocoa")
    parser.add_argument("--log", default=None, help="where to keep Chrome 2's output")
    args = parser.parse_args()
    failures: list[str] = []

    def check(ok: bool, message: str) -> None:
        print(("PASS " if ok else "FAIL ") + message, flush=True)
        if not ok:
            failures.append(message)

    home = os.path.abspath(args.home)
    env = dict(os.environ, HOME=home, CFFIXED_USER_HOME=home, QT_QPA_PLATFORM=args.platform)
    for var in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QTWEBENGINEPROCESS_PATH", "DYLD_PRINT_LIBRARIES"):
        env.pop(var, None)

    # 1. Import
    result = subprocess.run([args.python, "-c", IMPORT_CHECK, args.script], env=env, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, timeout=120)
    print(result.stdout.rstrip())
    lines = dict(line.split(" ", 1) for line in result.stdout.splitlines() if " " in line)
    check(result.returncode == 0, f"foxglove.py imports with {args.python}")
    check(lines.get("HAS_EXTENSIONS") == "True", f"HAS_EXTENSIONS = {lines.get('HAS_EXTENSIONS')}")
    engine = lines.get("ENGINE", "?")
    check(lines.get("PYQT6", "").startswith(engine + "/"), f"foxglove's PyQt6 is the engine's: {lines.get('PYQT6')}")

    # 2. Run it
    log_path = Path(args.log or os.path.join(home, "chrome2-run.log"))
    data = Path(home, "Library", "Application Support", "Foxglove")
    with open(log_path, "w", encoding="utf-8") as log:
        proc = subprocess.Popen([args.python, args.script], env=env, stdout=log, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, cwd=home)
        started = time.monotonic()
        while time.monotonic() - started < args.seconds and proc.poll() is None:
            time.sleep(0.5)
        alive = proc.poll() is None
        check(alive, f"Chrome 2 still running after {time.monotonic() - started:.0f} s (pid {proc.pid})")
        if alive:
            procs = [(proc.pid, "browser")] + [(pid, cmd) for pid, cmd in descendants(proc.pid)
                                               if "QtWebEngineProcess" in cmd]
            helpers = len(procs) - 1
            check(helpers > 0, f"{helpers} QtWebEngineProcess helpers running")
            for pid, cmd in procs:
                files = mapped_files(pid)
                bad = [path for path in files if path.startswith(FORBIDDEN)]
                qt = [path for path in files if ".framework/" in path and "/Qt" in path]
                outside = [path for path in qt if not os.path.realpath(path).startswith(engine + "/")]
                kind = "browser" if cmd == "browser" else (cmd.split("--type=")[1].split()[0] if "--type=" in cmd
                                                           else "helper")
                check(files != [] and not bad and not outside,
                      f"pid {pid} ({kind}): {len(files)} mapped images, {len(qt)} Qt framework images all from "
                      f"the engine, none from /opt/homebrew or /usr/local {bad[:3]} {outside[:3]}")
            proc.send_signal(signal.SIGTERM)
            try:
                code = proc.wait(timeout=45)
            except subprocess.TimeoutExpired:
                proc.kill()
                code = proc.wait()
                check(False, "Chrome 2 didn't quit within 45 s of SIGTERM")
            check(code == 0, f"Chrome 2 quit cleanly on SIGTERM (exit status {code})")
        else:
            check(False, f"Chrome 2 exited early with status {proc.returncode}")
    output = log_path.read_text(encoding="utf-8", errors="replace")
    print("---- Chrome 2 output (last 60 lines) ----")
    print("\n".join(output.splitlines()[-60:]))
    print("---- end ----")
    check("Traceback (most recent call last)" not in output, "no Python traceback in Chrome 2's output")
    check("Unexpected error" not in output, "no 'Unexpected error' logged by Chrome 2")
    profile = data / "Profiles" / "default"
    check(profile.is_dir(), f"profile folder created: {profile}")
    check((profile / "session.json").is_file(), "session.json saved on quit")
    storage = data / "QtWebEngine"
    print(f"INFO Qt WebEngine storage: {storage} exists={storage.is_dir()}")
    print("ALL PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
