#!/usr/bin/env bash
# Vendor the system shared libraries Qt6 needs into ./vendor/runtime, without root.
# Debian/Ubuntu only. Uses `apt-get download` + `dpkg-deb -x` into a private prefix.
#
# This is a development convenience for containers without apt privileges.
# On a normal Debian/Ubuntu desktop you do NOT need this: install the distro
# packages listed in docs/DEPENDENCIES.md instead.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEST="${1:-$HERE/vendor/runtime}"
ARCH="$(dpkg --print-architecture)"
APT_STATE="${APT_STATE:-/tmp/opticrip-apt}"

PKGS=(
  libxkbcommon0 libxkbcommon-x11-0 libxcb-cursor0 libegl1 libfontconfig1
  libfreetype6 libxcb-xkb1 libxcb-icccm4 libxcb-image0 libxcb-keysyms1
  libxcb-render-util0 libxcb-shape0 libxcb-xinerama0 libxcb-util1
  libx11-xcb1 libxcb-glx0 libexpat1 libpng16-16t64 libbrotli1 libgraphite2-3
  libharfbuzz0b libglib2.0-0t64 libpcre2-8-0 libbz2-1.0
)

mkdir -p "$DEST/root" "$APT_STATE/lists" "$APT_STATE/archives"
printf 'deb http://archive.ubuntu.com/ubuntu noble main universe\ndeb http://archive.ubuntu.com/ubuntu noble-updates main universe\n' \
  > "$APT_STATE/sources.list"

APTOPTS=(-o "Dir::State::lists=$APT_STATE/lists" -o "Dir::Cache=$APT_STATE/archives"
  -o "Dir::Etc::sourcelist=$APT_STATE/sources.list" -o Dir::Etc::sourceparts=/dev/null
  -o APT::Get::List-Cleanup=0 -o Acquire::Languages=none)

if [ ! -f "$APT_STATE/lists/lock" ] && [ -z "$(ls -A "$APT_STATE/lists" 2>/dev/null)" ]; then
  echo ">> updating private apt lists"
  apt-get "${APTOPTS[@]}" update
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
echo ">> downloading ${#PKGS[@]} packages for $ARCH"
(cd "$work" && apt-get "${APTOPTS[@]}" download "${PKGS[@]}")

echo ">> extracting into $DEST/root"
for deb in "$work"/*.deb; do dpkg-deb -x "$deb" "$DEST/root"; done

# Expose the extracted libdir; find where the libs landed.
# dpkg reports "amd64" but the multiarch triplet dir is x86_64-linux-gnu; discover it.
libdir="$(dirname "$(find "$DEST/root/usr/lib" -name "libxkbcommon.so.0" | head -1)")"
echo ">> done. Add this to your environment:"
echo "   export LD_LIBRARY_PATH=$libdir:\${LD_LIBRARY_PATH:-}"
echo "$libdir" > "$DEST/libdir.txt"
