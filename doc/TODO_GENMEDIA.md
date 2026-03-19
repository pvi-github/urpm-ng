# TODO — urpm genmedia (intégration de upanier + incrémental + AppStream)

## Contexte

Intégrer une réécriture de `genhdlist3` dans urpm-ng sous la forme d'une commande
`urpm genmedia` (ou similaire), avec trois objectifs majeurs :

1. Génération des métadonnées media au format Mageia/urpmi (rétrocompat totale)
2. Deltas incrémentaux numérotés pour accélérer les syncs côté client
3. AppStream de qualité (à la hauteur de Mint / Debian / Arch)

Point de départ : prototype `upanier.py` de papoteur-mga (plusieurs bugs bloquants
à corriger avant intégration — voir analyse séparée).

---

## 1. Génération media (base)

Réécriture Python de `genhdlist3` intégrée dans urpm-ng :
- `hdlist.cz` (gzip)
- `synthesis.hdlist.cz` (xz)
- `files.xml.lzma` (xz)
- `MD5SUM`
- Verrouillage du répertoire `media_info/` pendant la génération

**Questions ouvertes :**
- Quelle API publique exposer ? (`urpm genmedia <dir>` en CLI, + API Python interne)
- La classe `Pack` de upanier — à intégrer dans `urpm.core` ou réécrire from scratch ?

---

## 2. Cache d'indexation serveur (`media_index.db`)

### Problème

Réindexer 30 000 RPM à chaque modification est prohibitif. Il faut ne traiter
que ce qui a changé.

### Solution : DB SQLite dédiée côté serveur uniquement

Une base `media_index.db` distincte de `packages.db` (qui reste côté client),
stockant pour chaque RPM :

- `filename`, `mtime`, `size`, `sha256` — fingerprint pour détecter les changements
- Header extrait sérialisé (synthesis entry, hdlist entry, files, etc.)
- Index au moment de la dernière indexation de ce paquet

Lors d'une réindexation :
1. Scan du répertoire → comparaison des fingerprints
2. Seuls les RPM nouveaux / modifiés / supprimés sont traités
3. Les résultats sont mis en cache pour les réindexations suivantes

`media_index.db` est aussi la **source de vérité pour générer les deltas** : diff
entre l'état courant et l'état au dernier index → liste exacte des
ajouts/modifications/suppressions, sans avoir à comparer les fichiers full.

### Parallélisation

L'extraction des headers RPM est I/O + CPU (décompression). Traitement parallèle
via `concurrent.futures.ProcessPoolExecutor` (~6 workers) — pas de threads car le
module `rpm` C n'est pas thread-safe. Chaque worker traite un lot de RPM et renvoie
les headers. L'agrégation et l'écriture des fichiers finaux restent séquentielles.

---

## 3. Deltas incrémentaux

### Principe

Chaque réindexation incrémente un compteur global. Le client compare son index local
à l'index courant du miroir et ne télécharge que les deltas manquants.

### Fichiers côté serveur

```
media_info/
  index                       # numéro d'index courant (ex: 67)
  index_min                   # plus ancien delta disponible (ex: 45)
  hdlist.cz                   # full — pour primo-sync
  synthesis.hdlist.cz
  files.xml.lzma
  appstream.xml.gz            # full AppStream
  deltas/
    hdlist.62.cz              # delta : paquets ajoutés/modifiés à l'index 62
    hdlist.63.cz
    ...
    hdlist.67.cz
    synthesis.hdlist.62.cz
    ...
    files.xml.62.lzma
    ...
    appstream.62.xml.gz
    ...
    removed.62                # liste des noms de paquets supprimés à l'index 62
    removed.63
    ...
```

### Flux client

1. **Primo-sync** : téléchargement des fichiers full + mémorisation de `index` courant
2. **Sync suivante** :
   - Lire `index` et `index_min` distants
   - Si `local_index < index_min` → full sync (trop de retard, deltas prunés)
   - Sinon → télécharger et appliquer les deltas `local_index+1` … `index`
3. Mémoriser le nouvel `index` local

### Application des deltas (partie complexe)

Chaque delta contient les paquets **ajoutés ou modifiés** depuis l'index précédent.
Les suppressions sont encodées dans un fichier `removed.N` séparé (liste de noms,
un par ligne). Cas à gérer :

- **Ajout** : insertion dans DB locale (packages, provides, requires, files)
- **Mise à jour** (même nom, nouvelle version) : remplacement atomique, mise à jour
  de toutes les tables liées
- **Suppression** : retrait propre avec cascade sur provides, requires, files —
  vérifier les dépendances cassées résultantes
- **Renommage / split** : traité comme suppression + ajout ; les dépendances
  résultantes sont à la charge du résolveur

**Questions ouvertes :**
- Quelle granularité pour le pruning ? (nb de deltas max ? ancienneté en jours ?)
- Le delta `files.xml` est-il les entrées complètes des paquets modifiés, ou un
  diff XML structuré ?
- Gestion des arches multiples dans un même delta (x86_64 + i586 + noarch) ?

### Pruning

Un job de maintenance supprime les deltas antérieurs à `index_min` et met à jour
`index_min`. Si un client est trop en retard → full sync automatique côté client.

### Décision de sécurité : pas de deltas SQL

L'idée de distribuer des deltas SQL compressés à rejouer côté client a été écartée :
en cas de compromission d'un miroir, des deltas SQL malveillants seraient rejoués
avec les droits urpm sur la DB locale — surface d'attaque inacceptable. Le format
binaire structuré (synthesis/hdlist) est parsé strictement sans exécution de code ;
un delta corrompu casse au pire la DB locale mais ne peut pas exécuter de code
arbitraire. La signature GPG des deltas (même schéma que les RPM) reste la
protection principale.

---

## 4. AppStream

### Situation actuelle

- ~927 paquets sur ~30 000 ont des métadonnées XML dans `/usr/share/metainfo/`
- Les ~29 000 restants n'ont rien → sources complémentaires nécessaires

### Sources de données (par priorité)

1. **Métadonnées embarquées** (`/usr/share/metainfo/*.xml`) — source primaire,
   927 paquets, qualité maximale
2. **Flathub AppStream** — bonne couverture des apps desktop courantes
3. **Catalogues upstream GNOME / KDE** — pour leurs écosystèmes respectifs
4. **Synthèse depuis headers RPM** — fallback universel : générer un composant
   minimal depuis `Name`, `Summary`, `Description`, `Group`, `URL`, icône extraite
   du RPM si présente
5. **Wikidata** pour enrichissement (descriptions longues, screenshots) — à évaluer

### Objectif qualité

- Screenshots
- Descriptions longues (pas juste le Summary RPM)
- Ratings / reviews (ODRS ou équivalent)
- Content-rating OARS
- Composants non-desktop : fonts, codecs, input-methods, CLI tools
- Validation `appstreamcli validate` sans erreurs ni warnings
- Même schéma incrémental que hdlist/synthesis (deltas AppStream numérotés)

### Cache des métadonnées upstream

Stocké dans `media_index.db` (côté serveur uniquement) pour éviter de refetcher
les sources upstream à chaque réindexation. Mise à jour périodique découplée de
la réindexation RPM.

**Questions ouvertes :**
- Quelle stratégie de fallback pour les 29 000 paquets sans métadonnées ? Générer
  un composant AppStream minimal pour tous, ou seulement pour les paquets avec une
  entrée `.desktop` ?
- Fréquence de mise à jour des sources upstream vs réindexation locale ?
- Format de sortie : `appstream.xml.gz` à la Debian, ou catalogue par composant ?

---

## Architecture serveur — vue d'ensemble

```
Répertoire RPM
    │
    ▼
[Scan fingerprints]
    │  (seuls les RPM nouveaux/modifiés/supprimés)
    ▼
[ProcessPoolExecutor — ~6 workers]
    │  extraction headers RPM en parallèle
    ▼
[media_index.db]  ←──────────────────────────────┐
    │  cache fingerprints + headers sérialisés    │
    │  source de vérité pour les deltas           │ mise à jour
    ▼                                             │
[Génération deltas]  ──────────────────────────── ┘
    │  diff état courant vs dernier index
    │  → hdlist.N.cz, synthesis.N.cz, files.N.lzma, removed.N
    ▼
[Génération fichiers full]
    │  hdlist.cz, synthesis.hdlist.cz, files.xml.lzma
    ▼
[AppStream]
    │  métadonnées embarquées + sources upstream + fallback RPM headers
    │  → appstream.xml.gz + appstream.N.xml.gz
    ▼
[Incrément index + pruning]
    │  index++, suppression deltas < index_min
    ▼
media_info/  (atomique — lock UPDATING pendant toute l'opération)
```

---

## Ordre d'implémentation suggéré

```
Étape 1 : urpm genmedia de base
          (port de upanier, bugs corrigés, sans incrémental)
    ↓
Étape 2 : cache media_index.db + fingerprints + workers parallèles
    ↓
Étape 3 : index incrémental + génération des deltas côté serveur
    ↓
Étape 4 : consommation des deltas côté client (urpm sync)
    ↓
Étape 5 : AppStream — extraction métadonnées embarquées
    ↓
Étape 6 : AppStream — sources complémentaires + fallback RPM headers
    ↓
Étape 7 : AppStream incrémental (mêmes deltas)
    ↓
Étape 8 : pruning automatique + monitoring
```
