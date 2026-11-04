# WattWell-EVI

Configuração reutilizável da imagem Watt&Well DH em Debian 10 Buster `armhf`.

## Executar no EVI

Transfere **a pasta inteira** do repositório, incluindo `certs/` e `scripts/`.
No próximo equipamento, basta:

```sh
cd ~/WattWell-EVI
sudo sh configure.sh
```

O script pede o novo hostname; Enter mantém o nome persistente atual. Usa nomes
curtos como `evi3` ou `evi-lab`, sem `.local`. A alteração do nome é intencional;
utilitários do fabricante podem depender do nome original e devem ser validados.
O alias original é preservado em `/etc/hosts`, mas isso não altera verificações
que consultem diretamente o hostname do kernel.

Não é preciso instalar Git no EVI. Se ainda não houver Internet, transfere a pasta
por SCP a partir do computador que já tem acesso ao GitHub e ao equipamento.

## O que faz

1. Confirma Buster `armhf`, a família de imagem `ww-distro-dh-*`, ferramentas
   existentes e estados de pacotes. Impede duas configurações simultâneas. Pode
   concluir apenas o `libc-bin` no caso conhecido descrito abaixo.
2. Pede e valida o hostname; atualiza `/etc/hostname`, a associação `127.0.1.1` em
   `/etc/hosts` e o nome em execução. Mantém aliases, entradas IPv6 e uma cópia dos
   ficheiros originais. Se a aplicação do nome falhar, tenta repor esses ficheiros
   e o nome anterior antes de terminar.
3. Guarda os IPv4 permanentes da `eth1` observados **nesse equipamento**, com os
   respetivos labels. Instala um serviço próprio de DHCP para os próximos arranques.
   Preserva um cliente já ativo; só pede uma lease se não detetar outro cliente ou
   o loop DHCP conhecido. Não migra interfaces para networkd nem o reinicia.
4. Usa o conjunto público de certificados Mozilla incluído no projeto, apenas nas
   descargas do configurador e na configuração APT isolada. Verifica o hash do
   conjunto. Não substitui os certificados globais da Watt&Well nem desativa TLS.
5. Se faltar APT, recupera apenas ficheiros de execução ausentes de `apt` e
   `libapt-pkg5.0` **1.8.2.1**, a versão registada na imagem observada. Verifica
   assinatura Debian, hash do índice e SHA-256 dos pacotes. Não executa os scripts
   de instalação desses dois pacotes nem repõe ficheiros do fabricante por cima.
6. Simula a instalação de `avahi-daemon` com fontes e caches APT próprias. Fixa as
   versões de todos os pacotes instalados e recusa qualquer plano que os altere
   ou remova. Instala só Avahi e dependências novas, sem recomendações.
7. Configura Avahi para anunciar `<hostname>.local` apenas na `eth1`, com reflexão
   mDNS desativada e `qca0` excluída. Arranca Avahi e, se necessário, D-Bus; não
   reinicia D-Bus nem serviços EVI/CAN/PLC. Uma configuração Avahi existente que
   precise de alteração pode exigir reiniciar apenas o próprio Avahi.

O serviço `wattwell-eth1-dhcp.service` delega nos hooks DHCP existentes e repõe os
IPv4 permanentes guardados depois de cada chamada. Isto reduz perdas dos IPs
estáticos, mas **não garante uma sessão SSH ininterrupta** durante pedidos ou
renovações. Usa consola local ou acesso alternativo recuperável na primeira execução.
O teste anterior no EVI recebeu `192.168.15.106` e manteve `192.168.137.100` e
`192.168.200.11`; estes endereços não estão fixos no configurador.

## Repetição, backups e falhas

Backups e estado ficam em `/var/lib/wattwell-evi`, privados para root. A primeira
cópia de cada ficheiro alterado fica em `backups/`. O serviço DHCP não é reiniciado
só por repetir o script. Uma lease existente é preservada; a configuração privada
de hostname do novo cliente será usada quando ele arrancar. Não copiar este estado
para outro EVI: os endereços guardados pertencem ao equipamento original.

A configuração APT fica em `/var/lib/wattwell-evi-apt`. As fontes globais e os hooks
APT do fabricante não são carregados por essa invocação isolada. Durante a
instalação de pacotes, uma política temporária recusa ações normalizadas de
arranque/reinício; a política anterior é restaurada em sucesso, erro e interrupção.
O journal permite recuperar essa política na próxima execução após SIGKILL ou
falha de energia. Alterações feitas entretanto por outro administrador não são
sobrescritas automaticamente. A política não é uma sandbox para chamadas diretas
que scripts de pacotes possam executar.

O processo pára se faltar confiança criptográfica, ferramentas, DHCP, acesso ao
arquivo, versões compatíveis ou se um plano exigir upgrades. Algumas etapas
anteriores podem já estar aplicadas: não existe uma transação única para toda a
configuração, nem rollback automático de pacotes. Não usa reparação geral do dpkg,
`upgrade`, `autoremove`, `--allow-unauthenticated` ou `curl -k`.

A imagem observada também tem metadados incompletos de `libapt-pkg5.0`. O script
não inventa um ficheiro `.list` nem declara a imagem integralmente reparada.
Se encontrar um pacote incompleto, pára antes de alterar hostname/rede ou instalar
pacotes, exceto no caso específico de `libc-bin` descrito abaixo.
Guarda automaticamente o diagnóstico em
`/var/lib/wattwell-evi/package-state-report.txt`: estado dos pacotes incompletos,
`dpkg --audit`, scripts `postinst` apenas lidos, excertos dos logs disponíveis e
presença de `ldconfig`. A recolha do diagnóstico não executa scripts de manutenção.
Para analisar a causa, obter o relatório com:

```sh
sudo cat /var/lib/wattwell-evi/package-state-report.txt
```

O ficheiro é privado; os excertos podem conter dados de instalações anteriores.
`half-configured` não identifica a causa por si só. Não usar `dpkg --configure -a`
ou `apt-get -f install` sem rever o erro, os scripts e o impacto nesta imagem.

Existe uma única recuperação automática: `libc-bin:armhf` **2.28-10** com estado
`install ok half-configured`, `Config-Version` igual e todos os restantes pacotes
em estados aceites. Exige o hash do `postinst` observado/revisto nesta imagem,
`nsswitch.conf` igual ao modelo existente, ausência de hooks dpkg de invocação e
um ensaio `ldconfig -N -X` bem-sucedido. Só então executa
`dpkg --no-triggers --configure libc-bin`, com política temporária de serviços.
Não descarrega nem reinstala libc, não faz upgrade, não configura outros pacotes
e não altera diretamente o estado na base de dados.

Esta tentativa executa o `ldconfig` normal do `postinst`: pode atualizar a cache
e os links de bibliotecas da imagem. Guarda cópias do estado dpkg, `nsswitch.conf`
e caches existentes em `backups/`, além de `libc-bin-configure.log` e
`libc-bin-dpkg.log` em `/var/lib/wattwell-evi`. Estes backups não são um rollback
automático de todos os links. Se falhar ou o inventário final diferir, pára;
numa imagem já configurada, a tentativa é omitida.

Antes de usar num equipamento operacional, seguir [recuperação](docs/recovery.md)
e testar numa cópia da imagem. O restauro de uma imagem não substitui a preservação
de calibração, identidade ou chaves que possam estar fora do filesystem.

## Limites e verificação

Requer Python 3.7+, `dpkg`, `dpkg-deb`, `gpgv`, o keyring Debian existente, `ip`,
`systemctl`, `networkctl` e o cliente/hook DHCP já presentes na imagem observada.
A recuperação de APT depende do snapshot oficial de 2020-09-01: se estiver
indisponível, pára sem escolher outra versão. Avahi depende dos arquivos Buster.
Não serve de atualizador do sistema: o suporte LTS Debian Buster terminou em 2024.

A resolução `.local` deve ser confirmada noutro computador na mesma LAN, com
suporte mDNS. O script não altera NSS ou configurações de rede desse computador.

```sh
shellcheck -s sh configure.sh scripts/*.sh
python3 -m unittest discover -s tests -v
```

Os testes usam ficheiros temporários e comandos simulados. Há validação real de
TLS com o conjunto incluído e de assinaturas dos metadados Buster atuais. A
instalação completa e o arranque do serviço novo ainda não foram ensaiados na
imagem Watt&Well. Os scripts anteriores de diagnóstico e ensaio DHCP continuam
em `scripts/` para investigação; o comando normal é apenas `configure.sh`.

Detalhes: [rede](docs/network.md), [APT](docs/apt-bootstrap.md),
[certificados](certs/README.md).
