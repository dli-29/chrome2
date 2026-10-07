#!/bin/bash
# CI helper: install the newest python.org Python 3.14 with its official macOS installer (.pkg) - the same
# Python the user has (/Library/Frameworks/Python.framework/Versions/3.14). Needs sudo (GitHub runners have it).
set -euo pipefail

index=$(curl -fsSL --retry 3 https://www.python.org/ftp/python/)
url=""
for version in $(printf '%s\n' "$index" | grep -oE 'href="3\.14\.[0-9]+/"' | grep -oE '3\.14\.[0-9]+' | sort -t. -k3,3nr -u); do
  candidate="https://www.python.org/ftp/python/$version/python-$version-macos11.pkg"
  if curl -fsIL --retry 2 -o /dev/null "$candidate"; then url=$candidate; break; fi
done
[ -n "$url" ] || { echo "No python.org Python 3.14 installer found" >&2; exit 1; }
pkg="${RUNNER_TEMP:-/tmp}/python-3.14.pkg"
echo "Installing $url"
curl -fsSL --retry 3 -o "$pkg" "$url"
pkgutil --check-signature "$pkg" | head -4
sudo installer -pkg "$pkg" -target /
PY=/Library/Frameworks/Python.framework/Versions/3.14/bin/python3.14
"$PY" -VV
"$PY" -c 'import platform, sys, sysconfig; print("machine", platform.machine(), "| free-threaded", bool(sysconfig.get_config_var("Py_GIL_DISABLED")), "| prefix", sys.base_prefix)'
APP=/Library/Frameworks/Python.framework/Versions/3.14/Resources/Python.app/Contents/MacOS/Python
codesign -dv "$APP" 2>&1 | grep -E 'Identifier|flags|TeamIdentifier' || true
codesign -d --entitlements - "$APP" 2>/dev/null | grep -oE 'com\.apple\.security\.[a-z.-]+' || true
