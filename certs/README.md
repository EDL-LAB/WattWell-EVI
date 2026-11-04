# Bootstrap certificates

`cacert.pem` contains public Mozilla roots converted by the curl project and was
retrieved on 2026-11-04 over authenticated HTTPS from:

- https://curl.se/ca/cacert.pem
- https://curl.se/ca/cacert.pem.sha256

Verified SHA-256:
`796f1389bfea13c0ed78f663b765baaff54a508e5483786bd6ece73c5e3e3cbe`.

The conversion and license are described at https://curl.se/ca/ . The MPL 2.0
license is included in `LICENSE-MPL-2.0`. These are public certificates without
private keys.

The configurator checks the hash pinned in the code before using this bundle.
It is used only for bootstrap downloads and isolated APT; it is not imported
into the global trust store and does not replace Watt&Well certificates, keys
or identity. Updating the bundle requires a new authenticated download, review
and an update to the configurator's pinned hash. Do not work around failures by
replacing TLS verification with `--insecure`.
