# Plano incremental: APT em Buster armhf

## 1. Diagnóstico — implementado

Recolher o inventário com `scripts/inspect-device.sh`. Confirmar Debian 10 Buster,
`dpkg --print-architecture` igual a `armhf`, versão Watt&Well, estado de todos os
pacotes e ausência de operações concorrentes. Identificar serviços críticos,
ficheiros protegidos e mecanismos de atualização do fabricante.

Bloquear a fase seguinte enquanto houver verificações incompletas, pacotes
parcialmente configurados, versão da imagem desconhecida ou recuperação por testar.

### Resultado recebido: verificar APT existente antes de instalar

O alvo é `ww-distro-dh-v1.3.27`, Debian 10 Buster `armhf`. A base regista APT e
`libapt-pkg5.0` `1.8.2.1`, `debian-archive-keyring` `2019.1` e `gpgv`
`2.2.12-1+deb10u1`. `curl`, `wget` e `gpgv` foram encontrados; `apt-get` não.
As bibliotecas registadas incluem `libc6` `2.28-10`, `libgcc1` `1:8.3.0-6` e
`libstdc++6` `8.3.0-6`. Os estados dos pacotes e a integridade dos ficheiros ainda
não tinham sido confirmados nessa primeira recolha.

A recolha seguinte confirmou APT/libapt/isc-dhcp-client como
`install ok installed`, mas `/usr/bin/apt` e `/usr/bin/apt-get` não existem.
`dpkg --verify` reporta diferenças em binários/documentação e em
`/etc/logrotate.d/apt`; não substituir essa configuração local. Falta também a
lista de ficheiros de `libapt-pkg5.0:armhf`, pelo que a ausência de outros avisos
não prova integridade dessa biblioteca. A imagem pode ter sido reduzida
intencionalmente; não concluir corrupção generalizada nem reconstruir metadados
do dpkg automaticamente. A recuperação APT deve respeitar este estado específico.

O teste HTTPS falhou na resolução de `archive.debian.org`, e as saídas de rotas
não mostram rota por defeito IPv4/IPv6. Resolver a ligação antes de preparar
downloads diretos; a presença de `curl`, `wget` e `gpgv` não prova Internet funcional.

Verificar `${Status}`, caminhos absolutos e `dpkg --verify` antes de instalar:
um PATH incompleto pode explicar comandos não encontrados, e a imagem pode conter
ficheiros intencionalmente removidos. Não executar `dpkg -i` da versão `1.8.2.3`
indicada abaixo neste alvo: isso atualizaria o pacote `1.8.2.1`, fora do âmbito.
Se houver ficheiros ausentes num pacote corretamente instalado, preparar uma
recuperação da **mesma versão**, autenticada, com revisão dos efeitos de scripts,
hooks e timers. Não reinstalar automaticamente. O arquivo corrente pode não
conter essa versão: procurar metadados históricos autenticados ou o pacote
original fornecido pelo fabricante, sem inventar hashes ou relaxar assinaturas.

## 2. Bundle autenticado — por implementar após inventário

Com Internet no EVI, preferir preparação direta no equipamento quando já existir
um cliente de descarga e um verificador de assinaturas adequado. Não instalar
ferramentas de verificação a partir de pacotes não autenticados para verificar
esses mesmos pacotes. Se faltar uma raiz de confiança utilizável, autenticar o
bundle no computador de apoio e transferi-lo pelo acesso autenticado existente.
Internet disponível não prova que certificados, chaves ou ferramentas existam.

Preparar `prepare-apt-bundle.sh` e
`manifests/buster-armhf.tsv`. O manifesto só será criado com valores verificados:
pacote, versão, arquitetura, caminho de origem e SHA-256. Não usar placeholders
executáveis nem selecionar automaticamente a versão mais recente.

Fontes candidatas, limitadas a Buster e componente `main`:

```text
deb [arch=armhf check-valid-until=no] https://archive.debian.org/debian buster main
deb [arch=armhf check-valid-until=no] https://archive.debian.org/debian-security buster/updates main
```

São entradas para futura configuração isolada; não as acrescentar à imagem nesta
fase. Em Buster a suite de segurança é `buster/updates`.

O arquivo contém `apt_1.8.2.3_armhf.deb` e
`libapt-pkg5.0_1.8.2.3_armhf.deb`. A seleção depende das bibliotecas e versões da
imagem. Não presumir que estes dois pacotes bastam, não incluir `apt-utils` por
defeito e não resolver dependências contra os pacotes do computador de apoio.

Autenticar `InRelease` ou `Release` + `Release.gpg` com chaves Debian de origem
confiável; validar o hash do índice `Packages` e depois o SHA-256 de cada `.deb`.
Preservar metadados e evidências de verificação com o bundle. O mecanismo de
transferência para o EVI tem de autenticar também o manifesto/bundle; uma lista de
hashes transferida pelo mesmo canal não confiável não é uma raiz de confiança.

A validade temporal dos índices históricos pode ser desativada apenas nas fontes
arquivadas indicadas. Isso não desativa assinatura, TLS ou verificação de hashes.
Falhas de chaves, assinaturas ou certificados devem interromper a preparação;
não acertar o relógio para o passado nem usar `trusted=yes`,
`--allow-unauthenticated` ou repositórios de outra distribuição.

Resolver `Depends`, `Pre-Depends`, alternativas, restrições de versão, conflitos
e arquitetura com o inventário real. Conservar versões de **todos** os pacotes
instalados. Se não existir uma solução que apenas acrescente pacotes ausentes,
parar e documentar a incompatibilidade. Rever ficheiros e scripts de manutenção
dos pacotes e triggers existentes, especialmente bibliotecas, utilizadores/grupos,
serviços, timers APT e ficheiros locais não geridos pelo `dpkg`.

## 3. Instalação controlada — por implementar e testar na cópia

`install-apt.sh` mostrará o plano por defeito. Apenas `--apply` permitirá escrever,
depois de validar novamente a imagem, o inventário, os hashes, as dependências,
os conflitos, o espaço, a recuperação e as operações concorrentes.

Instalar com `dpkg` uma lista explícita de pacotes ausentes, ordenada pelas
dependências. Nunca usar um glob de todos os `.deb` da pasta, opções `--force-*`,
upgrades/downgrades, remoções ou reparação geral. Verificar colisões também com
ficheiros do fabricante fora do inventário `dpkg`. Um pacote já instalado na
versão esperada deve ser ignorado; qualquer versão diferente exige nova análise.
Uma repetição após falha parcial pára para diagnóstico, em vez de se declarar
idempotente ou retomar sem verificar os efeitos anteriores.

Preservar integralmente uma política `policy-rc.d` existente. Qualquer mecanismo
temporário exige teste da sua remoção/restauro em sucesso, erro e interrupção;
se não for possível conciliá-lo com a política existente, abortar. Este mecanismo
não bloqueia chamadas diretas a gestores de serviços, nem todos os efeitos de
triggers ou scripts. A revisão do bundle e o ensaio na cópia são obrigatórios.
Não habilitar timers de atualização automática, reiniciar, parar ou reconfigurar
serviços críticos. Se não for possível cumprir isto, a instalação não avança.

Após instalar, `apt-get --version` e `dpkg --audit` devem funcionar. Comparar versões
de todos os pacotes originais, hashes e links protegidos, serviços, tempos de
arranque/PIDs quando disponíveis e tarefas automáticas antes/depois. Registar todas
as alterações esperadas. Não desfazer automaticamente uma instalação falhada.

O acesso futuro aos arquivos usará fontes e opções APT isoladas, indicadas em cada
invocação, sem modificar fontes do fabricante nem relaxar confiança globalmente.
Rever também os hooks e configurações APT herdadas. `apt-get update` só atualiza
índices, mas pode executar hooks locais: não faz parte do diagnóstico. Não executar
`upgrade`, `dist-upgrade`, `full-upgrade` ou `autoremove`. `--no-upgrade` sozinho
não impede upgrades das dependências.

## 4. Critérios de aceitação

- Ensaiar numa cópia da imagem Watt&Well ARM, com recuperação testada.
- Instalação limpa; segunda execução sem alterações; interrupção/falha reportada.
- Rejeitar arquitetura errada, hash adulterado, dependências incompatíveis,
  estado dpkg incompleto e alterações do inventário entre plano e aplicação.
- Nenhum pacote original muda de versão; configurações, calibração e serviços
  críticos preservados; nenhum trabalho automático APT fica ativo por acidente.
- Testes funcionais EVI/EVCC definidos para o hardware e aprovados em bancada.

## Referências

- [Arquivo Debian](https://www.debian.org/distrib/archive.en.html)
- [Pacotes APT arquivados](https://archive.debian.org/debian/pool/main/a/apt/)
- [Cadeia de confiança APT em Buster](https://manpages.debian.org/buster/apt/apt-secure.8.en.html)
- [Validade dos índices arquivados](https://ftp-master.debian.org/#valid-until)
- [apt-get em Buster](https://manpages.debian.org/buster/apt/apt-get.8.en.html)
- [dpkg em Buster](https://manpages.debian.org/buster/dpkg/dpkg.1.en.html)
- [Política de serviços](https://manpages.debian.org/buster/init-system-helpers/invoke-rc.d.8.en.html)
- [Fim do suporte LTS Buster, 30/06/2024](https://www.debian.org/News/2024/20240615)
