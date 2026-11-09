# Activate the RPi -> EVI CSM mTLS connection

Run locally in each device's updated repository. The certificate assistant must
already have installed matching EVI server and RPi client identities and saved
`/var/lib/evi-mtls-setup/<role>/profile.json`. No CA setup or manual key copying is
needed here. Keep private keys on their devices; do not put them in the checkout.

This is a commissioning procedure for a stopped charging installation. The
assistants never start CSM, restart the charging backend, authorize an RFID or
send a power/start/stop command. No package installation, vendor library change,
firewall change, OCPP change, or network reconfiguration is performed.

## 1. EVI: install and start only the proxy

First apply the chosen hardware profile with `configure-csm.sh --apply`, while
CSM is already stopped. Keep HTTP at `127.0.0.1:8444` and Modbus disabled. Its
HTTP port must equal the saved certificate assistant `csm_port`. Confirm the
physical topology, protection and limits independently. The observed CSM v2.5.0
rejects `evixios` and `auto_start`; the configurator omits them. An omitted
`auto_start` field is not a guarantee about firmware defaults. Perform initial
startup with the vehicle disconnected and verify authorization behavior separately.
The configurator queries the actual binary version and accepts the observed
2.5.0, 2.9.0 and 2.11.0. On 2.11.0 it follows the supplied 1.4.4 factory TOML,
omitting `[graph]` and selecting the topology through `-g`. It keeps unused
EVIX-IO and the undocumented `auto_start` field omitted; it does not infer
support for optional fields from the distro marker.

```sh
git pull --ff-only
sudo sh setup/csm-https.sh --apply
```

The assistant reuses the saved names/ports and verifies the certificate identity,
chain, key match, exact purpose, and at least one hour of remaining leaf validity.
It checks the installed Caddy 2.11.9 binary and validates the generated Caddyfile
before installation. An exposed HTTP binding, enabled Modbus, running CSM,
unmanaged destination or unsafe file is refused. No key is regenerated.

Answer `y` to install the configuration/unit, then `y` to enable and start/restart
**only** `wattwell-csm-https.service`. Declining either operation preserves that
stage; configuration-only installation does not activate a changed listener.
Without `--apply` it validates/previews without installing a service.

The managed files are `/etc/wattwell-evi/csm-https/Caddyfile` and
`/etc/systemd/system/wattwell-csm-https.service`. Changes have protected backups.
The service reads the existing root-only key, has no Linux capabilities, uses a
private runtime directory, and cannot write the system or access home directories.
Use HTTPS ports above 1023. HTTP/3, Caddy's admin endpoint and automatic HTTPS are
disabled. No public port 80 listener or HTTP redirect is added.

The proxy requires a trusted client certificate with the exact dedicated subject
`CN=evcharger-csm-client` and only forwards the existing GET/PUT API allowlist to
loopback CSM. Logs do not record RFID query values. It does not start CSM; HTTP
502 is expected while CSM remains stopped.

## 2. RPi HMI: build and install the client connection

Run from the checkout actually used by the charging backend. If the registered
`evcharger-backend.service` points to another directory, the assistant refuses
and identifies that the installed checkout must be updated instead.

```sh
git pull --ff-only
npm run build:shared
npm run build:backend
sudo sh setup/csm-https.sh --apply
```

Existing dependencies must already be installed. The assistant does not install
or update packages. It verifies the client identity and asks for the existing
backend operating-system user (normally `pi`), then confirms installation.

A separate runtime copy of the client key, chain and root is installed under
`/etc/evcharger/csm-https/`, root-owned, directory 0750/files 0640, readable by the
backend user's primary group. Existing enrollment material remains root-only;
the certificate assistant can still renew/reissue without changing its key.
Only public connection fields are in `connection.json`: DNS identity, HTTPS port
and optional connection IPv4. No key, password or bearer token is stored in JSON.
The runtime private key is local only and must not be transferred or committed.

On the next backend start, this managed connection selects real CSM mode and
overrides legacy CSM host/port values. It uses HTTPS, the dedicated client
certificate, the enrolled CA and normal server-hostname verification. A saved EVI
IPv4 overrides only connection lookup: SNI, Host and certificate verification
still use `evi3.local` (or the saved EVI DNS name). Redirects and environment HTTP
proxies are disabled for this transport. Invalid settings, missing/unreadable TLS
material or TLS failures never select HTTP as a fallback. Legacy installations
without the managed file retain their existing transport.

RFID mode, whitelist data/HMAC/admin credentials, Grid whitelist transport and
power limits are preserved. Existing real-mode requirements, including whitelist
secrets, still apply. No app service is restarted. Rebuilds alone do not activate
this connection; the running backend loads it on its next start.

## 3. RPi: read-only commissioning check

```sh
sudo sh setup/csm-https.sh --check
```

This verifies the server chain/name, the dedicated client's access to
`GET /api/v1/csm/status`, rejection without a client certificate, and refusal or
timeout of a direct TCP connection to the CSM HTTP port. HTTP 502/503 confirms
proxy access but reports that the upstream CSM is unavailable; it does not count
as a successful CSM status read. The test sends no authorization or charge command.
Other HTTP responses, anonymous access or a reachable direct HTTP port fail.

After verifying the physical installation and making charging safe, the operator
can start the already configured CSM **on the EVI**. Keep the vehicle disconnected
for the first start. If the updated base unit has
`ConditionPathExists=/var/evi-data/user-configuration/services/evis-csm_is_enabled`,
explicitly allow activation first:

```sh
sudo touch /var/evi-data/user-configuration/services/evis-csm_is_enabled
```

This marker also permits future boot starts if the service is enabled. The
configurator leaves that choice to the operator. Then start CSM:

```sh
sudo systemctl start evis-csm.service
sudo systemctl --no-pager --full status evis-csm.service
```

Repeat the RPi `--check`; require HTTP **200** before commissioning RFID control.
Then, with charging stopped, restart the existing RPi backend to load the new
transport. For the supplied system service:

```sh
sudo systemctl restart evcharger-backend.service
```

If the backend is started manually, restart that existing process instead. Do
not start a second backend, frontend, kiosk or whitelist gateway. No vehicle
start/stop test is part of these checks.

## Expiry and scope

Issued identities last 24 hours. Automatic renewal is **not** installed. Before
expiry, rerun `setup/rpi-evi-certificates.sh` option 3 locally on each device, then
rerun this assistant to refresh/reload the proxy and the protected RPi runtime
copy, and restart the backend in a suitable stopped-charging window. The private
keys are reused. Expired identities fail closed. Proxy reconfiguration requires
CSM already stopped; this is commissioning support, not uninterrupted renewal.

This secures the RPi -> CSM HTTPS path and closes the configured direct HTTP and
Modbus listeners. Other vendor command paths (WDM, OCPP or other services),
firewall policy, physical interlocks and access to the RPi application's own API
need their own access controls. The assistant does not claim to secure those
interfaces or establish the electrical safety of the charger.

## A different installed checkout

If the RPi assistant reports a different `WorkingDirectory`, the source clone
and the running application are separate copies. Update the installed Git
checkout and rebuild there; do not relocate the service or copy configuration,
whitelist data or private keys between checkouts. On the RPi:

```sh
csm_app_dir=$(systemctl show evcharger-backend.service -p WorkingDirectory --value)
csm_repo=$(git -C "$csm_app_dir" rev-parse --show-toplevel) || exit
cd "$csm_repo" || exit
git pull --ff-only
npm run build:shared
npm run build:backend
sudo sh setup/csm-https.sh --apply
```

The saved enrollment profile and certificates are device-wide and are reused.
If Git reports local changes or the service directory is not a Git checkout,
stop and inspect that installation instead of overwriting it. Run `--check`
only after client installation succeeds.
