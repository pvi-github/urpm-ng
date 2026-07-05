# Migreren van urpmi naar urpm-ng

Een referentie van één pagina voor Mageia-gebruikers die vertrouwd
zijn met de klassieke ``urpmi``-gereedschapskist.  ``urpm-ng``
vervangt het geheel ``urpmi`` / ``urpme`` / ``urpmq`` / ``urpmf`` /
``urpmi.addmedia`` / ``urpmi.removemedia`` / ``urpmi.update`` door
één ``urpm``-binair met subcommando's.

Elk subcommando heeft een korte alias van één letter — deze
spiekbrief gebruikt de korte vormen omdat dat is wat je dagelijks
tikt; de lange vormen (``install``, ``erase``, ``upgrade``, …) werken
identiek en lezen beter in scripts.

Eén keer lezen; bij de hand houden wanneer je iemand anders helpt te
migreren.

Parameters die je zelf invult staan tussen ``<hoekhaken>``.

## Pakketbewerkingen

| ``urpmi`` / ``urpme``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi <pkg>``                      | ``urpm i <pkg>``             |
| ``urpmi --auto <pkg>``               | ``urpm i -y <pkg>``          |
| ``urpmi --test <pkg>``               | ``urpm i --test <pkg>``      |
| ``urpme <pkg>``                      | ``urpm e <pkg>``             |
| ``urpmi --auto-update``              | ``urpm u``                   |
| ``urpmi --no-install <pkg>``         | ``urpm dl <pkg>``            |

Opmerkingen :
- ``--auto`` en ``-y`` zijn overal in ``urpm-ng`` uitwisselbaar.
- ``urpm remove`` wordt uit hoffelijkheid geaccepteerd voor
  gebruikers die van apt / dnf komen — het canonieke werkwoord is
  ``e`` (``erase``).

## Mediabeheer

| ``urpmi.*`` / ``urpmq``              | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi.update -a``                  | ``urpm m u``                 |
| ``urpmi.update <medianame>``         | ``urpm m u <medianame>``     |
| ``urpmi.addmedia <url>``             | ``urpm m a <url>``           |
| ``urpmi.addmedia --distrib <url>``   | ``urpm m disc <url>``        |
| ``urpmi.removemedia <medianame>``    | ``urpm m r <medianame>``     |
| ``urpmi.removemedia -a``             | ``urpm m r --all``           |
| ``urpmq --list-media``               | ``urpm m l``                 |

Opmerkingen :
- ``m`` is de korte alias van ``media``.  ``m u`` = ``media update``,
  ``m a`` = ``media add``, ``m r`` = ``media remove``, ``m l`` =
  ``media list``, ``m disc`` = ``media discover``.  De volledige
  vorm ``urpm media update`` enz. schrijven werkt precies hetzelfde.

## Zoekopdrachten

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

Opmerkingen :
- Korte aliassen : ``q`` = ``query`` (ook ``search``, ``s``),
  ``sh`` = ``show``, ``d`` = ``depends``, ``rd`` = ``rdepends``
  (ook ``whatrequires``, ``wr``), ``wp`` = ``whatprovides``,
  ``f`` = ``find``, ``l`` = ``list``.

## Bouwen / distributie

| Klassiek Mageia                      | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``genhdlist2 <tree>``                | ``urpm genmedia <tree>``     |
| ``rpmbuild...`` ``bm -b <spec>``     | ``urpm build <spec>``        |
| ``mach``, ``mock``, ...              | ``urpm image make`` + ...    |
|                                      | ... ``urpm build --image``   |

## Verschillen in gedrag om te kennen

- **Eén binair, subcommando's.**  Alle handelingen leven onder
  ``urpm``.  Bash-completion staat standaard geïnstalleerd.
- **``urpm.cfg`` vervangt ``urpmi.cfg``** in ``/etc/urpm/urpm.cfg``.
  Bij de eerste uitvoering leest ``urpm m import`` het oude
  ``/etc/urpmi/urpmi.cfg`` in en migreert elke ingang, ook die op
  ``MIRRORLIST`` gebaseerd — geen handmatige bewerking nodig.
- **Native rollback.**  ``urpm h`` (history) en ``urpm rollback``
  dekken elke transactie — geen behoefte aan snapshotgereedschap
  van derden.
- **P2P-LAN-cache.**  Draait ``urpmd`` op meerdere machines op
  hetzelfde LAN, dan delen ze gedownloade pakketten automatisch.
  Geen configuratie nodig.
- **Container- / build-image-ondersteuning.**  ``urpm image make``
  bouwt een minimale Mageia chroot- / container-image, klaar voor
  ``urpm build`` — geen ``mach`` / ``mock``-geknutsel meer.
- **Exitcodes zijn gestructureerd** — zie ``urpm(1)`` ``EXIT CODES``.
  De meest voorkomende komen overeen met urpmi (0 = succes, niet
  gelijk aan nul = iets om naar te kijken).

## Snelstart na installatie (indien niet als RPM geïnstalleerd)

```sh
# De media importeren die je al onder urpmi had
sudo urpm m import

# Mirrors koppelen aan de zonet geïmporteerde mirrorlist-gebaseerde media
sudo urpm srv autoconfig

# Pakketlijsten verversen
sudo urpm m u

# Je bent klaar
urpm q firefox
sudo urpm i firefox
```

## Volledige documentatie

- ``urpm --help`` (ook ``urpm <subcommando> --help``)
- ``man urpm``
- [README.md](README.md) — overzicht installatie en functies
- [CHANGELOG.md](CHANGELOG.md) — geschiedenis release per release
