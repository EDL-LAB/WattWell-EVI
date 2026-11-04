# Certificados de bootstrap

`cacert.pem` contém raízes públicas Mozilla convertidas pelo projeto curl e foi
obtido em 2026-11-04, por HTTPS autenticado, de:

- https://curl.se/ca/cacert.pem
- https://curl.se/ca/cacert.pem.sha256

SHA-256 verificado:
`796f1389bfea13c0ed78f663b765baaff54a508e5483786bd6ece73c5e3e3cbe`.

A conversão e a licença estão descritas em https://curl.se/ca/ . Inclui-se a
licença MPL 2.0 em `LICENSE-MPL-2.0`. São certificados públicos, sem chaves privadas.

O configurador verifica o hash fixado no código antes de usar este conjunto.
É usado só pelas descargas de bootstrap e pelo APT isolado; não é importado no
store global nem substitui certificados, chaves ou identidade Watt&Well.
Atualizações deste conjunto exigem nova obtenção autenticada, revisão e atualização
do hash do configurador. Não resolver falhas substituindo TLS por `--insecure`.
