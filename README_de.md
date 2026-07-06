# urpm-ng

Ein moderner Paketmanager für Mageia Linux, in Python geschrieben.

urpm-ng ist eine vollständige Neuschreibung der klassischen urpmi-Werkzeugkette. Sie bietet bessere Performance, präzisere Auflösung von Abhängigkeiten und moderne Funktionen wie das P2P-Paket-Sharing.

## Voraussetzungen

### Distribution

Zurzeit brauchst du Mageia 9 oder Mageia 10.

### Firewall-Ports (für P2P-Sharing)

Das Paket `urpm-ng-daemon` liefert `/etc/shorewall/rules.urpm-ng` als
Include-Datei mit, und sein `%post` hängt sie automatisch in
`/etc/shorewall/rules` ein. Auf einer per Shorewall verwalteten Maschine
(dem Mageia-Standard) sind die folgenden Ports also direkt nach der
Installation offen, ohne weiteres Zutun:

- **TCP 9876** (Produktion) oder **TCP 9877** (Dev-Modus) — urpmd HTTP-API
- **UDP 9878** (Produktion) oder **UDP 9879** (Dev-Modus) — Peer-Discovery-Broadcasts

Falls Shorewall nicht im Einsatz ist (nacktes `iptables` / `nftables`),
öffne die Ports von Hand — die Datei `/etc/shorewall/rules.urpm-ng` im
Source-Tree taugt gut als Vorlage.

## Installation

### Pakete

urpm-ng ist zur besseren Flexibilität in mehrere Pakete aufgeteilt:

| Paket | Beschreibung |
|-------|--------------|
| `urpm-ng-core` | Minimal: CLI, Resolver, Datenbank |
| `urpm-ng-daemon` | Hintergrund-Daemon + P2P-Sharing |
| `urpm-ng` | Meta: zieht `-core` + `-daemon` (Standardinstallation) |
| `urpm-ng-appstream` | AppStream-Metadaten-Konfiguration (Mageia-OS-Metainfo, Distro-Config) |
| `urpm-ng-packagekit-backend` | PackageKit-Backend (Discover, GNOME Software) + D-Bus-Service |
| `urpm-ng-desktop` | Meta: zieht `-core` + `-daemon` + `-appstream` + `-packagekit-backend` |
| `urpm-ng-build` | Meta: zieht `-core` (für `urpm image` / `urpm build` — die Kommandos leben in `-core`) |
| `urpm-ng-genmedia` | Server-seitige Erzeugung von Medien-Metadaten (`urpm genmedia`, für Mirror-Maintainer) |
| `urpm-ng-all` | Meta: zieht alles oben Genannte |

**Das passende Paket wählen:**
- **Minimal-/Container-Install**: `urpm-ng-core`
- **Standard-CLI-Nutzung**: `urpm-ng`
- **Desktop mit GUI-Softwarezentren**: `urpm-ng-desktop`
- **Paket-Builder (bm-/mkimage-Nutzer)**: `urpm-ng-build`
- **Mirror-Maintainer, die Repositories veröffentlichen**: `urpm-ng-genmedia`

### Schnelle Installation / Update (`geturpm.sh`)

`geturpm.sh` ist der empfohlene Weg, urpm-ng auf einem frischen
Mageia-System zu installieren, und er kann auch eine bestehende
Installation aktualisieren. Er erkennt Release und Architektur automatisch,
zieht das neueste urpm-ng aus dem gewählten Kanal, und macht das
Richtige, egal ob urpm-ng schon installiert ist oder nicht (frische
Maschinen bootstrapen mit `urpmi`, spätere Updates laufen über urpm-ng
selbst).

**Schnell — via Pipe, ohne lokale Inspektion**

```bash
curl -fsSL https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh | bash
```

Prompts (Kanalwahl, „Proceed?", das Root-Passwort für `su`) werden
aus `/dev/tty` gelesen, damit ist die gepipete Form voll interaktiv
— gleiches Erlebnis wie beim Ausführen aus einer Datei.

**Verifiziert — herunterladen, lesen, dann ausführen** (empfohlen,
wenn du der Quelle nicht ohnehin schon vertraust):

```bash
curl -fsSLO https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh
less geturpm.sh                  # vor dem Ausführen prüfen
bash geturpm.sh                  # interaktiv: Kanal + Bestätigung
```

**Kanal wählen** (`--channel=CHAN`):

- `mgabiz` — holt aus dem Mageia.biz-Projekt-Repo (Standard, wenn
  kein Terminal verfügbar ist). Nutzt `urpm media discover` auf dem
  mgabiz-Mirror, spätere Updates laufen also über den Standard-Flow
  von `urpm media update`.
- `github` — holt Release-RPMs direkt von der GitHub-Releases-Seite.
  Nützlich, um ein bestimmtes Tag zu testen, oder wenn die
  mgabiz-Veröffentlichung einem Release hinterherhängt.

**Unbeaufsichtigte Läufe** — mit `-y` (überspringt die
„Proceed?"-Bestätigung) und `--channel=CHAN` (überspringt den
Kanal-Prompt) via `bash -s --`:

```bash
curl -fsSL <url>/geturpm.sh | bash -s -- -y --channel=mgabiz
```

Hinweis: Bei der ersten Installation importiert urpm-ng seine
Konfiguration automatisch aus vorhandenen `urpmi.cfg`- und
`urpmi/skip.list`-Dateien.

## Erstlauf-Einrichtung

urpm läuft direkt aus dem Karton. Fortgeschrittene Optionen (Blacklist, Redlist, Kernel-Keep) sind weiter unten unter **Konfiguration** dokumentiert.

Bei einer systemweiten Installation (in `/usr/bin/`) nutzt urpm:
- Datenbank: `/var/lib/urpm/packages.db`
- Daemon-Port: 9876
- PID-Datei: `/run/urpmd.pid`

### Medienquellen

Bei einer Installation über den RPM-Pfad (oder via `geturpm.sh`) werden
die Standard-Mageia-Medien und die zugehörigen Server automatisch
eingerichtet: `urpm-ng` importiert die vorhandene `urpmi.cfg` beim
ersten Lauf, und `urpm server autoconfig` füllt den Mirror-Pool aus
der Mageia-Mirror-API. Mehr ist zum Installieren von Paketen nicht
nötig.

Auf einer Maschine ohne vorherige `urpmi.cfg` (frisches Chroot,
Image-Build oder ein System, das nie urpmi hatte) läuft derselbe
Bootstrap in einem manuellen Durchgang:

```bash
urpm media list                       # Noch nichts da? Bootstrap:
urpm media import                     # Liest per Default /etc/urpmi/urpmi.cfg; No-op falls fehlt
urpm server autoconfig                # Mirrors aus der Mageia-API ziehen
urpm media update                     # Erste Metadaten-Sync
```

Zum Hinzufügen eines **Community-Repositorys** (MageiaLinux-Online,
mageia.biz, blogdrake, ein interner Mirror, …) `urpm media discover`
nutzen — es liest die `media.cfg` des Repos und fügt in einem Aufruf
alle darin angekündigten Medien hinzu:

```bash
urpm media discover https://www.mageia.biz/repo/Mageia/mgabiz/10/x86_64/media/
urpm media discover --dry-run https://download.mageialinux-online.org/...   # Vorschau
```

`urpm media add` ist einzelnen, nicht-discover-fähigen Custom-Medien
vorbehalten — also solchen, von denen du weißt, dass sie nicht über
eine `media.cfg` veröffentlicht werden. Die Syntax steht weiter unten
im Abschnitt **Medienverwaltung**.

---

# urpm - Kommandozeilen-Interface

## Globale Optionen

Diese Optionen gelten für die meisten Befehle und stehen vor dem Unterbefehl:

```bash
-V, --version              # urpm-Version anzeigen
-v, --verbose              # Ausführliche Ausgabe
-q, --quiet                # Leise Ausgabe
--nocolor                  # Farbige Ausgabe deaktivieren
--root DIR                 # DIR als Wurzel für RPM-Install nutzen (chroot, urpm-Config vom Host)
--urpm-root DIR            # DIR als Wurzel für urpm-Config UND RPM-Install nutzen
```

Die folgenden Eltern werden von transaktionalen und Query-Befehlen (`install`, `upgrade`, `erase`, `download`, `depends`, …) geerbt:

```bash
--arch ARCH                # Zielarchitektur (Standard: aktuelles System)
--debug COMPONENT          # Debug-Ausgabe aktivieren: solver, tsrun, orphans, download, timing, all
--watched PACKAGES         # Kommagetrennte Paketnamen, die während der Auflösung beobachtet werden
```

Hinweis: `--arch` (Eltern-Option, setzt die Zielarchitektur für den Vorgang) ist verschieden von `--allow-arch` (per-Aufruf-Option auf install/upgrade/download, erlaubt zusätzliche Architekturen neben der System-Arch — typischerweise `i686` für wine/steam auf x86_64).

## Ausgabe-Optionen

Die meisten Befehle unterstützen diese Ausgabe-Optionen:

```bash
--show-all            # Alle Einträge ohne Kürzung anzeigen
--flat                # Ein Eintrag pro Zeile (durch Skripte parsebar)
--json                # JSON-Ausgabe (für programmatische Nutzung)
```

Standardmäßig werden lange Listen in mehreren Spalten dargestellt und auf 10 Zeilen mit „… und N weitere" gekürzt. Nutze `--show-all`, um alles zu sehen.

Beispiele:
```bash
urpm list installed --flat          # Ein Paket pro Zeile
urpm search firefox --json          # JSON-Ausgabe
urpm i task-plasma --show-all       # Alle Abhängigkeiten anzeigen
```

## Atomare vs. Best-Effort-Transaktionen

Seit 0.7.9 läuft `urpm upgrade` standardmäßig im **Best-Effort**-Modus: Pakete, deren Abhängigkeiten nicht erfüllt werden können, werden aus der Transaktion gestrichen und am Ende mit ihrem Grund gemeldet (fehlende Abhängigkeit, Versionsmismatch, SRPM-Geschwisterkaskade, …). Die Transaktion wird für alles Übrige committet. Übergib `--atomic`, um in den Strict-Modus zu wechseln (empfohlen auf Servern): Jedes unlösbare Paket bricht die gesamte Transaktion ab.

`urpm install` hingegen ist standardmäßig **atomar**: Wenn ein angefordertes Paket nicht installiert werden kann, wird die gesamte Transaktion zurückgerollt. Übergib `--no-atomic`, um für den Install-Pfad in den Best-Effort-Modus zu wechseln.

## Exit-Codes

| Code | Bedeutung |
|------|-----------|
| 0    | Transaktion erfolgreich abgeschlossen, kein Paket übersprungen |
| 1    | Harter Fehlschlag: Transaktion abgebrochen (atomic-Modus, Netzwerk, Rechte, …) |
| 2    | Teilweise Transaktion: erfolgreich, aber mindestens ein Paket wurde gestrichen (übersprungene Pakete mit Grund auf stderr aufgelistet) |

Skriptbarer Check für den Teilfall:

```bash
urpm upgrade --auto || [ $? -eq 2 ] && echo "ok oder teilweise"
```

## Paketverwaltung

### Pakete installieren

```bash
urpm install <paket>          # Ein Paket installieren
urpm i <paket>                # Kurzalias

# Optionen
--auto, -y                    # Nicht-interaktiver Modus
--test                        # Dry-Run (Simulation)
--without-recommends          # Empfohlene Pakete überspringen
--with-suggests               # Auch vorgeschlagene Pakete installieren
--force                       # Trotz Abhängigkeitsproblemen erzwingen
--reinstall                   # Bereits installierte Pakete neu installieren (Reparatur)
--nosignature                 # GPG-Verifizierung überspringen (nicht empfohlen)
--noscripts                   # Pre-/Post-Install-Skripte überspringen (Chroot-/Container-Builds)
--no-peers                    # P2P-Download von LAN-Peers deaktivieren
--only-peers                  # Nur von LAN-Peers, nicht von Upstream-Mirrors laden
--no-atomic                   # Best-Effort-Modus (Standard bei install: atomar)
--download-only               # In den Cache laden, nicht installieren
--nodeps                      # Abhängigkeitsauflösung überspringen (mit --download-only)
--all                         # Für alle passenden Familien installieren (z. B. php8.4 + php8.5)
--install-src                 # Source-RPM installieren (spec/sources nach ~/rpmbuild/ auspacken)
--config-policy {keep,replace,ask}  # Konfliktrichtlinie für Konfig-Dateien (Standard: keep)
--prefer=<prefs>              # Alternative-Wahlen steuern (siehe unten)
--allow-arch <arch>           # Zusätzliche Architekturen erlauben (z. B. i686 für wine/steam)
--sync                        # Auf vollständigen Abschluss warten (Post-Install-Trigger)
```

#### Präferenzgesteuerte Installation

Beim Installieren von Paketen mit Alternativen (z. B. phpmyadmin, das verschiedene PHP-Versionen und Webserver nutzen kann) steuere die Wahl mit `--prefer`:

```bash
# PHP 8.4 mit Apache und php-fpm bevorzugen, mod_php ausschließen
urpm i phpmyadmin --prefer=php:8.4,apache,php-fpm,-apache-mod_php

# nginx statt apache bevorzugen
urpm i phpmyadmin --prefer=php:8.4,nginx,php-fpm
```

Präferenz-Syntax:
- `capability:version` — Versions-Constraint (z. B. `php:8.4`)
- `pattern` — Pakete bevorzugen, die diese Capability bereitstellen (z. B. `apache`, `php-fpm`)
- `-pattern` — Pakete, die auf dieses Muster passen, benachteiligen (z. B. `-apache-mod_php`)

Präferenzen arbeiten auf REQUIRES und PROVIDES der Pakete, nicht auf den Namen.

#### Architektur-Filterung

Standardmäßig berücksichtigt urpm nur Pakete, die zur System-Architektur und `noarch` passen. Das verhindert die versehentliche Installation von i686-Paketen auf x86_64-Systemen, wenn 32-Bit-Medien aktiv sind.

Zum Installieren von 32-Bit-Paketen (wine, steam, multilib):

```bash
urpm install wine --allow-arch i686
urpm install steam --allow-arch i686

# Mehrere Architekturen
urpm install meinpaket --allow-arch i686 --allow-arch armv7hl
```

### Pakete entfernen

```bash
urpm erase <paket>            # Ein Paket entfernen
urpm e <paket>                # Kurzalias

# Optionen
--auto, -y                    # Nicht-interaktiver Modus
--test                        # Dry-Run (Simulation)
--auto-orphans                # Verwaiste Abhängigkeiten mitentfernen (implizit bei -y, außer --keep-orphans)
--keep-orphans                # Verwaiste Abhängigkeiten nicht entfernen
--erase-recommends            # Auch nur empfohlene (nicht benötigte) Pakete entfernen
--keep-suggests               # Von verbleibenden Paketen vorgeschlagene Pakete behalten
--force                       # Trotz Abhängigkeitsproblemen erzwingen
--debug {solver,tsrun,all}    # Debug-Ausgabe für Resolver/Transaktion aktivieren
--sync                        # Auf vollständigen Abschluss warten (Post-Uninstall-Trigger)
```

### Metadaten aktualisieren (apt-Stil)

```bash
urpm update                   # Alle Medien-Metadaten aktualisieren
urpm update "Core Release"    # Bestimmtes Medium aktualisieren
```

Seit 0.7.x wird `files.xml.lzma` zusammen mit `synthesis.hdlist.cz` geholt, sobald das Medium sie veröffentlicht — kein Opt-in-Flag mehr nötig.

### Pakete herunterladen (ohne Installation)

```bash
urpm download <paket>         # Ein Paket in den Cache laden
urpm dl <paket>               # Kurzalias
urpm download --only-peers pkg  # Nur von LAN-Peers laden

# Optionen
--release, -r <version>       # Ziel-Release für Cross-Release-Downloads (z. B. cauldron)
--buildrequires, --br [SPEC]  # Build-Abhängigkeiten laden (Auto-Detect oder aus .spec/.src.rpm)
--without-recommends          # Empfohlene Pakete überspringen
--nodeps                      # Nur die genannten Pakete laden, keine Abhängigkeiten
--no-peers / --only-peers     # Wie bei install (Peer-Politik)
--allow-arch <arch>           # Zusätzliche Architekturen erlauben
--arch <arch>                 # Geerbt: Zielarchitektur
--show-all                    # Vollständige Liste der aufgelösten Pakete anzeigen
                              # (Standard kürzt auf 20 mit „… und N weitere")
```

### Pakete aktualisieren

```bash
urpm upgrade                  # Alle Pakete aktualisieren
urpm u                        # Kurzalias
urpm upgrade <paket>          # Bestimmte Pakete aktualisieren

# Optionen
--auto, -y                    # Nicht-interaktiver Modus
--test                        # Dry-Run (Simulation)
--atomic                      # Strict-Modus: Bricht die gesamte Transaktion bei einem unlösbaren Paket ab.
                              # Standard ist Best-Effort (siehe „Atomare vs. Best-Effort-Transaktionen" oben).
--with-recommends             # Empfohlene Pakete installieren
--with-suggests               # Auch vorgeschlagene Pakete installieren
--noerase-orphans             # Verwaiste Abhängigkeiten behalten (nicht entfernen)
--download-only               # In den Cache laden, ohne das Update anzuwenden
--nosignature                 # GPG-Verifizierung überspringen (nicht empfohlen)
--no-peers / --only-peers     # Deaktivieren / auf LAN-Peers beschränken
--force                       # Update trotz Abhängigkeitsproblemen erzwingen
--config-policy {keep,replace,ask}  # Konfliktrichtlinie für Konfig-Dateien (Standard: keep)
--allow-arch <arch>           # Zusätzliche Architekturen erlauben (z. B. i686)
--sync                        # Auf vollständigen Abschluss warten (Post-Install-Trigger)
```

### Verwaiste automatisch entfernen

```bash
urpm autoremove               # Unbenutzte Abhängigkeiten entfernen (Standard: --orphans)
urpm ar                       # Kurzalias

# Selektoren
--orphans, -o                 # Verwaiste Pakete (Standard)
--kernels, -k                 # Alte Kernel
--faildeps, -f                # Deps aus unterbrochenen Transaktionen
--buildrequires, -b           # Build-Abhängigkeiten (--builddeps, --br)
--all, -a                     # All das Obige

# Optionen
--auto, -y                    # Nicht-interaktiver Modus
```

## Suche und Abfrage

### Pakete suchen

```bash
urpm search <muster>          # Nach Name/Zusammenfassung suchen
urpm s <muster>               # Kurzalias
urpm q <muster>               # Query-Alias (urpmq-Kompatibilität)

# Optionen
--installed                   # Nur unter installierten Paketen suchen
--unavailable                 # Installierte Pakete auflisten, die in keinem Medium mehr sind
```

#### Nicht verfügbare Pakete finden

Zeigt installierte Pakete an, die in keinem konfigurierten Medium mehr verfügbar sind (wie `urpmq --unavailable`):

```bash
urpm q --unavailable          # Alle nicht verfügbaren Pakete auflisten
urpm q --unavailable php      # Nach Muster filtern
```

### Paket-Info anzeigen

```bash
urpm show <paket>             # Paket-Details anzeigen
urpm info <paket>             # Alias
```

### Pakete auflisten

```bash
urpm list installed           # Installierte Pakete auflisten
urpm list available           # Verfügbare Pakete auflisten
urpm list updates             # Verfügbare Updates auflisten
urpm list upgradable          # Alias für updates
```

### Abhängigkeiten

```bash
urpm depends <paket>          # Anzeigen, was ein Paket benötigt
urpm rdepends <paket>         # Anzeigen, was ein Paket benötigt (umgekehrte Deps)
urpm why <paket>              # Erklären, warum ein Paket installiert ist

# Optionen für depends
--tree                        # Abhängigkeitsbaum anzeigen
--prefer=<prefs>              # Nach Präferenzen filtern (gleiche Syntax wie install)
--legend                      # Symbol-Legende nach der Baum-Ausgabe zeigen

# Optionen für rdepends
--tree                        # Umgekehrten Abhängigkeitsbaum zeigen
--all                         # Alle rekursiven umgekehrten Deps zeigen (flach)
--depth=N                     # Maximale Baumtiefe (Standard: 3)
--hide-uninstalled            # Nur Pfade zu installierten Paketen zeigen
--legend                      # Symbol-Legende nach der Baum-Ausgabe zeigen
```

Beispiel mit Präferenzen:
```bash
# Zeigt phpmyadmin-Abhängigkeiten mit Bevorzugung von PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

Beispiel mit rdepends:
```bash
# Umgekehrter Abhängigkeitsbaum für rtkit, Tiefe 10, nur installierte Pfade
urpm rdepends --tree --hide-uninstalled --depth=10 rtkit
```

### Schwache Abhängigkeiten

```bash
urpm recommends <paket>       # Pakete, die von einem Paket empfohlen werden
urpm whatrecommends <paket>   # Pakete, die ein Paket empfehlen
urpm suggests <paket>         # Pakete, die von einem Paket vorgeschlagen werden
urpm whatsuggests <paket>     # Pakete, die ein Paket vorschlagen
```

### Datei-Abfragen

```bash
urpm provides <paket>         # Von einem Paket bereitgestellte Dateien auflisten
urpm whatprovides <datei>     # Finden, welches Paket eine Datei bereitstellt
urpm find <muster>            # Dateien in Paketen suchen (installiert + verfügbar)
urpm find -i <muster>         # Nur in installierten Paketen suchen
urpm find -a <muster>         # Nur in verfügbaren Paketen suchen
urpm find <muster> --all-versions  # Alle EVR mit einbeziehen, die den Treffer liefern
urpm find <muster> --limit 500     # Das Default-Limit von 100 Treffern anheben
```

`urpm find` sucht standardmäßig sowohl in installierten als auch in verfügbaren Paketen. `files.xml.lzma` wird automatisch als Teil jedes `urpm media update` geholt (sofern das Medium sie in `MD5SUM` ankündigt), es ist also kein Opt-in mehr nötig — der Toggle `--sync-files` wurde in 0.7.x entfernt.

## Paket-Markierung

```bash
urpm mark manual <paket>      # Als manuell installiert markieren
urpm mark auto <paket>        # Als automatisch installiert markieren (Abhängigkeit)
urpm mark show <paket>        # Installationsgrund anzeigen
```

## Paket-Sperren (Holds)

Pakete sperren, um Updates und den Ersatz durch Obsoletes zu verhindern:

```bash
urpm hold <paket>             # Ein Paket sperren
urpm hold <paket> -r "grund"  # Mit Grund sperren
urpm hold                     # Gesperrte Pakete auflisten
urpm unhold <paket>           # Sperre entfernen
```

Gesperrte Pakete sind geschützt gegen:
- Versions-Updates während `urpm upgrade`
- Ersatz durch Pakete, die sie obsolet machen

Beispiel:
```bash
# dhcpcd macht dhcp-client obsolet, aber du willst dhcp-client behalten
urpm hold dhcp-client -r "Prefer dhcp-client over dhcpcd"

# Jetzt überspringt urpm upgrade dhcp-client und warnt:
#   Gesperrte Pakete (1) übersprungen:
#     dhcp-client (würde von dhcpcd obsolet gemacht)

# Um den Ersatz später zu erlauben:
urpm unhold dhcp-client
```

## Historie und Rückgängig

```bash
urpm history                  # Transaktionshistorie zeigen (letzte 20)
urpm history -i               # Filter: nur Install-Transaktionen
urpm history -r               # Filter: nur Remove-Transaktionen
urpm history -d <id>          # Details der Transaktion <id> zeigen
urpm history --delete <id>... # Transaktionen aus dem Log entfernen

urpm undo [id]                # Eine Transaktion rückgängig machen (Standard: die letzte). Erstellt
                              # einen sauberen Historien-Eintrag. Nutze --auto/-y, um den Prompt zu
                              # überspringen.

urpm rollback <n>             # Die letzten n Transaktionen rollbacken
urpm rollback to <id>         # Auf eine bestimmte Transaktion zurückrollen
urpm rollback to <date>       # Auf ein Datum zurückrollen (JJJJ-MM-TT oder TT/MM/JJJJ)
```

## Hintergrund-Transaktionen

Wenn eine Transaktion abgekoppelt läuft (z. B. via Daemon oder PackageKit), verfolge ihren Fortschritt mit:

```bash
urpm progress                 # Aktuellen Fortschritt zeigen und beenden
urpm progress --watch         # Kontinuierlich beobachten bis zum Abschluss
```

## Medienverwaltung

```bash
urpm media list               # Konfigurierte Medien auflisten
urpm media add <url>          # Ein offizielles Mageia-Medium hinzufügen (auto-parsed)
urpm media add --custom "Name" kurzname <url>  # Ein eigenes / Drittanbieter-Medium hinzufügen
urpm media remove <name>...   # Ein oder mehrere Medien entfernen
urpm media remove --all       # JEDES konfigurierte Medium entfernen (fragt nach
                              # Bestätigung; mit -y/--auto überspringen).
                              # Verwaiste Server (keine Medien mehr) werden
                              # im selben Durchgang mit entfernt.
urpm media enable <name>      # Ein Medium aktivieren
urpm media disable <name>     # Ein Medium deaktivieren
urpm media update [name]      # Medien-Metadaten aktualisieren
urpm media import <datei>     # Aus urpmi.cfg importieren
urpm media link <name> +srv -srv  # Server einem Medium hinzufügen/entfernen
urpm media set <name> [opts]  # Medien-Einstellungen ändern (Sharing, Replikation, Quota…)
urpm media seed-info <name>   # Seed-Info anzeigen (Sektionen, Paketzahl, Größenabschätzung)
urpm media autoconfig -r 10   # Offizielle Mageia-Medien für Release 10 automatisch hinzufügen
urpm media discover <url>     # Medien aus dem media.cfg eines Repos entdecken
```

Nützliche Flags für `urpm media add`:

```bash
--import-key                  # Den vom Medium angekündigten GPG-Schlüssel importieren
--allow-unsigned              # Unsignierte Pakete erlauben (nur eigene Medien)
--version <ver>               # Ziel-Mageia-Version (nur eigene Medien: 9, 10, cauldron…)
--update                      # Als Update-Medium markieren
--disabled                    # Hinzufügen, aber deaktiviert lassen
-y, --auto                    # Nicht-interaktiv: den auto-detektierten Namen/short_name akzeptieren
```

### Medien aus einer alten urpmi.cfg importieren

Eine bestehende Mageia-Maschine von `urpmi` zu urpm-ng migrieren, ohne
jede Medienquelle von Hand nachzutragen. Sowohl URL-basierte Einträge
als auch `MIRRORLIST=`-Einträge werden importiert — Letztere als
pending Medien, denen `urpm server autoconfig` beim nächsten Lauf die
Server zuweist.

```bash
urpm media import /etc/urpmi/urpmi.cfg    # Standardpfad
urpm media import                          # Dasselbe (Default ist /etc/urpmi/urpmi.cfg)

# Optionen
--replace                     # Bereits vorhandene Medien mit gleichem short_name überschreiben
-r, --release <version>       # Ziel-Mageia-Release (Standard: Wert von /etc/mageia-release)
--arch <arch>                 # Zielarchitektur (Standard: `uname -m`)
-y, --auto                    # Nicht-interaktiv: Bestätigungs-Prompt überspringen
```

### Medien aus einem Repository entdecken

Alle verfügbaren Medien aus einem Mageia-kompatiblen Repository entdecken (offizielle Mirrors, Community-Repos wie MLO, Firmenspiegel):

```bash
urpm media discover https://repo.example.org/9/x86_64/media/       # Alle Medien hinzufügen
urpm media discover --dry-run https://repo.example.org/9/x86_64/media/  # Nur Vorschau
urpm media discover --sources --debug https://...                   # SRPMS und Debug einschließen

# Kategorien erzwungen aktivieren / deaktivieren (nonfree, tainted, 32bit, all)
urpm media discover --with nonfree,tainted https://...
urpm media discover --without nonfree https://...
urpm media discover --with all https://...
```

Der Befehl holt `media.cfg` vom Repository, entdeckt alle Medien und verknüpft bestehende Server, die den gleichen Inhalt hosten (überprüft per MD5-Checksumme von `synthesis.hdlist.cz`).

### Server-Medien-Verknüpfung

Server mit bestimmten Medienquellen verknüpfen oder trennen:

```bash
urpm media link "Core Release" +mirror1 +mirror2   # Server hinzufügen
urpm media link "Core Updates" -oldserver          # Server entfernen
urpm media link "Core Release" +all                # Alle verfügbaren Server hinzufügen
urpm media link "Core Release" -all +preferred     # Zurücksetzen und einen hinzufügen
```

Hinweis: Beim Hinzufügen von Servern prüft urpm, dass der Medieninhalt übereinstimmt, indem MD5-Checksummen von `synthesis.hdlist.cz` mit bestehenden Referenzservern verglichen werden.

### Medien automatisch konfigurieren

Offizielle Mageia-Medien für ein Release automatisch hinzufügen:

```bash
urpm media autoconfig --release 10              # Alle offiziellen Medien für Mageia 10 hinzufügen
urpm media autoconfig -r cauldron               # Medien für Cauldron hinzufügen
urpm media autoconfig -r 10 --no-nonfree        # Nonfree-Medien überspringen
urpm media autoconfig -r 10 --no-tainted        # Tainted-Medien überspringen
urpm media autoconfig -r 10 -n                  # Dry-Run: was hinzugefügt würde
```

### Medien-Einstellungen

Medien-Sharing und Replikation konfigurieren:

```bash
urpm media set "Core Release" --shared=yes           # Mit P2P-Peers teilen
urpm media set "Core Release" --replication=seed     # Vollständige Replikation (DVD-artig)
urpm media set "Core Release" --replication=on_demand  # Heruntergeladene Pakete cachen
urpm media set "Core Release" --quota=5G             # Cache-Größe begrenzen
urpm media set "Core Release" --retention=30         # Pakete 30 Tage behalten
urpm media set "Core Release" --priority=10          # Höhere Priorität
urpm media set "Core Release" --seeds=INSTALL,CAT_PLASMA5  # Seed-Sektionen
```

Beispiele:
```bash
# Offizielles Mageia-Medium hinzufügen (Server und Medium auto-detected)
urpm media add https://ftp.belnet.be/mageia/distrib/9/x86_64/media/core/release/

# Drittanbieter-Medium hinzufügen
urpm media add --custom "RPM Fusion" rpmfusion https://download1.rpmfusion.org/free/fedora/40/x86_64/os/
```

## Server-Verwaltung

Server sind Mirror-Quellen, die mehrere Medien bedienen können. urpm unterstützt mehrere Server pro Medium für Lastverteilung und Failover.

```bash
urpm server list              # Konfigurierte Server auflisten (mit Land)
urpm server add <name> <url>  # Einen Server hinzufügen (testet IP und scannt Medien)
urpm server remove <name> ... # Einen oder mehrere Server entfernen
urpm server enable <name>     # Einen Server aktivieren
urpm server disable <name>    # Einen Server deaktivieren
urpm server priority <name> <n>  # Server-Priorität setzen (höher = bevorzugt)
urpm server test [name]       # Konnektivität testen und IP-Modus erkennen
urpm server ip-mode <name> <mode>  # IP-Modus setzen (auto/ipv4/ipv6/dual)
urpm server autoconfig        # Server automatisch aus der Mageia-Mirror-API hinzufügen
urpm server stats [name]      # Performance-Statistiken eines Servers anzeigen
urpm server status            # Blacklistete / reputationsschwache Server anzeigen
urpm server unblacklist <name>   # Blacklist eines Servers aufheben (nach Prüfung)
urpm server ack-blacklist <name>  # Ein Blacklist quittieren (das Banner verstummt, ohne aufzuheben)
```

### Server-Liste

Optionen für urpm server list:
```bash
--all                 # Alle Server anzeigen, auch deaktivierte
```

### IP-Modus

Jeder Server hat einen IP-Modus für die Handhabung von IPv4/IPv6:
- `auto` — Das System entscheidet (kann 30 s Timeout auslösen, wenn IPv6 scheitert)
- `ipv4` — Nur IPv4
- `ipv6` — Nur IPv6
- `dual` — Beide funktionieren, IPv4 wird bevorzugt (empfohlen für Dual-Stack-Server)

Der IP-Modus wird beim Hinzufügen eines Servers automatisch erkannt. Nutze `server test` zur Neuerkennung oder `server ip-mode` zum manuellen Setzen.

### Bandbreiten-Tracking und automatisches Failover

urpm verfolgt die Download-Performance jedes Servers automatisch. Nach jedem Download oder jeder Metadaten-Sync wird die gemessene Geschwindigkeit mit einem EWMA (Exponentially Weighted Moving Average, α=0,3) protokolliert. Das gibt Trägheit, damit ein einzelner langsamer Transfer einen guten Server nicht ungerecht bestraft.

Server werden in der Reihenfolge `priority DESC, bandwidth_kbps DESC` probiert: Scheitert ein Server während eines Downloads oder einer Metadaten-Sync, wird automatisch der nächstbeste versucht, ohne Benutzereingriff. Innerhalb einer Session werden Geschwindigkeitsschätzungen zusätzlich im Speicher gehalten, sodass die Reihenfolge in Echtzeit angepasst wird, ohne auf den nächsten Lauf zu warten.

`urpm server autoconfig` misst die Latenz zu allen Mirror-Kandidaten und persistiert die Ergebnisse, sodass die Server-Reihenfolge schon beim allerersten Download aussagekräftig ist.

### Blacklist und Reputation

Ein Server, der ein korruptes oder unsigniertes RPM ausliefert, wird
**automatisch geblacklistet**: er wird von weiteren Downloads
ausgeschlossen, bis du ihn geprüft und freigegeben hast.
Signatur-Fehlschläge werden als aktive Manipulationssignale gewertet —
kein zeitgesteuerter Auto-Unblock.

Neben der Blacklist pflegt urpm eine gleitende **24-h-Reputation**
(Baseline 100), die bei korrupten Payloads, HTTP-4xx/5xx, Netzfehlern
und langsamen Transfers abfällt. Der Score ordnet den Pool um, ohne
Server ganz auszuschließen.

```bash
urpm server status               # Blacklistete / reputationsschwache Server auflisten
urpm server unblacklist <name>   # Blacklist nach menschlicher Prüfung aufheben
urpm server ack-blacklist <name> # Quittieren (Banner verstummt, ohne aufzuheben)
```

Bei `install` / `upgrade` / `media update` listet ein dauerhaftes
rotes Banner jede nicht-quittierte Blacklist mit
Reaktivierungsanweisungen — das Banner verschwindet nicht von selbst,
nur `unblacklist` oder `ack-blacklist` bringen es zum Schweigen.

`urpm server list` markiert geblacklistete Zeilen rot; ein Blick auf
den Pool reicht, um zu sehen, wer draußen ist.

### Geographische Filterung

Aus der Mageia-Mirror-API entdeckte Server tragen Länder- und Kontinent-Metadaten. Der `[server]`-Konfigurationsabschnitt (siehe unten) erlaubt, akzeptierte Mirrors einzuschränken:

```ini
# /etc/urpm/conf.d/10-server.cfg
[server]
country_blacklist = UA, RU        # Bestimmte Länder ausschließen
continent_whitelist = EU          # Nur europäische Mirrors
```

Die Filterung wird beim Hinzufügen von Mirrors angewendet (`urpm init`, `urpm media autoconfig`, `urpm server autoconfig` und die Hintergrund-Erweiterung des Pools). Server, die bereits in der Datenbank sind, werden beim ersten Lauf mit ihrem Land nachbefüllt; diejenigen, die den Filter nicht bestehen, werden automatisch deaktiviert.

Setze `auto_add = false`, um jedes automatische Hinzufügen von Mirrors zu verhindern.

Nutze `urpm server stats [name]`, um die gesammelten Metriken einzusehen:

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

## Peer-Verwaltung

Wenn urpmd auf mehreren Maschinen im selben LAN läuft, entdecken sie sich gegenseitig und teilen gecachte Pakete (P2P).

```bash
urpm peer list                # Entdeckte Peers auflisten
urpm peer downloads [host]    # Von Peers heruntergeladene Pakete zeigen (nach Host filtern)
urpm peer blacklist <host>    # Einen Peer blockieren (z. B. wenn er schlechte Pakete liefert)
urpm peer unblacklist <host>  # Einen Peer entblocken
urpm peer clean <host>        # Von einem bestimmten Peer geladene RPMs löschen
                              # (nach dem Blacklisten; <host> ist erforderlich)
```

### Nur-lokal-Modus

Nutze `--only-peers`, um exklusiv von LAN-Peers zu laden, ohne auf Upstream-Mirrors zurückzufallen:

```bash
urpm i --only-peers firefox   # Nur installieren, wenn von Peers verfügbar
urpm u --only-peers           # Nur mit Paketen von Peers aktualisieren
urpm download --only-peers pkg  # Nur von Peers laden
```

Nützlich für air-gapped Netze oder wenn du sicherstellen willst, dass alle Pakete aus vertrauenswürdigen lokalen Quellen kommen.

## Cache-Verwaltung

```bash
urpm cache info               # Cache-Info anzeigen
urpm cache clean              # Verwaiste RPMs aus dem Cache entfernen
urpm cache rebuild            # Paketdatenbank aus Synthesis-Dateien neu aufbauen
urpm cache rebuild-fts        # FTS-Index für schnelle Dateisuche neu aufbauen
urpm cache stats              # Detaillierte Statistiken
```

`urpm cache clean` akzeptiert `--dry-run/-n` (Vorschau), `--auto/-y` (keine Bestätigung) und `--verbose/-v` (jede verwaiste Datei auflisten).

## Mirror / Replikation

urpm-ng kann lokal einen Teilbestand an Paketen replizieren (ähnlich einem DVD-Installationsset) und diese den LAN-Peers zur Verfügung stellen. Nützlich für Install-Partys, Offline-Installationen und den Aufbau eines hausinternen Mirrors.

Zwei bewegliche Teile:

- **Politik pro Medium** — `urpm media set <name> --replication=…`
  steuert, wie jedes Medium repliziert wird (nur Metadaten,
  On-Demand-Cache oder vollständiger Seed).
- **Top-Level `urpm mirror`** — daemon-seitiger globaler Zustand
  (Quotas, ausgelieferte Versionen, ausgehende Bandbreitenbegrenzung)
  und explizite Wartungs-Trigger.

### Top-Level-Mirror-Steuerung

```bash
urpm mirror status            # Mirror-Status, Quotas und bediente Versionen anzeigen
urpm mirror enable            # Beginnt, gecachte Pakete an Peers auszuliefern
urpm mirror disable           # Beendet das Ausliefern von Paketen
urpm mirror quota [SIZE]      # Globales Cache-Quota anzeigen oder setzen (z. B. 10G, 500M)
urpm mirror enable-version 10,cauldron   # Auslieferung dieser Versionen wieder aufnehmen
urpm mirror disable-version 8,9          # Auslieferung dieser Versionen beenden
urpm mirror clean [-n]        # Quotas und Retention-Politiken erzwingen (--dry-run Vorschau)
urpm mirror sync [media]      # Replikations-Sync für `seed`-Medien erzwingen
urpm mirror sync --latest-only           # Kleinerer, DVD-artiger Sync
urpm mirror rate-limit [on|off|N/min]    # Ausgehende Bandbreitenbegrenzung konfigurieren
```

### Seed-basierte Replikation

Die Replikation nutzt die `rpmsrate-raw`-Datei von Mageia, um zu bestimmen, welche Pakete gespiegelt werden (dieselbe Logik wie beim DVD-Inhalt).

```bash
# Seed-basierte Replikation auf einem Medium aktivieren
urpm media set "Core Release" --replication=seed
urpm media set "Core Updates" --replication=seed

# Berechnete Seed-Menge einsehen
urpm media seed-info "Core Release"
# Ausgabe:
#   Sektionen: INSTALL, CAT_PLASMA5, CAT_GNOME, …
#   Seed-Pakete aus rpmsrate: 437
#   Locale-Muster: 3
#   Erweiterte Locale-Pakete: +237
#   Mit Abhängigkeiten: 2300 Pakete
#   Geschätzte Größe: ~3,5 GB

# Sync erzwingen (fehlende Pakete laden)
urpm mirror sync

# Nur die neueste Version jedes Pakets synchronisieren (kleiner, DVD-artig)
urpm mirror sync --latest-only
```

### Wie es funktioniert

1. Parst `/usr/share/meta-task/rpmsrate-raw` (aus dem Paket meta-task)
2. Extrahiert Pakete aus den Sektionen: INSTALL, CAT_PLASMA5, CAT_GNOME, CAT_XFCE usw.
3. Expandiert Locale-Muster (z. B. `libreoffice-langpack-ar` → alle Langpacks)
4. Löst Abhängigkeiten auf (Requires + Recommends)
5. Lädt fehlende Pakete parallel herunter

Die Standard-Seed-Sektionen decken alle wichtigen Desktopumgebungen und Anwendungen ab, was ~5 GB Pakete ergibt (vergleichbar mit einer Mageia-DVD).

### Replikations-Politiken

```bash
urpm media set <name> --replication=none       # Nur Metadaten, keine Pakete
urpm media set <name> --replication=on_demand  # Was geladen wird, cachen (Standard)
urpm media set <name> --replication=seed       # DVD-artiger Inhalt aus rpmsrate
```

## Konfiguration

### Blacklist (nie installieren/aktualisieren)

```bash
urpm config blacklist list    # Blacklistete Pakete anzeigen
urpm config blacklist add <pkg>
urpm config blacklist remove <pkg>
```

### Redlist (vor Auto-Remove warnen)

```bash
urpm config redlist list      # Redlistete Pakete anzeigen
urpm config redlist add <pkg>
urpm config redlist remove <pkg>
```

### Kernel-Verwaltung

```bash
urpm config kernel-keep       # Anzeigen, wie viele Kernel behalten werden
urpm config kernel-keep <n>   # Anzahl zu behaltender Kernel setzen
```

### Versionsmodus (System vs. Cauldron)

Wenn System- und Cauldron-Medien beide konfiguriert sind, entscheidet `version-mode`, welches beim Update gewinnt:

```bash
urpm config version-mode              # Aktuellen Modus anzeigen
urpm config version-mode system       # Auf der installierten Systemversion bleiben
urpm config version-mode cauldron     # Mit Cauldron rollen
urpm config version-mode auto         # Explizite Präferenz entfernen
```

### Auto-Upgrade-Hooks für Software-Zentren

Steuert, ob GNOME Software, KDE Discover oder der Offline-Update-Pfad von PackageKit Updates selbst installieren dürfen:

```bash
urpm config gnome-auto-upgrades [yes|no]      # GNOME Software
urpm config discover-auto-upgrades [yes|no]   # KDE Discover
urpm config packagekit-auto-upgrades [yes|no] # PackageKit Offline-Updates
```

Ohne Argument gibt jeder Unterbefehl die aktuelle Einstellung aus. Diese Hooks schalten die desktop-seitigen dconf/PolicyKit-Einstellungen um; die System-Policy wird separat vom Paket `urpm-ng-desktop` durchgesetzt.

### Konfiguration einsehen oder bearbeiten

```bash
urpm config show              # Effektive Konfiguration aus allen *.cfg zeigen
urpm config edit              # urpm.cfg in $EDITOR öffnen
urpm config edit 00-urpmi-compat   # Ein bestimmtes Drop-in öffnen
```

### Server-Auswahl

Der `[server]`-Abschnitt in `/etc/urpm/conf.d/10-server.cfg` steuert die automatische Mirror-Auswahl:

| Schlüssel | Standard | Beschreibung |
|-----------|----------|--------------|
| `auto_add` | `true` | Automatisches Hinzufügen von Mirrors erlauben |
| `country_blacklist` | *(leer)* | Kommagetrennte ISO-3166-Codes zum Ausschluss (z. B. `UA, RU`) |
| `country_whitelist` | *(leer)* | Nur diese Länder akzeptieren (schlägt Blacklist) |
| `continent_blacklist` | *(leer)* | Kontinent-Codes zum Ausschluss (`EU`, `NA`, `SA`, `AS`, `AF`, `OC`) |
| `continent_whitelist` | *(leer)* | Nur diese Kontinente akzeptieren (schlägt Blacklist) |

Ein Mirror muss **beide** Filter (Kontinent und Land) bestehen. Whitelist gewinnt gegenüber Blacklist auf jeder Ebene. Nutze `urpm config show`, um die effektiven Einstellungen zu sehen.

## GPG-Schlüssel

```bash
urpm key list                 # Installierte GPG-Schlüssel auflisten
urpm key import <file|url>    # Einen GPG-Schlüssel importieren
urpm key remove <keyid>       # Einen GPG-Schlüssel entfernen
```

## Build-Abhängigkeiten

Build-Abhängigkeiten für RPM-Erstellung installieren:

```bash
urpm install --buildrequires foo.spec    # Aus Spec-Datei
urpm install --buildrequires foo.src.rpm # Aus Source-RPM
urpm i -b                                # Auto-Detect im RPM-Build-Tree
urpm i --br                              # Kurzalias

# Optionen
--sync                        # Auf alle Scriptlets warten
```

Installierte Build-Abhängigkeiten werden in `/var/lib/rpm/installed-through-builddeps.list` verfolgt und vom regulären Orphan-Entfernen ausgeschlossen. Zum Aufräumen:

```bash
urpm autoremove --buildrequires          # Alle verfolgten Build-Deps entfernen
urpm ar -b                               # Kurzform
```

## Container-Build-System

urpm bietet ein komplettes Container-basiertes Build-System für RPM-Pakete via Docker oder Podman.

### Image-Verwaltung

```bash
# Verfügbare Build-Images auflisten
urpm image list

# Ein bestehendes Image aktualisieren (Medien + Pakete neu synchronisieren)
urpm image update mageia:10-build

# Ein oder mehrere Images löschen
urpm image delete mageia:10-build mageia:10-ci
```

### Build-Image erstellen

```bash
urpm image make --release 10 --tag mageia:10-build
urpm image make --release 10 --tag mageia:10-ci --profile ci

# Image für ein .spec oder .src.rpm (installiert BuildRequires automatisch)
urpm image make --release 10 --tag mga:10-foo --buildrequires SPECS/foo.spec

# Optionen
-r, --release <version>       # Mageia-Version (z. B. 10, cauldron)
-t, --tag <tag>               # Image-Tag (z. B. mageia:10-build)
--profile <name>              # Paket-Profil (Standard: build)
--arch <arch>                 # Zielarchitektur (Standard: Host)
-p, --packages <list>         # Zusätzliche Pakete (kommagetrennt)
--buildrequires <spec|srpm>   # BuildRequires aus einer .spec oder .src.rpm installieren
--addmedia <NAME> <URL>       # Ein zusätzliches Medium im Image hinzufügen (wiederholbar) --
                              # z. B. ein Drittanbieter- oder hausinterner Mirror
--import-key <URL>            # Einen öffentlichen GPG-Schlüssel im Image importieren (wiederholbar) --
                              # kombiniert mit --addmedia für signierte Drittanbieter-Medien
--runtime docker|podman       # Container-Runtime (Standard: Auto-Detect)
--keep-chroot                 # Temporäres Chroot nach Image-Erstellung behalten
-w, --workdir <path>          # Arbeitsverzeichnis für das Chroot (Standard: /tmp)
```

> **Rückwärtskompatibilität:** `urpm mkimage` bleibt als Alias für `urpm image make`.

### Profile

Profile definieren, welche Pakete im Image installiert werden:

| Profil | Beschreibung |
|--------|--------------|
| `build` | RPM-Build-Umgebung (Standard): rpm-build, gcc, make usw. |
| `ci` | CI/Testing: python3-pytest, git, python3-solv usw. |
| `minimal` | Minimales, nutzbares System mit urpm |

Profile werden geladen aus:
- `/usr/share/urpm/profiles/*.yaml` (System, aus dem Paket)
- `/etc/urpm/profiles/*.yaml` (lokale Ergänzungen)

### Pakete bauen

Standardmäßig aktualisiert `urpm build` Medien und Pakete im Container vor dem Bau, damit Builds immer gegen den neuesten Repo-Stand laufen. Nutze `--no-update`, um diesen Schritt zu überspringen — sinnvoll offline oder um wiederholte Builds zu beschleunigen.

```bash
# Aus Source-RPM bauen (Ausgabe nach ./build-output/)
urpm build -i mageia:10-build foo-1.0-1.mga10.src.rpm

# Aus Spec-Datei bauen (Ausgabe nach workspace/RPMS/ und SRPMS/)
urpm build -i mageia:10-build SPECS/foo.spec

# Bauen ohne vorheriges Auto-Update von Medien/Paketen
urpm build -i mga10-build --no-update SPECS/foo.spec

# Mit lokalen Abhängigkeiten bauen (z. B. libfoo aus vorherigem Build)
urpm build -i mageia:10-build SPECS/bar.spec -w 'RPMS/x86_64/libfoo*.rpm'

# Mehrere lokale Abhängigkeiten
urpm build -i mageia:10-build SPECS/app.spec \
    -w 'RPMS/x86_64/libfoo*.rpm' -w 'RPMS/x86_64/libbar*.rpm'

# Mehrere Builds parallel
urpm build -i mageia:10-build *.src.rpm --parallel 4

# Dritt-Builder: Output als foo-1.0-1.mlo.mga10.x86_64.rpm taggen
urpm build -i mageia:10-build --subrel mlo SPECS/foo.spec

# packager/vendor/dist überschreiben, ohne die Spec anzufassen
urpm build -i mageia:10-build --rpmmacros ./my-macros SPECS/foo.spec

# Optionen
-i, --image <tag>             # Zu nutzendes Docker/Podman-Image
-o, --output <dir>            # Ausgabeverzeichnis für SRPM-Builds (Standard: ./build-output)
-w, --with-rpms <pattern>     # Lokale RPMs vor dem Build vorinstallieren (Glob, wiederholbar)
--no-update                   # Auto-Update von Medien und Paketen vor dem Build überspringen
--runtime docker|podman       # Container-Runtime (Standard: Auto-Detect)
-j, --parallel <N>            # Anzahl paralleler Builds (Standard: 1)
--keep-container              # Container nach dem Build behalten (zum Debuggen)
--subrel <tag>                # Injiziert %subrel TAG, sodass die Ausgabe-RPMs zu NAME-VERSION-RELEASE.TAG.DIST.ARCH.rpm werden
--rpmmacros <file>            # Injiziert FILE als /root/.rpmmacros im Build-Container (kombinierbar mit --subrel)
```

### Workspace-Layout

Für Spec-Datei-Builds unterstützt urpm das Standard-RPM-Workspace-Layout:

```
workspace/
├── SPECS/
│   └── foo.spec
└── SOURCES/
    ├── foo-1.0.tar.gz
    └── patches/
```

Die Ergebnisse landen in:
```
workspace/
├── RPMS/
│   └── x86_64/
│       └── foo-1.0-1.mga10.x86_64.rpm
└── SRPMS/
    └── foo-1.0-1.mga10.src.rpm
```

### Beispiel-Workflow

```bash
# 1. Build-Image erstellen (einmalig)
urpm image make --release 10 --tag mga:10-build

# 2. Ein Paket bauen
urpm build --image mga:10-build ./mypackage.src.rpm

# 3. Später das Image aktualisieren, um neue Repo-Pakete aufzunehmen
urpm image update mga:10-build

# 4. Ergebnisse prüfen
ls ./build-output/
```

### Manueller Bootstrap (fortgeschritten)

Unter der Haube ruft `urpm image make` in einem frischen Chroot
`urpm init` auf, um den Medienkatalog zu befüllen. `urpm init` ist
direkt exponiert für Aufrufer, die ein Rootfs außerhalb des
containerisierten Pfads bootstrapen müssen — Installer-Skripte,
VM-Disk-Builds oder vorpräparierte Testwurzeln. Die Mirrors werden
aus der Mageia-Mirror-API geholt und durch die `[server]`-Sektion von
`/etc/urpm/conf.d/10-server.cfg` gefiltert.

```bash
# Ein Chroot-Rootfs für Mageia 10 bootstrappen
urpm --urpm-root /tmp/rootfs init --release 10 --arch x86_64

# Eine eigene Mirrorliste nutzen
urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'

# Optionen
--release, -r <version>     # Ziel-Mageia-Version (10, cauldron, …)
--mirrorlist <url>          # Überschreibt die automatisch erzeugte Mirrorlisten-URL
--arch <arch>               # Zielarchitektur (Standard: Host)
--auto, -y                  # Nicht-interaktiver Modus
--no-sync                   # Medien konfigurieren, aber die initiale Metadaten-Sync auslassen
```

Nachdem du in einem `--urpm-root`-Chroot gearbeitet hast, hänge `/dev` und `/proc` aus, die von `urpm init` gemountet wurden:

```bash
urpm --urpm-root /tmp/rootfs cleanup
```

## Werkzeuge für Repository-Maintainer

Die beiden folgenden Kommandos richten sich an Leute, die ein
Mageia-kompatibles Repository **veröffentlichen**, nicht an solche,
die es konsumieren. Sie werden zusammen dokumentiert, damit klar
bleibt, welches die Client-Metadaten liefert und welches sie erzeugt.

- **`urpm appstream`** (Client-Seite) — frischt den AppStream-Katalog
  auf der aktuellen Maschine auf, damit Software-Zentren aktuelle
  Beschreibungen sehen. Wohnt in `urpm-ng-appstream`.
- **`urpm genmedia`** (Server-Seite) — produziert den vollständigen
  Satz Medien-Metadaten, den ein Mirror seinen Clients ausliefert.
  Wohnt in `urpm-ng-genmedia` als separates Sub-Paket, damit die
  Basis-Client-Installation schlank bleibt.

### AppStream-Metadaten (`urpm appstream`)

urpm kann die AppStream-Kataloge erzeugen und auffrischen, die KDE Discover und GNOME Software konsumieren:

```bash
urpm appstream generate              # Katalog aus der Paketdatenbank erzeugen
urpm appstream generate -m core/release    # Auf ein bestimmtes Medium beschränken
urpm appstream generate --no-compress       # Einfaches XML statt gzip
urpm appstream status                # Katalogstatus pro Medium
urpm appstream merge                 # Per-Medium-Dateien in den einheitlichen Katalog mergen
urpm appstream merge --refresh       # Auch den System-AppStream-Cache auffrischen
urpm appstream init-distro           # OS-Metainfo-Datei erstellen (für Discover/GS nötig)
urpm appstream init-distro --force   # Bestehendes Metainfo überschreiben
```

### Medien-Erzeugung (`urpm genmedia`)

`urpm genmedia` ist das server-seitige Gegenstück zu `urpm appstream`: Wo `appstream` Kataloge konsumiert, um Client-Datenbanken zu füllen, **produziert** `genmedia` den vollständigen Satz von Medien-Metadaten, den ein Mageia-Mirror seinen Clients ausliefert. Es ist eine Python-Neuschreibung des historischen `genhdlist3`, in urpm-ng integriert und separat als `urpm-ng-genmedia` paketiert, damit der Abhängigkeits-Fußabdruck der Basis-Client-Installation nicht wächst.

Aus einem Verzeichnis mit RPM-Dateien:

```bash
urpm genmedia /path/to/rpms          # Standard: vollständige Erzeugung
urpm genmedia /path/to/rpms --incremental   # RPMs überspringen, deren SHA-256 unverändert ist
urpm genmedia /path/to/rpms --no-hdlist     # Die hdlist.cz-Ausgabe überspringen
urpm genmedia /path/to/rpms --xml-info      # Erneute Erzeugung der XML-Info-Dateien erzwingen
urpm genmedia /path/to/rpms --appstream-info  # AppStream-Katalog erzeugen
urpm genmedia /path/to/rpms --no-md5sum     # MD5SUM überspringen (schneller für Tests)
urpm genmedia /path/to/rpms --allow-empty-media  # Leeres Eingabeverzeichnis tolerieren
```

Der Befehl produziert das kanonische Layout, das jeder urpm-ng- oder urpmi-Client erwartet:

```
media_info/
  hdlist.cz                # Komprimierte binäre Paket-Header
  synthesis.hdlist.cz      # Leichte Abhängigkeits-Synthese
  files.xml.lzma           # Per-Paket-Dateilisten
  info.xml.lzma            # URL, sourcerpm, Lizenz, Beschreibung
  changelog.xml.lzma       # Per-Paket-Changelogs
  appstream.xml.gz         # Wenn --appstream-info gesetzt ist
  MD5SUM                   # Checksummen des Obigen
```

Der AppStream-Durchlauf extrahiert die eingebetteten `*.metainfo.xml`-Dateien, die von Upstream-Anwendungen (KDE, GNOME usw.) mitgeliefert werden, und generiert für Pakete ohne solches Metainfo einen minimalen Component aus RPM-Header-Feldern. Pakete, deren Inhalt vollständig nicht-user-facing ist (Devel-Header, Debug-Symbole, statische Archive, reine Runtime-Libraries), werden **gefiltert** statt mit einer Fallback-Kategorie ``System`` ausgeliefert — sie würden Discover und GNOME Software vollstellen, ohne je über einen App-Store installierbar zu sein.

Das Verzeichnis `media_info/` wird während einer Erzeugung gesperrt, sodass Clients, die gleichzeitig lesen, stets einen konsistenten Snapshot sehen.

## Paket-README-Nachrichten

`urpm readme` zeigt die README-Nachrichten der Pakete an, die dem Benutzer während einer Transaktion angezeigt werden (Mageia hält sie als `README.urpmi` / `README.upgrade`):

```bash
urpm readme                          # README der jüngsten Transaktion
urpm readme --transaction <id>       # README einer bestimmten Transaktion
urpm readme --list                   # Transaktionen mit README-Nachrichten auflisten
```

## Orphan-Bereinigung

```bash
urpm cleandeps                # Alias für `urpm autoremove --faildeps`:
                              # entfernt verwaiste Abhängigkeiten, die von
                              # unterbrochenen Transaktionen zurückgelassen wurden.
```

---

# urpmd - Hintergrund-Daemon

urpmd ist ein Hintergrunddienst mit:
- HTTP-API für Paketoperationen
- Geplante Hintergrundaufgaben
- P2P-Peer-Discovery fürs LAN-Paket-Sharing

## API-Endpunkte

### GET-Endpunkte

| Endpunkt | Beschreibung |
|----------|--------------|
| `/` | Service-Info |
| `/api/ping` | Health-Check |
| `/api/status` | Daemon-Status |
| `/api/media` | Konfigurierte Medien auflisten |
| `/api/available` | Verfügbare Pakete auflisten |
| `/api/updates` | Verfügbare Updates auflisten |
| `/api/peers` | Entdeckte LAN-Peers auflisten |

### POST-Endpunkte

| Endpunkt | Beschreibung |
|----------|--------------|
| `/api/refresh` | Medien-Metadaten auffrischen |
| `/api/available` | Verfügbare Pakete abfragen |
| `/api/announce` | Pakete an Peers ankündigen |
| `/api/have` | Abfragen, ob ein Peer bestimmte Pakete hat |

## Geplante Aufgaben

Der Daemon führt automatisch aus:
- Medien-Metadaten-Sync
- Cache-Aufräumen
- Verfügbarkeitsprüfung von Updates
- Peer-Discovery (UDP-Broadcast)

## P2P-Paket-Sharing

Wenn mehrere Maschinen im selben LAN urpmd laufen lassen, entdecken sie sich automatisch und können gecachte RPM-Pakete teilen, was Bandbreitenverbrauch reduziert.

---

# GUI-Integration (Discover / GNOME Software)

urpm-ng liefert ein PackageKit-Backend, mit dem grafische Software-Zentren Pakete verwalten können.

## Installation

```bash
urpm install urpm-ng-desktop
```

Oder das Backend direkt installieren:
```bash
urpm install urpm-ng-packagekit-backend
```

Das installiert:
- `libpk_backend_urpm.so` — PackageKit-Backend
- D-Bus-Service `org.mageia.Urpm.v1` — Privilegierte Operationen
- PolicyKit-Policies — Autorisierungsprompts
- AppStream-Konfiguration — Software-Katalog-Metadaten

## Unterstützte Anwendungen

- **KDE Discover** — Vollständige Unterstützung (Suche, Install, Remove, Updates)
- **GNOME Software** — Vollständige Unterstützung (Suche, Install, Remove, Updates)

## Wie es funktioniert

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
│  D-Bus Service  │
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

Eine dedizierte Qt6-GUI zur Paketverwaltung ist in Entwicklung. Details in `rpmdrake/README.md`.

## Fehlerbehebung

```bash
# Prüfen, ob der D-Bus-Service läuft
systemctl status urpm-dbus.service

# PackageKit-Backend prüfen
pkcon backend-details

# Dienste nach Update neu starten
systemctl restart packagekit.service
systemctl restart urpm-dbus.service

# D-Bus-Interface prüfen
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1
```

---

# Entwicklung & Beitragen

## Voraussetzungen

### Firewall-Ports

Siehe Abschnitt Voraussetzungen für die Netzwerkports fürs P2P-Sharing.

### Umgebung aufsetzen

Das Repository klonen:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

```


### Dev-Modus-Konfiguration

Lege eine Datei `.urpm.local` im Projekt-Root an, um den Dev-Modus zu konfigurieren:

```bash
cd /where/is/urpm-ng

# Dev-Modus (Port 9877, Benutzerdaten in ~/var/lib/urpm-dev/)
# In den Dev-Modus schalten
touch .urpm.local
```

Hinweis, du kannst durch Bearbeiten der `.urpm.local` ändern, wo urpm und urpmd ihre Daten ablegen:
```ini
# Eigenes Basisverzeichnis (optional)
base_dir=/path/lib/urpm-dev
```

Im Dev-Modus werden Daten standardmäßig in `/var/lib/urpm-dev/` gespeichert und der Daemon nutzt Port 9877.

**Beachte: Im Dev-Modus interagiert urpmd nur mit anderen urpmd im Dev-Modus.**

## Den Daemon starten

```bash
# Daemon starten (als root, ohne Hintergrundmodus)

cd /where/is/urpm-ng

./bin/urpmd --dev

```

## urpm starten

```bash
# urpm starten (als root in einer eigenen Konsole)

cd /where/is/urpm-ng

./bin/urpm --help

```

## Coden, testen, beitragen…

Beiträge jeder Art sind willkommen: Code, Tests, Übersetzen, Feedback geben… kein Beitrag ist zu klein.

Siehe `CLAUDE.md` für Entwicklungs-Guidelines und `doc/ARCHITECTURE.md` für die technische Architektur.

---

# Bekannte Probleme / TODO

- **`urpm find`-Performance** — Suche in files.xml ist langsamer als bei urpmf (2,5 s vs. 0,6 s). Braucht Optimierung.

---

# Lizenz

GPL-3.0 — siehe Datei LICENSE für Details.

# Autoren

- Maât (Pascal Vilarem)
- Papoteur (Mageia-Beitragender)
- Claude (KI-Assistent)
