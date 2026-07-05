# Bijdragen aan urpm-ng

urpm-ng is een klein vrijwilligersproject. Een handvol onderhouders, een klein groepje vaste testers, en veel te doen. Als je Mageia gebruikt en iets hier je oog vangt, zouden we je hulp waarderen — zelfs een vijf-minuten "geprobeerd, brak op stap X" is meer waard dan je denkt.

Dit document is er om duidelijk te maken *hoe* je kunt helpen, welk niveau van betrokkenheid je ook hebt. Niets hier veronderstelt dat je al eerder een distributietool hebt gepatcht.

## Hoe je kunt helpen

Vijf paden, van het lichtste tot het zwaarste. Kies dat wat bij je beschikbare tijd past — geen enkel is tweederangs.

### 1. Probeer het en vertel ons wat er gebeurt

Het meest nuttige dat een nieuwkomer kan doen. Installeer urpm-ng op je machine (volg het onderdeel *Installation* van [`README.md`](README.md) voor de huidige RPM-instructies), gebruik het een paar dagen voor wat je normaal met ``urpmi`` doet, en meld alles dat je verrast heeft — een crash, een verkeerde melding, een ontbrekende vertaling, een workflow die stroef aanvoelde.

- Waar melden: **GitHub issues** op <https://github.com/pvi-github/urpm-ng/issues>.
- Vermeld ten minste:
  - De Mageia-versie (``cat /etc/mageia-release``).
  - De architectuur (``uname -m``).
  - De urpm-ng-versie (``urpm --version`` — en ``rpm -q urpm-ng-core`` om te bevestigen welke RPM is geïnstalleerd en of het de systeem-RPM is).
  - De exacte commandolijn die verkeerd ging, wat je kreeg en wat je verwachtte.
- Geen logs meesturen tenzij we erom vragen.

### 2. Vertaal — of poets bestaande vertalingen bij

Zes talen zijn vertaald (fr / de / es / it / nl / pt). De dekking is breed maar niet volledig: strings glippen onvertaald door, sommige msgstrs klinken stroef, en een moedertaalspreker vangt valse vrienden die een eerste doorloop niet ziet. Als een van die talen je moedertaal is, is een doorloop over de bestaande vertalingen om de formulering te verfijnen en lokale idiomatische wendingen over te nemen zeer welkom.

- De strings leven in ``.po``-bestanden onder [`po/`](po/); open ze in je favoriete editor (poedit werkt prima).
- Lege of ``fuzzy``-ingangen zijn nieuwe of mogelijk verouderde strings — de gemakkelijkste plek om te beginnen.
- Draai ``msgfmt --check-format po/<lang>.po -o /dev/null`` — als dat lukt, lukt de build ook.
- Idem voor de docs: de canonieke ``README.md`` / ``MIGRATION.md`` / ``CHANGELOG.md`` hebben taal-broers en -zussen (``README_fr.md`` enz.); ook die zouden baat hebben bij een moedertaal-herlezing.

### 3. Verbeter de documentatie

Man-pagina's, README, migratie-spiekbrief, changelog — alles wat proza is. Zelfs een typfoutcorrectie helpt. Man-pagina's leven in ``man/<lang>/man1/urpm.1``; valideer met ``groff -man -Tutf8 -ww man/<lang>/man1/urpm.1``.

### 4. Los een bug op of voeg een klein feature toe

De backlog leeft op twee plekken:

- [`TODO.md`](TODO.md) in de repo-root — de zichtbare lijst.
- De diverse ``doc/TODO_*.md``-bestanden — thematische backlogs en per-onderwerp notities. Sommige zijn klaar om te coderen, andere hebben eerst discussie nodig. Vraag voor je een heel weekend investeert.

Lees verder voor de build- / test- / patch-flow.

### 5. Duik in het loodgieterswerk

Refactors, resolver-werk, ``urpmd``-achtergrondjobs, spec-file werk, mkimage- / build-container-hardening. Hier leeft de technische roadmap van het project. Zeg eerst hallo — coördineren voorkomt dat je op elkaars tenen trapt, of dat er op de jouwe wordt getrapt.

## De bronnen ophalen en bouwen

Twee build-paden. Het **eenvoudige** gebruikt ``bm`` (de ``build-mageia``-wrapper) op je host en heeft alleen ``urpmi`` nodig. Het **reproduceerbare** gebruikt ``urpm build`` binnen een container en vereist dat urpm-ng al is geïnstalleerd.

### Bootstrap-afhankelijkheden (eenmalig)

Op een verse Mageia-machine is ``urpmi`` aanwezig maar ``sudo`` misschien niet geconfigureerd — de klassieke ``su -c``-vorm werkt overal:

```sh
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# De buildtool (bm) plus elke BuildRequires die de spec declareert.
# --buildrequires leest de spec rechtstreeks, dus de lijst blijft
# automatisch synchroon. bm zelf zit niet in de BuildRequires van de
# spec (het roept rpmbuild aan i.p.v. door %build te worden verbruikt),
# vandaar de twee commando's.
su -c "urpmi bm && urpmi --buildrequires rpmbuild/SPECS/urpm-ng.spec"
```

### Eenvoudig pad — ``bm`` op de host

```sh
make rpm-all
```

Installeer daarna de zojuist gebouwde RPMs.

**Eerste keer — nog geen urpm-ng op het systeem** — geef alle RPMs in één keer aan ``urpmi`` (het versie-release-filter voorkomt dat een oudere build die nog in ``RPMS/`` staat wordt meegepakt):

```sh
RPMS=$(find rpmbuild/RPMS rpmdrake/rpmbuild/RPMS \
            -name "*-$(cat VERSION)-$(cat RELEASE).*.rpm")
su -c "urpmi $RPMS"
```

**Volgende iteraties** — de resolver van urpm-ng scant automatisch de broer/zus-map naar lokale RPMs (rapporteert "Found N sibling RPMs (available for dependencies)"), dus is het genoeg om naar de twee meta-pakketten te wijzen:

```sh
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Reproduceerbaar pad — container-build

Alleen bruikbaar zodra urpm-ng op de host is geïnstalleerd (kip-en-ei bij een allereerste installatie).

```sh
# Eenmalig: het build-image aanmaken (voorbeeld mga10 op x86_64)
su -c "urpm image make --release 10 --tag mga10-64"

# Bij elke volgende build — beide specs (urpm-ng en rpmdrake-ng)
urpm build --image mga10-64 rpmbuild/SPECS/urpm-ng.spec \
                            rpmdrake/rpmbuild/SPECS/rpmdrake-ng.spec

# Installeren — urpm-ng staat al op de host (voorwaarde van dit pad),
# dus is ``urpm i`` op de twee meta genoeg: de resolver haalt de
# broer/zus-RPMs uit dezelfde map automatisch op.
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### De tests draaien

```sh
pytest urpm/tests/
```

Zie [`doc/TESTING.md`](doc/TESTING.md) voor een pytest-spiekbrief en bekende dekkingsgaten.

Om in dev-modus te itereren zonder telkens een RPM te herbouwen, draaien de bronbestanden rechtstreeks vanuit de checkout — ``python -m urpm.cli.main <subcommando>`` werkt met een ``$PYTHONPATH`` die de checkout-root bevat.

## Je eerste bijdrage — de volledige rondgang

1. **Branch.** Vanaf de actieve versie-branch (momenteel ``0.8.x`` — kijk bij twijfel in het ``VERSION``-bestand aan de repo-root). ``main`` draagt alleen de uitgebrachte historie; nieuw werk landt daar nooit direct, het komt er via fast-forward-merge vanuit de versie-branch op het moment van release.
2. **Wijzig.** Schrijf de fix of het feature. Als je aan de resolver, de transactie-queue of ``urpmd`` komt, is een test in ``urpm/tests/`` toevoegen bijna verplicht. Voor CLI- of doc-werk is handmatig testen op je eigen machine genoeg.
3. **Test lokaal.** Draai ``pytest urpm/tests/`` (volledige suite voor alles user-visible, gerichte file anders). Repareer elke regressie voor je doorgaat.
4. **Werk het zichtbare oppervlak bij** als je wijziging user-facing is (een fix op een interne codepad heeft dit zelden nodig):
   - voeg een regel toe in [`CHANGELOG.md`](CHANGELOG.md) onder het kopje van de volgende versie;
   - werk de ``.po``-catalogi bij (elke nieuwe user-facing Engelse string is een nieuwe msgid);
   - werk ``man/<lang>/man1/urpm.1`` bij als een flag is toegevoegd, hernoemd of verwijderd;
   - werk README / MIGRATION-spiekbrief bij als de wijziging alledaagse commando's raakt.
5. **Commit.** Kort onderwerp (~50 tekens), conventional-prefix (``fix(gebied):``, ``feat(gebied):``, ``docs:``, ``chore:``, ``test:``, ``refactor:``). Het body legt het *waarom* uit — het *wat* toont de diff al.

Loop deze checklist door voor je een pull request opent:

- [ ] ``make rpm-all`` (of de container-build) slaagt.
- [ ] ``pytest urpm/tests/`` slaagt zonder regressies.
- [ ] Je hebt **je lokaal gebouwde RPMs geïnstalleerd** en getest vanuit die geïnstalleerde kopie (verhoog de ``release``-regel in ``rpmbuild/SPECS/urpm-ng.spec`` lokaal zodat het RPM-nummer hoger is dan dat van het systeem en er netjes overheen installeert — alleen lokale gemak, deze bump nooit committen).
- [ ] De voor de hand liggende smoke-commando's werken nog op de geïnstalleerde build, zonder dat je wijziging er een breekt:
  - ``urpm i <eenpakket>`` — installatiepad
  - ``urpm q <eenpakket>`` — query
  - ``urpm e <eenpakket>`` — erase
  - ``urpm f /pad/naar/bestand`` — find
  - ``urpm m u`` — media update
  - ``urpm u`` — systeem-upgrade
- [ ] Je branch is **rebased** op de doel-branch (geen merge commits tussen je werk en de tip).
- [ ] Docs / man-pagina's / vertalingen bijgewerkt zoals in stap 4.

6. **Push** naar je fork of je branch.
7. **Open een pull request** op GitHub. Beschrijf de bedoeling, de testdekking en elke bekende beperking. Vermeld de release-lijn die je richt en bevestig de checklist hierboven.
8. **Itereer op review.** Een reviewer bekijkt je diff en stelt vragen of doet suggesties. We mikken op peer-uitwisseling — niets persoonlijks, alles op de code.

## Waar ons te bereiken

- **Issues & PRs**: <https://github.com/pvi-github/urpm-ng>
- **Direct contact — Matrix**: [@maat_:matrix.org](https://matrix.to/#/@maat_:matrix.org)

## Waar de code woont

```
urpm/                  # Python-broncode
  cli/                 # Commandolijn-interface (urpm, subcommando's)
  core/                # Resolver, download, install, database, sync
  daemon/              # urpmd (achtergronddienst, LAN-P2P)
  genmedia/            # Server-side media-metadatageneratie
  tests/               # Alle tests wonen hier (niet in een top-level tests/)
rpmdrake/              # Qt6 GUI-frontend (rpmdrake-ng)
pk-backend-urpm/       # C-plugin: PackageKit-backend op urpm-ng
man/<lang>/man1/       # Vertaalde man-pagina's
po/                    # Vertaalcatalogi (.po)
doc/                   # Ontwerp-docs, plannen, TODOs, specs
rpmbuild/SPECS/        # Mageia-packaging (.spec)
data/                  # systemd-units, polkit-regels, config-sjablonen
```

Voor een diepere kaart, zie [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md). Voor de cumulatieve featurecatalogus [`FEATURES.md`](FEATURES.md).

## Stijlverwachtingen (kort)

- **Engels** in code, commentaar en commit-berichten. Een meertalige historie is verwarrend.
- **Docstrings** op elke publieke functie of klasse. Een one-liner volstaat; leg het *waarom* alleen uit wanneer het niet voor de hand ligt uit de naam.
- **Tests** waar praktisch — de suite is een regressienet, geen formeel bewijs. User-visible wijzigingen moeten ten minste een handmatige testnotitie meebrengen.
- **Commentaar** waar de code een verrassing verbergt (workaround, race, invariant). Nooit een commentaar dat de code dupliceert.

## Release-cyclus

Werk vindt plaats op een versie-branch (``0.8.x``, ``0.9.x``, …). Zodra een versie klaar is, wordt de branch fast-forward-gemerged in ``main``; ``main`` draagt daarmee de uitgebrachte historie. Tags worden op dat moment vanuit ``main`` geknipt en de RPMs worden op het binaire kanaal van het project gepubliceerd.

Versie-bumps in ``VERSION`` / ``pyproject.toml`` / ``rpmbuild/SPECS/urpm-ng.spec`` zijn een taak van de maintainer — commit geen bump in je bijdrage. Dat gezegd hebbende, voel je vrij om **lokaal** de ``release``-regel in de spec te verhogen zodat je gebouwde RPM netjes over die van het systeem installeert; alleen niet die regel stagen.
