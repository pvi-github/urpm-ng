# Revue comparative : DNF vs urpm-ng

Ce document identifie les fonctionnalités de DNF (et DNF5) absentes ou incomplètes dans urpm-ng, afin de prioriser les développements pour atteindre la parité fonctionnelle.

## Légende

| Symbole | Signification |
|---------|---------------|
| ✅ | Implémenté dans urpm-ng |
| ⚠️ | Partiellement implémenté |
| ❌ | Non implémenté (gap à combler) |
| ➖ | Non applicable à Mageia |

---

## 1. Gestion des paquets de base

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| install | `dnf install` | ✅ `urpm install` | Complet |
| remove | `dnf remove` | ✅ `urpm erase` | Complet |
| upgrade | `dnf upgrade` | ✅ `urpm upgrade` | Complet |
| downgrade | `dnf downgrade` | ❌ | **À implémenter** |
| reinstall | `dnf reinstall` | ✅ `urpm install --reinstall` | Complet |
| autoremove | `dnf autoremove` | ✅ `urpm autoremove` | Complet, même étendu |
| swap | `dnf swap pkg1 pkg2` | ❌ | Transaction combinée remove+install |
| distro-sync | `dnf distro-sync` | ❌ | Sync vers versions exactes du dépôt |
| check | `dnf check` | ❌ | Vérification intégrité BDD |

### Priorités
- **downgrade** : DIFFÉRÉE - rollback manuel, cas d'usage moins fréquent
- **distro-sync** : MOYENNE - utile pour réaligner un système sur le dépôt

---

## 2. Mises à jour de sécurité et advisories

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| Lister advisories | `dnf updateinfo list` | ❌ | Afficher les advisories disponibles |
| Info advisory | `dnf updateinfo info XXXX` | ❌ | Détails d'un advisory |
| Filtrer par CVE | `--cve CVE-2024-xxxx` | ❌ | Installer/lister par CVE |
| Filtrer par advisory | `--advisory MGASA-2024-xxxx` | ❌ | Installer par ID advisory |
| Filtrer par sévérité | `--security --sec-severity Critical` | ❌ | Critical/Important/Moderate/Low |
| Updates sécurité only | `dnf upgrade --security` | ❌ | N'installer que les patches sécu |
| Bugzilla filter | `--bz 12345` | ❌ | Filtrer par bug ID |

### Priorité : DIFFÉRÉE
Fonctionnalité entreprise nécessitant une infrastructure conséquente. À aborder une fois la base stabilisée.

### Prérequis
- Mageia doit publier des métadonnées d'advisories (format updateinfo.xml ou équivalent)
- Parser et stocker ces métadonnées dans la BDD urpm
- Base urpm-ng stable et communauté établie

---

## 3. Mises à jour automatiques et hors-ligne

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| Updates automatiques | `dnf-automatic` | ⚠️ | urpmd pré-télécharge mais n'installe pas auto |
| Config auto-updates | `/etc/dnf/automatic.conf` | ❌ | Scheduling, notification, auto-install |
| Offline upgrade | `dnf offline-upgrade download` | ❌ | Télécharger puis appliquer au reboot |
| Upgrade minimal | `dnf upgrade-minimal` | ❌ | Minimum nécessaire pour fix sécu/bug |

### Priorité : MOYENNE
- **offline-upgrade** : Important pour serveurs de production (appliquer au reboot propre)
- **automatic** : urpmd a la base, manque la partie auto-install configurable

---

## 4. Recherche et requêtes avancées

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| search | `dnf search` | ✅ `urpm search` | Complet |
| info | `dnf info` | ✅ `urpm show` | Complet |
| list | `dnf list` | ✅ `urpm list` | Complet |
| provides | `dnf provides` | ✅ `urpm whatprovides` | Complet |
| repoquery | `dnf repoquery` | ✅ via show/list/depends/… | Cœur complet ; manque filtres avancés (--arch, --srpm, --qf, --duplicates) |
| repoquery --files | `dnf repoquery -l pkg` | ✅ `urpm show --files pkg` | Fichiers installés + disponibles (via files.xml) |
| repoquery --requires | `dnf repoquery --requires` | ✅ `urpm depends` | Complet |
| repoquery --whatrequires | `dnf repoquery --whatrequires` | ✅ `urpm rdepends` | Complet |
| deplist | `dnf deplist` | ✅ `urpm depends` | Équivalent |

### Priorité : BASSE
urpm-ng couvre la majorité des cas d'usage. Le parsing hdlist.cz améliorerait les requêtes fichiers.

---

## 5. Groupes de paquets

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| group list | `dnf group list` | ❌ | Lister les groupes disponibles |
| group info | `dnf group info "Group Name"` | ❌ | Contenu d'un groupe |
| group install | `dnf group install "Group Name"` | ❌ | Installer un groupe |
| group remove | `dnf group remove "Group Name"` | ❌ | Supprimer un groupe |
| group upgrade | `dnf group upgrade` | ❌ | Mettre à jour un groupe |
| group mark | `dnf group mark install` | ❌ | Marquer groupe installé |

### Priorité : HAUTE
Les groupes de paquets facilitent l'installation d'environnements complets et attirent les utilisateurs.

**Implémentation** : Réutiliser la même source de données que le seeding (rpmsrate/compssUsers.pl). Cette approche garantit la cohérence entre :
- `urpm group list/install` pour les utilisateurs
- `urpm seed` pour la création de miroirs thématiques

```
urpm group list           → Liste les groupes disponibles (Plasma, GNOME, Développement, etc.)
urpm group info plasma    → Détail du contenu du groupe
urpm group install plasma → Installe l'ensemble des paquets du groupe
```

---

## 6. Modules (streams de versions)

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| module list | `dnf module list` | ➖ | Mageia n'utilise pas les modules |
| module enable | `dnf module enable nodejs:18` | ➖ | |
| module install | `dnf module install nodejs:18/default` | ➖ | |

### Non applicable
Mageia gère les versions multiples différemment (php8.3, php8.4 comme paquets séparés). Le système de préférences urpm (`--prefer=php:8.4`) couvre ce besoin.

---

## 7. Gestion des dépôts

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| repolist | `dnf repolist` | ✅ `urpm media list` | Complet |
| repoinfo | `dnf repoinfo` | ⚠️ | Basique, pas toutes les stats |
| config-manager | `dnf config-manager --add-repo` | ✅ `urpm media add` | Complet |
| repo enable/disable | `dnf config-manager --enable/--disable` | ✅ `urpm media enable/disable` | Complet |
| repo priority | Configuration priorité | ✅ `urpm server priority` | Complet |
| makecache | `dnf makecache` | ✅ `urpm media update` | Complet |
| clean | `dnf clean all` | ✅ `urpm cache clean` | Complet |

### Priorité : BASSE
Couverture fonctionnelle satisfaisante.

---

## 8. Historique et rollback

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| history list | `dnf history` | ✅ `urpm history` | Complet |
| history info | `dnf history info N` | ✅ `urpm history N` | Complet |
| history undo | `dnf history undo N` | ✅ `urpm undo N` | Complet |
| history redo | `dnf history redo N` | ❌ | Rejouer une transaction |
| history rollback | `dnf history rollback N` | ✅ `urpm rollback` | Complet |
| history replay | `dnf history replay file.json` | ❌ | Rejouer depuis fichier |
| history store | `dnf history store` | ❌ | Sauvegarder transaction |
| history userinstalled | `dnf history userinstalled` | ⚠️ | Via installed-through-deps.list |

### Priorité : BASSE
Les fonctions essentielles sont présentes. `redo` et `replay` sont des nice-to-have.

---

## 9. Options de téléchargement

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| --downloadonly | `dnf install --downloadonly` | ✅ `urpm install --download-only` | Télécharger sans installer |
| download command | `dnf download pkg` | ✅ `urpm download` / `urpm dl` | Télécharger RPM localement |
| --cacheonly | `dnf --cacheonly` | ❌ | Opérer depuis cache uniquement |

### Priorité : ~~HAUTE~~ FAIT
- **--downloadonly** : ✅ Implémenté (`urpm install --download-only`)
- **download** : ✅ Implémenté (`urpm download` / `urpm dl`)

---

## 10. Développement et debug

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| builddep | `dnf builddep foo.spec` | ✅ `urpm install --buildrequires` / `-b` | Installer les dépendances de build |
| debuginfo-install | `dnf debuginfo-install pkg` | ❌ | Installer les debuginfo |
| download --source | `dnf download --source pkg` | ❌ | Télécharger le SRPM |

### Priorité : builddep ✅ FAIT
- **builddep** : ✅ Implémenté (`urpm install --buildrequires` / `--builddeps` / `--br` / `-b`)
- debuginfo-install, download --source : MOYENNE

---

## 11. Plugins et extensions

| Fonctionnalité | DNF | urpm-ng | Notes |
|----------------|-----|---------|-------|
| Système de plugins | Architecture modulaire | ❌ | Pas de système de plugins |
| versionlock | `dnf versionlock` | ✅ `urpm hold` / `urpm unhold` | Complet (reason tracking, list) |
| needs-restarting | `dnf needs-restarting` | ✅ intégré dans install/upgrade | Détection auto via `should-restart` provides |
| system-upgrade | `dnf system-upgrade` | ❌ | Upgrade de version majeure |

### Priorités
- **system-upgrade** : HAUTE - **killer feature** pour l'adoption (Mageia 9 → 10)
- **needs-restarting** : ✅ FAIT (intégré dans cmd_install/cmd_upgrade via `check_needs_restart_from_provides`)
- **versionlock** : ✅ FAIT (`urpm hold` / `urpm unhold`, reason tracking, list)

---

## 12. Performance et architecture (DNF5)

DNF5 a été réécrit en C++ pour compenser les problèmes de performance de
DNF4 (démarrage lent, résolution séquentielle, absence de parallélisation).
urpm-ng a pris le problème à l'envers : une **architecture performante par
conception** (daemon, pré-téléchargement, parallélisation, P2P, indexation
FTS) plutôt qu'un portage brut dans un langage plus rapide.

Résultat mesuré : **urpm est 2 à 3× plus rapide que DNF** sur les
opérations courantes (install, upgrade, search) malgré un backend Python.

| Fonctionnalité | DNF5 | urpm-ng | Avantage |
|----------------|------|---------|----------|
| Résolveur | libsolv | ✅ libsolv | Parité (même moteur C++) |
| Téléchargements parallèles | Oui | ✅ Oui, multi-serveurs | urpm : répartition intelligente entre serveurs avec stats EWMA |
| Pré-téléchargement | Non | ✅ urpmd idle-aware | urpm : le daemon pré-charge en arrière-plan pendant l'idle |
| Partage P2P LAN | Non | ✅ UDP broadcast + HTTP | urpm : cache local > peers LAN > miroirs distants |
| Indexation fulltext | Non | ✅ SQLite FTS5 | urpm : recherche instantanée sans re-scan |
| Traitements arrière-plan | dnf5daemon (D-Bus) | ✅ urpmd (HTTP API) + PackageKit D-Bus | urpm : scheduler, triggers en background, sync metadata |
| Intégration GUI (Discover, GNOME Software) | PackageKit (dnf backend) | ✅ PackageKit (backend urpm D-Bus + PolicyKit) | Parité : même intégration desktop via D-Bus |
| Démarrage CLI | ~1.5s (C++) | ✅ ~0.35s | urpm : 4× plus rapide grâce à la DB locale + libsolv natif |
| Langage | C++ | Python | DNF5 compense l'archi par le langage ; urpm compense le langage par l'archi |
| Maintenabilité | Faible (C++) | ✅ Élevée (Python) | Onboarding contributeurs nettement plus facile |

---

## 13. Fonctionnalités uniques à urpm-ng

Ces fonctionnalités n'existent PAS dans DNF et sont un avantage de urpm-ng :

### Réseau et distribution

| Fonctionnalité | Commande | Notes |
|----------------|----------|-------|
| P2P LAN | `urpm peer` | Partage automatique de paquets entre machines LAN |
| Découverte peers | automatique | Broadcast UDP, TTL 180s, zero-config |
| Priorité de cache | automatique | Cache local > peers LAN > miroirs distants |
| Stats serveurs EWMA | `urpm server stats` | Tri dynamique des miroirs par performance mesurée |
| Failover intelligent | automatique | Bascule automatique sur serveur alternatif avec blacklist temporaire |
| Replication seed | `urpm seed` | Créer un miroir offline type DVD à partir de rpmsrate |

### Résolution et installation

| Fonctionnalité | Commande | Notes |
|----------------|----------|-------|
| Préférences `--prefer` | `urpm install --prefer php:8.4` | Guider les choix de providers avec version, positif/négatif |
| Alternatives interactives | automatique | Choix utilisateur quand plusieurs providers (DNF choisit silencieusement) |
| needs-restarting intégré | automatique | Détection reboot/services à redémarrer directement après install/upgrade |
| README.urpmi | `urpm readme` | Affichage des messages packager après installation |
| Orphelins versionnés | `urpm autoremove` | Détection d'orphelins avec graphe reverse-dep versionné |

### Outils développeurs / packageurs

| Fonctionnalité | Commande | Notes |
|----------------|----------|-------|
| Build containers | `urpm mkimage` / `urpm build` | Construction d'images chroot + build de RPMs isolé |
| Arbre de dépendances | `urpm depends --tree` | Visualisation arborescente des dépendances |
| Arbre de reverse-deps | `urpm rdepends --tree` | Visualisation arborescente des dépendances inverses |

### Daemon et arrière-plan

| Fonctionnalité | Commande | Notes |
|----------------|----------|-------|
| Daemon idle-aware | `urpmd` | Scheduler qui attend l'idle système pour travailler |
| Pré-téléchargement | automatique | Le daemon pré-charge les mises à jour en arrière-plan |
| Triggers en background | automatique | Les scriptlets rpm tournent en tâche de fond après extraction |
| Sync metadata planifié | `urpmd` | Rafraîchissement automatique des métadonnées |

### Requêtes et gestion

| Fonctionnalité | Commande | Notes |
|----------------|----------|-------|
| Recherche fulltext FTS5 | `urpm search` / `urpm q` | Indexation SQLite FTS5, résultats instantanés |
| Explication d'installation | `urpm why` | Trace la chaîne de dépendances qui a installé un paquet |
| Historique avec rollback | `urpm history undo/rollback` | Historique complet des transactions avec annulation |
| Hold avec raison | `urpm hold --reason "..."` | Gel de version avec justification traçable |
| Backend PackageKit D-Bus | automatique | Intégration Discover / GNOME Software via D-Bus + PolicyKit |
| Politique auto-upgrade | `urpm config gnome-auto-updates` | Contrôle de l'auto-update GNOME/Discover/PackageKit |

### En cours / planifié

| Fonctionnalité | Statut | Notes |
|----------------|--------|-------|
| rpmdrake-ng (Qt6) | 🚧 | GUI native Qt6 avec architecture MVC |
| Proxy cross-version | 🚧 | Servir des paquets pour autre version Mageia |
| Gestion de parc | 🚧 | Inventaire et déploiement centralisé |

---

## Résumé des priorités

> **Vision** : Construire une base saine qui attire la communauté, avant d'aborder les chantiers entreprise. L'algorithme de résolution des dépendances doit être battle-tested, l'architecture doit permettre le développement de GUI et d'outils tiers.

### Priorité HAUTE (attirer la communauté, killer features)

1. **system-upgrade** (Section 11)
   - Upgrade de version majeure Mageia (9 → 10)
   - **Killer feature** pour l'adoption

2. **groups** (Section 5)
   - Basé sur la même source que le seeding (rpmsrate)
   - Installation simplifiée d'environnements complets

3. ~~**needs-restarting** (Section 11)~~ ✅ FAIT — intégré dans install/upgrade

4. ~~**--downloadonly** et `download` (Section 9)~~ ✅ FAIT
   - `urpm install --download-only`, `urpm download` / `urpm dl`

5. ~~**builddep** (Section 10)~~ ✅ FAIT
   - `urpm install --buildrequires` / `--builddeps` / `--br` / `-b`

6. **automatic** config complète (Section 3)
   - Compléter urpmd avec configuration auto-install

### Priorité MOYENNE

7. **offline-upgrade** (Section 3)
8. **distro-sync** (Section 1)
9. **swap** (Section 1)
10. **check** intégrité BDD (Section 1)
11. **debuginfo-install** (Section 10)
12. **history redo/replay** (Section 8)

### Priorité DIFFÉRÉE (chantiers entreprise)

Ces fonctionnalités nécessitent une infrastructure conséquente (APIs sécurisées, métadonnées advisories, pilotage centralisé). À aborder une fois la base stabilisée et la communauté établie.

13. **Sécurité / Advisories** (Section 2)
    - Parsing métadonnées MGASA, filtres --security/--cve
    - Requiert que Mageia publie les métadonnées

14. ~~**versionlock** (Section 11)~~ ✅ FAIT
    - `urpm hold` / `urpm unhold` (reason tracking, list)

15. **downgrade** (Section 1)
    - Revenir à version antérieure

16. **APIs pilotage centralisé**
    - /api/upgrade, /api/install sécurisés
    - Console de gestion, inventaire parc

---

## Plan d'action suggéré

### Phase 0 : Fondations (en continu)

```
- Stabiliser l'algorithme de résolution des dépendances
  - Tests approfondis sur cas réels complexes
  - Couverture de tests unitaires et d'intégration

- Architecture extensible
  - API interne claire pour futures GUI
  - Séparation CLI / bibliothèque / daemon
  - Documentation développeur
```

### Phase 1 : Killer features et adoption (Priorité HAUTE)

```
1. Implémenter `urpm system-upgrade`
   - Phase download : télécharger tous les paquets nouvelle version
   - Phase apply : appliquer au reboot (ou online si possible)
   - Gestion des conflits de version majeure

2. Implémenter `urpm group`
   - Réutiliser la source rpmsrate/compssUsers.pl (comme seeding)
   - urpm group list / info / install / remove
   - Cohérence avec urpm seed

3. ~~Implémenter `urpm needs-restarting`~~ ✅ FAIT — intégré dans cmd_install/cmd_upgrade

4. ✅ --downloadonly et `urpm download` — FAIT
   - `urpm install --download-only`, `urpm download` / `urpm dl`

5. ✅ builddep — FAIT
   - `urpm install --buildrequires` / `--builddeps` / `--br` / `-b`

6. Compléter automatic config (urpmd)
   - Configuration auto-install (pas seulement pré-téléchargement)
   - Équivalent dnf-automatic
```

### Phase 2 : Consolidation (Priorité MOYENNE)

```
7. offline-upgrade (télécharger puis appliquer au reboot)
8. distro-sync (réaligner sur versions exactes du dépôt)
9. swap (transaction combinée remove+install)
10. check intégrité BDD
11. debuginfo-install
```

### Phase 3 : Entreprise (Priorité DIFFÉRÉE)

À aborder une fois la communauté établie et la base stable.

```
12. Infrastructure advisories (nécessite métadonnées Mageia)
13. ✅ versionlock — FAIT (`urpm hold` / `urpm unhold`)
14. downgrade
15. APIs sécurisées pour pilotage centralisé
```

---

## Sources

- [DNF Command Reference](https://dnf.readthedocs.io/en/latest/command_ref.html)
- [Fedora DNF Documentation](https://docs.fedoraproject.org/en-US/quick-docs/dnf/)
- [DNF5 Switch - Fedora Wiki](https://fedoraproject.org/wiki/Changes/SwitchToDnf5)
- [Red Hat Security Updates](https://docs.redhat.com/en/documentation/red_hat_enterprise_linux/9/html-single/managing_and_monitoring_security_updates/index)
- [DNF vs DNF5 - TecMint](https://www.tecmint.com/dnf-vs-dnf5/)
