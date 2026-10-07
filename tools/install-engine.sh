#!/bin/bash
# install-engine.sh - give Chrome 2 a Qt WebEngine that plays H.264/AAC video (Instagram Reels, TikTok, ...).
#
# One command, no admin password (everything stays in your home folder):
#
#   /bin/bash -c "$(curl -fsSL https://github.com/dli-29/chrome2/releases/download/engine-qt6.11.2-arm64/install-engine.sh)"
#
# What it does:
#   1. checks for an Apple Silicon Mac, a new enough macOS and python.org's Python 3.14
#   2. downloads the engine from the GitHub release - PyQt6 + Qt WebEngine built with H.264/AAC/HEVC
#      (Homebrew's bottles, made self-contained by .github/workflows/engine.yml) - checks its SHA-256 and
#      unpacks it to   ~/Library/Application Support/Chrome2 Engine/<version>/
#   3. makes the Python environment ~/chrome2-env (without pip's PyQt6) wired to the engine by a .pth file,
#      and installs anthropic + keyring into it
#   4. runs a codec self-test and prints the command that starts Chrome 2
# Running it again upgrades the engine (or repairs ~/chrome2-env). Your browser data
# (~/Library/Application Support/Foxglove) and your old ~/foxglove-env are never touched; to go back:
#   ~/foxglove-env/bin/python3 ~/Desktop/temp/foxglove.py
# Remove everything it installed:  bash install-engine.sh --uninstall
#
# Optional settings (environment variables): CHROME2_ENGINE_TAG (another release), CHROME2_ENGINE_BASE_URL
# (another download location: a URL, or a folder on this Mac holding the release files - e.g. ~/Downloads after
# downloading them in the browser), CHROME2_GITHUB_TOKEN (a GitHub token, to download from the release while the
# repository is private), CHROME2_ENGINE_HOME, CHROME2_VENV, CHROME2_PYTHON, CHROME2_SKIP_PIP=1.

DEFAULT_REPO="dli-29/chrome2"  # set by the release workflow
DEFAULT_ENGINE_TAG="engine-qt6.11.2-arm64"  # set by the release workflow
DEFAULT_MIN_MACOS="15.0"  # set by the release workflow

TARBALL="chrome2-engine-arm64.tar.gz"
MANIFEST="chrome2-engine-manifest.json"
PY_ORG="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"

say()  { printf '%s\n' "$*"; }
step() { printf '\n==> %s\n' "$*"; }
warn() { printf 'Warning: %s\n' "$*" >&2; }
die()  { printf '\nChrome 2 engine installer: %s\n' "$*" >&2; exit 1; }

# A path as you'd type it in Terminal: ~/... when it's in your home folder, quoted if it has spaces.
shell_path() {
  local p=$1 rest
  case $p in
    "$HOME"/*)
      rest=${p#"$HOME"/}
      # shellcheck disable=SC2088  # a literal ~ is the point: it's printed for the user to type
      case $rest in *[!A-Za-z0-9_./-]*) printf '~/"%s"' "$rest" ;; *) printf '~/%s' "$rest" ;; esac ;;
    *[!A-Za-z0-9_./-]*) printf '"%s"' "$p" ;;
    *) printf '%s' "$p" ;;
  esac
}

# version_ge 15.6.1 15.0  ->  true when the first dotted version is at least the second
version_num() {
  local IFS=.
  # shellcheck disable=SC2086
  set -- $1
  printf '%d' $(( ${1:-0} * 1000000 + ${2:-0} * 1000 + ${3:-0} ))
}
version_ge() { [ "$(version_num "$1")" -ge "$(version_num "$2")" ]; }

# Is $1 a CPython 3.14 (regular, not free-threaded) that runs natively on arm64?
python_ok() {
  "$1" - <<'PY' >/dev/null 2>&1
import platform, sys, sysconfig
ok = (sys.version_info[:2] == (3, 14) and sys.implementation.name == "cpython"
      and not sysconfig.get_config_var("Py_GIL_DISABLED") and platform.machine() == "arm64")
sys.exit(0 if ok else 1)
PY
}

curl_to() {  # curl_to FILE quiet|progress URL [curl options...]
  local target=$1 mode=$2 url=$3
  shift 3
  if [ "$mode" = quiet ]; then
    curl -fsSL --retry 3 --retry-delay 2 --connect-timeout 30 "$@" -o "$target" "$url"
  else
    curl -fL --retry 3 --retry-delay 2 --connect-timeout 30 --progress-bar "$@" -o "$target" "$url"
  fi
}

# get NAME FILE [quiet]: one file of the release - from the release URL (plain curl, no login), from a folder on
# this Mac, or (CHROME2_GITHUB_TOKEN, for a private repository) through GitHub's API.
get() {
  local name=$1 target=$2 mode=${3:-progress}
  case $BASE_URL in
    /*) cp "$BASE_URL/$name" "$target"; return ;;
  esac
  if [ -z "$TOKEN" ]; then
    curl_to "$target" "$mode" "$BASE_URL/$name"
    return
  fi
  if [ ! -f "$tmp/release.json" ]; then
    curl_to "$tmp/release.json" quiet "https://api.github.com/repos/$REPO/releases/tags/$TAG" -H "@$tmp/auth" ||
      return 1
  fi
  local id
  id=$("$PYTHON" -c 'import json, sys
print(next((str(a["id"]) for a in json.load(open(sys.argv[1]))["assets"] if a["name"] == sys.argv[2]), ""))' \
       "$tmp/release.json" "$name")
  [ -n "$id" ] || { warn "the release has no $name"; return 1; }
  curl_to "$target" "$mode" "https://api.github.com/repos/$REPO/releases/assets/$id" -H "@$tmp/auth" \
    -H "Accept: application/octet-stream"
}

uninstall() {
  step "Removing the Chrome 2 engine"
  if [ -e "$VENV" ]; then
    [ -f "$VENV/pyvenv.cfg" ] || die "$(shell_path "$VENV") isn't a Python virtual environment; not touching it."
    rm -rf "$VENV"
    say "Removed $(shell_path "$VENV")."
  fi
  if [ -d "$ENGINE_HOME" ]; then
    rm -rf "$ENGINE_HOME"
    say "Removed $(shell_path "$ENGINE_HOME")."
  fi
  say "Done. Your browser data and ~/foxglove-env were not touched."
}

main() {
  set -euo pipefail
  [ -n "${HOME:-}" ] || die "HOME isn't set."
  REPO="${CHROME2_REPO:-$DEFAULT_REPO}"
  TAG="${CHROME2_ENGINE_TAG:-$DEFAULT_ENGINE_TAG}"
  BASE_URL="${CHROME2_ENGINE_BASE_URL:-https://github.com/$REPO/releases/download/$TAG}"
  BASE_URL="${BASE_URL%/}"
  # shellcheck disable=SC2088  # (a literal ~/ that came in quoted)
  case $BASE_URL in "~/"*) BASE_URL="$HOME/${BASE_URL#"~/"}" ;; esac
  TOKEN="${CHROME2_GITHUB_TOKEN:-}"
  unset CHROME2_GITHUB_TOKEN  # (not passed on to pip & co.)
  [ -z "${CHROME2_ENGINE_BASE_URL:-}" ] || TOKEN=""
  ENGINE_HOME="${CHROME2_ENGINE_HOME:-$HOME/Library/Application Support/Chrome2 Engine}"
  VENV="${CHROME2_VENV:-$HOME/chrome2-env}"
  APP_SCRIPT="$HOME/Desktop/temp/foxglove.py"

  case "${1:-}" in
    --uninstall) uninstall; return 0 ;;
    -h|--help) sed -n '2,25p' "$0" 2>/dev/null || true; return 0 ;;
    "") ;;
    *) die "unknown option: $1 (use --uninstall, or nothing to install/upgrade)" ;;
  esac

  # 1. This Mac
  [ "$(id -u)" != 0 ] || die "Please run this as yourself, not with sudo: everything goes into your home folder."
  [ "$(uname -s)" = Darwin ] || die "This installer is for macOS."
  local machine os_version
  machine=$(uname -m)
  if [ "$machine" != arm64 ]; then
    if [ "$(sysctl -n hw.optional.arm64 2>/dev/null || true)" = 1 ]; then
      die "This Terminal runs in Intel mode (Rosetta), so it reports '$machine'. Quit Terminal, untick 'Open using Rosetta' in Terminal's Get Info window, and run this again."
    fi
    die "This engine is built for Apple Silicon Macs (M1 or newer); this Mac has an Intel processor ($machine). Your current setup (~/foxglove-env) keeps working."
  fi
  os_version=$(sw_vers -productVersion)
  version_ge "$os_version" "$DEFAULT_MIN_MACOS" ||
    die "This engine needs macOS $DEFAULT_MIN_MACOS or newer; this Mac runs macOS $os_version. Update macOS (System Settings > General > Software Update), or keep using ~/foxglove-env."

  local candidate PYTHON=""
  for candidate in "${CHROME2_PYTHON:-}" "$PY_ORG" "$(command -v python3.14 2>/dev/null || true)"; do
    [ -n "$candidate" ] && [ -x "$candidate" ] || continue
    if python_ok "$candidate"; then PYTHON=$candidate; break; fi
  done
  [ -n "$PYTHON" ] || die "Python 3.14 from python.org wasn't found (looked for $PY_ORG and python3.14 on your PATH). Install it from https://www.python.org/downloads/macos/ (macOS 64-bit universal2 installer) and run this again."
  say "macOS $os_version on $machine, Python: $PYTHON"

  local tmp stage=""
  tmp=$(mktemp -d "${TMPDIR:-/tmp}/chrome2-engine.XXXXXX")
  # shellcheck disable=SC2064
  trap "rm -rf '$tmp'" EXIT
  if [ -n "$TOKEN" ]; then
    (umask 077 && printf 'Authorization: Bearer %s\n' "$TOKEN" > "$tmp/auth")  # (a file: keeps it out of ps)
  fi

  read_manifest() { "$PYTHON" -c 'import json, sys; print(json.load(open(sys.argv[1])).get(sys.argv[2], ""))' "$1" "$2"; }

  # 2. The release
  step "Checking the engine release ($TAG)"
  get "$MANIFEST" "$tmp/$MANIFEST" quiet ||
    die "Couldn't download $BASE_URL/$MANIFEST - check your internet connection. (If the repository is private, downloads need a login: download the release files in your browser and run this with CHROME2_ENGINE_BASE_URL=~/Downloads, or set CHROME2_GITHUB_TOKEN.)"
  local version min_macos expected_sha qt chromium size
  version=$(read_manifest "$tmp/$MANIFEST" version)
  min_macos=$(read_manifest "$tmp/$MANIFEST" min_macos)
  expected_sha=$(read_manifest "$tmp/$MANIFEST" tarball_sha256)
  qt=$(read_manifest "$tmp/$MANIFEST" qt_version)
  chromium=$(read_manifest "$tmp/$MANIFEST" chromium_version)
  size=$(read_manifest "$tmp/$MANIFEST" tarball_bytes)
  case $version in "" | *[!A-Za-z0-9._-]* | .*) die "The release manifest has an unexpected version: '$version'." ;; esac
  version_ge "$os_version" "${min_macos:-$DEFAULT_MIN_MACOS}" ||
    die "Engine $version needs macOS $min_macos or newer; this Mac runs macOS $os_version."
  say "Engine $version: Qt $qt, Chromium $chromium (needs macOS $min_macos+)"

  local target="$ENGINE_HOME/$version"
  if [ -f "$target/.installed-sha256" ] && [ -n "$expected_sha" ] &&
     [ "$(cat "$target/.installed-sha256")" = "$expected_sha" ] && [ -f "$target/site/chrome2_engine_env.py" ]; then
    say "Already installed in $(shell_path "$target")."
  else
    step "Downloading the engine (${size:+$((size / 1000000)) MB})"
    get "$TARBALL.sha256" "$tmp/$TARBALL.sha256" quiet || die "Couldn't download $TARBALL.sha256."
    get "$TARBALL" "$tmp/$TARBALL" || die "Couldn't download $BASE_URL/$TARBALL."
    local published actual
    published=$(awk '{ print $1; exit }' "$tmp/$TARBALL.sha256")
    actual=$(shasum -a 256 "$tmp/$TARBALL" | awk '{ print $1 }')
    [ -n "$published" ] && [ "$actual" = "$published" ] ||
      die "The download is damaged (SHA-256 $actual, expected $published). Run this again."
    [ -z "$expected_sha" ] || [ "$expected_sha" = "$published" ] ||
      die "The release's manifest and checksum file disagree; try again in a few minutes."
    say "SHA-256 OK: $actual"

    step "Unpacking into $(shell_path "$ENGINE_HOME")"
    mkdir -p "$ENGINE_HOME"
    rm -rf "$ENGINE_HOME"/.unpacking.* 2>/dev/null || true
    stage=$(mktemp -d "$ENGINE_HOME/.unpacking.XXXXXX")
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp' '$stage'" EXIT
    tar -xzf "$tmp/$TARBALL" -C "$stage" </dev/null
    [ -f "$stage/chrome2-engine/site/chrome2_engine_env.py" ] || die "The download doesn't contain the engine."
    [ "$(read_manifest "$stage/chrome2-engine/manifest.json" version)" = "$version" ] ||
      die "The download doesn't match the release manifest."
    xattr -dr com.apple.quarantine "$stage/chrome2-engine" 2>/dev/null || true
    rm -rf "$target.previous"
    if [ -e "$target" ]; then mv "$target" "$target.previous"; fi
    mv "$stage/chrome2-engine" "$target"
    rm -rf "$target.previous" "$stage"
    printf '%s\n' "$actual" > "$target/.installed-sha256"
  fi

  # 3. The Python environment
  step "Setting up $(shell_path "$VENV")"
  if ! { [ -f "$VENV/pyvenv.cfg" ] && [ -x "$VENV/bin/python3" ] && python_ok "$VENV/bin/python3"; }; then
    if [ -e "$VENV" ] || [ -L "$VENV" ]; then
      [ -f "$VENV/pyvenv.cfg" ] ||
        die "$(shell_path "$VENV") exists but isn't a Python environment - move it away (or set CHROME2_VENV) and run this again."
      say "Re-creating it (it was made with another Python)."
      rm -rf "$VENV"
    fi
    "$PYTHON" -m venv "$VENV" </dev/null
  fi
  local venv_python="$VENV/bin/python3" purelib
  purelib=$("$venv_python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
  # A pip-installed PyQt6 in this environment would compete with the engine's: remove it.
  "$venv_python" -m pip uninstall -y -q PyQt6 PyQt6-Qt6 PyQt6-sip PyQt6-WebEngine PyQt6-WebEngine-Qt6 \
    </dev/null >/dev/null 2>&1 || true
  {
    printf '# Chrome 2 engine %s - written by install-engine.sh (re-run it after moving the engine)\n' "$version"
    printf '%s\n' "$target/site"
    printf 'import chrome2_engine_env\n'
  } > "$purelib/chrome2-engine.pth"
  say "Wired to the engine: $(shell_path "$purelib/chrome2-engine.pth")"

  if [ "${CHROME2_SKIP_PIP:-}" != 1 ]; then
    step "Installing anthropic and keyring (Claude side panel, password manager)"
    "$venv_python" -m pip install --disable-pip-version-check -q --upgrade anthropic keyring </dev/null ||
      die "pip couldn't install anthropic and keyring (internet connection?). Run this again to retry."
  fi

  local loaded engine_real
  loaded=$("$venv_python" -c 'import os, PyQt6.QtCore as m; print("\n" + os.path.realpath(m.__file__))' </dev/null |
           tail -n 1) || die "PyQt6 doesn't load in $(shell_path "$VENV")."
  engine_real=$(cd "$target" && pwd -P)
  case $loaded in
    "$engine_real"/*) ;;
    *) die "PyQt6 loads from $loaded instead of the engine ($engine_real)." ;;
  esac

  # 4. Self-test
  step "Self-test"
  local rc=0
  "$venv_python" -m chrome2_engine_selftest </dev/null || rc=$?
  case $rc in
    0) ;;
    2) warn "H.264 and AAC are supported, but the test clip didn't play in the background test - try a video in Chrome 2 itself." ;;
    *) die "The self-test failed (see above). Your previous setup still works: ~/foxglove-env/bin/python3 $(shell_path "$APP_SCRIPT")" ;;
  esac

  # Old engine versions: removed unless a running Chrome 2 still uses them.
  local old
  for old in "$ENGINE_HOME"/*; do
    [ -d "$old" ] && [ "$old" != "$target" ] || continue
    if pgrep -f -- "$old/" >/dev/null 2>&1; then
      say "Keeping the old engine $(basename "$old") for now: a running Chrome 2 uses it (run this again later to remove it)."
    else
      rm -rf "$old"
      say "Removed the old engine $(basename "$old")."
    fi
  done

  local start
  start="$(shell_path "$venv_python") $(shell_path "$APP_SCRIPT")"
  say ""
  say "Done: Chrome 2's engine is now Qt $qt / Chromium $chromium with H.264 and AAC."
  say ""
  say "Start Chrome 2 with:"
  say ""
  say "    $start"
  say ""
  [ -f "$APP_SCRIPT" ] || say "(foxglove.py isn't at $(shell_path "$APP_SCRIPT") - put its real path in that command.)"
  say "Quit Chrome 2 first if it's open. Your tabs, bookmarks, passwords and extensions carry over"
  say "(they live in ~/Library/Application Support/Foxglove)."
  say "Dock app: run once   $start --install-app   so it uses the new engine too."
  say "Back to the old engine any time:   ~/foxglove-env/bin/python3 $(shell_path "$APP_SCRIPT")"
}

main "$@"
