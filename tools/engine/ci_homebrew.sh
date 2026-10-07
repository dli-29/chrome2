#!/bin/bash
# CI helper:  ci_homebrew.sh hide | restore
# Moves Homebrew (/opt/homebrew, and an Intel-style /usr/local/Cellar + opt if present) out of the way, so a
# test proves the engine needs nothing from it - like on the user's Mac, which has no Homebrew.
set -euo pipefail

case "${1:-}" in
  hide)
    for dir in /opt/homebrew /usr/local/Cellar /usr/local/opt /usr/local/Homebrew; do
      if [ -e "$dir" ]; then sudo mv "$dir" "$dir.hidden-for-test"; echo "hid $dir"; fi
    done
    hash -r
    for dir in /opt/homebrew /usr/local/Cellar /usr/local/opt; do
      if [ -e "$dir" ]; then echo "still there: $dir" >&2; exit 1; fi
    done
    if command -v brew >/dev/null 2>&1; then echo "brew is still on PATH: $(command -v brew)" >&2; exit 1; fi
    echo "Homebrew is hidden:"; find /opt -maxdepth 1 -name "homebrew*" -print
    ;;
  restore)
    for dir in /opt/homebrew /usr/local/Cellar /usr/local/opt /usr/local/Homebrew; do
      if [ -e "$dir.hidden-for-test" ] && [ ! -e "$dir" ]; then sudo mv "$dir.hidden-for-test" "$dir"; echo "restored $dir"; fi
    done
    ;;
  *) echo "usage: $0 hide|restore" >&2; exit 2 ;;
esac
