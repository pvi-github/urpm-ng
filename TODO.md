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
- [x] Diagnostiquer et corriger les erreurs lors de `urpm mkimage` / `urpm build` :
  - Erreurs "groupe manquant dans /etc/group" ou "utilisateur manquant dans /etc/passwd"
  - Cause probable : mauvais ordre d'installation des RPMs, ou RPM manquant (ex. setup, shadow-utils)
  - La bonne solution peut être : corriger l'ordre d'install, ajouter des RPMs au profil, ou les deux
  - Ne pas masquer les erreurs : les comprendre et les corriger à la racine
- [ ] Exposer `--allow-no-root` en flag CLI pour install/upgrade/erase
  - Actuellement positionné seulement en interne par `build.py`/`mkimage.py` via `argparse.Namespace(..., allow_no_root=True)`
  - Permettrait à un utilisateur final de piloter un chroot user-owned depuis la CLI (user namespaces)
  - Le code backend gère déjà tout (InstallLock avec root alternatif, `use_userns`, etc.) — il manque juste l'exposition
- [ ] mkimage rootless avec scriptlets via `unshare --user --mount --pid --fork --map-root-user`
  - Actuellement rootless = `--noscripts` (pas de /proc ni /sys, pas de scriptlets)
  - Évolution : wrapper la phase d'install dans un user namespace pour monter /proc+/sys et exécuter les scriptlets
  - Permettrait des images conteneur 100% identiques root vs rootless
- [ ] Modification d'une image existante (chroot user-owned)
  - `urpm upgrade --root /path/to/image` : mettre à jour les paquets d'un chroot déjà construit
  - `urpm install --root /path/to/image <pkgs>` : ajouter des paquets a posteriori
  - `urpm erase --root /path/to/image <pkgs>` : retirer des paquets
  - Pré-requis : voir le point `--allow-no-root` ci-dessus
  - Cas d'usage : itérer sur une image de dev sans la reconstruire depuis zéro

### Import urpmi.cfg
- [x] Parser configs avec `mirrorlist: $MIRRORLIST`
- [x] Message suggérant autoconfig pour media officiels

---

## Phase 1 : Fonctionnalités utilisateur

### Queries et dépendances
- [ ] `--installed` / `-i` pour `depends` et `rdepends` : restreindre
  l'affichage aux paquets actuellement installés
  - Intérêt : "qu'est-ce qui dépend *réellement* de X sur ce système",
    sans le bruit des dépendances facultatives non installées
  - Cohérent avec le flag `--installed` déjà supporté par `search`
  - README mentionnait cette option à tort (retirée le 2026-04-10) : à
    ré-introduire quand le flag sera réellement implémenté

### Tests
- [ ] Corriger `test_simple_c_then_d` (TestHandleConflictDeps) qui échoue en sandbox userns
  - `unshare process exited with code 1` — rpm dans user namespace ne fonctionne pas sur la VM de dev
  - Diagnostiquer si c'est un problème de config VM (subuid/subgid, kernel) ou un bug du test harness

### Ergonomie CLI
- [x] ~~Renommer `urpm server autoconf` en `urpm server autoconfig`~~ (déjà fait)
- [ ] Renommer `urpm media discover --with` / `--without` en
  `--enabled` / `--disabled`
  - `--with` / `--without` laissent croire à un filtrage sur les paquets
    présents, alors qu'il s'agit en réalité d'activer/désactiver des
    media au moment de la découverte
  - Garder les anciens noms comme alias cachés pendant une release
    pour ne pas casser les scripts existants
  - Mettre à jour README, man pages, complétion bash
- [ ] Sélection interactive depuis les résultats de recherche
  - Liste numérotée + prompt "Install which one?" après `urpm search`
  - Permettrait d'installer directement depuis les résultats sans retaper le nom
  - Ref: spécification dans `doc/archives/urpm_modern_cli.md`

### Contrôle des serveurs (feedback utilisateur 2026-04-12)
- [x] Contrôle de l'auto-ajout de serveurs
  - Option dans `/etc/urpm/urpm.conf` (ou dropin) :
    `server_auto_add = true | false | ask`
  - `true` = comportement actuel (défaut, rétrocompatible)
  - `false` = seuls les serveurs ajoutés manuellement sont utilisés
  - `ask` = confirmation interactive (CLI) / notification (GUI/daemon)
  - Use-case : "je veux utiliser MON miroir local, rien d'autre"
- [x] Filtrage géographique des serveurs
  - `server_country_blacklist = UA, RU` (codes ISO 3166)
  - `server_country_whitelist = FR, DE, NL` (mutuellement exclusif)
  - Si les deux renseignées : whitelist gagne
  - Filtre au `discover` et au tri des miroirs, pas en supprimant
    des serveurs déjà ajoutés
  - Use-case : ne pas surcharger l'infra d'un pays en crise,
    contraintes RGPD (rester en EU)
  - Pré-requis : les miroirs Mageia exposent le pays dans
    `mirrors.json` / `media.cfg` — vérifier la couverture

### needs-restarting
- [x] Détecter reboot nécessaire (kernel, glibc, systemd)
- [x] Lister services à redémarrer
- Intégré dans `cmd_install` et `cmd_upgrade` via `check_needs_restart_from_provides`

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
- [x] Option `--download-only` sur upgrade
- [ ] Option `--destdir <path>` pour spécifier répertoire destination

### Alternatives (OR deps)
- [ ] Tests intensifs install avec alternatives
- [ ] Valider mode --auto
- [ ] Valider re-résolution après choix utilisateur

---

## Phase 2 : Fonctionnalités avancées

### Système de priorités serveurs (basse prio)
- [ ] Revoir le système `urpm server priority` : pas de borne, pas de
  presets, quasiment pas utilisé malgré la doc
  - Options à discuter : presets nommés (`high/normal/low/fallback`),
    bornage, meilleure doc, ou laisser tel quel
  - À traiter après le contrôle auto-ajout + filtrage géo (Phase 1)

### Internationalisation CLI
- [ ] Audit des chaînes non wrappées dans `urpm/cli/` — certaines strings échappent encore à `_()`
- [ ] Compléter les fichiers `.po` existants (fr, de, es, nl, pt) avec les chaînes manquantes
- [ ] Infrastructure i18n pour rpmdrake-ng (rien n'existe côté GUI pour l'instant)
- [ ] Wrapper les chaînes de rpmdrake-ng avec gettext une fois l'infrastructure en place

### Système de debug
- [ ] Généraliser `--debug` avec domaines et niveaux de verbosité
  - Syntaxe cible : `--debug solver=3,download=5,db=1` ou `--debug all=2`
  - Niveaux : 0 = muet, 1 = erreurs, 2 = warnings, 3 = info (défaut si =N absent), 4 = verbose, 5 = ultra-verbeux
  - Domaines : `solver`, `download`, `db`, `peer`, `daemon`, `appstream`, `build`
  - `--debug solver` seul → niveau 3 implicite
  - `--debug all` → tous les domaines au niveau 3
  - API interne : `debug_log(domain, level, msg)` — ne loggue que si level ≤ niveau configuré pour ce domaine
- [ ] Uniformiser l'activation du debug dans tous les modules (remplacer les DEBUG_* bool par l'API centralisée)
- [ ] S'assurer que chaque domaine a des logs utiles à chaque niveau

### Cache (commandes manquantes)
- [ ] `urpm cache verify` : vérifier l'intégrité du cache (checksums, fichiers corrompus)
- [ ] `urpm cache optimize` : VACUUM de la base SQLite pour récupérer l'espace et améliorer les perfs
- [ ] `urpm cache stats` : statistiques détaillées (hit rate, taille, temps de requête moyen)
- Ref: spécification dans `doc/archives/urpm_modern_cli.md`

### Vérification et diagnostic
- [ ] `urpm verify [<package>]` : vérifier l'intégrité des paquets installés
  - Options : `--all`, `--dependencies`, `--files`, `--checksums`
  - Détecter dépendances cassées, fichiers manquants/modifiés
- [ ] `urpm doctor` : diagnostic complet du système avec suggestions de correction
  - Vérifie : dépendances cassées, orphelins, fichiers modifiés, config invalide, cache corrompu
  - Propose des commandes de résolution (`urpm autoremove`, `urpm update --all`, etc.)
- Ref: spécification dans `doc/archives/urpm_modern_cli.md`

### Historique (compléments)
- [ ] `urpm history redo <id>` : refaire une transaction précédemment annulée
  - Symétrique de `urpm history undo`
  - Ref: spécification dans `doc/archives/urpm_modern_cli.md`

### system-upgrade
- [ ] Updates préliminaires
- [ ] Phase download
- [ ] Phase apply (reboot ou online)
- [ ] Gestion conflits version majeure

### Test container (`urpm test`)
- [ ] Utiliser mkimage pour créer un conteneur de test avec vrai root
  - Les 18 tests `rootonly` (cpio chown) passeraient sans être root sur l'hôte
  - Profil YAML dédié (`test`) avec le minimum pour rpm + les données de test
  - Commande `urpm test` ou `pytest --container` pour lancer dans l'image
  - Résout le problème vboxsf (TestFileConflicts) et le problème chown en un coup

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

#### Bugs & manques critiques (retours utilisateurs 2026-03)

**Liste des mises à jour**
- [ ] Colonnes non triables : impossible de trier par nom, date, taille, etc.
- [ ] Pas de recherche/filtre dans la liste des updates (pattern matching)
- [ ] Pas de sélection/désélection globale des mises à jour (tout cocher / tout décocher)

**Résilience téléchargement**
- [x] RPM corrompu (signature invalide ou download partiel) bloque l'update mais le fichier reste en cache
  - L'utilisateur novice se retrouve coincé (il faut supprimer manuellement les fichiers)
  - En cas de fail d'install : supprimer ou déplacer les fichiers corrompus automatiquement
  - Au prochain essai, le re-téléchargement doit se faire naturellement

**Texte non copiable**
- [ ] Encore de nombreuses boîtes de dialogue et messages d'erreur avec texte non sélectionnable/copiable
  - Utiliser des QLabel avec `setTextInteractionFlags(Qt.TextSelectableByMouse)` ou des QTextEdit readonly

**Database locked**
- [ ] Erreurs "database is locked" quand rpmdrake tente d'accéder à la DB pendant un lock
  - Le programme devrait retry automatiquement (backoff) au lieu de planter
  - Voir aussi : `doc/TODO_MU_LOCK.md` pour le fix côté CLI/daemon

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
- [ ] Dialogue README post-install : rendre le texte redimensionnable (QTextEdit dans dialogue dédié, pas dans le message de succès)
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
- [x] Fix: reset `_last_net_sample` après batch de downloads

### Download stats & priorités serveurs
- [x] Persister le débit constaté par serveur (SQLite, EWMA α=0.3) entre les sessions
- [x] Trier dynamiquement les serveurs par débit mesuré (priority DESC, bandwidth_kbps DESC)
- [x] Adaptation in-session sans attendre la prochaine exécution (dict mémoire lock-protégé)
- [x] Failover automatique sur download et sync metadata (boucle sur serveurs classés)
- [x] `urpm server stats` : affichage débit, latence, taux de succès, dernière vérification
- [ ] Afficher stats par serveur/peer à la fin du téléchargement (résumé post-install)
- [ ] Déprioriser automatiquement les serveurs lents ou instables (seuil configurable)
- [ ] Réinitialiser les stats si le serveur redevient rapide (fenêtre glissante)

### Synthesis : support `@recommends@` (Recommends vs Suggests)
- [x] Supporter le tag `@recommends@` dans le parser synthesis
  - Actuellement genhdlist2 mappe les Recommends RPM vers `@suggests@` (format legacy)
  - libsolv n'installe automatiquement que `SOLVABLE_RECOMMENDS`, pas `SOLVABLE_SUGGESTS`
  - Conséquence : tous les Recommends sont silencieusement ignorés en production
  - Solution : supporter `@recommends@` (via upanier ou extension synthesis) et le mapper vers `SOLVABLE_RECOMMENDS`
  - Ref: analyse détaillée dans `doc/archives/TESTS_INSTALL_DEBUG.md` (test_auto_select_h)

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
- [x] BuildRequires parser (--buildrequires)

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
