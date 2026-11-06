# Single-BMPU CSM configuration

This is a separate, explicit hardware profile based on the configuration supplied
for `evi1`. It is not run by the general `configure.sh` command. The profile is
not a universal safe operating limit for every BMPU, vehicle or power circuit.

Preview the complete proposed profile without writing anything:

```sh
sh configure-csm.sh
```

The default EVSE/WDM address is `192.168.200.11`. To use another address:

```sh
sh configure-csm.sh --evse-ip 192.168.200.12
```

The same address is used for the authorization endpoint on port `15716`.
The template is checksummed and its TOML syntax is tested; the only runtime
substitution is a validated IPv4 address. Editing the template requires reviewing
the code checksum and tests as well.

## Applying the profile

Check the physical single-BMPU wiring, voltage/current ratings, protection and
the existing graph before applying. This profile uses `contactorless`; the
script does not establish or verify an external safety/interlock circuit.

With charging stopped, high voltage made safe and `evis-csm.service` already
inactive, apply the profile:

```sh
sudo sh configure-csm.sh --apply
```

Add `--evse-ip` to apply an alternative address. Application refuses an active,
transitioning or unknown CSM service. It does not stop any service itself.

The script writes `/usr/local/etc/evis-csm/evis-csm-wattwell.toml` and atomically
selects it through `/usr/local/etc/evis-csm/evis-csm.toml`. It accepts an existing
regular file or a symlink to a regular profile in the same directory. Other
symlink destinations are refused. Original vendor profiles are not edited.
This selects a complete profile; additional settings in the previous profile
are not merged into it.

Before a change, the current contents, ownership/mode and original symlink
target are backed up under `/var/lib/wattwell-evi/csm/backup-*`, accessible only
to root. A pre-existing managed profile is saved as `managed-before.toml` too.
A repeated run with identical contents makes no changes. The existing
`evis-csm-1bmpu.dot` must be present and is never modified or generated. The
script does not validate that graph's hardware topology or alter BMPU node IDs.

## Settings

| Setting | Value |
| --- | --- |
| Grid current limit | 32 A |
| Supervisor CANopen node | 2 on `can0`, 500000 bit/s |
| Graph | Existing `evis-csm-1bmpu.dot` |
| Contactor manager | `contactorless` |
| EVSE | One, ID 1, WDM port 8333 |
| Automatic start | Disabled (`auto_start = false`) |
| Charging authorization | Enabled (`auth_enabled = true`) |
| Authorization endpoint | `<EVSE-IP>:15716` |
| DC charge limits | 9000 W, 490 V, 30 A |
| DC discharge limits | 9000 W, 490 V, 32 A |
| AC charge/discharge current | 32 A |
| HTTP | `0.0.0.0:8444` |
| Modbus | `0.0.0.0:5552` |
| OCPP / EVIX-IO / LEDs | Disabled |

The supplied comments suggesting automatic start and no authorization contradicted
the actual values. The values above are preserved; those comments are corrected
in English. The script does not start or configure an authorization/RFID service,
modify API token settings beyond the supplied profile, or test vehicle operation.
The requested all-interface HTTP and Modbus bindings are retained; network access
to those interfaces must be controlled for the installation.

## Two individually controlled BMPUs in parallel

[`config/csm-2bmpu-parallel.dot`](../config/csm-2bmpu-parallel.dot) extends the
supplied single-BMPU graph with `members="[96,97]"` and `serial=false` in one
power-unit group connected to EVSE A. This example assumes CSM controls both
CANopen nodes individually; it does not validate CSM firmware support or change
the units' addressing, calibration or current sharing.

With the equipment safe and CSM inactive, this graph can be installed as
`/usr/local/etc/evis-csm/evis-csm-2bmpu-parallel.dot` and selected in the CSM TOML:

```toml
[graph]
file_path = "/usr/local/etc/evis-csm/evis-csm-2bmpu-parallel.dot"
```

Keep existing operating limits until the physical parallel arrangement has been
validated. A second BMPU does not justify automatically doubling voltage or
raising the configured power/current limits. Merely selecting a DOT file does
not configure or establish a working electrical parallel system.

Watt&Well also documents a distinct master/slave cluster configuration in
[BMPU series AN002](https://wattandwell.com/app/uploads/BMPU-series-AN002-Parallel-and-series-operations.pdf).
If that mode is used, confirm how the particular CSM version represents the
cluster before listing both master and slave as independently controlled units.

`configure-csm.sh --apply` still selects its original **single-BMPU** profile and
graph. Rerunning it after manual parallel selection restores that single-BMPU
reference. The example graph is not automatically installed or selected.

## Activation and version scope

The **EVI Distro User Manual v1.4.4**, sections 6.1 and 6.2, documents CSM as
disabled by default, with the enable flag:

```text
/var/evi-data/user-configuration/services/evis-csm_is_enabled
```

The script leaves that flag and existing systemd enablement unchanged. Activation
or restart is a separate operator action after checking the equipment. The manual
lists CSM 2.11.0 for distro v1.4.4; it does not document the complete TOML schema.
The earlier `ww-distro-dh-v1.3.27` device is a different image. No firmware or CSM
version is upgraded or assumed equivalent by this script. Successful file
installation does not confirm acceptance by the CSM version on a particular EVI.
