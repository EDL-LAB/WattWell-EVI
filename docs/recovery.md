# Preservação e recuperação

## Pré-condições da futura instalação

1. Identificar modelo/revisão do hardware e versão exata da imagem Watt&Well.
2. Garantir uma janela de manutenção sem sessão de carregamento e com o estado
   seguro definido pelo fabricante. O diagnóstico não pára serviços para isso.
3. Confirmar acesso de recuperação independente do sistema que será alterado:
   consola, suporte de arranque ou processo documentado pelo fabricante.
4. Obter uma cópia consistente do suporte, incluindo tabela de partições, boot,
   rootfs e partições de dados/configuração. Preferir aquisição offline pelo
   procedimento do fabricante; copiar um filesystem ativo pode ser inconsistente.
5. Guardar SHA-256, tamanho, data, identificação do equipamento e versão da imagem
   junto da cópia. Guardar o suporte original sem alterações sempre que possível.
6. Testar o restauro numa unidade ou suporte de reserva compatível. Confirmar
   arranque, acesso e funcionamento dos serviços EVI/EVCC antes de usar o processo
   num equipamento operacional.

Não há aqui um comando genérico de clonagem/restauro: o dispositivo de bloco e
o método correto dependem do hardware. Um destino errado pode destruir dados.
Uma cópia do disco também pode não incluir firmware, identidade, chaves ou
calibração em EEPROM, secure elements ou dispositivos externos; identificar com o
fabricante como os preservar e recuperar. Não os copiar para outra unidade sem
confirmar as restrições de identidade e calibração.

## Inventário a preservar fora do Git

- Relatório de diagnóstico, base completa `/var/lib/dpkg`, seleções/holds e estado
  APT existente. São evidências adicionais; não substituem a imagem de recuperação.
- Aplicações e dependências locais, unidades de serviço e políticas de arranque.
- Configuração EVI/EVCC, rede, identidade, certificados e chaves com permissões,
  proprietários, links e atributos relevantes preservados.
- Configuração e calibração QCA/PLC, incluindo o que não esteja no filesystem.

Na cópia local parcial consultada durante a preparação, `etc/distro_version`
continha `evi-distro-v1.4.4`. Existiam configurações `evis-*`, `ocpp-controller` e
`qca/calibration`. O código de `ww_get_versions` refere `/usr/local/etc` no
equipamento. Isto orienta o inventário, mas não prova a versão ou cobertura do
equipamento alvo. Os ficheiros privados dessa cópia não foram incluídos no projeto.

## Em caso de falha

Parar a sequência e preservar relatórios, saída do instalador e estado do `dpkg`.
Não executar `apt-get -f install`, `dpkg --configure -a`, purge, reboot ou alterações
de serviços como tentativa automática de reparação. O impacto depende da fase em
que a instalação falhou; uma operação de pacotes não é transacional.

Usar o procedimento de recuperação validado. Restaurar apenas `/var/lib/dpkg`
deixaria o inventário de pacotes possivelmente incoerente com os ficheiros reais.
Após o restauro, confirmar configurações, calibração, serviços e comportamento do
equipamento. A recuperação deve respeitar o estado seguro definido pelo fabricante.
