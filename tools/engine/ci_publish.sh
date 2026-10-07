#!/bin/bash
# CI helper (publish job, needs GH_TOKEN with contents: write):  ci_publish.sh DIST_DIR VERSION_TAG NEWEST_TAG
#
# Publishes the tested engine in DIST_DIR to two releases:
#   NEWEST_TAG (engine-arm64)               always the newest engine - the installer's default, so running the
#                                           one-line command again upgrades Qt / Chromium; marked "Latest"
#   VERSION_TAG (engine-qt<version>-arm64)  history: the newest build for that Qt version
# A release is never left half-updated, so an install that runs meanwhile sees the old set or the new one:
#   1. this build's tarball + .sha256 go up under their own names (chrome2-engine-arm64-<version>.tar.gz)
#   2. the manifest (which names that tarball and its SHA-256), then install-engine.sh, are uploaded under a
#      temporary name and renamed over the old ones
#   3. then the fixed-name copies (chrome2-engine-arm64.tar.gz + .sha256, for downloads in the browser)
#   4. older versioned tarballs are deleted - except the previous one, for installs that are under way
set -euo pipefail

dist=$1 version_tag=$2 newest_tag=$3
repo=$GITHUB_REPOSITORY
manifest=chrome2-engine-manifest.json
field() { python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))[sys.argv[2]])' "$dist/$manifest" "$1"; }
tarball=$(field tarball)
alias_name=$(field tarball_alias)
digest=$(field tarball_sha256)
version=$(field version)
qt=$(field qt_version)
work=$(mktemp -d)

(cd "$dist" && sha256sum -c "$tarball.sha256")
[ "$(awk '{ print $1 }' "$dist/$tarball.sha256")" = "$digest" ] || { echo "manifest and .sha256 disagree" >&2; exit 1; }
cp "$dist/$tarball" "$work/$alias_name"
printf '%s  %s\n' "$digest" "$alias_name" > "$work/$alias_name.sha256"

asset_id() {  # asset_id RELEASE_ID NAME
  gh api "repos/$repo/releases/$1/assets?per_page=100" --jq "[.[] | select(.name == \"$2\") | .id][0] // empty"
}

swap_in() {  # swap_in TAG RELEASE_ID FILE NAME: upload FILE as NAME.next, then rename it over NAME
  local tag=$1 rid=$2 file=$3 name=$4 old new
  mkdir -p "$work/next"
  cp "$file" "$work/next/$name.next"
  old=$(asset_id "$rid" "$name.next")
  if [ -n "$old" ]; then gh api -X DELETE "repos/$repo/releases/assets/$old"; fi
  gh release upload "$tag" "$work/next/$name.next" --repo "$repo"
  new=$(asset_id "$rid" "$name.next")
  old=$(asset_id "$rid" "$name")
  if [ -n "$old" ]; then gh api -X DELETE "repos/$repo/releases/assets/$old"; fi
  gh api -X PATCH "repos/$repo/releases/assets/$new" -f name="$name" --jq '"  \(.name)  \(.size) bytes"'
}

notes() {  # notes TAG KIND(newest|version) > file
  python3 - "$1" "$2" "$repo" "${PRIVATE_REPO:-false}" "$dist/$manifest" "$newest_tag" <<'PY'
import json, sys
tag, kind, repo, private, path, newest = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4] == "true", sys.argv[5], sys.argv[6]
m = json.load(open(path))
url = f"https://github.com/{repo}/releases/download/{newest}/install-engine.sh"
lines = [
    f"Chrome 2's engine with **H.264 / AAC** (proprietary codecs): PyQt6 {m['pyqt_version']} + Qt WebEngine "
    f"{m['qt_version']} (Chromium {m['chromium_version']}), built from Homebrew's bottles and made self-contained.",
    "",
]
if kind == "version":
    lines += [f"This release keeps the newest build for Qt {m['qt_version']}. The install command below always "
              f"installs the **newest** engine (release `{newest}`); to install exactly this one, run it with "
              f"`CHROME2_ENGINE_TAG={tag}`.", ""]
else:
    lines += ["This release always holds the newest engine: running the install command again upgrades it "
              "(Qt, Chromium and their security fixes).", ""]
lines += [
    f"**Needs:** an Apple Silicon Mac, macOS {m['min_macos']} or newer, and python.org's Python 3.14. No admin password.",
    "",
    "**Install or upgrade** (Terminal):",
    "",
    "```",
    f'/bin/bash -c "$(curl -fsSL {url})"',
    "```",
    "",
    "Then start Chrome 2 with `~/chrome2-env/bin/python3 ~/Desktop/temp/foxglove.py`. Your data in "
    "`~/Library/Application Support/Foxglove` and your old `~/foxglove-env` are untouched.",
    "",
    "| | |",
    "|---|---|",
    f"| Engine version | {m['version']} |",
    f"| Tarball | `{m['tarball']}` ({m['tarball_bytes'] / 1e6:.0f} MB), SHA-256 `{m['tarball_sha256']}` |",
    f"| Unpacked | {m['size_bytes'] / 1e6:.0f} MB, {m['file_count']} files ({m['macho_count']} Mach-O) |",
    f"| Minimum macOS (largest LC_BUILD_VERSION minos) | {m['min_macos']} |",
    f"| Built on | macOS {m['build']['runner_macos']}, commit {m['build']['commit'][:12]}, run #{m['build']['build_number']} |",
    f"| Homebrew | qtbase {m['homebrew_versions'].get('qtbase')}, qtwebengine {m['homebrew_versions'].get('qtwebengine')}, pyqt {m['homebrew_versions'].get('pyqt')} |",
    "",
    "**Licenses and sources:** the bundle's `licenses/` folder has the license texts (Qt: LGPL-3.0, PyQt6: "
    "GPL-3.0, Chromium and the other libraries: their own) and `licenses/SOURCES.txt`, which lists every "
    "part with its version, license, upstream source and Homebrew's build recipe.",
    "",
    "**Codecs:** H.264, HEVC and AAC are covered by patents licensed through patent pools; no patent license "
    "comes with this download, which is meant for personal use. Check what applies to you before redistributing it.",
]
if private:
    lines += ["", "> **Note:** while this repository is private, these downloads need a GitHub login, so the one-line "
                  "command above gets *404*. Make the repository public, or download `install-engine.sh`, "
                  f"`{m['tarball_alias']}`, `{m['tarball_alias']}.sha256` and `chrome2-engine-manifest.json` in your "
                  "browser and run `CHROME2_ENGINE_BASE_URL=~/Downloads bash ~/Downloads/install-engine.sh`."]
print("\n".join(lines))
PY
}

publish() {  # publish TAG KIND
  local tag=$1 kind=$2 latest title rid
  if [ "$kind" = newest ]; then
    latest=--latest
    title="Chrome 2 engine (newest): Qt WebEngine $qt with H.264/AAC (Apple Silicon)"
  else
    latest=--latest=false
    title="Chrome 2 engine: Qt WebEngine $qt with H.264/AAC (Apple Silicon)"
  fi
  notes "$tag" "$kind" > "$work/notes-$kind.md"
  echo "==== $tag ($kind): engine $version"
  if ! gh release view "$tag" --repo "$repo" >/dev/null 2>&1; then
    gh release create "$tag" --repo "$repo" --title "$title" --notes-file "$work/notes-$kind.md" \
      --target "$GITHUB_SHA" "$latest"
  fi
  rid=$(gh api "repos/$repo/releases/tags/$tag" --jq .id)
  # 1. this build's own files: new names, so nothing a running install downloads changes
  gh release upload "$tag" "$dist/$tarball" "$dist/$tarball.sha256" --clobber --repo "$repo"
  # 2. the switch: the manifest names the new tarball; then the installer
  swap_in "$tag" "$rid" "$dist/$manifest" "$manifest"
  swap_in "$tag" "$rid" "$dist/install-engine.sh" install-engine.sh
  # 3. fixed-name copies for browser downloads
  swap_in "$tag" "$rid" "$work/$alias_name" "$alias_name"
  swap_in "$tag" "$rid" "$work/$alias_name.sha256" "$alias_name.sha256"
  gh release edit "$tag" --repo "$repo" --title "$title" --notes-file "$work/notes-$kind.md" "$latest" >/dev/null
  # the tag follows the commit these files were built from
  gh api -X PATCH "repos/$repo/git/refs/tags/$tag" -f sha="$GITHUB_SHA" -F force=true >/dev/null
  # 4. prune: keep this build's tarball and the previous one
  local keep="" line id name
  while read -r line; do
    id=${line%% *}
    name=${line#* }
    case $name in
      "$tarball" | "$tarball.sha256") continue ;;
    esac
    if [ -z "$keep" ]; then keep=${name%.sha256}; fi
    case $name in
      "$keep" | "$keep.sha256") continue ;;
    esac
    echo "  deleting old $name"
    gh api -X DELETE "repos/$repo/releases/assets/$id"
  done < <(gh api --paginate "repos/$repo/releases/$rid/assets?per_page=100" \
             --jq '[.[] | select(.name | test("^chrome2-engine-arm64-.+\\.tar\\.gz(\\.sha256)?$"))]
                   | sort_by(.created_at) | reverse | .[] | "\(.id) \(.name)"')
  gh release view "$tag" --repo "$repo" --json url,assets --jq '.url, (.assets[] | "  \(.name)  \(.size)")'
}

publish "$version_tag" version
publish "$newest_tag" newest
