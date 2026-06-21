# Mitwirken bei urpm-ng

Danke, dass du dir die Zeit nimmst, dieses Dokument zu lesen.  urpm-ng
soll der Python-Ersatz für die historische `urpmi`-Werkzeugkette von
Mageia werden, und jede Verbesserung — Fix, Feature, Doku, Übersetzung,
Test — ist willkommen.

Dieses Projekt wird von einer ehrenamtlichen Gemeinschaft getragen.  Der
Ton unserer Reviews und Diskussionen ist *unter Gleichgestellten*: Wir
zeigen auf den Code, nicht auf die Personen; wir formulieren Vorschläge,
keine Befehle; wir stellen ehrliche Fragen statt rhetorische.

> Übersetzungen dieses Dokuments findest du unter
> [`CONTRIBUTING.md`](CONTRIBUTING.md) (englisch, Referenzfassung),
> [`CONTRIBUTING_es.md`](CONTRIBUTING_es.md),
> [`CONTRIBUTING_fr.md`](CONTRIBUTING_fr.md),
> [`CONTRIBUTING_it.md`](CONTRIBUTING_it.md),
> [`CONTRIBUTING_nl.md`](CONTRIBUTING_nl.md) und
> [`CONTRIBUTING_pt.md`](CONTRIBUTING_pt.md).

## Ziele des Projekts

- **Lesbarer** Code: aussagekräftige Namen, kurze Funktionen, klar
  abgegrenzte Verantwortlichkeiten.
- **Dokumentiert**: Docstrings auf der gesamten öffentlichen API,
  Kommentare dort, wo das *Warum* nicht offensichtlich ist.
- **Leicht einzusteigen**: Eine neue Mitwirkende soll ein Modul
  verstehen können, ohne zuvor das gesamte Projekt zu lesen.
- **Konsistent**: einheitliche Konventionen durch die gesamte Codebasis.
- **Performant**: Qualität und Performance sind keine Gegensätze — wir
  streben beides zugleich an.

## Einrichtung

Voraussetzungen:

- Python 3.13 oder neuer
- `python3-solv`, `python3-zstandard`, `python3-rpm`, `python3-curl`,
  `python3-pyyaml`
- `rpmbuild` (für die Test-Fixtures und die Build-Pipeline)

Funktionsfähiger Checkout:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng
urpmi python3-solv python3-zstandard
pytest urpm/tests/
```

Siehe [`doc/HOWTO_TESTS.md`](doc/HOWTO_TESTS.md) für den
pytest-Spickzettel und [`doc/TESTING.md`](doc/TESTING.md) für den Stand
der Testabdeckung und die bekannten Lücken.

## Arbeitsablauf

1. Erstelle eine Branch von `main` (oder von der aktiven Releasezweig,
   falls vorhanden — `0.7.x` war beispielsweise bis zur 0.7.15 aktiv,
   `0.8.x` wird zum Zeitpunkt dieser Niederschrift vorbereitet).
2. Setze die Änderung um, möglichst mit Tests.
3. Führe die einschlägigen Tests lokal aus.  Manuelle Tests bleiben für
   alles erforderlich, was den Nutzer betrifft — die Testsuite ist ein
   Sicherheitsnetz gegen Regressionen, keine funktionale Garantie.
4. Öffne eine Pull Request, die beschreibt, was die Änderung tut *und
   warum*.

## Commit-Konventionen

- **Sprache**: Englisch.  Gemischtsprachige Commit-Nachrichten machen
  die Historie unleserlich.
- **Betreffzeile**: ≤ 50 Zeichen, conventional-commit-Präfix
  (`fix(...)`, `feat(...)`, `docs(...)`, `chore(...)`, `test(...)`).
- **Rumpf**: das *Warum* erklären, nicht nur das *Was*.  Das *Was* zeigt
  der Diff bereits.
- **Keine KI-Zuschreibung**: Der Commit-Hook weist `Co-Authored-By`-
  Zeilen ab, die KI-Assistenten nennen.  Autor des Commits ist der
  Mensch, der `git commit` ausgeführt hat.

## Git-Hygieneregeln

- Niemals `git add .` oder `git add -A` ausführen.  Dateien einzeln
  hinzufügen — so vermeidest du, versehentlich eine `.env`, einen
  Entwurf oder ein generiertes Artefakt zu committen.
- Vor jedem Commit `git status` ausführen, damit die Liste der
  gestagten Dateien sichtbar ist.
- Niemals ohne ausdrückliche Bestätigung committen — sei es vom
  Reviewer oder von dir selbst nach einer `git status`-Prüfung.
- Niemals die Commit-Hooks überspringen.  Wenn ein Hook anschlägt, die
  Ursache beheben statt `--no-verify` zu setzen.

## Was nicht committet wird

- **Entwürfe** im Repo-Wurzelverzeichnis oder unter `doc/`.  Lebende
  Entwürfe (`RELEASE_NOTE.md`, in Arbeit befindliche `doc/TODO_*.md`,
  `SPEC_*.md` vor der Validierung) bleiben in deinem lokalen Checkout
  und wandern erst nach Validierung in den Worktree.
- **Lokale Test-Artefakte**: `essais/` steht im gitignore — dorthin
  gehören transiente Skripte, Smoke-Test-Arbeitsverzeichnisse und
  `mktemp`-Ausgaben.  Niemals an die Repo-Wurzel.
- **Alles, was `bin/rebuild.sh` oder `bin/recup.sh` heißt** —
  persönliche Skripte, die nichts im Upstream zu suchen haben.
- **DNF-Vokabular**: urpm-ng zielt auf das Mageia-Ökosystem.  Ein
  Vergleich mit DNF in einer Fußnote ist in Ordnung, aber DNF-
  Befehlsnamen (`skip-broken`, `distro-sync`, ...) sollten nicht für
  unsere eigenen Optionen übernommen werden.
- **Generierte Dateien**: `.mo`-Kataloge, `__pycache__/`, `*.pyc`,
  Sphinx-Builds — alle gitignored, das soll auch so bleiben.

## Dokumentationskonventionen

- **Sprachen**: Code, Kommentare und Commit-Nachrichten auf Englisch.
  Die Nutzerdokumentation unter `doc/` ist ein französisch-englischer
  Mix (Status quo); übersetzte nutzersichtbare Strings laufen über
  `po/`, die Manpages liegen unter `man/<lang>/`.
- **Ton**: pädagogisch und verwertbar.  Ein Bericht muss auch für
  jemanden lesbar sein, der dem Gespräch nicht gefolgt ist.  Vermeide
  telegraphische Tabellen mit kryptischen Codes (C1/N7), es sei denn,
  jede Zelle wird erläutert.
- **Länge ist keine Tugend**, Kürze auf Kosten der Klarheit aber auch
  nicht.  Jeder Absatz soll dem Leser etwas geben, das er nicht selbst
  hätte ableiten können.

## Tests

- Die Tests liegen in `urpm/tests/`, **nicht** in einem
  Wurzelverzeichnis `tests/`.  RPMs werden mit
  `urpm/tests/gen_test_rpms.py` erzeugt, und pytest wird aus
  `urpm/tests/` heraus gestartet (die Testinfrastruktur erwartet dieses
  Arbeitsverzeichnis für manche Integrationstests).
- Die testspezifischen Temporärverzeichnisse werden inzwischen durch
  eine autouse-Fixture auf `BaseUrpmiTest` aufgeräumt, sodass ein Test,
  der vor seinem expliziten Cleanup abbricht, keine `/tmp`-Einträge
  mehr hinterlässt.
- Für Änderungen an Manpages bitte mit
  `groff -man -Tutf8 -ww man/<lang>/man1/urpm.1` validieren —
  vorbestehende UTF-8-Warnungen sind nicht blockierend, neue Fehler
  hingegen schon.

## Code-Review

Wir reviewen uns gegenseitig als Gleichgestellte.  Wenn du eine Review
erhältst:

- Der Reviewer zeigt auf den Code, nicht auf dich.
- »Warum hast du X gemacht?« ist eine echte Frage; antworte ehrlich,
  statt dich zu verteidigen.
- »Könnten wir stattdessen Y machen?« ist ein Vorschlag, kein Urteil.

Wenn du eine Review gibst:

- Selbstrelativierung ist in Ordnung, Vorschreiben nicht.  Schlage vor,
  diktiere nicht.
- Frage nach, bevor du annimmst.  »Vielleicht übersehe ich etwas,
  aber ...« ist ein guter Einstieg.
- Markiere Blocker klar; Stil-Nitpicks bleiben Stil-Nitpicks.

## Releases

Die Release-Arbeit läuft auf einem Versionszweig (`0.7.x`, `0.8.x`,
...) und wird zum Release-Zeitpunkt per Fast-Forward in `main`
gemergt.  `main` trägt die Geschichte der Releases; der aktive Zweig
trägt die laufende Arbeit.

`urpm/__init__.py` und `pyproject.toml` werden gemeinsam hochgezählt,
wenn eine Release getaggt wird.  Niemals eigenmächtig eine
Versionsnummer festlegen — frage den Projektverantwortlichen.

## Wo was wohnt

```
urpm/                  # Quellcode
  cli/                 # Befehlszeilenoberfläche
  core/                # Resolver, Datenbank, Download, Install...
  daemon/              # urpmd
  genmedia/            # Serverseitige Medienerzeugung (urpm genmedia)
  tests/               # ALLE Tests liegen hier, NICHT in /tests/
rpmdrake/              # Grafisches Frontend
man/<lang>/man1/       # Übersetzte Manpages
po/                    # Übersetzungskataloge (.po pro Sprache)
doc/                   # Designdokumente, Pläne, TODO-Dateien
rpmbuild/SPECS/        # Mageia-Packaging-.spec-Dateien
data/                  # systemd-Units, polkit-Regeln usw.
```

Für den kumulativen Katalog der Features, die urpm-ng bietet, siehe
[`FEATURES.md`](FEATURES.md).  Für die Historie pro Release siehe
[`CHANGELOG.md`](CHANGELOG.md).  Für die Backlogs siehe
[`TODO.md`](TODO.md) und die themenspezifischen Dateien unter
[`doc/TODO_*.md`](doc/).
