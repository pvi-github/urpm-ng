# urpm-ng TODO

> Voir [doc/ROADMAP.md](doc/ROADMAP.md) pour la vision et les priorités.
> Voir [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) pour les décisions techniques.
> Voir [CHANGELOG.md](CHANGELOG.md) pour l'historique des fonctionnalités.

---

## En cours

### Config files (.rpmnew)
- [x] `--config-policy=keep|replace|ask` pour install/upgrade
- [ ] `--config-policy=merge` : diff interactif (vimdiff/meld)

### Container builds
- [x] `--with-rpms` pour pré-installer des RPMs locaux
- [x] Profils YAML pour mkimage (`build`, `ci`, `minimal`)
- [x] Output workspace correct (`RPMS/x86_64/`, `SRPMS/`, `SPECS/log.<Name>`)
- [ ] SSL `update-ca-trust` à intégrer dans mkimage (pas en post-build)
- [ ] Diagnostiquer et corriger les erreurs lors de `urpm mkimage` / `urpm build` :
  - Erreurs "groupe manquant dans /etc/group" ou "utilisateur manquant dans /etc/passwd"
  - Cause probable : mauvais ordre d'installation des RPMs, ou RPM manquant (ex. setup, shadow-utils)
  - La bonne solution peut être : corriger l'ordre d'install, ajouter des RPMs au profil, ou les deux
  - Ne pas masquer les erreurs : les comprendre et les corriger à la racine

### Import urpmi.cfg
- [x] Parser configs avec `mirrorlist: $MIRRORLIST`
- [x] Message suggérant autoconfig pour media officiels

---

## Phase 1 : Fonctionnalités utilisateur

### needs-restarting
- [ ] Détecter reboot nécessaire (kernel, glibc, systemd)
- [ ] Lister services à redémarrer

### daemon
- [ ] Fichier `/etc/urpm/daemon.conf`
- [ ] Options: `log_destination` (syslog|file), `log_file`, `log_level`
- [ ] Reload config à chaud (SIGHUP)
- [ ] Intégrer config auto-install (scheduling, policies)

### Documentation
- [ ] Revue complète : cohérence code/docs (chemins, options, comportements)
- [ ] Guide de migration urpmi → urpm (EN, FR)
- [ ] Mettre à jour README.md (fonctionnalités implémentées depuis la dernière rédaction)
- [ ] Mettre à jour les pages man (EN/FR) pour les nouvelles commandes et options

### Download options
- [ ] Option `--download-only` sur upgrade
- [ ] Option `--destdir <path>` pour spécifier répertoire destination

### Alternatives (OR deps)
- [ ] Tests intensifs install avec alternatives
- [ ] Valider mode --auto
- [ ] Valider re-résolution après choix utilisateur

---

## Phase 2 : Fonctionnalités avancées

### Internationalisation CLI
- [ ] Audit des chaînes non wrappées dans `urpm/cli/` — certaines strings échappent encore à `_()`
- [ ] Compléter les fichiers `.po` existants (fr, de, es, nl, pt) avec les chaînes manquantes
- [ ] Infrastructure i18n pour rpmdrake-ng (rien n'existe côté GUI pour l'instant)
- [ ] Wrapper les chaînes de rpmdrake-ng avec gettext une fois l'infrastructure en place

### Système de debug
- [ ] Généraliser `--debug` au-delà de `--debug solver` : définir des domaines cohérents
  - Domaines envisagés : `solver`, `download`, `db`, `peer`, `daemon`, `appstream`, `build`
  - Syntaxe cible : `--debug solver,download` ou `--debug all`
- [ ] Uniformiser l'activation du debug dans tous les modules
- [ ] S'assurer que chaque domaine a des logs utiles et pas trop verbeux

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

### PackageKit / GNOME Software
- [ ] Investiguer pourquoi les mises à jour déclenchées depuis GNOME Software ne terminent pas
  - Vérifier les logs D-Bus (`journalctl -u packagekit` + `journalctl -u urpm-dbus`)
  - Identifier si c'est un timeout, un deadlock, ou un signal manquant côté backend
  - Corriger le bug à la racine dans `urpm/dbus/` ou le backend PackageKit
- [ ] Tests de non-régression : install, remove, upgrade depuis Discover et GNOME Software

### AppStream
- [ ] Enrichir l'AppStream de secours généré quand le miroir ne fournit pas d'`appstream.xml.lzma` :
  - Ajouter la licence (`project_license`) depuis les métadonnées RPM
  - Ajouter la description longue si disponible
  - Ajouter les catégories depuis les tags RPM Group
  - Ajouter l'URL du projet si présente dans les métadonnées RPM
- [ ] Faire gérer les paquets non graphiques (libs, outils CLI) par Discover et GNOME Software
  - Investiguer les filtres AppStream qui les excluent actuellement
  - Trouver le bon type de composant AppStream (`console-application`, `addon`, etc.)

### mgaonline-ng
- [ ] Applet systray
- [ ] Notification updates
- [ ] Suivi visuel

### groups (source rpmsrate)
- [ ] `urpm group list/info/install/remove`
- [ ] Cohérence avec `urpm seed`

### rpmdrake-ng

#### Refonte UX — priorité haute (retours utilisateurs)

Le premier jet est fonctionnel mais l'UX est jugée trop proche d'un gestionnaire de paquets Debian
(liste brute, colonnée) alors que la cible doit être à la hauteur de Discover ou GNOME Software.
Les utilisateurs comparent défavorablement à GNOME Software sur d'autres distros.

**Présentation générale**
- [ ] Icônes de paquets (depuis AppStream ou icône générique selon catégorie)
- [ ] Vue vignette optionnelle (en plus de la vue liste)
- [ ] Filtrer les descriptions parasites : ne pas afficher `gpg(bla bla bla)` comme description
- [ ] Panneau détail enrichi : description longue, screenshots AppStream, URL projet, licence

**Couleurs et lisibilité**
- [ ] Ajouter une légende visible des codes couleur (qu'est-ce qui correspond à quoi)
- [ ] Revoir la palette : deux nuances de bleu trop proches, difficiles à distinguer
- [ ] Cohérence globale des couleurs dans tous les états de l'UI

**Catégories**
- [ ] Revoir l'arbre de catégories : trop granulaire et mal regroupé
  - Fusionner : Sounds → Audio, Videos → Multimédia
  - S'inspirer des catégories du CCM Mageia et/ou du rpmdrake original (arbre plus compact)
  - Regarder la source de catégories utilisée par le rpmdrake original
  - Masquer ou regrouper devel/debug : l'utilisateur moyen ne sait pas à quoi ça sert
- [ ] Clarifier visuellement que "État" (installé/disponible/etc.) est lié à la liste en dessous

**Filtres**
- [ ] Réorganiser les filtres : actuellement éparpillés (au-dessus de l'arbre, en dessous...)
- [ ] Regrouper les filtres de façon logique et cohérente dans un seul panneau

**i18n**
- [ ] Infrastructure gettext pour rpmdrake-ng (rien n'existe côté GUI)
- [ ] Wrapper toutes les chaînes UI avec `_()`

**Tour / onboarding**
- [ ] Implémenter un tour guidé pour découvrir l'interface sans documentation
  - Qt Wizard ou système d'overlay avec tooltips contextuels
  - Accessible depuis un bouton "?" ou au premier lancement

**Fonctionnalités manquantes**
- [ ] IHM complète (voir [doc/SPEC-RPMDRAKE-NG.md](doc/SPEC-RPMDRAKE-NG.md))
- [ ] Recherche multicritères
- [ ] Gestion médias/peers/config

#### Améliorations futures rpmdrake-ng
- [ ] Screenshots AppStream dans panneau détails
- [ ] Indicateur mises à jour sécurité (MGASA) : badge ou icône
- [ ] Changelog preview (tooltip ou ligne dépliable)
- [ ] Favoris/signets : marquer des paquets surveillés
- [ ] Collections : sauvegarder/partager des listes de paquets
- [ ] Mode simulation (dry-run visuel)
- [ ] Jauge espace disque avant/après dans confirmation
- [ ] ETA estimé téléchargement + installation
- [ ] Historique avec "Annuler cette transaction" (rollback)

---

## Phase 4 : Consolidation

### Fonctions diverses
- [ ] offline-upgrade
- [ ] distro-sync
- [ ] swap (remove+install combiné)
- [ ] check intégrité BDD
- [ ] debuginfo-install
- [ ] history reinstall : intégrer resolver/downloader proprement
- [ ] rpmsrate LOCALES : rendre configurable

### Compatibilité legacy
- [ ] Wrappers urpmi/urpme/urpmq/urpmf avec mapping options

---

## Améliorations continues

### Idle detection
- [ ] Fix: reset `_last_net_sample` après batch de downloads

### Download stats & priorités serveurs
- [x] Persister le débit constaté par serveur (SQLite, EWMA α=0.3) entre les sessions
- [x] Trier dynamiquement les serveurs par débit mesuré (priority DESC, bandwidth_kbps DESC)
- [x] Adaptation in-session sans attendre la prochaine exécution (dict mémoire lock-protégé)
- [x] Failover automatique sur download et sync metadata (boucle sur serveurs classés)
- [x] `urpm server stats` : affichage débit, latence, taux de succès, dernière vérification
- [ ] Afficher stats par serveur/peer à la fin du téléchargement (résumé post-install)
- [ ] Déprioriser automatiquement les serveurs lents ou instables (seuil configurable)
- [ ] Réinitialiser les stats si le serveur redevient rapide (fenêtre glissante)

### Explications upgrade/remove
- [ ] Expliquer POURQUOI un paquet est supprimé
- [ ] Tracer les chaînes de dépendances pendant la résolution

### whatprovides
- [ ] Contraintes de version (== < <= > >=) pour filtrer

### Nommage API (cohérence avec urpme)
- [ ] Renommer `resolve_remove` → `resolve_erase`
- [ ] Renommer `TransactionType.REMOVE` → `TransactionType.ERASE`
- [ ] Renommer `execute_erase` reste correct (déjà "erase")

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

### v0.3.x
- [x] `--config-policy` pour install/upgrade (keep, replace, ask)
- [x] `--with-rpms` pour build (pré-install RPMs locaux)
- [x] Profils YAML mkimage (build, ci, minimal)
- [x] Output workspace rpmbuild (RPMS/arch/, SRPMS/, SPECS/log.*)
- [x] Import urpmi.cfg avec mirrorlist $MIRRORLIST
- [x] Modularisation CLI (urpm/cli/commands/)
- [x] Modularisation database (urpm/core/db/)
- [x] Modularisation resolver (urpm/core/resolution/)
- [x] Sous-paquets RPM (core, daemon, build, desktop, etc.)

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
