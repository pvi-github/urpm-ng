# urpm-ng

Un gestionnaire de paquets moderne pour Mageia Linux, écrit en Python.

urpm-ng est une réécriture complète de la suite urpmi classique, offrant de meilleures performances, une résolution de dépendances plus fine, et des fonctionnalités modernes comme le partage P2P de paquets.

## Prérequis

### Distribution

Pour le moment, il faut Mageia 9 ou Mageia 10, ou Cauldron.

### Ports pare-feu (pour le partage P2P)

Le paquet `urpm-ng-daemon` livre `/etc/shorewall/rules.urpm-ng` en
fichier d'include, et son `%post` le rattache automatiquement à
`/etc/shorewall/rules`. Sur une machine gérée par Shorewall (le
défaut Mageia) les ports suivants sont donc ouverts dès l'install,
sans intervention :

- **TCP 9876** (production) ou **TCP 9877** (mode dev) -- API HTTP d'urpmd
- **UDP 9878** (production) ou **UDP 9879** (mode dev) -- Broadcasts de découverte de pairs

Si Shorewall n'est pas en service (`iptables` / `nftables` bruts),
ouvrir les ports à la main — le fichier `/etc/shorewall/rules.urpm-ng`
dans l'arbre source sert de bon gabarit.

## Installation

### Paquets

urpm-ng est découpé en plusieurs paquets pour plus de souplesse :

| Paquet | Description |
|--------|-------------|
| `urpm-ng-core` | Minimal : CLI, résolveur, base de données |
| `urpm-ng-daemon` | Daemon en arrière-plan + partage P2P |
| `urpm-ng` | Méta : tire `-core` + `-daemon` (install standard) |
| `urpm-ng-appstream` | Config des métadonnées AppStream (metainfo OS Mageia, config distro) |
| `urpm-ng-packagekit-backend` | Backend PackageKit (Discover, GNOME Software) + service D-Bus |
| `urpm-ng-desktop` | Méta : tire `-core` + `-daemon` + `-appstream` + `-packagekit-backend` |
| `urpm-ng-build` | Méta : tire `-core` (pour `urpm image` / `urpm build` — les commandes vivent dans `-core`) |
| `urpm-ng-genmedia` | Génération de métadonnées média côté serveur (`urpm genmedia`, pour les mainteneurs de miroir) |
| `urpm-ng-all` | Méta : tire tout ce qui précède |

**Choisir le bon paquet :**
- **Install minimale / conteneur** : `urpm-ng-core`
- **Utilisation CLI standard** : `urpm-ng`
- **Bureau avec logiciels GUI** : `urpm-ng-desktop`
- **Empaqueteurs (utilisateurs de bm / mkimage)** : `urpm-ng-build`
- **Mainteneurs de miroirs qui publient des dépôts** : `urpm-ng-genmedia`

### Install / mise à jour rapide (`geturpm.sh`)

`geturpm.sh` est la voie recommandée pour installer urpm-ng sur une Mageia fraîche, et il peut aussi mettre à jour une install existante.  Il auto-détecte la release Mageia et l'architecture, tire la dernière urpm-ng depuis le canal choisi, et fait ce qu'il faut selon que urpm-ng est déjà installé ou pas (les machines fraîches se bootstrapent
avec `urpmi` ; les mises à jour ultérieures passent par urpm-ng lui-même).

**Rapide — via pipe, sans inspection locale**

```bash
curl -fsSL https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh | bash
```

Les prompts (choix du canal, « Proceed ? », mot de passe root pour `su`) sont lus depuis `/dev/tty`, donc la version "pipée" reste totalement interactive — même expérience qu'en lançant le script localement.

**Vérifié — télécharger, relire, puis exécuter** (recommandé si on ne
fait pas encore confiance à la source) :

```bash
curl -fsSLO https://raw.githubusercontent.com/pvi-github/urpm-ng/main/geturpm.sh
less geturpm.sh                  # relire avant d'exécuter
bash geturpm.sh                  # interactif : demande canal + confirmation
```

**Choix du canal** (`--channel=CHAN`) :

- `mgabiz` — récupère depuis le dépôt Mageia.biz (défaut quand aucun terminal n'est disponible). Utilise `urpm media discover` sur le miroir mgabiz, donc les mises à jour ultérieures passent par le flux standard `urpm update`.
- `github` — récupère les RPM directement depuis la page des releases GitHub. Utile pour tester un tag précis, ou quand la publication mgabiz est en retard sur une release.

**Exécution non-interactive** — ajouter `-y` (saute la confirmation « Proceed ? ») et `--channel=CHAN` (saute le prompt du canal en choisissant par défaut mgabiz) via `bash -s --` :

```bash
curl -fsSL <url>/geturpm.sh | bash -s -- -y --channel=mgabiz
```

Note : à la première install, urpm-ng importe sa configuration depuis les fichiers `urpmi.cfg` et `urpmi/skip.list` existants automatiquement.

## Premier lancement

urpm marche tel quel. Les options avancées (blacklist, redlist, kernel-keep) sont documentées plus bas dans la section **Configuration**.

Quand il est installé au niveau système (dans `/usr/bin/`), urpm utilise :
- Base de données : `/var/lib/urpm/packages.db`
- Port du daemon : 9876
- Fichier PID : `/run/urpmd.pid`

### Sources de médias

Sur une install faite par voie RPM (ou via `geturpm.sh`), les médias
Mageia standards et les serveurs pour les récupérer sont mis en place
automatiquement : `urpm-ng` importe le `urpmi.cfg` existant au premier
lancement et `urpm server autoconfig` peuple le pool de miroirs depuis
l'API mirroirs Mageia. Rien d'autre à faire pour installer des paquets.

Sur une machine sans `urpmi.cfg` préexistant (chroot frais, build
d'image, ou système qui n'a jamais eu urpmi), le même bootstrap se
fait en une passe manuelle :

```bash
urpm media list                       # Rien ? bootstrap :
urpm media import                     # Lit /etc/urpmi/urpmi.cfg par défaut ; no-op si absent
urpm server autoconfig                # Tire les miroirs depuis l'API Mageia
urpm media update                     # Première sync des métadonnées
```

Pour ajouter un **dépôt communautaire** (MageiaLinux-Online, mageia.biz,
blogdrake, un miroir interne, ...), utiliser `urpm media discover` : il
lit le `media.cfg` du dépôt et ajoute tous les médias qu'il annonce
d'un coup :

```bash
urpm media discover https://www.mageia.biz/repo/Mageia/mgabiz/10/x86_64/media/
urpm media discover --dry-run https://download.mageialinux-online.org/...   # Aperçu
```

`urpm media add` est réservé aux médias custom uniques hors flux
discover — c'est-à-dire ceux qu'on sait ne pas être publiés via un
`media.cfg`. Voir la section **Gestion des médias** plus bas pour la
syntaxe.

---

# urpm - Interface en ligne de commande

## Options globales

Ces options s'appliquent à la plupart des commandes et se placent avant la sous-commande :

```bash
-V, --version              # Afficher la version d'urpm
-v, --verbose              # Sortie verbeuse
-q, --quiet                # Sortie silencieuse
--nocolor                  # Désactiver la sortie en couleurs
--root DIR                 # Utiliser DIR comme racine pour l'install RPM (chroot, config urpm depuis l'hôte)
--urpm-root DIR            # Utiliser DIR comme racine pour la config urpm ET l'install RPM
```

Les parents suivants sont hérités par les commandes transactionnelles et de requête (`install`, `upgrade`, `erase`, `download`, `depends`, …) :

```bash
--arch ARCH                # Architecture cible (défaut : système courant)
--debug COMPONENT          # Activer la sortie de debug : solver, tsrun, orphans, download, timing, all
--watched PACKAGES         # Noms de paquets séparés par virgules à surveiller pendant la résolution
```

Note : `--arch` (option parente, fixe l'architecture cible de l'opération) est distinct d'`--allow-arch` (option locale sur install/upgrade/download, autorise des architectures additionnelles en plus de l'arch système — typiquement `i686` pour wine/steam sur x86_64).

## Options d'affichage

La plupart des commandes acceptent ces options de sortie :

```bash
--show-all            # Afficher tous les éléments sans troncature
--flat                # Un élément par ligne (parsable par des scripts)
--json                # Sortie JSON (pour usage programmatique)
```

Par défaut, les longues listes sont affichées en colonnes multiples et tronquées à 10 lignes avec "... et N autres". Utiliser `--show-all` pour tout voir.

Exemples :
```bash
urpm list installed --flat          # Un paquet par ligne
urpm search firefox --json          # Sortie JSON
urpm i task-plasma --show-all       # Affiche toutes les dépendances
```

### Mise à niveau de distribution (mga N → N+1)

```bash
urpm distupgrade                        # Détection auto de la cible = version actuelle + 1
urpm distupgrade --to 11                # Version cible explicite
urpm distupgrade --to cauldron          # Cible Cauldron (rolling)
urpm distupgrade --to cauldron:12       # Cauldron pointant sur 12 (pendant un freeze)

# Options
--to <version>                # Cible explicite ; auto = version actuelle + 1
--yes / -y / --auto           # Ignorer les prompts de confirmation, drop auto
                              # des anciens médias en Stage 4
--dry-run                     # Seulement vérifs Stage 0 + refresh métadonnées
--export-plan <fichier>       # Résoudre, écrire les NEVRAs dans <fichier>,
                              # restaurer la DB (aucun téléchargement, aucune
                              # installation) — à combiner avec `urpm download
                              # --from-file` sur un peer voisin pour préparer
                              # son cache avant le vrai distupgrade
--resume                      # Reprendre un distupgrade interrompu
--abort                       # Abandonner un distupgrade en cours ou interrompu

# Nettoyage post-upgrade
urpm media remove --distupgraded   # Supprime les dépôts mga N remplacés par
                                    # leur équivalent mga N+1
```

S'exécute par étapes : vérifications préalables (détection de version, maturité de la cible, prompt saut multi-version) → bascule des dépôts → résolution du plan de la version cible + prompt de confirmation → téléchargement → installation des composants critiques du système (rpm / python / glibc) d'abord, puis le reste → rapport post-transaction (fichiers `.rpmnew`, paquets mga N résiduels, médias tiers orphelins). Redémarrez pour que les correctifs post-démarrage s'exécutent au prochain démarrage.


## Transactions atomiques vs best-effort

Depuis la 0.7.9, `urpm upgrade` tourne en mode **best-effort** par défaut : les paquets dont les dépendances ne peuvent pas être satisfaites sont retirés de la transaction et rapportés à la fin avec leur raison (dépendance manquante, mismatch de version, cascade SRPM sœur, …). La transaction est validée pour tout le reste. Passer `--atomic` pour basculer en mode strict (recommandé sur les serveurs) : tout paquet non résolvable abandonne toute la transaction.

`urpm install`, au contraire, est **atomique par défaut** : si un paquet demandé ne peut pas être installé, toute la transaction est annulée. Passer `--no-atomic` pour opter pour le mode best-effort sur le chemin d'install.

## Codes de sortie

| Code | Signification |
|------|---------------|
| 0    | Transaction réussie, aucun paquet ignoré |
| 1    | Échec dur : transaction annulée (mode atomique, réseau, permission, …) |
| 2    | Transaction partielle : réussie mais au moins un paquet a été retiré (paquets ignorés listés sur stderr avec leur raison) |

Check scriptable pour le cas partiel :

```bash
urpm upgrade --auto || [ $? -eq 2 ] && echo "ok ou partiel"
```

## Gestion des paquets

### Installer des paquets

```bash
urpm install <paquet>         # Installer un paquet
urpm i <paquet>               # Alias court

# Options
--auto, -y                    # Mode non-interactif
--test                        # Simulation (dry run)
--without-recommends          # Sauter les paquets recommandés
--with-suggests               # Installer aussi les paquets suggérés
--force                       # Forcer malgré les problèmes de dépendances
--reinstall                   # Réinstaller les paquets déjà installés (réparation)
--nosignature                 # Sauter la vérification GPG (non recommandé)
--noscripts                   # Sauter les scripts pre/post install (builds chroot/conteneur)
--no-peers                    # Désactiver le download P2P depuis les pairs LAN
--only-peers                  # Ne télécharger que depuis les pairs LAN, pas les miroirs amont
--no-atomic                   # Mode best-effort (défaut pour install : atomique)
--download-only               # Télécharger dans le cache, pas installer
--nodeps                      # Sauter la résolution de dépendances (avec --download-only)
--all                         # Installer toutes les familles correspondantes (ex. php8.4 + php8.5)
--install-src                 # Installer le RPM source (extrait spec/sources dans ~/rpmbuild/)
--config-policy {keep,replace,ask}  # Politique de conflit sur fichiers de config (défaut : keep)
--prefer=<prefs>              # Guider les choix d'alternatives (voir plus bas)
--allow-arch <arch>           # Autoriser des architectures supplémentaires (ex. i686 pour wine/steam)
--sync                        # Attendre l'achèvement complet (triggers post-install)
```

#### Installation guidée par préférences

Quand on installe des paquets avec alternatives (ex. phpmyadmin qui peut utiliser différentes versions PHP et serveurs web), utiliser `--prefer` pour guider les choix :

```bash
# Préférer PHP 8.4 avec Apache et php-fpm, exclure mod_php
urpm i phpmyadmin --prefer=php:8.4,apache,php-fpm,-apache-mod_php

# Préférer nginx au lieu d'apache
urpm i phpmyadmin --prefer=php:8.4,nginx,php-fpm
```

Syntaxe des préférences :
- `capability:version` — Contrainte de version (ex. `php:8.4`)
- `pattern` — Préférer les paquets fournissant cette capacité (ex. `apache`, `php-fpm`)
- `-pattern` — Défavoriser les paquets correspondants (ex. `-apache-mod_php`)

Les préférences travaillent sur REQUIRES et PROVIDES des paquets, pas sur les noms.

#### Filtrage par architecture

Par défaut, urpm ne considère que les paquets correspondant à l'architecture du système et `noarch`. Cela empêche l'install accidentelle de paquets i686 sur x86_64 quand les médias 32-bit sont activés.

Pour installer des paquets 32-bit (wine, steam, multilib) :

```bash
urpm install wine --allow-arch i686
urpm install steam --allow-arch i686

# Plusieurs architectures
urpm install monpaquet --allow-arch i686 --allow-arch armv7hl
```

### Retirer des paquets

```bash
urpm erase <paquet>           # Retirer un paquet
urpm e <paquet>               # Alias court

# Options
--auto, -y                    # Mode non-interactif
--test                        # Simulation (dry run)
--auto-orphans                # Retirer aussi les dépendances orphelines (implicite avec -y sauf --keep-orphans)
--keep-orphans                # Ne pas retirer les dépendances orphelines
--erase-recommends            # Retirer aussi les paquets seulement recommandés (pas requis)
--keep-suggests               # Garder les paquets suggérés par les paquets restants
--force                       # Forcer malgré les problèmes de dépendances
--debug {solver,tsrun,all}    # Activer la sortie de debug pour résolveur/transaction
--sync                        # Attendre l'achèvement complet (triggers post-uninstall)
```

### Mettre à jour les métadonnées (façon apt)

```bash
urpm update                   # Mettre à jour toutes les métadonnées de médias
urpm update "Core Release"    # Mettre à jour un média spécifique
```

Depuis la 0.7.x, `files.xml.lzma` est récupéré en même temps que `synthesis.hdlist.cz` dès que le média le publie — aucun flag à activer.

### Télécharger des paquets (sans installer)

```bash
urpm download <paquet>        # Télécharger un paquet dans le cache
urpm dl <paquet>              # Alias court
urpm download --only-peers pkg  # Ne télécharger que depuis les pairs LAN

# Options
--release, -r <version>       # Release cible pour download cross-release (ex. cauldron)
--buildrequires, --br [SPEC]  # Télécharger les build deps (auto-détecte ou depuis .spec/.src.rpm)
--without-recommends          # Sauter les paquets recommandés
--nodeps                      # Télécharger uniquement les paquets listés, sans dépendances
--no-peers / --only-peers     # Comme install (politique pair)
--allow-arch <arch>           # Autoriser des architectures supplémentaires
--arch <arch>                 # Hérité : architecture cible
--show-all                    # Afficher la liste complète des paquets résolus
                              # (défaut tronque à 20 avec "... et N autres")
```

### Mettre à jour les paquets

```bash
urpm upgrade                  # Mettre à jour tous les paquets
urpm u                        # Alias court
urpm upgrade <paquet>         # Mettre à jour des paquets spécifiques

# Options
--auto, -y                    # Mode non-interactif
--test                        # Simulation (dry run)
--atomic                      # Mode strict : abandonne toute la transaction sur un paquet non résolvable.
                              # Défaut : best-effort (voir "Transactions atomiques vs best-effort" plus haut).
--with-recommends             # Installer les paquets recommandés
--with-suggests               # Installer aussi les paquets suggérés
--noerase-orphans             # Garder les dépendances orphelines (ne pas les retirer)
--download-only               # Télécharger dans le cache sans appliquer la mise à jour
--nosignature                 # Sauter la vérification GPG (non recommandé)
--no-peers / --only-peers     # Désactiver / limiter aux pairs LAN
--force                       # Forcer la mise à jour malgré des problèmes de dépendances
--config-policy {keep,replace,ask}  # Politique de conflit config (défaut : keep)
--allow-arch <arch>           # Autoriser des architectures supplémentaires (ex. i686)
--sync                        # Attendre l'achèvement complet (triggers post-install)
```

### Auto-retrait des orphelins

```bash
urpm autoremove               # Retirer les dépendances inutilisées (défaut : --orphans)
urpm ar                       # Alias court

# Sélecteurs
--orphans, -o                 # Paquets orphelins (défaut)
--kernels, -k                 # Vieux kernels
--faildeps, -f                # Deps de transactions interrompues
--buildrequires, -b           # Dépendances de build (--builddeps, --br)
--all, -a                     # Tout ce qui précède

# Options
--auto, -y                    # Mode non-interactif
```

## Recherche et requête

### Rechercher des paquets

```bash
urpm search <motif>           # Rechercher par nom/résumé
urpm s <motif>                # Alias court
urpm q <motif>                # Alias query (compatibilité urpmq)

# Options
--installed                   # Rechercher uniquement dans les paquets installés
--unavailable                 # Lister les paquets installés absents de tout média
```

#### Trouver les paquets indisponibles

Lister les paquets installés mais qui ne sont plus disponibles dans aucun média configuré (comme `urpmq --unavailable`) :

```bash
urpm q --unavailable          # Lister tous les paquets indisponibles
urpm q --unavailable php      # Filtrer par motif
```

### Afficher les infos d'un paquet

```bash
urpm show <paquet>               # Afficher les détails d'un paquet
urpm info <paquet>               # Alias
urpm show --files <paquet>       # Ajoute la liste des fichiers du paquet
                                 # (rpm -ql si installé, files.xml.lzma sinon)
urpm show --changelog <paquet>   # Ajoute le journal des modifications du paquet
                                 # (rpm -q --changelog ; paquets installés uniquement)
```

### Lister les paquets

```bash
urpm list installed           # Lister les paquets installés
urpm list available           # Lister les paquets disponibles
urpm list updates             # Lister les mises à jour disponibles
urpm list upgradable          # Alias pour updates
```

### Dépendances

```bash
urpm depends <paquet>         # Afficher ce qu'un paquet requiert
urpm rdepends <paquet>        # Afficher ce qui requiert un paquet (deps inverses)
urpm why <paquet>             # Expliquer pourquoi un paquet est installé

# Options pour depends
--tree                        # Afficher l'arbre de dépendances
--prefer=<prefs>              # Filtrer par préférences (même syntaxe qu'install)
--legend                      # Afficher la légende des symboles après l'arbre

# Options pour rdepends
--tree                        # Afficher l'arbre de dépendances inverses
--all                         # Afficher toutes les dépendances inverses récursives (plat)
--depth=N                     # Profondeur max de l'arbre (défaut : 3)
--hide-uninstalled            # Ne montrer que les chemins menant à des paquets installés
--legend                      # Afficher la légende des symboles après l'arbre
```

Exemple avec préférences :
```bash
# Afficher les deps de phpmyadmin en préférant PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

Exemple avec rdepends :
```bash
# Afficher l'arbre de deps inverses pour rtkit, profondeur 10, uniquement les chemins installés
urpm rdepends --tree --hide-uninstalled --depth=10 rtkit
```

### Dépendances faibles

```bash
urpm recommends <paquet>      # Afficher les paquets recommandés par un paquet
urpm whatrecommends <paquet>  # Afficher les paquets qui recommandent un paquet
urpm suggests <paquet>        # Afficher les paquets suggérés par un paquet
urpm whatsuggests <paquet>    # Afficher les paquets qui suggèrent un paquet
```

### Requêtes sur les fichiers

```bash
urpm provides <paquet>        # Lister les fichiers fournis par un paquet
urpm whatprovides <fichier>   # Trouver quel paquet fournit un fichier
urpm find <motif>             # Chercher des fichiers dans les paquets (installés + disponibles)
urpm find -i <motif>          # Chercher uniquement dans les paquets installés
urpm find -a <motif>          # Chercher uniquement dans les paquets disponibles
urpm find <motif> --all-versions  # Inclure toutes les EVR qui livrent le match
urpm find <motif> --limit 500     # Relever le cap par défaut de 100 hits
```

`urpm find` cherche par défaut à la fois dans les paquets installés et disponibles. `files.xml.lzma` est récupéré automatiquement à chaque `urpm media update` (conditionnellement au fait que le média l'annonce dans `MD5SUM`), donc aucun opt-in nécessaire — le toggle `--sync-files` a été retiré en 0.7.x.

## Marquage de paquets

```bash
urpm mark manual <paquet>     # Marquer comme installé manuellement
urpm mark auto <paquet>       # Marquer comme auto-installé (dépendance)
urpm mark show <paquet>       # Afficher la raison d'installation
```

## Blocages de paquets (holds)

Bloquer des paquets pour empêcher les mises à jour et le remplacement par des obsoletes :

```bash
urpm hold <paquet>            # Bloquer un paquet
urpm hold <paquet> -r "raison"  # Bloquer avec une raison
urpm hold                     # Lister les paquets bloqués
urpm unhold <paquet>          # Retirer le blocage
```

Les paquets bloqués sont protégés contre :
- Les mises à jour de version pendant `urpm upgrade`
- Le remplacement par des paquets qui les obsolètent

Exemple :
```bash
# dhcpcd obsolète dhcp-client, mais on veut garder dhcp-client
urpm hold dhcp-client -r "Prefer dhcp-client over dhcpcd"

# Maintenant urpm upgrade va sauter dhcp-client et prévenir :
#   Paquets bloqués (1) sautés :
#     dhcp-client (serait obsolété par dhcpcd)

# Pour autoriser le remplacement plus tard :
urpm unhold dhcp-client
```

## Historique et annulation

```bash
urpm history                  # Afficher l'historique des transactions (20 dernières)
urpm history -i               # Filtre : transactions d'install uniquement
urpm history -r               # Filtre : transactions de remove uniquement
urpm history -d <id>          # Afficher les détails de la transaction <id>
urpm history --delete <id>... # Supprimer des transactions du log

urpm undo [id]                # Annuler une transaction (défaut : la dernière). Enregistre
                              # une entrée propre dans l'historique. Utiliser --auto/-y pour
                              # sauter le prompt.

urpm rollback <n>             # Rollback des n dernières transactions
urpm rollback to <id>         # Rollback jusqu'à une transaction précise
urpm rollback to <date>       # Rollback jusqu'à une date (AAAA-MM-JJ ou JJ/MM/AAAA)
```

## Transactions en arrière-plan

Quand une transaction est détachée (ex. via le daemon ou PackageKit), suivre sa progression avec :

```bash
urpm progress                 # Afficher la progression courante et sortir
urpm progress --watch         # Surveiller en continu jusqu'à la fin
```

## Identité de distribution (`distro-switch`)

Une machine porte une identité de release à la fois — soit une stable
numérique (`10`, `11`, …), soit `cauldron`. Cette identité pilote quels
médias le résolveur considère quand il compose une transaction d'install
ou d'upgrade ; les médias dont le `mageia_version` ne match pas sont
laissés hors du pool, même s'ils sont encore activés en DB.

Basculer d'identité est un acte délibéré (une dist-upgrade en filigrane),
il vit donc dans son propre verbe plutôt que dans `urpm config`.

```bash
urpm distro-switch cauldron     # bascule la machine sur cauldron
urpm distro-switch 11           # bascule sur l'arbre mga11 numérique
urpm distro-switch cauldron:12  # cauldron avec un numérique cible explicite
```

Avant d'appliquer la bascule, la commande :

- Vérifie qu'au moins un média activé porte déjà l'identité cible (sinon
  vous vous retrouveriez avec un pool de candidats vide). Le diagnostic
  pointe vers `urpm media autoconfig -r <cible>` en cas d'échec.
- Alerte sur les médias de l'ancienne identité qui restent activés — ils
  vont disparaître du champ de vision du résolveur jusqu'à ré-alignement
  ou désactivation.
- Rafraîchit best-effort le `system-numeric` (le numérique effectif utilisé
  pour rendre les tags de release `.mgaN` et pour seeder
  `/etc/mageia-release` dans les conteneurs de build) : le paramètre
  explicite gagne en premier, puis l'identité elle-même si numérique,
  sinon un sondage du `media.cfg` d'un serveur activé.

Après la bascule, lance `urpm media update` pour récupérer les
méta-données de la nouvelle identité.

## Gestion des médias

```bash
urpm media list               # Lister les médias configurés
urpm media add <url>          # Ajouter un média Mageia officiel (auto-parsé)
urpm media add --custom "Nom" nom_court <url>  # Ajouter un média custom / tiers
urpm media remove <nom>...    # Retirer un ou plusieurs médias
urpm media remove --all       # Retirer TOUS les médias configurés (demande
                              # confirmation ; ajouter -y/--auto la saute).
                              # Les serveurs orphelins (sans média) sont
                              # retirés dans la même passe.
urpm media enable <nom>       # Activer un média
urpm media disable <nom>      # Désactiver un média
urpm media update [nom]       # Mettre à jour les métadonnées des médias
urpm media import <fichier>   # Importer depuis urpmi.cfg
urpm media link <nom> +srv -srv  # Lier/délier des serveurs à un média
urpm media set <nom> [opts]   # Modifier les paramètres d'un média (sharing, replication, quota…)
urpm media seed-info <nom>    # Afficher les infos du seed set (sections, nb paquets, taille estimée)
urpm media autoconfig -r 10   # Auto-ajouter les médias Mageia officiels pour la release 10
urpm media discover <url>     # Découvrir les médias depuis un media.cfg de repo
```

Flags utiles pour `urpm media add` :

```bash
--import-key                  # Importer la clé GPG annoncée par le média
--allow-unsigned              # Autoriser les paquets non signés (médias custom uniquement)
--version <ver>               # Version Mageia cible (médias custom uniquement : 9, 10, cauldron…)
--update                      # Marquer comme média de mises à jour
--disabled                    # Ajouter mais laisser désactivé
-y, --auto                    # Non-interactif : accepter le nom/short_name auto-détecté
```

### Importer les médias depuis un urpmi.cfg existant

Migrer une machine Mageia existante de `urpmi` vers urpm-ng sans
ré-ajouter chaque source à la main. Les entrées par URL et les
entrées `MIRRORLIST=` sont importées — ces dernières comme médias
pending que `urpm server autoconfig` viendra équiper en serveurs
au prochain run.

```bash
urpm media import /etc/urpmi/urpmi.cfg    # Chemin par défaut
urpm media import                          # Idem (défaut à /etc/urpmi/urpmi.cfg)

# Options
--replace                     # Écraser les médias existants correspondants par short_name
-r, --release <version>       # Release Mageia cible (défaut : valeur de /etc/mageia-release)
--arch <arch>                 # Architecture cible (défaut : `uname -m`)
-y, --auto                    # Non-interactif : sauter la confirmation
```

### Découvrir les médias depuis un dépôt

Découvrir tous les médias disponibles depuis n'importe quel dépôt compatible Mageia (miroirs officiels, dépôts communautaires comme MLO, miroirs d'entreprise) :

```bash
urpm media discover https://repo.example.org/9/x86_64/media/       # Ajouter tous les médias
urpm media discover --dry-run https://repo.example.org/9/x86_64/media/  # Aperçu uniquement
urpm media discover --sources --debug https://...                   # Inclure SRPMS et debug

# Force-active / force-désactive des catégories (nonfree, tainted, 32bit, all)
urpm media discover --with nonfree,tainted https://...
urpm media discover --without nonfree https://...
urpm media discover --with all https://...
```

La commande récupère `media.cfg` du dépôt, découvre tous les médias, et lie les serveurs existants qui hébergent le même contenu (vérifié par checksum MD5 de `synthesis.hdlist.cz`).

### Liaison serveur-média

Lier ou délier des serveurs à des sources média spécifiques :

```bash
urpm media link "Core Release" +mirror1 +mirror2   # Ajouter des serveurs
urpm media link "Core Updates" -oldserver          # Retirer un serveur
urpm media link "Core Release" +all                # Ajouter tous les serveurs disponibles
urpm media link "Core Release" -all +preferred     # Reset et en ajouter un
```

Note : quand on ajoute des serveurs, urpm vérifie que le contenu média correspond en comparant les checksums MD5 de `synthesis.hdlist.cz` avec les serveurs de référence existants.

### Auto-configurer les médias

Ajouter automatiquement les médias Mageia officiels pour une release :

```bash
urpm media autoconfig --release 10              # Ajouter tous les médias officiels pour Mageia 10
urpm media autoconfig -r cauldron               # Ajouter les médias pour Cauldron
urpm media autoconfig -r 10 --no-nonfree        # Sauter les médias nonfree
urpm media autoconfig -r 10 --no-tainted        # Sauter les médias tainted
urpm media autoconfig -r 10 -n                  # Dry-run : montre ce qui serait ajouté
```

### Paramètres de média

Configurer le partage et la réplication des médias :

```bash
urpm media set "Core Release" --shared=yes           # Partager avec les pairs P2P
urpm media set "Core Release" --replication=seed     # Réplication complète (DVD-like)
urpm media set "Core Release" --replication=on_demand  # Cache ce qui est téléchargé
urpm media set "Core Release" --quota=5G             # Limiter la taille du cache
urpm media set "Core Release" --retention=30         # Garder les paquets 30 jours
urpm media set "Core Release" --priority=10          # Priorité supérieure
urpm media set "Core Release" --seeds=INSTALL,CAT_PLASMA5  # Sections de seed
```

Exemples :
```bash
# Ajouter un média Mageia officiel (serveur et média auto-détectés)
urpm media add https://ftp.belnet.be/mageia/distrib/9/x86_64/media/core/release/

# Ajouter un média tiers custom
urpm media add --custom "RPM Fusion" rpmfusion https://download1.rpmfusion.org/free/fedora/40/x86_64/os/
```

## Gestion des serveurs

Les serveurs sont des sources de miroirs qui peuvent servir plusieurs médias. urpm accepte plusieurs serveurs par média pour l'équilibrage de charge et le failover.

```bash
urpm server list              # Lister les serveurs configurés (avec pays)
urpm server add <nom> <url>   # Ajouter un serveur (teste l'IP et scanne les médias)
urpm server remove <nom> ...  # Retirer un ou plusieurs serveurs
urpm server enable <nom>      # Activer un serveur
urpm server disable <nom>     # Désactiver un serveur
urpm server priority <nom> <n>  # Fixer la priorité du serveur (plus haut = préféré)
urpm server test [nom]        # Tester la connectivité et détecter le mode IP
urpm server ip-mode <nom> <mode>  # Fixer le mode IP (auto/ipv4/ipv6/dual)
urpm server autoconfig        # Auto-ajouter des serveurs depuis l'API mirroirs Mageia
urpm server stats [nom]       # Afficher les statistiques de performance d'un serveur
urpm server status            # Afficher les serveurs blacklistés / à faible réputation
urpm server unblacklist <nom> # Lever le blacklist d'un serveur (après revue)
urpm server ack-blacklist <nom>  # Acquitter un blacklist (silence le banner sans lever le blacklist)
```

### Liste des serveurs

Options pour urpm server list :
```bash
--all                 # Afficher tous les serveurs y compris les désactivés
```

### Mode IP

Chaque serveur a un mode IP pour gérer la connectivité IPv4/IPv6 :
- `auto` — Laisser le système décider (peut causer un timeout de 30s si IPv6 échoue)
- `ipv4` — Forcer IPv4 uniquement
- `ipv6` — Forcer IPv6 uniquement
- `dual` — Les deux marchent, préférer IPv4 (recommandé pour les serveurs dual-stack)

Le mode IP est auto-détecté à l'ajout du serveur. Utiliser `server test` pour re-détecter ou `server ip-mode` pour fixer manuellement.

### Suivi de bande passante et failover automatique

urpm suit automatiquement la performance de download de chaque serveur. Après chaque download ou sync de métadonnées, la vitesse mesurée est enregistrée avec une EWMA (Exponentially Weighted Moving Average, α=0.3), donnant une inertie de façon qu'un unique transfert lent ne pénalise pas injustement un bon serveur.

Les serveurs sont essayés dans l'ordre `priority DESC, bandwidth_kbps DESC` : si un serveur échoue pendant un download ou une sync de métadonnées, le suivant meilleur est essayé automatiquement sans intervention utilisateur. Dans une même session, des estimations de vitesse par serveur sont aussi gardées en mémoire, l'ordre s'adapte en temps réel sans attendre le prochain run.

`urpm server autoconfig` mesure la latence vers tous les candidats miroirs et persiste les résultats, donc l'ordre des serveurs est pertinent dès le tout premier download.

### Blacklist et réputation

Un serveur qui sert un RPM corrompu ou non signé est **auto-blacklisté** :
il est exclu des downloads suivants jusqu'à revue humaine. Les échecs
de signature sont traités comme des signaux actifs de manipulation —
pas d'auto-unblock temporel.

En parallèle du blacklist, urpm maintient une **réputation glissante à
24 h** (baseline 100) qui décroît sur les corps corrompus, les HTTP
4xx/5xx, les erreurs réseau et les transferts lents. Le score
réordonne le pool sans exclure les serveurs pour autant.

```bash
urpm server status               # Lister les serveurs blacklistés / à faible réputation
urpm server unblacklist <nom>    # Lever le blacklist après revue humaine
urpm server ack-blacklist <nom>  # Acquitter (silence le banner sans lever le blacklist)
```

Au moment d'`install` / `upgrade` / `media update`, un banner rouge
persistant liste chaque blacklist non acquitté avec les instructions
de réactivation — le banner ne disparaît pas de lui-même, seuls
`unblacklist` ou `ack-blacklist` le silencent.

`urpm server list` affiche en rouge les lignes blacklistées, un
coup d'œil sur le pool suffit pour savoir qui est écarté.

### Filtrage géographique

Les serveurs découverts depuis l'API mirroirs Mageia portent des méta-données de pays et continent. La section de configuration `[server]` (voir plus bas) permet de restreindre les miroirs acceptés :

```ini
# /etc/urpm/conf.d/10-server.cfg
[server]
country_blacklist = UA, RU        # Exclure des pays spécifiques
continent_whitelist = EU          # Uniquement les miroirs européens
```

Le filtrage est appliqué à l'ajout de miroirs (`urpm init`, `urpm media autoconfig`, `urpm server autoconfig`, et expansion du pool en arrière-plan). Les serveurs déjà en base sont complétés avec leur pays au premier run ; ceux qui échouent le filtre sont désactivés automatiquement.

Positionner `auto_add = false` pour empêcher tout ajout automatique de miroir.

Utiliser `urpm server stats [nom]` pour inspecter les métriques collectées :

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

## Gestion des pairs

Quand urpmd tourne sur plusieurs machines du même LAN, elles se découvrent mutuellement et partagent les paquets mis en cache (P2P).

```bash
urpm peer list                # Lister les pairs découverts
urpm peer downloads [host]    # Afficher les paquets téléchargés depuis les pairs (filtre par host)
urpm peer blacklist <host>    # Bloquer un pair (ex. s'il fournit de mauvais paquets)
urpm peer unblacklist <host>  # Débloquer un pair
urpm peer clean <host>        # Supprimer les RPMs téléchargés depuis un pair spécifique
                              # (à utiliser après blacklistage ; <host> obligatoire)
```

### Mode local uniquement

Utiliser `--only-peers` pour télécharger exclusivement depuis les pairs LAN sans fallback vers les miroirs amont :

```bash
urpm i --only-peers firefox   # Installer uniquement si disponible depuis les pairs
urpm u --only-peers           # Mettre à jour uniquement avec les paquets des pairs
urpm download --only-peers pkg  # Télécharger uniquement depuis les pairs
```

Utile pour les réseaux air-gapped ou pour garantir que tous les paquets viennent de sources locales de confiance.

## Gestion du cache

```bash
urpm cache info               # Afficher les infos de cache
urpm cache clean              # Retirer les RPMs orphelins du cache
urpm cache rebuild            # Reconstruire la base de paquets depuis les fichiers synthesis
urpm cache rebuild-fts        # Reconstruire l'index FTS pour la recherche rapide de fichiers
urpm cache stats              # Statistiques détaillées
```

`urpm cache clean` accepte `--dry-run/-n` (aperçu), `--auto/-y` (sans confirmation) et `--verbose/-v` (liste chaque fichier orphelin).

## Miroir / Réplication

urpm-ng peut répliquer localement un sous-ensemble de paquets (similaire à un jeu d'install DVD) et les exposer aux pairs LAN. Utile pour les install parties, les installations hors-ligne, et pour monter un miroir en interne.

Deux briques :

- **Politique par média** — `urpm media set <nom> --replication=…`
  contrôle comment chaque média est répliqué (métadonnées seules, cache
  à la demande, ou seed complet).
- **`urpm mirror` top-level** — état global côté daemon (quotas,
  versions servies, limite de bande passante sortante) et déclencheurs
  explicites de maintenance.

### Contrôle top-level du miroir

```bash
urpm mirror status            # Afficher l'état du miroir, quotas et versions servies
urpm mirror enable            # Commencer à servir les paquets en cache aux pairs
urpm mirror disable           # Arrêter de servir les paquets
urpm mirror quota [SIZE]      # Afficher ou fixer le quota global du cache (ex. 10G, 500M)
urpm mirror enable-version 10,cauldron   # Reprendre le service pour ces versions
urpm mirror disable-version 8,9          # Arrêter le service pour ces versions
urpm mirror clean [-n]        # Forcer quotas et politiques de rétention (--dry-run aperçu)
urpm mirror sync [média]      # Forcer une sync de réplication pour les médias en politique `seed`
urpm mirror sync --latest-only           # Sync plus petite, DVD-like
urpm mirror rate-limit [on|off|N/min]    # Configurer la limite de débit sortant
```

### Réplication basée sur seed

La réplication utilise le fichier `rpmsrate-raw` de Mageia pour déterminer quels paquets mirrorer (même logique que le contenu DVD).

```bash
# Activer la réplication seed-based sur un média
urpm media set "Core Release" --replication=seed
urpm media set "Core Updates" --replication=seed

# Voir le seed set calculé
urpm media seed-info "Core Release"
# Sortie :
#   Sections : INSTALL, CAT_PLASMA5, CAT_GNOME, …
#   Paquets seed depuis rpmsrate : 437
#   Motifs locale : 3
#   Paquets locale étendus : +237
#   Avec dépendances : 2300 paquets
#   Taille estimée : ~3.5 GB

# Forcer la sync (télécharger les paquets manquants)
urpm mirror sync

# Sync uniquement la dernière version de chaque paquet (plus petit, DVD-like)
urpm mirror sync --latest-only
```

### Comment ça fonctionne

1. Parse `/usr/share/meta-task/rpmsrate-raw` (depuis le paquet meta-task)
2. Extrait les paquets des sections : INSTALL, CAT_PLASMA5, CAT_GNOME, CAT_XFCE, etc.
3. Étend les motifs de locales (ex. `libreoffice-langpack-ar` → toutes les langpacks)
4. Résout les dépendances (Requires + Recommends)
5. Télécharge les paquets manquants en parallèle

Les sections seed par défaut couvrent tous les environnements de bureau majeurs et applications, résultant en ~5 GB de paquets (comparable à un DVD Mageia).

### Politiques de réplication

```bash
urpm media set <nom> --replication=none       # Métadonnées seulement, pas de paquets
urpm media set <nom> --replication=on_demand  # Cache ce qui est téléchargé (défaut)
urpm media set <nom> --replication=seed       # Contenu DVD-like depuis rpmsrate
```

## Configuration

### Blacklist (ne jamais installer/mettre à jour)

```bash
urpm config blacklist list    # Afficher les paquets blacklistés
urpm config blacklist add <pkg>
urpm config blacklist remove <pkg>
```

### Redlist (prévenir avant auto-remove)

```bash
urpm config redlist list      # Afficher les paquets redlistés
urpm config redlist add <pkg>
urpm config redlist remove <pkg>
```

### Gestion du kernel

```bash
urpm config kernel-keep       # Afficher combien de kernels garder
urpm config kernel-keep <n>   # Fixer le nombre de kernels à garder
```

### Mode de version (système vs cauldron)

Quand système et cauldron sont tous deux configurés, `version-mode` choisit qui gagne pour les mises à jour :

```bash
urpm config version-mode              # Afficher le mode courant
urpm config version-mode system       # Rester sur la version système installée
urpm config version-mode cauldron     # Rouler avec cauldron
urpm config version-mode auto         # Retirer la préférence explicite
```

### Hooks d'auto-upgrade pour les software centers

Contrôler si GNOME Software, KDE Discover ou le chemin d'update offline de PackageKit peuvent installer des mises à jour de leur propre initiative :

```bash
urpm config gnome-auto-upgrades [yes|no]      # GNOME Software
urpm config discover-auto-upgrades [yes|no]   # KDE Discover
urpm config packagekit-auto-upgrades [yes|no] # Updates offline PackageKit
```

Sans argument, chaque sous-commande affiche le réglage courant. Ces hooks toggle les réglages dconf/PolicyKit côté bureau ; la politique système est appliquée séparément par le paquet `urpm-ng-desktop`.

### Inspecter ou éditer la configuration

```bash
urpm config show              # Afficher la config effective depuis tous les *.cfg
urpm config edit              # Ouvrir urpm.cfg dans $EDITOR
urpm config edit 00-urpmi-compat   # Ouvrir un drop-in spécifique
```

### Sélection de serveur

La section `[server]` dans `/etc/urpm/conf.d/10-server.cfg` contrôle la sélection automatique de miroir :

| Clé | Défaut | Description |
|-----|--------|-------------|
| `auto_add` | `true` | Autoriser l'ajout automatique de miroirs |
| `country_blacklist` | *(vide)* | Codes ISO 3166 séparés par virgule à exclure (ex. `UA, RU`) |
| `country_whitelist` | *(vide)* | N'accepter que ces pays (l'emporte sur blacklist) |
| `continent_blacklist` | *(vide)* | Codes continent à exclure (`EU`, `NA`, `SA`, `AS`, `AF`, `OC`) |
| `continent_whitelist` | *(vide)* | N'accepter que ces continents (l'emporte sur blacklist) |

Un miroir doit passer **les deux** filtres continent et pays. Whitelist gagne sur blacklist à chaque niveau. Utiliser `urpm config show` pour voir les réglages effectifs.

## Clés GPG

```bash
urpm key list                 # Lister les clés GPG installées
urpm key import <fichier|url> # Importer une clé GPG
urpm key remove <keyid>       # Retirer une clé GPG
```

## Dépendances de build

Installer les dépendances de build pour la construction RPM :

```bash
urpm install --buildrequires foo.spec    # Depuis un fichier spec
urpm install --buildrequires foo.src.rpm # Depuis un RPM source
urpm i -b                                # Auto-détecte dans l'arbre de build RPM
urpm i --br                              # Alias court

# Options
--sync                        # Attendre que tous les scriptlets se terminent
```

Les dépendances de build installées sont trackées dans `/var/lib/rpm/installed-through-builddeps.list` et exclues du retrait d'orphelins normal. Pour les nettoyer :

```bash
urpm autoremove --buildrequires          # Retirer toutes les build deps trackées
urpm ar -b                               # Forme courte
```

## Système de build en conteneur

urpm fournit un système de build complet en conteneur pour les paquets RPM via Docker ou Podman.

### Gestion d'images

```bash
# Lister les images de build disponibles
urpm image list

# Mettre à jour une image existante (re-sync médias + paquets)
urpm image update mageia:10-build

# Supprimer une ou plusieurs images
urpm image delete mageia:10-build mageia:10-ci
```

### Créer une image de build

```bash
urpm image make --release 10 --tag mageia:10-build
urpm image make --release 10 --tag mageia:10-ci --profile ci

# Image de build pour un .spec ou .src.rpm (auto-installe BuildRequires)
urpm image make --release 10 --tag mga:10-foo --buildrequires SPECS/foo.spec

# Options
-r, --release <version>       # Version Mageia (ex. 10, cauldron)
-t, --tag <tag>               # Tag d'image (ex. mageia:10-build)
--profile <name>              # Profil de paquets (défaut : build)
--arch <arch>                 # Architecture cible (défaut : hôte)
-p, --packages <list>         # Paquets additionnels (séparés par virgule)
--buildrequires <spec|srpm>   # Installer les BuildRequires depuis un .spec ou .src.rpm
--addmedia <NAME> <URL>       # Ajouter un média supplémentaire dans l'image (répétable) --
                              # ex. un miroir tiers ou interne
--import-key <URL>            # Importer une clé publique GPG dans l'image (répétable) --
                              # à combiner avec --addmedia pour des médias tiers signés
--runtime docker|podman       # Runtime de conteneur (défaut : auto-détection)
--keep-chroot                 # Garder le chroot temporaire après création de l'image
-w, --workdir <path>          # Répertoire de travail pour le chroot (défaut : ~/.cache/urpm/mkimage).
                              # Sert aussi de TMPDIR au commit podman pour que les blobs
                              # d'image ne débordent pas sur un /tmp étroit.
--exclude PKG                 # Retire PKG de l'image finale via
                              # `urpm erase --force --keep-orphans --sync` (répétable).
                              # Usage canonique : `--exclude python3-zstandard` pour que
                              # mach de firefox ne se prenne pas sa propre contrainte.
--urpm-ng-source auto|local|media|github
                              # Origine d'urpm-ng-core (défaut : cascade auto)
--urpm-ng-core <path>         # Installer urpm-ng-core depuis ce RPM précis
--allow-disttag-mismatch      # Accepte un RPM local dont le disttag sort de la
                              # fenêtre de la cible (défaut : uniquement .mgaN. pour
                              # numérique ; .mgaN. et .mga{N-1}. pour cauldron/N — le
                              # packageur qui rebuild sur sa stable est couvert sans ce flag).
```

**Identité de release dans `--release`.** L'argument accepte trois formes :

- `--release 10` — épingle l'identité de la machine sur une release stable numérique.
- `--release cauldron` — épingle sur l'arbre de développement mouvant.
  Le numérique effectif (utilisé pour les tags de release `.mgaN` et le macro
  `%mgaversion` dans les conteneurs de build) est sondé au mieux depuis le
  `media.cfg` du miroir à l'init. Hors ligne ou en cas d'échec du sondage, il
  reste indéfini et les consommateurs se rabattent sur `/etc/mageia-release`.
- `--release cauldron:11` — cauldron avec un numérique cible explicite. Prime
  sur le sondage, marche hors ligne, et supplante le miroir quand le
  `media.cfg` côté serveur est en retard pendant une fenêtre de flip.

> **Compatibilité ascendante :** `urpm mkimage` est gardé comme alias pour `urpm image make`.

### Profils

Les profils définissent quels paquets sont installés dans l'image :

| Profil | Description |
|--------|-------------|
| `build` | Environnement de build RPM (défaut) : rpm-build, gcc, make, etc. |
| `ci` | CI/testing : python3-pytest, git, python3-solv, etc. |
| `minimal` | Système minimal utilisable avec urpm |

Les profils sont chargés depuis :
- `/usr/share/urpm/profiles/*.yaml` (système, depuis le paquet)
- `/etc/urpm/profiles/*.yaml` (ajouts locaux)

### Construire des paquets

Par défaut, `urpm build` auto-met-à-jour médias et paquets dans le conteneur avant de builder, pour que les builds tournent toujours contre le dernier état du dépôt. Utiliser `--no-update` pour sauter cette étape en offline ou pour accélérer des builds répétés.

```bash
# Build depuis un RPM source (sortie vers ./build-output/)
urpm build -i mageia:10-build foo-1.0-1.mga10.src.rpm

# Build depuis un fichier spec (sortie vers workspace/RPMS/ et SRPMS/)
urpm build -i mageia:10-build SPECS/foo.spec

# Build sans auto-update des médias/paquets d'abord
urpm build -i mga10-build --no-update SPECS/foo.spec

# Build avec des dépendances locales (ex. libfoo buildée précédemment)
urpm build -i mageia:10-build SPECS/bar.spec -w 'RPMS/x86_64/libfoo*.rpm'

# Plusieurs dépendances locales
urpm build -i mageia:10-build SPECS/app.spec \
    -w 'RPMS/x86_64/libfoo*.rpm' -w 'RPMS/x86_64/libbar*.rpm'

# Plusieurs builds en parallèle
urpm build -i mageia:10-build *.src.rpm --parallel 4

# Empaqueteur tiers : tag la sortie comme foo-1.0-1.mlo.mga10.x86_64.rpm
urpm build -i mageia:10-build --subrel mlo SPECS/foo.spec

# Surcharger packager/vendor/dist sans toucher au spec
urpm build -i mageia:10-build --rpmmacros ./my-macros SPECS/foo.spec

# Options
-i, --image <tag>             # Image Docker/Podman à utiliser
-o, --output <dir>            # Répertoire de sortie pour les builds SRPM (défaut : ./build-output)
-w, --with-rpms <pattern>     # Pré-installer des RPMs locaux avant le build (glob, répétable)
--no-update                   # Sauter l'auto-update des médias et paquets avant le build
--runtime docker|podman       # Runtime de conteneur (défaut : auto-détection)
-j, --parallel <N>            # Builds isolés multi-conteneurs (défaut : 1, chaînés dans un conteneur partagé)
--stop-on-fail                # Interrompre la chaîne au premier spec en échec (défaut : continuer)
--rollback-between-builds     # Rollback des BuildRequires par spec entre chaque build (alias : --rbb)
--keep-container              # Garder le conteneur après le build (pour debug)
--subrel <tag>                # Injecte %subrel TAG pour que les RPMs de sortie deviennent NAME-VERSION-RELEASE.TAG.DIST.ARCH.rpm
--rpmmacros <file>            # Injecte FILE comme /root/.rpmmacros dans le conteneur de build (combinable avec --subrel)
--build-cpus N                # Plafond de parallélisme du build à N threads (rpmbuild %_smp_mflags = -jN
                              # + podman --cpus). Défaut : max(1, nproc - 2) — l'hôte garde deux cœurs libres
                              # pour l'usage interactif.
--build-memory SIZE           # Plafond RAM du conteneur (ex : 8G, 12000M, 16GB). Passé à podman --memory.
                              # Défaut : max(2G, MemTotal - 2G).
--full-throttle               # Raccourci : pas de plafond CPU, pas de plafond mémoire. Écrase --build-cpus
                              # et --build-memory.
--strict-memory               # Ancre --memory-swap sur --build-memory (podman tue le process au plafond RAM).
                              # Défaut : swap illimité, aligné sur mock/systemd-nspawn. À utiliser en CI où
                              # un swap silencieux se confondrait avec un timeout.
--with FEATURE                # Passe `--with FEATURE` à rpmbuild (%bcond du spec). Répétable.
--without FEATURE             # Passe `--without FEATURE` à rpmbuild (%bcond du spec). Répétable.
```

#### Caps de ressources et parité mock

Le trio `--build-cpus` / `--build-memory` / `--strict-memory` est le levier
principal pour builder des specs lourdes (firefox, thunderbird, chromium)
sur des machines qui n'ont pas 32+ GB de RAM disponible. Les défauts
laissent deux CPUs et deux GB de RAM à l'hôte pour qu'il reste utilisable,
et surtout **le swap est laissé illimité par défaut** — le conteneur peut
déverser des pages froides sur le swap de l'hôte comme le wrapper
systemd-nspawn de mock le fait. Sans ça, le rustc de firefox se prend un
`SIGKILL` bien avant d'atteindre le vrai plafond RAM sur les hôtes < 16 GB.
`--strict-memory` restaure le lien `--memory-swap` pour la CI où un swap
silencieux serait indistinguable d'un hang.

#### Passage des bcond à rpmbuild

`--with FEATURE` et `--without FEATURE` sont forwardés tels quels à
rpmbuild pour que les specs qui déclarent `%bcond_with` / `%bcond_without`
puissent être basculées sans invoquer rpmbuild à la main. Exemple : un
spec firefox qui déclare `%bcond_without unified_build` (unités de
compilation unifiées actives par défaut) peut être compilé sans elles pour
un test contraint en mémoire via
`urpm build --without unified_build ./SPECS/firefox.spec`.

### Layout du workspace

Pour les builds à partir de spec, urpm supporte le layout de workspace RPM standard :

```
workspace/
├── SPECS/
│   └── foo.spec
└── SOURCES/
    ├── foo-1.0.tar.gz
    └── patches/
```

Les résultats sont placés dans :
```
workspace/
├── RPMS/
│   └── x86_64/
│       └── foo-1.0-1.mga10.x86_64.rpm
└── SRPMS/
    └── foo-1.0-1.mga10.src.rpm
```

### Exemple de workflow

```bash
# 1. Créer l'image de build (une fois)
urpm image make --release 10 --tag mga:10-build

# 2. Builder un paquet
urpm build --image mga:10-build ./mypackage.src.rpm

# 3. Plus tard, mettre à jour l'image pour récupérer les nouveaux paquets du dépôt
urpm image update mga:10-build

# 4. Vérifier les résultats
ls ./build-output/
```

### Bootstrap manuel (avancé)

Sous le capot, `urpm image make` appelle `urpm init` dans un chroot
frais pour peupler le catalogue média. `urpm init` est exposé
directement pour les appelants qui ont besoin de bootstrapper un rootfs
hors du chemin conteneurisé — scripts d'installation, builds de disque
VM, ou racines de test préparées. Les miroirs sont pris depuis l'API
mirroirs Mageia et filtrés par la section `[server]` de
`/etc/urpm/conf.d/10-server.cfg`.

```bash
# Bootstrap un rootfs chroot pour Mageia 10
urpm --urpm-root /tmp/rootfs init --release 10 --arch x86_64

# Utiliser une liste de miroirs custom
urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'

# Options
--release, -r <version>     # Version Mageia cible (10, cauldron, …)
--mirrorlist <url>          # Surcharger l'URL de la liste de miroirs auto-générée
--arch <arch>               # Architecture cible (défaut : hôte)
--auto, -y                  # Mode non-interactif
--no-sync                   # Configurer les médias mais sauter la sync initiale
```

Après avoir travaillé dans un chroot `--urpm-root`, démonter `/dev` et
`/proc` montés par `urpm init` :

```bash
urpm --urpm-root /tmp/rootfs cleanup
```

## Outils pour mainteneurs de dépôt

Les deux commandes ci-dessous s'adressent aux personnes qui
**publient** un dépôt compatible Mageia, pas à celles qui le
consomment. On les documente ensemble pour qu'il reste évident
laquelle livre les métadonnées client et laquelle les produit.

- **`urpm appstream`** (côté client) — rafraîchit le catalogue
  AppStream sur la machine courante pour que les software centres
  voient des descriptions à jour. Vit dans `urpm-ng-appstream`.
- **`urpm genmedia`** (côté serveur) — produit l'ensemble complet
  des métadonnées média qu'un miroir sert à ses clients. Vit dans
  `urpm-ng-genmedia`, sous-paquet séparé pour que l'install client
  de base reste légère.

### Métadonnées AppStream (`urpm appstream`)

urpm peut produire et rafraîchir les catalogues AppStream consommés par KDE Discover et GNOME Software :

```bash
urpm appstream generate              # Générer le catalogue depuis la base de paquets
urpm appstream generate -m core/release    # Limiter à un média spécifique
urpm appstream generate --no-compress       # XML brut au lieu de gzip
urpm appstream status                # Afficher le statut du catalogue par média
urpm appstream merge                 # Fusionner les fichiers par média dans le catalogue unifié
urpm appstream merge --refresh       # Rafraîchir aussi le cache AppStream système
urpm appstream init-distro           # Créer le fichier metainfo de l'OS (nécessaire pour Discover/GS)
urpm appstream init-distro --force   # Écraser un metainfo existant
```

### Génération de médias (`urpm genmedia`)

`urpm genmedia` est le pendant côté serveur d'`urpm appstream` : là où `appstream` consomme des catalogues pour peupler les bases clients, `genmedia` **produit** l'ensemble complet des métadonnées média qu'un miroir Mageia sert à ses clients. C'est une réécriture Python du historique `genhdlist3`, intégrée dans urpm-ng et empaquetée séparément comme `urpm-ng-genmedia` pour que l'empreinte des dépendances reste hors de l'install client de base.

À partir d'un répertoire de fichiers RPM :

```bash
urpm genmedia /path/to/rpms          # Défaut : génération complète
urpm genmedia /path/to/rpms --incremental   # Sauter les RPMs dont le SHA-256 n'a pas changé
urpm genmedia /path/to/rpms --no-hdlist     # Sauter la sortie hdlist.cz
urpm genmedia /path/to/rpms --xml-info      # Forcer la régénération des fichiers XML info
urpm genmedia /path/to/rpms --appstream-info  # Générer le catalogue AppStream
urpm genmedia /path/to/rpms --no-md5sum     # Sauter MD5SUM (plus rapide pour les tests)
urpm genmedia /path/to/rpms --allow-empty-media  # Tolérer un répertoire d'entrée vide
```

La commande produit le layout canonique attendu par tout client urpm-ng ou urpmi :

```
media_info/
  hdlist.cz                # Headers de paquets binaires compressés
  synthesis.hdlist.cz      # Synthèse légère de dépendances
  files.xml.lzma           # Listes de fichiers par paquet
  info.xml.lzma            # URL, sourcerpm, licence, description
  changelog.xml.lzma       # Changelogs par paquet
  appstream.xml.gz         # Quand --appstream-info est activé
  MD5SUM                   # Checksums de tout ce qui précède
```

La passe AppStream extrait les fichiers `*.metainfo.xml` embarqués et livrés par les applications amont (KDE, GNOME, etc.) et génère un composant minimal depuis les champs d'en-tête RPM pour les paquets qui en ont besoin mais n'en fournissent pas. Les paquets dont le contenu est entièrement non-user-facing (headers devel, symboles de debug, archives statiques, libs runtime pures) sont **filtrés** au lieu d'être émis avec une catégorie fallback ``System`` — ils encombreraient Discover et GNOME Software sans jamais être installables via une app store.

Le répertoire `media_info/` est verrouillé pendant qu'une génération tourne, de façon que les clients qui lisent en concurrence voient toujours un snapshot cohérent.

## Messages README des paquets

`urpm readme` affiche les messages README des paquets présentés à l'utilisateur pendant une transaction (Mageia les garde comme `README.urpmi` / `README.upgrade`) :

```bash
urpm readme                          # README de la transaction la plus récente
urpm readme --transaction <id>       # README d'une transaction spécifique
urpm readme --list                   # Lister les transactions ayant des messages README
```

## Nettoyage d'orphelins

```bash
urpm cleandeps                # Alias pour `urpm autoremove --faildeps` :
                              # retire les dépendances orphelines laissées
                              # par des transactions interrompues.
```

---

# urpmd - Daemon en arrière-plan

urpmd est un service en arrière-plan qui fournit :
- API HTTP pour les opérations sur paquets
- Tâches en arrière-plan planifiées
- Découverte P2P de pairs pour le partage LAN de paquets

## Endpoints de l'API

### Endpoints GET

| Endpoint | Description |
|----------|-------------|
| `/` | Info sur le service |
| `/api/ping` | Health check |
| `/api/status` | Statut du daemon |
| `/api/media` | Liste les médias configurés |
| `/api/available` | Liste les paquets disponibles |
| `/api/updates` | Liste les mises à jour disponibles |
| `/api/peers` | Liste les pairs LAN découverts |

### Endpoints POST

| Endpoint | Description |
|----------|-------------|
| `/api/refresh` | Rafraîchit les métadonnées de médias |
| `/api/available` | Interroge les paquets disponibles |
| `/api/announce` | Annonce des paquets aux pairs |
| `/api/have` | Interroge si un pair a des paquets spécifiques |

## Tâches planifiées

Le daemon effectue automatiquement :
- Sync des métadonnées de médias
- Nettoyage du cache
- Check de disponibilité des updates
- Découverte de pairs (broadcast UDP)

## Partage P2P de paquets

Quand plusieurs machines du même LAN font tourner urpmd, elles se découvrent automatiquement et peuvent partager les paquets RPM mis en cache, réduisant l'usage de bande passante.

---

# Intégration GUI (Discover / GNOME Software)

urpm-ng fournit un backend PackageKit permettant aux software centers graphiques de gérer les paquets.

## Installation

```bash
urpm install urpm-ng-desktop
```

Ou installer directement le backend :
```bash
urpm install urpm-ng-packagekit-backend
```

Cela installe :
- `libpk_backend_urpm.so` — Backend PackageKit
- Service D-Bus `org.mageia.Urpm.v1` — Opérations privilégiées
- Politiques PolicyKit — Prompts d'autorisation
- Configuration AppStream — Métadonnées de catalogue logiciel

## Applications supportées

- **KDE Discover** — Support complet (recherche, install, remove, updates)
- **GNOME Software** — Support complet (recherche, install, remove, updates)

## Comment ça marche

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
│  Service D-Bus  │
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

Une GUI Qt6 dédiée à la gestion de paquets est en développement. Voir `rpmdrake/README.md` pour les détails.

## Dépannage

```bash
# Vérifier si le service D-Bus tourne
systemctl status urpm-dbus.service

# Vérifier le backend PackageKit
pkcon backend-details

# Redémarrer les services après update
systemctl restart packagekit.service
systemctl restart urpm-dbus.service

# Vérifier l'interface D-Bus
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1
```

---

# Développement & contribution

## Prérequis

### Ports pare-feu

Voir la section Prérequis pour les ports réseau à ouvrir pour le partage P2P.

### Mettre en place l'environnement

Cloner le dépôt :

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

```


### Configuration du mode dev

Créer un fichier `.urpm.local` à la racine du projet pour personnaliser le mode dev :

```bash
cd /where/is/urpm-ng

# Mode dev (port 9877, données utilisateur dans ~/var/lib/urpm-dev/)
# Basculer vers le mode dev
touch .urpm.local
```

Nota, on peut changer où urpm & urpmd mettent leurs données en éditant le fichier .urpm.local :
```ini
# Répertoire de base custom (optionnel)
base_dir=/path/lib/urpm-dev
```

En mode dev, par défaut, les données sont stockées dans `/var/lib/urpm-dev/` et le daemon utilise le port 9877.

**Noter qu'en mode dev, urpmd n'interagira qu'avec d'autres urpmd en mode dev.**

## Lancer le daemon

```bash
# Lancer le daemon (en root, sans mode arrière-plan)

cd /where/is/urpm-ng

./bin/urpmd --dev

```

## Lancer urpm

```bash
# Lancer urpm (en root dans une console dédiée)

cd /where/is/urpm-ng

./bin/urpm --help

```

## Coder, tester, contribuer…

Les contributions de tous types sont bienvenues : code, tests, traductions, retours d'expérience… aucune contribution n'est trop petite.

Voir `CLAUDE.md` pour les guidelines de développement et `doc/ARCHITECTURE.md` pour l'architecture technique.

---

# Problèmes connus / TODO

- **Performance d'`urpm find`** — La recherche dans files.xml est plus lente qu'urpmf (2.5s vs 0.6s). Nécessite optimisation.

---

# Licence

GPL-3.0 — Voir le fichier LICENSE pour les détails.

# Auteurs

- Maât (Pascal Vilarem)
- Papoteur (Mageia Contributor)
- Claude (Assistant IA)
