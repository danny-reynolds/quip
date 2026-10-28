#!/bin/sh
# Show a work-safe boot line on the Plymouth splash, falling back to a
# fixed string if quip has nothing suitable (exit 1) or isn't there.
#
# Boot scripts don't see your PATH, so give quip's full path in QUIP, and
# point QUIP_STATE somewhere writable if $HOME isn't (no-repeat is skipped,
# with a warning, if it can't be saved).

QUIP="${QUIP:-$HOME/.local/bin/quip}"

line="$("$QUIP" get boot --max 3 --width 70 --no-repeat --set hostname="$(hostname)" 2>/dev/null)" \
    || line="Starting up."

if command -v plymouth >/dev/null 2>&1 && plymouth --ping 2>/dev/null; then
    plymouth display-message --text="$line"
else
    printf '%s\n' "$line"
fi
