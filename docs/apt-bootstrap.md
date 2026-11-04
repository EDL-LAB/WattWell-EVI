# Recuperação APT e instalação Avahi

Esta etapa faz parte de `sudo sh configure.sh`; não exige comandos manuais de
instalação. Destina-se ao estado observado: Buster `armhf`, imagem
`ww-distro-dh-v1.3.27`, APT/libapt `1.8.2.1` registados como instalados, executáveis
APT removidos e lista de ficheiros libapt ausente.

## Recuperação da mesma versão

Quando o APT existente já funciona e os métodos necessários existem, é reutilizado.
Caso contrário, a recuperação suporta apenas as versões registadas `1.8.2.1`.
A origem está fixada em:

```text
https://snapshot.debian.org/archive/debian/20200901T000000Z
```

O configurador verifica `Release.gpg` com o keyring Debian **já presente** na
imagem, exige Codename Buster, verifica o hash SHA-256 de
`main/binary-armhf/Packages.xz`, seleciona versão/arquitetura exatas e verifica o
SHA-256 de cada `.deb`. Há tentativas limitadas de descarga; um erro de rede,
assinatura ou versão ausente termina o processo, sem fallback para upgrades.
O snapshot é deliberadamente histórico; não se usa a validade temporal do índice
como prova de atualidade, e as assinaturas e hashes continuam obrigatórios.

Extrai os pacotes para uma pasta temporária privada e copia apenas ficheiros de
execução ausentes: ferramentas APT, métodos de transporte/autenticação, apt-helper
e biblioteca libapt. Não substitui ficheiros existentes, configurações, timers ou
scripts de arranque. Não executa `dpkg -i` desses pacotes, nem os seus scripts de
manutenção. Os ficheiros `.list` em falta não são reconstruídos automaticamente.

## Avahi sem alterar versões instaladas

O APT recebe configuração própria, incluindo fontes, preferências, listas,
certificados e cache. Não carrega os diretórios de configuração/hooks APT globais.
As fontes são apenas `buster main` e `buster/updates main` dos arquivos Debian,
com `signed-by` para o keyring existente. A exceção `check-valid-until=no` é
limitada a essas fontes arquivadas e não desativa autenticação.

Cada pacote instalado recebe uma preferência para a versão atual. O plano
simulado é recusado se contiver alteração/configuração de um pacote já instalado,
remoção ou purge. O inventário volta a ser comparado antes da aplicação e todas
as versões originais são verificadas depois. A aplicação usa `--no-remove`,
`--no-upgrade` e `--no-install-recommends`; estas opções complementam as
preferências e a validação do plano, não as substituem.

Uma política temporária `policy-rc.d` com saída 101 bloqueia os mecanismos normais
de arranque de serviços durante a instalação, preservando/restaurando a política
original e os seus atributos. Existe journal de recuperação. Isto não bloqueia
código arbitrário de scripts de pacotes nem constitui rollback de uma instalação
interrompida. Só depois é aplicada a configuração Avahi e arrancado o serviço.

Não são instalados `apt-utils`, `avahi-utils` ou `libnss-mdns` por defeito. Não há
alteração de `/etc/nsswitch.conf`, arranque de tarefas APT automáticas, atualização
de kernel, firmware, calibração ou reparação geral de pacotes.

## Verificação e limites

A cadeia de assinatura/hash é testada com fixtures, incluindo assinaturas e hashes
inválidos. TLS e assinaturas dos metadados Buster atuais foram verificados com o
conjunto de certificados incluído. O serviço oficial de snapshots devolveu erros
503/504 durante a preparação: a recuperação real de `1.8.2.1` e a instalação no EVI
não foram confirmadas. Se essa origem continuar indisponível, o script falha nessa
etapa e conserva as alterações anteriores, em vez de declarar sucesso.

Referências: [arquivo Debian](https://www.debian.org/distrib/archive.en.html),
[apt-secure Buster](https://manpages.debian.org/buster/apt/apt-secure.8.en.html),
[apt-get Buster](https://manpages.debian.org/buster/apt/apt-get.8.en.html),
[validade dos arquivos](https://ftp-master.debian.org/#valid-until).
