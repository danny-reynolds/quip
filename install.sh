#!/bin/sh
# Install quip:
#
#   curl -fsSL https://raw.githubusercontent.com/danny-reynolds/quip/main/install.sh | sh
#
# This only fetches the latest release and hands over to `quip install`,
# which does the real work. Options are passed through, for example:
#
#   curl -fsSL .../install.sh | sh -s -- --no-auto-update
#   curl -fsSL .../install.sh | sh -s -- --uninstall
#
# Environment:
#   QUIP_REF          install this tag or branch instead (v1.0.0, main)
#   QUIP_TARBALL      install from a local tarball, with no network at all
#   QUIP_UPDATE_BASE  repo URL (default https://github.com/danny-reynolds/quip)
#   QUIP_HOME         where the code and lines go (default ~/.local/share/quip)
#   QUIP_BIN          where the quip command goes (default ~/.local/bin)
#
# No root, no sudo, nothing outside your home folder unless you say so.

set -eu

BASE="${QUIP_UPDATE_BASE:-https://github.com/danny-reynolds/quip}"
BASE="${BASE%/}"

say() { printf '%s\n' "$*" >&2; }
die() { say "quip install: $*"; exit 4; }

case "${1:-}" in
    --uninstall)
        home="${QUIP_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/quip}"
        [ -f "$home/quip.py" ] || die "quip isn't installed in $home, so there's nothing to remove"
        exec python3 "$home/quip.py" uninstall
        ;;
esac

# Check what we need before touching anything.
command -v python3 >/dev/null 2>&1 \
    || die "python3 not found. On Debian: sudo apt install python3"
python3 -c 'import sys; sys.exit(sys.version_info < (3, 8))' \
    || die "python3 is $(python3 -c 'import platform; print(platform.python_version())'); quip needs 3.8 or later"
command -v tar >/dev/null 2>&1 \
    || die "tar not found. On Debian: sudo apt install tar"

fetch=""
if [ -z "${QUIP_TARBALL:-}" ]; then
    if command -v curl >/dev/null 2>&1; then
        fetch=curl
    elif command -v wget >/dev/null 2>&1; then
        fetch=wget
    else
        die "neither curl nor wget found. On Debian: sudo apt install curl"
    fi
fi

# fetch URL FILE
get() {
    if [ "$fetch" = curl ]; then
        curl -fsSL --proto '=https,http' -o "$2" "$1"
    else
        wget -q -O "$2" "$1"
    fi
}

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
trap 'exit 4' HUP INT TERM

if [ -n "${QUIP_TARBALL:-}" ]; then
    [ -f "$QUIP_TARBALL" ] || die "no such tarball: $QUIP_TARBALL"
    cp "$QUIP_TARBALL" "$tmp/quip.tar.gz"
else
    ref="${QUIP_REF:-}"
    if [ -z "$ref" ]; then
        get "$BASE/raw/main/VERSION" "$tmp/VERSION" \
            || die "couldn't fetch the latest version number from $BASE"
        version="$(tr -d ' \r\n' < "$tmp/VERSION")"
        case "$version" in
            ''|*[!0-9.]*) die "unexpected version number: $version" ;;
        esac
        ref="v$version"
    fi
    case "$ref" in
        [0-9]*) ref="v$ref" ;;
    esac
    say "Downloading quip $ref"
    get "$BASE/archive/refs/tags/$ref.tar.gz" "$tmp/quip.tar.gz" 2>/dev/null \
        || get "$BASE/archive/refs/heads/$ref.tar.gz" "$tmp/quip.tar.gz" \
        || die "couldn't download $ref from $BASE"
fi

mkdir "$tmp/src"
tar -xzf "$tmp/quip.tar.gz" -C "$tmp/src" || die "the download isn't a valid tarball"

src=""
for candidate in "$tmp/src" "$tmp"/src/*; do
    if [ -f "$candidate/quip.py" ]; then
        src="$candidate"
        break
    fi
done
[ -n "$src" ] || die "the tarball doesn't contain quip.py"

python3 "$src/quip.py" install --from "$src" "$@"
