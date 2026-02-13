# urpm-ng TODO

> Voir [doc/ROADMAP.md](doc/ROADMAP.md) pour la vision et les priorités.
> Voir [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) pour les décisions techniques.
> Voir [CHANGELOG.md](CHANGELOG.md) pour l'historique des fonctionnalités.

---

## En cours

### Alternatives (OR deps)
- [ ] Tests intensifs install avec alternatives
- [ ] Valider mode --auto
- [ ] Valider re-résolution après choix utilisateur

---

## Phase 1 : Fonctionnalités utilisateur

### needs-restarting
- [ ] Détecter reboot nécessaire (kernel, glibc, systemd)
- [ ] Lister services à redémarrer

### daemon config
- [ ] Fichier `/etc/urpm/daemon.conf`
- [ ] Options: `log_destination` (syslog|file), `log_file`, `log_level`
- [ ] Intégrer config auto-install (scheduling, policies)

### Documentation
- [ ] Revue complète : cohérence code/docs (chemins, options, comportements)
- [ ] Guide de migration urpmi → urpm (EN, FR)

### Download options
- [ ] Option `--download-only` sur upgrade
- [ ] Option `--destdir <path>` pour spécifier répertoire destination

---

## Phase 2 : Fonctionnalités avancées

### Internationalisation
- [ ] gettext
- [ ] Fichiers .po/.mo (fr, en, puis autres langues)

### system-upgrade
- [ ] Updates préliminaires
- [ ] Phase download
- [ ] Phase apply (reboot ou online)
- [ ] Gestion conflits version majeure

### Container commands (compléments)
- [ ] `urpm container list` : lister containers actifs
- [ ] `urpm container shell --image <tag>` : shell interactif
- [ ] `urpm container prune` : nettoyer containers terminés
- [ ] Test d'installation automatique dans container vierge

---

## Phase 3 : GUI & outils graphiques

### mgaonline-ng
- [ ] Applet systray
- [ ] Notification updates
- [ ] Suivi visuel

### groups (source rpmsrate)
- [ ] `urpm group list/info/install/remove`
- [ ] Cohérence avec `urpm seed`

### rpmdrake-ng
- [ ] IHM complète
- [ ] Recherche multicritères
- [ ] Gestion médias/peers/config

---

## Phase 4 : Consolidation

### Fonctions diverses
- [ ] offline-upgrade
- [ ] distro-sync
- [ ] swap (remove+install combiné)
- [ ] check intégrité BDD
- [ ] debuginfo-install

### Compatibilité legacy
- [ ] Wrappers urpmi/urpme/urpmq/urpmf avec mapping options

---

## Améliorations continues

### Idle detection
- [ ] Fix: reset `_last_net_sample` après batch de downloads

### Download stats & priorités serveurs
- [ ] Afficher stats par serveur/peer à la fin
- [ ] Tracker les performances des serveurs sur la durée
- [ ] Prioriser dynamiquement les serveurs les plus rapides

### Explications upgrade/remove
- [ ] Expliquer POURQUOI un paquet est supprimé
- [ ] Tracer les chaînes de dépendances pendant la résolution

### whatprovides
- [ ] Contraintes de version (== < <= > >=) pour filtrer

---

## Phase différée (entreprise)

- [ ] Infrastructure advisories (MGASA)
- [ ] downgrade
- [ ] APIs sécurisées (/api/upgrade, /api/install)
- [ ] Gestion de parc (inventaire, déploiement)
- [ ] Console de gestion centralisée

---

## Déjà implémenté (historique)

<details>
<summary>Voir les fonctionnalités complétées</summary>

### v0.2.x
- [x] D-Bus service (org.mageia.Urpm.v1)
- [x] PackageKit backend (pk-backend-urpm)
- [x] PolicyKit authentication
- [x] AppStream per-media avec fusion catalogue
- [x] TransactionQueue (install+erase atomique)
- [x] Réplication intelligente (rpmsrate seeds)
- [x] Download multi-release/arch
- [x] Container builds (mkimage, build)
- [x] BuildRequires parser (--builddeps)

### v0.1.x
- [x] Package holds (hold/unhold)
- [x] Obsoletes detection dans upgrade
- [x] urpm info avec Recommends, Suggests, Conflicts, Obsoletes
- [x] Filtrage par version système
- [x] Bash autocompletion
- [x] Installation RPM local avec résolution deps
- [x] Pages man (EN/FR)
- [x] --download-only sur install
- [x] urpm download
- [x] urpmf (search files) avec FTS5
- [x] hdlist.cz parser
- [x] files.xml.lzma sync
- [x] CacheManager avec quotas et éviction
- [x] why / rdepends
- [x] whatprovides
- [x] Alternatives (OR deps) - base implémentée

</details>
