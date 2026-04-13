# urpm-ng -- Roadmap

urpm-ng est le gestionnaire de paquets moderne de Mageia Linux.
Construit sur libsolv (le moteur de resolution utilise par les plus grandes distributions),
il offre un CLI complet, un daemon reseau avec partage P2P en LAN, et une
integration native avec les logithèques graphiques. Le code est ecrit en Python,
concu pour être lisible, performant et facile a auditer -- parce qu'un gestionnaire
de paquets est un composant critique qui merite un code exemplaire.

---

## Livre (Shipped)

### Gestion de paquets

- **install / erase / upgrade / reinstall** -- operations completes avec resolution de dependances
- **autoremove** -- nettoyage orphelins, vieux kernels, faildeps, builddeps (`--orphans`, `--kernels`, `--faildeps`, `--buildrequires`, `--all`)
- **blacklist / redlist** -- protection a deux niveaux contre les suppressions et installations non desirees
- **hold / unhold** -- verrouillage de paquets avec raison, protection contre upgrade et obsoletes
- **mark manual / auto** -- contrôle explicite du statut d'installation
- **--download-only** -- telecharger sans installer (install et upgrade)
- **download** -- recuperer un RPM sans l'installer
- **--config-policy** -- gestion des fichiers de configuration (.rpmnew) : keep, replace, ask
- **--allow-arch** -- installation multi-architecture (i686 pour wine/steam sur x86_64)
- **--force / --test / --auto** -- mode force, simulation, non-interactif
- **cleandeps** -- detection et suppression des dependances orphelines

### Resolveur

- **libsolv** -- moteur de resolution de reference, performant et eprouve
- **Weak deps** -- support complet Recommends, Suggests, Supplements, Enhances
- **Alternatives (OR-deps)** -- choix interactif ou automatique entre alternatives
- **--prefer** -- guidage de la resolution avec contraintes de version (`--prefer=php:8.4,apache,-mod_php`)
- **Familles versionnees** -- detection automatique php8.4/php8.5, preference pour la famille installee
- **Resolution de conflits** -- gestion automatique des conflits et providers
- **Obsoletes** -- detection et traitement lors des upgrades
- **TransactionQueue** -- install + erase atomiques dans une seule transaction

### Recherche et requêtes

- **search** -- recherche plein texte FTS5 dans noms, summaries et provides
- **show / info** -- informations detaillees avec Recommends, Suggests, Conflicts, Obsoletes
- **list** -- installed, available, updates, upgradable
- **whatprovides** -- quel paquet fournit une capability ou un fichier
- **depends / rdepends** -- arbre de dependances directes et inverses (`--tree`, `--depth`, `--hide-uninstalled`)
- **why** -- explication de la presence d'un paquet sur le systeme
- **recommends / suggests / whatrecommends / whatsuggests** -- exploration des dependances faibles
- **find** -- recherche dans les fichiers de tous les paquets (installes + disponibles) via files.xml indexe
- **--unavailable** -- liste des paquets installes absents des media configures

### Historique et rollback

- **history** -- liste des transactions avec details
- **undo** -- annulation d'une transaction specifique
- **rollback** -- retour a un etat anterieur (par nombre, ID ou date)
- **history --delete** -- suppression d'entrees d'historique
- **hold avec raison** -- verrouillage documente des paquets

### Medias et serveurs

- **media add / remove / list / update / enable / disable** -- gestion complete des sources
- **media import** -- import depuis urpmi.cfg existant
- **media autoconfig** -- ajout automatique des media officiels Mageia par release
- **media discover** -- decouverte automatique depuis media.cfg d'un depot
- **media link** -- association serveurs / medias avec verification MD5
- **media set** -- configuration sharing, replication, quota, retention, priorite, sync-files
- **server add / remove / list / enable / disable** -- gestion des miroirs
- **server stats** -- statistiques EWMA (debit, latence, taux de succes)
- **server autoconfig** -- mesure de latence et configuration automatique des miroirs
- **server test / ip-mode** -- test de connectivite et detection IPv4/IPv6/dual
- **Failover intelligent** -- basculement automatique vers le meilleur serveur en cas d'echec

### Reseau et P2P

- **Telechargements paralleles multi-serveurs** -- utilisation simultanee de plusieurs miroirs
- **P2P LAN** -- decouverte automatique par UDP broadcast, partage de paquets entre machines du reseau
- **Pre-telechargement idle** -- le daemon telecharge les mises a jour quand le systeme est inactif
- **Seed-based replication** -- miroir local partiel base sur rpmsrate (contenu type DVD, ~5 Go)
- **--only-peers** -- mode air-gapped, telechargement exclusivement depuis les pairs LAN
- **Priorite dynamique** -- tri des serveurs en temps reel par debit mesure

### Securite

- **Verification GPG** -- activee par defaut sur toutes les installations
- **urpm key list / import / remove** -- gestion complete des cles
- **Auto-import** -- import automatique des cles lors de l'ajout de media
- **PolicyKit** -- authentification privilegiee pour les operations D-Bus

### Construction de paquets

- **mkimage** -- creation d'images conteneur (Docker/Podman) pour build isole
- **build** -- construction de RPMs dans un conteneur avec workspace standard
- **Profils YAML** -- build, ci, minimal (systeme et personnalises)
- **--with-rpms** -- pre-installation de RPMs locaux dans le conteneur
- **--buildrequires** -- installation des dependances de build depuis spec/SRPM avec tracking
- **autoremove --buildrequires** -- nettoyage des dependances de build trackees
- **Builds paralleles** -- `--parallel N` pour constructions simultanees

### Daemon (urpmd)

- **Scheduler idle-aware** -- execution des tâches en arriere-plan quand CPU et reseau sont libres
- **Sync metadata** -- rafraichissement automatique des media
- **Pre-telechargement** -- telechargement anticipe des mises a jour disponibles
- **HTTP API** -- endpoints REST : ping, status, media, available, updates, peers, have, announce
- **ThreadingHTTPServer** -- requêtes paralleles pour servir les pairs

### Integrations

- **PackageKit D-Bus** -- backend complet pour Discover et GNOME Software (search, install, remove, updates)
- **Service D-Bus** -- `org.mageia.Urpm.v1` avec operations privilegiees
- **AppStream** -- generation de catalogue par media avec fusion, support logithèques graphiques
- **Import urpmi.cfg** -- migration transparente depuis urpmi
- **Bash completion** -- completion automatique pour toutes les commandes
- **Pages man** -- documentation EN et FR

### Performance

- **Demarrage ~0.35s** -- 4x plus rapide que les gestionnaires comparables, grâce a l'utilisation native de libsolv
- **Indexation FTS5** -- recherche quasi-instantanee dans la base de paquets
- **Cache intelligent** -- quotas, eviction, retention configurable par media
- **Adaptation temps reel** -- statistiques serveurs en memoire, pas d'attente entre sessions

---

## En cours (In Progress)

- **rpmdrake-ng** -- interface graphique Qt6 native pour la gestion de paquets (architecture MVC, premiers ecrans fonctionnels)
- **Stabilisation resolveur** -- derniers edge cases sur conflits, providers et alternatives complexes

---

## Planifie -- Priorite haute

- **system-upgrade** -- upgrade de version majeure Mageia (9 -> 10) avec telechargement prealable et application au reboot
- **groups (rpmsrate)** -- `urpm group list/info/install/remove` pour installer des environnements complets
- **Configuration daemon** -- `/etc/urpm/daemon.conf` avec log, scheduling, reload a chaud (SIGHUP)
- **Contrôle serveurs** -- politique d'auto-ajout, filtrage geographique, depriorisation automatique des serveurs lents
- **Documentation** -- guide de migration urpmi -> urpm, CONTRIBUTING.md, revue de coherence code/docs

---

## Planifie -- Priorite moyenne

- **Internationalisation CLI** -- audit des chaînes non wrappees, completion des fichiers .po (fr, de, es, nl, pt)
- **Systeme de debug par domaine** -- `--debug solver=3,download=5,db=1` avec niveaux de verbosite
- **offline-upgrade** -- telechargement anticipe puis application au reboot
- **distro-sync** -- realignement sur les versions exactes du depot
- **Test container** -- `urpm test` pour executer les tests dans un conteneur isole
- **Explications upgrade/remove** -- traçage des chaînes de dependances pour expliquer les suppressions
- **Wrappers legacy** -- compatibilite urpmi/urpme/urpmq/urpmf avec mapping d'options

---

## Horizon

- **mgaonline-ng** -- applet systray pour notification des mises a jour disponibles
- **Advisories MGASA/CVE** -- parsing des avis de securite, filtres `--security` et `--cve`
- **Gestion de parc** -- inventaire et deploiement centralise pour flottes de machines
- **Distribution IPFS** -- transport decentralise pour les paquets

---

## Contribuer

urpm-ng est un projet ouvert. Toute contribution est la bienvenue : code, tests,
traduction, retours d'experience, signalement de bugs ou de coquilles dans la
documentation -- il n'y a pas de contribution trop petite.

- **Code source** : [github.com/pvi-github/urpm-ng](https://github.com/pvi-github/urpm-ng)
- **Conventions de developpement** : voir `CLAUDE.md` a la racine du projet
- **Architecture technique** : voir `doc/ARCHITECTURE.md`
- **Tests** : voir `doc/TESTING.md`

Le projet est sous licence GPL-3.0.
