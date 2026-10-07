# Chrome 2 (formerly Foxglove)

A personal web browser in one Python file, built on PyQt6 + Qt WebEngine (Chromium 140). The owner runs it on an
Apple Silicon Mac (macOS 26) **without an admin password**, so never suggest anything that needs `sudo`, Homebrew
or Xcode on their machine. They are new to GitHub and the terminal: give exact, copy-pasteable commands.

## Layout
- `foxglove.py` - the whole browser (~18k lines). The file name stays `foxglove.py` on purpose.
- `tests/` - pytest suite (~450 tests, ~20 min for a full run). Fixtures: `tests/conftest.py` (`fg` = the loaded
  module, `qapp`, `harness`, `window` = a real BrowserWindow), helpers in `tests/helpers.py`.
- `tools/install-engine.sh`, `tools/engine/` - build/install of the codec-enabled engine (see below).
- `.github/workflows/engine.yml` - GitHub Actions job that builds, tests and publishes that engine.

## Main parts of foxglove.py
- Branding: `APP_NAME = "Chrome 2"`, but internal names stay `Foxglove`/`foxglove`: `DATA_NAME` (data folder
  `~/Library/Application Support/Foxglove`), the `foxglove://` and `foxglove-ext://` schemes, class names.
  Don't rename these - it would orphan the user's data and break installed extensions.
- Extensions: `ExtensionsController`, `ExtensionBridge` (foxglove-ext:// API bridge), `EXTENSION_SHIM_JS`
  (chrome.* polyfill), `NetRules` (declarativeNetRequest engine). Extension script worlds start at
  `FIRST_EXTENSION_WORLD = 16`; `APP_WORLD` = 1, autofill = 3, Claude agent = 4.
- Claude side panel (`Agent*` classes): Anthropic SDK, default model `claude-opus-5-5`, API key in the keychain via
  `SecretStore` (service "Chrome 2"). Passwords/card numbers are redacted from everything the agent sees.
- Autofill/password manager (`Autofill*`, `SecretStore`, keyring package).
- Tabs: pinned tabs and split view; `window.current_tab()` = focused side, `window.visible_tabs()`.
- New Tab page (`NewTabPage`, Chrome look), privacy screen (`PrivacyScreen`), VPN/proxy panel (`VpnPanel`,
  applied at startup, changes restart the app), site settings + cookie manager (`CookieIndex`),
  Chrome identity for sites (`apply_browser_identity`).
- `anthropic` and `keyring` are optional imports: the browser must still start without them.

## Running and testing (in this Linux sandbox)
```
QT_QPA_PLATFORM=offscreen QTWEBENGINE_CHROMIUM_FLAGS=--no-sandbox timeout 2400 \
  python3 -m pytest -q -p no:cacheprovider tests            # full suite
... python3 -m pytest -q -p no:cacheprovider tests/test_tabs.py   # one file - prefer this for small changes
```
Use a fresh `XDG_DATA_HOME` temp dir for manual runs. Smoke-launch: `timeout 20 python3 foxglove.py` with the same env
vars. Ignore GL/Vulkan/dbus noise. Many sites (Google sign-in pages, Chrome Web Store, Instagram, Discord, hCaptcha)
are blocked from this sandbox, so those can't be tested here.

## The codec engine (H.264/AAC for Instagram Reels / TikTok)
pip's PyQt6-WebEngine has no H.264/AAC. Homebrew's Qt WebEngine does, but the user can't install Homebrew. So
`engine.yml` installs Homebrew's `pyqt` on a GitHub macOS arm64 runner, `tools/engine/build_bundle.py` repackages it
into a relocatable bundle, CI tests it with Homebrew hidden on macOS 15 and 26 with python.org Python 3.14, then
publishes it as the GitHub Release `engine-arm64`. The user installs it with no admin rights:
```
/bin/bash -c "$(curl -fsSL https://github.com/dli-29/chrome2/releases/download/engine-arm64/install-engine.sh)"
```
and runs `~/chrome2-env/bin/python3 ~/Desktop/temp/foxglove.py`. The repo must be public for the free macOS runners
and anonymous downloads.

## User's machine (for instructions)
- `foxglove.py` lives at `~/Desktop/temp/foxglove.py`; the repo is private-by-default in their mind, so send them
  updated files directly (SendUserFile) rather than expecting them to pull from git.
- Environments: `~/chrome2-env` (codec engine, preferred), `~/foxglove-env` (old pip PyQt6, fallback).
- They use a paid residential HTTP proxy in the browser's VPN panel and can't browse without it.

## Conventions
- Repo: github.com/dli-29/chrome2 (renamed from foxglove), single branch `claude/funny-sagan-lqal0n`.
- Match the dense, typed, short-comment style of foxglove.py. Add a regression test for every bug fix.
- Keep cost down: work solo unless asked for agents/workflows, run only the related test files for small changes.
- The "Chrome 2" name and Google Chrome logo are for personal use only; don't help publish/distribute it as Chrome.
