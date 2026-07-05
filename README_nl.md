# urpm-ng

Een moderne pakketbeheerder voor Mageia Linux, geschreven in Python.

urpm-ng is een volledige herschrijving van de klassieke urpmi-gereedschappen, met snellere prestaties, betere afhankelijkheidsresolutie en moderne features zoals P2P-pakketdeling.

## Vereisten

### Distributie

Op dit moment heb je Mageia 9 of Mageia 10 nodig.

### Firewall-poorten (voor P2P-deling)

Het pakket `urpm-ng-daemon` levert `/etc/shorewall/rules.urpm-ng` als
include-bestand mee, en zijn `%post` haakt dat automatisch in
`/etc/shorewall/rules`. Op een door Shorewall beheerde machine (de
Mageia-standaard) staan de volgende poorten dus direct na installatie
open, zonder verdere actie:

- **TCP 9876** (productie) of **TCP 9877** (dev-modus) — urpmd HTTP-API
- **UDP 9878** (productie) of **UDP 9879** (dev-modus) — Peer discovery broadcasts

Draait er geen Shorewall (kale `iptables` / `nftables`), open de poorten
dan met de hand — het bestand `/etc/shorewall/rules.urpm-ng` uit de
broncode dient prima als sjabloon.

## Installatie

### Pakketten

urpm-ng is voor de flexibiliteit opgesplitst in meerdere pakketten:

| Pakket | Omschrijving |
|--------|--------------|
| `urpm-ng-core` | Minimaal: CLI, resolver, database |
| `urpm-ng-daemon` | Achtergronddaemon + P2P-deling |
| `urpm-ng` | Meta: trekt `-core` + `-daemon` binnen (standaardinstallatie) |
| `urpm-ng-appstream` | AppStream-metadata-configuratie (Mageia OS-metainfo, distro-config) |
| `urpm-ng-packagekit-backend` | PackageKit-backend (Discover, GNOME Software) + D-Bus-service |
| `urpm-ng-desktop` | Meta: trekt `-core` + `-daemon` + `-appstream` + `-packagekit-backend` binnen |
| `urpm-ng-build` | Meta: trekt `-core` binnen (voor `urpm image` / `urpm build` — de commando's leven in `-core`) |
| `urpm-ng-genmedia` | Serverzijdige generatie van media-metadata (`urpm genmedia`, voor mirror-beheerders) |
| `urpm-ng-all` | Meta: trekt alles hierboven binnen |

**Het juiste pakket kiezen:**
- **Minimale / container-installatie**: `urpm-ng-core`
- **Standaard CLI-gebruik**: `urpm-ng`
- **Desktop met grafische softwarecentra**: `urpm-ng-desktop`
- **Pakketbouwers (bm-/mkimage-gebruikers)**: `urpm-ng-build`
- **Mirror-beheerders die repositories publiceren**: `urpm-ng-genmedia`

### Snelle installatie / upgrade (`geturpm.sh`)

`geturpm.sh` is de aanbevolen manier om urpm-ng op een verse Mageia te
installeren, en kan een bestaande installatie ook opwaarderen. Het
detecteert de Mageia-release en -architectuur automatisch, haalt het
nieuwste urpm-ng uit het gekozen kanaal en doet het juiste ongeacht of
urpm-ng al geïnstalleerd is of niet (verse machines bootstrappen met
`urpmi`, latere upgrades lopen via urpm-ng zelf).

**Snel — via pipe, geen lokale inspectie**

```bash
curl -fsSL https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh | bash -s -- -y
```

`-y` (of `--yes`) is hier verplicht — het script heeft geen TTY
wanneer het gepipet wordt, en heeft die vlag nodig om bevestigingen
over te slaan. `bash -s --` zegt bash dat het het script van stdin
moet lezen en de rest als script-argumenten moet doorgeven.

**Geverifieerd — downloaden, lezen, dan uitvoeren** (aanbevolen als je
de bron nog niet vertrouwt):

```bash
curl -fsSLO https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh
less geturpm.sh                  # inspecteer voor je uitvoert
bash geturpm.sh                  # interactief: vraagt kanaal en bevestiging
bash geturpm.sh -y               # of niet-interactief
```

**Kanaalkeuze** (`--channel=CHAN`):

- `mgabiz` — haalt uit de Mageia.biz-projectrepo (standaard in
  niet-interactieve modus). Gebruikt `urpm media discover` op de
  mgabiz-mirror, latere updates lopen dus via de standaardflow van
  `urpm media update`.
- `github` — haalt release-RPMs rechtstreeks van de
  GitHub-releasepagina. Handig om een specifieke tag te testen, of
  wanneer de mgabiz-publicatie achterloopt op een release.

In interactieve modus (geen `--channel`, TTY aanwezig, geen `-y`)
vraagt het script welk kanaal je wilt gebruiken.

Noot: bij de eerste installatie importeert urpm-ng zijn configuratie
automatisch uit bestaande `urpmi.cfg`- en `urpmi/skip.list`-bestanden.

## Eerste opstart

urpm werkt out of the box. Geavanceerde opties (blacklist, redlist, kernel-keep) staan verderop onder **Configuratie**.

Bij een systeeminstallatie (in `/usr/bin/`) gebruikt urpm:
- Database: `/var/lib/urpm/packages.db`
- Daemon-poort: 9876
- PID-bestand: `/run/urpmd.pid`

### Mediabronnen

Bij een installatie via het RPM-pad (of via `geturpm.sh`) worden de
standaard Mageia-media en de bijbehorende servers automatisch opgezet:
`urpm-ng` importeert de bestaande `urpmi.cfg` bij de eerste run en
`urpm server autoconfig` vult de mirrorpool vanuit de Mageia-mirror-API.
Meer is er niet nodig om pakketten te installeren.

Op een machine zonder eerdere `urpmi.cfg` (verse chroot, image-build, of
een systeem dat nooit urpmi heeft gehad) draai je dezelfde bootstrap in
één handmatige doorloop:

```bash
urpm media list                       # Nog niets? Bootstrap:
urpm media import                     # Leest standaard /etc/urpmi/urpmi.cfg; no-op als afwezig
urpm server autoconfig                # Trek mirrors uit de Mageia-API
urpm media update                     # Eerste metadata-sync
```

Voor een **community-repository** (MageiaLinux-Online, mageia.biz,
blogdrake, een interne mirror, …) gebruik je `urpm media discover` —
het leest de `media.cfg` van de repo en voegt in één aanroep alle
aangekondigde media toe:

```bash
urpm media discover https://www.mageia.biz/repo/Mageia/mgabiz/10/x86_64/media/
urpm media discover --dry-run https://download.mageialinux-online.org/...   # Voorbeeld
```

`urpm media add` is voorbehouden aan één enkel, niet-discover-conform
custom-medium — dat wil zeggen een medium waarvan je weet dat het niet
via een `media.cfg` wordt gepubliceerd. Zie het onderdeel
**Mediabeheer** verderop voor de syntaxis.

---

# urpm - Opdrachtregel-interface

## Globale opties

Deze opties gelden voor de meeste commando's en staan vóór het subcommando:

```bash
-V, --version              # urpm-versie tonen
-v, --verbose              # Uitgebreide uitvoer
-q, --quiet                # Stille uitvoer
--nocolor                  # Kleuruitvoer uitschakelen
--root DIR                 # Gebruik DIR als root voor RPM-installatie (chroot, urpm-config van host)
--urpm-root DIR            # Gebruik DIR als root voor zowel urpm-config als RPM-installatie
```

De volgende ouders worden geërfd door transactie- en query-commando's
(`install`, `upgrade`, `erase`, `download`, `depends`, …):

```bash
--arch ARCH                # Doelarchitectuur (standaard: huidig systeem)
--debug COMPONENT          # Debug-uitvoer aan: solver, tsrun, orphans, download, timing, all
--watched PACKAGES         # Kommagescheiden pakketnamen om te volgen tijdens resolutie
```

Noot: `--arch` (ouderoptie, zet de doelarchitectuur voor de operatie)
is iets anders dan `--allow-arch` (per-aanroep-optie op
install/upgrade/download, staat extra architecturen toe naast de
systeemarchitectuur — meestal `i686` voor wine/steam op x86_64).

## Weergave-opties

De meeste commando's ondersteunen deze uitvoeropties:

```bash
--show-all            # Alle items zonder afkapping tonen
--flat                # Eén item per regel (parsebaar door scripts)
--json                # JSON-uitvoer (voor programmatisch gebruik)
```

Standaard worden lange lijsten in meerdere kolommen weergegeven en afgekapt op 10 regels met "… en N meer". Gebruik `--show-all` om alles te zien.

Voorbeelden:
```bash
urpm list installed --flat          # Eén pakket per regel
urpm search firefox --json          # JSON-uitvoer
urpm i task-plasma --show-all       # Alle afhankelijkheden tonen
```

## Atomische vs best-effort-transacties

Sinds 0.7.9 draait `urpm upgrade` standaard in **best-effort**-modus:
pakketten waarvan de afhankelijkheden niet vervuld kunnen worden,
worden uit de transactie geschrapt en op het einde met reden gemeld
(ontbrekende afhankelijkheid, versiemismatch, SRPM-broertjescascade,
…). De transactie wordt gecommit voor al de rest. Geef `--atomic` mee
om over te schakelen naar strict-modus (aanbevolen op servers): elk
onoplosbaar pakket breekt de hele transactie af.

`urpm install` daarentegen is **standaard atomisch**: als een gevraagd
pakket niet geïnstalleerd kan worden, wordt de hele transactie
teruggerold. Geef `--no-atomic` mee om voor het install-pad in
best-effort-modus te werken.

## Exit-codes

| Code | Betekenis |
|------|-----------|
| 0    | Transactie geslaagd, geen pakket overgeslagen |
| 1    | Harde fout: transactie afgebroken (atomic-modus, netwerk, rechten, …) |
| 2    | Deeltransactie: geslaagd, maar minstens één pakket werd geschrapt (overgeslagen pakketten met reden op stderr) |

Scriptbare check voor het deelgeval:

```bash
urpm upgrade --auto || [ $? -eq 2 ] && echo "ok of gedeeltelijk"
```

## Pakketbeheer

### Pakketten installeren

```bash
urpm install <pakket>         # Een pakket installeren
urpm i <pakket>               # Korte alias

# Opties
--auto, -y                    # Niet-interactieve modus
--test                        # Dry-run (simulatie)
--without-recommends          # Aanbevolen pakketten overslaan
--with-suggests               # Ook voorgestelde pakketten installeren
--force                       # Forceren ondanks afhankelijkheidsproblemen
--reinstall                   # Reeds geïnstalleerde pakketten opnieuw installeren (reparatie)
--nosignature                 # GPG-verificatie overslaan (niet aanbevolen)
--noscripts                   # Pre-/post-install-scripts overslaan (chroot-/container-builds)
--no-peers                    # P2P-download vanaf LAN-peers uitschakelen
--only-peers                  # Alleen van LAN-peers laden, geen upstream-mirrors
--no-atomic                   # Best-effort-modus (standaard is atomisch bij install)
--download-only               # Naar cache laden, niet installeren
--nodeps                      # Afhankelijkheidsresolutie overslaan (met --download-only)
--all                         # Installeren voor alle matchende families (bv. php8.4 + php8.5)
--install-src                 # Source-RPM installeren (spec/sources uitpakken naar ~/rpmbuild/)
--config-policy {keep,replace,ask}  # Conflictbeleid voor config-bestanden (standaard: keep)
--prefer=<prefs>              # Alternatievenkeuze sturen (zie hieronder)
--allow-arch <arch>           # Extra architecturen toestaan (bv. i686 voor wine/steam)
--sync                        # Wachten op volledige afronding (post-install-triggers)
```

#### Voorkeurgestuurde installatie

Bij het installeren van pakketten met alternatieven (bv. phpmyadmin, dat verschillende PHP-versies en webservers kan gebruiken) stuur je de keuze met `--prefer`:

```bash
# PHP 8.4 met Apache en php-fpm verkiezen, mod_php uitsluiten
urpm i phpmyadmin --prefer=php:8.4,apache,php-fpm,-apache-mod_php

# nginx in plaats van apache
urpm i phpmyadmin --prefer=php:8.4,nginx,php-fpm
```

Voorkeurssyntaxis:
- `capability:version` — Versiebeperking (bv. `php:8.4`)
- `pattern` — Pakketten verkiezen die deze capability leveren (bv. `apache`, `php-fpm`)
- `-pattern` — Pakketten die op dit patroon matchen benadelen (bv. `-apache-mod_php`)

Voorkeuren werken op REQUIRES en PROVIDES van pakketten, niet op de namen.

#### Architectuurfiltering

Standaard beschouwt urpm alleen pakketten die matchen met je systeemarchitectuur en `noarch`. Dat voorkomt dat je per ongeluk i686-pakketten installeert op een x86_64-systeem wanneer 32-bit-media aan staan.

Om 32-bit-pakketten (wine, steam, multilib) te installeren:

```bash
urpm install wine --allow-arch i686
urpm install steam --allow-arch i686

# Meerdere architecturen
urpm install mijnpakket --allow-arch i686 --allow-arch armv7hl
```

### Pakketten verwijderen

```bash
urpm erase <pakket>           # Een pakket verwijderen
urpm e <pakket>               # Korte alias

# Opties
--auto, -y                    # Niet-interactieve modus
--test                        # Dry-run (simulatie)
--auto-orphans                # Ook verweesde afhankelijkheden verwijderen (impliciet bij -y, tenzij --keep-orphans)
--keep-orphans                # Verweesde afhankelijkheden behouden
--erase-recommends            # Ook enkel aanbevolen (niet vereiste) pakketten verwijderen
--keep-suggests               # Pakketten die door resterende pakketten worden voorgesteld behouden
--force                       # Forceren ondanks afhankelijkheidsproblemen
--debug {solver,tsrun,all}    # Debug-uitvoer voor resolver/transactie
--sync                        # Wachten op volledige afronding (post-uninstall-triggers)
```

### Metadata bijwerken (apt-stijl)

```bash
urpm update                   # Alle media-metadata bijwerken
urpm update "Core Release"    # Specifiek medium bijwerken
```

Sinds 0.7.x wordt `files.xml.lzma` opgehaald naast `synthesis.hdlist.cz` telkens wanneer het medium het publiceert — geen opt-in-vlag nodig.

### Pakketten downloaden (zonder te installeren)

```bash
urpm download <pakket>        # Een pakket naar de cache laden
urpm dl <pakket>              # Korte alias
urpm download --only-peers pkg  # Enkel van LAN-peers

# Opties
--release, -r <version>       # Doelrelease voor cross-release-downloads (bv. cauldron)
--buildrequires, --br [SPEC]  # Build-afhankelijkheden laden (auto-detect of uit .spec/.src.rpm)
--without-recommends          # Aanbevolen pakketten overslaan
--nodeps                      # Enkel de opgegeven pakketten, geen afhankelijkheden
--no-peers / --only-peers     # Zoals bij install (peer-beleid)
--allow-arch <arch>           # Extra architecturen toestaan
--arch <arch>                 # Geërfd: doelarchitectuur
--show-all                    # Volledige lijst van opgeloste pakketten tonen
                              # (standaard kapt af op 20 met "… en N meer")
```

### Pakketten upgraden

```bash
urpm upgrade                  # Alle pakketten upgraden
urpm u                        # Korte alias
urpm upgrade <pakket>         # Specifieke pakketten upgraden

# Opties
--auto, -y                    # Niet-interactieve modus
--test                        # Dry-run (simulatie)
--atomic                      # Strict-modus: breekt de hele transactie af bij één onoplosbaar pakket.
                              # Standaard is best-effort (zie "Atomische vs best-effort-transacties" hierboven).
--with-recommends             # Aanbevolen pakketten installeren
--with-suggests               # Ook voorgestelde pakketten installeren
--noerase-orphans             # Verweesde afhankelijkheden behouden (niet verwijderen)
--download-only               # Naar cache laden zonder de upgrade toe te passen
--nosignature                 # GPG-verificatie overslaan (niet aanbevolen)
--no-peers / --only-peers     # Uitschakelen / beperken tot LAN-peers
--force                       # Upgrade forceren ondanks afhankelijkheidsproblemen
--config-policy {keep,replace,ask}  # Conflictbeleid voor config-bestanden (standaard: keep)
--allow-arch <arch>           # Extra architecturen toestaan (bv. i686)
--sync                        # Wachten op volledige afronding (post-install-triggers)
```

### Wezen automatisch verwijderen

```bash
urpm autoremove               # Ongebruikte afhankelijkheden verwijderen (standaard: --orphans)
urpm ar                       # Korte alias

# Selectoren
--orphans, -o                 # Verweesde pakketten (standaard)
--kernels, -k                 # Oude kernels
--faildeps, -f                # Deps uit onderbroken transacties
--buildrequires, -b           # Build-afhankelijkheden (--builddeps, --br)
--all, -a                     # Al het bovenstaande

# Opties
--auto, -y                    # Niet-interactieve modus
```

## Zoeken en opvragen

### Pakketten zoeken

```bash
urpm search <patroon>         # Zoek op naam/samenvatting
urpm s <patroon>              # Korte alias
urpm q <patroon>              # Query-alias (urpmq-compatibiliteit)

# Opties
--installed                   # Alleen zoeken tussen geïnstalleerde pakketten
--unavailable                 # Geïnstalleerde pakketten opsommen die in geen enkel medium meer voorkomen
```

#### Niet-beschikbare pakketten vinden

Toont pakketten die geïnstalleerd zijn maar in geen enkel geconfigureerd medium meer beschikbaar zijn (zoals `urpmq --unavailable`):

```bash
urpm q --unavailable          # Alle niet-beschikbare pakketten opsommen
urpm q --unavailable php      # Filter op patroon
```

### Pakketinfo tonen

```bash
urpm show <pakket>            # Pakketdetails tonen
urpm info <pakket>            # Alias
```

### Pakketten opsommen

```bash
urpm list installed           # Geïnstalleerde pakketten opsommen
urpm list available           # Beschikbare pakketten opsommen
urpm list updates             # Beschikbare updates opsommen
urpm list upgradable          # Alias voor updates
```

### Afhankelijkheden

```bash
urpm depends <pakket>         # Wat een pakket vereist tonen
urpm rdepends <pakket>        # Wat een pakket vereist (omgekeerde deps) tonen
urpm why <pakket>             # Uitleggen waarom een pakket geïnstalleerd is

# Opties voor depends
--tree                        # Afhankelijkheidsboom tonen
--prefer=<prefs>              # Filteren op voorkeuren (zelfde syntaxis als install)
--legend                      # Symboollegende na de boomweergave tonen

# Opties voor rdepends
--tree                        # Omgekeerde afhankelijkheidsboom tonen
--all                         # Alle recursieve omgekeerde deps (plat) tonen
--depth=N                     # Maximale boomdiepte (standaard: 3)
--hide-uninstalled            # Enkel paden naar geïnstalleerde pakketten tonen
--legend                      # Symboollegende na de boomweergave tonen
```

Voorbeeld met voorkeuren:
```bash
# Toon phpmyadmin-afhankelijkheden met voorkeur voor PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

Voorbeeld met rdepends:
```bash
# Omgekeerde afhankelijkheidsboom voor rtkit, diepte 10, enkel geïnstalleerde paden
urpm rdepends --tree --hide-uninstalled --depth=10 rtkit
```

### Zwakke afhankelijkheden

```bash
urpm recommends <pakket>      # Pakketten die door een pakket worden aanbevolen
urpm whatrecommends <pakket>  # Pakketten die een pakket aanbevelen
urpm suggests <pakket>        # Pakketten die door een pakket worden voorgesteld
urpm whatsuggests <pakket>    # Pakketten die een pakket voorstellen
```

### Bestand-queries

```bash
urpm provides <pakket>        # Bestanden die door een pakket worden geleverd opsommen
urpm whatprovides <bestand>   # Vinden welk pakket een bestand levert
urpm find <patroon>           # Bestanden in pakketten zoeken (geïnstalleerd + beschikbaar)
urpm find -i <patroon>        # Enkel in geïnstalleerde pakketten zoeken
urpm find -a <patroon>        # Enkel in beschikbare pakketten zoeken
urpm find <patroon> --all-versions  # Elke EVR meenemen die de match levert
urpm find <patroon> --limit 500     # De standaardlimiet van 100 treffers verhogen
```

`urpm find` doorzoekt standaard zowel geïnstalleerde als beschikbare pakketten. `files.xml.lzma` wordt automatisch opgehaald als onderdeel van elke `urpm media update` (voor zover het medium het aankondigt in `MD5SUM`), er is dus geen opt-in meer nodig — de toggle `--sync-files` is in 0.7.x verwijderd.

## Pakket-markering

```bash
urpm mark manual <pakket>     # Als handmatig geïnstalleerd markeren
urpm mark auto <pakket>       # Als automatisch geïnstalleerd markeren (afhankelijkheid)
urpm mark show <pakket>       # Installatiereden tonen
```

## Pakket-holds

Pakketten vastzetten om upgrades en vervanging door obsoletes te verhinderen:

```bash
urpm hold <pakket>            # Een pakket vastzetten
urpm hold <pakket> -r "reden" # Met reden vastzetten
urpm hold                     # Vastgezette pakketten opsommen
urpm unhold <pakket>          # Hold weghalen
```

Vastgezette pakketten zijn beschermd tegen:
- Versie-upgrades tijdens `urpm upgrade`
- Vervanging door pakketten die ze obsoleet maken

Voorbeeld:
```bash
# dhcpcd maakt dhcp-client obsoleet, maar je wil dhcp-client houden
urpm hold dhcp-client -r "Prefer dhcp-client over dhcpcd"

# Nu slaat urpm upgrade dhcp-client over en waarschuwt:
#   Vastgezette pakketten (1) overgeslagen:
#     dhcp-client (zou geobsoleteerd worden door dhcpcd)

# Om de vervanging later toch toe te staan:
urpm unhold dhcp-client
```

## Geschiedenis en ongedaan maken

```bash
urpm history                  # Transactiegeschiedenis tonen (laatste 20)
urpm history -i               # Filter: enkel install-transacties
urpm history -r               # Filter: enkel remove-transacties
urpm history -d <id>          # Details van transactie <id> tonen
urpm history --delete <id>... # Transacties uit het logboek verwijderen

urpm undo [id]                # Een transactie ongedaan maken (standaard: de laatste).
                              # Zet een net historiek-item. Gebruik --auto/-y om de prompt over te slaan.

urpm rollback <n>             # De laatste n transacties terugdraaien
urpm rollback to <id>         # Terug naar een specifieke transactie
urpm rollback to <date>       # Terug naar een datum (JJJJ-MM-DD of DD/MM/JJJJ)
```

## Achtergrondtransacties

Wanneer een transactie losgekoppeld draait (bv. via de daemon of PackageKit), volg je de voortgang met:

```bash
urpm progress                 # Huidige voortgang tonen en stoppen
urpm progress --watch         # Doorlopend volgen tot afronding
```

## Mediabeheer

```bash
urpm media list               # Geconfigureerde media opsommen
urpm media add <url>          # Een officieel Mageia-medium toevoegen (auto-parsed)
urpm media add --custom "Naam" kortnaam <url>  # Een eigen/derdenmedium toevoegen
urpm media remove <naam>...   # Eén of meerdere media verwijderen
urpm media remove --all       # ELK geconfigureerd medium verwijderen (vraagt om
                              # bevestiging; met -y/--auto sla je die over).
                              # Verweesde servers (geen media meer) worden
                              # in dezelfde doorloop opgeruimd.
urpm media enable <naam>      # Een medium activeren
urpm media disable <naam>     # Een medium deactiveren
urpm media update [naam]      # Media-metadata bijwerken
urpm media import <bestand>   # Uit urpmi.cfg importeren
urpm media link <naam> +srv -srv  # Servers aan een medium koppelen/ontkoppelen
urpm media set <naam> [opts]  # Media-instellingen wijzigen (sharing, replicatie, quota…)
urpm media seed-info <naam>   # Seed-info tonen (secties, aantal pakketten, geschatte omvang)
urpm media autoconfig -r 10   # Officiële Mageia-media voor release 10 automatisch toevoegen
urpm media discover <url>     # Media ontdekken uit de media.cfg van een repo
```

Nuttige vlaggen voor `urpm media add`:

```bash
--import-key                  # De door het medium aangekondigde GPG-sleutel importeren
--allow-unsigned              # Onondertekende pakketten toestaan (enkel eigen media)
--version <ver>               # Doel-Mageia-versie (enkel eigen media: 9, 10, cauldron…)
--update                      # Als update-medium markeren
--disabled                    # Toevoegen maar gedeactiveerd laten
-y, --auto                    # Niet-interactief: de auto-gedetecteerde naam/short_name aanvaarden
```

### Media importeren uit een oude urpmi.cfg

Migreer een bestaande Mageia-machine van `urpmi` naar urpm-ng zonder
elke mediabron handmatig toe te voegen. Zowel URL-gebaseerde items als
`MIRRORLIST=`-items worden geïmporteerd — die laatste als pending
media waaraan `urpm server autoconfig` bij de volgende run servers
koppelt.

```bash
urpm media import /etc/urpmi/urpmi.cfg    # Standaard bronpad
urpm media import                          # Idem (pad is standaard /etc/urpmi/urpmi.cfg)

# Opties
--replace                     # Bestaande media met dezelfde short_name overschrijven
-r, --release <version>       # Doel-Mageia-release (standaard: waarde van /etc/mageia-release)
--arch <arch>                 # Doelarchitectuur (standaard: `uname -m`)
-y, --auto                    # Niet-interactief: bevestigingsprompt overslaan
```

### Media ontdekken uit een repository

Ontdek alle beschikbare media uit een Mageia-compatibele repository
(officiële mirrors, community-repo's zoals MLO, bedrijfsmirrors):

```bash
urpm media discover https://repo.example.org/9/x86_64/media/       # Alle media toevoegen
urpm media discover --dry-run https://repo.example.org/9/x86_64/media/  # Enkel voorbeeld
urpm media discover --sources --debug https://...                   # SRPMS en debug meenemen

# Categorieën geforceerd aan-/uitzetten (nonfree, tainted, 32bit, all)
urpm media discover --with nonfree,tainted https://...
urpm media discover --without nonfree https://...
urpm media discover --with all https://...
```

Het commando haalt `media.cfg` van de repository, ontdekt alle media en koppelt bestaande servers die dezelfde inhoud hosten (geverifieerd via de MD5-checksum van `synthesis.hdlist.cz`).

### Server-mediumkoppeling

Servers aan specifieke mediabronnen koppelen of ontkoppelen:

```bash
urpm media link "Core Release" +mirror1 +mirror2   # Servers toevoegen
urpm media link "Core Updates" -oldserver          # Server verwijderen
urpm media link "Core Release" +all                # Alle beschikbare servers toevoegen
urpm media link "Core Release" -all +preferred     # Reset en er één toevoegen
```

Noot: bij het toevoegen van servers verifieert urpm dat de mediumsinhoud matcht door MD5-checksums van `synthesis.hdlist.cz` te vergelijken met bestaande referentieservers.

### Media automatisch configureren

Officiële Mageia-media voor een release automatisch toevoegen:

```bash
urpm media autoconfig --release 10              # Alle officiële media voor Mageia 10 toevoegen
urpm media autoconfig -r cauldron               # Media voor Cauldron toevoegen
urpm media autoconfig -r 10 --no-nonfree        # Nonfree-media overslaan
urpm media autoconfig -r 10 --no-tainted        # Tainted-media overslaan
urpm media autoconfig -r 10 -n                  # Dry-run: wat zou toegevoegd worden
```

### Media-instellingen

Media-sharing en replicatie configureren:

```bash
urpm media set "Core Release" --shared=yes           # Met P2P-peers delen
urpm media set "Core Release" --replication=seed     # Volledige replicatie (DVD-achtig)
urpm media set "Core Release" --replication=on_demand  # Gedownloade pakketten cachen
urpm media set "Core Release" --quota=5G             # Cache-grootte begrenzen
urpm media set "Core Release" --retention=30         # Pakketten 30 dagen bewaren
urpm media set "Core Release" --priority=10          # Hogere prioriteit
urpm media set "Core Release" --seeds=INSTALL,CAT_PLASMA5  # Seed-secties
```

Voorbeelden:
```bash
# Officieel Mageia-medium toevoegen (server en medium auto-gedetecteerd)
urpm media add https://ftp.belnet.be/mageia/distrib/9/x86_64/media/core/release/

# Custom derdenmedium toevoegen
urpm media add --custom "RPM Fusion" rpmfusion https://download1.rpmfusion.org/free/fedora/40/x86_64/os/
```

## Serverbeheer

Servers zijn mirrorbronnen die meerdere media kunnen bedienen. urpm ondersteunt meerdere servers per medium voor load balancing en failover.

```bash
urpm server list              # Geconfigureerde servers opsommen (met land)
urpm server add <naam> <url>  # Een server toevoegen (test IP en scant media)
urpm server remove <naam> ... # Eén of meerdere servers verwijderen
urpm server enable <naam>     # Een server activeren
urpm server disable <naam>    # Een server deactiveren
urpm server priority <naam> <n>  # Serverprioriteit zetten (hoger = verkozen)
urpm server test [naam]       # Connectiviteit testen en IP-modus detecteren
urpm server ip-mode <naam> <mode>  # IP-modus zetten (auto/ipv4/ipv6/dual)
urpm server autoconfig        # Servers automatisch toevoegen uit de Mageia-mirror-API
urpm server stats [naam]      # Prestatiestatistieken van een server tonen
urpm server status            # Geblacklistete / reputatiezwakke servers tonen
urpm server unblacklist <naam>   # Blacklist van een server opheffen (na review)
urpm server ack-blacklist <naam>  # Een blacklist bevestigen (banner valt stil, blijft geblokkeerd)
```

### Server-lijst

Opties voor urpm server list:
```bash
--all                 # Alle servers tonen, ook gedeactiveerde
```

### IP-modus

Elke server heeft een IP-modus om met IPv4/IPv6-connectiviteit om te gaan:
- `auto` — Systeem beslist (kan 30 s timeout geven als IPv6 faalt)
- `ipv4` — Enkel IPv4
- `ipv6` — Enkel IPv6
- `dual` — Beide werken, IPv4 wordt verkozen (aanbevolen voor dual-stack-servers)

De IP-modus wordt bij het toevoegen van een server automatisch gedetecteerd. Gebruik `server test` om opnieuw te detecteren of `server ip-mode` om handmatig te zetten.

### Bandbreedte-tracking en automatische failover

urpm volgt automatisch de downloadprestaties van elke server. Na elke
download of metadata-sync wordt de gemeten snelheid geregistreerd met
een EWMA (Exponentially Weighted Moving Average, α=0,3). Die traagheid
zorgt dat één trage transfer een goede server niet onterecht bestraft.

Servers worden geprobeerd in de volgorde `priority DESC, bandwidth_kbps DESC`:
faalt een server tijdens een download of metadata-sync, dan wordt
automatisch de volgende best geprobeerd, zonder tussenkomst van de
gebruiker. Binnen één sessie worden snelheidsschattingen per server ook
in geheugen bijgehouden, zodat de volgorde zich in real time aanpast
zonder te wachten op de volgende run.

`urpm server autoconfig` meet de latency naar alle mirror-kandidaten en
bewaart de resultaten, zodat de servervolgorde vanaf de allereerste
download zinvol is.

### Blacklist en reputatie

Een server die een corrupt of niet-ondertekend RPM levert, wordt
**automatisch geblacklist**: hij wordt uitgesloten van verdere
downloads tot je hem hebt nagekeken en vrijgegeven. Handtekeningfouten
worden behandeld als actieve manipulatiesignalen — geen tijdgestuurde
auto-unblock.

Naast de blacklist houdt urpm een schuivende **24 u-reputatie**-score
bij (basislijn 100), die daalt bij corrupte payloads, HTTP-4xx/5xx,
netwerkfouten en trage transfers. Die score herordent de pool zonder
servers volledig uit te sluiten.

```bash
urpm server status               # Geblacklistete / reputatiezwakke servers opsommen
urpm server unblacklist <naam>   # Blacklist opheffen na menselijke review
urpm server ack-blacklist <naam> # Bevestigen (banner stopt, blokkering blijft)
```

Bij `install` / `upgrade` / `media update` toont een blijvende rode
banner elke niet-bevestigde blacklist met heractivatie-instructies —
de banner verdwijnt niet vanzelf, enkel `unblacklist` of
`ack-blacklist` doet hem zwijgen.

`urpm server list` markeert geblacklistete rijen in het rood, zodat één
blik op de pool volstaat om te zien wie eruit ligt.

### Geografische filtering

Servers die uit de Mageia-mirror-API worden ontdekt, dragen land- en
continent-metadata. Het `[server]`-configuratieblok (zie hieronder)
laat je beperken welke mirrors aanvaard worden:

```ini
# /etc/urpm/conf.d/10-server.cfg
[server]
country_blacklist = UA, RU        # Specifieke landen uitsluiten
continent_whitelist = EU          # Enkel Europese mirrors
```

De filtering wordt toegepast wanneer mirrors worden toegevoegd (`urpm
init`, `urpm media autoconfig`, `urpm server autoconfig` en de
achtergronduitbreiding van de pool). Servers die al in de database
zitten worden bij de eerste run met hun land aangevuld; wie de filter
niet doorstaat wordt automatisch gedeactiveerd.

Zet `auto_add = false` om elke automatische mirrortoevoeging te
verhinderen.

Gebruik `urpm server stats [naam]` om de verzamelde metrieken te bekijken:

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

## Peer-beheer

Wanneer urpmd draait op meerdere machines in hetzelfde LAN, ontdekken ze elkaar en delen ze gecachete pakketten (P2P).

```bash
urpm peer list                # Ontdekte peers opsommen
urpm peer downloads [host]    # Pakketten getoond die van peers zijn gedownload (filter op host)
urpm peer blacklist <host>    # Een peer blokkeren (bv. als hij slechte pakketten levert)
urpm peer unblacklist <host>  # Een peer deblokkeren
urpm peer clean <host>        # RPMs die van een bepaalde peer kwamen verwijderen
                              # (nadat je hebt geblacklist; <host> is verplicht)
```

### Lokaal-modus

Gebruik `--only-peers` om exclusief van LAN-peers te downloaden, zonder terugval op upstream-mirrors:

```bash
urpm i --only-peers firefox   # Enkel installeren als beschikbaar bij peers
urpm u --only-peers           # Enkel upgraden met pakketten van peers
urpm download --only-peers pkg  # Enkel van peers laden
```

Handig voor air-gapped netwerken of wanneer je wil garanderen dat alle pakketten uit vertrouwde lokale bronnen komen.

## Cachebeheer

```bash
urpm cache info               # Cache-info tonen
urpm cache clean              # Verweesde RPMs uit de cache verwijderen
urpm cache rebuild            # Pakketdatabase opnieuw opbouwen uit synthesis-bestanden
urpm cache rebuild-fts        # FTS-index voor snelle bestand-zoekopdrachten herbouwen
urpm cache stats              # Gedetailleerde statistieken
```

`urpm cache clean` aanvaardt `--dry-run/-n` (voorbeeld), `--auto/-y`
(geen bevestiging) en `--verbose/-v` (elke verweesde bestand opsommen).

## Mirror / Replicatie

urpm-ng kan lokaal een deelverzameling pakketten repliceren (vergelijkbaar met een DVD-installatieset) en die aan LAN-peers aanbieden. Nuttig voor installfeesten, offline installaties en het opzetten van een interne mirror.

Twee bewegende delen:

- **Beleid per medium** — `urpm media set <naam> --replication=…`
  stuurt hoe elk medium gerepliceerd wordt (enkel metadata,
  on-demand caching of volledige seed).
- **Top-level `urpm mirror`** — daemon-zijdige globale toestand
  (quota's, bediende versies, uitgaande snelheidsbegrenzing) en
  expliciete onderhoudstriggers.

### Mirror-besturing op topniveau

```bash
urpm mirror status            # Mirror-status, quota's en bediende versies tonen
urpm mirror enable            # Begin gecachete pakketten aan peers te leveren
urpm mirror disable           # Stop met pakketten te leveren
urpm mirror quota [SIZE]      # Globale cache-quota tonen of zetten (bv. 10G, 500M)
urpm mirror enable-version 10,cauldron   # Levering van deze versies hervatten
urpm mirror disable-version 8,9          # Levering van deze versies stoppen
urpm mirror clean [-n]        # Quota's en retentiebeleid afdwingen (--dry-run voorbeeld)
urpm mirror sync [medium]     # Replicatie-sync forceren voor `seed`-media
urpm mirror sync --latest-only           # Kleinere, DVD-achtige sync
urpm mirror rate-limit [on|off|N/min]    # Uitgaande snelheidsbegrenzing configureren
```

### Seed-gebaseerde replicatie

De replicatie gebruikt het `rpmsrate-raw`-bestand van Mageia om te bepalen welke pakketten gespiegeld worden (dezelfde logica als de DVD-inhoud).

```bash
# Seed-gebaseerde replicatie op een medium aanzetten
urpm media set "Core Release" --replication=seed
urpm media set "Core Updates" --replication=seed

# De berekende seed-set bekijken
urpm media seed-info "Core Release"
# Uitvoer:
#   Secties: INSTALL, CAT_PLASMA5, CAT_GNOME, …
#   Seed-pakketten uit rpmsrate: 437
#   Locale-patronen: 3
#   Uitgebreide locale-pakketten: +237
#   Met afhankelijkheden: 2300 pakketten
#   Geschatte omvang: ~3,5 GB

# Sync forceren (ontbrekende pakketten laden)
urpm mirror sync

# Enkel de nieuwste versie van elk pakket synchroniseren (kleiner, DVD-achtig)
urpm mirror sync --latest-only
```

### Hoe het werkt

1. Parst `/usr/share/meta-task/rpmsrate-raw` (uit het pakket meta-task)
2. Extraheert pakketten uit de secties: INSTALL, CAT_PLASMA5, CAT_GNOME, CAT_XFCE, enz.
3. Breidt locale-patronen uit (bv. `libreoffice-langpack-ar` → alle langpacks)
4. Lost afhankelijkheden op (Requires + Recommends)
5. Downloadt ontbrekende pakketten parallel

De standaard seed-secties dekken alle grote desktopomgevingen en applicaties, wat op ~5 GB aan pakketten uitkomt (vergelijkbaar met een Mageia-DVD).

### Replicatiebeleid

```bash
urpm media set <naam> --replication=none       # Enkel metadata, geen pakketten
urpm media set <naam> --replication=on_demand  # Wat gedownload wordt cachen (standaard)
urpm media set <naam> --replication=seed       # DVD-achtige inhoud uit rpmsrate
```

## Configuratie

### Blacklist (nooit installeren/upgraden)

```bash
urpm config blacklist list    # Geblackliste pakketten tonen
urpm config blacklist add <pkg>
urpm config blacklist remove <pkg>
```

### Redlist (waarschuwen voor auto-remove)

```bash
urpm config redlist list      # Geredliste pakketten tonen
urpm config redlist add <pkg>
urpm config redlist remove <pkg>
```

### Kernel-beheer

```bash
urpm config kernel-keep       # Tonen hoeveel kernels bewaard worden
urpm config kernel-keep <n>   # Aantal te bewaren kernels zetten
```

### Versiemodus (systeem vs cauldron)

Wanneer zowel systeem- als cauldron-media geconfigureerd zijn, bepaalt `version-mode` welke wint bij upgrades:

```bash
urpm config version-mode              # Huidige modus tonen
urpm config version-mode system       # Blijven bij de geïnstalleerde systeemversie
urpm config version-mode cauldron     # Meelopen met cauldron
urpm config version-mode auto         # Expliciete voorkeur verwijderen
```

### Auto-upgrade-hooks voor softwarecentra

Bepaalt of GNOME Software, KDE Discover of het offline update-pad van PackageKit upgrades zelf mogen installeren:

```bash
urpm config gnome-auto-upgrades [yes|no]      # GNOME Software
urpm config discover-auto-upgrades [yes|no]   # KDE Discover
urpm config packagekit-auto-upgrades [yes|no] # PackageKit offline updates
```

Zonder argument toont elk subcommando de huidige instelling. Deze hooks schakelen de desktop-zijdige dconf/PolicyKit-instellingen om; het systeembeleid wordt apart afgedwongen door het pakket `urpm-ng-desktop`.

### Configuratie bekijken of bewerken

```bash
urpm config show              # Effectieve configuratie uit alle *.cfg-bestanden tonen
urpm config edit              # urpm.cfg in $EDITOR openen
urpm config edit 00-urpmi-compat   # Een specifieke drop-in openen
```

### Serverselectie

Het `[server]`-blok in `/etc/urpm/conf.d/10-server.cfg` stuurt de automatische mirrorselectie:

| Sleutel | Standaard | Omschrijving |
|---------|-----------|--------------|
| `auto_add` | `true` | Automatische toevoeging van mirrors toestaan |
| `country_blacklist` | *(leeg)* | Kommagescheiden ISO 3166-codes om uit te sluiten (bv. `UA, RU`) |
| `country_whitelist` | *(leeg)* | Enkel deze landen aanvaarden (overschrijft blacklist) |
| `continent_blacklist` | *(leeg)* | Continentcodes om uit te sluiten (`EU`, `NA`, `SA`, `AS`, `AF`, `OC`) |
| `continent_whitelist` | *(leeg)* | Enkel deze continenten aanvaarden (overschrijft blacklist) |

Een mirror moet **beide** filters (continent en land) doorstaan. Whitelist wint van blacklist op elk niveau. Gebruik `urpm config show` om de effectieve instellingen te zien.

## GPG-sleutels

```bash
urpm key list                 # Geïnstalleerde GPG-sleutels opsommen
urpm key import <file|url>    # Een GPG-sleutel importeren
urpm key remove <keyid>       # Een GPG-sleutel verwijderen
```

## Build-afhankelijkheden

Build-afhankelijkheden voor RPM-bouw installeren:

```bash
urpm install --buildrequires foo.spec    # Uit spec-bestand
urpm install --buildrequires foo.src.rpm # Uit source-RPM
urpm i -b                                # Auto-detect in RPM-buildboom
urpm i --br                              # Korte alias

# Opties
--sync                        # Wachten tot alle scriptlets klaar zijn
```

Geïnstalleerde build-afhankelijkheden worden gevolgd in `/var/lib/rpm/installed-through-builddeps.list` en uitgesloten van de reguliere wees-verwijdering. Opruimen kan met:

```bash
urpm autoremove --buildrequires          # Alle bijgehouden build-deps verwijderen
urpm ar -b                               # Korte vorm
```

## Container-buildsysteem

urpm biedt een compleet container-gebaseerd buildsysteem voor RPM-pakketten via Docker of Podman.

### Image-beheer

```bash
# Beschikbare build-images opsommen
urpm image list

# Een bestaand image bijwerken (media + pakketten opnieuw synchroniseren)
urpm image update mageia:10-build

# Eén of meerdere images verwijderen
urpm image delete mageia:10-build mageia:10-ci
```

### Build-image aanmaken

```bash
urpm image make --release 10 --tag mageia:10-build
urpm image make --release 10 --tag mageia:10-ci --profile ci

# Image voor een .spec of .src.rpm (installeert BuildRequires automatisch)
urpm image make --release 10 --tag mga:10-foo --buildrequires SPECS/foo.spec

# Opties
-r, --release <version>       # Mageia-versie (bv. 10, cauldron)
-t, --tag <tag>               # Image-tag (bv. mageia:10-build)
--profile <name>              # Pakketprofiel (standaard: build)
--arch <arch>                 # Doelarchitectuur (standaard: host)
-p, --packages <list>         # Extra pakketten (kommagescheiden)
--buildrequires <spec|srpm>   # BuildRequires uit een .spec of .src.rpm installeren
--addmedia <NAAM> <URL>       # Een extra medium in het image toevoegen (herhaalbaar) --
                              # bv. een derden- of interne mirror
--import-key <URL>            # Een publieke GPG-sleutel in het image importeren (herhaalbaar) --
                              # combineert met --addmedia voor ondertekende derdenmedia
--runtime docker|podman       # Container-runtime (standaard: auto-detect)
--keep-chroot                 # Tijdelijke chroot behouden na image-aanmaak
-w, --workdir <path>          # Werkmap voor de chroot (standaard: /tmp)
```

> **Achterwaartse compatibiliteit:** `urpm mkimage` blijft behouden als alias voor `urpm image make`.

### Profielen

Profielen bepalen welke pakketten in het image worden geïnstalleerd:

| Profiel | Omschrijving |
|---------|--------------|
| `build` | RPM-buildomgeving (standaard): rpm-build, gcc, make, enz. |
| `ci` | CI/testing: python3-pytest, git, python3-solv, enz. |
| `minimal` | Minimaal bruikbaar systeem met urpm |

Profielen worden geladen uit:
- `/usr/share/urpm/profiles/*.yaml` (systeem, uit het pakket)
- `/etc/urpm/profiles/*.yaml` (lokale aanvullingen)

### Pakketten bouwen

Standaard werkt `urpm build` media en pakketten in de container bij voor de build, zodat builds altijd tegen de laatste repostand draaien. Gebruik `--no-update` om die stap over te slaan bij offline werken of om herhaalde builds te versnellen.

```bash
# Uit source-RPM bouwen (uitvoer naar ./build-output/)
urpm build -i mageia:10-build foo-1.0-1.mga10.src.rpm

# Uit spec-bestand bouwen (uitvoer naar workspace/RPMS/ en SRPMS/)
urpm build -i mageia:10-build SPECS/foo.spec

# Bouwen zonder vooraf media/pakketten bij te werken
urpm build -i mga10-build --no-update SPECS/foo.spec

# Bouwen met lokale afhankelijkheden (bv. libfoo eerder gebouwd)
urpm build -i mageia:10-build SPECS/bar.spec -w 'RPMS/x86_64/libfoo*.rpm'

# Meerdere lokale afhankelijkheden
urpm build -i mageia:10-build SPECS/app.spec \
    -w 'RPMS/x86_64/libfoo*.rpm' -w 'RPMS/x86_64/libbar*.rpm'

# Meerdere builds parallel
urpm build -i mageia:10-build *.src.rpm --parallel 4

# Derdenbouwer: uitvoer als foo-1.0-1.mlo.mga10.x86_64.rpm taggen
urpm build -i mageia:10-build --subrel mlo SPECS/foo.spec

# packager/vendor/dist overschrijven zonder de spec aan te raken
urpm build -i mageia:10-build --rpmmacros ./my-macros SPECS/foo.spec

# Opties
-i, --image <tag>             # Te gebruiken Docker/Podman-image
-o, --output <dir>            # Uitvoermap voor SRPM-builds (standaard: ./build-output)
-w, --with-rpms <pattern>     # Lokale RPMs pre-installeren voor de build (glob, herhaalbaar)
--no-update                   # Auto-update van media en pakketten voor de build overslaan
--runtime docker|podman       # Container-runtime (standaard: auto-detect)
-j, --parallel <N>            # Aantal parallelle builds (standaard: 1)
--keep-container              # Container behouden na build (voor debugging)
--subrel <tag>                # Injecteert %subrel TAG, zodat de uitvoer-RPMs NAAM-VERSIE-RELEASE.TAG.DIST.ARCH.rpm worden
--rpmmacros <file>            # Injecteert FILE als /root/.rpmmacros in de build-container (combineerbaar met --subrel)
```

### Workspace-layout

Voor spec-bestand-builds ondersteunt urpm de standaard RPM-workspace-layout:

```
workspace/
├── SPECS/
│   └── foo.spec
└── SOURCES/
    ├── foo-1.0.tar.gz
    └── patches/
```

De resultaten landen in:
```
workspace/
├── RPMS/
│   └── x86_64/
│       └── foo-1.0-1.mga10.x86_64.rpm
└── SRPMS/
    └── foo-1.0-1.mga10.src.rpm
```

### Voorbeeld-workflow

```bash
# 1. Build-image aanmaken (eenmalig)
urpm image make --release 10 --tag mga:10-build

# 2. Een pakket bouwen
urpm build --image mga:10-build ./mypackage.src.rpm

# 3. Later het image bijwerken om nieuwe repo-pakketten binnen te halen
urpm image update mga:10-build

# 4. Resultaten controleren
ls ./build-output/
```

### Handmatige bootstrap (gevorderd)

Onder de motorkap roept `urpm image make` in een verse chroot `urpm init` aan
om de mediacatalogus te vullen. `urpm init` is rechtstreeks blootgesteld
voor aanroepers die een rootfs buiten het gecontaineriseerde pad moeten
bootstrappen — installer-scripts, VM-schijf-builds of voorgeprepareerde
testroots. Mirrors worden geplukt uit de Mageia-mirror-API en gefilterd
door het `[server]`-blok van `/etc/urpm/conf.d/10-server.cfg`.

```bash
# Een chroot-rootfs voor Mageia 10 bootstrappen
urpm --urpm-root /tmp/rootfs init --release 10 --arch x86_64

# Een eigen mirrorlijst gebruiken
urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'

# Opties
--release, -r <version>     # Doel-Mageia-versie (10, cauldron, …)
--mirrorlist <url>          # De automatisch gegenereerde mirrorlijst-URL overschrijven
--arch <arch>               # Doelarchitectuur (standaard: host)
--auto, -y                  # Niet-interactieve modus
--no-sync                   # Media configureren maar de initiële metadata-sync overslaan
```

Nadat je in een `--urpm-root`-chroot hebt gewerkt, ontkoppel je `/dev` en `/proc` die door `urpm init` gemount werden:

```bash
urpm --urpm-root /tmp/rootfs cleanup
```

## Gereedschap voor repository-beheerders

De twee onderstaande commando's zijn bedoeld voor wie een
Mageia-compatibele repository **publiceert**, niet voor wie er van
consumeert. Ze staan samen gedocumenteerd zodat duidelijk blijft welk
de client-metadata levert en welk ze produceert.

- **`urpm appstream`** (clientzijde) — ververst de AppStream-catalogus
  op de huidige machine zodat softwarecentra actuele beschrijvingen
  zien. Woont in `urpm-ng-appstream`.
- **`urpm genmedia`** (serverzijde) — produceert de volledige set
  media-metadata die een mirror aan zijn clients levert. Woont in
  `urpm-ng-genmedia` als apart subpakket, zodat de basis-clientinstallatie
  slank blijft.

### AppStream-metadata (`urpm appstream`)

urpm kan de AppStream-catalogi produceren en verversen die KDE Discover en GNOME Software consumeren:

```bash
urpm appstream generate              # Catalogus uit de pakketdatabase genereren
urpm appstream generate -m core/release    # Beperken tot een specifiek medium
urpm appstream generate --no-compress       # Platte XML in plaats van gzip
urpm appstream status                # Catalogusstatus per medium
urpm appstream merge                 # Per-medium-bestanden mergen tot de eengemaakte catalogus
urpm appstream merge --refresh       # Ook de systeem-AppStream-cache verversen
urpm appstream init-distro           # OS-metainfo-bestand aanmaken (nodig voor Discover/GS)
urpm appstream init-distro --force   # Bestaande metainfo overschrijven
```

### Media-generatie (`urpm genmedia`)

`urpm genmedia` is de serverzijdige tegenhanger van `urpm appstream`:
waar `appstream` catalogi consumeert om client-databases te vullen,
**produceert** `genmedia` de volledige set media-metadata die een
Mageia-mirror aan zijn clients levert. Het is een Python-herschrijving
van het historische `genhdlist3`, geïntegreerd in urpm-ng en apart
verpakt als `urpm-ng-genmedia` zodat de afhankelijkheidsvoetafdruk van
de basis-clientinstallatie niet groeit.

Vanuit een map met RPM-bestanden:

```bash
urpm genmedia /path/to/rpms          # Standaard: volledige generatie
urpm genmedia /path/to/rpms --incremental   # RPMs overslaan waarvan de SHA-256 niet is gewijzigd
urpm genmedia /path/to/rpms --no-hdlist     # De hdlist.cz-uitvoer overslaan
urpm genmedia /path/to/rpms --xml-info      # Regeneratie van XML-info-bestanden forceren
urpm genmedia /path/to/rpms --appstream-info  # AppStream-catalogus genereren
urpm genmedia /path/to/rpms --no-md5sum     # MD5SUM overslaan (sneller voor tests)
urpm genmedia /path/to/rpms --allow-empty-media  # Lege invoermap tolereren
```

Het commando produceert de canonieke layout die elke urpm-ng- of urpmi-client verwacht:

```
media_info/
  hdlist.cz                # Gecomprimeerde binaire pakket-headers
  synthesis.hdlist.cz      # Lichte afhankelijkheidssynthese
  files.xml.lzma           # Per-pakket-bestandenlijsten
  info.xml.lzma            # URL, sourcerpm, licentie, beschrijving
  changelog.xml.lzma       # Per-pakket-changelogs
  appstream.xml.gz         # Wanneer --appstream-info is gezet
  MD5SUM                   # Checksums van het bovenstaande
```

De AppStream-doorloop extraheert de ingebouwde `*.metainfo.xml`-bestanden die door upstream-applicaties (KDE, GNOME, enz.) worden meegeleverd, en genereert een minimale component uit RPM-headervelden voor pakketten die er geen hebben. Pakketten waarvan de inhoud volledig niet-user-facing is (devel-headers, debug-symbolen, statische archieven, pure runtime-libraries) worden **eruit gefilterd** in plaats van met een fallback-``System``-categorie te worden geleverd — ze zouden Discover en GNOME Software vervuilen zonder ooit via een app-store installeerbaar te zijn.

De map `media_info/` wordt tijdens een generatie vergrendeld, zodat gelijktijdig lezende clients altijd een consistente snapshot zien.

## Pakket-README-berichten

`urpm readme` toont de README-berichten die tijdens een transactie aan de gebruiker worden gepresenteerd (Mageia bewaart ze als `README.urpmi` / `README.upgrade`):

```bash
urpm readme                          # README van de meest recente transactie
urpm readme --transaction <id>       # README van een specifieke transactie
urpm readme --list                   # Transacties opsommen die README-berichten hebben
```

## Wees-opruiming

```bash
urpm cleandeps                # Alias voor `urpm autoremove --faildeps`:
                              # verwijdert verweesde afhankelijkheden die door
                              # onderbroken transacties zijn achtergebleven.
```

---

# urpmd - Achtergronddaemon

urpmd is een achtergronddienst met:
- HTTP-API voor pakketbewerkingen
- Geplande achtergrondtaken
- P2P-peer discovery voor LAN-pakketdeling

## API-eindpunten

### GET-eindpunten

| Eindpunt | Omschrijving |
|----------|--------------|
| `/` | Service-info |
| `/api/ping` | Health check |
| `/api/status` | Daemon-status |
| `/api/media` | Geconfigureerde media opsommen |
| `/api/available` | Beschikbare pakketten opsommen |
| `/api/updates` | Beschikbare updates opsommen |
| `/api/peers` | Ontdekte LAN-peers opsommen |

### POST-eindpunten

| Eindpunt | Omschrijving |
|----------|--------------|
| `/api/refresh` | Media-metadata verversen |
| `/api/available` | Beschikbare pakketten opvragen |
| `/api/announce` | Pakketten aan peers aankondigen |
| `/api/have` | Opvragen of een peer bepaalde pakketten heeft |

## Geplande taken

De daemon voert automatisch uit:
- Media-metadata-sync
- Cache-opruiming
- Beschikbaarheidscheck van updates
- Peer discovery (UDP-broadcast)

## P2P-pakketdeling

Wanneer meerdere machines op hetzelfde LAN urpmd draaien, ontdekken ze elkaar automatisch en kunnen ze gecachete RPM-pakketten delen, wat bandbreedte bespaart.

---

# GUI-integratie (Discover / GNOME Software)

urpm-ng levert een PackageKit-backend waarmee grafische softwarecentra pakketten kunnen beheren.

## Installatie

```bash
urpm install urpm-ng-desktop
```

Of installeer de backend rechtstreeks:
```bash
urpm install urpm-ng-packagekit-backend
```

Dat installeert:
- `libpk_backend_urpm.so` — PackageKit-backend
- D-Bus-service `org.mageia.Urpm.v1` — Geprivilegieerde operaties
- PolicyKit-policies — Autorisatieprompts
- AppStream-configuratie — Softwarecatalogusmetadata

## Ondersteunde toepassingen

- **KDE Discover** — Volledige ondersteuning (zoeken, installeren, verwijderen, updates)
- **GNOME Software** — Volledige ondersteuning (zoeken, installeren, verwijderen, updates)

## Hoe het werkt

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

Een aparte Qt6-GUI voor pakketbeheer is in ontwikkeling. Zie
`rpmdrake/README.md` voor details.

## Probleemoplossing

```bash
# Nakijken of de D-Bus-service draait
systemctl status urpm-dbus.service

# PackageKit-backend nakijken
pkcon backend-details

# Diensten herstarten na een update
systemctl restart packagekit.service
systemctl restart urpm-dbus.service

# D-Bus-interface nakijken
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1
```

---

# Ontwikkeling & bijdragen

## Vereisten

### Firewall-poorten

Zie het onderdeel Vereisten voor de netwerkpoorten die je voor P2P-deling moet openen.

### Je omgeving opzetten

De repository klonen:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

```


### Dev-modus-configuratie

Maak een `.urpm.local`-bestand in de projectroot aan om de dev-modus te configureren:

```bash
cd /waar/is/urpm-ng

# Dev-modus (poort 9877, gebruikersdata in ~/var/lib/urpm-dev/)
# Naar dev-modus omschakelen
touch .urpm.local
```

Noot: je kunt via het `.urpm.local`-bestand wijzigen waar urpm en urpmd hun data leggen:
```ini
# Eigen basismap (optioneel)
base_dir=/path/lib/urpm-dev
```

In dev-modus wordt data standaard in `/var/lib/urpm-dev/` bewaard en gebruikt de daemon poort 9877.

**Merk op dat urpmd in dev-modus enkel met andere urpmd in dev-modus interageert.**

## De daemon starten

```bash
# Daemon starten (als root, zonder achtergrondmodus)

cd /waar/is/urpm-ng

./bin/urpmd --dev

```

## urpm starten

```bash
# urpm starten (als root in een specifieke console)

cd /waar/is/urpm-ng

./bin/urpm --help

```

## Coderen, testen, bijdragen…

Bijdragen van elke aard zijn welkom: code, testen, vertalen, feedback geven… geen bijdrage is te klein.

Zie `CLAUDE.md` voor ontwikkelingsrichtlijnen en `doc/ARCHITECTURE.md` voor de technische architectuur.

---

# Bekende problemen / TODO

- **`urpm find`-prestaties** — Zoeken in files.xml is trager dan bij urpmf (2,5 s vs. 0,6 s). Behoeft optimalisatie.

---

# Licentie

GPL-3.0 — zie het bestand LICENSE voor details.

# Auteurs

- Maât (Pascal Vilarem)
- Papoteur (Mageia-bijdrager)
- Claude (AI-assistent)
