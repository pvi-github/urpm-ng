# Migration von urpmi zu urpm-ng

Eine einseitige Referenz für Mageia-Benutzer, die mit dem klassischen
``urpmi``-Werkzeugsatz vertraut sind.  ``urpm-ng`` ersetzt die
Gesamtheit von ``urpmi`` / ``urpme`` / ``urpmq`` / ``urpmf`` /
``urpmi.addmedia`` / ``urpmi.removemedia`` / ``urpmi.update`` durch
eine einzige ``urpm``-Binärdatei mit Unterbefehlen.

Jeder Unterbefehl hat einen kurzen Ein-Buchstaben-Alias — dieser
Spickzettel verwendet die Kurzformen, weil das dem täglichen Gebrauch
entspricht; die Langformen (``install``, ``erase``, ``upgrade``, …)
funktionieren identisch und lesen sich in Skripten besser.

Einmal lesen; griffbereit halten, wenn du anderen bei der Migration
hilfst.

Platzhalter in ``<spitzen Klammern>`` sind Werte, die du bereitstellst.

## Paketoperationen

| ``urpmi`` / ``urpme``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi <pkg>``                      | ``urpm i <pkg>``             |
| ``urpmi --auto <pkg>``               | ``urpm i -y <pkg>``          |
| ``urpmi --test <pkg>``               | ``urpm i --test <pkg>``      |
| ``urpme <pkg>``                      | ``urpm e <pkg>``             |
| ``urpmi --auto-update``              | ``urpm u``                   |
| ``urpmi --no-install <pkg>``         | ``urpm dl <pkg>``            |

Hinweise :
- ``--auto`` und ``-y`` sind überall in ``urpm-ng`` austauschbar.
- ``urpm remove`` wird als Erleichterung für Benutzer akzeptiert, die
  von apt / dnf kommen — das kanonische Verb ist ``e`` (``erase``).

## Medienverwaltung

| ``urpmi.*`` / ``urpmq``              | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi.update -a``                  | ``urpm m u``                 |
| ``urpmi.update <medianame>``         | ``urpm m u <medianame>``     |
| ``urpmi.addmedia <url>``             | ``urpm m a <url>``           |
| ``urpmi.addmedia --distrib <url>``   | ``urpm m disc <url>``        |
| ``urpmi.removemedia <medianame>``    | ``urpm m r <medianame>``     |
| ``urpmi.removemedia -a``             | ``urpm m r --all``           |
| ``urpmq --list-media``               | ``urpm m l``                 |

Hinweise :
- ``m`` ist der Kurzalias für ``media``.  ``m u`` = ``media update``,
  ``m a`` = ``media add``, ``m r`` = ``media remove``, ``m l`` =
  ``media list``, ``m disc`` = ``media discover``.  Die vollständige
  Form ``urpm media update`` usw. funktioniert genauso.

## Abfragen

| ``urpmq`` / ``urpmf``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmq <pkg>``                      | ``urpm q <pkg>``             |
| ``urpmq -i <pkg>``                   | ``urpm sh <pkg>``            |
| ``urpmq -d <pkg>``                   | ``urpm d <pkg>``             |
| ``urpmq -R <pkg>``                   | ``urpm rd <pkg>``            |
| ``urpmf --provides <pkg>``           | ``urpm wp <pkg>``            |
| ``urpmf --whatrequires <pkg>``       | ``urpm wr <pkg>``            |
| ``urpmf --files <path>``             | ``urpm f <path>``            |
| ``urpmq --list-orphans``             | ``urpm l --orphans``         |

Hinweise :
- Kurzaliase : ``q`` = ``query`` (auch ``search``, ``s``),
  ``sh`` = ``show``, ``d`` = ``depends``, ``rd`` = ``rdepends``
  (auch ``whatrequires``, ``wr``), ``wp`` = ``whatprovides``,
  ``f`` = ``find``, ``l`` = ``list``.

## Bauen / Verteilung

| Klassisches Mageia                   | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``genhdlist2 <tree>``                | ``urpm genmedia <tree>``     |
| ``rpmbuild...`` ``bm -b <spec>``     | ``urpm build <spec>``        |
| ``mach``, ``mock``, ...              | ``urpm image make`` + ...    |
|                                      | ... ``urpm build --image``   |

## Wissenswerte Verhaltensunterschiede

- **Eine Binärdatei, Unterbefehle.**  Alle Operationen leben unter
  ``urpm``.  Bash-Vervollständigung ist standardmäßig installiert.
- **``urpm.cfg`` ersetzt ``urpmi.cfg``** unter ``/etc/urpm/urpm.cfg``.
  Beim ersten Aufruf liest ``urpm m import`` die alte
  ``/etc/urpmi/urpmi.cfg`` und migriert jeden Eintrag, einschließlich
  ``MIRRORLIST``-basierter — keine manuelle Bearbeitung nötig.
- **Natives Rollback.**  ``urpm h`` (history) und ``urpm rollback``
  decken jede Transaktion ab — kein Bedarf an Snapshot-Werkzeugen
  von Drittanbietern.
- **P2P-LAN-Cache.**  Läuft ``urpmd`` auf mehreren Rechnern desselben
  LAN, teilen sie heruntergeladene Pakete automatisch.  Keine
  Konfiguration nötig.
- **Container- / Build-Image-Unterstützung.**  ``urpm image make``
  baut ein minimales Mageia-Chroot- / Container-Image, bereit für
  ``urpm build`` — keine ``mach`` / ``mock``-Hackereien mehr nötig.
- **Exit-Codes sind strukturiert** — siehe ``urpm(1)`` ``EXIT CODES``.
  Die häufigsten entsprechen urpmi (0 = Erfolg, ungleich Null = etwas
  zum Nachschauen).

## Schnellstart nach der Installation (wenn nicht als RPM installiert)

```sh
# Die Medien importieren, die du bereits unter urpmi hattest
sudo urpm m import

# Server an die soeben importierten mirrorlist-basierten Medien anhängen
sudo urpm srv autoconfig

# Paketlisten aktualisieren
sudo urpm m u

# Du bist bereit
urpm q firefox
sudo urpm i firefox
```

## Vollständige Dokumentation

- ``urpm --help`` (auch ``urpm <unterbefehl> --help``)
- ``man urpm``
- [README.md](README.md) — Übersicht über Installation und Funktionen
- [CHANGELOG.md](CHANGELOG.md) — Release-für-Release-Historie
