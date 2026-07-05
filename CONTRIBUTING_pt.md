# Contribuir para urpm-ng

O urpm-ng é um pequeno projeto voluntário. Um punhado de mantenedores, um grupo minúsculo de testadores regulares, e muito por fazer. Se usa Mageia e algo aqui lhe salta ao olho, agradecíamos a sua ajuda — mesmo um "experimentei, partiu no passo X" de cinco minutos vale mais do que pensa.

Este documento existe para tornar óbvio *como* pode ajudar, seja qual for o seu nível de compromisso. Nada aqui pressupõe que já tenha feito patches a uma ferramenta de distribuição antes.

## Como pode ajudar

Cinco caminhos, do mais leve ao mais pesado. Escolha o que se ajustar ao tempo que tem — nenhum é de segunda categoria.

### 1. Experimente e diga-nos o que se passou

O mais útil que um recém-chegado pode fazer. Instale o urpm-ng na sua máquina (siga a secção *Installation* do [`README.md`](README.md) para as instruções RPM atuais), use-o durante uns dias para o que costuma fazer com o ``urpmi``, e reporte tudo o que o surpreendeu — um crash, uma mensagem errada, uma tradução em falta, um fluxo que soou estranho.

- Onde reportar: **issues do GitHub** em <https://github.com/pvi-github/urpm-ng/issues>.
- Inclua, no mínimo:
  - A versão Mageia (``cat /etc/mageia-release``).
  - A arquitetura (``uname -m``).
  - A versão do urpm-ng (``urpm --version`` — e ``rpm -q urpm-ng-core`` para confirmar que RPM está instalado e se é o do sistema).
  - A linha de comandos exata que se portou mal, o que obteve e o que esperava.
- Não é preciso anexar logs a menos que os peçamos.

### 2. Traduza — ou refine as traduções existentes

Seis línguas já traduzidas (fr / de / es / it / nl / pt). A cobertura é ampla mas não completa: cadeias escapam por traduzir, alguns msgstr soam rígidos, e um ouvido nativo apanha falsos amigos que uma primeira passagem não vê. Se uma dessas é a sua língua materna, uma passagem pelas traduções existentes para afinar a formulação e adotar as expressões idiomáticas locais é muito bem-vinda.

- As cadeias vivem em ficheiros ``.po`` sob [`po/`](po/); abra-os no editor que preferir (poedit serve).
- As entradas vazias ou ``fuzzy`` são cadeias novas ou possivelmente desatualizadas — o ponto mais fácil por onde começar.
- Corra ``msgfmt --check-format po/<lang>.po -o /dev/null`` — se passar, a build também passa.
- O mesmo para a documentação: os canónicos ``README.md`` / ``MIGRATION.md`` / ``CHANGELOG.md`` têm irmãos por língua (``README_fr.md`` etc.); também beneficiariam de uma releitura nativa.

### 3. Melhore a documentação

Páginas de manual, README, folha de migração, changelog — qualquer coisa em prosa. Até uma correção de gralha é útil. As páginas man vivem em ``man/<lang>/man1/urpm.1``; valide com ``groff -man -Tutf8 -ww man/<lang>/man1/urpm.1``.

### 4. Corrija um bug ou acrescente uma pequena funcionalidade

O backlog vive em dois sítios:

- [`TODO.md`](TODO.md) na raiz do repo — a lista visível.
- Os vários ficheiros ``doc/TODO_*.md`` — backlogs temáticos e notas por assunto. Alguns estão prontos para código, outros precisam primeiro de discussão. Pergunte antes de investir um fim-de-semana inteiro.

Continue a ler para o fluxo de build / teste / patch.

### 5. Junte-se à canalização

Refactorizações, trabalho no resolvedor, jobs de fundo do ``urpmd``, trabalho em spec-files, endurecimento do mkimage / contentores de build. É aqui que vive o roadmap técnico do projeto. Diga olá primeiro — coordenar-se evita pisar os pés uns aos outros, ou que lhe pisem os seus.

## Obter as fontes e construir

Dois caminhos de build. O **simples** usa ``bm`` (o wrapper ``build-mageia``) na sua máquina e só precisa de ``urpmi``. O **reprodutível** usa ``urpm build`` dentro de um contentor e requer que o urpm-ng já esteja instalado.

### Dependências de arranque (uma só vez)

Numa Mageia acabada de instalar, o ``urpmi`` está disponível mas o ``sudo`` pode não estar configurado — a forma clássica ``su -c`` funciona em todo o lado:

```sh
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# A ferramenta de build (bm) mais cada BuildRequires que o spec
# declara. --buildrequires lê o spec diretamente, portanto a lista
# fica sincronizada automaticamente. O próprio bm não está nos
# BuildRequires do spec (invoca o rpmbuild em vez de ser consumido
# por %build), daí os dois comandos.
su -c "urpmi bm && urpmi --buildrequires rpmbuild/SPECS/urpm-ng.spec"
```

### Caminho simples — ``bm`` na máquina

```sh
make rpm-all
```

Depois instale os RPMs acabados de construir.

**Primeira vez — ainda sem urpm-ng no sistema** — passe todos os RPMs ao ``urpmi`` de uma vez (o filtro versão-release evita apanhar uma build antiga que continue em ``RPMS/``):

```sh
RPMS=$(find rpmbuild/RPMS rpmdrake/rpmbuild/RPMS \
            -name "*-$(cat VERSION)-$(cat RELEASE).*.rpm")
su -c "urpmi $RPMS"
```

**Iterações seguintes** — o resolvedor do urpm-ng varre automaticamente o diretório irmão à procura de RPMs locais (reporta "Found N sibling RPMs (available for dependencies)"), pelo que basta apontar para os dois meta-pacotes:

```sh
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Caminho reprodutível — build em contentor

Só utilizável a partir do momento em que o urpm-ng está instalado na máquina (ovo-e-galinha na primeira instalação).

```sh
# Uma só vez: criar a imagem de build (exemplo mga10 em x86_64)
su -c "urpm image make --release 10 --tag mga10-64"

# A cada build seguinte — ambos os specs (urpm-ng e rpmdrake-ng)
urpm build --image mga10-64 rpmbuild/SPECS/urpm-ng.spec \
                            rpmdrake/rpmbuild/SPECS/rpmdrake-ng.spec

# Instalar — o urpm-ng já está na máquina (pré-requisito deste
# caminho), pelo que ``urpm i`` sobre os dois meta chega: o
# resolvedor apanha automaticamente os RPMs irmãos do mesmo
# diretório.
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Correr os testes

```sh
pytest urpm/tests/
```

Ver [`doc/TESTING.md`](doc/TESTING.md) para uma folha rápida de pytest e as lacunas de cobertura conhecidas.

Para iterar em modo dev sem reconstruir um RPM a cada vez, os ficheiros de código correm diretamente do checkout — ``python -m urpm.cli.main <subcomando>`` funciona com um ``$PYTHONPATH`` que inclua a raiz do checkout.

## O seu primeiro contributo — a volta completa

1. **Branch.** A partir do branch da versão ativa (atualmente ``0.8.x`` — em caso de dúvida veja o ficheiro ``VERSION`` na raiz do repo). O ``main`` só transporta a história publicada; trabalho novo nunca aterra lá diretamente, chega por fast-forward-merge desde o branch de versão no momento do release.
2. **Altere.** Escreva o fix ou a funcionalidade. Se está a mexer no resolvedor, na fila de transações ou no ``urpmd``, adicionar um teste em ``urpm/tests/`` é quase obrigatório. Para trabalho de CLI ou documentação, um teste manual na sua máquina chega.
3. **Teste localmente.** Corra ``pytest urpm/tests/`` (suite completa para tudo o que é user-visible, ficheiro dirigido caso contrário). Corrija qualquer regressão antes de prosseguir.
4. **Atualize a superfície visível** se a sua alteração for user-facing (um fix num caminho interno raramente precisa disto):
   - acrescente uma entrada em [`CHANGELOG.md`](CHANGELOG.md) sob o título da próxima versão;
   - atualize os catálogos ``.po`` (qualquer nova cadeia inglesa user-facing é um novo msgid);
   - atualize ``man/<lang>/man1/urpm.1`` se um flag foi acrescentado, renomeado ou retirado;
   - atualize o README / a folha MIGRATION se a alteração afeta os comandos do dia-a-dia.
5. **Commit.** Assunto curto (~50 caracteres), prefixo convencional (``fix(zona):``, ``feat(zona):``, ``docs:``, ``chore:``, ``test:``, ``refactor:``). O corpo explica o *porquê* — o diff já mostra o *o quê*.

Antes de abrir uma pull request, passe por esta checklist:

- [ ] ``make rpm-all`` (ou a build em contentor) corre com sucesso.
- [ ] ``pytest urpm/tests/`` passa sem regressões.
- [ ] Instalou os seus **RPMs construídos localmente** e testou a partir dessa cópia instalada (suba a linha ``release`` em ``rpmbuild/SPECS/urpm-ng.spec`` localmente para que o número de RPM seja superior ao do sistema e se instale limpamente por cima — só uma conveniência local, nunca commitar esse aumento).
- [ ] Os comandos smoke óbvios continuam a funcionar sobre a build instalada, sem que a sua alteração parta nenhum deles:
  - ``urpm i <umpacote>`` — caminho de instalação
  - ``urpm q <umpacote>`` — query
  - ``urpm e <umpacote>`` — erase
  - ``urpm f /caminho/para/ficheiro`` — find
  - ``urpm m u`` — media update
  - ``urpm u`` — upgrade do sistema
- [ ] O seu branch está **rebased** sobre o branch de destino (sem merge commits entre o seu trabalho e a ponta).
- [ ] Docs / páginas man / traduções atualizadas conforme o passo 4.

6. **Push** para o seu fork ou o seu branch.
7. **Abra uma pull request** no GitHub. Descreva a intenção, a cobertura de testes e qualquer limitação conhecida. Mencione a linha de release visada e confirme a checklist acima.
8. **Iterar sobre a revisão.** Um revisor olhará para o seu diff e fará perguntas ou sugerirá ajustes. Visamos uma troca entre pares — nada pessoal, tudo sobre o código.

## Onde nos encontrar

- **Issues & PRs**: <https://github.com/pvi-github/urpm-ng>
- **Contacto direto — Matrix**: [@maat_:matrix.org](https://matrix.to/#/@maat_:matrix.org)

## Onde vive o código

```
urpm/                  # Fontes Python
  cli/                 # Interface de linha de comandos (urpm, subcomandos)
  core/                # Resolvedor, download, install, BD, sync
  daemon/              # urpmd (serviço em segundo plano, P2P LAN)
  genmedia/            # Geração de metadados do lado servidor
  tests/               # Todos os testes vivem aqui (não num tests/ raiz)
rpmdrake/              # Frontend GUI Qt6 (rpmdrake-ng)
pk-backend-urpm/       # Plugin em C: backend PackageKit sobre urpm-ng
man/<lang>/man1/       # Páginas man traduzidas
po/                    # Catálogos de tradução (.po)
doc/                   # Docs de desenho, planos, TODOs, specs
rpmbuild/SPECS/        # Empacotamento Mageia (.spec)
data/                  # Units systemd, regras polkit, modelos de config
```

Para um mapa mais profundo, ver [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md). Para o catálogo cumulativo de funcionalidades, [`FEATURES.md`](FEATURES.md).

## Expectativas de estilo (curto)

- **Inglês** no código, nos comentários e nas mensagens de commit. Um histórico multilingue desorienta.
- **Docstrings** em qualquer função ou classe pública. Uma linha basta; explique o *porquê* apenas quando não é óbvio a partir do nome.
- **Testes** quando prático — a suite é uma rede anti-regressão, não uma prova formal. Alterações user-visible devem trazer pelo menos uma nota de teste manual.
- **Comentários** onde o código esconde uma surpresa (contorno, race, invariante). Nunca um comentário que duplique o código.

## Ciclo de release

O trabalho passa por um branch de versão (``0.8.x``, ``0.9.x``, …). Quando uma versão está pronta, o branch é fast-forward-mergido em ``main``; ``main`` transporta então o histórico publicado. As tags são cortadas de ``main`` nesse momento e os RPMs são publicados no canal binário do projeto.

Os aumentos de versão em ``VERSION`` / ``pyproject.toml`` / ``rpmbuild/SPECS/urpm-ng.spec`` são responsabilidade do mantenedor — não faça commit de um aumento no seu contributo. Dito isto, sinta-se à vontade para subir **localmente** a linha ``release`` do spec para que o seu RPM construído se instale por cima do do sistema; apenas não faça stage dessa linha.
