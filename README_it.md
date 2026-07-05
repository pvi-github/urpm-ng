# urpm-ng

Un gestore di pacchetti moderno per Mageia Linux, scritto in Python.

urpm-ng è una riscrittura completa della suite classica urpmi, con prestazioni migliori, una risoluzione delle dipendenze più raffinata e funzionalità moderne come la condivisione P2P dei pacchetti.

## Prerequisiti

### Distribuzione

Al momento serve Mageia 9 o Mageia 10, oppure Cauldron.

### Porte del firewall (per la condivisione P2P)

Il pacchetto `urpm-ng-daemon` installa `/etc/shorewall/rules.urpm-ng`
come file di include, e il suo `%post` lo aggancia automaticamente a
`/etc/shorewall/rules`. Su una macchina gestita da Shorewall (il
default di Mageia) le porte seguenti sono quindi aperte fin
dall'installazione, senza alcuna azione da parte tua:

- **TCP 9876** (produzione) o **TCP 9877** (modalità dev) -- API HTTP di urpmd
- **UDP 9878** (produzione) o **UDP 9879** (modalità dev) -- Broadcast di scoperta dei peer

Se non usi Shorewall (`iptables` / `nftables` diretti), apri le porte
a mano — il file `/etc/shorewall/rules.urpm-ng` nell'albero dei
sorgenti è un buon modello.

## Installazione

### Pacchetti

urpm-ng è suddiviso in più pacchetti per maggiore flessibilità:

| Pacchetto | Descrizione |
|-----------|-------------|
| `urpm-ng-core` | Minimale: CLI, risolutore, database |
| `urpm-ng-daemon` | Daemon in background + condivisione P2P |
| `urpm-ng` | Meta: tira `-core` + `-daemon` (installazione standard) |
| `urpm-ng-appstream` | Configurazione dei metadati AppStream (metainfo OS Mageia, config della distro) |
| `urpm-ng-packagekit-backend` | Backend PackageKit (Discover, GNOME Software) + servizio D-Bus |
| `urpm-ng-desktop` | Meta: tira `-core` + `-daemon` + `-appstream` + `-packagekit-backend` |
| `urpm-ng-build` | Meta: tira `-core` (per `urpm image` / `urpm build` — i comandi vivono in `-core`) |
| `urpm-ng-genmedia` | Generazione dei metadati media lato server (`urpm genmedia`, per manutentori di mirror) |
| `urpm-ng-all` | Meta: tira tutto quanto sopra |

**Scegli il pacchetto giusto:**
- **Installazione minimale / container**: `urpm-ng-core`
- **Uso CLI standard**: `urpm-ng`
- **Desktop con software center GUI**: `urpm-ng-desktop`
- **Chi costruisce pacchetti (utenti di bm / mkimage)**: `urpm-ng-build`
- **Manutentori di mirror che pubblicano repository**: `urpm-ng-genmedia`

### Installazione / aggiornamento rapido (`geturpm.sh`)

`geturpm.sh` è la via consigliata per installare urpm-ng su una Mageia
fresca, e può anche aggiornare un'installazione esistente. Rileva
automaticamente la release Mageia e l'architettura, scarica l'ultima
urpm-ng dal canale che scegli, e fa la cosa giusta sia che urpm-ng sia
già installato oppure no (le macchine fresche fanno bootstrap con
`urpmi`; gli aggiornamenti successivi passano per urpm-ng stessa).

**Rapido — tramite pipe, senza ispezione locale**

```bash
curl -fsSL https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh | URPM_YES=1 bash
```

`URPM_YES=1` è obbligatorio qui — lo script non ha TTY quando passa
per pipe, e gli serve questo flag per saltare le conferme.

**Verificato — scarica, leggi, poi esegui** (consigliato se non ti
fidi già della sorgente):

```bash
curl -fsSLO https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh
less geturpm.sh                  # ispeziona prima di eseguire
bash geturpm.sh
```

**Selezione del canale** (`URPM_CHANNEL`):

- `mgabiz` — recupera dal repository del progetto Mageia.biz (default
  in modalità piped). Usa `urpm media discover` sul mirror mgabiz,
  quindi gli aggiornamenti futuri passano per il flusso standard
  `urpm media update`.
- `github` — recupera gli RPM di release direttamente dalla pagina
  releases di GitHub. Utile per testare un tag specifico, o quando la
  pubblicazione mgabiz è in ritardo su una release.

I canali `gitlab` e `codeberg` sono previsti ma non ancora
disponibili.

Nota: alla prima installazione, urpm-ng importa automaticamente la
propria configurazione dai file `urpmi.cfg` e `urpmi/skip.list`
esistenti.

## Configurazione iniziale

urpm funziona così com'è. Le opzioni avanzate (blacklist, redlist, kernel-keep) sono documentate più in basso nella sezione **Configurazione**.

Quando è installato a livello di sistema (in `/usr/bin/`), urpm usa:
- Database: `/var/lib/urpm/packages.db`
- Porta del daemon: 9876
- File PID: `/run/urpmd.pid`

### Sorgenti media

Su un'installazione fatta per via RPM (o tramite `geturpm.sh`), i media
Mageia standard e i server da cui recuperarli sono configurati
automaticamente: `urpm-ng` importa il `urpmi.cfg` esistente al primo
avvio e `urpm server autoconfig` popola il pool dei mirror dall'API
mirror di Mageia. Non serve altro per installare pacchetti.

Su una macchina senza `urpmi.cfg` preesistente (chroot fresco, build
d'immagine, o sistema che non ha mai avuto urpmi), lo stesso
bootstrap si fa in una passata manuale:

```bash
urpm media list                       # Niente? bootstrap:
urpm media import                     # Legge /etc/urpmi/urpmi.cfg di default; no-op se assente
urpm server autoconfig                # Recupera i mirror dall'API Mageia
urpm media update                     # Prima sync dei metadati
```

Per aggiungere un **repository comunitario** (MageiaLinux-Online,
mageia.biz, blogdrake, un mirror interno, ...), usa
`urpm media discover`: legge il `media.cfg` del repository e aggiunge
tutti i media che dichiara in una sola chiamata:

```bash
urpm media discover https://www.mageia.biz/repo/Mageia/mgabiz/10/x86_64/media/
urpm media discover --dry-run https://download.mageialinux-online.org/...   # Anteprima
```

`urpm media add` è riservato a un singolo media custom fuori dal flusso
discover — cioè uno che sai non essere pubblicato tramite un
`media.cfg`. Vedi la sezione **Gestione dei media** più in basso per
la sintassi.

---

# urpm - Interfaccia a riga di comando

## Opzioni globali

Queste opzioni si applicano alla maggior parte dei comandi e vanno prima del sottocomando:

```bash
-V, --version              # Mostra la versione di urpm
-v, --verbose              # Output verboso
-q, --quiet                # Output silenzioso
--nocolor                  # Disabilita l'output colorato
--root DIR                 # Usa DIR come root per l'install RPM (chroot, config urpm dall'host)
--urpm-root DIR            # Usa DIR come root sia per la config urpm sia per l'install RPM
```

I parent seguenti sono ereditati dai comandi transazionali e di query
(`install`, `upgrade`, `erase`, `download`, `depends`, ...):

```bash
--arch ARCH                # Architettura di destinazione (default: sistema corrente)
--debug COMPONENT          # Abilita l'output di debug: solver, tsrun, orphans, download, timing, all
--watched PACKAGES         # Nomi di pacchetti separati da virgole da osservare durante la risoluzione
```

Nota: `--arch` (opzione parent, fissa l'architettura di destinazione
dell'operazione) è distinto da `--allow-arch` (opzione locale su
install/upgrade/download, autorizza architetture aggiuntive oltre a
quella di sistema — tipicamente `i686` per wine/steam su x86_64).

## Opzioni di visualizzazione

La maggior parte dei comandi accetta queste opzioni di output:

```bash
--show-all            # Mostra tutti gli elementi senza troncatura
--flat                # Un elemento per riga (parsabile da script)
--json                # Output JSON (per uso programmatico)
```

Di default, gli elenchi lunghi sono mostrati in formato multi-colonna e troncati a 10 righe con "... e altri N". Usa `--show-all` per vedere tutto.

Esempi:
```bash
urpm list installed --flat          # Un pacchetto per riga
urpm search firefox --json          # Output JSON
urpm i task-plasma --show-all       # Mostra tutte le dipendenze
```

## Transazioni atomiche vs best-effort

Dalla 0.7.9, `urpm upgrade` gira in modalità **best-effort** di default:
i pacchetti le cui dipendenze non possono essere soddisfatte vengono
rimossi dalla transazione e riportati alla fine con la loro ragione
(dipendenza mancante, mismatch di versione, cascata SRPM sorella, ...).
La transazione è comunque validata per tutto il resto. Passa `--atomic`
per commutare in modalità stretta (consigliata sui server): qualsiasi
pacchetto irrisolvibile abortisce l'intera transazione.

`urpm install`, al contrario, è **atomico di default**: se un pacchetto
richiesto non può essere installato, l'intera transazione viene
annullata. Passa `--no-atomic` per optare per la modalità best-effort
sul percorso di install.

## Codici di uscita

| Codice | Significato |
|--------|-------------|
| 0      | Transazione riuscita, nessun pacchetto saltato |
| 1      | Errore duro: transazione abortita (modalità atomica, rete, permessi, ...) |
| 2      | Transazione parziale: riuscita ma almeno un pacchetto è stato rimosso (i pacchetti saltati sono elencati su stderr con la loro ragione) |

Check scriptabile per il caso parziale:

```bash
urpm upgrade --auto || [ $? -eq 2 ] && echo "ok o parziale"
```

## Gestione dei pacchetti

### Installare pacchetti

```bash
urpm install <pacchetto>      # Installa un pacchetto
urpm i <pacchetto>            # Alias corto

# Opzioni
--auto, -y                    # Modalità non interattiva
--test                        # Simulazione (dry run)
--without-recommends          # Salta i pacchetti raccomandati
--with-suggests               # Installa anche i pacchetti suggeriti
--force                       # Forza nonostante i problemi di dipendenze
--reinstall                   # Reinstalla i pacchetti già installati (riparazione)
--nosignature                 # Salta la verifica GPG (sconsigliato)
--noscripts                   # Salta gli script pre/post install (build chroot/container)
--no-peers                    # Disabilita il download P2P dai peer LAN
--only-peers                  # Scarica solo dai peer LAN, niente mirror upstream
--no-atomic                   # Modalità best-effort (per install il default è atomico)
--download-only               # Scarica nella cache, non installa
--nodeps                      # Salta la risoluzione delle dipendenze (con --download-only)
--all                         # Installa per tutte le famiglie corrispondenti (es. php8.4 + php8.5)
--install-src                 # Installa il RPM sorgente (estrae spec/sources in ~/rpmbuild/)
--config-policy {keep,replace,ask}  # Politica di conflitto sui file di config (default: keep)
--prefer=<prefs>              # Guida le scelte fra alternative (vedi sotto)
--allow-arch <arch>           # Autorizza architetture aggiuntive (es. i686 per wine/steam)
--sync                        # Attendi il completamento totale (trigger post-install)
```

#### Installazione guidata da preferenze

Quando installi pacchetti con alternative (es. phpmyadmin che può usare versioni di PHP e server web diversi), usa `--prefer` per guidare le scelte:

```bash
# Preferisci PHP 8.4 con Apache e php-fpm, escludi mod_php
urpm i phpmyadmin --prefer=php:8.4,apache,php-fpm,-apache-mod_php

# Preferisci nginx al posto di apache
urpm i phpmyadmin --prefer=php:8.4,nginx,php-fpm
```

Sintassi delle preferenze:
- `capability:version` — Vincolo di versione (es. `php:8.4`)
- `pattern` — Preferisci i pacchetti che forniscono questa capability (es. `apache`, `php-fpm`)
- `-pattern` — Sfavorisci i pacchetti corrispondenti (es. `-apache-mod_php`)

Le preferenze operano su REQUIRES e PROVIDES dei pacchetti, non sui nomi.

#### Filtraggio per architettura

Di default, urpm considera solo i pacchetti compatibili con l'architettura del tuo sistema e `noarch`. Questo evita l'installazione accidentale di pacchetti i686 su sistemi x86_64 quando i media 32-bit sono attivi.

Per installare pacchetti 32-bit (wine, steam, multilib):

```bash
urpm install wine --allow-arch i686
urpm install steam --allow-arch i686

# Più architetture
urpm install miopacchetto --allow-arch i686 --allow-arch armv7hl
```

### Rimuovere pacchetti

```bash
urpm erase <pacchetto>        # Rimuovi un pacchetto
urpm e <pacchetto>            # Alias corto

# Opzioni
--auto, -y                    # Modalità non interattiva
--test                        # Simulazione (dry run)
--auto-orphans                # Rimuovi anche le dipendenze orfane (implicito con -y, salvo --keep-orphans)
--keep-orphans                # Non rimuovere le dipendenze orfane
--erase-recommends            # Rimuovi anche i pacchetti solo raccomandati (non richiesti)
--keep-suggests               # Tieni i pacchetti suggeriti da pacchetti rimasti
--force                       # Forza nonostante i problemi di dipendenze
--debug {solver,tsrun,all}    # Abilita l'output di debug per risolutore/transazione
--sync                        # Attendi il completamento totale (trigger post-uninstall)
```

### Aggiornare i metadati (stile apt)

```bash
urpm update                   # Aggiorna i metadati di tutti i media
urpm update "Core Release"    # Aggiorna un media specifico
```

Dalla 0.7.x, `files.xml.lzma` viene recuperato insieme a `synthesis.hdlist.cz` ogni volta che il media lo pubblica — nessun flag da abilitare.

### Scaricare pacchetti (senza installarli)

```bash
urpm download <pacchetto>     # Scarica un pacchetto nella cache
urpm dl <pacchetto>           # Alias corto
urpm download --only-peers pkg  # Scarica solo dai peer LAN

# Opzioni
--release, -r <version>       # Release di destinazione per download cross-release (es. cauldron)
--buildrequires, --br [SPEC]  # Scarica le build deps (auto-rileva o da .spec/.src.rpm)
--without-recommends          # Salta i pacchetti raccomandati
--nodeps                      # Scarica solo i pacchetti indicati, senza dipendenze
--no-peers / --only-peers     # Come install (politica peer)
--allow-arch <arch>           # Autorizza architetture aggiuntive
--arch <arch>                 # Ereditato: architettura di destinazione
--show-all                    # Stampa l'elenco completo dei pacchetti risolti
                              # (di default tronca a 20 con "... e altri N")
```

### Aggiornare i pacchetti

```bash
urpm upgrade                  # Aggiorna tutti i pacchetti
urpm u                        # Alias corto
urpm upgrade <pacchetto>      # Aggiorna pacchetti specifici

# Opzioni
--auto, -y                    # Modalità non interattiva
--test                        # Simulazione (dry run)
--atomic                      # Modalità stretta: aborto totale della transazione su qualsiasi pacchetto irrisolvibile.
                              # Default: best-effort (vedi "Transazioni atomiche vs best-effort" più su).
--with-recommends             # Installa i pacchetti raccomandati
--with-suggests               # Installa anche i pacchetti suggeriti
--noerase-orphans             # Tieni le dipendenze orfane (non rimuoverle)
--download-only               # Scarica nella cache senza applicare l'aggiornamento
--nosignature                 # Salta la verifica GPG (sconsigliato)
--no-peers / --only-peers     # Disabilita / limita ai peer LAN
--force                       # Forza l'aggiornamento nonostante problemi di dipendenze
--config-policy {keep,replace,ask}  # Politica di conflitto config (default: keep)
--allow-arch <arch>           # Autorizza architetture aggiuntive (es. i686)
--sync                        # Attendi il completamento totale (trigger post-install)
```

### Auto-rimozione degli orfani

```bash
urpm autoremove               # Rimuovi le dipendenze inutilizzate (default: --orphans)
urpm ar                       # Alias corto

# Selettori
--orphans, -o                 # Pacchetti orfani (default)
--kernels, -k                 # Vecchi kernel
--faildeps, -f                # Deps di transazioni interrotte
--buildrequires, -b           # Dipendenze di build (--builddeps, --br)
--all, -a                     # Tutto quanto sopra

# Opzioni
--auto, -y                    # Modalità non interattiva
```

## Ricerca e query

### Cercare pacchetti

```bash
urpm search <pattern>         # Cerca per nome/riassunto
urpm s <pattern>              # Alias corto
urpm q <pattern>              # Alias query (compatibilità urpmq)

# Opzioni
--installed                   # Cerca solo fra i pacchetti installati
--unavailable                 # Elenca i pacchetti installati assenti da ogni media
```

#### Trovare i pacchetti non disponibili

Elenca i pacchetti installati ma che non sono più disponibili in nessun media configurato (come `urpmq --unavailable`):

```bash
urpm q --unavailable          # Elenca tutti i pacchetti non disponibili
urpm q --unavailable php      # Filtra per pattern
```

### Mostrare le info di un pacchetto

```bash
urpm show <pacchetto>         # Mostra i dettagli di un pacchetto
urpm info <pacchetto>         # Alias
```

### Elencare i pacchetti

```bash
urpm list installed           # Elenca i pacchetti installati
urpm list available           # Elenca i pacchetti disponibili
urpm list updates             # Elenca gli aggiornamenti disponibili
urpm list upgradable          # Alias per updates
```

### Dipendenze

```bash
urpm depends <pacchetto>      # Mostra cosa richiede un pacchetto
urpm rdepends <pacchetto>     # Mostra cosa richiede un pacchetto (deps inverse)
urpm why <pacchetto>          # Spiega perché un pacchetto è installato

# Opzioni per depends
--tree                        # Mostra l'albero delle dipendenze
--prefer=<prefs>              # Filtra per preferenze (stessa sintassi di install)
--legend                      # Mostra la legenda dei simboli dopo l'albero

# Opzioni per rdepends
--tree                        # Mostra l'albero delle dipendenze inverse
--all                         # Mostra tutte le dipendenze inverse ricorsive (piatto)
--depth=N                     # Profondità massima dell'albero (default: 3)
--hide-uninstalled            # Mostra solo i percorsi che portano a pacchetti installati
--legend                      # Mostra la legenda dei simboli dopo l'albero
```

Esempio con preferenze:
```bash
# Mostra le deps di phpmyadmin preferendo PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

Esempio con rdepends:
```bash
# Mostra l'albero di deps inverse per rtkit, profondità 10, solo percorsi installati
urpm rdepends --tree --hide-uninstalled --depth=10 rtkit
```

### Dipendenze deboli

```bash
urpm recommends <pacchetto>     # Mostra i pacchetti raccomandati da un pacchetto
urpm whatrecommends <pacchetto> # Mostra i pacchetti che raccomandano un pacchetto
urpm suggests <pacchetto>       # Mostra i pacchetti suggeriti da un pacchetto
urpm whatsuggests <pacchetto>   # Mostra i pacchetti che suggeriscono un pacchetto
```

### Query sui file

```bash
urpm provides <pacchetto>     # Elenca i file forniti da un pacchetto
urpm whatprovides <file>      # Trova quale pacchetto fornisce un file
urpm find <pattern>           # Cerca file nei pacchetti (installati + disponibili)
urpm find -i <pattern>        # Cerca solo nei pacchetti installati
urpm find -a <pattern>        # Cerca solo nei pacchetti disponibili
urpm find <pattern> --all-versions  # Includi ogni EVR che contiene il match
urpm find <pattern> --limit 500     # Alza il tetto di default di 100 hit
```

`urpm find` cerca di default sia nei pacchetti installati sia in quelli disponibili. `files.xml.lzma` viene recuperato automaticamente a ogni `urpm media update` (condizionato al fatto che il media lo dichiari in `MD5SUM`), quindi nessun opt-in è necessario — il toggle `--sync-files` è stato rimosso nella 0.7.x.

## Marcatura dei pacchetti

```bash
urpm mark manual <pacchetto>  # Marca come installato manualmente
urpm mark auto <pacchetto>    # Marca come installato automaticamente (dipendenza)
urpm mark show <pacchetto>    # Mostra la ragione dell'installazione
```

## Blocchi di pacchetti (holds)

Blocca i pacchetti per impedire aggiornamenti e sostituzione tramite obsoletes:

```bash
urpm hold <pacchetto>         # Blocca un pacchetto
urpm hold <pacchetto> -r "ragione"  # Blocca con una ragione
urpm hold                     # Elenca i pacchetti bloccati
urpm unhold <pacchetto>       # Rimuovi il blocco
```

I pacchetti bloccati sono protetti da:
- Aggiornamenti di versione durante `urpm upgrade`
- Sostituzione tramite pacchetti che li obsoletano

Esempio:
```bash
# dhcpcd obsoleta dhcp-client, ma vuoi tenere dhcp-client
urpm hold dhcp-client -r "Prefer dhcp-client over dhcpcd"

# Ora urpm upgrade salta dhcp-client e avvisa:
#   Pacchetti bloccati (1) saltati:
#     dhcp-client (sarebbe obsoletato da dhcpcd)

# Per autorizzare la sostituzione più tardi:
urpm unhold dhcp-client
```

## Storico e annullamento

```bash
urpm history                  # Mostra lo storico delle transazioni (le ultime 20)
urpm history -i               # Filtro: solo transazioni di install
urpm history -r               # Filtro: solo transazioni di remove
urpm history -d <id>          # Mostra i dettagli della transazione <id>
urpm history --delete <id>... # Cancella transazioni dal log

urpm undo [id]                # Annulla una transazione (default: l'ultima). Registra una voce
                              # pulita nello storico. Usa --auto/-y per saltare il prompt.

urpm rollback <n>             # Rollback delle ultime n transazioni
urpm rollback to <id>         # Rollback fino a una transazione precisa
urpm rollback to <date>       # Rollback fino a una data (YYYY-MM-DD o DD/MM/YYYY)
```

## Transazioni in background

Quando una transazione è staccata (es. tramite il daemon o PackageKit), segui il suo avanzamento con:

```bash
urpm progress                 # Mostra l'avanzamento corrente ed esce
urpm progress --watch         # Osserva in continuo fino al termine
```

## Gestione dei media

```bash
urpm media list               # Elenca i media configurati
urpm media add <url>          # Aggiungi un media Mageia ufficiale (auto-parsato)
urpm media add --custom "Nome" nome_corto <url>  # Aggiungi un media custom / di terze parti
urpm media remove <nome>...   # Rimuovi uno o più media
urpm media remove --all       # Rimuovi TUTTI i media configurati (chiede
                              # conferma; aggiungi -y/--auto per saltarla).
                              # I server orfani (senza media residui) sono
                              # eliminati nella stessa passata.
urpm media enable <nome>      # Abilita un media
urpm media disable <nome>     # Disabilita un media
urpm media update [nome]      # Aggiorna i metadati dei media
urpm media import <file>      # Importa da urpmi.cfg
urpm media link <nome> +srv -srv  # Lega/scioglie server a un media
urpm media set <nome> [opts]  # Modifica i parametri del media (sharing, replication, quota…)
urpm media seed-info <nome>   # Mostra le info del seed set (sezioni, num. pacchetti, dim. stimata)
urpm media autoconfig -r 10   # Auto-aggiungi i media Mageia ufficiali per la release 10
urpm media discover <url>     # Scopri i media dal media.cfg di un repo
```

Flag utili per `urpm media add`:

```bash
--import-key                  # Importa la chiave GPG dichiarata dal media
--allow-unsigned              # Autorizza pacchetti non firmati (solo media custom)
--version <ver>               # Versione Mageia di destinazione (solo media custom: 9, 10, cauldron…)
--update                      # Marca come media di aggiornamenti
--disabled                    # Aggiungi ma lascia disabilitato
-y, --auto                    # Non interattivo: accetta il nome/short_name auto-rilevato
```

### Importare i media da un urpmi.cfg legacy

Migra una macchina Mageia esistente da `urpmi` a urpm-ng senza
riaggiungere a mano ogni sorgente. Le voci per URL e quelle
`MIRRORLIST=` sono entrambe importate — queste ultime come media
pendenti a cui `urpm server autoconfig` aggancerà server alla prossima
esecuzione.

```bash
urpm media import /etc/urpmi/urpmi.cfg    # Percorso di default
urpm media import                          # Idem (default a /etc/urpmi/urpmi.cfg)

# Opzioni
--replace                     # Sovrascrivi i media esistenti corrispondenti per short_name
-r, --release <version>       # Release Mageia di destinazione (default: valore di /etc/mageia-release)
--arch <arch>                 # Architettura di destinazione (default: `uname -m`)
-y, --auto                    # Non interattivo: salta la conferma
```

### Scoprire i media da un repository

Scopri tutti i media disponibili da un qualsiasi repository compatibile Mageia (mirror ufficiali, repo comunitari come MLO, mirror aziendali):

```bash
urpm media discover https://repo.example.org/9/x86_64/media/       # Aggiungi tutti i media
urpm media discover --dry-run https://repo.example.org/9/x86_64/media/  # Solo anteprima
urpm media discover --sources --debug https://...                   # Includi SRPMS e debug

# Force-abilita / force-disabilita categorie (nonfree, tainted, 32bit, all)
urpm media discover --with nonfree,tainted https://...
urpm media discover --without nonfree https://...
urpm media discover --with all https://...
```

Il comando recupera `media.cfg` dal repository, scopre tutti i media, e collega i server esistenti che ospitano lo stesso contenuto (verificato tramite checksum MD5 di `synthesis.hdlist.cz`).

### Legame server-media

Lega o scioglie server a sorgenti media specifiche:

```bash
urpm media link "Core Release" +mirror1 +mirror2   # Aggiungi server
urpm media link "Core Updates" -oldserver          # Rimuovi un server
urpm media link "Core Release" +all                # Aggiungi tutti i server disponibili
urpm media link "Core Release" -all +preferred     # Reset e aggiungine uno
```

Nota: quando aggiungi server, urpm verifica che il contenuto media corrisponda confrontando i checksum MD5 di `synthesis.hdlist.cz` con quelli dei server di riferimento esistenti.

### Auto-configurare i media

Aggiungi automaticamente i media Mageia ufficiali per una release:

```bash
urpm media autoconfig --release 10              # Aggiungi tutti i media ufficiali per Mageia 10
urpm media autoconfig -r cauldron               # Aggiungi i media per Cauldron
urpm media autoconfig -r 10 --no-nonfree        # Salta i media nonfree
urpm media autoconfig -r 10 --no-tainted        # Salta i media tainted
urpm media autoconfig -r 10 -n                  # Dry-run: mostra cosa verrebbe aggiunto
```

### Parametri di media

Configura la condivisione e la replica dei media:

```bash
urpm media set "Core Release" --shared=yes           # Condividi con i peer P2P
urpm media set "Core Release" --replication=seed     # Replica completa (DVD-like)
urpm media set "Core Release" --replication=on_demand  # Cache di ciò che viene scaricato
urpm media set "Core Release" --quota=5G             # Limita la dimensione della cache
urpm media set "Core Release" --retention=30         # Tieni i pacchetti 30 giorni
urpm media set "Core Release" --priority=10          # Priorità più alta
urpm media set "Core Release" --seeds=INSTALL,CAT_PLASMA5  # Sezioni di seed
```

Esempi:
```bash
# Aggiungi un media Mageia ufficiale (server e media auto-rilevati)
urpm media add https://ftp.belnet.be/mageia/distrib/9/x86_64/media/core/release/

# Aggiungi un media custom di terze parti
urpm media add --custom "RPM Fusion" rpmfusion https://download1.rpmfusion.org/free/fedora/40/x86_64/os/
```

## Gestione dei server

I server sono sorgenti mirror che possono servire più media. urpm supporta più server per media per il bilanciamento di carico e il failover.

```bash
urpm server list              # Elenca i server configurati (con paese)
urpm server add <nome> <url>  # Aggiungi un server (testa l'IP e scansiona i media)
urpm server remove <nome> ... # Rimuovi uno o più server
urpm server enable <nome>     # Abilita un server
urpm server disable <nome>    # Disabilita un server
urpm server priority <nome> <n>  # Fissa la priorità del server (più alto = preferito)
urpm server test [nome]       # Testa la connettività e rileva la modalità IP
urpm server ip-mode <nome> <mode>  # Fissa la modalità IP (auto/ipv4/ipv6/dual)
urpm server autoconfig        # Auto-aggiungi server dall'API mirror Mageia
urpm server stats [nome]      # Mostra le statistiche di performance di un server
urpm server status            # Mostra i server blacklistati / a bassa reputazione
urpm server unblacklist <nome>   # Rimuovi il blacklist di un server (dopo revisione)
urpm server ack-blacklist <nome>  # Riconosci un blacklist (silenzia il banner senza rimuovere il blacklist)
```

### Elenco dei server

Opzioni per urpm server list:
```bash
--all                 # Mostra tutti i server, inclusi quelli disabilitati
```

### Modalità IP

Ogni server ha una modalità IP per gestire la connettività IPv4/IPv6:
- `auto` — Lascia decidere al sistema (può causare timeout di 30s se IPv6 fallisce)
- `ipv4` — Forza solo IPv4
- `ipv6` — Forza solo IPv6
- `dual` — Entrambi funzionano, preferisci IPv4 (consigliato per server dual-stack)

La modalità IP è auto-rilevata all'aggiunta del server. Usa `server test` per ri-rilevare o `server ip-mode` per fissarla manualmente.

### Monitoraggio della banda e failover automatico

urpm traccia automaticamente la performance di download di ogni server. Dopo ogni download o sync di metadati, la velocità misurata viene registrata con un EWMA (Exponentially Weighted Moving Average, α=0.3), fornendo un'inerzia che evita a un singolo trasferimento lento di penalizzare ingiustamente un buon server.

I server vengono provati nell'ordine `priority DESC, bandwidth_kbps DESC`: se un server fallisce durante un download o una sync di metadati, il successivo migliore viene provato automaticamente senza intervento dell'utente. All'interno di una stessa sessione, delle stime di velocità per server sono tenute anche in memoria, così l'ordine si adatta in tempo reale senza aspettare il run successivo.

`urpm server autoconfig` misura la latenza verso tutti i candidati mirror e ne persiste i risultati, quindi l'ordine dei server è già significativo fin dal primo download.

### Blacklist e reputazione

Un server che serve un RPM corrotto o non firmato è **auto-blacklistato**:
viene escluso dai download successivi finché non lo rivedi e lo sblocchi
manualmente. I fallimenti di firma sono trattati come segnali attivi di
manomissione — nessun auto-unblock temporale.

Insieme alla blacklist, urpm mantiene un **punteggio di reputazione
scorrevole a 24 h** (baseline 100) che scende su corpi corrotti, HTTP
4xx/5xx, errori di rete e trasferimenti lenti. Il punteggio riordina il
pool senza escludere del tutto i server.

```bash
urpm server status               # Elenca i server blacklistati / a bassa reputazione
urpm server unblacklist <nome>   # Rimuovi il blacklist dopo revisione umana
urpm server ack-blacklist <nome> # Riconosci (silenzia il banner senza sbloccare)
```

Al momento di `install` / `upgrade` / `media update`, un banner rosso
persistente elenca ogni blacklist non riconosciuta con le istruzioni di
riattivazione — il banner non sparisce da solo, solo `unblacklist` o
`ack-blacklist` lo silenziano.

`urpm server list` mostra in rosso le righe blacklistate, così un
colpo d'occhio sul pool basta per capire chi è fuori.

### Filtraggio geografico

I server scoperti dall'API mirror di Mageia portano metadati di paese e continente. La sezione di configurazione `[server]` (vedi più sotto) permette di limitare quali mirror sono accettati:

```ini
# /etc/urpm/conf.d/10-server.cfg
[server]
country_blacklist = UA, RU        # Escludi paesi specifici
continent_whitelist = EU          # Solo mirror europei
```

Il filtraggio viene applicato all'aggiunta di mirror (`urpm init`, `urpm media autoconfig`, `urpm server autoconfig`, ed espansione del pool in background). I server già presenti in database sono completati con il loro paese al primo run; quelli che non passano il filtro sono disabilitati automaticamente.

Imposta `auto_add = false` per impedire ogni aggiunta automatica di mirror.

Usa `urpm server stats [nome]` per ispezionare le metriche raccolte:

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

## Gestione dei peer

Quando urpmd gira su più macchine della stessa LAN, si scoprono a vicenda e condividono i pacchetti in cache (P2P).

```bash
urpm peer list                # Elenca i peer scoperti
urpm peer downloads [host]    # Mostra i pacchetti scaricati dai peer (filtra per host)
urpm peer blacklist <host>    # Blocca un peer (es. se fornisce pacchetti sbagliati)
urpm peer unblacklist <host>  # Sblocca un peer
urpm peer clean <host>        # Cancella gli RPM scaricati da un peer specifico
                              # (da usare dopo aver messo in blacklist; <host> obbligatorio)
```

### Modalità solo locale

Usa `--only-peers` per scaricare esclusivamente dai peer LAN senza fallback ai mirror upstream:

```bash
urpm i --only-peers firefox   # Installa solo se disponibile dai peer
urpm u --only-peers           # Aggiorna solo con i pacchetti dei peer
urpm download --only-peers pkg  # Scarica solo dai peer
```

Utile per reti air-gapped o per garantire che tutti i pacchetti provengano da sorgenti locali fidate.

## Gestione della cache

```bash
urpm cache info               # Mostra le info della cache
urpm cache clean              # Rimuovi gli RPM orfani dalla cache
urpm cache rebuild            # Ricostruisci il database dei pacchetti dai file synthesis
urpm cache rebuild-fts        # Ricostruisci l'indice FTS per la ricerca rapida di file
urpm cache stats              # Statistiche dettagliate
```

`urpm cache clean` accetta `--dry-run/-n` (anteprima), `--auto/-y` (senza conferma) e `--verbose/-v` (elenca ogni file orfano).

## Mirror / Replica

urpm-ng può replicare localmente un sottoinsieme di pacchetti (simile a un set di installazione DVD) ed esporli ai peer LAN. Utile per install party, installazioni offline, e per allestire un mirror interno.

Due componenti in gioco:

- **Politica per media** — `urpm media set <nome> --replication=…`
  controlla come ciascun media viene replicato (solo metadati, cache
  a richiesta, o seed completo).
- **`urpm mirror` top-level** — stato globale lato daemon (quote,
  versioni servite, limite di banda in uscita) e trigger espliciti di
  manutenzione.

### Controllo top-level del mirror

```bash
urpm mirror status            # Mostra stato del mirror, quote e versioni servite
urpm mirror enable            # Inizia a servire i pacchetti in cache ai peer
urpm mirror disable           # Smetti di servire i pacchetti
urpm mirror quota [SIZE]      # Mostra o fissa la quota globale della cache (es. 10G, 500M)
urpm mirror enable-version 10,cauldron   # Riprendi il servizio per queste versioni
urpm mirror disable-version 8,9          # Ferma il servizio per queste versioni
urpm mirror clean [-n]        # Forza quote e politiche di retention (--dry-run anteprima)
urpm mirror sync [media]      # Forza una sync di replica per i media in politica `seed`
urpm mirror sync --latest-only           # Sync più piccola, DVD-like
urpm mirror rate-limit [on|off|N/min]    # Configura il rate limit in uscita
```

### Replica basata su seed

La replica usa il file `rpmsrate-raw` di Mageia per determinare quali pacchetti mirrorare (stessa logica del contenuto DVD).

```bash
# Abilita la replica seed-based su un media
urpm media set "Core Release" --replication=seed
urpm media set "Core Updates" --replication=seed

# Guarda il seed set calcolato
urpm media seed-info "Core Release"
# Output:
#   Sezioni: INSTALL, CAT_PLASMA5, CAT_GNOME, …
#   Pacchetti seed da rpmsrate: 437
#   Pattern di locale: 3
#   Pacchetti locale espansi: +237
#   Con dipendenze: 2300 pacchetti
#   Dimensione stimata: ~3.5 GB

# Forza la sync (scarica i pacchetti mancanti)
urpm mirror sync

# Sync solo dell'ultima versione di ciascun pacchetto (più piccolo, DVD-like)
urpm mirror sync --latest-only
```

### Come funziona

1. Parsa `/usr/share/meta-task/rpmsrate-raw` (dal pacchetto meta-task)
2. Estrae i pacchetti dalle sezioni: INSTALL, CAT_PLASMA5, CAT_GNOME, CAT_XFCE, ecc.
3. Espande i pattern di locale (es. `libreoffice-langpack-ar` → tutte le langpack)
4. Risolve le dipendenze (Requires + Recommends)
5. Scarica i pacchetti mancanti in parallelo

Le sezioni di seed di default coprono tutti i principali ambienti desktop e le applicazioni, risultando in ~5 GB di pacchetti (paragonabile a un DVD Mageia).

### Politiche di replica

```bash
urpm media set <nome> --replication=none       # Solo metadati, nessun pacchetto
urpm media set <nome> --replication=on_demand  # Cache di ciò che viene scaricato (default)
urpm media set <nome> --replication=seed       # Contenuto DVD-like da rpmsrate
```

## Configurazione

### Blacklist (non installare/aggiornare mai)

```bash
urpm config blacklist list    # Mostra i pacchetti blacklistati
urpm config blacklist add <pkg>
urpm config blacklist remove <pkg>
```

### Redlist (avvisa prima dell'auto-remove)

```bash
urpm config redlist list      # Mostra i pacchetti redlistati
urpm config redlist add <pkg>
urpm config redlist remove <pkg>
```

### Gestione del kernel

```bash
urpm config kernel-keep       # Mostra quanti kernel tenere
urpm config kernel-keep <n>   # Fissa il numero di kernel da tenere
```

### Modalità di versione (sistema vs cauldron)

Quando sono configurati sia sistema sia cauldron, `version-mode` sceglie chi vince per gli aggiornamenti:

```bash
urpm config version-mode              # Mostra la modalità corrente
urpm config version-mode system       # Resta sulla versione di sistema installata
urpm config version-mode cauldron     # Segui cauldron
urpm config version-mode auto         # Rimuovi la preferenza esplicita
```

### Hook di auto-upgrade per i software center

Controlla se GNOME Software, KDE Discover o il percorso di update offline di PackageKit possono installare aggiornamenti di propria iniziativa:

```bash
urpm config gnome-auto-upgrades [yes|no]      # GNOME Software
urpm config discover-auto-upgrades [yes|no]   # KDE Discover
urpm config packagekit-auto-upgrades [yes|no] # Update offline PackageKit
```

Senza argomento, ogni sottocomando stampa l'impostazione corrente. Questi hook commutano le impostazioni dconf/PolicyKit lato desktop; la policy di sistema è applicata separatamente dal pacchetto `urpm-ng-desktop`.

### Ispezionare o modificare la configurazione

```bash
urpm config show              # Mostra la config effettiva da tutti i *.cfg
urpm config edit              # Apri urpm.cfg nell'$EDITOR
urpm config edit 00-urpmi-compat   # Apri un drop-in specifico
```

### Selezione dei server

La sezione `[server]` in `/etc/urpm/conf.d/10-server.cfg` controlla la selezione automatica dei mirror:

| Chiave | Default | Descrizione |
|--------|---------|-------------|
| `auto_add` | `true` | Autorizza l'aggiunta automatica di mirror |
| `country_blacklist` | *(vuoto)* | Codici ISO 3166 separati da virgola da escludere (es. `UA, RU`) |
| `country_whitelist` | *(vuoto)* | Accetta solo questi paesi (prevale sulla blacklist) |
| `continent_blacklist` | *(vuoto)* | Codici continente da escludere (`EU`, `NA`, `SA`, `AS`, `AF`, `OC`) |
| `continent_whitelist` | *(vuoto)* | Accetta solo questi continenti (prevale sulla blacklist) |

Un mirror deve passare **entrambi** i filtri continente e paese. La whitelist vince sulla blacklist a ciascun livello. Usa `urpm config show` per vedere le impostazioni effettive.

## Chiavi GPG

```bash
urpm key list                 # Elenca le chiavi GPG installate
urpm key import <file|url>    # Importa una chiave GPG
urpm key remove <keyid>       # Rimuovi una chiave GPG
```

## Dipendenze di build

Installa le dipendenze di build per la costruzione di RPM:

```bash
urpm install --buildrequires foo.spec    # Da un file spec
urpm install --buildrequires foo.src.rpm # Da un RPM sorgente
urpm i -b                                # Auto-rileva nell'albero di build RPM
urpm i --br                              # Alias corto

# Opzioni
--sync                        # Attendi che tutti gli scriptlet terminino
```

Le dipendenze di build installate sono tracciate in `/var/lib/rpm/installed-through-builddeps.list` ed escluse dalla rimozione ordinaria degli orfani. Per ripulirle:

```bash
urpm autoremove --buildrequires          # Rimuovi tutte le build deps tracciate
urpm ar -b                               # Forma corta
```

## Sistema di build in container

urpm fornisce un sistema di build completo in container per i pacchetti RPM tramite Docker o Podman.

### Gestione delle immagini

```bash
# Elenca le immagini di build disponibili
urpm image list

# Aggiorna un'immagine esistente (re-sync media + pacchetti)
urpm image update mageia:10-build

# Cancella una o più immagini
urpm image delete mageia:10-build mageia:10-ci
```

### Creare un'immagine di build

```bash
urpm image make --release 10 --tag mageia:10-build
urpm image make --release 10 --tag mageia:10-ci --profile ci

# Immagine di build per un .spec o .src.rpm (auto-installa BuildRequires)
urpm image make --release 10 --tag mga:10-foo --buildrequires SPECS/foo.spec

# Opzioni
-r, --release <version>       # Versione Mageia (es. 10, cauldron)
-t, --tag <tag>               # Tag dell'immagine (es. mageia:10-build)
--profile <name>              # Profilo di pacchetti (default: build)
--arch <arch>                 # Architettura di destinazione (default: host)
-p, --packages <list>         # Pacchetti aggiuntivi (separati da virgola)
--buildrequires <spec|srpm>   # Installa i BuildRequires da un .spec o .src.rpm
--addmedia <NAME> <URL>       # Aggiungi un media extra dentro l'immagine (ripetibile) --
                              # es. un mirror di terze parti o interno
--import-key <URL>            # Importa una chiave pubblica GPG dentro l'immagine (ripetibile) --
                              # da abbinare a --addmedia per media di terze parti firmati
--runtime docker|podman       # Runtime del container (default: auto-rilevamento)
--keep-chroot                 # Tieni il chroot temporaneo dopo la creazione dell'immagine
-w, --workdir <path>          # Directory di lavoro per il chroot (default: /tmp)
```

> **Compatibilità all'indietro:** `urpm mkimage` è mantenuto come alias di `urpm image make`.

### Profili

I profili definiscono quali pacchetti vengono installati nell'immagine:

| Profilo | Descrizione |
|---------|-------------|
| `build` | Ambiente di build RPM (default): rpm-build, gcc, make, ecc. |
| `ci` | CI/testing: python3-pytest, git, python3-solv, ecc. |
| `minimal` | Sistema minimale utilizzabile con urpm |

I profili sono caricati da:
- `/usr/share/urpm/profiles/*.yaml` (sistema, dal pacchetto)
- `/etc/urpm/profiles/*.yaml` (aggiunte locali)

### Compilare pacchetti

Di default, `urpm build` auto-aggiorna media e pacchetti dentro il container prima di compilare, così che i build girino sempre contro l'ultimo stato del repository. Usa `--no-update` per saltare questo passo quando lavori offline o per accelerare build ripetuti.

```bash
# Build da un RPM sorgente (output in ./build-output/)
urpm build -i mageia:10-build foo-1.0-1.mga10.src.rpm

# Build da un file spec (output in workspace/RPMS/ e SRPMS/)
urpm build -i mageia:10-build SPECS/foo.spec

# Build senza auto-update di media/pacchetti prima
urpm build -i mga10-build --no-update SPECS/foo.spec

# Build con dipendenze locali (es. libfoo compilata in precedenza)
urpm build -i mageia:10-build SPECS/bar.spec -w 'RPMS/x86_64/libfoo*.rpm'

# Più dipendenze locali
urpm build -i mageia:10-build SPECS/app.spec \
    -w 'RPMS/x86_64/libfoo*.rpm' -w 'RPMS/x86_64/libbar*.rpm'

# Più build in parallelo
urpm build -i mageia:10-build *.src.rpm --parallel 4

# Packager di terze parti: tagga l'output come foo-1.0-1.mlo.mga10.x86_64.rpm
urpm build -i mageia:10-build --subrel mlo SPECS/foo.spec

# Sovrascrivi packager/vendor/dist senza toccare lo spec
urpm build -i mageia:10-build --rpmmacros ./my-macros SPECS/foo.spec

# Opzioni
-i, --image <tag>             # Immagine Docker/Podman da usare
-o, --output <dir>            # Directory di output per build da SRPM (default: ./build-output)
-w, --with-rpms <pattern>     # Pre-installa RPM locali prima del build (glob, ripetibile)
--no-update                   # Salta l'auto-update di media e pacchetti prima del build
--runtime docker|podman       # Runtime del container (default: auto-rilevamento)
-j, --parallel <N>            # Numero di build in parallelo (default: 1)
--keep-container              # Tieni il container dopo il build (per il debug)
--subrel <tag>                # Inietta %subrel TAG in modo che gli RPM di output diventino NAME-VERSION-RELEASE.TAG.DIST.ARCH.rpm
--rpmmacros <file>            # Inietta FILE come /root/.rpmmacros nel container di build (combinabile con --subrel)
```

### Layout del workspace

Per i build da file spec, urpm supporta il layout di workspace RPM standard:

```
workspace/
├── SPECS/
│   └── foo.spec
└── SOURCES/
    ├── foo-1.0.tar.gz
    └── patches/
```

I risultati sono piazzati in:
```
workspace/
├── RPMS/
│   └── x86_64/
│       └── foo-1.0-1.mga10.x86_64.rpm
└── SRPMS/
    └── foo-1.0-1.mga10.src.rpm
```

### Esempio di workflow

```bash
# 1. Crea l'immagine di build (una volta sola)
urpm image make --release 10 --tag mga:10-build

# 2. Compila un pacchetto
urpm build --image mga:10-build ./mypackage.src.rpm

# 3. Più tardi, aggiorna l'immagine per recuperare i nuovi pacchetti del repo
urpm image update mga:10-build

# 4. Controlla i risultati
ls ./build-output/
```

### Bootstrap manuale (avanzato)

Sotto il cofano, `urpm image make` chiama `urpm init` dentro un chroot
fresco per popolare il catalogo media. `urpm init` è esposto
direttamente per i chiamanti che devono bootstrap-are un rootfs fuori
dal percorso containerizzato — script di installazione, build di dischi
VM, o root di test pre-preparate. I mirror sono presi dall'API mirror
di Mageia e filtrati dalla sezione `[server]` di
`/etc/urpm/conf.d/10-server.cfg`.

```bash
# Bootstrap di un rootfs chroot per Mageia 10
urpm --urpm-root /tmp/rootfs init --release 10 --arch x86_64

# Usa una lista mirror custom
urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'

# Opzioni
--release, -r <version>     # Versione Mageia di destinazione (10, cauldron, …)
--mirrorlist <url>          # Sovrascrivi l'URL della lista mirror auto-generata
--arch <arch>               # Architettura di destinazione (default: host)
--auto, -y                  # Modalità non interattiva
--no-sync                   # Configura i media ma salta la sync iniziale
```

Dopo aver lavorato in un chroot `--urpm-root`, smonta `/dev` e `/proc`
montati da `urpm init`:

```bash
urpm --urpm-root /tmp/rootfs cleanup
```

## Strumenti per manutentori di repository

I due comandi qui sotto si rivolgono alle persone che
**pubblicano** un repository compatibile Mageia, non a quelle che
lo consumano. Li documentiamo insieme così resta ovvio quale
consegna i metadati client e quale li produce.

- **`urpm appstream`** (lato client) — aggiorna il catalogo AppStream
  sulla macchina corrente in modo che i software center vedano
  descrizioni aggiornate. Vive in `urpm-ng-appstream`.
- **`urpm genmedia`** (lato server) — produce l'insieme completo dei
  metadati media che un mirror serve ai suoi client. Vive in
  `urpm-ng-genmedia`, sotto-pacchetto separato in modo che
  l'installazione client di base resti leggera.

### Metadati AppStream (`urpm appstream`)

urpm può produrre e aggiornare i cataloghi AppStream consumati da KDE Discover e GNOME Software:

```bash
urpm appstream generate              # Genera il catalogo dal database dei pacchetti
urpm appstream generate -m core/release    # Limita a un media specifico
urpm appstream generate --no-compress       # XML puro invece di gzip
urpm appstream status                # Mostra lo stato del catalogo per media
urpm appstream merge                 # Unisci i file per-media nel catalogo unificato
urpm appstream merge --refresh       # Aggiorna anche la cache AppStream di sistema
urpm appstream init-distro           # Crea il file metainfo dell'OS (necessario per Discover/GS)
urpm appstream init-distro --force   # Sovrascrivi un metainfo esistente
```

### Generazione dei media (`urpm genmedia`)

`urpm genmedia` è il complemento lato server di `urpm appstream`: dove `appstream` consuma cataloghi per popolare i database client, `genmedia` **produce** l'insieme completo dei metadati media che un mirror Mageia serve ai suoi client. È una riscrittura Python dello storico `genhdlist3`, integrata in urpm-ng e impacchettata separatamente come `urpm-ng-genmedia` in modo che l'impronta di dipendenze resti fuori dall'installazione client di base.

A partire da una directory di file RPM:

```bash
urpm genmedia /path/to/rpms          # Default: generazione completa
urpm genmedia /path/to/rpms --incremental   # Salta gli RPM il cui SHA-256 non è cambiato
urpm genmedia /path/to/rpms --no-hdlist     # Salta l'output hdlist.cz
urpm genmedia /path/to/rpms --xml-info      # Forza la rigenerazione dei file XML info
urpm genmedia /path/to/rpms --appstream-info  # Genera il catalogo AppStream
urpm genmedia /path/to/rpms --no-md5sum     # Salta MD5SUM (più rapido per i test)
urpm genmedia /path/to/rpms --allow-empty-media  # Tollera una directory di input vuota
```

Il comando produce il layout canonico atteso da ogni client urpm-ng o urpmi:

```
media_info/
  hdlist.cz                # Header compressi dei pacchetti binari
  synthesis.hdlist.cz      # Sintesi leggera delle dipendenze
  files.xml.lzma           # Elenchi file per pacchetto
  info.xml.lzma            # URL, sourcerpm, licenza, descrizione
  changelog.xml.lzma       # Changelog per pacchetto
  appstream.xml.gz         # Quando --appstream-info è attivo
  MD5SUM                   # Checksum di tutto quanto sopra
```

Il pass AppStream estrae i file `*.metainfo.xml` embedded forniti dalle applicazioni upstream (KDE, GNOME, ecc.) e genera un componente minimale dai campi dell'header RPM per i pacchetti che ne hanno bisogno ma non ne forniscono. I pacchetti il cui contenuto è interamente non-user-facing (header devel, simboli di debug, archivi statici, librerie di runtime pure) sono **filtrati** invece di essere emessi con una categoria fallback ``System`` — ingombrerebbero Discover e GNOME Software senza mai essere installabili tramite un app store.

La directory `media_info/` è bloccata mentre una generazione è in corso, in modo che i client che leggono in concorrenza vedano sempre uno snapshot coerente.

## Messaggi README dei pacchetti

`urpm readme` mostra i messaggi README dei pacchetti presentati all'utente durante una transazione (Mageia li tiene come `README.urpmi` / `README.upgrade`):

```bash
urpm readme                          # README della transazione più recente
urpm readme --transaction <id>       # README di una transazione specifica
urpm readme --list                   # Elenca le transazioni con messaggi README
```

## Pulizia degli orfani

```bash
urpm cleandeps                # Alias per `urpm autoremove --faildeps`:
                              # rimuove le dipendenze orfane lasciate
                              # da transazioni interrotte.
```

---

# urpmd - Daemon in background

urpmd è un servizio in background che fornisce:
- API HTTP per le operazioni sui pacchetti
- Task in background pianificati
- Scoperta P2P dei peer per la condivisione LAN di pacchetti



## Endpoint dell'API

### Endpoint GET

| Endpoint | Descrizione |
|----------|-------------|
| `/` | Info sul servizio |
| `/api/ping` | Health check |
| `/api/status` | Stato del daemon |
| `/api/media` | Elenca i media configurati |
| `/api/available` | Elenca i pacchetti disponibili |
| `/api/updates` | Elenca gli aggiornamenti disponibili |
| `/api/peers` | Elenca i peer LAN scoperti |

### Endpoint POST

| Endpoint | Descrizione |
|----------|-------------|
| `/api/refresh` | Aggiorna i metadati dei media |
| `/api/available` | Interroga i pacchetti disponibili |
| `/api/announce` | Annuncia pacchetti ai peer |
| `/api/have` | Interroga se un peer ha pacchetti specifici |

## Task pianificati

Il daemon esegue automaticamente:
- Sync dei metadati dei media
- Pulizia della cache
- Check di disponibilità degli update
- Scoperta dei peer (broadcast UDP)

## Condivisione P2P dei pacchetti

Quando più macchine sulla stessa LAN eseguono urpmd, si scoprono automaticamente e possono condividere gli RPM in cache, riducendo l'uso di banda.

---

# Integrazione GUI (Discover / GNOME Software)

urpm-ng fornisce un backend PackageKit che permette ai software center grafici di gestire i pacchetti.

## Installazione

```bash
urpm install urpm-ng-desktop
```

Oppure installa il backend direttamente:
```bash
urpm install urpm-ng-packagekit-backend
```

Questo installa:
- `libpk_backend_urpm.so` — Backend PackageKit
- Servizio D-Bus `org.mageia.Urpm.v1` — Operazioni privilegiate
- Policy PolicyKit — Prompt di autorizzazione
- Configurazione AppStream — Metadati del catalogo software

## Applicazioni supportate

- **KDE Discover** — Supporto completo (ricerca, install, remove, update)
- **GNOME Software** — Supporto completo (ricerca, install, remove, update)

## Come funziona

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
│  Servizio D-Bus │
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

Una GUI Qt6 dedicata alla gestione dei pacchetti è in sviluppo. Vedi `rpmdrake/README.md` per i dettagli.

## Risoluzione dei problemi

```bash
# Verifica se il servizio D-Bus è in esecuzione
systemctl status urpm-dbus.service

# Verifica il backend PackageKit
pkcon backend-details

# Riavvia i servizi dopo un update
systemctl restart packagekit.service
systemctl restart urpm-dbus.service

# Verifica l'interfaccia D-Bus
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1
```

---

# Sviluppo & contributi

## Prerequisiti

### Porte del firewall

Vedi la sezione Prerequisiti per le porte di rete da aprire per la condivisione P2P.

### Preparare l'ambiente

Clona il repository:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

```


### Configurazione della modalità dev

Crea un file `.urpm.local` alla radice del progetto per personalizzare la modalità dev:

```bash
cd /where/is/urpm-ng

# Modalità dev (porta 9877, dati utente in ~/var/lib/urpm-dev/)
# Passa alla modalità dev
touch .urpm.local
```

Nota, puoi cambiare dove urpm e urpmd mettono i loro dati modificando il file .urpm.local:
```ini
# Directory di base custom (opzionale)
base_dir=/path/lib/urpm-dev
```

In modalità dev, di default, i dati sono memorizzati in `/var/lib/urpm-dev/` e il daemon usa la porta 9877.

**Nota che in modalità dev urpmd interagirà solo con altri urpmd in modalità dev.**

## Lanciare il daemon

```bash
# Lancia il daemon (come root, senza modalità background)

cd /where/is/urpm-ng

./bin/urpmd --dev

```

## Lanciare urpm

```bash
# Lancia urpm (come root in una console dedicata)

cd /where/is/urpm-ng

./bin/urpm --help

```

## Codice, test, contributi…

I contributi di ogni tipo sono benvenuti: codice, test, traduzioni, feedback… nessun contributo è troppo piccolo.

Vedi `CLAUDE.md` per le linee guida di sviluppo e `doc/ARCHITECTURE.md` per l'architettura tecnica.

---

# Problemi noti / TODO

- **Performance di `urpm find`** — La ricerca in files.xml è più lenta di urpmf (2.5s vs 0.6s). Necessita ottimizzazione.

---

# Licenza

GPL-3.0 — Vedi il file LICENSE per i dettagli.

# Autori

- Maât (Pascal Vilarem)
- Papoteur (Mageia Contributor)
- Claude (Assistente IA)
