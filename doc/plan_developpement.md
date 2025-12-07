# Plan de développement urpm-ng

## Mapping des commandes et alias

| Legacy | Nouvelle commande | Alias court | Delta |
|--------|------------------|-------------|-------|
| `urpmi` | `urpm install` | `urpm i` | +1 espace |
| `urpme` | `urpm remove` | `urpm r` | +1 espace |
| `urpmq` | `urpm search` | `urpm s` | différent |
| `urpmq` | `urpm query` | `urpm q` | +1 espace |
| `urpmf` | `urpm find` | `urpm f` | +1 espace |
| `urpmi.addmedia` | `urpm media add` | `urpm m a` | - |
| `urpmi.removemedia` | `urpm media remove` | `urpm m r` | - |
| `urpmi.update` | `urpm update --lists` | `urpm u -l` | - |

---

## Phase 1 : Restructuration et fondations

**Objectif** : Code propre, modulaire, prêt pour la suite

- [ ] Réorganiser en package Python (`urpm/`)
- [ ] Intégrer `parse_hdlist.py` dans core
- [ ] Module `compression.py` unifié (zstd/gzip/xz/bz2)
- [ ] Schéma SQLite étendu (tables `media`, `config`, `history`)
- [ ] CLI avec système d'alias dès le départ (argparse avec aliases)
- [ ] Tests unitaires de base

---

## Phase 2 : Gestion des médias

**Objectif** : Pouvoir s'abonner à des dépôts

- [ ] Parser `/etc/urpmi/urpmi.cfg`
- [ ] `urpm media list` / `urpm m l` / `urpm m ls`
- [ ] `urpm media add` / `urpm m a`
- [ ] `urpm media remove` / `urpm m r`
- [ ] `urpm media enable/disable` / `urpm m e` / `urpm m d`
- [ ] Support mirrorlist

---

## Phase 3 : Téléchargement et cache

**Objectif** : Synchroniser les métadonnées depuis les miroirs

- [ ] Module `download.py` (HTTP avec resume)
- [ ] `urpm media update` / `urpm m u` - sync métadonnées
- [ ] Import auto synthesis/hdlist → SQLite
- [ ] `urpm cache info/clean/rebuild` / `urpm c ...`

---

## Phase 4 : Recherche et requêtes

**Objectif** : CLI fonctionnelle pour les opérations read-only

- [ ] `urpm search` / `urpm s` - recherche
- [ ] `urpm query` / `urpm q` - alias pour compatibilité urpmq
- [ ] `urpm show` / `urpm sh` / `urpm info` - détails paquet
- [ ] `urpm list` / `urpm l` - installés, disponibles, updates
- [ ] `urpm provides` / `urpm p` - qui fournit
- [ ] `urpm find` / `urpm f` - quel paquet contient un fichier
- [ ] `urpm depends` / `urpm d` - dépendances
- [ ] `urpm rdepends` / `urpm rd` - dépendances inverses
- [ ] Support `--json`

---

## Phase 5 : Résolution de dépendances

**Objectif** : Calculer l'arbre des dépendances

- [ ] Algo résolution Python (MVP)
- [ ] Gestion provides/requires avec versions
- [ ] Détection conflits
- [ ] Option: binding C++/Rust pour perf

---

## Phase 6 : Installation / Désinstallation

**Objectif** : Actions concrètes sur le système

- [ ] `urpm install` / `urpm i` - installer
- [ ] `urpm reinstall` / `urpm ri` - réinstaller
- [ ] `urpm remove` / `urpm r` - désinstaller
- [ ] `urpm update` / `urpm u` - mise à jour paquets
- [ ] `urpm upgrade` - alias de `urpm update --all`
- [ ] `urpm autoremove` / `urpm ar` - orphelins
- [ ] Historique transactions SQLite
- [ ] Mode `--test` (dry-run)

---

## Phase 7 : Système de deltas

**Objectif** : Ne télécharger que ce qui change

- [ ] Format delta (à définir)
- [ ] Génération côté serveur
- [ ] Application côté client
- [ ] Fallback si trop de retard
- [ ] `urpm delta status/stats`

---

## Phase 8 : Wrappers rétrocompat

**Objectif** : Migration transparente

- [ ] `urpmi` → `urpm install`
- [ ] `urpme` → `urpm remove`
- [ ] `urpmq` → `urpm query`
- [ ] `urpmf` → `urpm find`
- [ ] `urpmi.addmedia` → `urpm media add`
- [ ] `urpmi.update` → `urpm media update`
- [ ] Mapping complet des options legacy
- [ ] Message d'avertissement "deprecated, use urpm ..."

---

## Phase 9 : Polish et production

**Objectif** : Prêt pour les utilisateurs

- [ ] Tests d'intégration
- [ ] Gestion erreurs robuste
- [ ] Logs et verbose
- [ ] Man pages
- [ ] Packaging RPM
- [ ] Doc contributeur
