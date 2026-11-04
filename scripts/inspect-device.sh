#!/bin/sh
# Only writes inside a newly created report directory. Never invokes vendor tools.
set -eu
umask 077
export LC_ALL=C
export SYSTEMD_PAGER=cat
export SYSTEMD_COLORS=0

usage() {
    printf 'Usage: sh %s /absolute/path/to/new-report-directory\n' "$0"
}

if [ "${1-}" = --help ] && [ "$#" -eq 1 ]; then
    usage
    exit 0
fi
if [ "$#" -ne 1 ]; then
    usage >&2
    exit 1
fi
case "$1" in
    /*) report=$1 ;;
    *) printf 'Report directory must be an absolute path.\n' >&2; exit 1 ;;
esac
# mkdir must fail for existing directories, files and symlinks: never reuse a report.
mkdir -m 700 -- "$report"
printf 'check\texit_code\n' > "$report/checks.tsv"
incomplete=0

capture() {
    name=$1
    shift
    if command -v "$1" >/dev/null 2>&1; then
        if "$@" > "$report/$name" 2> "$report/$name.stderr"; then
            code=0
        else
            code=$?
        fi
    else
        code=127
        : > "$report/$name"
        printf 'Command unavailable: %s\n' "$1" > "$report/$name.stderr"
    fi
    printf '%s\t%s\n' "$name" "$code" >> "$report/checks.tsv"
    if [ "$code" -ne 0 ]; then
        incomplete=1
    fi
}

capture timestamp.txt date -u '+%Y-%m-%dT%H:%M:%SZ'
capture kernel.txt uname -srmo
capture identity.txt id
capture os-release.txt cat /etc/os-release
capture debian-version.txt cat /etc/debian_version
capture wattwell-version.txt cat /usr/local/etc/distro_version
capture architecture.txt dpkg --print-architecture
capture foreign-architectures.txt dpkg --print-foreign-architectures
capture dpkg-version.txt dpkg --version
capture dpkg-audit.txt dpkg --audit
# Separate fields preserve multiarch identity, holds, and incomplete package states.
# shellcheck disable=SC2016 # These placeholders are expanded by dpkg-query.
capture packages.tsv dpkg-query -W '-f=${Package}\t${Architecture}\t${Version}\t${Status}\n'
capture selections.txt dpkg --get-selections
capture disk.txt df -Pk / /usr /var /tmp "$report"
capture mounts.txt findmnt -rn -o TARGET,SOURCE,FSTYPE
capture services.txt systemctl --no-pager --plain list-units --all --type=service
capture service-files.txt systemctl --no-pager --plain list-unit-files --type=service
capture timers.txt systemctl --no-pager --plain list-timers --all
capture timer-files.txt systemctl --no-pager --plain list-unit-files --type=timer
capture hostname.txt hostname
capture static-hostname.txt cat /etc/hostname
capture hosts.txt cat /etc/hosts
capture ip-addresses.txt ip addr show
capture ipv4-routes.txt ip -4 route show table all
capture ipv6-routes.txt ip -6 route show table all
capture processes.txt ps -eo pid,ppid,comm
# Absence here is inventory data, not a failed collection or a reason to install.
# shellcheck disable=SC2016 # Variables belong to the child shell.
capture available-tools.txt sh -c '
    for tool in apt apt-get apt-cache dpkg dhclient udhcpc dhcpcd curl wget \
        gpgv sha256sum hostnamectl avahi-daemon avahi-resolve-host-name; do
        printf "%s: " "$tool"
        command -v "$tool" || printf "unavailable\n"
    done
'

# Hash configuration and policy files without copying credentials or executing hooks.
# shellcheck disable=SC2016 # Variables belong to the child shell.
capture protected-files.sha256 sh -c '
    result=0
    for path in /etc/apt /etc/dpkg /etc/evis /etc/init.d \
        /etc/systemd/system /usr/local/etc /usr/sbin/policy-rc.d \
        /etc/network /etc/systemd/network /etc/NetworkManager /etc/dhcp \
        /etc/dhcpcd.conf /etc/hostname /etc/hosts /etc/resolv.conf \
        /etc/nsswitch.conf /etc/avahi; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            find "$path" -type f -exec sha256sum -- {} + || result=1
        fi
    done
    for path in /usr/local/bin/evi* /usr/local/bin/ev-combocs \
        /usr/local/bin/ocpp-controller /usr/local/bin/ww_get_versions \
        /usr/local/bin/dhcp-loop-eth1.sh /usr/local/bin/test-dhcp-eth1.sh \
        /usr/local/bin/setEth.sh; do
        if [ -f "$path" ] && [ ! -L "$path" ]; then
            sha256sum -- "$path" || result=1
        fi
    done
    exit "$result"
'
# Record link targets separately; find above deliberately does not follow symlinks.
# shellcheck disable=SC2016 # Variables belong to the child shell.
capture protected-links.txt sh -c '
    result=0
    for path in /etc/apt /etc/dpkg /etc/evis /etc/init.d \
        /etc/systemd/system /usr/local/etc /usr/sbin/policy-rc.d /usr/local/bin \
        /etc/network /etc/systemd/network /etc/NetworkManager /etc/dhcp \
        /etc/dhcpcd.conf /etc/hostname /etc/hosts /etc/resolv.conf \
        /etc/nsswitch.conf /etc/avahi; do
        if [ -e "$path" ] || [ -L "$path" ]; then
            find "$path" -type l -printf "%p -> %l\n" || result=1
        fi
    done
    exit "$result"
'

if [ -s "$report/dpkg-audit.txt" ]; then
    incomplete=1
fi
if ! awk -F '\t' '$4 != "install ok installed" && $4 != "hold ok installed" &&
    $4 != "deinstall ok config-files" && $4 != "purge ok config-files" { bad=1 }
    END { exit bad }' "$report/packages.tsv"; then
    incomplete=1
fi

{
    printf 'Inspection only. APT installation is NOT authorized by this report.\n'
    printf 'Review checks.tsv, stderr files and package states.\n'
    printf 'Expected target: Debian 10 (buster), dpkg architecture armhf.\n'
    printf 'Confirm image identity and critical services manually.\n'
    printf 'Not a backup; hashes exclude symlink targets outside the listed paths.\n'
    if [ "$incomplete" -ne 0 ]; then
        printf 'INCOMPLETE: failed/missing checks or unhealthy dpkg state.\n'
    else
        printf 'Collection complete; compatibility and recovery remain unverified.\n'
    fi
} > "$report/summary.txt"
printf 'Report: %s\n' "$report"
if [ "$incomplete" -ne 0 ]; then
    printf 'Incomplete collection; read summary.txt and checks.tsv.\n' >&2
    exit 2
fi
