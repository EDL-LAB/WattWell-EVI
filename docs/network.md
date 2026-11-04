# Network, hostname and Avahi

The configuration command is `sudo sh configure.sh`.

## Observed state

Image `ww-distro-dh-v1.3.27` uses Buster `armhf`. `networkd` is active, but `eth1`
is unmanaged, without a `.network` file. `dhclient` is already installed under
`/sbin`, outside the user's PATH. Another client does not need to be installed.

The trial confirmed lease `192.168.15.106`, server/gateway `192.168.15.1`, retention
of permanent addresses `192.168.137.100/24` and `192.168.200.11/24`, and working DNS.
The running hostname and `/etc/hostname` were `evi3`, but `/etc/hosts` contained
only aliases `dhsom` and `elbe-daemon`. TLS verification failed with the existing
curl installation.

## Applying this to the next EVI

The name is requested interactively and validated before files are changed.
Enter keeps the persistent name. `/etc/hosts` receives the chosen name at
`127.0.1.1`, preserving aliases, comments and all other lines. `evi3` is not
hardcoded. This choice does not modify `EVIS_NAME`, identity, certificates or
Watt&Well code.

The configurator saves permanent global IPv4 addresses that it **observes on that
EVI's eth1**. It installs `wattwell-eth1-dhcp.service`, a dedicated DHCPv4 client
for subsequent boots, without changing the existing network manager. Startup
waits for the `eth1` device, refuses a competing client or the known DHCP loop,
and leaves CAN and PLC untouched. An active client is preserved; it is not killed
by matching a process pattern.

The new hook delegates to `/sbin/dhclient-script`, preserves its exit status and
restores saved permanent IPv4 addresses that have disappeared. It does not
suppress existing hooks or their errors. The private DHCP configuration keeps
the chosen hostname. An original client that is already running may continue
using its previous configuration until the next boot; setup does not restart it.

SSH may be temporarily interrupted during requests/renewals. Restoring the IPs
afterwards does not guarantee session continuity. A vendor manager that later
starts another client also requires coordination: process checks are not a lock
against all external managers. The configurator does not migrate the interface
to networkd, flush addresses or restart the entire network. Original hooks may
have other effects that must be checked on the test image. Do not copy state
from `/var/lib/wattwell-evi` between devices.

The included Mozilla bundle authenticates only configurator and isolated APT
downloads. No global Watt&Well certificate is replaced or disabled.

Avahi receives the chosen name, `allow-interfaces=eth1`, `deny-interfaces=qca0`
and `enable-reflector=no`. Other settings are preserved. NSS is not changed and
mDNS reflection is not installed. Only Avahi may be restarted to apply a change;
D-Bus is started if needed and is not restarted.

A `ww_get_versions` utility from another local copy accepted only `dhsom` and
`raspberrypi`. This does not prove the same limitation exists in this image, but
keeping an alias in `/etc/hosts` does not fix direct kernel hostname checks.
Validate vendor utilities after renaming; the configurator does not rewrite them.

References: [Buster dhclient](https://manpages.debian.org/buster/isc-dhcp-client/dhclient.8.en.html),
[hosts](https://manpages.debian.org/buster/manpages/hosts.5.en.html),
[Buster Avahi](https://manpages.debian.org/buster/avahi-daemon/avahi-daemon.conf.5.en.html),
[curl TLS](https://curl.se/docs/sslcerts.html).
