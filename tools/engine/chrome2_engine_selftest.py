"""Chrome 2 engine self-test:  ~/chrome2-env/bin/python3 -m chrome2_engine_selftest [--json] [--platform NAME]

Asks the bundled Qt WebEngine (Chromium) which media formats it supports - H.264 and AAC are the ones the plain
pip PyQt6-WebEngine lacks - and plays the bundled H.264+AAC test clip for a moment.

Exit status: 0 = H.264 and AAC supported and the clip played, 1 = H.264 or AAC missing (or the engine didn't
start), 2 = supported but the clip didn't play here (with the off-screen platform that can be the test, not
the engine).

Also used by the CI checks (tools/engine/engine_check_playback.py), through MediaProbe.
"""
from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CLIP = os.path.join(ROOT, "share", "selftest-h264-aac.mp4")

CODECS = {  # name -> MIME type for MediaSource.isTypeSupported / canPlayType
    "H.264": 'video/mp4; codecs="avc1.42E01E"',
    "H.264 High": 'video/mp4; codecs="avc1.64001F"',
    "AAC": 'audio/mp4; codecs="mp4a.40.2"',
    "HEVC (H.265)": 'video/mp4; codecs="hvc1.1.6.L93.B0"',
    "VP9": 'video/webm; codecs="vp9"',
    "AV1": 'video/mp4; codecs="av01.0.05M.08"',
    "Opus": 'audio/webm; codecs="opus"',
}
REQUIRED = ("H.264", "AAC")

PAGE_JS = r"""
(() => {
  const codecs = %s;
  const r = window.__c2 = {support: {}, canPlay: {}, play: {}, userAgent: navigator.userAgent};
  for (const [name, type] of Object.entries(codecs)) {
    r.support[name] = !!(window.MediaSource && MediaSource.isTypeSupported(type));
    r.canPlay[name] = document.createElement('video').canPlayType(type);
  }
  window.__c2play = (key, mode, url, mime, need) => {
    const p = r.play[key] = {mode, url, done: false, played: false, currentTime: 0, videoWidth: 0, videoHeight: 0,
                             error: null, events: []};
    const v = document.createElement('video');
    v.muted = true; v.playsInline = true; v.autoplay = true; v.width = 320; v.height = 180;
    document.body.appendChild(v);
    const fail = (msg) => { if (!p.done) { p.error = msg; p.done = true; } };
    v.addEventListener('error', () => fail(v.error ? `MediaError ${v.error.code}: ${v.error.message}` : 'error event'));
    for (const ev of ['loadedmetadata', 'canplay', 'playing', 'waiting', 'stalled', 'ended'])
      v.addEventListener(ev, () => p.events.push(ev));
    const started = performance.now();
    const tick = () => {
      if (p.done) return;
      p.currentTime = v.currentTime; p.videoWidth = v.videoWidth; p.videoHeight = v.videoHeight;
      p.readyState = v.readyState; p.paused = v.paused; p.duration = v.duration;
      if (v.currentTime > need && v.videoWidth > 0) { p.played = true; p.done = true; return; }
      if (performance.now() - started > 30000) { fail('timed out (currentTime ' + v.currentTime + ')'); return; }
      setTimeout(tick, 100);
    };
    if (mode === 'mse') {
      if (!window.MediaSource || !MediaSource.isTypeSupported(mime)) { fail('MediaSource rejects ' + mime); return; }
      const ms = new MediaSource();
      v.src = URL.createObjectURL(ms);
      ms.addEventListener('sourceopen', async () => {
        try {
          const data = await (await fetch(url)).arrayBuffer();
          const sb = ms.addSourceBuffer(mime);
          sb.addEventListener('updateend', () => { if (ms.readyState === 'open') ms.endOfStream(); });
          sb.addEventListener('error', () => fail('SourceBuffer error'));
          sb.appendBuffer(data);
        } catch (e) { fail('MSE: ' + e); }
      }, {once: true});
    } else {
      v.src = url;
    }
    v.play().catch(e => p.events.push('play() rejected: ' + e));
    tick();
  };
  return true;
})();
""" % json.dumps(CODECS)


def engine_info() -> dict:
    from PyQt6 import QtCore, QtWebEngineCore
    return {
        "qt": QtCore.QT_VERSION_STR,
        "pyqt": QtCore.PYQT_VERSION_STR,
        "chromium": QtWebEngineCore.qWebEngineChromiumVersion(),
        "pyqt_path": os.path.dirname(QtCore.__file__),
        "qtwebenginecore_module": QtWebEngineCore.__file__,
        "python": sys.version.split()[0],
        "executable": sys.executable,
    }


class MediaProbe:
    """Loads *page_url* (or a blank page with *base_url*) in a visible QWebEngineView, collects codec support and
    runs the given plays: [(key, mode 'file'|'mse', media url, MIME type for MSE, seconds that must play)]."""

    def __init__(self, plays, page_url=None, base_url=None, timeout_s=60.0):
        from PyQt6.QtCore import QTimer, QUrl
        from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([sys.argv[0] or "chrome2-selftest"])
        self.plays, self.result, self.finished = plays, {"load_ok": None}, False
        self.profile = QWebEngineProfile()  # off the record: nothing is written to disk
        self.page = QWebEnginePage(self.profile)
        settings = self.page.settings()
        settings.setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
        settings.setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
        self.view = QWebEngineView()
        self.view.setPage(self.page)
        self.view.resize(720, 480)
        self.view.show()  # a page that isn't shown may not play video
        self.page.loadFinished.connect(self._loaded)
        self.poll = QTimer()
        self.poll.setInterval(250)
        self.poll.timeout.connect(self._poll)
        QTimer.singleShot(int(timeout_s * 1000), self._timeout)
        if page_url:
            self.page.load(QUrl(page_url))
        else:
            self.page.setHtml("<!doctype html><html><body style='background:#000'></body></html>",
                              QUrl(base_url or "about:blank"))

    def run(self) -> dict:
        if not self.finished:
            self.app.exec()
        return self.result

    def _loaded(self, ok: bool) -> None:
        if self.result["load_ok"] is not None:
            return
        self.result["load_ok"] = ok
        if not ok:
            self._finish("page failed to load")
            return
        self.page.runJavaScript(PAGE_JS)
        for key, mode, url, mime, need in self.plays:
            self.page.runJavaScript(f"__c2play({json.dumps(key)}, {json.dumps(mode)}, {json.dumps(url)}, "
                                    f"{json.dumps(mime)}, {float(need)})")
        self.poll.start()

    def _poll(self) -> None:
        self.page.runJavaScript("JSON.stringify(window.__c2 || null)", self._got)

    def _got(self, value) -> None:
        if self.finished or not value:
            return
        data = json.loads(value)
        self.result.update(data)
        plays = data.get("play", {})
        if all(plays.get(key, {}).get("done") for key, *_rest in self.plays):
            self._finish(None)

    def _timeout(self) -> None:
        self._finish("timed out")

    def _finish(self, error) -> None:
        if self.finished:
            return
        self.finished = True
        if error:
            self.result.setdefault("error", error)
        self.poll.stop()
        self.app.quit()

    def close(self) -> None:
        """Pages go before their profile (Chromium complains otherwise)."""
        from PyQt6 import sip
        self.view.close()
        for item in (self.page, self.view, self.profile):
            if not sip.isdeleted(item):
                sip.delete(item)


def main(argv=None) -> int:
    import argparse
    parser = argparse.ArgumentParser(prog="python3 -m chrome2_engine_selftest", description=__doc__.split("\n")[0])
    parser.add_argument("--json", action="store_true", help="print the raw results as JSON too")
    parser.add_argument("--platform", default=None, help="Qt platform (default: offscreen - no window)")
    parser.add_argument("--no-play", action="store_true", help="only ask which formats are supported")
    args = parser.parse_args(argv)
    os.environ["QT_QPA_PLATFORM"] = args.platform or os.environ.get("CHROME2_SELFTEST_PLATFORM", "offscreen")
    try:
        info = engine_info()
    except ImportError as exc:
        print(f"Chrome 2 engine self-test: PyQt6 / Qt WebEngine didn't load: {exc}")
        return 1
    print("Chrome 2 engine self-test")
    print(f"  Engine: Qt {info['qt']}, Chromium {info['chromium']}, PyQt6 {info['pyqt']}, Python {info['python']}")
    print(f"  PyQt6 loaded from: {info['pyqt_path']}")
    from PyQt6.QtCore import QUrl
    plays = []
    if not args.no_play and os.path.isfile(CLIP):
        plays.append(("clip", "file", QUrl.fromLocalFile(CLIP).toString(), "", 1.0))
    probe = MediaProbe(plays, base_url=QUrl.fromLocalFile(os.path.dirname(CLIP) + "/").toString(), timeout_s=45)
    result = probe.run()
    support = result.get("support") or {}
    for name in CODECS:
        if name in support:
            print(f"  {name}: {'supported' if support[name] else 'not supported'}")
    status = 0 if support and all(support.get(name) for name in REQUIRED) else 1
    if not support:
        print(f"  (the test page didn't run: {result.get('error', 'unknown error')})")
    clip = (result.get("play") or {}).get("clip")
    if plays:
        if clip and clip.get("played"):
            print(f"  Playback: OK - the H.264+AAC test clip played {clip['currentTime']:.1f} s "
                  f"({clip['videoWidth']}x{clip['videoHeight']})")
        else:
            detail = (clip or {}).get("error") or result.get("error") or "no progress"
            print(f"  Playback: the test clip didn't play here ({detail})")
            status = status or 2
    if args.json:
        print(json.dumps({"info": info, "result": result}, indent=2))
    probe.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
