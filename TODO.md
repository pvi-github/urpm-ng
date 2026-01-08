# urpm-ng TODO

> Voir [doc/ROADMAP.md](doc/ROADMAP.md) pour la vision et les priorités.
> Voir [doc/ARCHITECTURE.md](doc/ARCHITECTURE.md) pour les décisions techniques.
> Voir [CHANGELOG.md](CHANGELOG.md) pour l'historique des fonctionnalités.

---

## En cours

### Clarification update/upgrade (style apt)
Revoir la répartition des responsabilités :
- `urpm update` = mise à jour métadonnées (media update, sync DB)
- `urpm upgrade` = mise à jour des paquets
- `urpm distupgrade` (futur) = mise à jour majeure du système

Aujourd'hui c'est confus : update fait les deux selon les arguments.

### Alternatives (OR deps)
- [ ] Tests intensifs install avec alternatives
- [ ] Valider mode --auto
- [ ] Valider re-résolution après choix utilisateur

---

## Phase 1 : Developper & community features

### Bash autocompletion (prioritaire)
- [x] Script completion pour commandes/sous-commandes
- [x] Completion des noms de paquets (installés et disponibles)
- [x] Completion des noms de médias/serveurs
- [x] Installation via `/etc/bash_completion.d/urpm`

### Installation RPM local
- [x] `urpm install /chemin/vers/paquet.rpm`
- [x] Vérification signature, alerte + confirmation si non signé
- [x] Résolution des dépendances depuis les médias configurés

### needs-restarting
- [ ] Détecter reboot nécessaire (kernel, glibc, systemd)
- [ ] Lister services à redémarrer

### daemon config
- [ ] Fichier `/etc/urpm/daemon.conf`
- [ ] Options: `log_destination` (syslog|file), `log_file`, `log_level`
- [ ] Intégrer config auto-install (scheduling, policies)

### Documentation EN/FR
- [x] Pages man (urpm.1, urpmd.8 en EN et FR)
- [ ] Revue complète : cohérence code/docs (chemins, options, comportements)
- [ ] Initier un guide de migration urpmi → urpm (EN, FR)

### --downloadonly / download
- [x] Option `--download-only` sur install
- [ ] Option `--download-only` sur upgrade
- [ ] Option `--destdir <path>` pour spécifier répertoire destination (compatibilité urpmi)
- [ ] Commande `urpm download <pkg>`

### builddep
- [ ] Parser BuildRequires du SRPM/.spec
- [ ] `urpm builddep <pkg.spec>`
- [ ] `urpm builddep <pkg.src.rpm>`
- [ ] `urpm builddep` (dans une arborescence de travail RPM)

### Parsing hdlist.cz
- [ ] Vérifier si le DL des hdlists.cz est encore nécessaire. Parce que quand on fait un urpmf c'est un fichier media_info/files.xml.lzma qui est récupéré et analysé.
- [ ] Liste des fichiers contenus <-- ça c'est dans le media_info/files.xml.lzma
- [ ] Description longue, changelog, scripts <-- ça c'est peut être dans les hdlist.cz

### Recherche fichiers (urpmf)
- [ ] Chercher dans paquets disponibles (pas seulement installés)
- [ ] Support patterns/regex

---

## Phase 2 : tricky/huge features

### Internationalisation EN/FR
- [ ] gettext
- [ ] Fichiers .po/.mo (fr, en)
- [ ] Traduction de la documentation (man pages & guides)

### system-upgrade
- [ ] Updates préliminaires
- [ ] Phase download
- [ ] Phase apply (reboot ou online) en une fois ou deux fois
- [ ] Gestion conflits version majeure

### --rootdir ou --bootstrap
- [ ] il faut que urpm soit capable de faire un bootstrap et donc faire comme urpmi :
      - [ ] urpmi.addmedia --distrib --mirrorlist 'https://mirrors.mageia.org/api/mageia.cauldron.x86_64.list' --urpmi-root /tmp/mageia-rootfs
      - [ ] urpmi basesystem-minimal urpmi locales locales-en bash --auto --no-suggests --no-recommends --urpmi-root /tmp/mageia-rootfs --root /tmp/mageia-rootfs

Il y a aussi le rpm mageia nommé rpmbootstrap qui est à regarder.

Le principe est de pouvoir créer dans un répretoire donné toute l'arborescence nécessaire à la préparation d'un chroot ou d'une base d'image docker, donc :
1. arborescence minimale
2. initilalisation d'une base urpm (bonne archi, bonne version)
3. installation du basesystem minimal (rpm, glibc, kerneli, bash, locales  de base, tzdata, vim, urpm, etc)

Je ne sais pas si c'est bien d'aller jusqu'à générer l'image docker (entrer dans le chroot, faire l'urpm m u et urpm u, vidanger le cache urpm, effacer /etc/syconfig/network, rebuilder la rmpdb peut être ? sortir du chroot faire le dockerfile, le tar de l'arborescence fraichement créée, et le docker build...

L'idée c'est que rapidement urpm puisse aussi tester des installs et builder des rpm en environnement tout neuf... donc urpm build-image|bi --option bla bla mga9 x86_64 et urpm rebuild|rb unfichier.src.rpm mga9 x86_64 ou urpm build|b mga9 x86_64 (dans une arborescence de travail RPM avec SOURCES et SPECS)
Ça instanciera un conteneur à partir de l'image de ref, avec un répretoire "partagé" pour la persistance, ça fera le urpm builddep et ensuite le bm/rpmbuild
Et ça pourra aussi instancier un autre conteneur vierge pour tester le urpm i du fichier et valider que ça s'installe sans souci.
Attention que le urpm dans son conteneur devra pouvoir tirer partie des peers dispos (et peut être même qu'il faudra prévoir un download prédictif sur la machine locale de façon à disposer au moins d'un peer avec toutes les dépendances nécessaires...

Et à la fin l'idée c'est que ça puisse même être effectué par urpmd, piloté via les API par un ordonnanceur pour faire du rebuild de masse parallélisé.

### Split ?

- [ ] Prévoir de séparer en plusieurs rpms : urpm, urpmd, urpmb (b pour build), urpms (s pour scedule)

---

## Phase 3 : GUI & applet

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

## Phase 4 : Consolidation & stabilisation

### fonctions diverses
- [ ] offline-upgrade
- [ ] distro-sync
- [ ] swap (remove+install combiné)
- [ ] check intégrité BDD
- [ ] debuginfo-install


### Compatibilité legacy
- [ ] Wrappers urpmi/urpme/urpmq/urpmf avec mapping options

---

## Améliorations continues & debug

### Internationalisation
- [ ] gettext
- [ ] Fichiers .po/.mo (de, es, it, pt_BR, ru)
- [ ] Pages man (de, es, it, pt_BR, ru)
- [ ] Guide migration urpmi → urpm (de, es, it, pt_BR, ru)

### Quotas et cache
- [ ] Implémenter CacheManager avec quotas par média et global
- [ ] Éviction intelligente (non-référencés d'abord, puis score obsolescence)

### Idle detection
- [ ] Fix: reset `_last_net_sample` après batch de downloads (évite pause sur propre trafic)

### Download stats & priorités serveurs
- [ ] Afficher stats par serveur/peer à la fin (volume, paquets, débit moyen)
- [ ] Tracker les performances des serveurs sur la durée
- [ ] Prioriser dynamiquement les serveurs les plus rapides
- [ ] Tester IPv4/IPv6 en arrière-plan (pendant downloads prédictifs) et choisir le plus rapide pour dual

### Explications upgrade/remove
- [ ] Expliquer POURQUOI un paquet est supprimé (comme urpmi: "en raison du manque de...")
- [ ] Tracer les chaînes de dépendances pendant la résolution

### Refonte why/rdepends

Mutualiser le code et améliorer les deux commandes.

**Problème actuel :**
- `why` et `rdepends` ont du code dupliqué
- Performance médiocre (parcours répétés)
- Gestion des "cassures" de chaîne complexe

**Cassure = chaîne incomplète**

Exemple : logiciel X requiert "son" (pulseaudio OU pipewire)
```
X (explicite) → son → pulseaudio (installé) ✓ chaîne OK
                    → pipewire (PAS installé) ✗ cassure
                          └── pipewire-libs (installé)

Y (explicite) → pipewire-libs ✓ chaîne OK directe
```

`why pipewire-libs` doit répondre "Y" (chaîne valide), pas "orphelin"
juste parce que la branche pipewire est cassée. pipewire-libs peut
être requis par autre chose via une chaîne complète.

**Algo requis :**
- Explorer TOUTES les chaînes remontantes
- Éliminer celles avec cassure (paquet manquant)
- Garder celles qui arrivent à un paquet explicite
- Orphelin = aucune chaîne valide

**Options de sortie rdepends :**
- [ ] `--json` (exploitable par why ou outils tiers)
- [ ] `--tree` (affichage hiérarchique)
- [ ] `--recursive` (avec déduplication)

**Mutualisation :**
- [ ] `why` exploite le même graphe que `rdepends`
- [ ] Code partagé pour construction du graphe

### whatprovides
- [ ] Contraintes de version (== < <= > >=) pour filtrer

---

## Phase différée (entreprise)

- [ ] Infrastructure advisories (MGASA)
- [ ] versionlock
- [ ] downgrade
- [ ] APIs sécurisées (/api/upgrade, /api/install)
- [ ] Gestion de parc (inventaire, déploiement)
- [ ] Console de gestion centralisée

---
