# urpm-ng

Um gestor de pacotes moderno para Mageia Linux, escrito em Python.

O urpm-ng é uma reescrita completa da suite urpmi clássica, com melhor desempenho, resolução de dependências mais fina e funcionalidades modernas como a partilha P2P de pacotes.

## Pré-requisitos

### Distribuição

De momento é preciso Mageia 9 ou Mageia 10.

### Portas de firewall (para partilha P2P)

O pacote `urpm-ng-daemon` entrega `/etc/shorewall/rules.urpm-ng` como
ficheiro de include, e o seu `%post` liga-o automaticamente a
`/etc/shorewall/rules`. Numa máquina gerida por Shorewall (o padrão
Mageia), as portas seguintes ficam portanto abertas logo após a
instalação, sem intervenção:

- **TCP 9876** (produção) ou **TCP 9877** (modo dev) -- API HTTP do urpmd
- **UDP 9878** (produção) ou **UDP 9879** (modo dev) -- broadcasts de descoberta de pares

Se o Shorewall não estiver em uso (apenas `iptables` / `nftables`),
abre as portas à mão — o ficheiro `/etc/shorewall/rules.urpm-ng` na
árvore de fontes serve de bom modelo.

## Instalação

### Pacotes

O urpm-ng está dividido em vários pacotes para maior flexibilidade:

| Pacote | Descrição |
|--------|-----------|
| `urpm-ng-core` | Mínimo: CLI, resolvedor, base de dados |
| `urpm-ng-daemon` | Daemon em segundo plano + partilha P2P |
| `urpm-ng` | Meta: puxa `-core` + `-daemon` (instalação padrão) |
| `urpm-ng-appstream` | Configuração dos metadados AppStream (metainfo OS Mageia, config distro) |
| `urpm-ng-packagekit-backend` | Backend PackageKit (Discover, GNOME Software) + serviço D-Bus |
| `urpm-ng-desktop` | Meta: puxa `-core` + `-daemon` + `-appstream` + `-packagekit-backend` |
| `urpm-ng-build` | Meta: puxa `-core` (para `urpm image` / `urpm build` — os comandos vivem no `-core`) |
| `urpm-ng-genmedia` | Geração de metadados de média do lado servidor (`urpm genmedia`, para mantenedores de espelhos) |
| `urpm-ng-all` | Meta: puxa tudo o que está acima |

**Escolher o pacote certo:**
- **Instalação mínima / contentor**: `urpm-ng-core`
- **Uso CLI padrão**: `urpm-ng`
- **Ambiente gráfico com software centers**: `urpm-ng-desktop`
- **Empacotadores (utilizadores de bm / mkimage)**: `urpm-ng-build`
- **Mantenedores de espelhos que publicam repositórios**: `urpm-ng-genmedia`

### Instalação / actualização rápida (`geturpm.sh`)

O `geturpm.sh` é a via recomendada para instalar o urpm-ng numa Mageia
fresca, e também consegue actualizar uma instalação existente. Ele
detecta automaticamente a release Mageia e a arquitectura, puxa o
urpm-ng mais recente do canal escolhido, e faz o que for adequado
consoante o urpm-ng já esteja instalado ou não (máquinas frescas fazem
bootstrap com `urpmi`; as actualizações seguintes passam pelo próprio
urpm-ng).

**Rápido — por pipe, sem inspecção local**

```bash
curl -fsSL https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh | bash
```

Os prompts (escolha do canal, «Proceed?», palavra-passe root para o
`su`) são lidos de `/dev/tty`, portanto a versão por pipe é
totalmente interactiva — mesma experiência que executar o script a
partir de um ficheiro.

**Verificado — descarregar, ler, depois executar** (recomendado se
ainda não confias na fonte):

```bash
curl -fsSLO https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh
less geturpm.sh                  # inspeccionar antes de executar
bash geturpm.sh                  # interactivo: pede canal e confirmação
```

**Escolha do canal** (`--channel=CHAN`):

- `mgabiz` — vai buscar ao repositório Mageia.biz (padrão quando não
  há terminal disponível). Usa `urpm media discover` no espelho
  mgabiz, portanto as actualizações futuras passam pelo fluxo padrão
  `urpm media update`.
- `github` — vai buscar os RPMs directamente à página de releases do
  GitHub. Útil para testar uma tag específica, ou quando a publicação
  mgabiz está atrasada em relação a uma release.

**Execuções sem supervisão** — acrescenta `-y` (salta a confirmação
«Proceed?») e `--channel=CHAN` (salta o prompt do canal) através de
`bash -s --`:

```bash
curl -fsSL <url>/geturpm.sh | bash -s -- -y --channel=mgabiz
```

Nota: na primeira instalação, o urpm-ng importa a sua configuração
automaticamente a partir dos ficheiros `urpmi.cfg` e `urpmi/skip.list`
existentes.

## Primeira execução

O urpm funciona à primeira. As opções avançadas (blacklist, redlist, kernel-keep) estão documentadas mais abaixo, na secção **Configuração**.

Quando instalado ao nível do sistema (em `/usr/bin/`), o urpm usa:
- Base de dados: `/var/lib/urpm/packages.db`
- Porta do daemon: 9876
- Ficheiro PID: `/run/urpmd.pid`

### Fontes de média

Numa instalação feita pela via RPM (ou via `geturpm.sh`), os médias
Mageia padrão e os servidores para os ir buscar são configurados
automaticamente: o `urpm-ng` importa o `urpmi.cfg` existente à primeira
execução e o `urpm server autoconfig` preenche o pool de espelhos a
partir da API de espelhos Mageia. Não é preciso mais nada para instalar
pacotes.

Numa máquina sem `urpmi.cfg` prévio (chroot fresco, build de imagem, ou
sistema que nunca teve urpmi), o mesmo bootstrap faz-se numa passagem
manual:

```bash
urpm media list                       # Nada ainda? bootstrap:
urpm media import                     # Lê /etc/urpmi/urpmi.cfg por omissão; no-op se ausente
urpm server autoconfig                # Puxa espelhos da API Mageia
urpm media update                     # Primeira sincronização de metadados
```

Para adicionar um **repositório comunitário** (MageiaLinux-Online,
mageia.biz, blogdrake, um espelho interno, ...), usa `urpm media
discover` — lê o `media.cfg` do repositório e adiciona todos os médias
que ele anuncia numa só chamada:

```bash
urpm media discover https://www.mageia.biz/repo/Mageia/mgabiz/10/x86_64/media/
urpm media discover --dry-run https://download.mageialinux-online.org/...   # Antevisão
```

O `urpm media add` fica reservado para um único média personalizado que
não seja compatível com o discover — ou seja, um que sabes que não é
publicado através de um `media.cfg`. Consulta a secção **Gestão de
médias** mais abaixo para a sintaxe.

---

# urpm - Interface de linha de comandos

## Opções globais

Estas opções aplicam-se à maioria dos comandos e colocam-se antes do subcomando:

```bash
-V, --version              # Mostrar a versão do urpm
-v, --verbose              # Saída verbosa
-q, --quiet                # Saída silenciosa
--nocolor                  # Desactivar saída a cores
--root DIR                 # Usar DIR como raiz para a instalação RPM (chroot, config urpm do host)
--urpm-root DIR            # Usar DIR como raiz para a config urpm E para a instalação RPM
```

Os parentes seguintes são herdados pelos comandos transaccionais e de consulta (`install`, `upgrade`, `erase`, `download`, `depends`, ...):

```bash
--arch ARCH                # Arquitectura alvo (por omissão: sistema actual)
--debug COMPONENT          # Activar saída de debug: solver, tsrun, orphans, download, timing, all
--watched PACKAGES         # Nomes de pacotes separados por vírgulas a observar durante a resolução
```

Nota: `--arch` (opção parente, fixa a arquitectura alvo da operação) é distinta de `--allow-arch` (opção local em install/upgrade/download, autoriza arquitecturas adicionais para além da do sistema — tipicamente `i686` para wine/steam em x86_64).

## Opções de apresentação

A maioria dos comandos suporta estas opções de saída:

```bash
--show-all            # Mostrar todos os elementos sem truncar
--flat                # Um elemento por linha (parsável por scripts)
--json                # Saída JSON (para uso programático)
```

Por omissão, as listas longas são apresentadas em múltiplas colunas e truncadas a 10 linhas com "... e mais N". Usa `--show-all` para ver tudo.

Exemplos:
```bash
urpm list installed --flat          # Um pacote por linha
urpm search firefox --json          # Saída JSON
urpm i task-plasma --show-all       # Mostrar todas as dependências
```

## Transacções atómicas vs best-effort

Desde a 0.7.9, o `urpm upgrade` corre em modo **best-effort** por omissão: os pacotes cujas dependências não podem ser satisfeitas são retirados da transacção e reportados no fim com a respectiva razão (dependência em falta, incompatibilidade de versão, cascata SRPM irmã, ...). A transacção é confirmada para tudo o resto. Passa `--atomic` para mudar para modo estrito (recomendado em servidores): qualquer pacote insolúvel aborta a transacção inteira.

O `urpm install`, pelo contrário, é **atómico por omissão**: se algum pacote pedido não puder ser instalado, toda a transacção é revertida. Passa `--no-atomic` para optar pelo modo best-effort no caminho de instalação.

## Códigos de saída

| Código | Significado |
|--------|-------------|
| 0      | Transacção concluída com sucesso, nenhum pacote ignorado |
| 1      | Falha grave: transacção abortada (modo atómico, rede, permissões, ...) |
| 2      | Transacção parcial: bem-sucedida mas pelo menos um pacote foi descartado (pacotes ignorados listados em stderr com a razão) |

Verificação scriptável para o caso parcial:

```bash
urpm upgrade --auto || [ $? -eq 2 ] && echo "ok ou parcial"
```

## Gestão de pacotes

### Instalar pacotes

```bash
urpm install <pacote>         # Instalar um pacote
urpm i <pacote>               # Alias curto

# Opções
--auto, -y                    # Modo não-interactivo
--test                        # Simulação (dry run)
--without-recommends          # Saltar pacotes recomendados
--with-suggests               # Instalar também os pacotes sugeridos
--force                       # Forçar apesar de problemas de dependências
--reinstall                   # Reinstalar pacotes já instalados (reparação)
--nosignature                 # Saltar verificação GPG (não recomendado)
--noscripts                   # Saltar scripts pre/post install (builds chroot/contentor)
--no-peers                    # Desactivar download P2P a partir de pares LAN
--only-peers                  # Descarregar apenas de pares LAN, não de espelhos a montante
--no-atomic                   # Modo best-effort (por omissão em install: atómico)
--download-only               # Descarregar para a cache, não instalar
--nodeps                      # Saltar resolução de dependências (com --download-only)
--all                         # Instalar todas as famílias correspondentes (ex.: php8.4 + php8.5)
--install-src                 # Instalar o RPM fonte (extrai spec/sources para ~/rpmbuild/)
--config-policy {keep,replace,ask}  # Política de conflitos em ficheiros de config (por omissão: keep)
--prefer=<prefs>              # Guiar as escolhas de alternativas (ver abaixo)
--allow-arch <arch>           # Autorizar arquitecturas adicionais (ex.: i686 para wine/steam)
--sync                        # Esperar pela conclusão total (triggers pós-instalação)
```

#### Instalação guiada por preferências

Quando se instalam pacotes com alternativas (ex.: phpmyadmin, que pode usar diferentes versões de PHP e servidores web), usa `--prefer` para orientar as escolhas:

```bash
# Preferir PHP 8.4 com Apache e php-fpm, excluir mod_php
urpm i phpmyadmin --prefer=php:8.4,apache,php-fpm,-apache-mod_php

# Preferir nginx em vez de apache
urpm i phpmyadmin --prefer=php:8.4,nginx,php-fpm
```

Sintaxe das preferências:
- `capability:version` — Restrição de versão (ex.: `php:8.4`)
- `pattern` — Preferir pacotes que fornecem esta capability (ex.: `apache`, `php-fpm`)
- `-pattern` — Desfavorecer pacotes que correspondem (ex.: `-apache-mod_php`)

As preferências trabalham sobre REQUIRES e PROVIDES dos pacotes, não sobre os nomes.

#### Filtragem por arquitectura

Por omissão, o urpm só considera pacotes que correspondam à arquitectura do sistema e `noarch`. Isto evita instalações acidentais de pacotes i686 em sistemas x86_64 quando os médias 32-bit estão activos.

Para instalar pacotes 32-bit (wine, steam, multilib):

```bash
urpm install wine --allow-arch i686
urpm install steam --allow-arch i686

# Múltiplas arquitecturas
urpm install meupacote --allow-arch i686 --allow-arch armv7hl
```

### Remover pacotes

```bash
urpm erase <pacote>           # Remover um pacote
urpm e <pacote>               # Alias curto

# Opções
--auto, -y                    # Modo não-interactivo
--test                        # Simulação (dry run)
--auto-orphans                # Remover também as dependências órfãs (implícito com -y a menos que --keep-orphans)
--keep-orphans                # Não remover as dependências órfãs
--erase-recommends            # Remover também os pacotes apenas recomendados (não requeridos)
--keep-suggests               # Manter os pacotes sugeridos pelos pacotes que ficam
--force                       # Forçar apesar de problemas de dependências
--debug {solver,tsrun,all}    # Activar debug para o resolvedor / transacção
--sync                        # Esperar pela conclusão total (triggers pós-desinstalação)
```

### Actualizar metadados (estilo apt)

```bash
urpm update                   # Actualizar todos os metadados de médias
urpm update "Core Release"    # Actualizar um média específico
```

Desde a 0.7.x, o `files.xml.lzma` é obtido em conjunto com o `synthesis.hdlist.cz` sempre que o média o publica — sem flag para activar.

### Descarregar pacotes (sem instalar)

```bash
urpm download <pacote>        # Descarregar um pacote para a cache
urpm dl <pacote>              # Alias curto
urpm download --only-peers pkg  # Só descarrega de pares LAN

# Opções
--release, -r <version>       # Release alvo para downloads cross-release (ex.: cauldron)
--buildrequires, --br [SPEC]  # Descarregar dependências de build (auto-detecta ou a partir de .spec/.src.rpm)
--without-recommends          # Saltar pacotes recomendados
--nodeps                      # Descarregar apenas os pacotes listados, sem dependências
--no-peers / --only-peers     # Igual a install (política de pares)
--allow-arch <arch>           # Autorizar arquitecturas adicionais
--arch <arch>                 # Herdado: arquitectura alvo
--show-all                    # Imprimir a lista completa de pacotes resolvidos
                              # (por omissão trunca a 20 com "... e mais N")
```

### Actualizar pacotes

```bash
urpm upgrade                  # Actualizar todos os pacotes
urpm u                        # Alias curto
urpm upgrade <pacote>         # Actualizar pacotes específicos

# Opções
--auto, -y                    # Modo não-interactivo
--test                        # Simulação (dry run)
--atomic                      # Modo estrito: aborta toda a transacção em qualquer pacote insolúvel.
                              # Por omissão é best-effort (ver "Transacções atómicas vs best-effort" acima).
--with-recommends             # Instalar pacotes recomendados
--with-suggests               # Instalar também pacotes sugeridos
--noerase-orphans             # Manter dependências órfãs (não as remover)
--download-only               # Descarregar para a cache sem aplicar a actualização
--nosignature                 # Saltar verificação GPG (não recomendado)
--no-peers / --only-peers     # Desactivar / limitar aos pares LAN
--force                       # Forçar actualização apesar de problemas de dependências
--config-policy {keep,replace,ask}  # Política de conflitos de config (por omissão: keep)
--allow-arch <arch>           # Autorizar arquitecturas adicionais (ex.: i686)
--sync                        # Esperar pela conclusão total (triggers pós-instalação)
```

### Remoção automática de órfãos

```bash
urpm autoremove               # Remover dependências não utilizadas (por omissão: --orphans)
urpm ar                       # Alias curto

# Selectores
--orphans, -o                 # Pacotes órfãos (por omissão)
--kernels, -k                 # Kernels antigos
--faildeps, -f                # Dependências de transacções interrompidas
--buildrequires, -b           # Dependências de build (--builddeps, --br)
--all, -a                     # Todos os anteriores

# Opções
--auto, -y                    # Modo não-interactivo
```

## Pesquisa e consulta

### Procurar pacotes

```bash
urpm search <padrão>          # Procurar por nome/resumo
urpm s <padrão>               # Alias curto
urpm q <padrão>               # Alias query (compatibilidade urpmq)

# Opções
--installed                   # Procurar apenas nos pacotes instalados
--unavailable                 # Listar pacotes instalados que já não estão em nenhum média
```

#### Encontrar pacotes indisponíveis

Lista os pacotes que estão instalados mas já não estão disponíveis em nenhum média configurado (como `urpmq --unavailable`):

```bash
urpm q --unavailable          # Listar todos os pacotes indisponíveis
urpm q --unavailable php      # Filtrar por padrão
```

### Mostrar informação de um pacote

```bash
urpm show <pacote>               # Mostrar detalhes do pacote
urpm info <pacote>               # Alias
urpm show --files <pacote>       # Acrescenta a lista de ficheiros do pacote
                                 # (rpm -ql se instalado, files.xml.lzma caso contrário)
urpm show --changelog <pacote>   # Acrescenta o registo de alterações do pacote
                                 # (rpm -q --changelog; apenas pacotes instalados)
```

### Listar pacotes

```bash
urpm list installed           # Listar pacotes instalados
urpm list available           # Listar pacotes disponíveis
urpm list updates             # Listar actualizações disponíveis
urpm list upgradable          # Alias para updates
```

### Dependências

```bash
urpm depends <pacote>         # Mostrar o que um pacote requer
urpm rdepends <pacote>        # Mostrar o que requer um pacote (dependências inversas)
urpm why <pacote>             # Explicar porque é que um pacote está instalado

# Opções para depends
--tree                        # Mostrar árvore de dependências
--prefer=<prefs>              # Filtrar por preferências (mesma sintaxe que install)
--legend                      # Mostrar legenda dos símbolos após a árvore

# Opções para rdepends
--tree                        # Mostrar árvore de dependências inversas
--all                         # Mostrar todas as dependências inversas recursivas (plano)
--depth=N                     # Profundidade máxima da árvore (por omissão: 3)
--hide-uninstalled            # Só mostrar caminhos que levam a pacotes instalados
--legend                      # Mostrar legenda dos símbolos após a árvore
```

Exemplo com preferências:
```bash
# Mostrar dependências do phpmyadmin preferindo PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

Exemplo com rdepends:
```bash
# Árvore de dependências inversas para rtkit, profundidade 10, só caminhos instalados
urpm rdepends --tree --hide-uninstalled --depth=10 rtkit
```

### Dependências fracas

```bash
urpm recommends <pacote>      # Mostrar pacotes recomendados por um pacote
urpm whatrecommends <pacote>  # Mostrar pacotes que recomendam um pacote
urpm suggests <pacote>        # Mostrar pacotes sugeridos por um pacote
urpm whatsuggests <pacote>    # Mostrar pacotes que sugerem um pacote
```

### Consultas sobre ficheiros

```bash
urpm provides <pacote>        # Listar ficheiros fornecidos por um pacote
urpm whatprovides <ficheiro>  # Encontrar qual pacote fornece um ficheiro
urpm find <padrão>            # Procurar ficheiros em pacotes (instalados + disponíveis)
urpm find -i <padrão>         # Procurar apenas em pacotes instalados
urpm find -a <padrão>         # Procurar apenas em pacotes disponíveis
urpm find <padrão> --all-versions  # Incluir todas as EVR que trazem o match
urpm find <padrão> --limit 500     # Aumentar o tecto padrão de 100 hits
```

O `urpm find` procura por omissão tanto em pacotes instalados como disponíveis. O `files.xml.lzma` é obtido automaticamente em cada `urpm media update` (condicional a o média anunciá-lo no `MD5SUM`), portanto não é necessário nenhum opt-in — o toggle `--sync-files` foi removido na 0.7.x.

## Marcação de pacotes

```bash
urpm mark manual <pacote>     # Marcar como instalado manualmente
urpm mark auto <pacote>       # Marcar como auto-instalado (dependência)
urpm mark show <pacote>       # Mostrar a razão da instalação
```

## Bloqueios de pacotes (holds)

Bloquear pacotes para prevenir actualizações e substituição por obsoletes:

```bash
urpm hold <pacote>            # Bloquear um pacote
urpm hold <pacote> -r "razão" # Bloquear com uma razão
urpm hold                     # Listar pacotes bloqueados
urpm unhold <pacote>          # Retirar o bloqueio
```

Os pacotes bloqueados ficam protegidos de:
- Actualizações de versão durante o `urpm upgrade`
- Substituição por pacotes que os obsoletem

Exemplo:
```bash
# O dhcpcd obsoleta dhcp-client, mas queres manter dhcp-client
urpm hold dhcp-client -r "Prefer dhcp-client over dhcpcd"

# Agora o urpm upgrade vai saltar dhcp-client e avisar:
#   Pacotes bloqueados (1) saltados:
#     dhcp-client (seria obsoletado por dhcpcd)

# Para autorizar a substituição mais tarde:
urpm unhold dhcp-client
```

## Histórico e desfazer

```bash
urpm history                  # Mostrar histórico de transacções (últimas 20)
urpm history -i               # Filtro: só transacções de instalação
urpm history -r               # Filtro: só transacções de remoção
urpm history -d <id>          # Mostrar detalhes da transacção <id>
urpm history --delete <id>... # Apagar transacções do log de histórico

urpm undo [id]                # Desfazer uma transacção (por omissão: a última). Regista
                              # uma entrada limpa no histórico. Usa --auto/-y para saltar o prompt.

urpm rollback <n>             # Rollback das n últimas transacções
urpm rollback to <id>         # Rollback até uma transacção específica
urpm rollback to <date>       # Rollback até uma data (AAAA-MM-DD ou DD/MM/AAAA)
```

## Transacções em segundo plano

Quando uma transacção é destacada (ex.: pelo daemon ou pelo PackageKit), acompanha o seu progresso com:

```bash
urpm progress                 # Mostrar o progresso corrente e sair
urpm progress --watch         # Observar continuamente até à conclusão
```

## Identidade da distribuição (`distro-switch`)

Uma máquina carrega uma única identidade de release de cada vez — ou uma
stable numérica (`10`, `11`, …), ou `cauldron`. Essa identidade decide
que média o resolvedor considera ao compor uma transacção de instalação
ou actualização; as média cuja `mageia_version` não coincide ficam
fora do pool de candidatos, mesmo que continuem activadas na BD.

Mudar de identidade é um acto deliberado (uma dist-upgrade em
filigrana), pelo que vive no seu próprio verbo em vez de em
`urpm config`.

```bash
urpm distro-switch cauldron     # move a máquina para cauldron
urpm distro-switch 11           # move-a para a árvore numérica mga11
urpm distro-switch cauldron:12  # cauldron com um numérico alvo explícito
```

Antes de aplicar a mudança, o comando:

- Verifica que pelo menos uma média activada já carrega a identidade
  alvo (caso contrário ficaria com um pool de candidatos vazio). O
  diagnóstico aponta para `urpm media autoconfig -r <alvo>` em caso de
  falha.
- Avisa sobre média da identidade antiga que continuam activadas —
  saem do campo de visão do resolvedor até serem realinhadas ou
  desactivadas.
- Actualiza best-effort o `system-numeric` (o numérico efectivo usado
  para renderizar as tags de release `.mgaN` e para semear
  `/etc/mageia-release` dentro dos contentores de compilação): a
  substituição explícita ganha primeiro, depois a própria identidade se
  for numérica, senão uma sondagem do `media.cfg` de um servidor
  activado.

Após a mudança, executar `urpm media update` para sincronizar as
metadata da nova identidade.

## Gestão de médias

```bash
urpm media list               # Listar médias configurados
urpm media add <url>          # Adicionar média Mageia oficial (auto-parsed)
urpm media add --custom "Nome" nome_curto <url>  # Adicionar média personalizado/terceiro
urpm media remove <nome>...   # Remover um ou mais médias
urpm media remove --all       # Remover TODOS os médias configurados (pede
                              # confirmação; -y/--auto salta-a).
                              # Servidores órfãos (sem média) são
                              # removidos na mesma passagem.
urpm media enable <nome>      # Activar um média
urpm media disable <nome>     # Desactivar um média
urpm media update [nome]      # Actualizar os metadados dos médias
urpm media import <file>      # Importar a partir de urpmi.cfg
urpm media link <nome> +srv -srv  # Ligar/desligar servidores a um média
urpm media set <nome> [opts]  # Modificar definições do média (sharing, replication, quota...)
urpm media seed-info <nome>   # Mostrar info do seed set (secções, contagem de pacotes, tamanho estimado)
urpm media autoconfig -r 10   # Auto-adicionar médias Mageia oficiais para a release 10
urpm media discover <url>     # Descobrir médias a partir do media.cfg de um repositório
```

Flags úteis para `urpm media add`:

```bash
--import-key                  # Importar a chave GPG anunciada pelo média
--allow-unsigned              # Autorizar pacotes não assinados (apenas médias personalizados)
--version <ver>               # Versão Mageia alvo (apenas médias personalizados: 9, 10, cauldron...)
--update                      # Marcar como média de actualizações
--disabled                    # Adicionar mas deixar desactivado
-y, --auto                    # Não-interactivo: aceitar o nome/short_name auto-detectado
```

### Importar médias a partir de um urpmi.cfg antigo

Migrar uma máquina Mageia existente de `urpmi` para o urpm-ng sem
adicionar cada fonte à mão. Tanto as entradas baseadas em URL como as
entradas `MIRRORLIST=` são importadas — estas últimas como médias
pendentes que o `urpm server autoconfig` vem equipar com servidores na
execução seguinte.

```bash
urpm media import /etc/urpmi/urpmi.cfg    # Caminho por omissão
urpm media import                          # Idem (por omissão /etc/urpmi/urpmi.cfg)

# Opções
--replace                     # Sobrescrever as entradas de média que já existem por short_name
-r, --release <version>       # Release Mageia alvo (por omissão: valor de /etc/mageia-release)
--arch <arch>                 # Arquitectura alvo (por omissão: `uname -m`)
-y, --auto                    # Não-interactivo: saltar o prompt de confirmação
```

### Descobrir médias a partir de um repositório

Descobrir todos os médias disponíveis num qualquer repositório
compatível Mageia (espelhos oficiais, repos comunitários como MLO,
espelhos corporativos):

```bash
urpm media discover https://repo.example.org/9/x86_64/media/       # Adicionar todos os médias
urpm media discover --dry-run https://repo.example.org/9/x86_64/media/  # Só antevisão
urpm media discover --sources --debug https://...                   # Incluir SRPMS e debug

# Forçar activar / desactivar categorias (nonfree, tainted, 32bit, all)
urpm media discover --with nonfree,tainted https://...
urpm media discover --without nonfree https://...
urpm media discover --with all https://...
```

O comando obtém o `media.cfg` do repositório, descobre todos os médias, e liga os servidores existentes que alojam o mesmo conteúdo (verificado pela checksum MD5 do `synthesis.hdlist.cz`).

### Ligação servidor-média

Ligar ou desligar servidores a fontes de média específicas:

```bash
urpm media link "Core Release" +mirror1 +mirror2   # Adicionar servidores
urpm media link "Core Updates" -oldserver          # Remover um servidor
urpm media link "Core Release" +all                # Adicionar todos os servidores disponíveis
urpm media link "Core Release" -all +preferred     # Reset e adicionar um
```

Nota: ao adicionar servidores, o urpm verifica que o conteúdo do média corresponde comparando as checksums MD5 do `synthesis.hdlist.cz` com os servidores de referência existentes.

### Auto-configurar médias

Adicionar automaticamente os médias Mageia oficiais para uma release:

```bash
urpm media autoconfig --release 10              # Adicionar todos os médias oficiais para Mageia 10
urpm media autoconfig -r cauldron               # Adicionar médias para Cauldron
urpm media autoconfig -r 10 --no-nonfree        # Saltar médias nonfree
urpm media autoconfig -r 10 --no-tainted        # Saltar médias tainted
urpm media autoconfig -r 10 -n                  # Dry-run: mostra o que seria adicionado
```

### Definições de média

Configurar a partilha e a replicação dos médias:

```bash
urpm media set "Core Release" --shared=yes           # Partilhar com pares P2P
urpm media set "Core Release" --replication=seed     # Replicação completa (estilo DVD)
urpm media set "Core Release" --replication=on_demand  # Cache do que é descarregado
urpm media set "Core Release" --quota=5G             # Limitar o tamanho da cache
urpm media set "Core Release" --retention=30         # Manter pacotes 30 dias
urpm media set "Core Release" --priority=10          # Prioridade mais alta
urpm media set "Core Release" --seeds=INSTALL,CAT_PLASMA5  # Secções de seed
```

Exemplos:
```bash
# Adicionar um média Mageia oficial (servidor e média auto-detectados)
urpm media add https://ftp.belnet.be/mageia/distrib/9/x86_64/media/core/release/

# Adicionar um média terceiro personalizado
urpm media add --custom "RPM Fusion" rpmfusion https://download1.rpmfusion.org/free/fedora/40/x86_64/os/
```

## Gestão de servidores

Os servidores são fontes de espelhos que podem servir vários médias. O urpm suporta múltiplos servidores por média para balanceamento de carga e failover.

```bash
urpm server list              # Listar servidores configurados (com país)
urpm server add <nome> <url>  # Adicionar um servidor (testa IP e faz scan dos médias)
urpm server remove <nome> ... # Remover um ou mais servidores
urpm server enable <nome>     # Activar um servidor
urpm server disable <nome>    # Desactivar um servidor
urpm server priority <nome> <n>  # Fixar prioridade do servidor (mais alto = preferido)
urpm server test [nome]       # Testar conectividade e detectar modo IP
urpm server ip-mode <nome> <mode>  # Fixar o modo IP (auto/ipv4/ipv6/dual)
urpm server autoconfig        # Auto-adicionar servidores a partir da API de espelhos Mageia
urpm server stats [nome]      # Mostrar estatísticas de desempenho de um servidor
urpm server status            # Mostrar servidores blacklisted / com baixa reputação
urpm server unblacklist <nome>   # Levantar o blacklist de um servidor (após revisão)
urpm server ack-blacklist <nome> # Confirmar um blacklist (silencia o aviso)
```

### Lista de servidores

Opções para urpm server list:
```bash
--all                 # Mostrar todos os servidores, incluindo os desactivados
```

### Modo IP

Cada servidor tem um modo IP para gerir a conectividade IPv4/IPv6:
- `auto` — Deixar o sistema decidir (pode causar timeout de 30s se IPv6 falhar)
- `ipv4` — Forçar apenas IPv4
- `ipv6` — Forçar apenas IPv6
- `dual` — Ambos funcionam, prefere IPv4 (recomendado para servidores dual-stack)

O modo IP é auto-detectado ao adicionar um servidor. Usa `server test` para re-detectar ou `server ip-mode` para fixar manualmente.

### Monitorização de largura de banda e failover automático

O urpm segue automaticamente o desempenho de download de cada servidor. Após cada download ou sincronização de metadados, a velocidade medida é registada com um EWMA (Exponentially Weighted Moving Average, α=0.3), dando inércia para que uma única transferência lenta não penalize injustamente um bom servidor.

Os servidores são tentados na ordem `priority DESC, bandwidth_kbps DESC`: se um servidor falhar durante um download ou uma sincronização de metadados, o seguinte melhor é tentado automaticamente sem intervenção do utilizador. Dentro de uma mesma sessão, estimativas de velocidade por servidor são também mantidas em memória, portanto a ordenação adapta-se em tempo real sem esperar pela execução seguinte.

O `urpm server autoconfig` mede a latência a todos os candidatos a espelho e persiste os resultados, portanto a ordenação de servidores é significativa desde o primeiro download.

### Blacklist e reputação

Um servidor que sirva um RPM corrompido ou não assinado é
**auto-blacklisted**: fica excluído dos downloads seguintes até o
revisares e o levantares. As falhas de assinatura são tratadas como
sinais activos de manipulação — sem auto-unblock temporal.

Em paralelo com o blacklist, o urpm mantém uma **pontuação de
reputação** deslizante de 24 h (base 100) que drena com corpos
corrompidos, HTTP 4xx/5xx, erros de rede e transferências lentas. A
pontuação reordena o pool sem excluir os servidores por completo.

```bash
urpm server status               # Listar servidores blacklisted e com baixa reputação
urpm server unblacklist <nome>   # Levantar o blacklist após revisão humana
urpm server ack-blacklist <nome> # Confirmar (silencia o aviso sem desbloquear)
```

Em `install` / `upgrade` / `media update`, um aviso vermelho persistente lista cada blacklist não confirmado com as instruções de reactivação — o aviso não desaparece sozinho, apenas `unblacklist` ou `ack-blacklist` o silenciam.

O `urpm server list` assinala as linhas blacklisted a vermelho, portanto uma vista de olhos ao pool diz-te imediatamente quem está fora.

### Filtragem geográfica

Os servidores descobertos a partir da API de espelhos Mageia trazem
metadados de país e continente. A secção de configuração `[server]`
(ver mais abaixo) permite restringir os espelhos aceites:

```ini
# /etc/urpm/conf.d/10-server.cfg
[server]
country_blacklist = UA, RU        # Excluir países específicos
continent_whitelist = EU          # Apenas espelhos europeus
```

A filtragem é aplicada quando os espelhos são adicionados (`urpm init`, `urpm media autoconfig`, `urpm server autoconfig`, e expansão do pool em segundo plano). Os servidores já em base de dados são preenchidos com o país à primeira execução; os que falham o filtro ficam desactivados automaticamente.

Define `auto_add = false` para impedir qualquer adição automática de espelhos.

Usa `urpm server stats [nome]` para inspeccionar as métricas recolhidas:

```
$ urpm server stats mirror1
mirror1  https://mirror.example.com/mageia/
  Status        : enabled
  Priority      : 50
  IP mode       : dual
  Bandwidth     : 12 400 KB/s
  Latency       : 18 ms
  Success rate  : 98% (245/250)
  Last check    : 3m ago
  Media         : Core Release, Core Updates, Nonfree Release
```

## Gestão de pares

Quando o urpmd está a correr em várias máquinas da mesma LAN, elas descobrem-se mutuamente e partilham os pacotes em cache (P2P).

```bash
urpm peer list                # Listar pares descobertos
urpm peer downloads [host]    # Mostrar pacotes descarregados a partir de pares (filtro por host)
urpm peer blacklist <host>    # Bloquear um par (ex.: se estiver a fornecer pacotes maus)
urpm peer unblacklist <host>  # Desbloquear um par
urpm peer clean <host>        # Apagar os RPMs descarregados a partir de um par específico
                              # (usar após blacklistar; <host> é obrigatório)
```

### Modo apenas-local

Usa `--only-peers` para descarregar exclusivamente de pares LAN sem fallback para espelhos a montante:

```bash
urpm i --only-peers firefox   # Instalar só se disponível a partir de pares
urpm u --only-peers           # Actualizar só com pacotes de pares
urpm download --only-peers pkg  # Descarregar só de pares
```

Útil para redes air-gapped ou quando queres garantir que todos os pacotes vêm de fontes locais de confiança.

## Gestão da cache

```bash
urpm cache info               # Mostrar informação da cache
urpm cache clean              # Remover RPMs órfãos da cache
urpm cache rebuild            # Reconstruir a base de pacotes a partir dos ficheiros synthesis
urpm cache rebuild-fts        # Reconstruir o índice FTS para pesquisa rápida de ficheiros
urpm cache stats              # Estatísticas detalhadas
```

O `urpm cache clean` aceita `--dry-run/-n` (antevisão), `--auto/-y` (sem confirmação) e `--verbose/-v` (lista cada ficheiro órfão).

## Mirror / Replicação

O urpm-ng pode replicar localmente um subconjunto de pacotes (parecido com um conjunto de instalação em DVD) e expô-los aos pares LAN. Útil para install parties, instalações offline e para montar um espelho interno.

Duas peças móveis:

- **Política por média** — `urpm media set <nome> --replication=...`
  controla como cada média é replicado (apenas metadados, cache a
  pedido, ou seed completo).
- **`urpm mirror` de nível superior** — estado global do lado do
  daemon (quotas, versões servidas, limite de débito de saída) e
  disparadores explícitos de manutenção.

### Controlo do mirror ao nível superior

```bash
urpm mirror status            # Mostrar estado do mirror, quotas e versões servidas
urpm mirror enable            # Começar a servir pacotes em cache aos pares
urpm mirror disable           # Parar de servir pacotes
urpm mirror quota [SIZE]      # Mostrar ou fixar a quota global da cache (ex.: 10G, 500M)
urpm mirror enable-version 10,cauldron   # Retomar o serviço para estas versões
urpm mirror disable-version 8,9          # Parar o serviço para estas versões
urpm mirror clean [-n]        # Aplicar quotas e políticas de retenção (--dry-run antevisão)
urpm mirror sync [média]      # Forçar sincronização de replicação para médias em política `seed`
urpm mirror sync --latest-only           # Sync mais pequeno, estilo DVD
urpm mirror rate-limit [on|off|N/min]    # Configurar limite de débito de saída
```

### Replicação baseada em seed

A replicação usa o ficheiro `rpmsrate-raw` da Mageia para determinar que pacotes espelhar (mesma lógica que o conteúdo do DVD).

```bash
# Activar replicação seed-based num média
urpm media set "Core Release" --replication=seed
urpm media set "Core Updates" --replication=seed

# Ver o seed set calculado
urpm media seed-info "Core Release"
# Saída:
#   Sections: INSTALL, CAT_PLASMA5, CAT_GNOME, ...
#   Seed packages from rpmsrate: 437
#   Locale patterns: 3
#   Expanded locale packages: +237
#   With dependencies: 2300 packages
#   Estimated size: ~3.5 GB

# Forçar sync (descarregar pacotes em falta)
urpm mirror sync

# Sync apenas da última versão de cada pacote (mais pequeno, estilo DVD)
urpm mirror sync --latest-only
```

### Como funciona

1. Faz parse de `/usr/share/meta-task/rpmsrate-raw` (do pacote meta-task)
2. Extrai pacotes das secções: INSTALL, CAT_PLASMA5, CAT_GNOME, CAT_XFCE, etc.
3. Expande padrões de locale (ex.: `libreoffice-langpack-ar` → todos os langpacks)
4. Resolve dependências (Requires + Recommends)
5. Descarrega em paralelo os pacotes em falta

As secções seed por omissão cobrem todos os ambientes gráficos e aplicações principais, resultando em ~5 GB de pacotes (comparável a um DVD Mageia).

### Políticas de replicação

```bash
urpm media set <nome> --replication=none       # Só metadados, sem pacotes
urpm media set <nome> --replication=on_demand  # Cache do que é descarregado (por omissão)
urpm media set <nome> --replication=seed       # Conteúdo estilo DVD a partir de rpmsrate
```

## Configuração

### Blacklist (nunca instalar/actualizar)

```bash
urpm config blacklist list    # Mostrar pacotes na blacklist
urpm config blacklist add <pkg>
urpm config blacklist remove <pkg>
```

### Redlist (avisar antes de auto-remove)

```bash
urpm config redlist list      # Mostrar pacotes na redlist
urpm config redlist add <pkg>
urpm config redlist remove <pkg>
```

### Gestão do kernel

```bash
urpm config kernel-keep       # Mostrar quantos kernels manter
urpm config kernel-keep <n>   # Fixar o número de kernels a manter
```

### Modo de versão (sistema vs cauldron)

Quando os médias de sistema e de cauldron estão ambos configurados, o `version-mode` escolhe qual ganha nas actualizações:

```bash
urpm config version-mode              # Mostrar o modo actual
urpm config version-mode system       # Manter a versão de sistema instalada
urpm config version-mode cauldron     # Ir com cauldron
urpm config version-mode auto         # Retirar a preferência explícita
```

### Hooks de auto-upgrade para software centers

Controla se o GNOME Software, o KDE Discover ou o caminho de update offline do PackageKit podem instalar actualizações por sua própria iniciativa:

```bash
urpm config gnome-auto-upgrades [yes|no]      # GNOME Software
urpm config discover-auto-upgrades [yes|no]   # KDE Discover
urpm config packagekit-auto-upgrades [yes|no] # Actualizações offline do PackageKit
```

Sem argumento, cada subcomando imprime a definição actual. Estes hooks alternam as definições dconf/PolicyKit do lado do ambiente gráfico; a política do sistema é aplicada separadamente pelo pacote `urpm-ng-desktop`.

### Inspeccionar ou editar a configuração

```bash
urpm config show              # Mostrar a config efectiva de todos os *.cfg
urpm config edit              # Abrir urpm.cfg no $EDITOR
urpm config edit 00-urpmi-compat   # Abrir um drop-in específico
```

### Selecção de servidores

A secção `[server]` em `/etc/urpm/conf.d/10-server.cfg` controla a selecção automática de espelhos:

| Chave | Padrão | Descrição |
|-------|--------|-----------|
| `auto_add` | `true` | Autorizar a adição automática de espelhos |
| `country_blacklist` | *(vazio)* | Códigos ISO 3166 separados por vírgula a excluir (ex.: `UA, RU`) |
| `country_whitelist` | *(vazio)* | Aceitar apenas estes países (prevalece sobre blacklist) |
| `continent_blacklist` | *(vazio)* | Códigos de continente a excluir (`EU`, `NA`, `SA`, `AS`, `AF`, `OC`) |
| `continent_whitelist` | *(vazio)* | Aceitar apenas estes continentes (prevalece sobre blacklist) |

Um espelho tem de passar **ambos** os filtros, continente e país. A whitelist ganha à blacklist em cada nível. Usa `urpm config show` para ver as definições efectivas.

## Chaves GPG

```bash
urpm key list                 # Listar chaves GPG instaladas
urpm key import <ficheiro|url> # Importar uma chave GPG
urpm key remove <keyid>       # Remover uma chave GPG
```

## Dependências de build

Instalar dependências de build para construção de RPMs:

```bash
urpm install --buildrequires foo.spec    # A partir de um ficheiro spec
urpm install --buildrequires foo.src.rpm # A partir de um RPM fonte
urpm i -b                                # Auto-detecta na árvore de build RPM
urpm i --br                              # Alias curto

# Opções
--sync                        # Esperar que todos os scriptlets terminem
```

As dependências de build instaladas são registadas em `/var/lib/rpm/installed-through-builddeps.list` e ficam excluídas da remoção regular de órfãos. Para as limpar:

```bash
urpm autoremove --buildrequires          # Remover todas as build deps registadas
urpm ar -b                               # Forma curta
```

## Sistema de build em contentor

O urpm fornece um sistema de build completo em contentor para pacotes RPM, usando Docker ou Podman.

### Gestão de imagens

```bash
# Listar imagens de build disponíveis
urpm image list

# Actualizar uma imagem existente (re-sync de médias + pacotes)
urpm image update mageia:10-build

# Apagar uma ou mais imagens
urpm image delete mageia:10-build mageia:10-ci
```

### Criar uma imagem de build

```bash
urpm image make --release 10 --tag mageia:10-build
urpm image make --release 10 --tag mageia:10-ci --profile ci

# Imagem de build para um .spec ou .src.rpm (auto-instala BuildRequires)
urpm image make --release 10 --tag mga:10-foo --buildrequires SPECS/foo.spec

# Opções
-r, --release <version>       # Versão Mageia (ex.: 10, cauldron)
-t, --tag <tag>               # Tag da imagem (ex.: mageia:10-build)
--profile <name>              # Perfil de pacotes (por omissão: build)
--arch <arch>                 # Arquitectura alvo (por omissão: host)
-p, --packages <list>         # Pacotes adicionais (separados por vírgula)
--buildrequires <spec|srpm>   # Instalar BuildRequires de um .spec ou .src.rpm
--addmedia <NAME> <URL>       # Adicionar um média extra dentro da imagem (repetível) --
                              # ex.: um espelho terceiro ou interno
--import-key <URL>            # Importar uma chave pública GPG dentro da imagem (repetível) --
                              # combina com --addmedia para médias terceiros assinados
--runtime docker|podman       # Runtime de contentor (por omissão: auto-detecção)
--keep-chroot                 # Manter o chroot temporário após criação da imagem
-w, --workdir <path>          # Directório de trabalho para o chroot (por omissão: ~/.cache/urpm/mkimage).
                              # Também serve de TMPDIR para a fase de commit do podman, para
                              # que os blobs de imagem não transbordem um /tmp apertado.
--exclude PKG                 # Remove PKG da imagem final via
                              # `urpm erase --force --keep-orphans --sync` (repetível).
                              # Uso canónico: `--exclude python3-zstandard` para que o
                              # mach do firefox não tropece na sua própria restrição de versão.
--urpm-ng-source auto|local|media|github
                              # Onde ir buscar o urpm-ng-core (por omissão: cascata auto)
--urpm-ng-core <path>         # Instala o urpm-ng-core a partir deste RPM específico
--allow-disttag-mismatch      # Aceita um RPM local cujo disttag fica fora da janela do
                              # alvo (por omissão: apenas .mgaN. para numérico;
                              # .mgaN. e .mga{N-1}. para cauldron/N — o empacotador que
                              # recompila na sua stable já está coberto sem esta flag).
```

**Identidade de release em `--release`.** O argumento aceita três formas:

- `--release 10` — fixa a identidade da máquina numa stable numérica.
- `--release cauldron` — fixa na árvore de desenvolvimento em movimento.
  O numérico efectivo (usado para as tags de release `.mgaN` e para o
  macro `%mgaversion` dentro dos contentores de compilação) é sondado
  best-effort a partir do `media.cfg` do espelho no init. Offline ou
  quando a sondagem falha, fica indefinido e os consumidores recorrem
  a `/etc/mageia-release`.
- `--release cauldron:11` — cauldron com um numérico alvo explícito.
  Vence a sondagem, funciona offline e prevalece sobre o espelho quando
  o `media.cfg` do lado servidor está atrasado durante uma janela de flip.

> **Compatibilidade retroactiva:** `urpm mkimage` mantém-se como alias de `urpm image make`.

### Perfis

Os perfis definem que pacotes são instalados na imagem:

| Perfil | Descrição |
|--------|-----------|
| `build` | Ambiente de build RPM (por omissão): rpm-build, gcc, make, etc. |
| `ci` | CI/testing: python3-pytest, git, python3-solv, etc. |
| `minimal` | Sistema mínimo utilizável com urpm |

Os perfis são carregados de:
- `/usr/share/urpm/profiles/*.yaml` (sistema, do pacote)
- `/etc/urpm/profiles/*.yaml` (adições locais)

### Construir pacotes

Por omissão, `urpm build` faz auto-update dos médias e pacotes dentro do contentor antes de compilar, para que os builds corram sempre contra o último estado do repositório. Usa `--no-update` para saltar este passo em offline ou para acelerar builds repetidos.

```bash
# Build a partir de RPM fonte (saída para ./build-output/)
urpm build -i mageia:10-build foo-1.0-1.mga10.src.rpm

# Build a partir de ficheiro spec (saída para workspace/RPMS/ e SRPMS/)
urpm build -i mageia:10-build SPECS/foo.spec

# Build sem auto-update prévio de médias/pacotes
urpm build -i mga10-build --no-update SPECS/foo.spec

# Build com dependências locais (ex.: libfoo construída antes)
urpm build -i mageia:10-build SPECS/bar.spec -w 'RPMS/x86_64/libfoo*.rpm'

# Várias dependências locais
urpm build -i mageia:10-build SPECS/app.spec \
    -w 'RPMS/x86_64/libfoo*.rpm' -w 'RPMS/x86_64/libbar*.rpm'

# Vários builds em paralelo
urpm build -i mageia:10-build *.src.rpm --parallel 4

# Empacotador terceiro: marca a saída como foo-1.0-1.mlo.mga10.x86_64.rpm
urpm build -i mageia:10-build --subrel mlo SPECS/foo.spec

# Sobrescrever packager/vendor/dist sem tocar no spec
urpm build -i mageia:10-build --rpmmacros ./my-macros SPECS/foo.spec

# Opções
-i, --image <tag>             # Imagem Docker/Podman a usar
-o, --output <dir>            # Directório de saída para builds SRPM (por omissão: ./build-output)
-w, --with-rpms <pattern>     # Pré-instalar RPMs locais antes do build (glob, repetível)
--no-update                   # Saltar auto-update de médias e pacotes antes do build
--runtime docker|podman       # Runtime de contentor (por omissão: auto-detecção)
-j, --parallel <N>            # Builds isolados multi-contêiner (por omissão: 1, encadeados em contêiner compartilhado)
--stop-on-fail                # Parar a cadeia no primeiro spec com falha (por omissão: continuar)
--rollback-between-builds     # Reverter os BuildRequires de cada spec entre builds (alias: --rbb)
--keep-container              # Manter o contentor após o build (para debug)
--subrel <tag>                # Injecta %subrel TAG para que os RPMs de saída fiquem NAME-VERSION-RELEASE.TAG.DIST.ARCH.rpm
--rpmmacros <file>            # Injecta FILE como /root/.rpmmacros no contentor de build (combinável com --subrel)
--build-cpus N                # Limita o paralelismo de compilação a N threads
                              # (rpmbuild %_smp_mflags = -jN + podman --cpus).
                              # Por omissão: max(1, nproc - 2), para que o host mantenha
                              # dois núcleos livres para o trabalho interactivo.
--build-memory SIZE           # Tecto de RAM do contentor (ex.: 8G, 12000M, 16GB).
                              # Transmitido a podman --memory. Por omissão: max(2G, MemTotal - 2G).
--full-throttle               # Atalho: sem tecto de CPU, sem tecto de memória.
                              # Sobrepõe-se a --build-cpus e --build-memory.
--strict-memory               # Amarra --memory-swap a --build-memory (podman mata o processo
                              # ao atingir o tecto de RAM). Por omissão: swap ilimitado,
                              # alinhado com mock/systemd-nspawn. Usar em CI onde swap silencioso
                              # não seria distinguível de um hang.
--with FEATURE                # Encaminha `--with FEATURE` ao rpmbuild (%bcond do spec). Repetível.
--without FEATURE             # Encaminha `--without FEATURE` ao rpmbuild (%bcond do spec). Repetível.
```

#### Tectos de recursos e paridade com mock

O trio `--build-cpus` / `--build-memory` / `--strict-memory` é a alavanca
principal para compilar specs pesados (firefox, thunderbird, chromium)
em máquinas que não têm 32+ GB de RAM livre. Os valores por omissão
deixam ao host dois CPUs e dois GB de RAM para que se mantenha
utilizável e, sobretudo, **o swap fica ilimitado por omissão** — o
contentor pode despejar páginas frias no swap do host tal como faz o
wrapper systemd-nspawn do mock. Sem isso, o rustc do firefox apanha
`SIGKILL` bem antes do verdadeiro tecto de RAM em hosts < 16 GB.
`--strict-memory` volta a amarrar `--memory-swap` para a CI, onde swap
silencioso seria indistinguível de um hang.

#### Encaminhamento de bcond ao rpmbuild

`--with FEATURE` e `--without FEATURE` são encaminhados tal e qual ao
rpmbuild, para que specs que declarem `%bcond_with` / `%bcond_without`
possam ser alternados sem invocar o rpmbuild à mão. Exemplo: um spec
firefox que declara `%bcond_without unified_build` (unidades de
tradução unificadas activas por omissão) pode ser compilado sem elas
para um teste com restrições de memória via
`urpm build --without unified_build ./SPECS/firefox.spec`.

### Layout do workspace

Para builds a partir de spec, o urpm suporta o layout padrão de workspace RPM:

```
workspace/
├── SPECS/
│   └── foo.spec
└── SOURCES/
    ├── foo-1.0.tar.gz
    └── patches/
```

Os resultados são colocados em:
```
workspace/
├── RPMS/
│   └── x86_64/
│       └── foo-1.0-1.mga10.x86_64.rpm
└── SRPMS/
    └── foo-1.0-1.mga10.src.rpm
```

### Exemplo de workflow

```bash
# 1. Criar a imagem de build (uma vez)
urpm image make --release 10 --tag mga:10-build

# 2. Construir um pacote
urpm build --image mga:10-build ./mypackage.src.rpm

# 3. Mais tarde, actualizar a imagem para apanhar novos pacotes do repositório
urpm image update mga:10-build

# 4. Verificar os resultados
ls ./build-output/
```

### Bootstrap manual (avançado)

Debaixo do capot, `urpm image make` chama `urpm init` dentro de um
chroot fresco para popular o catálogo de médias. `urpm init` está
exposto directamente para chamadores que precisem de fazer bootstrap
de um rootfs fora do caminho conteinerizado — scripts de instalador,
builds de disco VM, ou raízes de teste pré-preparadas. Os espelhos
são retirados da API de espelhos Mageia e filtrados pela secção
`[server]` de `/etc/urpm/conf.d/10-server.cfg`.

```bash
# Bootstrap de um rootfs chroot para Mageia 10
urpm --urpm-root /tmp/rootfs init --release 10 --arch x86_64

# Usar uma lista de espelhos personalizada
urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'

# Opções
--release, -r <version>     # Versão Mageia alvo (10, cauldron, ...)
--mirrorlist <url>          # Sobrescrever a URL de lista de espelhos auto-gerada
--arch <arch>               # Arquitectura alvo (por omissão: host)
--auto, -y                  # Modo não-interactivo
--no-sync                   # Configurar os médias mas saltar a sincronização inicial
```

Depois de trabalhares dentro de um chroot `--urpm-root`, desmonta `/dev` e `/proc` montados pelo `urpm init`:

```bash
urpm --urpm-root /tmp/rootfs cleanup
```

## Ferramentas para mantenedores de repositório

Os dois comandos abaixo destinam-se a quem **publica** um repositório
compatível Mageia, não a quem o consome. Documentam-se em conjunto para
que se mantenha óbvio qual entrega metadados ao cliente e qual os
produz.

- **`urpm appstream`** (lado cliente) — refresca o catálogo AppStream
  na máquina actual para que os software centers vejam descrições
  actualizadas. Vive em `urpm-ng-appstream`.
- **`urpm genmedia`** (lado servidor) — produz o conjunto completo de
  metadados de média que um espelho serve aos clientes. Vive em
  `urpm-ng-genmedia`, sub-pacote separado para que a instalação
  cliente base fique leve.

### Metadados AppStream (`urpm appstream`)

O urpm consegue produzir e refrescar os catálogos AppStream consumidos pelo KDE Discover e pelo GNOME Software:

```bash
urpm appstream generate              # Gerar catálogo a partir da base de pacotes
urpm appstream generate -m core/release    # Limitar a um média específico
urpm appstream generate --no-compress       # XML simples em vez de gzip
urpm appstream status                # Mostrar o estado do catálogo por média
urpm appstream merge                 # Fundir os ficheiros por média no catálogo unificado
urpm appstream merge --refresh       # Refrescar também a cache AppStream do sistema
urpm appstream init-distro           # Criar o ficheiro metainfo do OS (necessário para Discover/GS)
urpm appstream init-distro --force   # Sobrescrever um metainfo existente
```

### Geração de médias (`urpm genmedia`)

O `urpm genmedia` é o par do lado servidor do `urpm appstream`: onde o
`appstream` consome catálogos para popular bases de dados clientes, o
`genmedia` **produz** o conjunto completo de metadados de média que um
espelho Mageia serve aos seus clientes. É uma reescrita em Python do
histórico `genhdlist3`, integrada no urpm-ng e empacotada em separado
como `urpm-ng-genmedia` para que a pegada de dependências fique fora
da instalação cliente base.

A partir de um directório de ficheiros RPM:

```bash
urpm genmedia /path/to/rpms          # Por omissão: geração completa
urpm genmedia /path/to/rpms --incremental   # Saltar RPMs cujo SHA-256 não mudou
urpm genmedia /path/to/rpms --no-hdlist     # Saltar a saída hdlist.cz
urpm genmedia /path/to/rpms --xml-info      # Forçar regeneração dos ficheiros XML info
urpm genmedia /path/to/rpms --appstream-info  # Gerar catálogo AppStream
urpm genmedia /path/to/rpms --no-md5sum     # Saltar MD5SUM (mais rápido para testes)
urpm genmedia /path/to/rpms --allow-empty-media  # Tolerar um directório de entrada vazio
```

O comando produz o layout canónico esperado por qualquer cliente urpm-ng ou urpmi:

```
media_info/
  hdlist.cz                # Headers de pacotes binários comprimidos
  synthesis.hdlist.cz      # Síntese leve de dependências
  files.xml.lzma           # Listas de ficheiros por pacote
  info.xml.lzma            # URL, sourcerpm, licença, descrição
  changelog.xml.lzma       # Changelogs por pacote
  appstream.xml.gz         # Quando --appstream-info está activo
  MD5SUM                   # Checksums de tudo o que está acima
```

A passagem AppStream extrai os ficheiros `*.metainfo.xml` embebidos e entregues pelas aplicações a montante (KDE, GNOME, etc.) e gera um componente mínimo a partir dos campos de header RPM para pacotes que dele precisam mas não o fornecem. Os pacotes cujo conteúdo é inteiramente não-user-facing (headers devel, símbolos de debug, arquivos estáticos, bibliotecas runtime puras) são **filtrados** em vez de emitidos com uma categoria fallback ``System`` — atulhariam o Discover e o GNOME Software sem nunca serem instaláveis por uma app store.

O directório `media_info/` é bloqueado enquanto uma geração corre, para que os clientes que lêem em concorrência vejam sempre um snapshot consistente.

## Mensagens README de pacotes

O `urpm readme` mostra as mensagens README de pacotes apresentadas ao utilizador durante uma transacção (a Mageia guarda-as como `README.urpmi` / `README.upgrade`):

```bash
urpm readme                          # README da transacção mais recente
urpm readme --transaction <id>       # README de uma transacção específica
urpm readme --list                   # Listar transacções com mensagens README
```

## Limpeza de órfãos

```bash
urpm cleandeps                # Alias para `urpm autoremove --faildeps`:
                              # remove dependências órfãs deixadas para trás
                              # por transacções interrompidas.
```

---

# urpmd - Daemon em segundo plano

O urpmd é um serviço em segundo plano que fornece:
- API HTTP para operações sobre pacotes
- Tarefas agendadas em segundo plano
- Descoberta P2P de pares para partilha LAN de pacotes



## Endpoints da API

### Endpoints GET

| Endpoint | Descrição |
|----------|-----------|
| `/` | Info do serviço |
| `/api/ping` | Health check |
| `/api/status` | Estado do daemon |
| `/api/media` | Lista médias configurados |
| `/api/available` | Lista pacotes disponíveis |
| `/api/updates` | Lista actualizações disponíveis |
| `/api/peers` | Lista pares LAN descobertos |

### Endpoints POST

| Endpoint | Descrição |
|----------|-----------|
| `/api/refresh` | Refresca metadados de médias |
| `/api/available` | Consulta pacotes disponíveis |
| `/api/announce` | Anuncia pacotes aos pares |
| `/api/have` | Consulta se um par tem pacotes específicos |

## Tarefas agendadas

O daemon efectua automaticamente:
- Sincronização de metadados de médias
- Limpeza da cache
- Verificação de disponibilidade de actualizações
- Descoberta de pares (broadcast UDP)

## Partilha P2P de pacotes

Quando várias máquinas na mesma LAN estão a correr o urpmd, elas descobrem-se automaticamente e podem partilhar os pacotes RPM em cache, reduzindo o uso de largura de banda.

---

# Integração GUI (Discover / GNOME Software)

O urpm-ng fornece um backend PackageKit que permite aos software centers gráficos gerir pacotes.

## Instalação

```bash
urpm install urpm-ng-desktop
```

Ou instala directamente o backend:
```bash
urpm install urpm-ng-packagekit-backend
```

Isto instala:
- `libpk_backend_urpm.so` — Backend PackageKit
- Serviço D-Bus `org.mageia.Urpm.v1` — Operações privilegiadas
- Políticas PolicyKit — Prompts de autorização
- Configuração AppStream — Metadados de catálogo de software

## Aplicações suportadas

- **KDE Discover** — Suporte completo (pesquisa, install, remove, actualizações)
- **GNOME Software** — Suporte completo (pesquisa, install, remove, actualizações)

## Como funciona

```
┌─────────────────┐
│  Discover /     │
│  GNOME Software │
└────────┬────────┘
         │
┌────────▼────────┐
│   PackageKit    │
│ (libpk_backend_ │
│    urpm.so)     │
└────────┬────────┘
         │
┌────────▼────────┐
│  Serviço D-Bus  │
│  + PolicyKit    │
│ (org.mageia.    │
│   Urpm.v1)      │
└────────┬────────┘
         │
┌────────▼────────┐
│  urpm-ng core   │
│  (Python)       │
└─────────────────┘
```

### rpmdrake-ng (Qt6)

Está em desenvolvimento uma GUI Qt6 dedicada à gestão de pacotes. Consulta `rpmdrake/README.md` para detalhes.

## Resolução de problemas

```bash
# Verificar se o serviço D-Bus está a correr
systemctl status urpm-dbus.service

# Verificar o backend PackageKit
pkcon backend-details

# Reiniciar serviços após update
systemctl restart packagekit.service
systemctl restart urpm-dbus.service

# Verificar a interface D-Bus
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1
```

---

# Desenvolvimento e contribuição

## Pré-requisitos

### Portas de firewall

Vê a secção Pré-requisitos para as portas de rede a abrir para a partilha P2P.

### Preparar o ambiente

Clonar o repositório:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

```


### Configuração do modo dev

Cria um ficheiro `.urpm.local` na raiz do projecto para personalizar o modo dev:

```bash
cd /where/is/urpm-ng

# Modo dev (porta 9877, dados de utilizador em ~/var/lib/urpm-dev/)
# Passar para modo dev
touch .urpm.local
```

Nota, podes alterar onde o urpm e o urpmd colocam os dados editando o ficheiro .urpm.local:
```ini
# Directório base personalizado (opcional)
base_dir=/path/lib/urpm-dev
```

Em modo dev, por omissão, os dados são guardados em `/var/lib/urpm-dev/` e o daemon usa a porta 9877.

**Nota que em modo dev o urpmd só interage com outros urpmd em modo dev.**

## Correr o daemon

```bash
# Correr o daemon (como root, sem modo em segundo plano)

cd /where/is/urpm-ng

./bin/urpmd --dev

```

## Correr o urpm

```bash
# Correr o urpm (como root numa consola dedicada)

cd /where/is/urpm-ng

./bin/urpm --help

```

## Programar, testar, contribuir...

Contribuições de todos os tipos são bem-vindas: código, testes, traduções, comentários... nenhuma contribuição é demasiado pequena.

Consulta `CLAUDE.md` para orientações de desenvolvimento e `doc/ARCHITECTURE.md` para a arquitectura técnica.

---

# Problemas conhecidos / TODO

- **Desempenho do `urpm find`** — A pesquisa em files.xml é mais lenta do que urpmf (2.5s vs 0.6s). Precisa de optimização.

---

# Licença

GPL-3.0 — Consulta o ficheiro LICENSE para detalhes.

# Autores

- Maât (Pascal Vilarem)
- Papoteur (Mageia Contributor)
- Claude (Assistente IA)
