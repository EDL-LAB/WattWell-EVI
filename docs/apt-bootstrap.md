# APT recovery and Avahi installation

This step is part of `sudo sh configure.sh`; no manual installation commands are
required. It targets the observed state: Buster `armhf`, image
`ww-distro-dh-v1.3.27`, APT/libapt `1.8.2.1` registered as installed, APT executables
removed and the libapt file list missing.

The initial check also supports completing only `half-configured`
`libc-bin:armhf` 2.28-10 in the reviewed case, validating the `postinst` and NSS
template, running a read-only `ldconfig` trial, keeping backups and using
`--no-triggers`. See the limitations in the
[README failure section](../README.md#repeated-runs-backups-and-failures).
This attempt does not repair missing `.list` files or prove the integrity of the
entire image.

## Recovering the same version

If the existing APT works and the required methods are present, it is reused.
Otherwise, recovery supports only the registered versions `1.8.2.1`.
The source is fixed to:

```text
https://snapshot.debian.org/archive/debian/20200901T000000Z
```

The configurator verifies `Release.gpg` with the Debian keyring **already present**
on the image, requires Codename Buster, verifies the SHA-256 hash of
`main/binary-armhf/Packages.xz`, selects the exact versions/architecture and checks
the SHA-256 hash of each `.deb`. Download retries are limited; a network error,
signature error or missing version ends the process, without falling back to
upgrades. The snapshot is deliberately historical; index expiry is not used as
proof of freshness, and signatures and hashes remain mandatory.

Snapshot metadata requires matching `GOODSIG` and `VALIDSIG` records from the
existing trusted keyring. Additional expired signers or missing public keys are
tolerated. This supports both expired Stretch signatures on the 2020 Release
and the unknown newer signer on the 2023 security Release. Expired signatures
alone, a missing key alone, bad/revoked signatures or other verification errors
still stop the operation. No keys are imported or global keyrings changed.

Packages are extracted to a private temporary directory, and only missing runtime
files are copied: APT tools (including the `apt-key` helper needed by APT 1.8 for
signature verification), transport/authentication methods, apt-helper and the
libapt library. Runtime checks require executable helpers, not just a working
`apt-get --version`, and run again after recovery. Existing files, configurations,
timers and startup scripts are not replaced. The script does not run `dpkg -i` for these packages or execute
their maintainer scripts. Missing `.list` files are not rebuilt automatically.

## Avahi without changing installed versions

APT receives its own configuration, including sources, preferences, lists,
certificates and cache. Global APT configuration/hook directories are not loaded.
The only sources are `buster main` and `buster/updates main` from the Debian
archives, with `signed-by` pointing to the existing keyring. The
`check-valid-until=no` exception is restricted to those archived sources and does
not disable authentication.

The main configuration points to `/dev/null`; configuration, source, preference
and extra keyring directories point to a dedicated, verified empty directory.
This avoids loading global vendor configuration or treating `-` as a directory
under `/etc/apt`. The existing keyring remains explicitly selected with
`signed-by`; missing verification helpers are restored rather than bypassed.

Every installed package receives a preference for its current version. The
simulated plan is rejected if it changes/configures an existing package, removes
one or purges one. The inventory is compared again before applying the plan, and
all original versions are checked afterwards. The actual installation uses
`--no-remove`, `--no-upgrade` and `--no-install-recommends`; these options supplement
the preferences and plan validation rather than replacing them.

Before planning, `apt-get check` records whether the existing package graph is
consistent. For the reviewed `librsync-dev`, `libwebsockets-dev` and `npm` failures,
the targeted path uses a temporary planning copy, authenticated package downloads,
payload collision checks and two dpkg simulations. Only explicit new packages
are installed against the real database; existing dependency records are not
changed and dependency enforcement is not disabled. See
[the targeted procedure](../README.md#avahi-when-the-existing-package-graph-is-incomplete).
Other baseline failures stop configuration. Binary caches are also private to
the configurator; the temporary planner disables its binary caches.

A temporary `policy-rc.d` policy returning 101 blocks standard service startup
mechanisms during installation, preserving/restoring the original policy and its
attributes. A recovery journal is kept. This does not block arbitrary code in
package scripts or provide rollback for an interrupted installation. Only then
is the Avahi configuration applied and the service started.

`apt-utils`, `avahi-utils` and `libnss-mdns` are not installed by default. There
are no changes to `/etc/nsswitch.conf`, automatic APT task startup, kernel or
firmware updates, calibration changes or general package repair.

## Verification and limitations

The signature/hash chain is tested with fixtures, including invalid signatures
and hashes. TLS and signatures of the current archived Buster metadata were
verified using the included certificate bundle. The upstream APT 1.8.2.1
`apt-key` source was also exercised locally with the generated isolated
configuration and the host's `apt-config`/`gpgv`: authentic archived Debian
metadata was accepted and modified metadata was rejected. No global package
hooks or missing configuration-directory warnings were observed in that check.

The targeted path was also exercised with real authenticated Buster packages
and a synthetic inventory containing the three reviewed dependency failures.
The APT plan and both dpkg simulations passed, stopping before installation.
Tests also cover unknown baseline failures, upgrades, malformed package metadata,
payload collisions, concurrent inventory changes and restoration of the service
policy after errors. These checks do not replace a full test on the Watt&Well EVI.

The official snapshot service returned 503/504 errors during preparation. The
EVI subsequently reached the Avahi stage after runtime recovery, where a missing
`apt-key` exposed the need for the additional helper checks. The corrected full
installation is still unverified on the EVI. If the snapshot is unavailable,
the script fails at that step and retains earlier changes instead of declaring
success.

References: [Debian archive](https://www.debian.org/distrib/archive.en.html),
[Buster apt-secure](https://manpages.debian.org/buster/apt/apt-secure.8.en.html),
[Buster apt-get](https://manpages.debian.org/buster/apt/apt-get.8.en.html),
[archive validity](https://ftp-master.debian.org/#valid-until).
