#!/bin/sh
# A login banner line. Drop a copy in /etc/update-motd.d/ (as, say,
# 60-quip) and make it executable. Set QUIP to quip's full path, because
# update-motd doesn't see your PATH.

QUIP="${QUIP:-quip}"

count="$(systemctl list-units --state=running --type=service --no-legend 2>/dev/null | wc -l)"

"$QUIP" get status --width 78 --no-repeat \
    --set hostname="$(hostname)" --set count="$count" 2>/dev/null \
    || echo "Nothing to report."
