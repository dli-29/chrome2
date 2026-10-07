#!/usr/bin/env python3
"""CI check (run with the venv the installer made):  ~/chrome2-env/bin/python3 engine_check_playback.py MEDIA_DIR

Proves, with Homebrew out of the way, that the installed engine
  * is what gets imported (PyQt6 and every Qt framework load from the engine folder),
  * reports H.264 and AAC as supported (MediaSource.isTypeSupported / canPlayType),
  * actually plays an H.264+AAC .mp4 served over HTTP - both as a plain <video src> and through Media Source
    Extensions with a fragmented .mp4 (how Instagram Reels / TikTok / YouTube stream),
  * and that neither this process nor Chromium's helper processes map anything from /opt/homebrew or /usr/local.

MEDIA_DIR must hold test-h264-aac.mp4 and test-h264-aac-frag.mp4. Exit status 0 only if everything holds.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer

FORBIDDEN = ("/opt/homebrew", "/usr/local/")
NEED_SECONDS = 1.5
MSE_TYPE = 'video/mp4; codecs="avc1.4D401F, mp4a.40.2"'
EVIDENCE_TYPES = {"H.264": 'video/mp4; codecs="avc1.42E01E"', "AAC": 'audio/mp4; codecs="mp4a.40.2"'}


class RangeHandler(SimpleHTTPRequestHandler):
    """SimpleHTTPRequestHandler + byte ranges (what a media server does)."""

    _remaining: int | None = None

    def log_message(self, *args) -> None:
        pass

    def end_headers(self) -> None:
        if self._remaining is None:
            self.send_header("Accept-Ranges", "bytes")
        super().end_headers()

    def send_head(self):
        path = self.translate_path(self.path)
        header = self.headers.get("Range", "").strip()
        match = re.fullmatch(r"bytes=(\d*)-(\d*)", header)
        if os.path.isdir(path) or not os.path.isfile(path) or not match or match.groups() == ("", ""):
            self._remaining = None
            return super().send_head()
        size = os.path.getsize(path)
        first, last = match.groups()
        if first == "":
            start, end = max(0, size - int(last)), size - 1
        else:
            start, end = int(first), min(int(last) if last else size - 1, size - 1)
        if start >= size or start > end:
            self.send_error(416)
            return None
        handle = open(path, "rb")
        handle.seek(start)
        self._remaining = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(self._remaining))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        return handle

    def copyfile(self, source, outputfile) -> None:
        if self._remaining is None:
            super().copyfile(source, outputfile)
            return
        remaining, self._remaining = self._remaining, None
        while remaining > 0:
            chunk = source.read(min(1 << 16, remaining))
            if not chunk:
                break
            outputfile.write(chunk)
            remaining -= len(chunk)

    def handle(self) -> None:
        try:
            super().handle()
        except (BrokenPipeError, ConnectionResetError):
            pass


def serve(directory: str) -> tuple[ThreadingHTTPServer, int]:
    with open(os.path.join(directory, "index.html"), "w", encoding="utf-8") as fh:
        fh.write("<!doctype html><html><head><title>Chrome 2 engine test</title></head>"
                 "<body style='background:#000'></body></html>")

    def handler(*args, **kwargs):
        return RangeHandler(*args, directory=directory, **kwargs)

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1]


def dyld_images() -> list[str]:
    import ctypes
    libsystem = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    libsystem._dyld_image_count.restype = ctypes.c_uint32
    libsystem._dyld_get_image_name.restype = ctypes.c_char_p
    libsystem._dyld_get_image_name.argtypes = [ctypes.c_uint32]
    return [libsystem._dyld_get_image_name(i).decode("utf-8", "replace")
            for i in range(libsystem._dyld_image_count())]


def descendants(pid: int) -> list[tuple[int, str]]:
    table = subprocess.run(["ps", "-A", "-o", "pid=,ppid=,command="], stdout=subprocess.PIPE, text=True).stdout
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
    """Executable images mapped into *pid* (lsof's 'txt' entries)."""
    out = subprocess.run(["lsof", "-n", "-P", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
    return [line[1:] for line in out.splitlines() if line.startswith("n")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("media_dir")
    parser.add_argument("--report", default=None, help="write the results as JSON here")
    parser.add_argument("--platform", default=os.environ.get("QT_QPA_PLATFORM", "cocoa"))
    parser.add_argument("--mse-type", default=MSE_TYPE, help="MIME type for the MSE SourceBuffer")
    args = parser.parse_args()
    os.environ["QT_QPA_PLATFORM"] = args.platform
    failures: list[str] = []
    report: dict = {"platform": args.platform}

    def check(ok: bool, message: str) -> None:
        print(("PASS " if ok else "FAIL ") + message, flush=True)
        if not ok:
            failures.append(message)

    engine = os.environ.get("CHROME2_ENGINE")
    check(bool(engine), f"chrome2_engine_env ran from the venv's .pth (CHROME2_ENGINE={engine})")
    engine_real = os.path.realpath(engine or "/nonexistent")

    # (a) Where PyQt6 and Qt come from
    from PyQt6 import QtCore, QtGui, QtNetwork, QtWebEngineCore, QtWebEngineWidgets, QtWidgets  # noqa: F401
    import chrome2_engine_selftest as selftest
    info = selftest.engine_info()
    report["engine"] = info
    print(f"INFO Qt {info['qt']} / PyQt6 {info['pyqt']} / Chromium {info['chromium']} / Python {info['python']} "
          f"({sys.executable})")
    for module in (QtCore, QtWebEngineCore, QtWebEngineWidgets):
        path = os.path.realpath(module.__file__)
        check(path.startswith(engine_real + "/"), f"{module.__name__} loads from the engine: {path}")
    from PyQt6.QtCore import QLibraryInfo
    from PyQt6.QtNetwork import QSslSocket
    for name in ("PrefixPath", "PluginsPath", "LibrariesPath", "DataPath", "TranslationsPath"):
        print(f"INFO QLibraryInfo.{name} = {QLibraryInfo.path(getattr(QLibraryInfo.LibraryPath, name))}")
    print(f"INFO QT_PLUGIN_PATH = {os.environ.get('QT_PLUGIN_PATH')}")
    print(f"INFO QTWEBENGINEPROCESS_PATH = {os.environ.get('QTWEBENGINEPROCESS_PATH')}")
    backends = QSslSocket.availableBackends()
    check(QSslSocket.supportsSsl(), f"QtNetwork TLS works (backends {backends}, active {QSslSocket.activeBackend()}, "
                                    f"{QSslSocket.sslLibraryVersionString()})")

    # (b) Codecs and playback over HTTP
    server, port = serve(os.path.abspath(args.media_dir))
    base = f"http://127.0.0.1:{port}"
    plays = [("progressive", "file", f"{base}/test-h264-aac.mp4", "", NEED_SECONDS),
             ("mse", "mse", f"{base}/test-h264-aac-frag.mp4", args.mse_type, NEED_SECONDS)]
    probe = selftest.MediaProbe(plays, page_url=f"{base}/index.html", timeout_s=90)
    result = probe.run()
    report["result"] = result
    support = result.get("support") or {}
    can_play = result.get("canPlay") or {}
    for name, mime in EVIDENCE_TYPES.items():
        check(support.get(name) is True, f"MediaSource.isTypeSupported('{mime}') = {str(support.get(name)).lower()}")
        check(can_play.get(name) in ("probably", "maybe"), f"canPlayType('{mime}') = {can_play.get(name)!r}")
    for name in selftest.CODECS:
        if name not in EVIDENCE_TYPES:
            print(f"INFO {name}: isTypeSupported={support.get(name)} canPlayType={can_play.get(name)!r}")
    for key, *_rest in plays:
        play = (result.get("play") or {}).get(key) or {}
        ok = bool(play.get("played")) and play.get("currentTime", 0) > 1 and play.get("videoWidth", 0) > 0 \
            and not play.get("error")
        check(ok, f"playback {key}: currentTime={play.get('currentTime', 0):.2f}s videoWidth={play.get('videoWidth')} "
                  f"videoHeight={play.get('videoHeight')} error={play.get('error') or 'none'} "
                  f"events={','.join(play.get('events', []))}")
    if result.get("error"):
        print(f"INFO probe: {result['error']}")

    # (d) What is mapped: this process (dyld) and every Chromium helper process (lsof), while they still run
    if sys.platform == "darwin":
        images = dyld_images()
        bad = [path for path in images if path.startswith(FORBIDDEN)]
        in_engine = [path for path in images if os.path.realpath(path).startswith(engine_real + "/")]
        check(not bad, f"dyld: {len(images)} images in this process, {len(in_engine)} from the engine, "
                       f"{len(bad)} from /opt/homebrew or /usr/local {bad[:5]}")
        qt_images = [p for p in images if re.search(r"/Qt\w+\.framework/", p)]
        check(bool(qt_images) and all(os.path.realpath(p).startswith(engine_real + "/") for p in qt_images),
              f"all {len(qt_images)} Qt frameworks in this process come from the engine")
        helpers = [(pid, cmd) for pid, cmd in descendants(os.getpid()) if "QtWebEngineProcess" in cmd]
        check(bool(helpers), f"{len(helpers)} QtWebEngineProcess helper processes running")
        for pid, cmd in helpers:
            files = mapped_files(pid)
            bad = [path for path in files if path.startswith(FORBIDDEN)]
            exe = next((path for path in files if path.endswith("/QtWebEngineProcess")), "?")
            kind = re.search(r"--type=(\S+)", cmd)
            check(not bad and os.path.realpath(exe).startswith(engine_real + "/"),
                  f"helper pid {pid} ({kind.group(1) if kind else '?'}): {len(files)} mapped images, "
                  f"executable {exe}, forbidden {bad[:5]}")
        report["dyld_images"] = images
    else:
        print("INFO not macOS: skipping the dyld / lsof checks")
    probe.close()
    server.shutdown()

    report["failures"] = failures
    if args.report:
        with open(args.report, "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=2)
    print(("ALL PASSED" if not failures else f"{len(failures)} CHECK(S) FAILED") + f" (platform {args.platform})")
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(main())
