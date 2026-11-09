# Local interactive EVI certificate assistants

Run the same command in each device's own repository:

```sh
sudo sh setup/rpi-evi-certificates.sh
```

The repository determines its role: Grid Controller is the CA operator,
WattWell-EVI is the EVI server, and ev-charging is the RPi client. This is a local
assistant. Device enrollment can use its original sudo user's existing SSH access
to the Grid; the manual JSON workflow remains available.
The manual EVI/RPi workflow uses Python 3.7+ and OpenSSL; automatic mode also
needs SSH/SCP and runuser. Only the Grid loads the
existing cryptography/Smallstep certificate helper.

The assistant asks for EVI/RPi/Grid DNS names, optional IPv4 addresses, CA/HTTPS/
CSM ports and one/two EVSE connectors. Settings are validated and displayed
before saving. Enter keeps defaults or saved values. The IPv4 values are
connection records; a saved Grid IPv4 selects the SSH destination. Interfaces,
DHCP, DNS and routes are not changed at this certificate preparation stage. Certificate identities use the DNS names, not
an IP substituted into TLS verification. The RPi client has the dedicated
logical identity `evcharger-csm-client`, independent of its network hostname.

Public request/response JSON files carry the connection profile, so select the
existing file when asked to load the same settings on another device. Keep
profiles identical across the three devices. Saved local settings are under
`/var/lib/evi-mtls-setup/<role>/profile.json` with protected backups on changes.
No passwords or private keys belong in profiles or exchange JSON files.

## Automatic request and installation (device option 3)

Update the Grid, EVI and RPi repositories first. Prepare the Grid CA/profile once
as described below; an already matching CA needs no change or restart. Then run
the assistant locally on the EVI and on the RPi and choose **3: request/install
through Grid SSH** (the device default). Each device:

1. Verifies/reuses its existing private key and exports only its public request.
2. Asks for the Grid SSH operator, SSH port and Grid-Controller checkout path
   (defaults: `pi`, 22, `/home/pi/Documents/Grid-Controller`). It uses the saved
   Grid CA DNS name as the verified SSH identity. If a Grid IPv4 is saved,
   `HostName=<IPv4>` selects the address without relying on `.local` resolution,
   while `HostKeyAlias=<Grid DNS name>` preserves the existing host-key trust.
   Without a saved Grid IPv4, SSH uses DNS as before. No interface or DNS changes
   are made, and the CA HTTPS URL/certificate name remain unchanged.
3. Uploads the public JSON over SSH, asks the Grid operator for sudo and explicit
   approval of the exact CSR/identity, and retrieves the signed public response.
4. Validates the role, full profile, CSR, chain, hostname, purpose, expiry and
   local key before offering installation. No manual `scp`, PEM copying or root
   fingerprint typing is needed in this mode.

SSH runs as the device user who invoked sudo, using that user's existing SSH
credentials and `known_hosts`. Unknown/changed host keys are refused with
`StrictHostKeyChecking=yes`; neither host-key checking nor TLS is disabled.
Establish and independently verify trust in the Grid SSH host key beforehand,
using the same Grid hostname and port. No SSH account, key or sudo rule is added.
The existing operator must be able to log in and run sudo on the Grid; remote
root SSH login and passwordless sudo are not required. Passwords are entered
interactively and never recorded by the script.

A private per-run SSH control socket reuses the authenticated connection and is
closed on completion/failure. Agent/port forwarding is disabled. Only the public
request and response cross the link: CA signing keys, authorization tokens and
device private keys stay on their respective machines. The first root is trusted
because it comes directly from the authenticated Grid signing command. An
already installed different root is still refused. Offline imports still require
independent root-fingerprint confirmation.

The Grid approval command requires an already saved matching Grid profile and
the existing scoped provisioners. It never updates CA policy or restarts the CA.
Profile mismatches (including recorded IPs) are refused rather than silently
changing the Grid's approved settings. New public requests use a protected
per-run directory below the Grid operator's `~/evi-certificates`; successful
installation removes only those two temporary public files. If approval,
connectivity or validation fails, no installed certificate or private key is
replaced. Diagnostic public exchange files may remain for operator review.

Automatic mode needs `ssh`, `scp` and `runuser` on the device. Missing tools or
SSH access cause a clear failure; the assistant does not install packages or
silently switch to an unverified transport.

## Manual JSON workflow (device options 1 and 2)

1. On the Grid, choose **prepare CA identities** if needed. The existing backend
   prepares scoped provisioners with backups and preserves old provisioners and
   deny rules. A changed CA configuration offers a separate, explicit restart
   of **only** step-ca, then verifies its HTTPS health. Declining leaves services
   untouched. An already matching CA is not restarted. Existing manual CA setup
   can be reused.
2. On the EVI and RPi, choose **prepare/export public request**. The private key
   and CSR are generated locally or verified/reused. EVI uses the existing
   `/etc/wattwell-evi/csm-https/server.key` and `server.csr`; it preserves the key
   already generated by `prepare-csm-https.sh`. RPi uses
   `/etc/evcharger/csm-tls/client.key` and `client.csr`. TLS directories are 0700
   and private keys 0600. Incomplete, linked, mismatched or differently named
   identities are refused rather than overwritten.
3. Transfer only the exported `*-request.json` files to the Grid via an already
   trusted SSH transfer, trusted removable media or another authenticated
   channel. No manual PEM copying is required. Compare the displayed CSR
   SHA-256 fingerprint with the device's own display; a CSR signature proves
   key possession, not which physical machine submitted it.
4. On the Grid, load the request's settings and choose **sign a public device
   request**. Confirm the identity and independently checked CSR fingerprint.
   The existing helper issues a validated 24-hour chain using a five-minute
   CSR-bound token consumed locally. The assistant exports a public
   `*-certificate.json` response and displays the public root CA fingerprint.
5. Transfer each response to its corresponding target. On that target, load
   the same saved/request profile and choose **install Grid response**. The
   response must match the role, profile, CSR and local private key. On first
   trust, enter the root CA fingerprint independently verified on the Grid;
   a root bundled in the response is not trusted just because it arrived with
   a certificate. An existing trusted root cannot be silently replaced.
6. The target verifies the exact subject, sole DNS SAN, sole role-specific EKU,
   leaf status, chain, hostname, expiry and key match before installing public
   material. Extra root certificates are refused. Changed public chains get
   backups; private keys are never transferred or replaced. The root is saved
   as `root-ca.pem`; EVI also gets the same root as `client-ca.pem` for its
   planned mTLS verification. Targets do not start or restart any service.

The default public exchange directory is `~/evi-certificates` of the original
sudo user. The JSON files contain public CSRs/certificates and network settings;
never transfer `*.key`, the Grid signing vault, tokens or passwords.
A new matching server chain may be issued even if an earlier manual chain
exists; this does not rotate its local private key.

## Deliberate commissioning boundary

This assistant replaces the manual certificate commands and PEM handoffs. It
**does not activate HTTPS**, modify the RPi application's transport, close CSM
HTTP/Modbus access, install a proxy service, or configure automatic renewal.
The Caddy installation/preparation and protected cutover remain described in
`docs/csm-https.md` in WattWell-EVI and `docs/evi-csm-certificates.md` in the Grid.
Certificates expire after 24 hours: renewal/reissuance and safe listener reload
must be completed before continuous operation. No fallback to HTTP or disabled
verification is added by these assistants. No charging command is sent.
