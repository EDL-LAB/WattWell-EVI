# Automatic CSM certificate renewal

This is the optional renewal step after mTLS commissioning. Run each assistant
locally from that device's installed checkout. It preserves private keys, CA
trust, CSM configuration, RFID/whitelist settings and charging service state.
No APT changes or system upgrades are performed.

## 1. Recover expired identities

On both the EVI and RPi HMI, update their own repository and run:

```sh
git pull --ff-only
sudo sh setup/rpi-evi-certificates.sh
```

Keep the saved settings and select **3: request/install through Grid SSH**.
Approve the exact CSR on the Grid as before. This reuses the existing device
key and installs a new 24-hour certificate; it does not reload running services.
An expired certificate cannot bootstrap the renewal timer.

## 2. Authorize the existing EVI public key on the Grid

Update the Grid-Controller checkout before running the EVI certificate wizard.
When its server request is approved through Grid SSH, the Grid now offers to
also authorize automatic server issuance for that same public key and exact
DNS identity. Approve that and the restart of **only step-ca**. The already
transferred public request is reused; no separate SCP or second assistant is
needed. Matching existing authorization is reused without changing the CA.
Private device keys remain on their own devices.

For previously issued certificates or manual JSON exchange, the separate
`sudo sh setup/csm-renewal.sh --apply` Grid assistant remains available. It
accepts the public server request JSON or matching `server.csr` already on the
Grid; if neither is present, copy only that public file from the EVI through
the trusted Grid SSH connection first. Compare its CSR fingerprint before
approval. Existing certificates do not need reissuing just to add this rule.

The new `evi-csm-renew-evi3-local` JWK provisioner contains only the EVI public
P-256 key. The Grid template fixes CN/SAN to `evi3.local`, `serverAuth` only,
CA=false and a maximum 24-hour lifetime. Existing CA provisioners, policy deny
rules and the RFID client's identity remain separate. A conflicting key under
the same provisioner name is refused rather than overwritten.

This is durable device authorization: possession of that EVI key permits
issuance of that exact server identity until the provisioner is removed and
the CA configuration reloaded. It does not depend on the old leaf remaining
valid at the CA. The local helper nevertheless refuses expired leaves and
requires the interactive recovery above. Revoke this scoped provisioner if
the EVI key is compromised; do not remove unrelated CA provisioners.

## 3. Enable renewal on the EVI

From `~/WattWell-EVI`, with the existing HTTPS proxy already active:

```sh
sudo sh setup/csm-renewal.sh --apply
```

The assistant verifies a fresh CA issuance before installing the timer. It
also loads the recovered certificate into the existing Caddy 2.11.9 process
using SIGUSR1 and verifies the certificate actually served over TLS. CSM is
not stopped or restarted. There is no HTTP authorization/start/stop command.

## 4. Enable renewal on the RPi HMI

From the checkout actually used by `evcharger-backend.service`:

```sh
npm run build:shared
npm run build:backend
sudo sh setup/csm-renewal.sh --refresh
```

Enter the existing backend user, normally `pi`. With charging stopped, load
the new certificate hot-reload code **once**:

```sh
sudo systemctl restart evcharger-backend.service
sudo sh setup/csm-renewal.sh --apply
sudo sh setup/csm-https.sh --check
```

The refresh copies only the renewed public client chain to the protected
runtime directory, retaining the existing key, CA and group access. Subsequent
backend requests detect a changed chain and use a new HTTPS agent; in-flight
requests and RFID sessions remain intact. No backend restart is needed for
later renewals. The CA's existing client certificate renewal path is used;
renewal after expiry remains disabled.

## Operation and recovery

Both devices install the same local hourly timer. It renews with eight hours
remaining, retries at the next hourly run after failure and never bypasses
certificate verification. A failure before validation leaves the previous
chain intact. A reload failure is retried even when the newly issued chain is
not yet due. The validity window requires a correct clock and a reachable CA.
After an outage that lasts past expiry, recover interactively with step 1 and
run `sudo sh setup/csm-renewal.sh --refresh` on each affected device.

```sh
systemctl --no-pager list-timers wattwell-csm-renewal.timer
sudo journalctl --no-pager -u wattwell-csm-renewal.service -n 30
```

Only protected installed copies under `/usr/local/lib/wattwell-csm-renewal`
are run by root. The timer does not execute a user-writable Git checkout. The
private Step CLI 0.30.6 binary is downloaded from the official release and its
architecture-specific SHA-256 is checked before execution; no global `step`
installation is required. Re-run `--apply` after updating these scripts to
update the installed copies.

To disable automatic runs without touching the charger:

```sh
sudo systemctl disable --now wattwell-csm-renewal.timer
```

Disabling the timer does not revoke certificates or remove the Grid's scoped
server authorization. Existing 24-hour certificates still expire normally.

The optional isolated real-CA integration test in Grid-Controller requires
`EVI_TEST_STEP` and `EVI_TEST_STEP_CA` and never uses production CA material.
Unit checks and offline token generation do not prove the live Grid's
configuration; the device installer performs a verified CA preflight.

References: [Smallstep token flags](https://smallstep.com/docs/step-cli/reference/ca/token/),
[Caddy signal reload behavior](https://caddyserver.com/docs/command-line#signals).
