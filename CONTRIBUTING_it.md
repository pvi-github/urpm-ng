# Contribuire a urpm-ng

urpm-ng è un piccolo progetto volontario (speriamo in crescita). Una manciata di manutentori, un gruppo minuscolo di tester regolari, e molto da fare. Se usi Mageia e qualcosa qui ti colpisce l'occhio, apprezzeremmo il tuo aiuto — anche un "l'ho provato, si è rotto al passo X" di cinque minuti vale più di quanto pensi.

Questo documento serve a rendere ovvio *come* puoi aiutare, qualunque sia il tuo livello di impegno. Nulla qui presume che tu abbia già patchato uno strumento di distribuzione.

## Come puoi aiutare

Cinque strade, dalla più leggera alla più pesante. Scegli quella che si adatta al tempo che hai — nessuna è di seconda classe.

### 1. Provalo e dicci cosa succede (nel bene e nel male)

La cosa più utile che un nuovo arrivato può fare. Installa urpm-ng sulla tua macchina (segui la sezione *Installation* di [`README.md`](README.md) per le istruzioni RPM attuali), usalo per un paio di giorni per quello che fai di solito con ``urpmi``, e segnala tutto ciò che ti ha sorpreso — un crash, un messaggio sbagliato, una traduzione mancante, un flusso che è sembrato goffo, ripetitivo o innaturale.

- Dove segnalare: **issue GitHub** su <https://github.com/pvi-github/urpm-ng/issues>.
- Per favore includi, al minimo:
  - La versione Mageia (``cat /etc/mageia-release``).
  - L'architettura (``uname -m``).
  - La versione di urpm-ng (``urpm --version`` — e ``rpm -q urpm-ng-core`` per confermare quale RPM è installato e se è quello di sistema).
  - La riga di comando esatta che si è comportata male, cosa hai ottenuto e cosa ti aspettavi.
- Non serve allegare log se non li chiediamo.

### 2. Traduci — o rifinisci le traduzioni esistenti

Sei lingue già tradotte (fr / de / es / it / nl / pt). La copertura è ampia ma non completa: stringhe scivolano non tradotte, alcuni msgstr suonano rigidi, e un orecchio madrelingua coglie i falsi amici che una prima passata non vede. Se una di quelle è la tua lingua madre, una passata sulle traduzioni esistenti per limare la formulazione e adottare i giri idiomatici locali è molto benvenuta.

- Le stringhe vivono nei file ``.po`` sotto [`po/`](po/); aprili nell'editor che preferisci (poedit va bene).
- Le voci vuote o ``fuzzy`` sono stringhe nuove o possibilmente obsolete — il punto più facile da cui partire.
- Esegui ``msgfmt --check-format po/<lang>.po -o /dev/null`` — se passa, passa anche il build.
- Stessa storia per la doc: i canonici ``README.md`` / ``MIGRATION.md`` / ``CHANGELOG.md`` hanno fratelli per lingua (``README_fr.md`` ecc.); anche loro beneficerebbero di una rilettura madrelingua.

### 3. Migliora la documentazione

Pagine di manuale, README, cheat sheet di migrazione, changelog — qualunque cosa in prosa. Anche una correzione di refuso è utile. Le pagine man vivono in ``man/<lang>/man1/urpm.1``; valida con ``groff -man -Tutf8 -ww man/<lang>/man1/urpm.1``.

### 4. Correggi un bug o aggiungi una piccola funzionalità

Il backlog vive in due posti:

- [`TODO.md`](TODO.md) alla radice del repo — la lista visibile.
- I vari file ``doc/TODO_*.md`` — backlog tematici e note per argomento. Alcuni sono pronti da codificare, altri hanno prima bisogno di discussione. Chiedi prima di investire un intero weekend.

Continua a leggere per il flusso build / test / patch.

### 5. Unisciti all'impianto (il più impegnativo)

Refactor, lavoro sul risolutore, job di background di ``urpmd``, lavoro sui file spec, irrigidimento di mkimage / container di build. Qui vive la roadmap tecnica del progetto. Fai un saluto prima — coordinarsi evita di pestarsi i piedi, o di farseli pestare. Non mordiamo, promesso.

## Ottenere i sorgenti e compilare

Due percorsi di build. Il **semplice** usa ``bm`` (il wrapper ``build-mageia``) sul tuo host e serve solo ``urpmi``. Il **riproducibile** usa ``urpm build`` dentro un container e richiede urpm-ng già installato.

### Dipendenze di bootstrap (una sola volta)

Su una Mageia appena installata, ``urpmi`` è disponibile ma ``sudo`` può non essere configurato — la forma classica ``su -c`` funziona ovunque:

```sh
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

# Lo strumento di build (bm) più ogni BuildRequires dichiarato dallo
# spec. --buildrequires legge lo spec direttamente, quindi la lista
# resta sincronizzata automaticamente. bm stesso non è nei
# BuildRequires dello spec (invoca rpmbuild anziché essere consumato
# da %build), da cui i due comandi.
su -c "urpmi bm && urpmi --buildrequires rpmbuild/SPECS/urpm-ng.spec"
```

### Percorso semplice — ``bm`` sull'host

```sh
make rpm-all
```

Poi installa gli RPM appena costruiti.

**Prima volta — nessun urpm-ng ancora sul sistema** — passa tutti gli RPM a ``urpmi`` in una sola volta (il filtro versione-release evita di raccogliere un build più vecchio ancora in ``RPMS/``):

```sh
RPMS=$(find rpmbuild/RPMS rpmdrake/rpmbuild/RPMS \
            -name "*-$(cat VERSION)-$(cat RELEASE).*.rpm")
su -c "urpmi $RPMS"
```

**Iterazioni successive** — il risolutore di urpm-ng scansiona automaticamente la directory sorella per gli RPM locali (segnala "Found N sibling RPMs (available for dependencies)"), quindi basta puntare ai due meta-pacchetti:

```sh
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Percorso riproducibile — build in container

Utilizzabile solo una volta che urpm-ng è installato sull'host (uovo-e-gallina alla primissima installazione). Garantisce un build pulito e isolato e permette di costruire per altre versioni di Mageia o altre architetture da un'unica postazione senza toccare l'host.

```sh
# Una sola volta: creare l'immagine di build (esempio: mga10 su x86_64).
# Il ``tag`` è il nome con cui invocherai l'immagine nei build
# successivi — creane più di uno se vuoi costruire per più
# versioni e/o architetture da una sola postazione.
su -c "urpm image make --release 10 --tag mga10-64"

# Ad ogni build successiva — entrambi gli spec (urpm-ng e rpmdrake-ng)
urpm build --image mga10-64 rpmbuild/SPECS/urpm-ng.spec \
                            rpmdrake/rpmbuild/SPECS/rpmdrake-ng.spec

# Installazione — urpm-ng è già sull'host (prerequisito di questo
# percorso), quindi ``urpm i`` sui due meta basta: il risolutore
# raccoglie automaticamente gli RPM fratelli dalla stessa directory.
su -c "urpm i \
    rpmbuild/RPMS/noarch/urpm-ng-all-$(cat VERSION)-$(cat RELEASE).*.rpm \
    rpmdrake/rpmbuild/RPMS/noarch/rpmdrake-ng-$(cat VERSION)-$(cat RELEASE).*.rpm"
```

### Eseguire i test

```sh
# Attenzione: il pytest completo richiede tempo — dai 30 ai 60 minuti.
pytest urpm/tests/
```

Vedi [`doc/TESTING.md`](doc/TESTING.md) per un promemoria pytest e le lacune di copertura note.

Per iterare in modalità dev senza ricostruire un RPM ogni volta, i file sorgente girano direttamente dal checkout — ``python -m urpm.cli.main <sottocomando>`` funziona con ``$PYTHONPATH`` che include la radice del checkout.

## Il tuo primo contributo — il giro completo

1. **Branch.** Dal branch della versione attiva (attualmente ``0.8.x`` — controlla il file ``VERSION`` alla radice del repo in caso di dubbio). ``main`` porta solo la storia rilasciata; il nuovo lavoro non ci atterra mai direttamente, ci arriva per fast-forward-merge dal branch di versione al momento del rilascio.
2. **Modifica.** Scrivi il fix o la funzionalità. Se stai toccando il risolutore, la coda di transazioni o ``urpmd``, aggiungere un test in ``urpm/tests/`` è quasi obbligatorio. Per lavoro CLI o doc, un test manuale sulla tua macchina basta.
3. **Testa in locale.** Esegui ``pytest urpm/tests/`` (suite completa per tutto ciò che è user-visible, file mirato altrimenti). Correggi ogni regressione prima di proseguire.
4. **Aggiorna la superficie visibile** se il tuo cambiamento è user-facing (un fix su un percorso di codice interno raramente ne ha bisogno):
   - aggiorna i cataloghi ``.po`` (ogni nuova stringa inglese user-facing è un nuovo msgid);
   - aggiorna ``man/<lang>/man1/urpm.1`` se un flag è stato aggiunto, rinominato o rimosso;
   - aggiorna il README / il cheat sheet MIGRATION se il cambiamento tocca i comandi quotidiani.

   La voce in ``CHANGELOG.md`` in sé è compito del manutentore al momento del rilascio, non fa parte di una PR.
5. **Commit.** Oggetto breve (~50 caratteri), prefisso conventional (``fix(zona):``, ``feat(zona):``, ``docs:``, ``chore:``, ``test:``, ``refactor:``). Il corpo spiega il *perché* — il diff mostra già il *cosa*.

Prima di aprire una pull request, ripassa questa checklist:

- [ ] ``make rpm-all`` (o il build in container) va a buon fine.
- [ ] ``pytest urpm/tests/`` passa senza regressioni.
- [ ] Hai **installato i tuoi RPM buildati in locale** e testato da quella copia installata (alza la riga ``release`` in ``rpmbuild/SPECS/urpm-ng.spec`` in locale così che il numero di RPM sia superiore a quello di sistema e si installi pulitamente sopra — solo una comodità locale, non committare mai quel bump).
- [ ] I comandi smoke ovvi funzionano ancora sul build installato, senza che il tuo cambiamento ne rompa nessuno:
  - ``urpm i <unpacchetto>`` — percorso di installazione
  - ``urpm q <unpacchetto>`` — query
  - ``urpm e <unpacchetto>`` — erase
  - ``urpm f /percorso/al/file`` — find
  - ``urpm m u`` — media update
  - ``urpm u`` — upgrade di sistema
- [ ] Il tuo branch è **rebased** sul branch bersaglio (nessun merge commit tra il tuo lavoro e la punta).
- [ ] Doc / pagine man / traduzioni aggiornate come al punto 4.

6. **Push** sul tuo fork o sul tuo branch.
7. **Apri una pull request** su GitHub. Descrivi l'intento, la copertura di test e ogni limitazione nota. Menziona la linea di release presa di mira e conferma la checklist qui sopra.
8. **Itera sulla review.** Un revisore guarderà il tuo diff e farà domande o suggerirà ritocchi. Puntiamo a uno scambio tra pari — nulla di personale, tutto sul codice. Cerchiamo di formulare le review con gentilezza; se un commento cadesse fuori bersaglio, l'intenzione non è mai ostile — la bussola è il progetto e Mageia.

## Dove raggiungerci

- **Issue & PR**: <https://github.com/pvi-github/urpm-ng>
- **Contatto diretto — Matrix**: [@maat_:matrix.org](https://matrix.to/#/@maat_:matrix.org)

## Dove vive il codice

```
urpm/                  # Sorgenti Python
  cli/                 # Interfaccia a riga di comando (urpm, sottocomandi)
  core/                # Risolutore, download, install, DB, sync
  daemon/              # urpmd (servizio di background, P2P LAN)
  genmedia/            # Generazione metadati lato server
  tests/               # Tutti i test vivono qui (non in un tests/ di root)
rpmdrake/              # Front-end GUI Qt6 (rpmdrake-ng)
pk-backend-urpm/       # Plugin C: backend PackageKit su urpm-ng
man/<lang>/man1/       # Pagine man tradotte
po/                    # Cataloghi di traduzione (.po)
doc/                   # Doc di design, piani, TODO, spec
rpmbuild/SPECS/        # Pacchettizzazione Mageia (.spec)
data/                  # Unit systemd, regole polkit, template di config
```

Per una mappa più profonda, vedi [`doc/ARCHITECTURE.md`](doc/ARCHITECTURE.md). Per il catalogo cumulativo delle funzionalità, [`FEATURES.md`](FEATURES.md).

## Aspettative di stile (breve)

- **Inglese** nel codice, nei commenti e nei messaggi di commit. Uno storico multilingua disorienta.
- **Docstring** su ogni funzione o classe pubblica. Una riga basta; spiega il *perché* solo quando non è ovvio dal nome.
- **Test** quando è pratico — la suite è una rete anti-regressione, non una prova formale. I cambiamenti user-visible dovrebbero come minimo portare una nota di test manuale.
- **Commenti** dove il codice nasconde una sorpresa (workaround, race, invariante). Mai un commento che duplica il codice.

## Ciclo di release

Il lavoro passa da un branch di versione (``0.8.x``, ``0.9.x``, …). Quando una versione è pronta, il branch viene fast-forward-mergiato in ``main``; ``main`` porta quindi la storia rilasciata. I tag vengono tagliati da ``main`` in quel momento e gli RPM vengono pubblicati sul canale binario del progetto.

I bump di versione in ``VERSION`` / ``pyproject.toml`` / ``rpmbuild/SPECS/urpm-ng.spec`` sono affare del maintainer — non committare un bump nel tuo contributo. Detto ciò, sentiti libero di alzare **localmente** la riga ``release`` dello spec così che il tuo RPM buildato si installi sopra quello di sistema; solo, non staggarla.
