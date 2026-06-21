# Bijdragen aan urpm-ng

Bedankt dat je de tijd neemt om dit document te lezen.  urpm-ng wil de
Python-vervanger worden van de historische `urpmi`-gereedschapsketen
van Mageia, en elke verbetering — fix, functie, documentatie,
vertaling, test — is welkom.

Dit project wordt gedragen door een gemeenschap van vrijwilligers.  De
toon van onze reviews en discussies is *van gelijke tot gelijke*: we
wijzen naar de code, niet naar de personen; we formuleren
suggesties, geen bevelen; we stellen eerlijke vragen in plaats van
retorische.

> Vertalingen van dit document zijn beschikbaar in
> [`CONTRIBUTING.md`](CONTRIBUTING.md) (Engels, referentieversie),
> [`CONTRIBUTING_de.md`](CONTRIBUTING_de.md),
> [`CONTRIBUTING_es.md`](CONTRIBUTING_es.md),
> [`CONTRIBUTING_fr.md`](CONTRIBUTING_fr.md),
> [`CONTRIBUTING_it.md`](CONTRIBUTING_it.md) en
> [`CONTRIBUTING_pt.md`](CONTRIBUTING_pt.md).

## Doelstellingen van het project

- **Leesbare** code: sprekende namen, korte functies, duidelijke
  verantwoordelijkheden.
- **Gedocumenteerd**: docstrings op de volledige publieke API,
  commentaar daar waar het *waarom* niet voor de hand ligt.
- **Makkelijk om in te stappen**: een nieuwe bijdrager moet een
  module kunnen begrijpen zonder eerst het hele project te moeten
  doornemen.
- **Consistent**: uniforme conventies in heel de codebase.
- **Performant**: kwaliteit en performantie sluiten elkaar niet uit —
  we mikken op beide.

## Opzetten

Voorwaarden:

- Python 3.13 of recenter
- `python3-solv`, `python3-zstandard`, `python3-rpm`, `python3-curl`,
  `python3-pyyaml`
- `rpmbuild` (voor de test-fixtures en de build-pipeline)

Een werkende checkout:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng
urpmi python3-solv python3-zstandard
pytest urpm/tests/
```

Zie [`doc/HOWTO_TESTS.md`](doc/HOWTO_TESTS.md) voor de pytest-spiekbrief
en [`doc/TESTING.md`](doc/TESTING.md) voor de stand van de dekking en
de bekende hiaten.

## Werkstroom

1. Vertak vanaf `main` (of vanaf de actieve releasebranch indien die
   bestaat — `0.7.x` was bijvoorbeeld actief tot release 0.7.15,
   `0.8.x` is degene die op het moment van schrijven wordt
   voorbereid).
2. Schrijf de wijziging, met tests waar mogelijk.
3. Voer de relevante tests lokaal uit.  Handmatig testen blijft nodig
   voor alles wat voor de gebruiker zichtbaar is — de suite is een
   vangnet tegen regressies, geen functionele garantie.
4. Open een pull request die beschrijft wat de wijziging doet *en
   waarom*.

## Commit-conventies

- **Taal**: Engels.  Gemengde commit-berichten maken de geschiedenis
  troebel.
- **Onderwerpregel**: ≤ 50 tekens, conventional-commit-prefix
  (`fix(...)`, `feat(...)`, `docs(...)`, `chore(...)`, `test(...)`).
- **Tekst**: het *waarom* uitleggen, niet alleen het *wat*.  De diff
  toont het wat al.
- **Geen AI-attributie**: de commit-hook weigert `Co-Authored-By`-
  regels die AI-assistenten vermelden.  De auteur van de commit is de
  mens die `git commit` heeft uitgevoerd.

## Regels van git-hygiëne

- Voer nooit `git add .` of `git add -A` uit.  Voeg bestanden één voor
  één toe — zo vermijd je dat je per ongeluk een `.env`, een klad of
  een gegenereerd artefact mee-committeert.
- Voer altijd `git status` uit vóór de commit, zodat de lijst van
  ge-stagede bestanden zichtbaar is.
- Committeer nooit zonder uitdrukkelijke bevestiging, van de reviewer
  of van jezelf na een `git status`-controle.
- Sla de commit-hooks nooit over.  Als een hook klaagt, los dan de
  onderliggende oorzaak op in plaats van `--no-verify` te gebruiken.

## Wat niet gecommit wordt

- **Kladversies** in de hoofdmap van het repo of onder `doc/`.
  Levende kladversies (`RELEASE_NOTE.md`, lopende `doc/TODO_*.md`,
  `SPEC_*.md` vóór validatie) blijven in je lokale checkout en worden
  pas na validatie naar de worktree gepromoveerd.
- **Lokale test-artefacten**: `essais/` staat in gitignore — zet daar
  je tijdelijke scripts, je werkmappen voor smoke tests en de uitvoer
  van `mktemp`.  Nooit in de hoofdmap van het repo.
- **Alles dat `bin/rebuild.sh` of `bin/recup.sh` heet** — persoonlijke
  scripts van bijdragers die niets bij upstream te zoeken hebben.
- **DNF-woordenschat**: urpm-ng richt zich op het Mageia-ecosysteem.
  Een vergelijking met DNF in een voetnoot is prima, maar het lenen
  van DNF-commandonamen (`skip-broken`, `distro-sync`, ...) voor onze
  eigen opties moet vermeden worden.
- **Gegenereerde bestanden**: `.mo`-catalogi, `__pycache__/`, `*.pyc`,
  Sphinx-builds — allemaal in gitignore, laat dat zo.

## Documentatieconventies

- **Talen**: code, commentaar en commit-berichten in het Engels.  De
  gebruikersdocumentatie onder `doc/` is een Frans/Engels mengsel
  (status quo); door de gebruiker geziene strings lopen via `po/`, en
  de man-pagina's wonen in `man/<lang>/`.
- **Toon**: didactisch en bruikbaar.  Een rapport moet leesbaar zijn
  voor iemand die de conversatie niet heeft gevolgd.  Vermijd
  telegram-stijl tabellen met cryptische codes (C1/N7), tenzij elke
  cel wordt toegelicht.
- **Lengte is geen deugd**, maar bondigheid ten koste van
  duidelijkheid evenmin.  Elke alinea moet de lezer iets bieden dat
  hij niet zelf kon afleiden.

## Tests

- De tests wonen in `urpm/tests/`, **niet** in een `tests/`-map op
  hoofdniveau.  Synthetiseer de RPM's met
  `urpm/tests/gen_test_rpms.py` en draai pytest vanuit
  `urpm/tests/` (de testinfrastructuur verwacht deze werkmap voor
  bepaalde integratietests).
- De tijdelijke mappen per test worden tegenwoordig opgeruimd door
  een autouse-fixture op `BaseUrpmiTest`, waardoor een test die
  faalt vóór de expliciete opruiming geen `/tmp`-resten meer
  achterlaat.
- Voor wijzigingen aan man-pagina's, valideer met
  `groff -man -Tutf8 -ww man/<lang>/man1/urpm.1` — reeds bestaande
  UTF-8-waarschuwingen zijn niet blokkerend, nieuwe fouten wel.

## Code-review

We reviewen elkaar als gelijken.  Wanneer je een review ontvangt:

- De reviewer wijst naar de code, niet naar jou.
- "Waarom heb je X gedaan?" is een echte vraag; antwoord eerlijk in
  plaats van je te verdedigen.
- "Zouden we in plaats daarvan Y kunnen doen?" is een voorstel, geen
  vonnis.

Wanneer je een review geeft:

- Zelfrelativering mag, voorschriften niet.  Stel voor, schrijf niet
  voor.
- Vraag voor je veronderstelt.  "Misschien zie ik iets over het hoofd,
  maar ..." is een goede opener.
- Markeer blockers duidelijk; stijl-nitpicks blijven stijl-nitpicks.

## Releases

Het releasewerk gebeurt op een versie-branch (`0.7.x`, `0.8.x`, ...)
en wordt op het releasemoment fast-forward gemerged naar `main`.
`main` draagt de geschiedenis van de releases; de actieve branch
draagt het werk in uitvoering.

`urpm/__init__.py` en `pyproject.toml` worden samen opgehoogd
wanneer een release een tag krijgt.  Beslis nooit op eigen houtje
over een versienummer — vraag het aan de projecteigenaar.

## Waar de dingen wonen

```
urpm/                  # Broncode
  cli/                 # Commandoregel-interface
  core/                # Resolver, database, download, install...
  daemon/              # urpmd
  genmedia/            # Mediagenerator aan serverzijde (urpm genmedia)
  tests/               # ALLE tests wonen hier, NIET in /tests/
rpmdrake/              # Grafische front-end
man/<lang>/man1/       # Vertaalde man-pagina's
po/                    # Vertalingscatalogi (.po per taal)
doc/                   # Designdocumenten, plannen, TODO-bestanden
rpmbuild/SPECS/        # .spec-bestanden voor Mageia-packaging
data/                  # systemd-units, polkit-regels enz.
```

Voor de cumulatieve catalogus van functies die urpm-ng biedt, zie
[`FEATURES.md`](FEATURES.md).  Voor de geschiedenis per release, zie
[`CHANGELOG.md`](CHANGELOG.md).  Voor de backlogs, zie
[`TODO.md`](TODO.md) en de onderwerp-specifieke bestanden onder
[`doc/TODO_*.md`](doc/).
