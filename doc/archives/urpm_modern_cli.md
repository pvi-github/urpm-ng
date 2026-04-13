# urpm : Interface CLI moderne - Spécification complète

## 🎯 Philosophie

Une interface unifiée, moderne et intuitive inspirée de `git`, `cargo`, `npm` :
- **Un seul binaire** : `urpm`
- **Commandes cohérentes** : `urpm <verb> <noun>`
- **Alias courts** : `urpm i` = `urpm install`
- **Aide contextuelle** : `urpm help <command>`

## 📦 Commandes principales

### INSTALLATION

```bash
# Installation de packages
urpm install <package>...
urpm i <package>...                    # Alias court

# Options
urpm install firefox --auto            # Pas de confirmation
urpm install firefox --test            # Simulation
urpm install firefox --no-recommends   # Sans recommandations
urpm install firefox --force           # Forcer

# Exemples
urpm i firefox
urpm i firefox thunderbird libreoffice
urpm i kernel-desktop-latest --auto
urpm i --test gimp                     # Voir ce qui serait installé

# Réinstallation
urpm reinstall <package>
urpm ri <package>

# Exemples
urpm ri firefox                        # Réparer une installation
```

### SUPPRESSION

```bash
# Suppression de packages
urpm remove <package>...
urpm r <package>...                    # Alias court

# Options
urpm remove firefox --auto             # Pas de confirmation
urpm remove firefox --test             # Simulation
urpm remove firefox --keep-deps        # Garder les dépendances

# Nettoyage automatique
urpm autoremove                        # Supprimer orphelins
urpm ar                                # Alias court

# Exemples
urpm r firefox
urpm r --test gimp                     # Voir ce qui serait supprimé
urpm ar                                # Nettoyer les orphelins
```

### RECHERCHE & INFORMATION

```bash
# Recherche de packages
urpm search <pattern>
urpm s <pattern>                       # Alias court

# Options
urpm search --name firefox             # Seulement dans les noms
urpm search --description java         # Dans les descriptions
urpm search --installed vim            # Seulement installés
urpm search --available python         # Seulement disponibles

# Exemples
urpm s firefox
urpm s "text editor"
urpm s --installed                     # Tous les packages installés

# Information détaillée
urpm show <package>
urpm sh <package>                      # Alias court
urpm info <package>                    # Alias de show

# Exemples
urpm show firefox
urpm show firefox --files              # Avec liste des fichiers
urpm show firefox --changelog          # Avec changelog

# Lister des packages
urpm list [filter]
urpm l [filter]                        # Alias court

# Filtres disponibles
urpm list updates                      # Mises à jour disponibles
urpm list installed                    # Packages installés
urpm list available                    # Packages disponibles
urpm list upgradable                   # Alias de updates
urpm list all                          # Tous les packages

# Exemples
urpm l updates
urpm l installed | grep kernel
urpm l available --group Games
```

### REQUÊTES AVANCÉES

```bash
# Qui fournit un fichier ?
urpm provides <file|capability>
urpm p <file|capability>               # Alias court

# Exemples
urpm p /usr/bin/gcc
urpm p libgtk-3.so.0
urpm provides "webclient"

# Dépendances d'un package
urpm depends <package>
urpm d <package>                       # Alias court

# Options
urpm depends --recursive firefox       # Récursif
urpm depends --tree firefox            # Affichage en arbre

# Exemples
urpm d firefox
urpm d --tree firefox

# Dépendances inverses (qui dépend de ce package)
urpm rdepends <package>
urpm rd <package>                      # Alias court

# Exemples
urpm rd libgtk-3
urpm rd --recursive python3

# Recherche dans les fichiers
urpm files <package>                   # Fichiers d'un package
urpm f <package>                       # Alias court

urpm find <pattern>                    # Trouver quel package contient un fichier
urpm fn <pattern>                      # Alias court

# Exemples
urpm f firefox                         # Fichiers de firefox
urpm fn /usr/bin/gcc                   # Quel package contient gcc
```

### MISE À JOUR

```bash
# Mise à jour des métadonnées
urpm update --lists
urpm u --lists                         # Alias court
urpm refresh                           # Alias alternatif

# Mise à jour d'un package
urpm update <package>...
urpm u <package>...

# Mise à jour de tous les packages
urpm update --all
urpm u -a                              # Alias court
urpm upgrade                           # Alias alternatif

# Options
urpm update --auto                     # Pas de confirmation
urpm update --test                     # Simulation
urpm update --security                 # Seulement mises à jour de sécurité

# Exemples
urpm u --lists                         # Rafraîchir les métadonnées
urpm u firefox                         # Mettre à jour firefox
urpm u --all                           # Tout mettre à jour
urpm u -a --test                       # Voir ce qui serait mis à jour
```

### GESTION DES MÉDIAS

```bash
# Lister les médias
urpm media list
urpm m l                               # Alias court
urpm m ls                              # Alias alternatif

# Affichage
  [✓] main              http://mirror.example.com/main
  [✓] updates           http://mirror.example.com/updates  [update]
  [ ] contrib           http://mirror.example.com/contrib  [disabled]

# Ajouter un média
urpm media add <name> <url>
urpm m a <name> <url>

# Options
urpm m add updates http://... --update         # Média de mise à jour
urpm m add contrib http://... --disabled       # Ajouté mais désactivé
urpm m add --distrib http://...                # Distribution complète

# Exemples
urpm m a contrib http://mirror.example.com/contrib
urpm m a --distrib http://mirror.example.com/

# Supprimer un média
urpm media remove <name>
urpm m r <name>

# Exemples
urpm m r contrib

# Activer/désactiver un média
urpm media enable <name>
urpm m e <name>

urpm media disable <name>
urpm m d <name>

# Exemples
urpm m e contrib
urpm m d contrib

# Mettre à jour les métadonnées d'un média
urpm media update [<name>]
urpm m u [<name>]

# Exemples
urpm m u                               # Tous les médias
urpm m u main                          # Seulement main
```

### CACHE & MAINTENANCE

```bash
# Information sur le cache
urpm cache info
urpm c info

# Affichage
  Cache: ~/.cache/urpm/packages.db
  Size: 87.3 MB
  Packages: 3142
  Provides: 15234
  Requires: 42156
  Last update: 2024-12-04 10:30:00

# Nettoyer le cache
urpm cache clean
urpm c clean

# Options
urpm c clean --packages                # Nettoyer RPMs téléchargés
urpm c clean --metadata                # Nettoyer métadonnées obsolètes
urpm c clean --all                     # Tout nettoyer

# Reconstruire le cache
urpm cache rebuild
urpm c rebuild

# Vérifier le cache
urpm cache verify
urpm c verify

# Optimiser le cache (VACUUM SQLite)
urpm cache optimize
urpm c optimize

# Statistiques du cache
urpm cache stats
urpm c stats

# Affichage
  Cache statistics:
    Total packages: 3142
    Installed: 1543 (49%)
    Available updates: 23
    Disk usage: 87.3 MB
    Cache hits: 98.7%
    Average query time: 12ms
```

### HISTORIQUE

```bash
# Lister les transactions
urpm history list
urpm h l

# Affichage
  ID   Date                 Action      Packages
  ───────────────────────────────────────────────────────
  142  2024-12-04 10:30:00  install     firefox (+2 deps)
  141  2024-12-03 15:20:00  update      kernel (+3 deps)
  140  2024-12-02 09:15:00  remove      gimp
  139  2024-12-01 14:45:00  install     libreoffice (+15 deps)

# Options
urpm h l --limit 50                    # Dernières 50 transactions
urpm h l --since "2024-12-01"          # Depuis une date
urpm h l --package firefox             # Transactions concernant firefox

# Info détaillée d'une transaction
urpm history info <id>
urpm h i <id>

# Affichage
  Transaction ID: 142
  Date: 2024-12-04 10:30:00
  Command: urpm install firefox
  User: john
  Return code: 0
  
  Installed:
    - firefox-120.0-1.mga9.x86_64 (156.8 MB)
    - mozilla-nss-3.95-1.mga9.x86_64 (42.3 MB)
    - nspr-4.35-1.mga9.x86_64 (0.3 MB)
  
  Total: 199.4 MB

# Annuler une transaction
urpm history undo <id>
urpm h u <id>

# Exemples
urpm h u 142                           # Annuler l'installation de firefox

# Refaire une transaction annulée
urpm history redo <id>
urpm h redo <id>

# Revenir à un état antérieur
urpm history rollback <id>
urpm h rollback <id>

# Exemples
urpm h rollback 140                    # Revenir à l'état de la transaction 140
```

### VÉRIFICATION

```bash
# Vérifier l'intégrité du système
urpm verify [<package>]
urpm v [<package>]

# Options
urpm verify --all                      # Tous les packages
urpm verify --dependencies             # Seulement les dépendances
urpm verify --files                    # Seulement les fichiers
urpm verify --checksums                # Vérifier les checksums

# Exemples
urpm v firefox                         # Vérifier firefox
urpm v --all                           # Vérifier tout
urpm v --dependencies                  # Trouver dépendances cassées

# Affichage
  Verifying system integrity...
  
  ✓ 3120 packages OK
  
  ✗ 2 packages with issues:
    broken-package-1.0
      Missing dependency: libfoo.so.1
    
    old-package-2.0
      Missing dependency: python2 (obsolete)
  
  ⚠ 5 packages with modified files:
    apache2
      /etc/httpd/conf/httpd.conf (modified)

# Diagnostic complet du système
urpm doctor
urpm doc

# Vérifie et propose des solutions pour :
# - Dépendances cassées
# - Packages orphelins
# - Fichiers modifiés
# - Configuration invalide
# - Cache corrompu

# Affichage
  Running system diagnostics...
  
  ✓ Cache: OK
  ✓ Dependencies: OK
  ✗ Found 3 orphaned packages
  ⚠ 2 packages have available updates
  
  Suggestions:
    1. Run 'urpm autoremove' to clean orphans
    2. Run 'urpm update --all' to update packages
```

### CONFIGURATION

```bash
# Lister la configuration
urpm config list
urpm cfg l

# Affichage
  cache.enabled = true
  cache.path = ~/.cache/urpm
  delta.enabled = true
  delta.max-chain-length = 10
  auto-select = false
  verify-rpm = true

# Obtenir une valeur
urpm config get <key>
urpm cfg g <key>

# Exemples
urpm cfg g cache.enabled

# Définir une valeur
urpm config set <key=value>
urpm cfg s <key=value>

# Exemples
urpm cfg s auto-select=true
urpm cfg s delta.enabled=false

# Réinitialiser la configuration
urpm config reset
urpm cfg reset

# Éditer la configuration
urpm config edit
urpm cfg edit                          # Ouvre dans $EDITOR
```

### DELTAS

```bash
# Statut du système de deltas
urpm delta status
urpm delta st

# Affichage
  Delta support: enabled
  Current version: 142
  Available on mirrors: main, updates
  
  Statistics (last 30 days):
    Downloads saved: 1.2 GB (91%)
    Average delta size: 2.3 MB
    Full download avoided: 47 times

# Activer/désactiver les deltas
urpm delta enable
urpm delta disable

# Statistiques détaillées
urpm delta stats [<media>]

# Affichage
  Media: main
    Current version: 142
    Last sync: 2024-12-04 10:30:00
    Sync method: incremental (5 deltas)
    
    This update:
      Downloaded: 2.3 MB (deltas)
      Would have downloaded: 45.8 MB (full)
      Savings: 95%
    
    Last 30 days:
      Total downloads: 23.4 MB
      Would have been: 234.5 MB
      Total savings: 90%
```

## 🎨 Système d'aide intégré

```bash
# Aide générale
urpm help
urpm --help
urpm -h

# Aide sur une commande
urpm help install
urpm install --help
urpm i -h

# Exemples d'usage
urpm examples
urpm examples install
urpm examples search

# Version
urpm --version
urpm -V

# Affichage
  urpm 2.0.0
  Cache backend: SQLite 3.42.0
  Resolver: Rust 1.75.0
  Python: 3.11.6
```

## 🎯 Options globales

```bash
# Ces options fonctionnent avec toutes les commandes

--verbose, -v        Mode verbeux
--quiet, -q          Mode silencieux
--yes, -y            Répondre oui à toutes les questions (--auto)
--no, -n             Répondre non à toutes les questions
--test               Mode simulation
--root <path>        Utiliser un autre root
--config <file>      Utiliser un autre fichier de config
--no-color           Désactiver les couleurs
--json               Sortie au format JSON
--help, -h           Afficher l'aide
--version, -V        Afficher la version

# Exemples
urpm install firefox -y                # Installation sans confirmation
urpm install firefox --test            # Voir ce qui serait fait
urpm search python --json              # Sortie JSON pour scripts
urpm install firefox -v                # Mode verbeux
```

## 📊 Formats de sortie

### Format par défaut (humain)

```
$ urpm install firefox

Resolving dependencies... ✓ done (0.08s)

The following packages will be installed:
  firefox              120.0-1.mga9    156.8 MB
  mozilla-nss          3.95-1.mga9      42.3 MB
  nspr                 4.35-1.mga9       0.3 MB

Total download size: 199.4 MB
Total installed size: 534.2 MB

Proceed with installation? [Y/n]
```

### Format JSON (scripts)

```bash
$ urpm install firefox --json

{
  "command": "install",
  "status": "success",
  "transaction": {
    "id": 142,
    "timestamp": "2024-12-04T10:30:00Z",
    "packages": [
      {
        "name": "firefox",
        "version": "120.0",
        "release": "1.mga9",
        "arch": "x86_64",
        "action": "install",
        "size": 164532224,
        "download_size": 156834567
      }
    ],
    "dependencies": [
      {
        "name": "mozilla-nss",
        "version": "3.95",
        "reason": "required by firefox"
      }
    ]
  },
  "summary": {
    "packages_installed": 3,
    "download_size": 199423456,
    "installed_size": 534234567,
    "duration": 12.34
  }
}
```

### Format verbeux

```bash
$ urpm install firefox -v

[2024-12-04 10:30:00] INFO: Loading cache from ~/.cache/urpm/packages.db
[2024-12-04 10:30:00] DEBUG: Cache contains 3142 packages
[2024-12-04 10:30:00] INFO: Resolving dependencies for firefox
[2024-12-04 10:30:00] DEBUG: Checking: firefox → mozilla-nss >= 3.90
[2024-12-04 10:30:00] DEBUG: Found: mozilla-nss-3.95-1.mga9.x86_64
[2024-12-04 10:30:00] DEBUG: Checking: firefox → nspr >= 4.30
[2024-12-04 10:30:00] DEBUG: Found: nspr-4.35-1.mga9.x86_64
[2024-12-04 10:30:00] INFO: Resolution completed in 0.082s
[2024-12-04 10:30:01] INFO: Downloading packages...
[2024-12-04 10:30:01] DEBUG: Using mirror: http://mirror1.example.com
[2024-12-04 10:30:01] DEBUG: Parallel downloads: 4 connections
...
```

## 🔗 Compatibilité rétrocompatible

### Wrappers pour compatibilité

```bash
# Les anciens binaires restent disponibles via wrappers

urpmi <package>       →  urpm install <package>
urpme <package>       →  urpm remove <package>
urpmq <package>       →  urpm search <package>
urpmf <pattern>       →  urpm find <pattern>
urpmi.update          →  urpm update --lists
urpmi.addmedia        →  urpm media add
urpmi.removemedia     →  urpm media remove

# Toutes les options des anciennes commandes sont supportées
urpmi --auto firefox              # Fonctionne
urpm install --auto firefox       # Équivalent moderne
```

### Migration en douceur

```bash
# Détection automatique de l'usage ancien
$ urpmi firefox
⚠ Note: 'urpmi' is deprecated, use 'urpm install' instead
  Tip: Create an alias: alias urpmi='urpm install'

Installing firefox...
```

## 🎨 Interface interactive

### Sélection multiple

```bash
$ urpm search java

Found 15 packages matching 'java':
  [1] java-11-openjdk        OpenJDK 11
  [2] java-17-openjdk        OpenJDK 17 (recommended)
  [3] java-1.8.0-openjdk     Legacy OpenJDK 8
  [4] java-21-openjdk        OpenJDK 21 (latest)
  ...

Install which one? [1-15, q to quit]: 2

Installing java-17-openjdk...
```

### Résolution de conflits

```bash
$ urpm install package-a

⚠ Conflict detected:
  package-a requires libfoo >= 2.0
  package-b (installed) requires libfoo < 2.0

Options:
  [1] Remove package-b and install package-a
  [2] Cancel installation
  [3] Show more details

Choice [1-3]: _
```

### Barres de progression

```bash
Downloading packages...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100% 
  ✓ firefox-120.0-1.mga9.x86_64        156.8 MB  [12.3 MB/s]
  ✓ mozilla-nss-3.95-1.mga9.x86_64     42.3 MB   [15.1 MB/s]
  ✓ nspr-4.35-1.mga9.x86_64            0.3 MB    [2.1 MB/s]

Installing packages...
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━ 100%
  ✓ Preparing...
  ✓ Installing firefox-120.0-1.mga9.x86_64
  ✓ Installing mozilla-nss-3.95-1.mga9.x86_64
  ✓ Installing nspr-4.35-1.mga9.x86_64

✓ Transaction completed successfully in 12.3s
```

## 📚 Structure du code

```
urpm/
├── cli/
│   ├── __init__.py
│   ├── main.py              # Point d'entrée principal
│   ├── commands/
│   │   ├── __init__.py
│   │   ├── install.py       # urpm install
│   │   ├── remove.py        # urpm remove
│   │   ├── search.py        # urpm search
│   │   ├── update.py        # urpm update
│   │   ├── media.py         # urpm media
│   │   ├── cache.py         # urpm cache
│   │   ├── history.py       # urpm history
│   │   └── ...
│   ├── ui/
│   │   ├── progress.py      # Barres de progression
│   │   ├── prompt.py        # Prompts interactifs
│   │   ├── table.py         # Affichage tableaux
│   │   └── color.py         # Gestion des couleurs
│   └── compat/
│       ├── urpmi.py         # Wrapper urpmi
│       ├── urpme.py         # Wrapper urpme
│       └── urpmq.py         # Wrapper urpmq
├── core/
│   ├── cache.py
│   ├── resolver.py
│   ├── transaction.py
│   └── ...
└── ...
```

## ✅ TODO pour implémentation

### Phase 1 : Commandes de base
- [ ] `urpm install`
- [ ] `urpm remove`
- [ ] `urpm search`
- [ ] `urpm show`
- [ ] `urpm list`

### Phase 2 : Mise à jour
- [ ] `urpm update`
- [ ] `urpm upgrade`

### Phase 3 : Médias
- [ ] `urpm media list`
- [ ] `urpm media add`
- [ ] `urpm media remove`
- [ ] `urpm media update`

### Phase 4 : Requêtes avancées
- [ ] `urpm provides`
- [ ] `urpm depends`
- [ ] `urpm rdepends`
- [ ] `urpm files`
- [ ] `urpm find`

### Phase 5 : Maintenance
- [ ] `urpm cache`
- [ ] `urpm history`
- [ ] `urpm verify`
- [ ] `urpm doctor`
- [ ] `urpm config`

### Phase 6 : Wrappers compatibilité
- [ ] `urpmi` → `urpm install`
- [ ] `urpme` → `urpm remove`
- [ ] `urpmq` → `urpm search`
- [ ] `urpmf` → `urpm find`
- [ ] `urpmi.update` → `urpm media update`

---

**Note** : Cette interface est inspirée des meilleurs outils modernes (git, cargo, npm, apt) tout en restant fidèle à la philosophie urpmi.