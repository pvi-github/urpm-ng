# Architecture de urpmi (système legacy Perl)

## Vue d'ensemble

urpmi est le gestionnaire de paquets de Mageia/Mandriva, écrit en Perl avec des bindings C (XS) vers librpm.

### Dépôts sources

- **urpmi** : http://gitweb.mageia.org/software/rpm/urpmi/
- **perl-URPM** : http://gitweb.mageia.org/software/rpm/perl-URPM/
- **rpmtools** : http://gitweb.mageia.org/software/rpm/rpmtools/
- Miroirs GitHub : https://github.com/shlomif/urpmi, https://github.com/OpenMandrivaSoftware/rpmtools

---

## Architecture des composants

```
┌─────────────────────────────────────────────────────────────────────┐
│                        EXÉCUTABLES CLI                               │
├─────────────┬─────────────┬─────────────┬─────────────┬─────────────┤
│   urpmi     │   urpme     │   urpmq     │   urpmf     │ urpmi.add   │
│ (install)   │ (remove)    │ (query)     │ (files)     │ media etc.  │
└──────┬──────┴──────┬──────┴──────┬──────┴──────┬──────┴──────┬──────┘
       │             │             │             │             │
       └─────────────┴─────────────┼─────────────┴─────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────┐
│                          urpm.pm                                     │
│  Classe principale - hérite de URPM et Exporter                      │
│  - Initialisation système (chemins, config)                          │
│  - Gestion des erreurs fatales/logs                                  │
│  - Enregistrement des RPM                                            │
└──────────────────────────────────┬──────────────────────────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────┐
│                      MODULES urpm/*.pm                               │
├─────────────────┬─────────────────┬─────────────────┬───────────────┤
│ urpm/media.pm   │ urpm/select.pm  │ urpm/install.pm │ urpm/cfg.pm   │
│ Gestion médias  │ Sélection pkgs  │ Transactions    │ Configuration │
├─────────────────┼─────────────────┼─────────────────┼───────────────┤
│ urpm/download.pm│ urpm/mirrors.pm │ urpm/orphans.pm │ urpm/args.pm  │
│ Téléchargements │ Sélection miroir│ Paquets orphel. │ Arguments CLI │
├─────────────────┼─────────────────┼─────────────────┼───────────────┤
│ urpm/signature  │ urpm/lock.pm    │ urpm/cdrom.pm   │ urpm/util.pm  │
│ Vérif. GPG      │ Verrous fichier │ Support CD-ROM  │ Utilitaires   │
└─────────────────┴─────────────────┴─────────────────┴───────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────┐
│                    perl-URPM (Bindings XS)                           │
├─────────────────────────────────────────────────────────────────────┤
│ URPM.pm (25 Ko)           │ URPM.xs (95 Ko)                         │
│ - API Perl haut niveau    │ - Bindings C vers librpm                │
│ - parse_synthesis()       │ - rpmReadPackageFile()                  │
│ - parse_hdlist()          │ - headerGet()                           │
│ - search()                │ - Transaction management                │
├───────────────────────────┼─────────────────────────────────────────┤
│ URPM/Resolve.pm (79 Ko)   │ URPM/Build.pm (18 Ko)                   │
│ - Résolution dépendances  │ - Génération synthesis/hdlist           │
│ - Gestion conflits        │ - build_synthesis()                     │
│ - Backtracking            │ - build_info()                          │
└───────────────────────────┴─────────────────────────────────────────┘
                                   │
┌──────────────────────────────────▼──────────────────────────────────┐
│                          librpm (C)                                  │
│  Bibliothèque système RPM - gestion headers, transactions, DB        │
└─────────────────────────────────────────────────────────────────────┘
```

---

## Format des fichiers de métadonnées

### synthesis.hdlist.cz

Fichier texte compressé (zstd, xz, gzip, bzip2) contenant les métadonnées légères.

**Structure par paquet** (lignes @ précèdent @info du paquet) :
```
@provides@capability1@capability2[version]@...
@requires@dep1@dep2[>= version]@...
@conflicts@conflict1@...
@obsoletes@obsolete1@...
@suggests@suggest1@...
@summary@Description courte du paquet
@info@name-version-release.arch@epoch@size@group
```

**Exemple réel** :
```
@provides@firefox@firefox(x86-64)@webclient
@requires@libgtk-3.so.0()(64bit)@libglib-2.0.so.0()(64bit)@mozilla-nss >= 3.90
@conflicts@mozilla-firefox
@obsoletes@mozilla-firefox < 120
@summary@Mozilla Firefox Web Browser
@info@firefox-120.0-1.mga9.x86_64@0@156834567@Networking/WWW
```

**Parsing** : `@` est le séparateur de champs. Les tags sont lus séquentiellement et associés au `@info` qui suit.

### hdlist.cz (VALIDÉ avec parse_hdlist.py)

Archive zstd contenant les **headers RPM complets** (binaires) concaténés.

**Structure d'un header RPM :**
```
Magic (3 bytes): 0x8E 0xAD 0xE8
Version (1 byte): 0x01
Reserved (4 bytes): 0x00...
nindex (4 bytes, big-endian): nombre d'entrées index
hsize (4 bytes, big-endian): taille du data store
Index entries (nindex × 16 bytes): tag, type, offset, count
Data store (hsize bytes): valeurs (strings null-terminated, etc.)
```

- Contient TOUTES les métadonnées (changelog, filelists, scripts...)
- Beaucoup plus volumineux que synthesis
- Nécessaire pour certaines opérations (liste fichiers, changelog)

**Génération** : `genhdlist2` (rpmtools) extrait les headers des RPM et les empaquète.

---

## Flux de données

### 1. Configuration des médias

```
urpm.addmedia "nom" "url"
       │
       ▼
┌─────────────────────────────────────────┐
│ urpm/cfg.pm : Parse /etc/urpmi/urpmi.cfg│
│ Format bloc :                           │
│   nom url {                             │
│     update                              │
│     mirrorlist=...                      │
│   }                                     │
└────────────────────┬────────────────────┘
                     ▼
┌─────────────────────────────────────────┐
│ urpm/media.pm : configure()             │
│ - Charge les médias actifs              │
│ - Filtre par excludemedia/media         │
│ - Prépare les URLs                      │
└────────────────────┬────────────────────┘
                     ▼
┌─────────────────────────────────────────┐
│ urpm/mirrors.pm : pick_one()            │
│ - Télécharge mirrorlist si besoin       │
│ - Cache 24h avec invalidation           │
│ - Tri par proximité géographique        │
└─────────────────────────────────────────┘
```

### 2. Mise à jour des métadonnées

```
urpm.update / urpmi.update
       │
       ▼
┌─────────────────────────────────────────┐
│ urpm/media.pm : update_media()          │
│ - Vérifie MD5SUM du media_info          │
│ - Compare avec cache local              │
└────────────────────┬────────────────────┘
                     │
       ┌─────────────┴─────────────┐
       ▼                           ▼
┌──────────────────┐      ┌──────────────────┐
│ Média local      │      │ Média distant    │
│ copy_and_own()   │      │ _download_media_ │
│                  │      │ info_file()      │
└────────┬─────────┘      └────────┬─────────┘
         │                         │
         └───────────┬─────────────┘
                     ▼
┌─────────────────────────────────────────┐
│ _parse_synthesis()                      │
│ → URPM::parse_synthesis__XS()           │
│ - Décompresse (auto-détection format)   │
│ - Parse ligne par ligne                 │
│ - Remplit depslist[]                    │
│ - Indexe provides{} et obsoletes{}      │
└─────────────────────────────────────────┘
```

### 3. Installation de paquets

```
urpmi firefox
       │
       ▼
┌─────────────────────────────────────────┐
│ urpm/select.pm : search_packages()      │
│ - Recherche exacte par provides         │
│ - Fallback fuzzy si pas trouvé          │
│ - Filtre par architecture               │
└────────────────────┬────────────────────┘
                     ▼
┌─────────────────────────────────────────┐
│ URPM::Resolve : resolve_requested()     │
│ - unsatisfied_requires() pour chaque pkg│
│ - find_required_package() pour deps     │
│ - _handle_conflicts() si conflits       │
│ - backtrack_selected() si impasse       │
│ - Scoring candidats (arch, locale, etc.)│
└────────────────────┬────────────────────┘
                     ▼
┌─────────────────────────────────────────┐
│ urpm/download.pm                        │
│ - curl/wget/aria2/prozilla              │
│ - Support parallèle (aria2)             │
│ - Metalink pour multi-miroirs           │
│ - Resume des téléchargements            │
└────────────────────┬────────────────────┘
                     ▼
┌─────────────────────────────────────────┐
│ urpm/install.pm                         │
│ - Crée URPM::Transaction                │
│ - Schedule packages (add/remove)        │
│ - trans->check() validation             │
│ - trans->run() exécution                │
│ - Callbacks progress/erreurs            │
└─────────────────────────────────────────┘
```

---

## Résolution des dépendances (URPM::Resolve)

### Algorithme principal

```
resolve_requested(urpm, state, requested, options)
│
├─► Pour chaque paquet demandé :
│   │
│   ├─► unsatisfied_requires(pkg)
│   │   - Vérifie cache installed
│   │   - Vérifie paquets sélectionnés
│   │   - Vérifie base RPM
│   │
│   ├─► Si dépendance non satisfaite :
│   │   │
│   │   └─► find_required_package(capability)
│   │       - Score candidats :
│   │         • requested > non-requested
│   │         • upgrade > install
│   │         • same-arch > noarch
│   │         • locale-match bonus
│   │       - Sélectionne meilleur score
│   │
│   └─► _handle_conflicts()
│       - Détecte conflits avec sélection
│       - Tente upgrade du conflit
│       - Sinon backtrack
│
├─► backtrack_selected() si impasse
│   - Désélectionne paquet problématique
│   - Essaie alternatives
│   - Limite tentatives (évite boucles)
│
└─► build_transaction_set()
    - Tri topologique par dépendances
    - Détection cycles
    - Ordre d'installation
```

### Structures de données

```perl
$state = {
    selected => {
        pkg_id => {
            from => [dep_id, ...],  # Qui l'a requis
            promote => bool,        # Promu pour résoudre
            requested => bool,      # Demandé explicitement
        }
    },
    rejected => {
        pkg_id => {
            closure => { reason => "..." },
            backtrack => { ... }
        }
    },
    whatrequires => {
        capability => [pkg_id, ...]
    }
};
```

---

## Configuration (urpm/cfg.pm)

### Fichier /etc/urpmi/urpmi.cfg

```
# Bloc global
{
  verify-rpm: yes
  auto: no
  limit-rate: 500k
  downloader: aria2
  retry: 3
  priority-upgrade: rpm urpmi perl-URPM
}

# Médias
main http://mirror.example.com/main {
  update
}

updates http://mirror.example.com/updates {
  update
  mirrorlist: http://mirrors.example.com/updates.list
}

contrib http://mirror.example.com/contrib {
  ignore  # désactivé
}
```

### Options supportées

| Catégorie | Options |
|-----------|---------|
| **Flags** | update, ignore, synthesis, noreconfigure, no-recommends, static, virtual |
| **Valeurs** | hdlist, with_synthesis, mirrorlist, limit-rate, xml-info, excludepath, priority-upgrade |
| **Booléens** | verify-rpm, fuzzy, allow-force, allow-nodeps, pre-clean, post-clean, keep, auto |
| **Downloaders** | curl-options, wget-options, aria2-options, rsync-options |

---

## Téléchargement (urpm/download.pm)

### Protocols supportés

| Protocol | Downloader | Particularités |
|----------|-----------|----------------|
| HTTP/HTTPS | curl, wget, aria2 | Proxy, auth, resume |
| FTP | curl, wget, aria2 | Timestamp check |
| RSYNC | rsync | Sync incrémental |
| SSH | rsync over ssh | Connexion persistante |
| FILE | cp | Local seulement |

### Fonctionnalités avancées

- **Parallélisme** : aria2 avec `--split=3`
- **Resume** : `--continue` (curl/wget)
- **Metalink** : Construction dynamique pour aria2, 8 miroirs max, scoring préférence
- **Rate limiting** : Configurable globalement
- **Retry** : Configurable, blacklist des miroirs en échec

---

## Points d'attention pour urpm-ng

### Ce qu'on doit répliquer

1. **Format synthesis** : Parser le format @ exactement comme l'original
2. **Résolution dépendances** : Algorithme similaire avec backtracking
3. **Configuration** : Même format urpmi.cfg pour migration transparente
4. **Téléchargement** : Support multi-protocoles, resume, parallélisme

### Ce qu'on peut améliorer

1. **Cache SQLite** : Déjà planifié, remplace le parsing répété
2. **Deltas** : Système de différentiels pour updates incrémentaux
3. **Performance** :
   - C++ pour résolution deps (vs Perl interprété)
   - Queries SQLite indexées (vs parcours linéaire)
4. **Architecture** : Module Python propre vs modules Perl entremêlés

### Risques / Complexité

1. **Parsing hdlist** : Headers RPM binaires, nécessite librpm ou reverse-engineering
2. **Compatibilité librpm** : Versions 4.9 à 4.17 supportées dans l'original
3. **Edge cases** : Conflits circulaires, provides virtuels, epochs

---

## Références

- Wiki Mageia URPMI : https://wiki.mageia.org/en/URPMI
- genhdlist2 : https://metacpan.org/dist/rpmtools/view/genhdlist2
- perl-URPM CPAN : Distribué via CPAN pour les tests
- Blog Mageia urpmi : https://blog.mageia.org/en/2020/08/28/news-from-our-package-manager-urpmi/
