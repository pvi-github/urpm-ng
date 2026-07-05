# Zu urpm-ng beitragen

urpm-ng ist ein kleines freiwilliges Projekt (hoffentlich wachsend). Eine Handvoll Betreuer, eine winzige Gruppe regelmäßiger Tester, und viel zu tun. Wenn du Mageia benutzt und dir hier etwas ins Auge fällt, würden wir uns über deine Hilfe freuen — selbst ein fünfminütiges „ausprobiert, bei Schritt X gescheitert" ist mehr wert, als du denkst.

Dieses Dokument soll klar machen, *wie* du helfen kannst, egal wie stark du dich einbringen willst. Nichts hier setzt voraus, dass du schon einmal ein Distributionstool gepatcht hast.

## Wie du helfen kannst

Fünf Wege, vom leichtesten zum schwersten. Wähle den, der zu deiner Zeit passt — keiner ist zweitklassig.

### 1. Probier es aus und sag uns, was passiert (das Gute und das Schlechte)

Das Nützlichste, was jemand von außen tun kann. Installiere urpm-ng auf deinem Rechner (folge dem Abschnitt *Installation* der [`README.md`](README.md) für die aktuellen RPM-Anweisungen), benutze es ein paar Tage lang für das, was du sonst mit ``urpmi`` machst, und melde alles, was dich überrascht hat — einen Absturz, eine falsche Meldung, eine fehlende Übersetzung, einen Ablauf, der sich holprig, repetitiv oder unnatürlich anfühlte.

- Wohin melden: **GitHub Issues** unter <https://github.com/pvi-github/urpm-ng/issues>.
- Bitte mindestens einschließen:
  - Die Mageia-Version (``cat /etc/mageia-release``).
  - Die Architektur (``uname -m``).
  - Die urpm-ng-Version (``urpm --version`` — und ``rpm -q urpm-ng-core``, um zu bestätigen, welches RPM installiert ist und ob es das System-RPM ist).
  - Die exakte Kommandozeile, die schiefging, was du bekommen hast und was du erwartet hattest.
- Logs nur anhängen, wenn wir danach fragen.

### 2. Übersetze — oder poliere bestehende Übersetzungen

Sechs Sprachen sind übersetzt (fr / de / es / it / nl / pt). Die Abdeckung ist breit, aber nicht vollständig: Strings rutschen unübersetzt durch, manche msgstrs klingen holprig, und ein muttersprachliches Ohr entdeckt falsche Freunde, die ein Erstdurchgang nicht sieht. Wenn eine dieser Sprachen deine Muttersprache ist, ist ein Durchgang über die bestehenden Übersetzungen zur Verfeinerung sehr willkommen.

- Die Strings leben in ``.po``-Dateien unter [`po/`](po/); öffne sie im Editor deiner Wahl (poedit reicht).
- Leere oder ``fuzzy``-Einträge sind neue oder möglicherweise veraltete Strings — der einfachste Startpunkt.
- Führe ``msgfmt --check-format po/<lang>.po -o /dev/null`` aus — wenn das durchläuft, tut es auch der Build.
- Dasselbe gilt für die Doku: die kanonischen ``README.md`` / ``MIGRATION.md`` / ``CHANGELOG.md`` haben Sprachgeschwister (``README_fr.md`` usw.), die ebenfalls von einer muttersprachlichen Nachlese profitieren würden.

### 3. Verbessere die Dokumentation

Man-Pages, das README, das Migrations-Merkblatt, das Changelog — alles, was Prosa ist. Selbst eine Tippfehlerkorrektur ist nützlich. Man-Pages leben unter ``man/<lang>/man1/urpm.1``; validiere mit ``groff -man -Tutf8 -ww man/<lang>/man1/urpm.1``.

### 4. Behebe einen Bug oder füge ein kleines Feature hinzu

Der Backlog lebt an zwei Orten:

- [`TODO.md`](TODO.md) im Repo-Root — die sichtbare Liste.
- Die verschiedenen ``doc/TODO_*.md``-Dateien — thematische Backlogs und themenbezogene Notizen. Manche sind bereit zum Coden, manche brauchen erst Diskussion. Frag nach, bevor du ein ganzes Wochenende investierst.

Lies weiter für den Build- / Test- / Patch-Ablauf.

### 5. Steig in die Klempnerei ein (das schwerste Kaliber)

Refactorings, Arbeit am Resolver, ``urpmd``-Hintergrundjobs, Arbeit an Spec-Dateien, mkimage- / Build-Container-Härtung. Hier lebt die technische Roadmap des Projekts. Sag zuerst Hallo — Koordination vermeidet, sich gegenseitig auf die Füße zu treten, oder auf die Füße getreten zu werden. Wir beißen nicht, versprochen.

## Sourcen holen und bauen

Zwei Build-Wege. Der **einfache** benutzt ``bm`` (den ``build-mageia``-Wrapper) auf deinem Host und braucht nur ``urpmi``. Der **reproduzierbare** benutzt ``urpm build`` in einem Container und braucht ein bereits installiertes urpm-ng.

### Bootstrap-Abhängigkeiten (einmalig)

Auf einer frischen Mageia-Box ist ``urpmi`` verfügbar, aber ``sudo`` ist womöglich nicht konfiguriert — die klassische ``su -c``-Form funktioniert überall:

```sh
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# Das Build-Tool (bm) und jede vom Spec deklarierte BuildRequires.
# --buildrequires liest das Spec direkt, daher bleibt die Liste
# automatisch synchron. bm selbst steht nicht in den BuildRequires
# des Specs (es ruft rpmbuild auf, statt vom %build konsumiert zu
# werden), deshalb die zwei Befehle.
su -c "urpmi bm && urpmi --buildrequires rpmbuild/SPECS/urpm-ng.spec"
```

### Einfacher Weg — ``bm`` auf dem Host

```sh
make rpm-all
```

Dann installiere die frisch gebauten RPMs.

**Erste Male — noch kein urpm-ng auf dem System** — gib alle RPMs auf einen Schlag an ``urpmi`` (der Version-Release-Filter verhindert, dass ein älterer Build im ``RPMS/``-Ordner mitgenommen wird):

```sh
RPMS=$(find rpmbuild/RPMS rpmdrake/rpmbuild/RPMS \
            -name "*-$(cat VERSION)-$(cat RELEASE).*.rpm")
su -c "urpmi $RPMS"
```

**Weitere Iterationen** — der Resolver von urpm-ng scannt das Nachbarverzeichnis automatisch nach lokalen RPMs (er meldet „Found N sibling RPMs (available for dependencies)"), es reicht also, auf die beiden Meta-Pakete zu zeigen:

```sh
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Reproduzierbarer Weg — Container-Build

Nur nutzbar, wenn urpm-ng bereits auf dem Host installiert ist (Henne-und-Ei bei der allerersten Installation). Er garantiert einen sauberen, isolierten Build und erlaubt, andere Mageia-Versionen oder Architekturen von derselben Arbeitsstation aus zu bauen, ohne den Host anzufassen.

```sh
# Einmalig: das Build-Image erstellen (Beispiel: mga10 auf x86_64).
# Der ``tag`` ist der Name, mit dem du das Image bei späteren
# Builds aufrufst — erstelle mehrere, wenn du von einer
# Arbeitsstation aus mehrere Versionen und/oder Architekturen
# ansteuern willst.
su -c "urpm image make --release 10 --tag mga10-64"

# Danach bei jedem Build — beide Specs (urpm-ng und rpmdrake-ng)
urpm build --image mga10-64 rpmbuild/SPECS/urpm-ng.spec \
                            rpmdrake/rpmbuild/SPECS/rpmdrake-ng.spec

# Install — urpm-ng ist auf dem Host bereits vorhanden (Voraussetzung
# dieses Wegs), also reicht ``urpm i`` auf die zwei Meta-Pakete: der
# Resolver holt sich die geschwisterlichen RPMs aus dem gleichen
# Verzeichnis von selbst.
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Die Tests laufen lassen

```sh
# Achtung: das volle pytest dauert eine Weile — 30 bis 60 Minuten.
pytest urpm/tests/
```

Siehe [`doc/TESTING.md`](doc/TESTING.md) für einen pytest-Spickzettel und bekannte Abdeckungslücken.

Für schnelle Dev-Iteration ohne bei jedem Mal ein RPM zu bauen, laufen die Quelldateien direkt aus dem Checkout — ``python -m urpm.cli.main <unterbefehl>`` funktioniert, sofern dein ``$PYTHONPATH`` die Checkout-Wurzel enthält.

## Dein erster Beitrag — der ganze Rundlauf

1. **Branch.** Von der aktiven Versions-Branch (aktuell ``0.8.x`` — wirf im Zweifel einen Blick in die ``VERSION``-Datei im Repo-Root). ``main`` trägt nur die veröffentlichte Historie; neue Arbeit landet dort nie direkt, sondern per Fast-Forward-Merge von der Versions-Branch zum Release-Zeitpunkt.
2. **Ändere.** Schreib den Fix oder das Feature. Wenn du den Resolver, die Transaktionsqueue oder ``urpmd`` anfasst, ist ein Test in ``urpm/tests/`` fast Pflicht. Für CLI- oder Doku-Arbeit reicht manuelles Testen auf deiner Kiste.
3. **Lokal testen.** ``pytest urpm/tests/`` (die volle Suite für alles User-Sichtbare, ansonsten die gezielte Datei). Behebe jede Regression, bevor du weitermachst.
4. **Sichtbare Oberfläche aktualisieren**, wenn deine Änderung user-facing ist (ein Fix an einem internen Codepfad braucht das selten):
   - aktualisiere die ``.po``-Kataloge (jeder neue englische user-facing String ist ein neuer msgid);
   - aktualisiere ``man/<lang>/man1/urpm.1``, wenn ein Flag hinzugefügt, umbenannt oder entfernt wurde;
   - aktualisiere README / MIGRATION-Merkblatt, wenn die Änderung tägliche Befehle betrifft.

   Der Eintrag in ``CHANGELOG.md`` selbst ist Sache des Maintainers zum Release, kein Teil einer PR.

5. **Commit.** Kurze Betreffzeile (~50 Zeichen), Conventional-Prefix (``fix(bereich):``, ``feat(bereich):``, ``docs:``, ``chore:``, ``test:``, ``refactor:``). Der Body erklärt das *Warum* — das *Was* zeigt schon der Diff.

Bevor du einen Pull Request öffnest, geh diese Checkliste durch:

- [ ] ``make rpm-all`` (oder der Container-Build) läuft durch.
- [ ] ``pytest urpm/tests/`` läuft ohne Regression durch.
- [ ] Du hast deine **lokal gebauten RPMs installiert** und aus dieser installierten Kopie heraus getestet (erhöhe die ``release``-Zeile in ``rpmbuild/SPECS/urpm-ng.spec`` lokal, damit die RPM-Nummer höher ist als die des Systems und sauber darüber installiert — nur lokale Bequemlichkeit, niemals diesen Bump committen).
- [ ] Die naheliegenden Smoke-Befehle laufen auf dem installierten Build noch, ohne dass deine Änderung einen davon bricht:
  - ``urpm i <einpaket>`` — Installationspfad
  - ``urpm q <einpaket>`` — Query
  - ``urpm e <einpaket>`` — Erase
  - ``urpm f /pfad/zur/datei`` — Find
  - ``urpm m u`` — Media Update
  - ``urpm u`` — System-Upgrade
- [ ] Deine Branch ist **rebased** auf die Zielbranch (keine Merge-Commits zwischen deiner Arbeit und der Spitze).
- [ ] Doku / Man-Pages / Übersetzungen aktualisiert wie in Schritt 4.

6. **Push** in dein Fork oder deine Branch.
7. **Öffne einen Pull Request** auf GitHub. Beschreibe die Absicht, die Testabdeckung und jede bekannte Einschränkung. Nenn die Release-Linie, die du anvisierst, und bestätige die obige Checkliste.
8. **Auf Review iterieren.** Ein Reviewer schaut deinen Diff durch und stellt Fragen oder schlägt Anpassungen vor. Wir streben einen kollegialen Austausch an — nichts Persönliches, alles am Code. Wir bemühen uns um freundliche Formulierungen; sollte ein Kommentar mal danebenliegen, ist die Absicht nie feindselig — Kompass sind das Projekt und Mageia.

## Wo du uns erreichst

- **Issues & PRs**: <https://github.com/pvi-github/urpm-ng>
- **Direkter Kontakt — Matrix**: [@maat_:matrix.org](https://matrix.to/#/@maat_:matrix.org)

## Wo der Code lebt

```
urpm/                  # Python-Quellen
  cli/                 # Kommandozeile (urpm, Unterbefehle)
  core/                # Resolver, Download, Install, DB, Sync
  daemon/              # urpmd (Hintergrunddienst, LAN-P2P)
  genmedia/            # Server-seitige Medien-Metadaten-Generierung
  tests/               # Alle Tests leben hier (nicht in einem top-level tests/)
rpmdrake/              # Qt6-GUI-Frontend (rpmdrake-ng)
pk-backend-urpm/       # C-Plugin: PackageKit-Backend auf urpm-ng
man/<lang>/man1/       # Übersetzte Man-Pages
po/                    # Übersetzungskataloge (.po)
doc/                   # Design-Docs, Pläne, TODOs, Specs
rpmbuild/SPECS/        # Mageia-Packaging (.spec)
data/                  # systemd-Units, polkit-Regeln, Config-Vorlagen
```

Für eine tiefere Karte siehe [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md). Für den kumulativen Feature-Katalog [`FEATURES.md`](FEATURES.md).

## Stil-Erwartungen (kurz)

- **Englisch** im Code, in Kommentaren und Commit-Nachrichten. Eine mehrsprachige Historie ist verwirrend.
- **Docstrings** an jeder öffentlichen Funktion oder Klasse. Ein Einzeiler reicht; erkläre das *Warum* nur, wenn es nicht aus dem Namen hervorgeht.
- **Tests**, wenn praktisch — die Suite ist ein Regressions-Netz, kein formaler Beweis. User-sichtbare Änderungen sollten mindestens eine manuelle Testnotiz mitbringen.
- **Kommentare** dort, wo der Code eine Überraschung versteckt (Workaround, Race, Invariante). Nie ein Kommentar, der den Code doppelt.

## Release-Zyklus

Die Arbeit passiert auf einer Versions-Branch (``0.8.x``, ``0.9.x``, …). Wenn eine Version bereit ist, wird die Branch per Fast-Forward-Merge in ``main`` übernommen; ``main`` trägt daher die veröffentlichte Historie. Tags werden zu diesem Zeitpunkt aus ``main`` geschnitten und die RPMs auf dem Binärkanal des Projekts veröffentlicht.

Versions-Bumps in ``VERSION`` / ``pyproject.toml`` / ``rpmbuild/SPECS/urpm-ng.spec`` sind Sache des Maintainers — committe in deinem Beitrag keinen Bump. Fühl dich aber frei, die ``release``-Zeile im Spec **lokal** hochzusetzen, damit dein gebautes RPM sauber über das des Systems installiert wird; staging dieser Zeile aber nicht.
