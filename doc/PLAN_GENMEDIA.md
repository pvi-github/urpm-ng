# TODO — urpm genmedia (intégration de upanier + incrémental + AppStream)

> **Note** : ce document est le **plan initial** de l'intégration genmedia
> (mars 2026).  Pour le statut courant des travaux, la liste des bugs
> résolus, et les actions en cours, voir [`TODO_GENMEDIA.md`](TODO_GENMEDIA.md).

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
- `info.xml.lzma` (xz)
- `changelog.xml.lzma` (xz)
- `MD5SUM`
- Verrouillage du répertoire `media_info/` pendant la génération

**Questions ouvertes :**
- Quelle API publique exposer ? (`urpm genmedia <dir>` en CLI, + API Python interne)
- La classe `Pack` de upanier — à intégrer dans `urpm.core` ou réécrire from scratch ?

cmd_genmedia(
    rpms_dir: directory containing *.rpm files
    xml-info-filter: default=b".lzma:xz -7" : use FILTER to compress XML media info, default: .lzma:xz -7
    synthesis-filter: default=b".cz:xz -7" : use FILTER to compress synthesis.hdlist, default: .cz:xz -7")
    no-md5sum : do not generate MD5SUM
    no-hdlist : do not generate hdlist.cz
    allow-empty-media : allow empty media
    file-deps : use file_deps.lst file
    hdlist-filter: default=b".cz:gzip -9" : use FILTER to compress hdlist, default: .cz:gzip -9
    xml-info : Force to generate xml info. By default genhdlist3 will only regenerate xml info files already there in media_info
    appstream-info : Force to generate Appstream xml metadata.
    versioned : generate versioned media info, default: no
    media-info-dir : directory containing media info files (default: %(rpms_dir)s/media_info)
    verbose : be verbose
    version : print version and exit
    )

Options à considérer:
    nolock : do not lock the media_info directory
    no-bad-rpm : do not fail on bad rpm
    clean
    incremental
    mageia-tree : Walk in the tree sections/type.
    no-clean-old-rpms : do not clean old rpms.
    only-clean-old-rpms : only clean old rpms.
---

## 2. Deltas incrémentaux

### Principe

Chaque réindexation incrémente un compteur global. Le client compare son index local
à l'index courant du miroir et ne télécharge que les deltas manquants.

### Fichiers côté serveur

```
media_info/
  index              # numéro d'index courant (ex: 67)
  index_min          # plus ancien delta disponible (ex: 45)
  hdlist.cz          # full — pour primo-sync
  synthesis.hdlist.cz
  files.xml.lzma
  deltas/
    hdlist.62.cz
    hdlist.63.cz
    ...
    hdlist.67.cz
    synthesis.hdlist.62.cz
    ...
    files.xml.62.lzma
    ...
```

### Flux client

1. **Primo-sync** : téléchargement des fichiers full + mémorisation de `index` courant
2. **Sync suivante** :
   - Lire `index` distant
   - Si `local_index < index_min` → full sync (trop de retard, deltas prunés)
   - Sinon → télécharger et appliquer les deltas `local_index+1` … `index`
3. Mémoriser le nouvel `index` local

### Application des deltas (partie complexe)

Chaque delta contient les paquets **ajoutés ou modifiés** depuis l'index précédent.
Il faut aussi encoder les **suppressions**. Cas à gérer :

- Ajout de paquet : insertion dans DB locale
- Mise à jour (même nom, nouvelle version) : remplacement, mise à jour provides/requires/files
- Suppression : retrait propre avec cascade sur provides, requires, files
- Renommage / split de paquet : cas limite, à définir

**Questions ouvertes :**
- Format de l'entrée "suppression" dans le delta — header RPM fantôme ? Fichier manifest séparé ?
- Quelle granularité pour le pruning ? (nb de deltas max ? ancienneté en jours ?)
- Le delta `files.xml` est-il un diff XML ou un fichier complet des fichiers nouveaux/modifiés ?
- on pourrait imaginer les deltas des xml et synthesis comme étant des diff sur les fichiers complets avant compression. L'application consisterait à appliquer les patchs. Ensuite, la mise à jour de la BD ferait soit une comparaison, soit une reconstruction complète (bestial). Logiquement, il n'y a que des ajouts/suppressions d'enregistrements.
- il resterait utile d'avoir une liste des rpms ajoutés/supprimés. Ça permettrait de reconstruire hdlist par greffage des parties modifiées.

### Pruning

Un job de maintenance supprime les deltas antérieurs à `index_min` et met à jour
`index_min`. Si un client est trop en retard → full sync automatique.

---

## 3. AppStream

### Situation actuelle

- ~927 paquets sur ~30 000 ont des métadonnées XML dans `/usr/share/metainfo/`
- Les 29 000 restants n'ont rien → il faut des sources complémentaires

### Sources de données envisagées

- **Métadonnées embarquées** (`/usr/share/metainfo/*.xml`) — source primaire, 927 paquets
- **AppStream upstream** (https://github.com/ximion/appstream-data ou équivalent)
- **Flathub AppStream** — bonne couverture des apps desktop courantes
- **GNOME / KDE AppStream** — pour les apps de ces environnements
- **Synthèse depuis headers RPM** — fallback : générer un composant minimal depuis
  `Name`, `Summary`, `Description`, `Group`, `URL`, `Icon` du header RPM
- **Wikidata / OpenStreetMap** pour enrichissement (screenshots, ratings) — à évaluer

### Objectif qualité

- Screenshots
- Descriptions longues (pas juste le Summary RPM)
- Ratings / reviews (ODRS ou équivalent)
- Content-rating OARS
- Composants non-desktop : fonts, codecs, input-methods, CLI tools
- Validation `appstreamcli validate` sans erreurs
- Même schéma incrémental que hdlist/synthesis (deltas AppStream numérotés)

**Questions ouvertes :**
- Quelle stratégie de fallback pour les 29 000 paquets sans métadonnées ?
- Où stocker le cache des métadonnées upstream (DB SQLite dans urpm ?) ?
- Fréquence de mise à jour des sources upstream vs réindexation locale ?
- Format de sortie : `appstream.xml.gz` à la Debian, ou catalogue par composant ?

Papoteur:
- fallback avec les données issues des rpms
- stockage. ça dépend des données. pour les screenshots, ça peut être un cache. Des données upstream ne devraient-elles pas être accédées que par le côté serveur qui fait les listes de synthèse ? Il faut raisonner par type de données.
- il me semble que packagekit est prévu pour lire `appstream.xml.gz`
---

## Ordre d'implémentation suggéré

```
Étape 1 : urpm genmedia de base (port de upanier, bugs corrigés)
    ↓
Étape 2 : index incrémental + génération des deltas côté serveur
    ↓
Étape 3 : consommation des deltas côté client (urpm sync)
    ↓
Étape 4 : AppStream — extraction métadonnées embarquées
    ↓
Étape 5 : AppStream — sources complémentaires + fallback RPM headers
    ↓
Étape 6 : AppStream incrémental (mêmes deltas)
    ↓
Étape 7 : pruning automatique + monitoring
```
