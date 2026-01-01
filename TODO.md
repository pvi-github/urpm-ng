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


### Bootstrap
- [ ] Quand python3-solv ou python3-zstandard manquent, proposer installation via DL direct ou urpmi

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
- [ ] Pages man
- [ ] Guide migration urpmi → urpm

### --downloadonly / download
- [x] Option `--download-only` sur install
- [ ] Option `--download-only` sur upgrade
- [ ] Option `--destdir <path>` pour spécifier répertoire destination (compatibilité urpmi)
- [ ] Commande `urpm download <pkg>`

### builddep
- [ ] Parser BuildRequires du SRPM/.spec
- [ ] `urpm builddep <pkg.spec>`

### Parsing hdlist.cz
- [ ] Liste des fichiers contenus
- [ ] Description longue, changelog, scripts

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
