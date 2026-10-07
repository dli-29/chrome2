"""Chrome 2 engine self-test:  ~/chrome2-env/bin/python3 -m chrome2_engine_selftest [--json] [--platform NAME]

Asks the bundled Qt WebEngine (Chromium) which media formats it supports - H.264 and AAC are the ones the plain
pip PyQt6-WebEngine lacks - plays the bundled H.264+AAC test clip for a moment (muted) and checks that its AAC
audio is really decoded: the audio bytes Chromium's media pipeline decoded while playing, and the clip decoded
with Web Audio (decodeAudioData). Also reports whether Chromium will decode H.264 / HEVC in hardware
(MediaCapabilities' powerEfficient; VideoToolbox on a real Mac).

Exit status: 0 = H.264 and AAC supported, the clip played and its audio was decoded, 1 = H.264 or AAC missing
(or the engine didn't start), 2 = supported, but playback or audio decoding couldn't be confirmed here (with the
off-screen platform that can be the test, not the engine).

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
HARDWARE = {  # name -> 1080p stream for MediaCapabilities.decodingInfo
    "H.264": 'video/mp4; codecs="avc1.640028"',
    "HEVC": 'video/mp4; codecs="hvc1.1.6.L120.90"',
}

PAGE_JS = r"""
(() => {
  const codecs = %s, hardware = %s;
  const r = window.__c2 = {support: {}, canPlay: {}, play: {}, decode: {}, hardware: {}, hardwareDone: false,
                           userAgent: navigator.userAgent};
  for (const [name, type] of Object.entries(codecs)) {
    r.support[name] = !!(window.MediaSource && MediaSource.isTypeSupported(type));
    r.canPlay[name] = document.createElement('video').canPlayType(type);
  }
  const caps = navigator.mediaCapabilities;
  Promise.all(Object.entries(hardware).map(([name, type]) => !caps ? null : caps.decodingInfo({
      type: 'media-source', video: {contentType: type, width: 1920, height: 1080, bitrate: 6000000, framerate: 30}})
    .then(i => { r.hardware[name] = {supported: i.supported, smooth: i.smooth, powerEfficient: i.powerEfficient}; })
    .catch(e => { r.hardware[name] = {error: String(e)}; })))
    .finally(() => { r.hardwareDone = true; });
  const counts = (v) => ({
    audioBytes: typeof v.webkitAudioDecodedByteCount === 'number' ? v.webkitAudioDecodedByteCount : null,
    videoBytes: typeof v.webkitVideoDecodedByteCount === 'number' ? v.webkitVideoDecodedByteCount : null});
  // Play url (mode 'file': <video src>, 'mse': Media Source Extensions); done when `need` seconds played with a
  // picture and - with needAudio - decoded audio (or after 30 s: error).
  window.__c2play = (key, mode, url, mime, need, muted, needAudio) => {
    const p = r.play[key] = {mode, url, muted, done: false, played: false, currentTime: 0, videoWidth: 0,
                             videoHeight: 0, audioBytes: null, videoBytes: null, error: null, events: []};
    const v = document.createElement('video');
    v.muted = muted; v.playsInline = true; v.autoplay = true; v.width = 320; v.height = 180;
    document.body.appendChild(v);
    const fail = (msg) => { if (!p.done) { p.error = msg; p.done = true; } };
    v.addEventListener('error', () => fail(v.error ? `MediaError ${v.error.code}: ${v.error.message}` : 'error event'));
    for (const ev of ['loadedmetadata', 'canplay', 'playing', 'waiting', 'stalled', 'ended'])
      v.addEventListener(ev, () => p.events.push(ev));
    const started = performance.now();
    const tick = () => {
      if (p.done) return;
      Object.assign(p, counts(v));
      p.currentTime = v.currentTime; p.videoWidth = v.videoWidth; p.videoHeight = v.videoHeight;
      p.readyState = v.readyState; p.paused = v.paused; p.duration = v.duration;
      const audioOk = !needAudio || p.audioBytes === null || p.audioBytes > 0;
      if (v.currentTime > need && v.videoWidth > 0 && audioOk) { p.played = true; p.done = true; return; }
      if (performance.now() - started > 30000) {
        fail(v.currentTime > need && v.videoWidth > 0 ? 'played, but no audio was decoded'
                                                      : 'timed out (currentTime ' + v.currentTime + ')');
        return;
      }
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
  // Decode url's audio with Web Audio (no audio device needed): ok when it gives > 0.5 s of non-silent sound.
  window.__c2decode = (key, url) => {
    const d = r.decode[key] = {url, done: false, ok: false, error: null};
    const load = () => url.startsWith('file:') ? new Promise((resolve, reject) => {
      const x = new XMLHttpRequest();
      x.open('GET', url); x.responseType = 'arraybuffer';
      x.onload = () => x.response && x.response.byteLength ? resolve(x.response)
                                                         : reject(new Error('empty response, status ' + x.status));
      x.onerror = () => reject(new Error('XMLHttpRequest failed'));
      x.send();
    }) : fetch(url).then(resp => { if (!resp.ok) throw new Error('HTTP ' + resp.status); return resp.arrayBuffer(); });
    load().then(data => new OfflineAudioContext(1, 44100, 44100).decodeAudioData(data)).then(audio => {
      let sum = 0, n = 0;
      for (let c = 0; c < audio.numberOfChannels; c++) {
        const samples = audio.getChannelData(c);
        for (let i = 0; i < samples.length; i += 7) { sum += samples[i] * samples[i]; n++; }
      }
      const rms = n ? Math.sqrt(sum / n) : 0;
      Object.assign(d, {duration: audio.duration, sampleRate: audio.sampleRate, channels: audio.numberOfChannels,
                        rms, ok: audio.duration > 0.5 && rms > 0.01});
      if (!d.ok) d.error = 'decoded, but silent or too short';
    }).catch(e => { d.error = String(e && e.message || e); }).finally(() => { d.done = true; });
  };
  return true;
})();
""" % (json.dumps(CODECS), json.dumps(HARDWARE))


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
    hardware decoding info, and runs the given plays: [(key, mode 'file'|'mse', media url, MIME type for MSE,
    seconds that must play, muted, audio must be decoded)] and Web Audio decodes: [(key, media url)]."""

    def __init__(self, plays, page_url=None, base_url=None, timeout_s=60.0, decodes=()):
        from PyQt6.QtCore import QTimer, QUrl
        from PyQt6.QtWebEngineCore import QWebEnginePage, QWebEngineProfile, QWebEngineSettings
        from PyQt6.QtWebEngineWidgets import QWebEngineView
        from PyQt6.QtWidgets import QApplication

        self.app = QApplication.instance() or QApplication([sys.argv[0] or "chrome2-selftest"])
        self.plays, self.decodes, self.result, self.finished = plays, list(decodes), {"load_ok": None}, False
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
        for key, mode, url, mime, need, muted, need_audio in self.plays:
            self.page.runJavaScript(f"__c2play({json.dumps(key)}, {json.dumps(mode)}, {json.dumps(url)}, "
                                    f"{json.dumps(mime)}, {float(need)}, {json.dumps(bool(muted))}, "
                                    f"{json.dumps(bool(need_audio))})")
        for key, url in self.decodes:
            self.page.runJavaScript(f"__c2decode({json.dumps(key)}, {json.dumps(url)})")
        self.poll.start()

    def _poll(self) -> None:
        self.page.runJavaScript("JSON.stringify(window.__c2 || null)", self._got)

    def _got(self, value) -> None:
        if self.finished or not value:
            return
        data = json.loads(value)
        self.result.update(data)
        plays, decodes = data.get("play", {}), data.get("decode", {})
        if all(plays.get(key, {}).get("done") for key, *_rest in self.plays) and \
                all(decodes.get(key, {}).get("done") for key, _url in self.decodes) and data.get("hardwareDone"):
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
    parser.add_argument("--verbose", action="store_true", help="show Chromium's own log messages")
    args = parser.parse_args(argv)
    os.environ["QT_QPA_PLATFORM"] = args.platform or os.environ.get("CHROME2_SELFTEST_PLATFORM", "offscreen")
    flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "").split()
    if not args.verbose and not any(flag.startswith("--log-level") for flag in flags):
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = " ".join(flags + ["--log-level=3"])  # (as Chrome 2 does)
    try:
        info = engine_info()
    except ImportError as exc:
        print(f"Chrome 2 engine self-test: PyQt6 / Qt WebEngine didn't load: {exc}")
        return 1
    print("Chrome 2 engine self-test")
    print(f"  Engine: Qt {info['qt']}, Chromium {info['chromium']}, PyQt6 {info['pyqt']}, Python {info['python']}")
    print(f"  PyQt6 loaded from: {info['pyqt_path']}")
    from PyQt6.QtCore import QUrl
    plays, decodes = [], []
    if not args.no_play and os.path.isfile(CLIP):
        clip_url = QUrl.fromLocalFile(CLIP).toString()
        # muted (no beep during the install); the audio proof is the bytes decoded meanwhile, or Web Audio's decode
        plays.append(("clip", "file", clip_url, "", 1.0, True, False))
        decodes.append(("clip", clip_url))
    probe = MediaProbe(plays, base_url=QUrl.fromLocalFile(os.path.dirname(CLIP) + "/").toString(), timeout_s=45,
                       decodes=decodes)
    result = probe.run()
    support = result.get("support") or {}
    for name in CODECS:
        if name in support:
            print(f"  {name}: {'supported' if support[name] else 'not supported'}")
    status = 0 if support and all(support.get(name) for name in REQUIRED) else 1
    if not support:
        print(f"  (the test page didn't run: {result.get('error', 'unknown error')})")
    clip = (result.get("play") or {}).get("clip")
    decoded = (result.get("decode") or {}).get("clip") or {}
    if plays:
        if clip and clip.get("played"):
            print(f"  Playback: OK - the H.264+AAC test clip played {clip['currentTime']:.1f} s "
                  f"({clip['videoWidth']}x{clip['videoHeight']})")
        else:
            detail = (clip or {}).get("error") or result.get("error") or "no progress"
            print(f"  Playback: the test clip didn't play here ({detail})")
            status = status or 2
        audio_bytes = (clip or {}).get("audioBytes")
        proofs = []
        if audio_bytes:
            proofs.append(f"{audio_bytes} bytes while playing")
        if decoded.get("ok"):
            proofs.append(f"Web Audio decoded {decoded['duration']:.1f} s at {decoded['sampleRate']:.0f} Hz")
        if proofs:
            print(f"  AAC audio: decoded ({'; '.join(proofs)})")
        else:
            print(f"  AAC audio: not confirmed (while playing: {audio_bytes} bytes; Web Audio: "
                  f"{decoded.get('error') or 'no result'})")
            status = status or 2
    hardware = result.get("hardware") or {}
    if hardware:
        print("  Hardware video decoding: " + ", ".join(
            f"{name} {'yes' if caps.get('powerEfficient') else 'no'}" if "error" not in caps else f"{name} ?"
            for name, caps in hardware.items()) + "  (MediaCapabilities powerEfficient)")
    if args.json:
        print(json.dumps({"info": info, "result": result}, indent=2))
    probe.close()
    return status


if __name__ == "__main__":
    sys.exit(main())
