# WattWell-EVI

Reusable configuration for the Watt&Well DH image on Debian 10 Buster `armhf`.

## Run on the EVI

Transfer **the entire repository folder**, including `certs/` and `scripts/`.
On the next device, run:

```sh
cd ~/WattWell-EVI
sudo sh configure.sh
```

The script prompts for a new hostname; Enter keeps the current persistent name.
Use short names such as `evi3` or `evi-lab`, without `.local`. Renaming is an
intentional change: vendor utilities may depend on the original name and must
be checked. The original alias is preserved in `/etc/hosts`, but this does not
affect checks that read the kernel hostname directly.

Git does not need to be installed on the EVI. If Internet access is not yet
available, transfer the folder over SCP from a computer that already has access
to GitHub and the device.

## CSM configuration

The separate `configure-csm.sh` script asks you to choose 1 BMPU, 2 BMPUs in
parallel or 2 BMPUs in series, then asks for the internal EVSE/WDM address, local
HTTP port and Modbus binding before previewing the proposed configuration:

```sh
sh configure-csm.sh
```

After checking the equipment and with CSM already stopped:

```sh
sudo sh configure-csm.sh --apply
```

It aligns the service command with the selected graph using a backed-up systemd
override. It preserves the original configuration in a private backup and does not start,
restart or enable CSM. It queries the installed binary with `--version`, accepts
the observed versions 2.5.0, 2.9.0 and 2.11.0, and refuses an unreviewed version.
For 2.11.0, it emits the confirmed `auto_start = false` setting. The earlier
versions omit that assignment: 2.5.0 rejected it and 2.9.0 has not been verified.
The selected graph is kept in `graph.file_path` and the matching service `-g`
argument, as in the working profile. Unused EVIX-IO configuration is omitted.
It keeps `auth_enabled = true`, the requested 9 kW / 490 V limits and supervisor
CAN node 2. Commission first with the vehicle disconnected. HTTP is loopback-only
(`127.0.0.1:8444` by default), and Modbus is disabled unless explicitly selected.
Use `--evse-ip`, `--http-port`, `--modbus-ip disabled|<IPv4>` and `--modbus-port`
to supply settings without the corresponding prompts. Use `--topology 1bmpu`, `--topology 2bmpu-parallel` or
`--topology 2bmpu-series` to skip the question. See [profile details and activation scope](docs/csm.md).

## CSM HTTPS preparation

Prepare the verified ARMv7 Caddy binary and an EVI server CSR without starting
services or changing existing access:

```sh
sudo sh prepare-csm-https.sh --hostname evi3.local --apply
```

The private key stays on the EVI. HTTPS is not active after this command;
certificate signing, the RPi client and the protected cutover are separate steps.
See [CSM HTTPS preparation](docs/csm-https.md).

## GitHub CLI

`sudo sh configure.sh` also installs the official ARM GitHub CLI binary at
`/usr/local/bin/gh`, already on the configurator's PATH. It does not depend on
`apt install gh` or repair the unrelated package graph. Git itself is not
installed by this step.

To install only GitHub CLI for your user, the separate command remains available:

```sh
python3 scripts/install-gh.py
export PATH="$HOME/.local/bin:$PATH"
gh --version
```

The installer pins GitHub CLI **2.103.3** and verifies the official ARMv6 archive's
SHA-256 using the project's TLS certificate bundle. Without sudo it installs to
`~/.local/bin/gh`; as root it installs to `/usr/local/bin/gh`. It tests the
executable before publishing it and refuses to
overwrite a different existing binary or symlink. Repeating the command keeps
an identical binary. It does not change APT, packages, services or shell profiles.
Authentication remains a separate user action. If the system CA store cannot
validate GitHub, use the project bundle for that invocation:

```sh
SSL_CERT_FILE="$PWD/certs/cacert.pem" gh auth login --hostname github.com --git-protocol https --web
```

Source: [official GitHub CLI release](https://github.com/cli/cli/releases/tag/v2.103.3).

## What it does

To investigate the reviewed pre-existing dependency failures after configuration:

```sh
sudo sh configure.sh --plan-package-repair
```

This mode only prepares a simulation. It adds the authenticated Debian
2020-09-01 and Debian Security 2023-03-01 snapshot indexes to private copies of
the existing archive lists. The security snapshot provides `libnode-dev`
`10.24.0~dfsg-1~deb10u3`, matching the observed installed Node libraries. It
pins every currently installed version and excludes new package versions whose
exact dependencies conflict with installed libraries. APT then plans the complete
repair, including transitive npm dependencies, without a partial list of targets.
Plans changing or removing existing packages are rejected. Global sources, the
configurator's archive lists and the live dpkg database are not changed.
The full result is saved under `/var/lib/wattwell-evi/package-repair-plan-*/plan.txt`.
It does not install packages, change network/hostname settings or run package
scripts. Review the new packages, file collisions and maintainer scripts before
applying any repair. Normal configuration does not automatically repair this graph.

1. Checks Buster `armhf`, the `ww-distro-dh-*` image family, existing tools and
   package states. Prevents two concurrent configuration runs. It may complete
   only `libc-bin` in the known case described below.
2. Prompts for and validates the hostname; updates `/etc/hostname`, the
   `127.0.1.1` mapping in `/etc/hosts` and the running hostname. Preserves aliases,
   IPv6 entries and a copy of the original files. If applying the name fails,
   it attempts to restore those files and the previous name before exiting.
3. Saves the permanent IPv4 addresses observed on **that device's eth1**, with
   their labels. Uses `ip -4 -o addr show dev eth1`, without depending on JSON
   fields missing from some responses of the image's `ip` tool. Validates the
   interface, prefixes, labels and lifetimes; excludes dynamic leases. Installs
   a dedicated DHCP service for subsequent boots. Preserves an active client;
   requests a lease only if no other client or known DHCP loop is detected.
   Does not migrate interfaces to networkd or restart it.
4. Uses the public Mozilla certificate bundle included in the project, only for
   configurator downloads and the isolated APT configuration. Verifies the
   bundle's hash. Does not replace Watt&Well's global certificates or disable TLS.
5. If APT is missing, restores only missing runtime files from `apt` and
   `libapt-pkg5.0` **1.8.2.1**, the version registered on the observed image.
   Verifies the Debian signature, index hash and package SHA-256 hashes. Does
   not execute those two packages' installation scripts or overwrite vendor files.
   Includes `apt-key`, required for signature verification by this APT version,
   and checks that the required runtime helpers are executable.
6. Simulates installing missing `avahi-daemon` and `libnss-mdns` packages with
   dedicated APT sources and caches. The NSS module is required for ordinary
   programs such as `ping` and `ssh` to resolve `.local` through Avahi.
   Pins every installed package to its current version and rejects any plan
   that changes or removes them. Installs only missing Avahi/mDNS packages and new dependencies,
   without recommendations. If the existing dependency failures are limited to
   the reviewed `librsync-dev`, `libwebsockets-dev` and `npm` cases, uses the
   targeted installation procedure described below instead of repairing them.
7. Configures Avahi to announce `<hostname>.local` only on `eth1`, with mDNS
   reflection disabled and `qca0` excluded. Starts Avahi and, if needed, D-Bus;
   does not restart D-Bus or EVI/CAN/PLC services. Updating an existing Avahi
   configuration may require restarting only Avahi itself.
8. Installs the verified GitHub CLI binary at `/usr/local/bin/gh`, without APT
   dependency repair or changes to system services. Authentication is manual.

The `wattwell-eth1-dhcp.service` delegates to the existing DHCP hooks and restores
the saved permanent IPv4 addresses after each call. This reduces the risk of
losing static addresses, but **does not guarantee an uninterrupted SSH session**
during requests or renewals. Use a local console or a recoverable alternative
connection for the first run. The previous EVI trial received `192.168.15.106`
and retained `192.168.137.100` and `192.168.200.11`; these addresses are not
hardcoded in the configurator.

## Repeated runs, backups and failures

Backups and state are stored in `/var/lib/wattwell-evi`, accessible only to root.
The first copy of each changed file is kept under `backups/`. Repeating the script
does not restart the DHCP service. An existing lease is preserved; the new
client's private hostname configuration takes effect when that client starts.
Do not copy this state to another EVI: the saved addresses belong to the original
device.

APT configuration is stored in `/var/lib/wattwell-evi-apt`. This isolated
invocation does not load the vendor's global APT sources or hooks. During package
installation, a temporary policy refuses standard service start/restart actions;
the previous policy is restored on success, failure and interruption. The journal
allows that policy to be recovered on the next run after SIGKILL or power loss.
Changes made by another administrator in the meantime are not automatically
overwritten. The policy is not a sandbox for direct calls that package scripts
may execute.

The process stops if cryptographic trust, tools, DHCP, archive access or compatible
versions are missing, or if a plan requires upgrades. Earlier steps may already
have been applied: there is no single transaction for the entire configuration
and no automatic package rollback. It does not use general dpkg repair,
`upgrade`, `autoremove`, `--allow-unauthenticated` or `curl -k`.

The observed image also has incomplete `libapt-pkg5.0` metadata. The script does
not invent a `.list` file or claim that the entire image has been repaired.
If it finds an incomplete package, it stops before changing the hostname/network
or installing packages, except for the specific `libc-bin` case below.
It automatically saves diagnostics to
`/var/lib/wattwell-evi/package-state-report.txt`: incomplete package states,
`dpkg --audit`, maintainer `postinst` scripts read without execution, excerpts
from available logs and the presence of `ldconfig`. Diagnostic collection does
not execute maintainer scripts. To investigate the cause, retrieve the report:

```sh
sudo cat /var/lib/wattwell-evi/package-state-report.txt
```

The file is private; excerpts may contain details of earlier installations.
`half-configured` alone does not identify the cause. Do not use
`dpkg --configure -a` or `apt-get -f install` without reviewing the error, scripts
and impact on this image.

There is a single automatic recovery case: `libc-bin:armhf` **2.28-10** in state
`install ok half-configured`, with a matching `Config-Version` and all other
packages in accepted states. It requires the hash of the `postinst` observed and
reviewed on this image, `nsswitch.conf` matching the existing template, no unknown
dpkg invocation hooks and a successful `ldconfig -N -X` trial. Only the exact
reviewed content of `/etc/dpkg/dpkg.cfg.d/pkg-config-hook-config` is accepted:
it acts only on `add-architecture`/`remove-architecture` and performs no action
during `configure`. The file is preserved; modified or additional hooks still
require review. Only then does the script run
`dpkg --no-triggers --configure libc-bin`, with a temporary service policy.
It does not download or reinstall libc, upgrade it, configure other packages
or edit the database state directly.

This attempt runs the normal `ldconfig` from the `postinst`: it may update the
image's library cache and symlinks. Copies of the dpkg state, `nsswitch.conf` and
existing caches are kept in `backups/`, along with `libc-bin-configure.log` and
`libc-bin-dpkg.log` under `/var/lib/wattwell-evi`. These backups do not provide
an automatic rollback of all symlinks. If the attempt fails or the final inventory
differs, the script stops. On an already configured image, this attempt is skipped.

Before using this on an operational device, follow the [recovery guide](docs/recovery.md)
and test on an image copy. Restoring an image does not replace the need to preserve
calibration, identity or keys that may be stored outside the filesystem.

## Avahi when the existing package graph is incomplete

The observed image reports missing dependencies for `librsync-dev`,
`libwebsockets-dev` and `npm`. Normal APT installation stops on that pre-existing
state. The configurator does not remove those packages, install their missing
development/Node dependencies, or run `--fix-broken`.

Only for those reviewed package names, it creates a temporary **planning copy**
of the status database, omitting their `Depends`/`Pre-Depends` fields in that copy.
Only normal dpkg installation writes to the live database; the workaround never
removes its dependency fields.
APT then plans only missing Avahi/mDNS packages and their new dependencies. Any proposed change to an
existing package or any removal is refused.

Each new package is downloaded over HTTPS using SHA-256 information from the
authenticated APT indexes. Package names, versions and architectures are checked.
New essential packages or packages with pre-dependencies are refused. The script
also refuses payloads that would overwrite existing files, including files not
tracked by dpkg. A separate dpkg simulation validates configuration of the new
packages against the **unaltered original records plus the new package records**;
another simulation checks installation against the live database. No maintainer
scripts execute during these simulations.

Only then are the explicit new `.deb` files installed with normal dpkg dependency
checks and the temporary service policy. The real installation does not use the
planning database, `--force-depends` or `--ignore-depends`. Normal package triggers
may run under the service policy; it does not sandbox arbitrary maintainer code.
The inventory is rechecked before and after installation. A concurrent database
change, an unexpected package/version or an incomplete package state stops setup.

The original graph remains incomplete: `apt-get check` may still report the
three pre-existing cases after successful Avahi setup. Diagnostics are kept in
`/var/lib/wattwell-evi/apt-baseline-check.txt`, `avahi-targeted-plan.txt`,
`avahi-status-before.txt`, `avahi-dpkg.log` and `avahi-dpkg-output.txt`. The status
snapshot is evidence, not a standalone recovery image or an automatic rollback.
Unknown baseline dependency failures continue to block installation. Full
installation on the actual Watt&Well image remains unverified.

## Requirements, verification and limitations

Requires Python 3.7+, `dpkg`, `dpkg-deb`, `gpgv`, the existing Debian keyring, `ip`,
`systemctl`, `networkctl` and the DHCP client/hook already present on the observed
image. APT recovery depends on the official 2020-09-01 snapshot: if unavailable,
it stops without choosing a different version. Avahi depends on the Buster archives.
This is not a system updater: Debian Buster LTS support ended in 2024.

Confirm `.local` resolution from another computer on the same LAN with mDNS
support. The script does not change that computer's NSS or network configuration.

```sh
shellcheck -s sh configure.sh scripts/*.sh
python3 -m unittest discover -s tests -v
```

Tests use temporary files and mocked commands. TLS with the included bundle and
signatures of the current archived Buster metadata have been checked against the
real services. The full installation and startup of the new service have not yet
been tested on the Watt&Well image. The earlier diagnostic and DHCP trial scripts
remain in `scripts/` for investigation; the normal command is just `configure.sh`.

Details: [network](docs/network.md), [APT](docs/apt-bootstrap.md),
[certificates](certs/README.md).

## RPi to EVI CSM HTTPS commissioning

After installing both mTLS identities, use `sudo sh setup/csm-https.sh --apply`
locally on the EVI and RPi. See the [ordered build, activation and read-only checks](docs/csm-https-activation.md).
The assistant reuses the saved profile; only the proxy can be started automatically
after confirmation. CSM/backend activation and certificate renewal remain separate.
