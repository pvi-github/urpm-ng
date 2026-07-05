# urpm-ng

Un gestionnaire de paquets moderne pour Mageia Linux, écrit en Python.

urpm-ng est une réécriture complète de la suite urpmi classique, offrant de meilleures performances, une résolution de dépendances plus fine, et des fonctionnalités modernes comme le partage P2P de paquets.

## Prérequis

### Distribution

Pour le moment, il faut Mageia 9 ou Mageia 10.

### Ports pare-feu à ouvrir (pour le partage P2P)

Si tu veux utiliser le partage P2P de paquets entre machines LAN, ouvre ces ports :
- **TCP 9876** (production) ou **TCP 9877** (mode dev) -- API HTTP d'urpmd
- **UDP 9878** (production) ou **UDP 9879** (mode dev) -- Broadcasts de découverte de pairs

Utilise le Centre de Contrôle Mageia (MCC) > Sécurité > Pare-feu, ou édite directement `/etc/shorewall/rules.drakx`.

## Installation

### Paquets

urpm-ng est découpé en plusieurs paquets pour plus de souplesse :

| Paquet | Description |
|--------|-------------|
| `urpm-ng-core` | Minimal : CLI, résolveur, base de données |
| `urpm-ng-daemon` | Daemon en arrière-plan + partage P2P |
| `urpm-ng` | Install standard (core + daemon) |
| `urpm-ng-desktop` | Intégration bureau (Discover, GNOME Software) |
| `urpm-ng-build` | Outils de build en conteneur (image, build) |
| `urpm-ng-all` | Tout |

**Choisir le bon paquet :**
- **Install minimale / conteneur** : `urpm-ng-core`
- **Utilisation CLI standard** : `urpm-ng`
- **Bureau avec logiciels GUI** : `urpm-ng-desktop`
- **Empaqueteurs** : `urpm-ng-build`

### Install ou mise à jour RPM… fonctionne dans tous les cas (one-liner)

Copie-colle et exécute dans un terminal :

```bash
mkdir -p $HOME/tmp/urpm-ng && cd $HOME/tmp/urpm-ng && \
MGAVER=$(rpm -q --qf '%{version}' mageia-release-Default 2>/dev/null | cut -d. -f1) && \
ARCH=$(uname -m) && \
VER=$(curl -s https://api.github.com/repos/pvi-github/urpm-ng/releases | grep -m1 '"tag_name"' | cut -d'"' -f4) && \
echo "Downloading urpm-ng $VER for Mageia $MGAVER ($ARCH)..." && \
curl -s "https://api.github.com/repos/pvi-github/urpm-ng/releases/tags/$VER" | \
  grep browser_download_url | grep '\.rpm"' | cut -d'"' -f4 | \
  grep -v '\.src\.rpm' | grep -v '\-debuginfo' | grep -v '\-debugsource' | \
  grep "mga${MGAVER}" | grep "\.${ARCH}\.\|\.noarch\." | xargs -n1 curl -sLO && \
if urpm --version 2>/dev/null | grep -qE 'urpm (0\.([3-9]|[0-9]{2,})|[1-9][0-9]*)\.'; then \
  su -c "urpm i --reinstall $HOME/tmp/urpm-ng/urpm-ng-all-*.rpm"; \
else \
  su -c "urpmi $HOME/tmp/urpm-ng/*.rpm && urpm mark auto \$(rpm -qa 'urpm-ng-*' | grep -v urpm-ng-all | sed 's/-[0-9].*//')"; \
fi
```

Note : à la première install, urpm-ng importera sa configuration depuis urpmi.

## Configuration

urpm marche tel quel. Les options avancées (blacklist, redlist, kernel-keep) sont documentées plus bas.

Quand il est installé au niveau système (dans `/usr/bin/`), urpm utilise :
- Base de données : `/var/lib/urpm/packages.db`
- Port du daemon : 9876
- Fichier PID : `/run/urpmd.pid`

### Sources de médias

Comment configurer les sources de médias de paquets & serveurs miroirs.

Nota : pour une installation par RPM, ces étapes ne devraient pas être nécessaires.

```bash
# Liste les médias configurés
urpm media list

# S'il n'y en a aucun, tente l'import depuis un urpmi.cfg existant
urpm media import /etc/urpmi/urpmi.cfg

# Ajoute une source de média spécifique au besoin
urpm media add http://mirror.example.com/distrib/10/x86_64/media/core/release
urpm media add http://mirror.example.com/distrib/10/x86_64/media/core/updates
urpm media add http://mirror.example.com/distrib/10/x86_64/media/core/update_testing

# Configure d'autres serveurs
urpm server autoconfig

# Met à jour les métadonnées des médias
urpm media update
```

---

# urpm - Interface en ligne de commande

## Options globales

Ces options s'appliquent à la plupart des commandes et se placent avant la sous-commande :

```bash
-V, --version              # Affiche la version d'urpm
-v, --verbose              # Sortie verbeuse
-q, --quiet                # Sortie silencieuse
--nocolor                  # Désactive la sortie en couleurs
--root DIR                 # Utilise DIR comme racine pour l'install RPM (chroot, config urpm depuis l'hôte)
--urpm-root DIR            # Utilise DIR comme racine pour la config urpm ET l'install RPM
```

Les parents suivants sont hérités par les commandes transactionnelles et de requête (`install`, `upgrade`, `erase`, `download`, `depends`, …) :

```bash
--arch ARCH                # Architecture cible (défaut : système courant)
--debug COMPONENT          # Active la sortie de debug : solver, tsrun, orphans, download, timing, all
--watched PACKAGES         # Noms de paquets séparés par virgules à surveiller pendant la résolution
```

Note : `--arch` (option parente, fixe l'architecture cible de l'opération) est distinct d'`--allow-arch` (option locale sur install/upgrade/download, autorise des architectures additionnelles en plus de l'arch système — typiquement `i686` pour wine/steam sur x86_64).

## Options d'affichage

La plupart des commandes acceptent ces options de sortie :

```bash
--show-all            # Affiche tous les éléments sans troncature
--flat                # Un élément par ligne (parsable par des scripts)
--json                # Sortie JSON (pour usage programmatique)
```

Par défaut, les longues listes sont affichées en colonnes multiples et tronquées à 10 lignes avec "... et N autres". Utilise `--show-all` pour tout voir.

Exemples :
```bash
urpm list installed --flat          # Un paquet par ligne
urpm search firefox --json          # Sortie JSON
urpm i task-plasma --show-all       # Affiche toutes les dépendances
```

## Transactions atomiques vs best-effort

Depuis la 0.7.9, `urpm upgrade` tourne en mode **best-effort** par défaut : les paquets dont les dépendances ne peuvent pas être satisfaites sont retirés de la transaction et rapportés à la fin avec leur raison (dépendance manquante, mismatch de version, cascade SRPM sœur, …). La transaction est validée pour tout le reste. Passe `--atomic` pour basculer en mode strict (recommandé sur les serveurs) : tout paquet non résolvable abandonne toute la transaction.

`urpm install`, au contraire, est **atomique par défaut** : si un paquet demandé ne peut pas être installé, toute la transaction est annulée. Passe `--no-atomic` pour opter pour le mode best-effort sur le chemin d'install.

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

## Bootstrap et chroot

### Initialiser une nouvelle configuration urpm (`urpm init`)

Bootstrap les médias urpm dans une racine fraîche ou un chroot pour construction d'image. Les miroirs sont pris depuis l'API mirroirs Mageia et filtrés par la section `[server]` de `/etc/urpm/conf.d/10-server.cfg`.

```bash
# Bootstrap un rootfs chroot pour Mageia 10
urpm --urpm-root /tmp/rootfs init --release 10 --arch x86_64

# Utilise une liste de miroirs custom
urpm init --mirrorlist 'https://mirrors.mageia.org/api/mageia.10.x86_64.list'

# Options
--release, -r <version>     # Version Mageia cible (10, cauldron, …)
--mirrorlist <url>          # Surcharge l'URL de la liste de miroirs auto-générée
--arch <arch>               # Architecture cible (défaut : hôte)
--auto, -y                  # Mode non-interactif
--no-sync                   # Configure les médias mais saute la sync initiale
```

### Démonter un chroot (`urpm cleanup`)

Après avoir travaillé dans un chroot `--urpm-root`, démonte `/dev` et `/proc` montés par `urpm init` :

```bash
urpm --urpm-root /tmp/rootfs cleanup
```

## Gestion des paquets

### Installer des paquets

```bash
urpm install <paquet>         # Installe un paquet
urpm i <paquet>               # Alias court

# Options
--auto, -y                    # Mode non-interactif
--test                        # Simulation (dry run)
--without-recommends          # Saute les paquets recommandés
--with-suggests               # Installe aussi les paquets suggérés
--force                       # Force malgré les problèmes de dépendances
--reinstall                   # Réinstalle les paquets déjà installés (réparation)
--nosignature                 # Saute la vérification GPG (non recommandé)
--noscripts                   # Saute les scripts pre/post install (builds chroot/conteneur)
--no-peers                    # Désactive le download P2P depuis les pairs LAN
--only-peers                  # Ne télécharge que depuis les pairs LAN, pas les miroirs amont
--no-atomic                   # Mode best-effort (défaut pour install : atomique)
--download-only               # Télécharge dans le cache, n'installe pas
--nodeps                      # Saute la résolution de dépendances (avec --download-only)
--all                         # Installe toutes les familles correspondantes (ex. php8.4 + php8.5)
--install-src                 # Installe le RPM source (extrait spec/sources dans ~/rpmbuild/)
--config-policy {keep,replace,ask}  # Politique de conflit sur fichiers de config (défaut : keep)
--prefer=<prefs>              # Guide les choix d'alternatives (voir plus bas)
--allow-arch <arch>           # Autorise des architectures supplémentaires (ex. i686 pour wine/steam)
--sync                        # Attend l'achèvement complet (triggers post-install)
```

#### Installation guidée par préférences

Quand tu installes des paquets avec alternatives (ex. phpmyadmin qui peut utiliser différentes versions PHP et serveurs web), utilise `--prefer` pour guider les choix :

```bash
# Préfère PHP 8.4 avec Apache et php-fpm, exclut mod_php
urpm i phpmyadmin --prefer=php:8.4,apache,php-fpm,-apache-mod_php

# Préfère nginx au lieu d'apache
urpm i phpmyadmin --prefer=php:8.4,nginx,php-fpm
```

Syntaxe des préférences :
- `capability:version` — Contrainte de version (ex. `php:8.4`)
- `pattern` — Préfère les paquets fournissant cette capacité (ex. `apache`, `php-fpm`)
- `-pattern` — Défavorise les paquets correspondants (ex. `-apache-mod_php`)

Les préférences travaillent sur REQUIRES et PROVIDES des paquets, pas sur les noms.

#### Filtrage par architecture

Par défaut, urpm ne considère que les paquets correspondant à l'architecture de ton système et `noarch`. Cela empêche l'install accidentelle de paquets i686 sur x86_64 quand les médias 32-bit sont activés.

Pour installer des paquets 32-bit (wine, steam, multilib) :

```bash
urpm install wine --allow-arch i686
urpm install steam --allow-arch i686

# Plusieurs architectures
urpm install monpaquet --allow-arch i686 --allow-arch armv7hl
```

### Retirer des paquets

```bash
urpm erase <paquet>           # Retire un paquet
urpm e <paquet>               # Alias court

# Options
--auto, -y                    # Mode non-interactif
--test                        # Simulation (dry run)
--auto-orphans                # Retire aussi les dépendances orphelines (implicite avec -y sauf --keep-orphans)
--keep-orphans                # Ne retire pas les dépendances orphelines
--erase-recommends            # Retire aussi les paquets seulement recommandés (pas requis)
--keep-suggests               # Garde les paquets suggérés par les paquets restants
--force                       # Force malgré les problèmes de dépendances
--debug {solver,tsrun,all}    # Active la sortie de debug pour résolveur/transaction
--sync                        # Attend l'achèvement complet (triggers post-uninstall)
```

### Mettre à jour les métadonnées (façon apt)

```bash
urpm update                   # Met à jour toutes les métadonnées de médias
urpm update "Core Release"    # Met à jour un média spécifique
urpm update --files           # Sync aussi files.xml
```

### Télécharger des paquets (sans installer)

```bash
urpm download <paquet>        # Télécharge un paquet dans le cache
urpm dl <paquet>              # Alias court
urpm download --only-peers pkg  # Ne télécharge que depuis les pairs LAN

# Options
--release, -r <version>       # Release cible pour download cross-release (ex. cauldron)
--buildrequires, --br [SPEC]  # Télécharge les build deps (auto-détecte ou depuis .spec/.src.rpm)
--without-recommends          # Saute les paquets recommandés
--nodeps                      # Télécharge uniquement les paquets listés, sans dépendances
--no-peers / --only-peers     # Comme install (politique pair)
--allow-arch <arch>           # Autorise des architectures supplémentaires
--arch <arch>                 # Hérité : architecture cible
--show-all                    # Affiche la liste complète des paquets résolus
                              # (défaut tronque à 20 avec "... et N autres")
```

### Mettre à jour les paquets

```bash
urpm upgrade                  # Met à jour tous les paquets
urpm u                        # Alias court
urpm upgrade <paquet>         # Met à jour des paquets spécifiques

# Options
--auto, -y                    # Mode non-interactif
--test                        # Simulation (dry run)
--atomic                      # Mode strict : abandonne toute la transaction sur un paquet non résolvable.
                              # Défaut : best-effort (voir "Transactions atomiques vs best-effort" plus haut).
--with-recommends             # Installe les paquets recommandés
--with-suggests               # Installe aussi les paquets suggérés
--noerase-orphans             # Garde les dépendances orphelines (ne les retire pas)
--download-only               # Télécharge dans le cache sans appliquer la mise à jour
--nosignature                 # Saute la vérification GPG (non recommandé)
--no-peers / --only-peers     # Désactive / limite aux pairs LAN
--force                       # Force la mise à jour malgré des problèmes de dépendances
--config-policy {keep,replace,ask}  # Politique de conflit config (défaut : keep)
--allow-arch <arch>           # Autorise des architectures supplémentaires (ex. i686)
--sync                        # Attend l'achèvement complet (triggers post-install)
```

### Auto-retrait des orphelins

```bash
urpm autoremove               # Retire les dépendances inutilisées (défaut : --orphans)
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
urpm search <motif>           # Recherche par nom/résumé
urpm s <motif>                # Alias court
urpm q <motif>                # Alias query (compatibilité urpmq)

# Options
--installed                   # Recherche uniquement dans les paquets installés
--unavailable                 # Liste les paquets installés absents de tout média
```

#### Trouver les paquets indisponibles

Liste les paquets installés mais qui ne sont plus disponibles dans aucun média configuré (comme `urpmq --unavailable`) :

```bash
urpm q --unavailable          # Liste tous les paquets indisponibles
urpm q --unavailable php      # Filtre par motif
```

### Afficher les infos d'un paquet

```bash
urpm show <paquet>            # Affiche les détails d'un paquet
urpm info <paquet>            # Alias
```

### Lister les paquets

```bash
urpm list installed           # Liste les paquets installés
urpm list available           # Liste les paquets disponibles
urpm list updates             # Liste les mises à jour disponibles
urpm list upgradable          # Alias pour updates
```

### Dépendances

```bash
urpm depends <paquet>         # Affiche ce qu'un paquet requiert
urpm rdepends <paquet>        # Affiche ce qui requiert un paquet (deps inverses)
urpm why <paquet>             # Explique pourquoi un paquet est installé

# Options pour depends
--tree                        # Affiche l'arbre de dépendances
--prefer=<prefs>              # Filtre par préférences (même syntaxe qu'install)
--legend                      # Affiche la légende des symboles après l'arbre

# Options pour rdepends
--tree                        # Affiche l'arbre de dépendances inverses
--all                         # Affiche toutes les dépendances inverses récursives (plat)
--depth=N                     # Profondeur max de l'arbre (défaut : 3)
--hide-uninstalled            # Ne montre que les chemins menant à des paquets installés
--legend                      # Affiche la légende des symboles après l'arbre
```

Exemple avec préférences :
```bash
# Affiche les deps de phpmyadmin en préférant PHP 8.4
urpm depends phpmyadmin --prefer=php:8.4
```

Exemple avec rdepends :
```bash
# Affiche l'arbre de deps inverses pour rtkit, profondeur 10, uniquement les chemins installés
urpm rdepends --tree --hide-uninstalled --depth=10 rtkit
```

### Dépendances faibles

```bash
urpm recommends <paquet>      # Affiche les paquets recommandés par un paquet
urpm whatrecommends <paquet>  # Affiche les paquets qui recommandent un paquet
urpm suggests <paquet>        # Affiche les paquets suggérés par un paquet
urpm whatsuggests <paquet>    # Affiche les paquets qui suggèrent un paquet
```

### Requêtes sur les fichiers

```bash
urpm provides <paquet>        # Liste les fichiers fournis par un paquet
urpm whatprovides <fichier>   # Trouve quel paquet fournit un fichier
urpm find <motif>             # Cherche des fichiers dans les paquets (installés + disponibles)
urpm find -i <motif>          # Cherche uniquement dans les paquets installés
urpm find -a <motif>          # Cherche uniquement dans les paquets disponibles
```

Pour chercher dans les paquets disponibles, il faut activer la sync de files.xml :

```bash
urpm media set --all --sync-files  # Active la sync files.xml sur tous les médias
urpm media update --files          # Télécharge files.xml (~500 MB, 10-15 min la 1re fois)
```

Une fois activée, urpmd synchronisera automatiquement files.xml quotidiennement quand le système est inactif.

## Marquage de paquets

```bash
urpm mark manual <paquet>     # Marque comme installé manuellement
urpm mark auto <paquet>       # Marque comme auto-installé (dépendance)
urpm mark show <paquet>       # Affiche la raison d'installation
```

## Blocages de paquets (holds)

Bloque des paquets pour empêcher les mises à jour et remplacement par des obsoletes :

```bash
urpm hold <paquet>            # Bloque un paquet
urpm hold <paquet> -r "raison"  # Bloque avec une raison
urpm hold                     # Liste les paquets bloqués
urpm unhold <paquet>          # Retire le blocage
```

Les paquets bloqués sont protégés contre :
- Les mises à jour de version pendant `urpm upgrade`
- Le remplacement par des paquets qui les obsolètent

Exemple :
```bash
# dhcpcd obsolète dhcp-client, mais tu veux garder dhcp-client
urpm hold dhcp-client -r "Prefer dhcp-client over dhcpcd"

# Maintenant urpm upgrade va sauter dhcp-client et prévenir :
#   Paquets bloqués (1) sautés :
#     dhcp-client (serait obsolété par dhcpcd)

# Pour autoriser le remplacement plus tard :
urpm unhold dhcp-client
```

## Historique et annulation

```bash
urpm history                  # Affiche l'historique des transactions (20 dernières)
urpm history -i               # Filtre : transactions d'install uniquement
urpm history -r               # Filtre : transactions de remove uniquement
urpm history -d <id>          # Affiche les détails de la transaction <id>
urpm history --delete <id>... # Supprime des transactions du log

urpm undo [id]                # Annule une transaction (défaut : la dernière). Enregistre
                              # une entrée propre dans l'historique. Utilise --auto/-y pour
                              # sauter le prompt.

urpm rollback <n>             # Rollback des n dernières transactions
urpm rollback to <id>         # Rollback jusqu'à une transaction précise
urpm rollback to <date>       # Rollback jusqu'à une date (AAAA-MM-JJ ou JJ/MM/AAAA)
```

## Transactions en arrière-plan

Quand une transaction est détachée (ex. via le daemon ou PackageKit), suis sa progression avec :

```bash
urpm progress                 # Affiche la progression courante et sort
urpm progress --watch         # Surveille en continu jusqu'à la fin
```

## Gestion des médias

```bash
urpm media list               # Liste les médias configurés
urpm media add <url>          # Ajoute un média Mageia officiel (auto-parsé)
urpm media add --custom "Nom" nom_court <url>  # Ajoute un média custom / tiers
urpm media remove <nom>...    # Retire un ou plusieurs médias
urpm media remove --all       # Retire TOUS les médias configurés (demande
                              # confirmation ; ajouter -y/--auto la saute).
                              # Les serveurs orphelins (sans média) sont
                              # retirés dans la même passe.
urpm media enable <nom>       # Active un média
urpm media disable <nom>      # Désactive un média
urpm media update [nom]       # Met à jour les métadonnées des médias
urpm media import <fichier>   # Importe depuis urpmi.cfg
urpm media link <nom> +srv -srv  # Lie/délie des serveurs à un média
urpm media set <nom> [opts]   # Modifie les paramètres d'un média (sharing, replication, quota…)
urpm media seed-info <nom>    # Affiche les infos du seed set (sections, nb paquets, taille estimée)
urpm media autoconfig -r 10   # Auto-ajoute les médias Mageia officiels pour la release 10
urpm media discover <url>     # Découvre les médias depuis un media.cfg de repo
```

Flags utiles pour `urpm media add` :

```bash
--import-key                  # Importe la clé GPG annoncée par le média
--allow-unsigned              # Autorise les paquets non signés (médias custom uniquement)
--version <ver>               # Version Mageia cible (médias custom uniquement : 9, 10, cauldron…)
--update                      # Marque comme média de mises à jour
--disabled                    # Ajoute mais laisse désactivé
```

### Découvrir les médias depuis un dépôt

Découvre tous les médias disponibles depuis n'importe quel dépôt compatible Mageia (miroirs officiels, dépôts communautaires comme MLO, miroirs d'entreprise) :

```bash
urpm media discover https://repo.example.org/9/x86_64/media/       # Ajoute tous les médias
urpm media discover --dry-run https://repo.example.org/9/x86_64/media/  # Aperçu uniquement
urpm media discover --sources --debug https://...                   # Inclut SRPMS et debug

# Force-active / force-désactive des catégories (nonfree, tainted, 32bit, all)
urpm media discover --with nonfree,tainted https://...
urpm media discover --without nonfree https://...
urpm media discover --with all https://...
```

La commande récupère `media.cfg` du dépôt, découvre tous les médias, et lie les serveurs existants qui hébergent le même contenu (vérifié par checksum MD5 de `synthesis.hdlist.cz`).

### Liaison serveur-média

Lie ou délie des serveurs à des sources média spécifiques :

```bash
urpm media link "Core Release" +mirror1 +mirror2   # Ajoute des serveurs
urpm media link "Core Updates" -oldserver          # Retire un serveur
urpm media link "Core Release" +all                # Ajoute tous les serveurs disponibles
urpm media link "Core Release" -all +preferred     # Reset et ajoute-en un
```

Note : quand tu ajoutes des serveurs, urpm vérifie que le contenu média correspond en comparant les checksums MD5 de `synthesis.hdlist.cz` avec les serveurs de référence existants.

### Auto-configurer les médias

Ajoute automatiquement les médias Mageia officiels pour une release :

```bash
urpm media autoconfig --release 10              # Ajoute tous les médias officiels pour Mageia 10
urpm media autoconfig -r cauldron               # Ajoute les médias pour Cauldron
urpm media autoconfig -r 10 --no-nonfree        # Saute les médias nonfree
urpm media autoconfig -r 10 --no-tainted        # Saute les médias tainted
urpm media autoconfig -r 10 -n                  # Dry-run : montre ce qui serait ajouté
```

### Paramètres de média

Configure le partage et la réplication des médias :

```bash
urpm media set "Core Release" --shared=yes           # Partage avec les pairs P2P
urpm media set "Core Release" --replication=seed     # Réplication complète (DVD-like)
urpm media set "Core Release" --replication=on_demand  # Cache ce qui est téléchargé
urpm media set "Core Release" --quota=5G             # Limite la taille du cache
urpm media set "Core Release" --retention=30         # Garde les paquets 30 jours
urpm media set "Core Release" --priority=10          # Priorité supérieure
urpm media set "Core Release" --seeds=INSTALL,CAT_PLASMA5  # Sections de seed
urpm media set "Core Release" --sync-files           # Active la sync files.xml pour urpm find
urpm media set --all --sync-files                    # Active sur tous les médias
```

Exemples :
```bash
# Ajoute un média Mageia officiel (serveur et média auto-détectés)
urpm media add https://ftp.belnet.be/mageia/distrib/9/x86_64/media/core/release/

# Ajoute un média tiers custom
urpm media add --custom "RPM Fusion" rpmfusion https://download1.rpmfusion.org/free/fedora/40/x86_64/os/
```

## Gestion des serveurs

Les serveurs sont des sources de miroirs qui peuvent servir plusieurs médias. urpm accepte plusieurs serveurs par média pour l'équilibrage de charge et le failover.

```bash
urpm server list              # Liste les serveurs configurés (avec pays)
urpm server add <nom> <url>   # Ajoute un serveur (teste l'IP et scanne les médias)
urpm server remove <nom> ...  # Retire un ou plusieurs serveurs
urpm server enable <nom>      # Active un serveur
urpm server disable <nom>     # Désactive un serveur
urpm server priority <nom> <n>  # Fixe la priorité du serveur (plus haut = préféré)
urpm server test [nom]        # Teste la connectivité et détecte le mode IP
urpm server ip-mode <nom> <mode>  # Fixe le mode IP (auto/ipv4/ipv6/dual)
urpm server autoconfig        # Auto-ajoute des serveurs depuis l'API mirroirs Mageia
urpm server stats [nom]       # Affiche les statistiques de performance d'un serveur
```

### Liste des serveurs

Options pour urpm server list :
```bash
--all                 # Affiche tous les serveurs y compris les désactivés
```

### Mode IP

Chaque serveur a un mode IP pour gérer la connectivité IPv4/IPv6 :
- `auto` — Laisse le système décider (peut causer un timeout de 30s si IPv6 échoue)
- `ipv4` — Force IPv4 uniquement
- `ipv6` — Force IPv6 uniquement
- `dual` — Les deux marchent, préfère IPv4 (recommandé pour les serveurs dual-stack)

Le mode IP est auto-détecté à l'ajout du serveur. Utilise `server test` pour re-détecter ou `server ip-mode` pour fixer manuellement.

### Suivi de bande passante et failover automatique

urpm suit automatiquement la performance de download de chaque serveur. Après chaque download ou sync de métadonnées, la vitesse mesurée est enregistrée avec une EWMA (Exponentially Weighted Moving Average, α=0.3), donnant une inertie de façon qu'un unique transfert lent ne pénalise pas injustement un bon serveur.

Les serveurs sont essayés dans l'ordre `priority DESC, bandwidth_kbps DESC` : si un serveur échoue pendant un download ou une sync de métadonnées, le suivant meilleur est essayé automatiquement sans intervention utilisateur. Dans une même session, des estimations de vitesse par serveur sont aussi gardées en mémoire, l'ordre s'adapte en temps réel sans attendre le prochain run.

`urpm server autoconfig` mesure la latence vers tous les candidats miroirs et persiste les résultats, donc l'ordre des serveurs est pertinent dès le tout premier download.

### Filtrage géographique

Les serveurs découverts depuis l'API mirroirs Mageia portent des méta-données de pays et continent. La section de configuration `[server]` (voir plus bas) te permet de restreindre les miroirs acceptés :

```ini
# /etc/urpm/conf.d/10-server.cfg
[server]
country_blacklist = UA, RU        # Exclut des pays spécifiques
continent_whitelist = EU          # Uniquement les miroirs européens
```

Le filtrage est appliqué à l'ajout de miroirs (`urpm init`, `urpm media autoconfig`, `urpm server autoconfig`, et expansion du pool en arrière-plan). Les serveurs déjà en base sont complétés avec leur pays au premier run ; ceux qui échouent le filtre sont désactivés automatiquement.

Positionne `auto_add = false` pour empêcher tout ajout automatique de miroir.

Utilise `urpm server stats [nom]` pour inspecter les métriques collectées :

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
urpm peer list                # Liste les pairs découverts
urpm peer downloads [host]    # Affiche les paquets téléchargés depuis les pairs (filtre par host)
urpm peer blacklist <host>    # Bloque un pair (ex. s'il fournit de mauvais paquets)
urpm peer unblacklist <host>  # Débloque un pair
urpm peer clean <host>        # Supprime les RPMs téléchargés depuis un pair spécifique
                              # (à utiliser après blacklistage ; <host> obligatoire)
```

### Mode local uniquement

Utilise `--only-peers` pour télécharger exclusivement depuis les pairs LAN sans fallback vers les miroirs amont :

```bash
urpm i --only-peers firefox   # Installe uniquement si disponible depuis les pairs
urpm u --only-peers           # Met à jour uniquement avec les paquets des pairs
urpm download --only-peers pkg  # Télécharge uniquement depuis les pairs
```

Utile pour les réseaux air-gapped ou quand tu veux garantir que tous les paquets viennent de sources locales de confiance.

## Gestion du cache

```bash
urpm cache info               # Affiche les infos de cache
urpm cache clean              # Retire les RPMs orphelins du cache
urpm cache rebuild            # Reconstruit la base de paquets depuis les fichiers synthesis
urpm cache rebuild-fts        # Reconstruit l'index FTS pour la recherche rapide de fichiers
urpm cache stats              # Statistiques détaillées
```

`urpm cache clean` accepte `--dry-run/-n` (aperçu), `--auto/-y` (sans confirmation) et `--verbose/-v` (liste chaque fichier orphelin).

## Mirroir local de paquets

Au-delà de la politique `--replication` par média décrite plus bas, la commande de premier niveau `urpm mirror` expose l'état miroir côté daemon (quotas, versions servies, rate limit) et permet de déclencher explicitement les tâches de maintenance.

```bash
urpm mirror status            # Affiche l'état du miroir, quotas et versions servies
urpm mirror enable            # Commence à servir les paquets en cache aux pairs
urpm mirror disable           # Arrête de servir les paquets
urpm mirror quota [SIZE]      # Affiche ou fixe le quota global du cache (ex. 10G, 500M)
urpm mirror enable-version 10,cauldron   # Reprend le service pour ces versions
urpm mirror disable-version 8,9          # Arrête le service pour ces versions
urpm mirror clean [-n]        # Force quotas et politiques de rétention (--dry-run aperçu)
urpm mirror sync [média]      # Force une sync de réplication pour les médias en politique `seed`
urpm mirror sync --latest-only           # Sync plus petite, DVD-like
urpm mirror rate-limit [on|off|N/min]    # Configure la limite de débit sortant
```

## Miroir / Réplication

urpm-ng peut répliquer localement un sous-ensemble de paquets, similaire à un jeu d'install DVD. Utile pour les install parties ou les installations hors-ligne.

### Réplication basée sur seed

La réplication utilise le fichier `rpmsrate-raw` de Mageia pour déterminer quels paquets mirrorer (même logique que le contenu DVD).

```bash
# Active la réplication seed-based sur un média
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

# Force la sync (télécharge les paquets manquants)
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
urpm config blacklist list    # Affiche les paquets blacklistés
urpm config blacklist add <pkg>
urpm config blacklist remove <pkg>
```

### Redlist (prévenir avant auto-remove)

```bash
urpm config redlist list      # Affiche les paquets redlistés
urpm config redlist add <pkg>
urpm config redlist remove <pkg>
```

### Gestion du kernel

```bash
urpm config kernel-keep       # Affiche combien de kernels garder
urpm config kernel-keep <n>   # Fixe le nombre de kernels à garder
```

### Mode de version (système vs cauldron)

Quand système et cauldron sont tous deux configurés, `version-mode` choisit qui gagne pour les mises à jour :

```bash
urpm config version-mode              # Affiche le mode courant
urpm config version-mode system       # Reste sur la version système installée
urpm config version-mode cauldron     # Roule avec cauldron
urpm config version-mode auto         # Retire la préférence explicite
```

### Hooks d'auto-upgrade pour les software centers

Contrôle si GNOME Software, KDE Discover ou le chemin d'update offline de PackageKit peuvent installer des mises à jour de leur propre initiative :

```bash
urpm config gnome-auto-upgrades [yes|no]      # GNOME Software
urpm config discover-auto-upgrades [yes|no]   # KDE Discover
urpm config packagekit-auto-upgrades [yes|no] # Updates offline PackageKit
```

Sans argument, chaque sous-commande affiche le réglage courant. Ces hooks toggle les réglages dconf/PolicyKit côté bureau ; la politique système est appliquée séparément par le paquet `urpm-ng-desktop`.

### Inspecter ou éditer la configuration

```bash
urpm config show              # Affiche la config effective depuis tous les *.cfg
urpm config edit              # Ouvre urpm.cfg dans $EDITOR
urpm config edit 00-urpmi-compat   # Ouvre un drop-in spécifique
```

### Sélection de serveur

La section `[server]` dans `/etc/urpm/conf.d/10-server.cfg` contrôle la sélection automatique de miroir :

| Clé | Défaut | Description |
|-----|--------|-------------|
| `auto_add` | `true` | Autorise l'ajout automatique de miroirs |
| `country_blacklist` | *(vide)* | Codes ISO 3166 séparés par virgule à exclure (ex. `UA, RU`) |
| `country_whitelist` | *(vide)* | N'accepte que ces pays (l'emporte sur blacklist) |
| `continent_blacklist` | *(vide)* | Codes continent à exclure (`EU`, `NA`, `SA`, `AS`, `AF`, `OC`) |
| `continent_whitelist` | *(vide)* | N'accepte que ces continents (l'emporte sur blacklist) |

Un miroir doit passer **les deux** filtres continent et pays. Whitelist gagne sur blacklist à chaque niveau. Utilise `urpm config show` pour voir les réglages effectifs.

## Clés GPG

```bash
urpm key list                 # Liste les clés GPG installées
urpm key import <fichier|url> # Importe une clé GPG
urpm key remove <keyid>       # Retire une clé GPG
```

## Dépendances de build

Installe les dépendances de build pour la construction RPM :

```bash
urpm install --buildrequires foo.spec    # Depuis un fichier spec
urpm install --buildrequires foo.src.rpm # Depuis un RPM source
urpm i -b                                # Auto-détecte dans l'arbre de build RPM
urpm i --br                              # Alias court

# Options
--sync                        # Attend que tous les scriptlets se terminent
```

Les dépendances de build installées sont trackées dans `/var/lib/rpm/installed-through-builddeps.list` et exclues du retrait d'orphelins normal. Pour les nettoyer :

```bash
urpm autoremove --buildrequires          # Retire toutes les build deps trackées
urpm ar -b                               # Forme courte
```

## Système de build en conteneur

urpm fournit un système de build complet en conteneur pour les paquets RPM via Docker ou Podman.

### Gestion d'images

```bash
# Liste les images de build disponibles
urpm image list

# Met à jour une image existante (re-sync médias + paquets)
urpm image update mageia:10-build

# Supprime une ou plusieurs images
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
--buildrequires <spec|srpm>   # Installe les BuildRequires depuis un .spec ou .src.rpm
--runtime docker|podman       # Runtime de conteneur (défaut : auto-détection)
--keep-chroot                 # Garde le chroot temporaire après création de l'image
-w, --workdir <path>          # Répertoire de travail pour le chroot (défaut : /tmp)
```

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

Par défaut, `urpm build` auto-met-à-jour médias et paquets dans le conteneur avant de builder, pour que les builds tournent toujours contre le dernier état du dépôt. Utilise `--no-update` pour sauter cette étape en offline ou pour accélérer des builds répétés.

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

# Surcharge packager/vendor/dist sans toucher au spec
urpm build -i mageia:10-build --rpmmacros ./my-macros SPECS/foo.spec

# Options
-i, --image <tag>             # Image Docker/Podman à utiliser
-o, --output <dir>            # Répertoire de sortie pour les builds SRPM (défaut : ./build-output)
-w, --with-rpms <pattern>     # Pré-installe des RPMs locaux avant le build (glob, répétable)
--no-update                   # Saute l'auto-update des médias et paquets avant le build
--runtime docker|podman       # Runtime de conteneur (défaut : auto-détection)
-j, --parallel <N>            # Nombre de builds en parallèle (défaut : 1)
--keep-container              # Garde le conteneur après le build (pour debug)
--subrel <tag>                # Injecte %subrel TAG pour que les RPMs de sortie deviennent NAME-VERSION-RELEASE.TAG.DIST.ARCH.rpm
--rpmmacros <file>            # Injecte FILE comme /root/.rpmmacros dans le conteneur de build (combinable avec --subrel)
```

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
# 1. Crée l'image de build (une fois)
urpm image make --release 10 --tag mga:10-build

# 2. Build un paquet
urpm build --image mga:10-build ./mypackage.src.rpm

# 3. Plus tard, met à jour l'image pour récupérer les nouveaux paquets du dépôt
urpm image update mga:10-build

# 4. Vérifie les résultats
ls ./build-output/
```

## Métadonnées AppStream

urpm peut produire et rafraîchir les catalogues AppStream consommés par KDE Discover et GNOME Software :

```bash
urpm appstream generate              # Génère le catalogue depuis la base de paquets
urpm appstream generate -m core/release    # Limite à un média spécifique
urpm appstream generate --no-compress       # XML brut au lieu de gzip
urpm appstream status                # Affiche le statut du catalogue par média
urpm appstream merge                 # Fusionne les fichiers par média dans le catalogue unifié
urpm appstream merge --refresh       # Rafraîchit aussi le cache AppStream système
urpm appstream init-distro           # Crée le fichier metainfo de l'OS (nécessaire pour Discover/GS)
urpm appstream init-distro --force   # Écrase un metainfo existant
```

## Génération de médias (urpm genmedia)

`urpm genmedia` est le pendant côté serveur d'`urpm appstream` : là où `appstream` consomme des catalogues pour peupler les bases clients, `genmedia` **produit** l'ensemble complet des métadonnées média qu'un miroir Mageia sert à ses clients. C'est une réécriture Python du historique `genhdlist3`, intégrée dans urpm-ng et empaquetée séparément comme `urpm-ng-genmedia` pour que l'empreinte des dépendances reste hors de l'install client de base.

À partir d'un répertoire de fichiers RPM :

```bash
urpm genmedia /path/to/rpms          # Défaut : génération complète
urpm genmedia /path/to/rpms --incremental   # Saute les RPMs dont le SHA-256 n'a pas changé
urpm genmedia /path/to/rpms --no-hdlist     # Saute la sortie hdlist.cz
urpm genmedia /path/to/rpms --xml-info      # Force la régénération des fichiers XML info
urpm genmedia /path/to/rpms --appstream-info  # Génère le catalogue AppStream
urpm genmedia /path/to/rpms --no-md5sum     # Saute MD5SUM (plus rapide pour les tests)
urpm genmedia /path/to/rpms --allow-empty-media  # Tolère un répertoire d'entrée vide
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
urpm readme --list                   # Liste les transactions ayant des messages README
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

Ou installe directement le backend :
```bash
urpm install urpm-ng-packagekit-backend
```

Ceci installe :
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
# Vérifie si le service D-Bus tourne
systemctl status urpm-dbus.service

# Vérifie le backend PackageKit
pkcon backend-details

# Redémarre les services après update
systemctl restart packagekit.service
systemctl restart urpm-dbus.service

# Vérifie l'interface D-Bus
gdbus introspect --system --dest org.mageia.Urpm.v1 \
  --object-path /org/mageia/Urpm/v1
```

---

# Développement & contribution

## Prérequis

### Ports pare-feu

Voir la section Prérequis pour les ports réseau à ouvrir pour le partage P2P.

### Mettre en place ton environnement

Clone le dépôt :

```bash
git clone https://github.com/pvi-github/urpm-ng.git
cd urpm-ng

```


### Configuration du mode dev

Crée un fichier `.urpm.local` à la racine du projet pour personnaliser le mode dev :

```bash
cd /where/is/urpm-ng

# Mode dev (port 9877, données utilisateur dans ~/var/lib/urpm-dev/)
# Bascule vers le mode dev
touch .urpm.local
```

Nota, tu peux changer où urpm & urpmd mettent leurs données en éditant le fichier .urpm.local :
```ini
# Répertoire de base custom (optionnel)
base_dir=/path/lib/urpm-dev
```

En mode dev, par défaut, les données sont stockées dans `/var/lib/urpm-dev/` et le daemon utilise le port 9877.

**Note qu'en mode dev, urpmd n'interagira qu'avec d'autres urpmd en mode dev.**

## Lancer le daemon

```bash
# Lance le daemon (en root, sans mode arrière-plan)

cd /where/is/urpm-ng

./bin/urpmd --dev

```

## Lancer urpm

```bash
# Lance urpm (en root dans une console dédiée)

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
