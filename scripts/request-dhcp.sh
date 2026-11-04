#!/bin/sh
# Initial DHCP trial on the observed unmanaged interface, using the installed client.
set -eu
umask 077
PATH=${PATH:-/usr/bin:/bin}:/usr/sbin:/sbin
export PATH LC_ALL=C SYSTEMD_PAGER=cat SYSTEMD_COLORS=0

if [ "$#" -ne 0 ]; then
    printf 'Usage: sudo sh %s\nOnly requests DHCPv4 on eth1.\n' "$0" >&2
    exit 1
fi
if [ "$(id -u)" -ne 0 ]; then
    printf 'Run with administrative access (for example sudo sh %s).\n' "$0" >&2
    exit 1
fi
for tool in dhclient networkctl ps awk; do
    if ! command -v "$tool" >/dev/null 2>&1; then
        printf 'Required tool unavailable: %s\n' "$tool" >&2
        exit 1
    fi
done

state=$(networkctl --no-pager status eth1)
case "$state" in
    *"(unmanaged)"*) ;;
    *) printf 'eth1 is not confirmed unmanaged; refusing a competing client.\n' >&2
       exit 1 ;;
esac

# Capture first so a failed process listing cannot look like an empty result.
processes=$(ps -eo comm=,args=)
if printf '%s\n' "$processes" | awk '
    $1 ~ /^(dhclient|dhcpcd|udhcpc)$/ ||
    $0 ~ /(^|[[:space:]\/])dhcp-loop-eth1\.sh([[:space:]]|$)/ { found=1 }
    END { exit found ? 0 : 1 }
'; then
    printf 'A DHCP client or vendor DHCP loop exists; inspect it before retrying.\n' >&2
    exit 1
fi

printf 'Requesting DHCPv4 on eth1 using the existing DHCP hooks.\n'
printf 'Addresses, routes and DNS may change; remote access may be interrupted.\n'
printf 'No boot configuration is added; the client may stay running to renew its lease.\n'
exec dhclient -4 -v -1 eth1
