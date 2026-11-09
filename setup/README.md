# Local connection setup

Run these commands from the root of the device's own repository.

| Connection | Command | Repositories |
| --- | --- | --- |
| RPi HMI -> EVI: configure proxy/client after certificates | `sudo sh setup/csm-https.sh --apply` | ev-charging, WattWell-EVI |
| RPi HMI -> EVI: prepare/install mTLS certificates | `sudo sh setup/rpi-evi-certificates.sh` | Grid-Controller, ev-charging, WattWell-EVI |

Each assistant identifies the local repository role. The certificate assistant
offers automatic public request/sign/install through an already trusted Grid SSH
connection, with operator approval; manual JSON exchange is also available.
Private keys stay on their devices. No remote service configuration is applied.

The certificate assistant does not activate the EVI proxy or change the RPi CSM
client. Whitelist setup runs only in Grid-Controller and ev-charging; the EVI does not
host that link. See the [certificate workflow](../docs/interactive-evi-https.md).

The renamed commands replace the former root-level launchers. Installed keys,
certificates, saved profiles, JSON exchange formats and service configuration
paths are unchanged. Python implementation files live beside the launchers here;
the existing CA/installation helpers remain under `scripts/`.

After certificate installation, follow [proxy/client commissioning](../docs/csm-https-activation.md).
The CSM HTTPS assistant can activate only the EVI proxy after confirmation;
CSM and the RPi charging backend are never started or restarted automatically.
