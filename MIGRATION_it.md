# Migrare da urpmi a urpm-ng

Un riferimento in una pagina per gli utenti Mageia che conoscono la
strumentazione ``urpmi`` classica.  ``urpm-ng`` sostituisce l'insieme
``urpmi`` / ``urpme`` / ``urpmq`` / ``urpmf`` / ``urpmi.addmedia`` /
``urpmi.removemedia`` / ``urpmi.update`` con un unico binario
``urpm`` e i suoi sottocomandi.

Ogni sottocomando ha un alias corto di una lettera — questo bigino
usa le forme corte perché è ciò che si scrive tutti i giorni; le
forme lunghe (``install``, ``erase``, ``upgrade``, …) funzionano
identicamente e sono più leggibili negli script.

Da leggere una volta; tienilo a portata di mano quando aiuti qualcuno
a migrare.

I parametri da fornire sono indicati fra ``<parentesi angolari>``.

## Operazioni sui pacchetti

| ``urpmi`` / ``urpme``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi <pkg>``                      | ``urpm i <pkg>``             |
| ``urpmi --auto <pkg>``               | ``urpm i -y <pkg>``          |
| ``urpmi --test <pkg>``               | ``urpm i --test <pkg>``      |
| ``urpme <pkg>``                      | ``urpm e <pkg>``             |
| ``urpmi --auto-update``              | ``urpm u``                   |
| ``urpmi --no-install <pkg>``         | ``urpm dl <pkg>``            |

Note :
- ``--auto`` e ``-y`` sono intercambiabili ovunque in ``urpm-ng``.
- ``urpm remove`` è accettato per comodità degli utenti che vengono
  da apt / dnf — il verbo canonico è ``e`` (``erase``).

## Gestione dei media

| ``urpmi.*`` / ``urpmq``              | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi.update -a``                  | ``urpm m u``                 |
| ``urpmi.update <medianame>``         | ``urpm m u <medianame>``     |
| ``urpmi.addmedia <url>``             | ``urpm m a <url>``           |
| ``urpmi.addmedia --distrib <url>``   | ``urpm m disc <url>``        |
| ``urpmi.removemedia <medianame>``    | ``urpm m r <medianame>``     |
| ``urpmi.removemedia -a``             | ``urpm m r --all``           |
| ``urpmq --list-media``               | ``urpm m l``                 |

Note :
- ``m`` è l'alias corto di ``media``.  ``m u`` = ``media update``,
  ``m a`` = ``media add``, ``m r`` = ``media remove``, ``m l`` =
  ``media list``, ``m disc`` = ``media discover``.  Scrivere la
  forma completa ``urpm media update`` ecc. funziona esattamente
  allo stesso modo.

## Interrogazioni

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

Note :
- Alias corti : ``q`` = ``query`` (anche ``search``, ``s``),
  ``sh`` = ``show``, ``d`` = ``depends``, ``rd`` = ``rdepends``
  (anche ``whatrequires``, ``wr``), ``wp`` = ``whatprovides``,
  ``f`` = ``find``, ``l`` = ``list``.

## Compilazione / distribuzione

| Mageia classico                      | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``genhdlist2 <tree>``                | ``urpm genmedia <tree>``     |
| ``rpmbuild...`` ``bm -b <spec>``     | ``urpm build <spec>``        |
| ``mach``, ``mock``, ...              | ``urpm image make`` + ...    |
|                                      | ... ``urpm build --image``   |

## Differenze di comportamento da conoscere

- **Un solo binario, sottocomandi.**  Tutte le operazioni vivono
  sotto ``urpm``.  Il completamento Bash è installato di default.
- **``urpm.cfg`` sostituisce ``urpmi.cfg``** in
  ``/etc/urpm/urpm.cfg``.  Al primo lancio, ``urpm m import`` legge
  il vecchio ``/etc/urpmi/urpmi.cfg`` e migra ogni voce, incluse
  quelle basate su ``MIRRORLIST`` — nessuna modifica manuale
  necessaria.
- **Rollback nativo.**  ``urpm h`` (history) e ``urpm rollback``
  coprono ogni transazione — non serve strumentazione di snapshot di
  terze parti.
- **Cache P2P LAN.**  Se ``urpmd`` gira su più macchine della stessa
  LAN, condividono automaticamente i pacchetti scaricati.  Nessuna
  configurazione necessaria.
- **Supporto container / immagine di build.**  ``urpm image make``
  costruisce un'immagine chroot / container Mageia minima pronta per
  ``urpm build`` — niente più smanettamenti con ``mach`` / ``mock``.
- **Codici di uscita strutturati** — vedi ``urpm(1)`` ``EXIT CODES``.
  I più comuni corrispondono a urpmi (0 = successo, diverso da zero
  = qualcosa da guardare).

## Avvio rapido dopo l'installazione (se non installato come RPM)

```sh
# Importare i media che avevi già sotto urpmi
sudo urpm m import

# Attaccare i mirror ai media basati su mirrorlist appena importati
sudo urpm srv autoconfig

# Aggiornare le liste dei pacchetti
sudo urpm m u

# Sei pronto
urpm q firefox
sudo urpm i firefox
```

## Documentazione completa

- ``urpm --help`` (anche ``urpm <sottocomando> --help``)
- ``man urpm``
- [README.md](README.md) — panoramica installazione e funzionalità
- [CHANGELOG.md](CHANGELOG.md) — storia versione per versione
