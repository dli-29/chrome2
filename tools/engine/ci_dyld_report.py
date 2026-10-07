#!/usr/bin/env python3
"""CI helper:  ci_dyld_report.py ENGINE_DIR STDERR_FILE...

Reads what DYLD_PRINT_LIBRARIES=1 printed (every image dyld loaded, in the browser process and in each
QtWebEngineProcess helper) and fails if anything came from /opt/homebrew or /usr/local. Prints a summary.
If dyld printed nothing (a hardened-runtime process ignores DYLD_* variables) it says so and exits 0: the
in-process and lsof checks of engine_check_playback.py / engine_check_foxglove.py still cover that case.
"""
import os
import re
import sys

FORBIDDEN = ("/opt/homebrew", "/usr/local/")
LINE = re.compile(r"^dyld\[(\d+)\]: (?:<[^>]*> )?(/.+?)\s*$")


def main() -> int:
    engine = os.path.realpath(sys.argv[1])
    loads, pids, bad = [], set(), []
    for name in sys.argv[2:]:
        with open(name, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                match = LINE.match(line.strip())
                if match:
                    pid, path = match.groups()
                    pids.add(pid)
                    loads.append(path)
                    if path.startswith(FORBIDDEN):
                        bad.append(f"pid {pid}: {path}")
    if not loads:
        print("DYLD: DYLD_PRINT_LIBRARIES printed nothing (the process ignores DYLD_* variables); "
              "relying on the in-process dyld image list and lsof checks")
        return 0
    from_engine = [p for p in loads if os.path.realpath(p).startswith(engine + "/")]
    helper = [p for p in loads if p.endswith("/QtWebEngineProcess")]
    webengine = sorted({p for p in loads if "QtWebEngineCore.framework" in p})
    system = [p for p in loads if p.startswith(("/usr/lib/", "/System/"))]
    other = sorted({p for p in loads if p not in from_engine and p not in system
                    and not p.startswith("/Library/Frameworks/Python.framework/")})
    print(f"DYLD: {len(loads)} images loaded in {len(pids)} processes: {len(from_engine)} from the engine, "
          f"{len(system)} system, {len(bad)} from /opt/homebrew or /usr/local")
    for path in webengine[:3] + helper[:1]:
        print(f"DYLD:   e.g. {path}")
    for path in other[:20]:
        print(f"DYLD:   other: {path}")
    for entry in bad[:40]:
        print(f"DYLD: FORBIDDEN {entry}")
    if bad:
        return 1
    if not webengine:
        print("DYLD: QtWebEngineCore never showed up in the dyld output")
        return 1
    print("DYLD: OK - nothing loaded from /opt/homebrew or /usr/local")
    return 0


if __name__ == "__main__":
    sys.exit(main())
