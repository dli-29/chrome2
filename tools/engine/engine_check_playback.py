#!/usr/bin/env python3
"""CI check (run with the venv the installer made):  ~/chrome2-env/bin/python3 engine_check_playback.py MEDIA_DIR

Proves, with Homebrew out of the way, that the installed engine
  * is what gets imported (PyQt6 and every Qt framework load from the engine folder),
  * reports H.264 and AAC as supported (MediaSource.isTypeSupported / canPlayType),
  * actually plays an H.264+AAC .mp4 served over HTTP - both as a plain <video src> and through Media Source
    Extensions with a fragmented .mp4 (how Instagram Reels / TikTok / YouTube stream) - with sound on, and
    Chromium's media pipeline really decodes the AAC track (webkitAudioDecodedByteCount > 0),
  * decodes AAC with Web Audio (decodeAudioData of an .m4a and of the .mp4: duration and a non-silent signal),
  * keeps Qt's own paths (QLibraryInfo: prefix, plugins, libraries, data, translations) inside the engine, and
    finds its plugins there even without QT_PLUGIN_PATH,
  * and that neither this process nor Chromium's helper processes map anything from /opt/homebrew or /usr/local.

MEDIA_DIR must hold test-h264-aac.mp4 (10 s), test-h264-aac-frag.mp4 and test-aac.m4a (3 s).
Exit status 0 only if everything holds.
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
DECODES = {"aac-m4a": ("test-aac.m4a", 3.0), "mp4-audio": ("test-h264-aac.mp4", 10.0)}  # file, seconds of audio
QT_PATHS = ("PrefixPath", "PluginsPath", "LibrariesPath", "LibraryExecutablesPath", "DataPath", "ArchDataPath",
            "TranslationsPath", "QmlImportsPath")

# Run in a separate Python (the venv's): Qt with QT_PLUGIN_PATH removed must still find the engine's plugins.
PLUGINS_WITHOUT_ENV = r"""
import json, os, sys
os.environ.pop("QT_PLUGIN_PATH", None)
os.environ["QT_QPA_PLATFORM"] = "offscreen"
from PyQt6.QtCore import QCoreApplication, QLibraryInfo
from PyQt6.QtGui import QGuiApplication, QImageReader
app = QGuiApplication([sys.argv[0]])
print(json.dumps({"platform": app.platformName(), "libraryPaths": QCoreApplication.libraryPaths(),
                  "plugins": QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath),
                  "imageFormats": sorted(bytes(f).decode() for f in QImageReader.supportedImageFormats())}))
"""


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
    """Executable images mapped into *pid* (lsof's 'txt' entries)."""
    out = subprocess.run(["lsof", "-n", "-P", "-a", "-p", str(pid), "-d", "txt", "-Fn"],
                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True).stdout
    return [line[1:] for line in out.splitlines() if line.startswith("n")]


HTTPS_URLS = ("https://www.google.com/generate_204", "https://example.com/", "https://github.com/")


def https_with_qt(urls) -> tuple[bool, str]:
    """QtNetwork over HTTPS (what Chrome 2 uses for Web Store downloads): Qt's TLS backend with the system's roots."""
    from PyQt6.QtCore import QEventLoop, QTimer, QUrl
    from PyQt6.QtNetwork import QNetworkAccessManager, QNetworkReply, QNetworkRequest
    manager, detail = QNetworkAccessManager(), "no URL tried"
    for url in urls:
        reply = manager.get(QNetworkRequest(QUrl(url)))
        ssl_errors: list[str] = []
        reply.sslErrors.connect(lambda errors: ssl_errors.extend(e.errorString() for e in errors))
        loop = QEventLoop()
        reply.finished.connect(loop.quit)
        QTimer.singleShot(20000, loop.quit)
        loop.exec()
        status = reply.attribute(QNetworkRequest.Attribute.HttpStatusCodeAttribute)
        if reply.isFinished() and reply.error() == QNetworkReply.NetworkError.NoError and not ssl_errors:
            return True, f"{url} -> HTTP {status}"
        detail = f"{url}: {reply.errorString()} {ssl_errors}"
        reply.abort()
    return False, detail


def https_with_chromium(page, urls) -> tuple[bool, str]:
    """A page over HTTPS in Qt WebEngine (Chromium's own network stack and certificate checks)."""
    from PyQt6.QtCore import QEventLoop, QTimer, QUrl
    detail = "no URL tried"
    for url in urls:
        outcome: dict = {}
        loop = QEventLoop()

        def finished(ok: bool) -> None:
            outcome["ok"] = ok
            loop.quit()

        page.loadFinished.connect(finished)
        page.load(QUrl(url))
        QTimer.singleShot(30000, loop.quit)
        loop.exec()
        page.loadFinished.disconnect(finished)
        if outcome.get("ok"):
            return True, f"{url} loaded in Chromium (title {page.title()!r})"
        detail = f"{url}: load {'failed' if outcome else 'timed out'}"
    return False, detail


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

    def inside(path: str) -> bool:
        real = os.path.realpath(path)
        return real == engine_real or real.startswith(engine_real + "/")

    for name in QT_PATHS:
        member = getattr(QLibraryInfo.LibraryPath, name, None)
        if member is None:
            continue
        path = QLibraryInfo.path(member)
        check(inside(path), f"QLibraryInfo.{name} is inside the engine: {path}")
    prefix = QLibraryInfo.path(QLibraryInfo.LibraryPath.PrefixPath)
    check(os.path.realpath(prefix) == engine_real, f"Qt's prefix is the engine folder itself: {prefix}")
    plugins_dir = QLibraryInfo.path(QLibraryInfo.LibraryPath.PluginsPath)
    check(os.path.isfile(os.path.join(plugins_dir, "platforms", "libqcocoa.dylib")),
          f"QLibraryInfo.PluginsPath holds the engine's plugins: {plugins_dir}")
    print(f"INFO QLibraryInfo.SettingsPath = {QLibraryInfo.path(QLibraryInfo.LibraryPath.SettingsPath)} (not used on macOS)")
    print(f"INFO QT_PLUGIN_PATH = {os.environ.get('QT_PLUGIN_PATH')}")
    out = subprocess.run([sys.executable, "-c", PLUGINS_WITHOUT_ENV], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         text=True, timeout=120)
    try:
        bare = json.loads(out.stdout.strip().splitlines()[-1])
    except (IndexError, ValueError):
        bare = {}
        print(f"INFO plugin check output: {out.stdout[-500:]} {out.stderr[-1500:]}")
    check(bool(bare) and bare.get("platform") == "offscreen" and {"webp", "svg", "jpeg"} <= set(bare.get("imageFormats", []))
          and all(inside(p) or p.startswith("/Library/Frameworks/Python.framework/") for p in bare.get("libraryPaths", [])),
          f"without QT_PLUGIN_PATH Qt still finds the engine's plugins: platform {bare.get('platform')}, "
          f"library paths {bare.get('libraryPaths')}, image formats {bare.get('imageFormats')}")
    print(f"INFO QTWEBENGINEPROCESS_PATH = {os.environ.get('QTWEBENGINEPROCESS_PATH')}")
    backends = QSslSocket.availableBackends()
    check(QSslSocket.supportsSsl(), f"QtNetwork TLS works (backends {backends}, active {QSslSocket.activeBackend()}, "
                                    f"{QSslSocket.sslLibraryVersionString()})")

    # (b) Codecs and playback over HTTP
    server, port = serve(os.path.abspath(args.media_dir))
    base = f"http://127.0.0.1:{port}"
    # (sound on: no muted shortcut in Chromium's audio path; the runner has no speakers to bother)
    plays = [("progressive", "file", f"{base}/test-h264-aac.mp4", "", NEED_SECONDS, False, True),
             ("mse", "mse", f"{base}/test-h264-aac-frag.mp4", args.mse_type, NEED_SECONDS, False, True)]
    decodes = [(key, f"{base}/{name}") for key, (name, _seconds) in DECODES.items()]
    probe = selftest.MediaProbe(plays, page_url=f"{base}/index.html", timeout_s=90, decodes=decodes)
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
        audio, video = play.get("audioBytes"), play.get("videoBytes")
        check(isinstance(audio, int) and audio > 0,
              f"playback {key}: Chromium decoded the AAC track while playing (webkitAudioDecodedByteCount={audio}, "
              f"webkitVideoDecodedByteCount={video})")
    for key, (name, seconds) in DECODES.items():
        dec = (result.get("decode") or {}).get(key) or {}
        ok = bool(dec.get("ok")) and abs(dec.get("duration", 0) - seconds) < 0.3
        check(ok, f"Web Audio decodeAudioData({name}): duration={dec.get('duration', 0):.2f}s (expected {seconds}) "
                  f"sampleRate={dec.get('sampleRate')} channels={dec.get('channels')} rms={dec.get('rms', 0):.3f} "
                  f"error={dec.get('error') or 'none'}")
    for name, caps in (result.get("hardware") or {}).items():
        print(f"INFO MediaCapabilities 1080p {name}: {caps} (powerEfficient = hardware decoding; CI's virtual Macs "
              f"may have none)")
    if result.get("error"):
        print(f"INFO probe: {result['error']}")

    # HTTPS works without Homebrew's OpenSSL configuration: QtNetwork (OpenSSL backend) and Chromium
    ok, detail = https_with_qt(HTTPS_URLS)
    check(ok, f"HTTPS with QtNetwork ({QSslSocket.activeBackend()}): {detail}")
    ok, detail = https_with_chromium(probe.page, HTTPS_URLS[1:])
    check(ok, f"HTTPS page in Qt WebEngine: {detail}")

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
