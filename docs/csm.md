# CSM configuration

This is a separate hardware configurator based on the profile supplied for `evi1`.
It is not run by the general `configure.sh` command. The profile is
not a universal safe operating limit for every BMPU, vehicle or power circuit.

Choose a topology interactively and preview the complete proposed profile without
writing anything:

```sh
sh configure-csm.sh
```

After the topology, the interactive configurator asks for the internal EVSE/WDM
IPv4 address, the local HTTP port, and the Modbus binding. Press Enter for
`192.168.200.11`, HTTP `127.0.0.1:8444`, and disabled Modbus. These defaults apply
in non-interactive use too. To supply the EVSE address explicitly:

```sh
sh configure-csm.sh --evse-ip 192.168.200.12
```

The menu asks for an explicit choice:

| Choice | Topology | Example graph | BMPU nodes | `serial` |
| --- | --- | --- | --- | --- |
| 1 | One BMPU | [csm-1bmpu.dot](../config/csm-1bmpu.dot) | 96 | false |
| 2 | Two in parallel | [csm-2bmpu-parallel.dot](../config/csm-2bmpu-parallel.dot) | 96, 97 | false |
| 3 | Two in series | [csm-2bmpu-series.dot](../config/csm-2bmpu-series.dot) | 96, 97 | true |

For non-interactive use, select explicitly, for example:

```sh
sh configure-csm.sh --topology 2bmpu-parallel
sudo sh configure-csm.sh --apply --topology 2bmpu-series
```

The same IPv4 address is used for the authorization endpoint on port `15716`.
The template and bundled graphs are checksummed; the TOML syntax is tested.
Only validated addresses, ports, the Modbus switch and selected graph path are
substituted.
All three choices keep the same operating limits below. Selecting series or
parallel does not configure the BMPU firmware, wiring, addressing or protection.

## HTTP and Modbus bindings

CSM HTTP always binds to `127.0.0.1`; it is an upstream for the separate HTTPS/mTLS
proxy, not a LAN API. Keep port `8444` for the supplied Caddy example. If you choose
another HTTP port, use the same upstream port in the proxy and the certificate
assistant connection settings. This configurator does not install or activate
the proxy, change certificates, edit those other settings, or configure the RPi.

The interactive Modbus answer accepts:

- `disabled` (default): disable the CSM Modbus listener.
- `127.0.0.1`: enable it for local integrations only, then ask for its port.
- A specific EVI LAN IPv4: enable a required external integration, then ask for
  its port. All-interface `0.0.0.0` and multicast/broadcast bindings are refused.

LAN Modbus is a separate command path without this HTTPS/mTLS protection. Do not
enable it merely to make RFID authorization work. If an existing Grid integration
requires it, choose the EVI address that integration uses and restrict access
separately before commissioning. This script does not modify firewall rules.

Command-line values skip their corresponding prompts. For non-interactive use:

```sh
# Default: local HTTP, disabled Modbus.
sudo sh configure-csm.sh --apply --topology 1bmpu

# Required local Modbus integration; no LAN Modbus listener.
sudo sh configure-csm.sh --apply --topology 1bmpu \
  --evse-ip 192.168.200.11 --http-port 8444 \
  --modbus-ip 127.0.0.1 --modbus-port 5552
```

`--modbus-ip disabled` explicitly skips the Modbus question. Ports must be integers
from 1 to 65535; HTTP and enabled Modbus must use different ports. Bind addresses
belong to the EVI, not the RPi or Grid. These selections are written into the managed
TOML and take effect at the next separately authorized CSM start. Repeating the same
answers produces the same profile; previous custom answers are not loaded as defaults.

## Applying the profile

Check the physical wiring, voltage/current ratings, protection and selected
graph before applying. This profile uses `contactorless`; the
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

The script also installs a systemd override at
`/etc/systemd/system/evis-csm.service.d/90-wattwell.conf`. It replaces `ExecStart`
with the known CSM executable, canonical TOML path and the selected graph as `-g`.
This prevents an existing `-g /usr/local/etc/evis-csm/starterkit.dot` argument
from conflicting with the graph selected in TOML. An unrecognized existing
command is refused for review. The base service file and other service settings
are preserved. A previous managed override is backed up as
`service-override-before.conf`. `systemctl daemon-reload` loads the unit definition;
the script does not start, restart or enable CSM. A failed reload is reported and
can be retried by rerunning the configurator.

Before a change, the current contents, ownership/mode and original symlink
target are backed up under `/var/lib/wattwell-evi/csm/backup-*`, accessible only
to root. A pre-existing managed profile is saved as `managed-before.toml` too.
The selected bundled graph is installed as
`/usr/local/etc/evis-csm/evis-csm-wattwell-<topology>.dot`; an existing managed
graph is backed up as `graph-before.dot`. Vendor DOT files are never overwritten.
A repeated run with identical profile and graph makes no changes. The script
does not validate the physical topology or alter BMPU node IDs.

## Settings

| Setting | Value |
| --- | --- |
| Grid current limit | 32 A |
| Supervisor CANopen node | 2 on `can0`, 500000 bit/s |
| Graph | Selected managed copy of the bundled DOT example |
| Contactor manager | `contactorless` |
| EVSE | One, ID 1, WDM port 8333 |
| Automatic start | Disabled (`auto_start = false`) |
| Charging authorization | Enabled (`auth_enabled = true`) |
| Authorization endpoint | `<EVSE-IP>:15716` |
| DC charge limits | 9000 W, 490 V, 30 A |
| DC discharge limits | 9000 W, 490 V, 32 A |
| AC charge/discharge current | 32 A |
| HTTP | `127.0.0.1:8444`; configurable port, loopback binding retained |
| Modbus | Disabled by default; explicit IPv4 and port when enabled |
| OCPP / EVIX-IO / LEDs | Disabled |

The supplied comments suggesting automatic start and no authorization contradicted
the actual values. The values above are preserved; those comments are corrected
in English. The script does not start or configure an authorization/RFID service,
modify API token settings beyond the supplied profile, or test vehicle operation.
The earlier all-interface HTTP and Modbus defaults have been replaced by
loopback HTTP and disabled Modbus. Existing external integrations must be checked
before selecting this complete profile; required Modbus access must be chosen explicitly.

## Two individually controlled BMPUs in parallel

[`config/csm-2bmpu-parallel.dot`](../config/csm-2bmpu-parallel.dot) extends the
supplied single-BMPU graph with `members="[96,97]"` and `serial=false` in one
power-unit group connected to EVSE A. This example assumes CSM controls both
CANopen nodes individually; it does not validate CSM firmware support or change
the units' addressing, calibration or current sharing.

Select menu option 2, or pass `--topology 2bmpu-parallel`. The script installs
its managed graph and sets the TOML `graph.file_path` to that file.

Keep existing operating limits until the physical parallel arrangement has been
validated. A second BMPU does not justify automatically doubling voltage or
raising the configured power/current limits. Merely selecting a DOT file does
not configure or establish a working electrical parallel system.

Watt&Well also documents a distinct master/slave cluster configuration in
[BMPU series AN002](https://wattandwell.com/app/uploads/BMPU-series-AN002-Parallel-and-series-operations.pdf).
If that mode is used, confirm how the particular CSM version represents the
cluster before listing both master and slave as independently controlled units.

The series example follows the supplied `serial=true` graph for nodes 96 and 97.
Both two-BMPU examples require confirmation against the installed CSM version
and actual electrical arrangement before activation. The script never raises
the single-BMPU test limits automatically.

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
