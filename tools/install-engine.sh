#!/bin/bash
# install-engine.sh - give Chrome 2 a Qt WebEngine that plays H.264/AAC video (Instagram Reels, TikTok, ...).
#
# One command, no admin password (everything stays in your home folder):
#
#   /bin/bash -c "$(curl -fsSL https://github.com/dli-29/chrome2/releases/download/engine-arm64/install-engine.sh)"
#
# What it does:
#   1. checks for an Apple Silicon Mac, a new enough macOS and python.org's Python 3.14
#   2. downloads the newest engine from the GitHub release - PyQt6 + Qt WebEngine built with H.264/AAC/HEVC
#      (Homebrew's bottles, made self-contained by .github/workflows/engine.yml) - checks its SHA-256 and
#      unpacks it to   ~/Library/Application Support/Chrome2 Engine/<version>/
#   3. makes the Python environment ~/chrome2-env (without pip's PyQt6) wired to the engine by a .pth file,
#      and installs anthropic + keyring into it
#   4. runs a codec self-test and prints the command that starts Chrome 2
# Running it again upgrades to the newest engine, repairs a damaged one, or repairs ~/chrome2-env. Your browser
# data (~/Library/Application Support/Foxglove) and your old ~/foxglove-env are never touched; to go back:
#   ~/foxglove-env/bin/python3 ~/Desktop/temp/foxglove.py
# Options:  --reinstall  (download and unpack the engine again)    --uninstall  (remove what this installed)
#
# Optional settings (environment variables): CHROME2_ENGINE_TAG (a specific release, e.g. engine-qt6.11.2-arm64;
# the default, engine-arm64, is always the newest), CHROME2_ENGINE_BASE_URL (another download location: a URL,
# or a folder on this Mac holding the release files - e.g. ~/Downloads after downloading them in the browser),
# CHROME2_GITHUB_TOKEN (a GitHub token, to download from the release while the repository is private),
# CHROME2_ENGINE_HOME (the folder for the engine versions; only folders this installer made are ever removed
# from it), CHROME2_VENV, CHROME2_PYTHON, CHROME2_SKIP_PIP=1.

DEFAULT_REPO="dli-29/chrome2"  # set by the release workflow
DEFAULT_ENGINE_TAG="engine-arm64"  # set by the release workflow
DEFAULT_MIN_MACOS="15.0"  # set by the release workflow
NEWEST_TAG="engine-arm64"  # the release that always holds the newest engine

TARBALL="chrome2-engine-arm64.tar.gz"  # the tarball's fixed name (the manifest names the versioned file)
MANIFEST="chrome2-engine-manifest.json"
PY_ORG="/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14"
MARK_INSTALLED=".installed-sha256"   # in every engine folder this installer unpacked
MARK_STAGE=".chrome2-engine-unpacking"  # in its temporary unpacking folders
MARK_VENV=".chrome2-engine-venv"     # in the Python environment it made
PIN="provided-by-the-chrome2-engine"

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

# Only what this installer made is ever replaced or removed - never other folders in CHROME2_ENGINE_HOME.
is_engine_dir() {  # an unpacked engine: its manifest says chrome2-engine and it carries the installer's mark
  [ -d "$1" ] && [ ! -L "$1" ] && [ -f "$1/$MARK_INSTALLED" ] && [ -f "$1/manifest.json" ] &&
    grep -Eq '"name": *"chrome2-engine"' "$1/manifest.json" 2>/dev/null
}
is_stage_dir() { [ -d "$1" ] && [ ! -L "$1" ] && [ -f "$1/$MARK_STAGE" ]; }
is_our_venv() {
  [ -f "$1/pyvenv.cfg" ] || return 1
  [ -f "$1/$MARK_VENV" ] && return 0
  local pth
  for pth in "$1"/lib/python3.*/site-packages/chrome2-engine.pth; do [ -f "$pth" ] && return 0; done
  return 1
}

# Does a running program (Chrome 2, or one of its QtWebEngineProcess helpers) use the engine folder $1?
engine_in_use() {
  local dir=$1 procs maps
  procs=$(ps -A -ww -o command= 2>/dev/null || true)
  case $procs in *"$dir/"*) return 0 ;; esac
  maps=$(lsof -n -w -d txt -Fn 2>/dev/null || true)  # (libraries mapped by this user's processes)
  case $maps in *"n$dir/"*) return 0 ;; esac
  return 1
}

curl_to() {  # curl_to FILE quiet|progress URL [curl options...]
  local target=$1 mode=$2 url=$3
  shift 3
  # --speed-time/--speed-limit: give up on a stalled connection (curl's --retry then starts over)
  local opts=(-fL --retry 3 --retry-delay 2 --connect-timeout 30 --speed-time 60 --speed-limit 1024)
  if [ "$mode" = quiet ]; then
    curl "${opts[@]}" -sS --max-time 300 "$@" -o "$target" "$url"
  else
    curl "${opts[@]}" --progress-bar "$@" -o "$target" "$url"
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
  if [ -e "$VENV" ] || [ -L "$VENV" ]; then
    if is_our_venv "$VENV"; then
      rm -rf "$VENV"
      say "Removed $(shell_path "$VENV")."
    else
      warn "$(shell_path "$VENV") wasn't made by this installer, so it was left alone."
    fi
  fi
  if [ -d "$ENGINE_HOME" ]; then
    local dir kept=0
    for dir in "$ENGINE_HOME"/* "$ENGINE_HOME"/.unpacking.*; do
      if is_engine_dir "$dir"; then
        if engine_in_use "$dir"; then
          say "Kept the engine $(basename "$dir"): a running Chrome 2 uses it (quit it and run this again)."
          kept=1
          continue
        fi
        rm -rf "$dir"
        say "Removed the engine $(basename "$dir")."
      elif is_stage_dir "$dir"; then
        rm -rf "$dir"
      fi
    done
    if rmdir "$ENGINE_HOME" 2>/dev/null; then
      say "Removed $(shell_path "$ENGINE_HOME")."
    elif [ "$kept" = 0 ]; then
      say "$(shell_path "$ENGINE_HOME") holds other files, so it was left in place."
    fi
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
  ENGINE_HOME="${ENGINE_HOME%/}"
  VENV="${CHROME2_VENV:-$HOME/chrome2-env}"
  VENV="${VENV%/}"
  APP_SCRIPT="$HOME/Desktop/temp/foxglove.py"

  local reinstall=0
  case "${1:-}" in
    --uninstall) uninstall; return 0 ;;
    --reinstall) reinstall=1 ;;
    -h|--help) sed -n '2,27p' "$0" 2>/dev/null || true; return 0 ;;
    "") ;;
    *) die "unknown option: $1 (use --reinstall, --uninstall, or nothing to install/upgrade)" ;;
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
  local py_version
  py_version=$("$PYTHON" -c 'import sys; print(sys.version.split()[0])')
  say "macOS $os_version on $machine, Python $py_version ($PYTHON)"

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
  # (a second try: while a new engine is being published, the manifest is replaced - gone for a moment)
  get "$MANIFEST" "$tmp/$MANIFEST" quiet || { sleep 5; get "$MANIFEST" "$tmp/$MANIFEST" quiet; } ||
    die "Couldn't download $BASE_URL/$MANIFEST - check your internet connection. (If the repository is private, downloads need a login: download the release files in your browser and run this with CHROME2_ENGINE_BASE_URL=~/Downloads, or set CHROME2_GITHUB_TOKEN.)"
  local version min_macos expected_sha qt chromium size tarball
  version=$(read_manifest "$tmp/$MANIFEST" version)
  min_macos=$(read_manifest "$tmp/$MANIFEST" min_macos)
  expected_sha=$(read_manifest "$tmp/$MANIFEST" tarball_sha256)
  qt=$(read_manifest "$tmp/$MANIFEST" qt_version)
  chromium=$(read_manifest "$tmp/$MANIFEST" chromium_version)
  size=$(read_manifest "$tmp/$MANIFEST" tarball_bytes)
  tarball=$(read_manifest "$tmp/$MANIFEST" tarball)
  case $version in "" | *[!A-Za-z0-9._-]* | .*) die "The release manifest has an unexpected version: '$version'." ;; esac
  case $tarball in chrome2-engine-arm64*.tar.gz) ;; *) tarball=$TARBALL ;; esac
  case $tarball in *[!A-Za-z0-9._-]*) die "The release manifest names an unexpected file: '$tarball'." ;; esac
  case $BASE_URL in  # a folder with the browser-downloaded files: those have the fixed name
    /*) [ -f "$BASE_URL/$tarball" ] || tarball=$TARBALL ;;
  esac
  version_ge "$os_version" "${min_macos:-$DEFAULT_MIN_MACOS}" ||
    die "Engine $version needs macOS $min_macos or newer; this Mac runs macOS $os_version."
  say "Engine $version: Qt $qt, Chromium $chromium (needs macOS $min_macos+)"
  if [ "$TAG" != "$NEWEST_TAG" ] && [ -z "${CHROME2_ENGINE_BASE_URL:-}" ] && [ -z "$TOKEN" ]; then
    local newest=""  # (best effort: is there a newer engine than the release asked for?)
    if curl -fsSL --max-time 20 -o "$tmp/newest.json" \
         "https://github.com/$REPO/releases/download/$NEWEST_TAG/$MANIFEST" 2>/dev/null; then
      newest=$(read_manifest "$tmp/newest.json" version 2>/dev/null || true)
    fi
    if [ -n "$newest" ] && [ "$newest" != "$version" ]; then
      say "Note: the newest engine is $newest ($TAG was asked for). Run this without CHROME2_ENGINE_TAG to get it."
    fi
  fi

  mkdir -p "$ENGINE_HOME"
  local dir
  for dir in "$ENGINE_HOME"/.unpacking.*; do  # left over from an interrupted run
    if is_stage_dir "$dir"; then rm -rf "$dir"; fi
  done
  local target="$ENGINE_HOME/$version"
  if { [ -e "$target" ] || [ -L "$target" ]; } && ! is_engine_dir "$target"; then
    die "$(shell_path "$target") exists but wasn't made by this installer - move it away (or set CHROME2_ENGINE_HOME) and run this again."
  fi

  # Download + check + unpack into $target (replacing an older copy of the same version)
  install_engine() {
    if [ -e "$target" ] && engine_in_use "$target"; then
      die "Chrome 2 is running on the engine in $(shell_path "$target"). Quit Chrome 2 and run this again."
    fi
    step "Downloading the engine (${size:+$((size / 1000000)) MB})"
    get "$tarball.sha256" "$tmp/$tarball.sha256" quiet || die "Couldn't download $tarball.sha256."
    get "$tarball" "$tmp/$tarball" || die "Couldn't download $BASE_URL/$tarball."
    local published actual
    published=$(awk '{ print $1; exit }' "$tmp/$tarball.sha256")
    actual=$(shasum -a 256 "$tmp/$tarball" | awk '{ print $1 }')
    [ -n "$published" ] && [ "$actual" = "$published" ] ||
      die "The download is damaged (SHA-256 $actual, expected $published). Run this again."
    [ -z "$expected_sha" ] || [ "$expected_sha" = "$published" ] ||
      die "The release's manifest and checksum file disagree; try again in a few minutes."
    say "SHA-256 OK: $actual"

    step "Unpacking into $(shell_path "$ENGINE_HOME")"
    stage=$(mktemp -d "$ENGINE_HOME/.unpacking.XXXXXX")
    : > "$stage/$MARK_STAGE"
    # shellcheck disable=SC2064
    trap "rm -rf '$tmp' '$stage'" EXIT
    tar -xzf "$tmp/$tarball" -C "$stage" </dev/null
    [ -f "$stage/chrome2-engine/site/chrome2_engine_env.py" ] || die "The download doesn't contain the engine."
    [ "$(read_manifest "$stage/chrome2-engine/manifest.json" version)" = "$version" ] ||
      die "The download doesn't match the release manifest."
    xattr -dr com.apple.quarantine "$stage/chrome2-engine" 2>/dev/null || true
    printf '%s\n' "$actual" > "$stage/chrome2-engine/$MARK_INSTALLED"
    if [ -e "$target" ]; then
      is_engine_dir "$target" || die "$(shell_path "$target") wasn't made by this installer; not replacing it."
      if engine_in_use "$target"; then
        die "Chrome 2 is running on the engine in $(shell_path "$target"). Quit Chrome 2 and run this again."
      fi
      mv "$target" "$stage/replaced"
    fi
    mv "$stage/chrome2-engine" "$target"
    rm -rf "$stage"
  }

  # Is the engine in $target complete and unchanged? (files.sha256 lists every file of the bundle)
  engine_intact() {
    [ -f "$target/site/chrome2_engine_env.py" ] || return 1
    [ -f "$target/files.sha256" ] || return 0  # (an engine built before the list existed: the self-test decides)
    (cd "$target" && shasum -a 256 -c --status files.sha256) </dev/null 2>/dev/null
  }

  local fresh=1
  if [ "$reinstall" = 0 ] && is_engine_dir "$target" && [ -n "$expected_sha" ] &&
     [ "$(cat "$target/$MARK_INSTALLED")" = "$expected_sha" ]; then
    if engine_intact; then
      fresh=0
      if [ "$TAG" = "$NEWEST_TAG" ]; then
        say "Already installed in $(shell_path "$target") - the newest engine."
      else
        say "Already installed in $(shell_path "$target")."
      fi
    else
      say "The engine in $(shell_path "$target") is damaged (files are missing or changed): installing it again."
    fi
  fi
  [ "$fresh" = 0 ] || install_engine

  # 3. The Python environment
  step "Setting up $(shell_path "$VENV")"
  if [ -e "$VENV" ] || [ -L "$VENV" ]; then
    is_our_venv "$VENV" ||
      die "$(shell_path "$VENV") exists but wasn't made by this installer - move it away (or set CHROME2_VENV to another folder) and run this again."
    if ! { [ -x "$VENV/bin/python3" ] && python_ok "$VENV/bin/python3"; }; then
      say "Re-creating it (it was made with another Python)."
      rm -rf "$VENV"
    fi
  fi
  if [ ! -e "$VENV" ]; then
    "$PYTHON" -m venv "$VENV" </dev/null
    : > "$VENV/$MARK_VENV"
  fi
  local venv_python="$VENV/bin/python3" purelib
  purelib=$("$venv_python" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
  # A pip-installed PyQt6 in this environment would compete with the engine's: remove it, and keep pip from
  # installing it again (a constraints file that no PyQt6 release satisfies, set in the environment's pip.conf).
  "$venv_python" -m pip uninstall -y -q PyQt6 PyQt6-Qt6 PyQt6-sip PyQt6-WebEngine PyQt6-WebEngine-Qt6 \
    </dev/null >/dev/null 2>&1 || true
  local constraints="$VENV/chrome2-engine-constraints.txt" package
  {
    printf '# Written by install-engine.sh: PyQt6 and Qt WebEngine come from the Chrome 2 engine (see chrome2-engine.pth),\n'
    printf '# so pip must not install them into this environment.\n'
    for package in PyQt6 PyQt6-Qt6 PyQt6-sip PyQt6-WebEngine PyQt6-WebEngine-Qt6; do
      printf '%s===%s\n' "$package" "$PIN"
    done
  } > "$constraints"
  if [ ! -e "$VENV/pip.conf" ] || grep -q 'install-engine.sh' "$VENV/pip.conf"; then
    {
      printf '# Written by install-engine.sh: pip may not install PyQt6 here - the Chrome 2 engine brings its own.\n'
      printf '[install]\nconstraint = %s\n' \
        "$("$PYTHON" -c 'import pathlib, sys; print(pathlib.Path(sys.argv[1]).as_uri())' "$constraints")"
    } > "$VENV/pip.conf"
  fi
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

  # 4. Self-test (an engine that was already installed and fails it is downloaded again, once)
  local rc loaded engine_real
  self_test() {
    rc=0
    loaded=$("$venv_python" -c 'import os, PyQt6.QtCore as m; print("\n" + os.path.realpath(m.__file__))' \
               </dev/null 2>"$tmp/selftest.log" | tail -n 1) || rc=1
    if [ "$rc" != 0 ]; then
      say "PyQt6 doesn't load in $(shell_path "$VENV")."
      return 0
    fi
    engine_real=$(cd "$target" && pwd -P)
    case $loaded in
      "$engine_real"/*) ;;
      *) die "PyQt6 loads from $loaded instead of the engine ($engine_real)." ;;
    esac
    step "Self-test"
    "$venv_python" -m chrome2_engine_selftest </dev/null 2>"$tmp/selftest.log" || rc=$?
  }
  self_test
  if [ "$rc" != 0 ] && [ "$rc" != 2 ] && [ "$fresh" = 0 ]; then
    say "(the test's messages:)"
    tail -n 15 "$tmp/selftest.log" || true
    say "The installed engine failed its self-test: downloading it again."
    fresh=1
    install_engine
    self_test
  fi
  [ "$rc" = 0 ] || { say "(the test's messages:)"; tail -n 25 "$tmp/selftest.log" || true; }
  case $rc in
    0) ;;
    2) warn "H.264 and AAC are supported, but the background test couldn't confirm that the test clip plays with sound - try a video with sound in Chrome 2 itself." ;;
    *) die "The self-test failed (see above). Run this again with --reinstall to download the engine again. Your previous setup still works: ~/foxglove-env/bin/python3 $(shell_path "$APP_SCRIPT")" ;;
  esac

  # Old engine versions: removed unless a running Chrome 2 still uses them (and only folders this made).
  local old
  for old in "$ENGINE_HOME"/*; do
    if [ "$old" = "$target" ] || ! is_engine_dir "$old"; then continue; fi
    if engine_in_use "$old"; then
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
  say "A visible playback test on this Mac:   $(shell_path "$venv_python") -m chrome2_engine_selftest --platform cocoa"
  say "Back to the old engine any time:   ~/foxglove-env/bin/python3 $(shell_path "$APP_SCRIPT")"
}

main "$@"
