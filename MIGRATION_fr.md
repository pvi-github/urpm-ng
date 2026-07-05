# Migrer de urpmi à urpm-ng

Une référence sur une page pour les utilisateurs Mageia habitués à
l'outillage ``urpmi`` classique.  ``urpm-ng`` remplace l'ensemble
``urpmi`` / ``urpme`` / ``urpmq`` / ``urpmf`` / ``urpmi.addmedia`` /
``urpmi.removemedia`` / ``urpmi.update`` par un unique binaire
``urpm`` avec des sous-commandes.

Chaque sous-commande a un alias court d'une lettre — cette anti-sèche
utilise les formes courtes parce que c'est ce qu'on tape au
quotidien ; les formes longues (``install``, ``erase``, ``upgrade``,
…) fonctionnent identiquement et sont plus lisibles dans les scripts.

À lire une fois ; à garder sous la main quand tu aides un autre
utilisateur à migrer.

Les paramètres à fournir sont notés entre ``<crochets triangulaires>``.

## Opérations sur les paquets

| ``urpmi`` / ``urpme``                | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi <pkg>``                      | ``urpm i <pkg>``             |
| ``urpmi --auto <pkg>``               | ``urpm i -y <pkg>``          |
| ``urpmi --test <pkg>``               | ``urpm i --test <pkg>``      |
| ``urpme <pkg>``                      | ``urpm e <pkg>``             |
| ``urpmi --auto-update``              | ``urpm u``                   |
| ``urpmi --no-install <pkg>``         | ``urpm dl <pkg>``            |

Remarques :
- ``--auto`` et ``-y`` sont interchangeables partout dans ``urpm-ng``.
- ``urpm remove`` est accepté par commodité pour les utilisateurs
  venant d'apt / dnf — le verbe canonique est ``e`` (``erase``).

## Gestion des médias

| ``urpmi.*`` / ``urpmq``              | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``urpmi.update -a``                  | ``urpm m u``                 |
| ``urpmi.update <medianame>``         | ``urpm m u <medianame>``     |
| ``urpmi.addmedia <url>``             | ``urpm m a <url>``           |
| ``urpmi.addmedia --distrib <url>``   | ``urpm m disc <url>``        |
| ``urpmi.removemedia <medianame>``    | ``urpm m r <medianame>``     |
| ``urpmi.removemedia -a``             | ``urpm m r --all``           |
| ``urpmq --list-media``               | ``urpm m l``                 |

Remarques :
- ``m`` est l'alias court de ``media``.  ``m u`` = ``media update``,
  ``m a`` = ``media add``, ``m r`` = ``media remove``, ``m l`` =
  ``media list``, ``m disc`` = ``media discover``.  Écrire la forme
  complète ``urpm media update`` etc. fonctionne exactement de la
  même façon.

## Requêtes

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

Remarques :
- Alias courts : ``q`` = ``query`` (aussi ``search``, ``s``),
  ``sh`` = ``show``, ``d`` = ``depends``, ``rd`` = ``rdepends``
  (aussi ``whatrequires``, ``wr``), ``wp`` = ``whatprovides``,
  ``f`` = ``find``, ``l`` = ``list``.

## Construction / distribution

| Mageia classique                     | ``urpm``                     |
|:-------------------------------------|:-----------------------------|
| ``genhdlist2 <tree>``                | ``urpm genmedia <tree>``     |
| ``rpmbuild...`` ``bm -b <spec>``     | ``urpm build <spec>``        |
| ``mach``, ``mock``, ...              | ``urpm image make`` + ...    |
|                                      | ... ``urpm build --image``   |

## Différences de comportement à connaître

- **Un seul binaire, des sous-commandes.**  Toutes les opérations
  vivent sous ``urpm``.  La complétion Bash est installée par défaut.
- **``urpm.cfg`` remplace ``urpmi.cfg``** dans ``/etc/urpm/urpm.cfg``.
  Au premier lancement, ``urpm m import`` lit l'ancien
  ``/etc/urpmi/urpmi.cfg`` et migre chaque entrée, y compris celles
  basées sur ``MIRRORLIST`` — aucune édition manuelle nécessaire.
- **Rollback natif.**  ``urpm h`` (history) et ``urpm rollback``
  couvrent chaque transaction — pas besoin d'outillage tiers de
  snapshot.
- **Cache P2P LAN.**  Si ``urpmd`` tourne sur plusieurs machines du
  même LAN, elles partagent automatiquement les paquets téléchargés.
  Aucune configuration nécessaire.
- **Support conteneur / image de build.**  ``urpm image make``
  construit une image de chroot / conteneur Mageia minimale prête
  pour ``urpm build`` — plus besoin des bricolages ``mach`` /
  ``mock``.
- **Codes de sortie structurés** — voir ``urpm(1)`` ``EXIT CODES``.
  Les plus courants correspondent à urpmi (0 = succès, non-zéro =
  quelque chose à regarder).

## Démarrage rapide après l'installation (si pas installé en tant que RPM)

```sh
# Importer les médias que tu avais déjà sous urpmi
sudo urpm m import

# Attacher les miroirs aux médias mirrorlist qu'on vient d'importer
sudo urpm srv autoconfig

# Rafraîchir les listes de paquets
sudo urpm m u

# Tu es prêt·e
urpm q firefox
sudo urpm i firefox
```

## Documentation complète

- ``urpm --help`` (aussi ``urpm <sous-commande> --help``)
- ``man urpm``
- [README.md](README.md) — présentation de l'installation et des fonctionnalités
- [CHANGELOG.md](CHANGELOG.md) — historique release par release
