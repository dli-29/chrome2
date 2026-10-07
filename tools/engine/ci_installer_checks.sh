#!/bin/bash
# CI helper:  ci_installer_checks.sh DIST_DIR   (after a normal install into $HOME, the test job's fake home)
#
# The installer's safety and repair paths, run exactly as the user would run the installer:
#   1. a damaged engine is repaired by running the installer again (files.sha256), and an engine that fails its
#      self-test is downloaded again
#   2. an engine that a running Chrome 2 uses is never replaced
#   3. pip can't add PyQt6 to the environment, and a pip-installed PyQt6 can't mix into the engine's
#   4. only folders the installer made are ever removed - with other folders next to the engines, with
#      CHROME2_ENGINE_HOME set to ~/Library/Application Support itself (where Chrome 2's data lives), with
#      CHROME2_VENV pointing at a Python environment it didn't make, and with --uninstall
set -euo pipefail

dist=$(cd "$1" && pwd)
export CHROME2_ENGINE_BASE_URL="file://$dist"
PYORG=${PYORG:-/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14}
installer="$dist/install-engine.sh"
logs="${RUNNER_TEMP:-/tmp}/logs"
mkdir -p "$logs"
appsup="$HOME/Library/Application Support"
engines="$appsup/Chrome2 Engine"
vpy="$HOME/chrome2-env/bin/python3"
version=$("$PYORG" -c 'import json, sys; print(json.load(open(sys.argv[1]))["version"])' "$dist/chrome2-engine-manifest.json")
target="$engines/$version"
failures=0

check() {  # check "description" command...
  local what=$1
  shift
  if "$@"; then echo "PASS $what"; else echo "FAIL $what"; failures=$((failures + 1)); fi
}
installer_run() {  # installer_run NAME [env VAR=VALUE...] -- [installer args]: output in $logs/NAME.log, status in $status
  local name=$1
  shift
  local envs=()
  while [ "$#" -gt 0 ] && [ "$1" != -- ]; do envs+=("$1"); shift; done
  [ "$#" -gt 0 ] && shift
  echo "==== $name: ${envs[*]:-} install-engine.sh $*"
  status=0
  env ${envs[@]+"${envs[@]}"} /bin/bash "$installer" "$@" > "$logs/$name.log" 2>&1 || status=$?
  sed 's/^/    | /' "$logs/$name.log" | grep -v '^    | *$' | tail -n 30
  echo "     (exit status $status)"
}
logged() { grep -q -- "$2" "$logs/$1.log"; }
intact() { (cd "$target" && shasum -a 256 -c --status files.sha256); }

# 1a. Damaged engine (a file deleted) -> repaired
pak=$(find "$target" -name qtwebengine_resources.pak -type f | head -n 1)
[ -n "$pak" ] || { echo "no qtwebengine_resources.pak in $target"; exit 1; }
rm "$pak"
installer_run repair-missing-file --
check "re-running the installer notices the missing file" logged repair-missing-file "is damaged"
check "... and repairs the engine" test "$status" = 0 -a -f "$pak"
check "... which passes its self-test again" logged repair-missing-file "AAC audio: decoded"
check "... and is complete" intact

# 1b. An engine without a file list (older builds) that fails its self-test -> downloaded again
core=$(find "$target" -path '*/QtWebEngineCore.framework/Versions/A/QtWebEngineCore' -type f | head -n 1)
rm "$target/files.sha256"
: > "$core"
installer_run repair-selftest --
check "a failing self-test on an installed engine makes the installer download it again" \
  logged repair-selftest "failed its self-test: downloading it again"
check "... and the result works" test "$status" = 0 -a -s "$core" -a -f "$target/files.sha256"
check "... and is complete" intact

# 2. In use: a Python process that has the engine's Qt loaded (like a running Chrome 2)
"$vpy" -c 'import PyQt6.QtCore, time; print("ready", flush=True); time.sleep(600)' > "$logs/sleeper.log" 2>&1 &
sleeper=$!
for _ in $(seq 60); do grep -q ready "$logs/sleeper.log" && break; sleep 0.5; done
installer_run reinstall-in-use -- --reinstall
check "--reinstall refuses to replace an engine a running program uses" test "$status" != 0
check "... saying so" logged reinstall-in-use "Chrome 2 is running on the engine"
check "... and leaves it intact" intact
kill "$sleeper" 2>/dev/null || true
wait "$sleeper" 2>/dev/null || true
installer_run reinstall -- --reinstall
check "--reinstall works once nothing uses the engine" test "$status" = 0
check "... and downloads it again" logged reinstall "SHA-256 OK"

# 3. pip and PyQt6
status=0
"$vpy" -m pip install --dry-run --disable-pip-version-check PyQt6 > "$logs/pip-pyqt6.log" 2>&1 || status=$?
tail -n 5 "$logs/pip-pyqt6.log"
check "pip refuses to install PyQt6 into ~/chrome2-env (pip.conf constraint)" test "$status" != 0
purelib=$("$vpy" -c 'import sysconfig; print(sysconfig.get_path("purelib"))')
mkdir "$purelib/PyQt6"
: > "$purelib/PyQt6/__init__.py"
printf 'raise SystemExit("the pip-installed PyQt6 was used")\n' > "$purelib/PyQt6/QtMultimedia.py"
out=$("$vpy" - 2>&1 <<'PY' || true
import os
import PyQt6, PyQt6.QtCore
try:
    import PyQt6.QtMultimedia
    print("IMPORTED")
except ModuleNotFoundError:
    print("NOT-FOUND")
print("PyQt6.__path__", PyQt6.__path__, "QtCore from", os.path.realpath(PyQt6.QtCore.__file__))
PY
)
printf "%s\n" "$out"
check "a pip-installed PyQt6 can't add modules to the engine's PyQt6" grep -q NOT-FOUND <<<"$out"
check "... and Python warns about it" grep -q "ignoring the pip-installed PyQt6" <<<"$out"
rm -rf "$purelib/PyQt6"

# 4. Only what the installer made is removed
mkdir -p "$engines/My notes" "$engines/6.0.0-b1" "$engines/6.0.0-b0"
echo keep > "$engines/My notes/keep.txt"
echo keep > "$engines/6.0.0-b1/keep.txt"  # looks like an engine version, but the installer didn't make it
printf '{\n  "name": "chrome2-engine",\n  "version": "6.0.0-b0"\n}\n' > "$engines/6.0.0-b0/manifest.json"
echo old > "$engines/6.0.0-b0/.installed-sha256"  # an old engine the installer made
mkdir -p "$appsup/Foxglove" "$appsup/Another App"
echo keep > "$appsup/Foxglove/ci-keep.txt"
echo keep > "$appsup/Another App/keep.txt"
installer_run other-folders --
check "an old engine version is removed" test "$status" = 0 -a ! -e "$engines/6.0.0-b0"
check "... but not other folders next to it" test -f "$engines/My notes/keep.txt" -a -f "$engines/6.0.0-b1/keep.txt"

second="$HOME/second env"
installer_run engine-home-appsup CHROME2_ENGINE_HOME="$appsup" CHROME2_VENV="$second" CHROME2_SKIP_PIP=1 --
check "CHROME2_ENGINE_HOME=~/Library/Application Support installs there" test "$status" = 0 -a -d "$appsup/$version"
check "... without touching Chrome 2's data or other apps' folders" \
  test -f "$appsup/Foxglove/ci-keep.txt" -a -f "$appsup/Another App/keep.txt" -a -d "$appsup/Foxglove/Profiles"
check "... or the other engine folder" intact
status=0
"$second/bin/python3" -m pip install --dry-run --disable-pip-version-check PyQt6 > "$logs/pip-pyqt6-2.log" 2>&1 || status=$?
check "the pip.conf constraint works in an environment whose path has a space" test "$status" != 0
installer_run engine-home-appsup-uninstall CHROME2_ENGINE_HOME="$appsup" CHROME2_VENV="$second" -- --uninstall
check "--uninstall with that setting removes its engine and environment" \
  test "$status" = 0 -a ! -e "$appsup/$version" -a ! -e "$second"
check "... and nothing else in Application Support" \
  test -f "$appsup/Foxglove/ci-keep.txt" -a -f "$appsup/Another App/keep.txt" -a -d "$appsup/Foxglove/Profiles" -a -d "$appsup"

mkdir -p "$HOME/foxglove-env/bin"
printf 'home = /Library/Frameworks/Python.framework/Versions/3.14/bin\n' > "$HOME/foxglove-env/pyvenv.cfg"
installer_run venv-not-ours CHROME2_VENV="$HOME/foxglove-env" CHROME2_SKIP_PIP=1 --
check "the installer won't take over a Python environment it didn't make" \
  test "$status" != 0 -a -f "$HOME/foxglove-env/pyvenv.cfg"
installer_run uninstall-venv-not-ours CHROME2_VENV="$HOME/foxglove-env" -- --uninstall
check "--uninstall leaves a Python environment it didn't make alone" \
  test "$status" = 0 -a -f "$HOME/foxglove-env/pyvenv.cfg"
check "--uninstall removes the engine versions" test ! -e "$target"
check "... but keeps the folders it didn't make (and so the engines folder)" \
  test -f "$engines/My notes/keep.txt" -a -f "$engines/6.0.0-b1/keep.txt"
installer_run uninstall -- --uninstall
check "--uninstall removes ~/chrome2-env" test "$status" = 0 -a ! -e "$HOME/chrome2-env"
check "... and never touches Chrome 2's data" test -f "$appsup/Foxglove/ci-keep.txt" -a -d "$appsup/Foxglove/Profiles"

echo
if [ "$failures" = 0 ]; then echo "ALL INSTALLER CHECKS PASSED"; else echo "$failures INSTALLER CHECK(S) FAILED"; exit 1; fi
