# Contribuire a urpm-ng

Grazie per aver dedicato del tempo a leggere questo documento.  urpm-ng
ambisce a diventare il sostituto in Python della storica catena di
strumenti `urpmi` di Mageia, e ogni miglioramento — fix, funzionalità,
documentazione, traduzione, test — è benvenuto.

Questo progetto è portato avanti da una comunità di volontari.  Il tono
delle nostre revisioni e discussioni è *tra pari*: indichiamo il
codice, non le persone; formuliamo suggerimenti, non ordini; poniamo
domande sincere piuttosto che retoriche.

> Le traduzioni di questo documento sono disponibili in
> [`CONTRIBUTING.md`](CONTRIBUTING.md) (inglese, versione di
> riferimento),
> [`CONTRIBUTING_de.md`](CONTRIBUTING_de.md),
> [`CONTRIBUTING_es.md`](CONTRIBUTING_es.md),
> [`CONTRIBUTING_fr.md`](CONTRIBUTING_fr.md),
> [`CONTRIBUTING_nl.md`](CONTRIBUTING_nl.md) e
> [`CONTRIBUTING_pt.md`](CONTRIBUTING_pt.md).

## Obiettivi del progetto

- Codice **leggibile**: nomi espliciti, funzioni brevi, responsabilità
  chiare.
- **Documentato**: docstring su tutta l'API pubblica, commenti là dove
  il *perché* non è evidente.
- **Facile da affrontare per chi arriva**: un nuovo contributore deve
  poter comprendere un modulo senza dover leggere prima l'intero
  progetto.
- **Coerente**: convenzioni uniformi in tutto il codice.
- **Performante**: qualità e prestazioni non sono inconciliabili —
  miriamo a entrambe.

## Predisposizione dell'ambiente

Requisiti:

- Python 3.13 o più recente
- `python3-solv`, `python3-zstandard`, `python3-rpm`, `python3-curl`,
  `python3-pyyaml`
- `rpmbuild` (per le fixture dei test e la pipeline di build)

Un checkout funzionante:

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng
urpmi python3-solv python3-zstandard
pytest urpm/tests/
```

Vedere [`doc/HOWTO_TESTS.md`](doc/HOWTO_TESTS.md) per il prontuario di
pytest, e [`doc/TESTING.md`](doc/TESTING.md) per lo stato della
copertura e le lacune note.

## Flusso di lavoro

1. Diramare a partire da `main` (oppure dalla branca di release attiva
   se esiste — ad esempio `0.7.x` è rimasta attiva fino alla 0.7.15,
   `0.8.x` è quella in preparazione al momento in cui si scrive).
2. Realizzare la modifica, con i test quando possibile.
3. Eseguire i test pertinenti in locale.  I test manuali restano
   necessari per tutto ciò che è visibile all'utente — la suite è una
   rete anti-regressione, non una garanzia funzionale.
4. Aprire una pull request che descriva cosa fa la modifica *e perché*.

## Convenzioni di commit

- **Lingua**: inglese.  I messaggi misti rendono illeggibile la
  cronologia.
- **Oggetto**: ≤ 50 caratteri, prefisso conventional-commit
  (`fix(...)`, `feat(...)`, `docs(...)`, `chore(...)`, `test(...)`).
- **Corpo**: spiegare il *perché*, non solo il *cosa*.  Il diff mostra
  già il cosa.
- **Nessuna attribuzione all'IA**: l'hook di commit rifiuta le righe
  `Co-Authored-By` che nominano assistenti IA.  L'autore del commit è
  l'essere umano che ha digitato `git commit`.

## Regole di igiene git

- Mai eseguire `git add .` né `git add -A`.  Aggiungere i file uno a
  uno — evita di committare per sbaglio un `.env`, una bozza o un
  artefatto generato.
- Eseguire sempre `git status` prima del commit, così l'elenco dei
  file in staging è ben visibile.
- Mai committare senza una conferma esplicita, da parte del reviewer o
  di te stesso dopo un `git status`.
- Mai saltare gli hook di commit.  Se un hook protesta, correggere la
  causa anziché ricorrere a `--no-verify`.

## Cosa non committare

- **Bozze** nella radice del repo o sotto `doc/`.  Le bozze vive
  (`RELEASE_NOTE.md`, `doc/TODO_*.md` in corso, `SPEC_*.md` prima della
  validazione) restano nel tuo checkout locale e arrivano nel worktree
  solo una volta validate.
- **Artefatti di test locali**: `essais/` è gitignorato — riponi lì
  gli script transitori, le directory di lavoro per gli smoke test,
  gli output di `mktemp`.  Mai nella radice del repo.
- **Tutto ciò che si chiama `bin/rebuild.sh` o `bin/recup.sh`** —
  script personali del contributore che non hanno nulla da fare nel
  ramo principale.
- **Vocabolario DNF**: urpm-ng si rivolge all'ecosistema Mageia.  Un
  confronto con DNF in una nota a piè di pagina è accettabile, ma
  evitare di prendere in prestito i nomi dei comandi di DNF
  (`skip-broken`, `distro-sync`, ...) per le nostre opzioni.
- **File generati**: cataloghi `.mo`, `__pycache__/`, `*.pyc`, build di
  Sphinx — tutti gitignorati, lasciali tali.

## Convenzioni di documentazione

- **Lingue**: codice, commenti e messaggi di commit in inglese.  La
  documentazione utente sotto `doc/` è in mistura francese/inglese
  (status quo); le stringhe rivolte all'utente passano per `po/` e le
  pagine man vivono in `man/<lang>/`.
- **Tono**: didattico e azionabile.  Un rapporto deve essere
  leggibile anche da chi non ha seguito la conversazione.  Evitare
  tabelle telegrafiche con codici criptici (C1/N7) salvo quando ogni
  cella è spiegata.
- **La lunghezza non è una virtù**, ma nemmeno la brevità a scapito
  della chiarezza.  Ogni paragrafo deve offrire al lettore qualcosa
  che non saprebbe dedurre da solo.

## Test

- I test risiedono in `urpm/tests/`, **non** in una directory `tests/`
  in radice.  Sintetizza gli RPM con `urpm/tests/gen_test_rpms.py` ed
  esegui pytest da `urpm/tests/` (l'infrastruttura di test pretende
  questa directory di lavoro per alcuni test di integrazione).
- Le directory temporanee per test sono ora ripulite da una fixture
  autouse su `BaseUrpmiTest`, perciò un test che fallisce prima della
  propria pulizia esplicita non lascia più residui in `/tmp`.
- Per modifiche alle pagine man, valida con
  `groff -man -Tutf8 -ww man/<lang>/man1/urpm.1` — i warning UTF-8
  preesistenti non sono bloccanti, gli errori nuovi sì.

## Revisione del codice

Ci revisioniamo a vicenda da pari.  Quando ricevi una revisione:

- Il reviewer indica il codice, non te.
- «Perché hai fatto X?» è una domanda autentica; rispondi
  sinceramente anziché difenderti.
- «Potremmo fare Y al posto suo?» è una proposta, non un verdetto.

Quando dai una revisione:

- L'autoironia va bene, l'imposizione no.  Suggerisci, non dettare.
- Chiedi prima di presumere.  «Forse mi sfugge qualcosa, ma ...» è una
  buona apertura.
- Segnala chiaramente i blocker; i cavilli stilistici restino cavilli
  stilistici.

## Rilasci

Il lavoro di rilascio avviene su una branca di versione (`0.7.x`,
`0.8.x`, ...) e viene mergiato in fast-forward su `main` al momento del
rilascio.  `main` porta la storia dei rilasci; la branca attiva porta
il lavoro in corso.

`urpm/__init__.py` e `pyproject.toml` vengono incrementati assieme
quando si tagga un rilascio.  Mai decidere unilateralmente un numero
di versione — chiedere al responsabile del progetto.

## Dove vivono le cose

```
urpm/                  # Sorgente
  cli/                 # Interfaccia a riga di comando
  core/                # Resolver, base dati, download, install...
  daemon/              # urpmd
  genmedia/            # Generazione lato server (urpm genmedia)
  tests/               # TUTTI i test vivono qui, NON in /tests/
rpmdrake/              # Front-end grafico
man/<lang>/man1/       # Pagine man tradotte
po/                    # Cataloghi di traduzione (.po per lingua)
doc/                   # Documenti di design, piani, file TODO
rpmbuild/SPECS/        # File .spec per il packaging Mageia
data/                  # Unit systemd, regole polkit, ecc.
```

Per il catalogo cumulativo delle funzionalità di urpm-ng, vedere
[`FEATURES.md`](FEATURES.md).  Per la cronologia dei rilasci, vedere
[`CHANGELOG.md`](CHANGELOG.md).  Per i backlog, vedere
[`TODO.md`](TODO.md) e i file specifici sotto
[`doc/TODO_*.md`](doc/).
