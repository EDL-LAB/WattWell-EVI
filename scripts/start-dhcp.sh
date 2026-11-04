#!/bin/sh
# The setup command and boot service share the same single-client guard.
set -eu
PATH=/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin
export PATH LC_ALL=C
case "${1-}" in ''|--once) ;; *) exit 1 ;; esac
state=$(networkctl --no-pager status eth1)
case "$state" in
    *"(unmanaged)"*) ;;
    *) printf 'eth1 já é gerida; não será iniciado outro cliente DHCP.\n'; exit 0 ;;
esac
processes=$(ps -eo comm=,args=)
if printf '%s\n' "$processes" | awk '
    $1 ~ /^(dhclient|dhcpcd|udhcpc)$/ ||
    $0 ~ /(^|[[:space:]\/])dhcp-loop-eth1\.sh([[:space:]]|$)/ { found=1 }
    END { exit found ? 0 : 1 }
'; then
    printf 'Já existe cliente DHCP ou loop do fabricante; será preservado.\n'
    exit 0
fi
if [ "${1-}" = --once ]; then
    exec dhclient -4 -v -1 -cf /var/lib/wattwell-evi/dhclient.conf \
        -sf /usr/local/lib/wattwell-evi/dhclient-hook.sh \
        -pf /run/wattwell-eth1-dhcp.pid -lf /var/lib/wattwell-evi/dhclient.leases eth1
fi
exec dhclient -4 -d -cf /var/lib/wattwell-evi/dhclient.conf \
    -sf /usr/local/lib/wattwell-evi/dhclient-hook.sh \
    -pf /run/wattwell-eth1-dhcp.pid -lf /var/lib/wattwell-evi/dhclient.leases eth1
