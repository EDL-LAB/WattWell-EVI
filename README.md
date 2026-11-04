# WattWell-EVI

Procedimentos pequenos e verificáveis para configurar EVI/EVCC Watt&Well sem
atualizar a imagem do fabricante nem alterar os serviços críticos.

## Estado

Inclui **diagnóstico** e **ensaio de DHCPv4 na eth1**. Não existe instalador APT
executável. Nenhum script instala, atualiza ou remove pacotes. O diagnóstico só
recolhe informação e escreve o relatório; o ensaio DHCP, quando invocado, altera
a rede através dos hooks existentes e pode interromper o acesso remoto.

O alvo confirmado é Debian 10 Buster `armhf`, imagem `ww-distro-dh-v1.3.27`.
O inventário recebido regista APT/libapt `1.8.2.1` e `isc-dhcp-client` `4.4.1-2`,
com estado `install ok installed` nos três pacotes. Os binários `/usr/bin/apt` e
`/usr/bin/apt-get` não existem. `dpkg --verify` aponta diferenças em ficheiros APT
e avisa que falta a lista de ficheiros de libapt; a integridade não está validada.
O `dhclient` existe em `/sbin` e `/usr/sbin`, fora do PATH do utilizador `evis`.

`systemd-networkd` está ativo, mas `networkctl` confirma `eth1` **unmanaged**, sem
ficheiro `.network`. Não é necessário instalar DHCP. Antes de alterar a gestão
persistente, ensaiar o cliente existente sem iniciar instâncias concorrentes.

Embora tenha sido indicado acesso à Internet, o último teste falhou na resolução
de `archive.debian.org`, sem rota por defeito nas saídas IPv4/IPv6 recebidas.
Confirmar conectividade antes de descarregar pacotes. A preparação poderá ocorrer
diretamente no equipamento com downloads e metadados autenticados; o bundle no
computador de apoio continua a ser uma alternativa.

O âmbito inclui também **cliente DHCP na `eth1`**, mudança de hostname e Avahi.
O futuro instalador pedirá o hostname durante a execução; não haverá um nome
fixo no código. O [plano de rede](docs/network.md) descreve
as dependências conhecidas e a ordem de aplicação; ainda não aplica alterações.

## Recolher o inventário no equipamento

Transfere `scripts/inspect-device.sh` pelo método de acesso já existente. Não
instales ferramentas para executar o diagnóstico. Usa `sh` e uma pasta absoluta
nova, num volume com espaço e permissões adequadas:

```sh
sh scripts/inspect-device.sh /tmp/evi-inspection-before-apt
```

A pasta de destino tem de ser nova; uma segunda recolha usa outro nome. O script
não requer root, mas permissões insuficientes tornam a recolha incompleta.
Se for necessária uma recolha privilegiada, usa o acesso administrativo habitual
do equipamento e uma pasta num diretório privado e de confiança.

Códigos de saída: `0` recolha concluída, `1` erro de utilização/escrita ou falha
inesperada, `2` recolha incompleta ou estado `dpkg` a investigar. **Zero não
significa que é seguro instalar.** O script recolhe informação também num sistema
fora do alvo, sem o declarar compatível.

Lê `summary.txt`, `checks.tsv`, os ficheiros `.stderr`, `os-release.txt`,
`architecture.txt`, `wattwell-version.txt`, `dpkg-audit.txt` e `packages.tsv`.
O relatório inclui versões/estados de pacotes, seleções, espaço, serviços e timers,
hashes de configuração e de alguns binários EVI. Não copia conteúdos de configuração
ou credenciais para o relatório, não carrega ficheiros de configuração como código e não chama
`ww_get_versions`, `wdm`, `plctool`, utilitários QCA ou binários EVI.

Inclui também hostname atual/persistente, `/etc/hosts`, endereços e rotas IPv4/IPv6,
nomes de processos (sem argumentos/ambiente), ferramentas disponíveis e hashes
da configuração de rede/DHCP/Avahi. Endereços, nomes e rotas são dados privados
do relatório; não é efetuado qualquer pedido DHCP ou teste ativo de conectividade.

Uma recolha não é uma fotografia atómica: não a uses como base de instalação se
ocorrerem alterações de pacotes/configuração durante ou depois dela. Os hashes
não cobrem todos os ficheiros de uma imagem; os links são inventariados sem seguir
os seus destinos. Um ficheiro removido ou alterado pode produzir uma recolha
incompleta ou inconsistente. Revê a cobertura antes da fase de instalação.

Mantém relatórios privados: nomes de serviços, caminhos e versões também podem
ser sensíveis. A pasta é criada com modo `0700`, ficheiros com `umask 077`. Não
guardes relatórios, imagens, pacotes ou credenciais no Git. Exporta o relatório
antes de reiniciar se estiver em `/tmp`.

## Antes de qualquer instalação

### Primeiro ensaio de DHCP

Depois de transferir/extrair este projeto no EVI, usar consola local ou acesso
alternativo que permita recuperar uma quebra da ligação:

```sh
sudo sh scripts/request-dhcp.sh
```

O script acrescenta `/usr/sbin` e `/sbin` ao PATH, confirma `eth1` unmanaged e
recusa avançar se encontrar dhclient/dhcpcd/udhcpc ou o loop DHCP conhecido do
fabricante. Não termina processos nem reinicia serviços. Invoca o cliente existente
com `-4 -v -1 eth1`: procura uma lease; após sucesso o cliente pode continuar a
renová-la. Não é uma configuração de arranque nem uma garantia de manter os IPs
estáticos: os hooks existentes podem alterar endereços, hostname, rotas e DNS.
Se a origem da configuração atual não estiver clara, rever esses hooks antes de
executar. A verificação de processos não elimina corridas com outros gestores.

Após o pedido, recolher:

```sh
ip -4 addr show dev eth1
ip route
getent hosts archive.debian.org
curl -I --connect-timeout 5 --max-time 15 https://archive.debian.org/debian/
```

Conferir os dois endereços iniciais no resultado. Se não houver lease, não repetir
em loop, não inventar um gateway e não reiniciar a rede como reparação automática.
Um `DHCPDISCOVER` sem resposta pode significar que falta servidor DHCP/partilha de
Internet no segmento da `eth1`; não prova avaria do cliente.

### Recuperação da imagem

Segue [o procedimento de recuperação](docs/recovery.md). A imagem de recuperação
e a calibração devem estar preservadas e o restauro deve ter sido testado numa
unidade de reserva ou suporte sobresselente.

Consulta o [plano incremental de APT](docs/apt-bootstrap.md). Aplicar a configuração
de rede, hostname e Avahi será uma etapa explícita separada da instalação do
gestor de pacotes. Upgrades, tarefas automáticas APT e atualizações de
firmware/calibração continuam fora do âmbito.

## Verificação em desenvolvimento

No computador de apoio, sem ligar ao equipamento:

```sh
sh -n scripts/inspect-device.sh
shellcheck -s sh scripts/inspect-device.sh
shellcheck -s sh scripts/request-dhcp.sh
python3 -m unittest discover -s tests -v
```

Python e ShellCheck são ferramentas de desenvolvimento; não são dependências do
diagnóstico no EVI. Os testes usam comandos simulados e não executam `dpkg`,
`systemctl` ou `dhclient` reais.
