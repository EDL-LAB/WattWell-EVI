# Rede, hostname e Avahi

## Estado conhecido

O operador confirmou Internet no EVI e forneceu a saída de `ip addr`:

- hostname no prompt: `dhsom`;
- `eth1`: `192.168.137.100/24` (label `eth1:0`) e `192.168.200.11/24`;
- interfaces `qca0`, `can0` e `can1` ativas.

O operador confirmou **cliente DHCP na `eth1`** e pediu que o script pergunte o
novo hostname durante a execução. Não implementar servidor DHCP nem fixar um
hostname no código.

O inventário posterior confirmou a imagem `ww-distro-dh-v1.3.27`, Buster `armhf`.
`systemd-networkd` está ativo; `networking` está em falha e NetworkManager/dhcpcd
inativos. A recolha posterior mostrou `Network File: n/a` e estado
`routable (unmanaged)` para `eth1`: o networkd não a está a gerir. Os ficheiros
observados em `/lib/systemd/network` correspondem a interfaces de contentores,
não à `eth1`. Não há um ficheiro aplicado ao qual acrescentar um complemento.
Não tentar reparar ou iniciar o serviço `networking` em paralelo por estar em falha.

A tabela IPv4 principal recebida contém apenas as duas rotas diretamente ligadas,
sem rota por defeito. `ip -6 route show default` também não devolveu rotas, e
`curl` falhou a resolver `archive.debian.org`. A ligação ao arquivo não está
operacional. Isto não exclui políticas de rotas/proxy não recolhidas. Não presumir um
gateway com base no prefixo `192.168.137.0/24`.

`dpkg-query -W` regista `isc-dhcp-client` `4.4.1-2`, mas `command -v` não encontrou
`dhclient`. A recolha posterior confirmou `install ok installed` e o executável
em `/sbin/dhclient` e `/usr/sbin/dhclient`: falta `/sbin` no PATH de `evis`.
Não instalar ou reinstalar o cliente DHCP.

A saída de endereços por si só não mostra a rota de saída, DNS ou cliente DHCP.
Os IPv4 apresentados têm validade `forever`; isso não prova por si só o mecanismo
que os configurou. Não inferir que DHCP falta ou que um dos IPs pode ser removido.

Na cópia local parcial do fabricante existem `dhcp-loop-eth1.sh` e
`test-dhcp-eth1.sh`. Ambos acrescentam os dois endereços acima, terminam processos
por padrão `dhclient.*eth1` e invocam `dhclient`. O loop volta a fazê-lo a cada
20 segundos após terminar a tentativa anterior. Isto não prova que os scripts
estejam instalados ou ativos no alvo. Não os executar, duplicar ou desativar sem
confirmar o gestor de rede em uso e a origem dos processos.

## Dados a confirmar

1. Existe servidor DHCP no segmento da `eth1` e como fornece saída/DNS?
2. Que scripts/unidades/hooks do fabricante configuram os dois IPs estáticos?
3. Existe cliente DHCP ou loop do fabricante já em execução?
4. Por qual endereço/interface está a sessão de acesso atual?

O diagnóstico recolhe endereços, rotas, serviços, nomes de processos e ferramentas
disponíveis. Não copia o conteúdo de perfis de rede, que pode incluir credenciais.
Pode ser necessário rever de forma privada apenas os ficheiros pertinentes após
identificar o gestor. A recolha não prova que uma alteração de rede seja segura.

## Sequência de implementação

### APT e pacotes

Com Internet, descarregar diretamente no EVI depois de validar ferramentas,
assinaturas e dependências conforme o plano APT. Não é necessário preparar sempre
um bundle fora do equipamento. Apenas acrescentar pacotes em falta; nenhuma
alteração de versões existentes nem reparação geral automática.

### DHCP

Para o primeiro ensaio, usar `scripts/request-dhcp.sh`, que chama o `dhclient`
existente sem configuração de arranque, sem reiniciar serviços e recusando clientes
ou o loop conhecido já ativos. Usa os hooks instalados e não garante preservar os
IPs durante a obtenção/renovação da lease. Exige caminho de recuperação da ligação.

Para a configuração persistente, não migrar a `eth1` para networkd sem identificar
a origem dos IPs e coordenar a passagem para um único gestor. Se for escolhida essa
migração, `DHCP=ipv4` na secção `[Network]` ativa DHCPv4 sem pacote adicional.
Como a interface está unmanaged, será preciso um ficheiro `.network` próprio para
`eth1`, mantendo explicitamente os dois IPs e a configuração IPv6 relevante.
Não o ativar enquanto o ensaio dhclient estiver em execução.

Quando existir um ficheiro aplicado, um complemento `.network.d/*.conf` pode preservar
o ficheiro do fabricante, mas tem de corresponder ao ficheiro realmente aplicado;
o primeiro `.network` compatível ganha, e não o ficheiro criado mais recentemente.
Confirmar que o complemento não afeta outras interfaces que usem o mesmo ficheiro.

Rever as definições existentes e preservar os endereços estáticos persistentes,
DNS, IPv6/RA e rotas necessárias. Na secção `[DHCP]`, considerar `UseHostname=no`
para preservar a escolha explícita de hostname, e rever os efeitos de `UseDNS`,
`UseRoutes`, NTP e métrica sobre a configuração original. Não depender de uma opção
de uma versão systemd posterior à instalada. Validar sem reinício global da rede;
o método de aplicação depende da versão e do caminho de recuperação disponível.

Se afinal `eth1` for gerida por outro mecanismo, reutilizar esse mecanismo. Não
iniciar `dhclient` concorrente só porque o pacote figura no inventário.

Limitar qualquer pedido a `eth1`, sem atuar sobre `qca0` ou CAN. Um pedido DHCP
pode alterar endereços, rota por defeito e DNS através dos hooks: não basta manter
os IPs manualmente antes da chamada para garantir o acesso. Preservar os dois IPs
até confirmar a função de cada um, a configuração persistente e um caminho de
recuperação. Não fazer `ip addr flush`, `dhclient -r`, `pkill` por padrão ou
reiniciar globalmente a rede durante uma sessão remota.

### Hostname

O futuro instalador deve perguntar `Novo hostname (Enter para manter o atual):`
antes de alterar o sistema. Mostrar o nome atual; ler texto literal, sem `eval`
ou execução do conteúdo introduzido. Aceitar um nome curto de 1 a 63 caracteres
ASCII minúsculos, algarismos e hífen, sem hífen inicial/final ou sufixo `.local`.
Entrada inválida volta a pedir; Enter conserva o hostname. Fim da entrada ou
interrupção termina antes de aplicar alterações. O plano deve mostrar o hostname
escolhido e o correspondente nome `.local`. A compatibilidade e a disponibilidade
do nome na rede continuam a necessitar de verificação.

O utilitário `ww_get_versions` na cópia local usa `platform.node()` e só aceita
`raspberrypi` ou `dhsom`; outro nome termina com `Unsupported platform`.
É uma dependência concreta do utilitário, não prova de que os serviços EVI tenham
a mesma limitação. Rever os chamadores e a versão instalada antes de mudar o nome.
Não reescrever o código do fabricante ou a identidade `EVIS_NAME` automaticamente.

Após definir um nome compatível e confirmar as dependências, preservar
`/etc/hostname` e `/etc/hosts`; aplicar pelo mecanismo existente (`hostnamectl`
se disponível), atualizar só a associação local pertinente em `/etc/hosts` e
validar nome persistente, nome em execução e resolução local. Não é necessário
reiniciar os serviços EVI só para alterar o hostname.

Se o objetivo for apenas um nome amigável `.local`, pode configurar-se `host-name`
no Avahi mantendo `dhsom` no sistema, desde que o operador escolha essa alternativa.
Não assumir que isso satisfaz um pedido de mudar o hostname do sistema.

### Avahi

Usar `avahi-daemon` para anunciar `<hostname>.local` na LAN. `avahi-utils` é
opcional para diagnóstico; `libnss-mdns` só é necessário se o próprio EVI precisar
de resolver nomes mDNS através de NSS, sujeito à configuração já existente.
Não alterar `/etc/nsswitch.conf` por defeito só para anunciar o EVI.

Preservar a configuração existente. A configuração pretendida limita a descoberta
à interface de gestão `eth1` (`allow-interfaces=eth1`) e mantém a reflexão mDNS
desativada (`enable-reflector=no`), sem anunciar sobre PLC `qca0`. São opções das
secções `[server]` e `[reflector]`, respetivamente; não duplicar secções ou
substituir todo o ficheiro cegamente. Rever também serviços Avahi já publicados.

Controlar scripts de instalação para que Avahi/D-Bus não arranquem antes da
configuração. Se D-Bus já estiver em uso, não o reiniciar. Ativar Avahi será uma
ação intencional apenas após a revisão, sem reiniciar serviços críticos do EVI.

Validar resolução de `<hostname>.local` a partir de outro equipamento na mesma
LAN. mDNS depende de suporte no cliente e de multicast UDP 5353; Internet por si
só não garante resolução `.local`. Vários IPs na `eth1` podem originar anúncios de
vários endereços; confirmar que o cliente consegue atingir o endereço escolhido.

## Referências

- [dhclient em Buster](https://manpages.debian.org/buster/isc-dhcp-client/dhclient.8.en.html)
- [hostnamectl em Buster](https://manpages.debian.org/buster/systemd/hostnamectl.1.en.html)
- [Configuração Avahi em Buster](https://manpages.debian.org/buster/avahi-daemon/avahi-daemon.conf.5.en.html)
- [DHCP e seleção de ficheiros networkd em Buster](https://manpages.debian.org/buster/systemd/systemd.network.5.en.html)
