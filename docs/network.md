# Rede, hostname e Avahi

O comando de configuração é `sudo sh configure.sh`.

## Estado observado

A imagem `ww-distro-dh-v1.3.27` usa Buster `armhf`. O `networkd` está ativo, mas a
`eth1` está unmanaged, sem ficheiro `.network`. `dhclient` já está instalado em
`/sbin`, fora do PATH do utilizador. Não é necessário instalar outro cliente.

O ensaio confirmou lease `192.168.15.106`, servidor/rota `192.168.15.1`, manutenção
dos IPs permanentes `192.168.137.100/24` e `192.168.200.11/24`, e DNS funcional.
O hostname em execução e em `/etc/hostname` era `evi3`, mas `/etc/hosts` só tinha
os aliases `dhsom` e `elbe-daemon`. A verificação TLS falhou com o curl existente.

## Aplicação num próximo EVI

O nome é pedido interativamente e validado antes de alterar ficheiros. Enter
mantém o nome persistente. `/etc/hosts` recebe o nome escolhido em `127.0.1.1`,
preservando aliases, comentários e as restantes linhas. Não fixa `evi3` no código.
A escolha não modifica `EVIS_NAME`, identidade, certificados ou código Watt&Well.

O configurador guarda os IPv4 globais permanentes que **observa na eth1 desse EVI**.
Instala `wattwell-eth1-dhcp.service`, um cliente DHCPv4 próprio para os próximos
arranques, sem alterar o gestor de rede existente. O arranque aguarda pelo device
`eth1`, recusa um cliente concorrente ou o loop DHCP conhecido e não toca em CAN
ou PLC. Um cliente já ativo é preservado; não é terminado por padrão de processo.

O hook novo delega em `/sbin/dhclient-script`, preserva o seu estado de saída e
repõe os IPv4 permanentes guardados que tenham desaparecido. Não suprime os hooks
existentes nem os seus erros. A configuração DHCP privada mantém o hostname
escolhido. O cliente original que já esteja em execução pode continuar a usar a
configuração anterior até ao próximo arranque; não é reiniciado pelo setup.

Há risco de interrupção temporária de SSH durante pedidos/renovações. A reposição
posterior dos IPs não é garantia de continuidade da sessão. Um gestor do fabricante
que inicie posteriormente outro cliente também exige coordenação: a verificação
de processos não é um bloqueio contra todos os gestores externos. O configurador
não migra a interface para networkd, não limpa endereços nem reinicia globalmente
a rede. Os hooks originais podem produzir outros efeitos que têm de ser validados
na imagem de ensaio. Não copiar o estado de `/var/lib/wattwell-evi` entre unidades.

O conjunto Mozilla incluído autentica apenas as descargas do configurador e do
APT isolado. Nenhum certificado global da Watt&Well é substituído ou desativado.

Avahi recebe o nome escolhido, `allow-interfaces=eth1`, `deny-interfaces=qca0` e
`enable-reflector=no`. As outras configurações são preservadas. Não se altera NSS
nem se instala reflexão mDNS. Apenas Avahi pode ser reiniciado para aplicar uma
alteração; D-Bus é iniciado se necessário e não é reiniciado.

Um utilitário `ww_get_versions` de outra cópia local só aceitava `dhsom` e
`raspberrypi`. Isso não prova a mesma limitação nesta imagem, mas manter um alias
em `/etc/hosts` não resolve testes diretos do nome do kernel. Validar os utilitários
do fabricante após a mudança de nome; o configurador não os reescreve.

Referências: [dhclient Buster](https://manpages.debian.org/buster/isc-dhcp-client/dhclient.8.en.html),
[hosts](https://manpages.debian.org/buster/manpages/hosts.5.en.html),
[Avahi Buster](https://manpages.debian.org/buster/avahi-daemon/avahi-daemon.conf.5.en.html),
[TLS curl](https://curl.se/docs/sslcerts.html).
