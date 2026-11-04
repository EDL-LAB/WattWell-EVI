#!/bin/sh
# Restore this device's permanent addresses after the existing vendor DHCP hooks.
set -u
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH LC_ALL=C
[ "${interface-}" = eth1 ] || exit 1
[ -s /var/lib/wattwell-evi/static-addresses ] || exit 1
result=0
/sbin/dhclient-script || result=$?
while read -r address label; do
    [ -n "$address" ] || continue
    current=$(ip -4 -o addr show dev eth1) || exit 1
    if ! printf '%s\n' "$current" | awk -v address="$address" '$4 == address { found=1 } END { exit found ? 0 : 1 }'; then
        ip -4 addr add "$address" dev eth1 label "$label" || result=1
    fi
done < /var/lib/wattwell-evi/static-addresses
exit "$result"
