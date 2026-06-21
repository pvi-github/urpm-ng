# Contribuir para o urpm-ng

Obrigado por dedicar tempo à leitura deste documento.  O urpm-ng
pretende tornar-se o substituto em Python da histórica cadeia de
ferramentas `urpmi` da Mageia, e qualquer melhoria — fix,
funcionalidade, documentação, tradução, teste — é bem-vinda.

Este projecto é levado a cabo por uma comunidade de voluntários.  O
tom das nossas revisões e discussões é *entre pares*: apontamos para
o código, não para as pessoas; formulamos sugestões, não ordens;
fazemos perguntas honestas em vez de retóricas.

> As traduções deste documento estão disponíveis em
> [`CONTRIBUTING.md`](CONTRIBUTING.md) (inglês, versão de referência),
> [`CONTRIBUTING_de.md`](CONTRIBUTING_de.md),
> [`CONTRIBUTING_es.md`](CONTRIBUTING_es.md),
> [`CONTRIBUTING_fr.md`](CONTRIBUTING_fr.md),
> [`CONTRIBUTING_it.md`](CONTRIBUTING_it.md) e
> [`CONTRIBUTING_nl.md`](CONTRIBUTING_nl.md).

## Objectivos do projecto

- Código **legível**: nomes explícitos, funções curtas,
  responsabilidades claras.
- **Documentado**: docstrings em toda a API pública, comentários
  onde o *porquê* não é evidente.
- **Fácil de adoptar**: um novo contribuidor deve conseguir
  compreender um módulo sem ter de ler todo o projecto primeiro.
- **Coerente**: convenções uniformes em todo o código.
- **Performante**: qualidade e desempenho não são incompatíveis —
  procuramos ambos.

## Preparação do ambiente

Requisitos:

- Python 3.13 ou mais recente
- `python3-solv`, `python3-zstandard`, `python3-rpm`, `python3-curl`,
  `python3-pyyaml`
- `rpmbuild` (para as fixtures de teste e a pipeline de build)

Um checkout funcional:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng
urpmi python3-solv python3-zstandard
pytest urpm/tests/
```

Ver [`doc/HOWTO_TESTS.md`](doc/HOWTO_TESTS.md) para o resumo de
referência do pytest, e [`doc/TESTING.md`](doc/TESTING.md) para o
estado da cobertura e as lacunas conhecidas.

## Fluxo de trabalho

1. Ramificar a partir de `main` (ou da branca de release activa se
   existir — por exemplo, `0.7.x` esteve activa até à 0.7.15,
   `0.8.x` é a que está a ser preparada no momento em que se
   escreve).
2. Implementar a alteração, com testes sempre que possível.
3. Executar os testes pertinentes localmente.  Os testes manuais
   continuam a ser necessários para tudo o que seja visível ao
   utilizador — a suite é uma rede contra regressões, não uma
   garantia funcional.
4. Abrir uma pull request a descrever o que a alteração faz *e
   porquê*.

## Convenções de commit

- **Língua**: inglês.  As mensagens em várias línguas baralham o
  histórico.
- **Linha de assunto**: ≤ 50 caracteres, prefixo conventional-commit
  (`fix(...)`, `feat(...)`, `docs(...)`, `chore(...)`, `test(...)`).
- **Corpo**: explicar o *porquê*, não apenas o *quê*.  O diff já
  mostra o quê.
- **Sem atribuição a IA**: o hook de commit rejeita linhas
  `Co-Authored-By` que nomeiem assistentes de IA.  O autor do commit
  é o ser humano que escreveu `git commit`.

## Regras de higiene git

- Nunca executar `git add .` nem `git add -A`.  Adicionar os
  ficheiros um a um — evita comprometer-se acidentalmente com um
  `.env`, um rascunho ou um artefacto gerado.
- Executar sempre `git status` antes de submeter, para que a lista
  dos ficheiros em staging fique visível.
- Nunca submeter sem uma confirmação explícita, do revisor ou de ti
  mesmo depois de um `git status`.
- Nunca saltar os hooks de commit.  Se um hook protestar, corrige a
  causa de fundo em vez de recorrer a `--no-verify`.

## O que não se compromete

- **Rascunhos** na raiz do repositório ou em `doc/`.  Os rascunhos
  vivos (`RELEASE_NOTE.md`, `doc/TODO_*.md` em curso, `SPEC_*.md`
  antes de validados) ficam no teu checkout local e só são
  promovidos ao worktree quando validados.
- **Artefactos de teste locais**: `essais/` está no gitignore —
  coloca aí os scripts transitórios, as directorias de trabalho de
  smoke test, os resultados de `mktemp`.  Nunca na raiz do
  repositório.
- **Tudo o que tenha nome `bin/rebuild.sh` ou `bin/recup.sh`** —
  scripts pessoais de contribuidores que não têm nada que fazer no
  upstream.
- **Vocabulário do DNF**: o urpm-ng dirige-se ao ecossistema da
  Mageia.  Comparar com o DNF numa nota de rodapé é aceitável, mas
  evitar tomar emprestados os nomes de comandos do DNF
  (`skip-broken`, `distro-sync`, ...) para as nossas próprias
  opções.
- **Ficheiros gerados**: catálogos `.mo`, `__pycache__/`, `*.pyc`,
  builds do Sphinx — todos gitignorados, deixa-os assim.

## Convenções de documentação

- **Línguas**: código, comentários e mensagens de commit em inglês.
  A documentação para o utilizador em `doc/` está numa mistura de
  francês e inglês (status quo); as cadeias visíveis para o
  utilizador passam por `po/` e as páginas man vivem em
  `man/<lang>/`.
- **Tom**: pedagógico e útil.  Um relatório deve ser legível por
  alguém que não tenha acompanhado a conversa.  Evita tabelas
  telegráficas com códigos crípticos (C1/N7), a não ser que cada
  célula seja explicada.
- **A extensão não é uma virtude**, mas a brevidade à custa da
  clareza também não.  Cada parágrafo deve oferecer ao leitor algo
  que ele não conseguiria deduzir sozinho.

## Testes

- Os testes vivem em `urpm/tests/`, **não** numa directoria
  `tests/` na raiz.  Sintetiza os RPM com
  `urpm/tests/gen_test_rpms.py` e executa o pytest a partir de
  `urpm/tests/` (a infraestrutura de testes exige esta directoria
  de trabalho para alguns testes de integração).
- As directorias temporárias por teste são agora limpas por uma
  fixture autouse em `BaseUrpmiTest`, pelo que um teste que falhe
  antes da sua limpeza explícita já não deixa entradas em `/tmp`.
- Para alterações às páginas man, valida com
  `groff -man -Tutf8 -ww man/<lang>/man1/urpm.1` — os avisos UTF-8
  pré-existentes não são bloqueantes, os erros novos são.

## Revisão de código

Revemo-nos mutuamente como pares.  Quando recebes uma revisão:

- O revisor está a apontar para o código, não para ti.
- «Porque é que fizeste X?» é uma pergunta autêntica; responde com
  honestidade em vez de te defenderes.
- «Podíamos fazer Y em vez disso?» é uma proposta, não um veredicto.

Quando dás uma revisão:

- A autodepreciação é aceitável, a prescrição não.  Sugere, não
  ditas.
- Pergunta antes de presumir.  «Talvez me esteja a escapar alguma
  coisa, mas ...» é uma boa abertura.
- Marca os blockers com clareza; os nitpicks de estilo continuam a
  ser nitpicks de estilo.

## Releases

O trabalho de release decorre numa branca de versão (`0.7.x`,
`0.8.x`, ...) e é incorporado em fast-forward em `main` no momento
da release.  `main` carrega o histórico das releases; a branca
activa carrega o trabalho em curso.

`urpm/__init__.py` e `pyproject.toml` são incrementados em conjunto
quando uma release é etiquetada.  Nunca decidas um número de versão
unilateralmente — pergunta ao responsável do projecto.

## Onde vivem as coisas

```
urpm/                  # Fonte
  cli/                 # Interface de linha de comandos
  core/                # Resolver, base de dados, download, install...
  daemon/              # urpmd
  genmedia/            # Geração do lado servidor (urpm genmedia)
  tests/               # TODOS os testes vivem aqui, NÃO em /tests/
rpmdrake/              # Front-end gráfico
man/<lang>/man1/       # Páginas man traduzidas
po/                    # Catálogos de tradução (.po por língua)
doc/                   # Documentos de design, planos, ficheiros TODO
rpmbuild/SPECS/        # Ficheiros .spec de packaging Mageia
data/                  # Unidades systemd, regras polkit, etc.
```

Para o catálogo cumulativo das funcionalidades que o urpm-ng
oferece, ver [`FEATURES.md`](FEATURES.md).  Para o histórico por
release, ver [`CHANGELOG.md`](CHANGELOG.md).  Para os backlogs, ver
[`TODO.md`](TODO.md) e os ficheiros específicos por tópico em
[`doc/TODO_*.md`](doc/).
