# Preservation and recovery

## Installation prerequisites

1. Identify the hardware model/revision and exact Watt&Well image version.
2. Arrange a maintenance window without a charging session and with the
   manufacturer-defined safe state. Diagnostics do not stop services to do this.
3. Confirm recovery access independent of the system being modified: a console,
   boot medium or manufacturer-documented process.
4. Obtain a consistent copy of the storage medium, including the partition table,
   boot files, rootfs and data/configuration partitions. Prefer offline acquisition
   using the manufacturer's procedure; copying a live filesystem may be inconsistent.
5. Keep the SHA-256 hash, size, date, device identification and image version with
   the copy. Keep the original medium unchanged whenever possible.
6. Test restoration on a compatible spare device or storage medium. Confirm boot,
   access and EVI/EVCC service operation before using the process on an
   operational device.

There is no generic clone/restore command here: the block device and correct
method depend on the hardware. A wrong destination can destroy data. A disk copy
may also exclude firmware, identity, keys or calibration stored in EEPROM, secure
elements or external devices; establish with the manufacturer how to preserve
and recover them. Do not copy them to another device without confirming identity
and calibration restrictions.

## Inventory to preserve outside Git

- Diagnostic report, the complete `/var/lib/dpkg` database, selections/holds and
  existing APT state. These are additional evidence, not a replacement for the
  recovery image.
- Local applications and dependencies, service units and startup policies.
- EVI/EVCC configuration, network settings, identity, certificates and keys, with
  permissions, ownership, symlinks and relevant attributes preserved.
- QCA/PLC configuration and calibration, including data outside the filesystem.

In the partial local copy consulted during preparation, `etc/distro_version`
contained `evi-distro-v1.4.4`. Configurations for `evis-*`, `ocpp-controller` and
`qca/calibration` were present. The `ww_get_versions` code refers to
`/usr/local/etc` on the device. This guides the inventory but does not prove the
target device's version or coverage. Private files from that copy were not
included in this project.

## If a step fails

Stop the sequence and preserve reports, installer output and dpkg state. Do not
run `apt-get -f install`, `dpkg --configure -a`, purge, reboot or service changes
as an automatic repair attempt. The impact depends on the installation stage
that failed; a package operation is not transactional.

Use the validated recovery procedure. Restoring only `/var/lib/dpkg` could leave
the package inventory inconsistent with the actual files. After restoring,
confirm configurations, calibration, services and device behavior. Recovery must
respect the manufacturer-defined safe state.
