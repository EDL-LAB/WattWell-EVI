# Preparing EVI CSM HTTPS and mTLS

For the local interactive certificate assistant, see [interactive-evi-https.md](interactive-evi-https.md).

Status: preparation only. This does not secure a running installation yet.
The target observed is Debian 10 armhf, ARMv7, kernel 5.10.74. Caddy is installed
as a separate, pinned static binary; APT and the vendor OpenSSL libraries are
not changed. Runtime compatibility is checked by executing `caddy version`
before publishing the binary. No service is installed, started or enabled.

## Prepare on the EVI

```sh
git pull --ff-only
sh prepare-csm-https.sh --hostname evi3.local
sudo sh prepare-csm-https.sh --hostname evi3.local --apply
sudo cat /etc/wattwell-evi/csm-https/server.csr
```

Replace `evi3.local` with the DNS name of each EVI. This name must resolve from
the RPi and appear in the issued server certificate SAN. The preview writes
nothing. Apply installs official Caddy 2.11.9 for Linux ARMv7, authenticated by
the project's HTTPS CA bundle and pinned archive SHA-256:

```text
8c191dd2688c30dbe19793a0a3f532ede64547c5a70a47eebf7d8ab43a4d7514
```

The digest was obtained from the official GitHub release asset metadata.
An existing different Caddy is refused. The installed binary is separate from
system package management; future updates need explicit review of the pin.

The script creates an EC P-256 private key and public certificate signing request
(CSR). The key stays in `server.key` (0600) inside a root-only directory (0700).
Only the public `server.csr` should be transferred to the Grid CA operator.
Rerunning checks and reuses the key/CSR. Partial identities, symlinks, mismatched
keys and another hostname are refused instead of replacing private material.
The script does not change CSM, OCPP, systemd, firewall or network configuration.

## Certificates still required

Use the existing Grid CA, with separate, narrowly scoped issuance policies:

| Identity | Certificate subject | SAN | Extended key usage |
| --- | --- | --- | --- |
| EVI server | `CN=evi3.local` | DNS `evi3.local` | serverAuth |
| RPi CSM client | exactly `CN=evcharger-csm-client` | DNS `evcharger-csm-client.local` | clientAuth |

The subject for the client has no additional O/OU attributes, as the example
matches the complete subject exactly. The CA must restrict who can obtain this
identity. Trusting a CA alone does not authorize every certificate it issues.
The existing Grid templates approve `evcharger.local` and the Grid client;
they do not automatically approve either new identity. Do not broaden those
existing policies or reuse/copy their private keys. Generate the client key on
the RPi and send only its CSR to the CA. Verify CA root fingerprints separately
before trusting them. Keep the root CA private key and CA issuance secrets off
both the RPi and EVI.

The preparation script does not issue certificates or configure renewal.
Before production, configure renewal, certificate reload and expiry monitoring
for both identities. Certificate revocation enforcement must match the selected
CA/proxy policy; disabling issuance alone does not instantly invalidate an
already-issued certificate. Never fall back to unverified TLS or plain HTTP.

## Staged proxy example

[The Caddyfile example](../config/csm-https.Caddyfile.example) expects:

- `server-chain.pem`: EVI server certificate and intermediate chain.
- `server.key`: private key generated on this EVI.
- `client-ca.pem`: approved CA certificate chain for the RPi client.

All are under `/etc/wattwell-evi/csm-https/`. Keep the directory private. The
example is not copied to an active path by the preparation script.

It listens on HTTPS TCP 8443, requires a trusted, valid client certificate and
only forwards the exact dedicated RPi subject. It exposes only GET/PUT on the
CSM status and EVSE 1/2 data, authorization, limitations and allocation endpoints
used by the current RPi application. Other requests return 403. Automatic HTTPS,
HTTP redirects, the Caddy admin API and access logging are disabled. Global logs
are discarded as upstream errors can include query-string card identifiers;
production monitoring must use sanitized service/health/expiry checks.

Validate the adapted configuration with the pinned Caddy binary and real public
certificate material before commissioning. Do not expose a general CSM admin
interface or proxy arbitrary destinations.

## Remaining cutover, in an equipment-safe maintenance window

1. Verify both signed identities, purposes, hostname, key matches and expiry.
2. Install a restricted systemd proxy service and validate the configuration.
3. Adapt the RPi CSM client for HTTPS, CA verification and its client key/certificate.
   Its current `http://` URL must not remain as an automatic fallback. Preserve
   the whitelist logic and existing whitelist administration credentials.
4. Bind CSM HTTP to `127.0.0.1:8444` and make that setting persistent in the CSM
   configurator too; the current configurator still selects `0.0.0.0`. Restrict
   alternative command interfaces used in the installation, including Modbus,
   and review the running OCPP controller separately. No such changes are made
   by this preparation step. Avoid cutting a required integration or SSH access.
5. Test an authorized read, absent client certificate, untrusted client CA,
   wrong client identity from the trusted CA, expired certificate, wrong server
   hostname and direct HTTP reachability. Verify denied requests never reach CSM.
6. Commission start/stop separately under the physical charging safety procedure.

No charging or production authorization tests are part of preparation.

## Isolated verification

```sh
python3 -m unittest discover -s tests -p test_prepare_csm_https.py
CADDY_TEST_BINARY=/path/to/caddy-2.11.9 python3 -m unittest discover -s tests -p test_csm_https_proxy.py
```

Preparation tests use temporary files and real OpenSSL CSR generation, including
key reuse, hostname mismatch, key mismatch, incomplete files, symlink rejection,
archive member checks, and preservation of a different installed binary.
The optional integration test starts only a loopback Caddy and a dummy HTTP
upstream with temporary CA/server/client certificates. It verifies an authorized
read and dummy authorization PUT, rejection of a different identity signed by
the trusted CA even with spoofed identity headers, absent/foreign client
certificates, wrong server hostname/CA, and forbidden paths/methods. Denied
requests must never reach the dummy upstream. No EVI or charging is contacted.
The downloaded ARMv7 binary was inspected as a statically linked ARM EABI5 ELF;
the real proxy test ran with the same release's amd64 binary. ARM execution and
production certificate expiry/renewal/revocation remain device commissioning.
