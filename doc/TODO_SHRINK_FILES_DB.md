# TODO : Réduire `/var/lib/urpm/packages.db` (3,8 Go → ~150 Mo)

## Contexte

La base SQLite `/var/lib/urpm/packages.db` pèse 3,8 Go sur une machine
Mageia 10 RC standard, et continuera de gonfler quand les médias updates
de mga10 stable se rempliront (aujourd'hui leurs `files.xml.lzma` sont
des stubs de 65 octets en attendant la sortie stable).

### Mesure (mai 2026, dbstat)

| Élément | Taille | % |
|---|---:|---:|
| `package_files_fts_data` (FTS5 sur paths) | 2,24 Go | 59 % |
| `package_files` (table des chemins) | 756 Mo | 20 % |
| `idx_pf_dir_filename` | 496 Mo | 13 % |
| `idx_pf_filename` | 197 Mo | 5 % |
| `package_files_fts_docsize` | 74 Mo | 2 % |
| **Sous-total fichiers (table + indexes + FTS)** | **~3,75 Go** | **98 %** |
| Tout le reste (packages, deps, history, P2P…) | ~150 Mo | 2 % |

Pour comparaison : les `files.xml.lzma` source font **26 Mo compressés**
(404 Mo décompressés) tous médias confondus. On consomme donc ~9× la
taille des données brutes et ~140× la taille compressée pour héberger
ce qui sert uniquement à `urpm f`.

## Décision

Ne plus stocker les listes de fichiers en DB. À la place, scanner les
`files.xml.lzma` (déjà téléchargés par `urpm media update`) à la
demande, en streaming. Pas de cache : `urpm f` est une commande
one-shot, et tout cache disque recréerait le problème qu'on cherche à
fuir.

Approche validée le 2026-05-09.

## Sémantique de matching (alignée sur le comportement actuel)

À conserver à l'identique pour ne pas casser les habitudes utilisateur.
Voir `urpm/cli/commands/query.py::cmd_find` (rpm side) et
`urpm/core/db/files.py::search_files` (DB B-tree fallback) — les deux
appliquent les mêmes règles, transcrites :

| Pattern utilisateur | Comportement |
|---|---|
| `urpm f /usr/bin/bash` (commence par `/`) | Anchored sur le path complet (match exact, ou fnmatch si wildcards présents) |
| `urpm f bash` (ni `/` ni wildcard) | Auto-wrap en `*/bash` → match les fichiers dont le **basename** est exactement `bash` |
| `urpm f "*.so.6"` (wildcard explicite, pas de `/`) | fnmatch sur basename (équivalent `*/<glob>`) |
| `urpm f "/usr/lib*/libfoo*"` (anchored + wildcard) | fnmatch sur path complet |

Toujours **case-insensitive** par défaut.

Implémentation cible :

```python
def _compile_pattern(pattern: str) -> re.Pattern:
    if pattern.startswith('/') or '*' in pattern or '?' in pattern:
        glob = pattern
    else:
        glob = '*/' + pattern
    return re.compile(fnmatch.translate(glob), re.IGNORECASE)
```

## Dédup multi-médias

Un même fichier peut apparaître dans plusieurs `files.xml.lzma`
(typiquement un paquet présent en `core/release` ET en `core/updates`
sous deux EVR différents). Sans traitement, le scanner émet plusieurs
résultats redondants pour la même requête.

### Comportement par défaut : β — dédup `(name, arch)` sur EVR max

Pour chaque `(name, arch)`, on garde **uniquement la NEVRA d'EVR la
plus haute** rencontrée pendant le scan, tous médias confondus. Cela
correspond à la sémantique attendue de `urpm f` : *« quel paquet me
donnerait ce fichier si je l'installais maintenant ? »*.

Implémentation prévue : dict `(name, arch) → (best_evr, [matches])`
mis à jour pendant le scan. Comparaison EVR via `solv.pool.evrcmp()`
(libsolv déjà chargé pour le résolveur) ou `rpm.labelCompare`.

Edge case acté : si la version récente a déplacé ou supprimé le
fichier, l'utilisateur ne le verra plus — c'est cohérent avec ce
qu'il obtiendrait à l'install, donc OK.

**Cas important — fournisseurs concurrents.** La dédup est par
`(name, arch)`, pas par path. Quand plusieurs paquets *différents*
(noms distincts, mutuellement incompatibles via Conflicts/Provides
virtuels) fournissent le même fichier, ils restent tous visibles.
C'est précisément le résultat utile : l'utilisateur voit qu'il a un
choix à faire.

Exemple : `/usr/sbin/sendmail` est fourni par **postfix** comme par
**sendmail** (paquets concurrents, fournissant tous deux la
capability virtuelle `MTA`). Sortie attendue :

```
$ urpm f /usr/sbin/sendmail
/usr/sbin/sendmail → postfix-3.8.6-1.mga10.x86_64    [core/release]
/usr/sbin/sendmail → sendmail-8.18.1-1.mga10.x86_64  [core/release]
```

Autre cas typique : `/usr/bin/vi` fourni par `vim-minimal`,
`vim-enhanced`, `nvi`, etc.

### Opt-in : α — afficher toutes les versions

Flag `--all-versions` (long-form uniquement, `-a` est déjà pris par
`--available`). Désactive la dédup, affiche un résultat par
`(NEVRA, path, médium)`.

Exemple :

```
$ urpm f /usr/bin/bash
/usr/bin/bash → bash-5.2.21-2.mga10.x86_64  [core/updates]

$ urpm f --all-versions /usr/bin/bash
/usr/bin/bash → bash-5.2.21-1.mga10.x86_64  [core/release]
/usr/bin/bash → bash-5.2.21-2.mga10.x86_64  [core/updates]
```

### Mise à jour de l'aide CLI

Le help text actuel de `--available` dans `urpm/cli/main.py:984`
mentionne `urpm media update --files` qui n'existera plus. À
remplacer par une formulation neutre du genre *« Search only in
available packages »*.

---

## Périmètre du refactor

Issu d'un survey du code (mai 2026).

### À garder

- `packages_fts` (FTS5 sur name/summary/description, pour `urpm
  search`) : taille négligeable, hors top 15 dbstat. Pas la cible.
- `urpm/core/files_xml.py::parse_files_xml` : déjà en streaming
  (iterparse + LZMA à la volée + `elem.clear()` post-paquet). Quasi
  prêt à l'emploi, sera réutilisé par le scanner.
- Branche "installed" de `cmd_find` (lookup rpmdb via `RPMTAG_FILENAMES`)
  inchangée — c'est uniquement la branche "available" qui change.

### À retirer

#### 1. Tables et indexes SQLite

- Table `package_files` (création : `urpm/core/database.py:345`).
- Indexes `idx_pf_filename`, `idx_pf_dir_filename`, `idx_pf_media`
  (création : `database.py:356-360`).
- Table virtuelle `package_files_fts` + ses tables auxiliaires FTS5
  (`*_data`, `*_docsize`, `*_idx`, `*_config`).
  Création : `urpm/core/db/files.py:589`.

Ajouter une migration de schéma qui DROP toutes ces tables/indexes,
suivie d'un `VACUUM`. Bumper la version de schéma.

#### 2. Code mort résultant dans `urpm/core/db/files.py`

Sites d'écriture (à supprimer ou simplifier) :
- `:53` DELETE pré-import complet
- `:71,83` INSERT batch (import_files_xml)
- `:350` DELETE FTS (differential sync)
- `:365` DELETE par nevra (differential sync)
- `:416` INSERT batch (insert_package_files_batch)
- `:424` INSERT FTS (sync après insert)
- `:449` DELETE (clear_package_files)
- `:674` INSERT batched (rebuild_fts_index)

Sites de lecture (à supprimer ou réécrire pour ne plus toucher la
DB) :
- `:196-205` SELECT B-tree fallback (search_files)
- `:232,237` SELECT (get_files_for_package)
- `:312` SELECT DISTINCT (get_package_nevras_for_media)
- `:842` SELECT FTS MATCH (search_files_fts)

Note : `get_files_for_package(nevra)` est utilisé par d'autres
endroits ? À vérifier avant de supprimer aveuglément. Si oui,
réécrire en streaming aussi (chercher le `<files fn="…">` du nevra
dans le `files.xml.lzma` du media correspondant).

#### 3. Machinerie `sync_files`

Tout le sous-système qui orchestrait le « téléchargement opt-in des
files.xml » disparaît. Notamment :

- `db.has_any_sync_files_media()`, `db.set_all_media_sync_files()`,
  `db.get_media_with_sync_files()`, `db.is_fts_available()`,
  `db.is_fts_index_current()`, `db.get_files_stats()`.
- Table `files_xml_state` (md5, last_sync, file_count, pkg_count,
  compressed_size).
- Table `fts_state`.
- Flag CLI `--files` de `urpm media update`.
- Colonne `sync_files` sur `media` (vérifier si présente, à droper
  via migration).
- Le prompt interactif « Activer cette fonctionnalité ? (~500 Mo,
  10-15 min) » dans `cmd_find`.

Le `urpm media update` standard télécharge déjà les `files.xml.lzma`
si nécessaires (à confirmer ; sinon il faudra que `media update` les
fetch systématiquement, ce qui ajoute 26 Mo de download — acceptable).

### À ajouter

Un nouveau module `urpm/core/files_scanner.py` (ou méthode dans
`files_xml.py`, à arbitrer) avec :

```python
def iter_file_matches(media_paths, pattern, *,
                      case_insensitive=True) -> Iterator[Match]:
    """Stream-scan files.xml.lzma of the given media for paths
    matching `pattern`.  Yields Match(nevra, path, media_name)."""
```

- Réutilise `parse_files_xml()` (déjà streaming).
- Compile le pattern une fois avec la fonction `_compile_pattern`
  ci-dessus.
- Itère médium par médium (séquentiel pour démarrer ; mesurer avant
  de paralléliser).
- Pas de cache, pas d'état persistant.

Et une réécriture de la branche "available" de
`urpm/cli/commands/query.py::cmd_find` qui appelle ce scanner.

## Plan d'exécution

1. **Survey** (FAIT le 2026-05-09).
2. **Design du scanner** :
   - Décider de l'emplacement (`files_scanner.py` vs méthode dans
     `files_xml.py`).
   - Vérifier que `urpm media update` télécharge bien
     `files.xml.lzma` ; sinon, ajouter le fetch.
   - Vérifier les autres consommateurs de
     `get_files_for_package()` (qui lit la table à supprimer) et
     prévoir leur réécriture.
3. **Implémentation** :
   - Scanner + tests unitaires sur un mini `files.xml.lzma` synthétique.
   - Réécriture de `cmd_find` branche available.
4. **Migration DB** :
   - Bump schéma.
   - DROP des tables et indexes listés ci-dessus.
   - VACUUM.
   - Suppression du code mort dans `db/files.py`.
   - Suppression de la machinerie `sync_files`.
5. **Validation** :
   - `urpm f /usr/bin/bash` (path absolu)
   - `urpm f bash` (basename auto-wrap)
   - `urpm f "*.so.6"` (glob explicite)
   - `urpm f "/usr/lib*/libfoo*"` (anchored glob)
   - Mesure du temps : cible <1 s sur SSD, sub-second acceptable.
   - Mesure de la taille de DB après `VACUUM` : cible ~150 Mo.
   - Suite de tests `urpm/tests/`.

## Fichiers concernés

- `urpm/core/database.py` — création de schéma à nettoyer + migration.
- `urpm/core/db/files.py` — gros nettoyage.
- `urpm/core/files_xml.py` — parser à réutiliser tel quel.
- `urpm/core/files_scanner.py` — **nouveau**.
- `urpm/cli/commands/query.py` — `cmd_find` réécrit branche available.
- `urpm/cli/commands/media.py` — drop `--files` flag de `media update`,
  vérifier que les `files.xml.lzma` sont bien fetchés.
- `urpm/core/sync.py` — `import_files_xml` à supprimer (lignes 927-929
  et environs).

## Risques et points ouverts

- **Performance non mesurée** : on suppose <1 s sur SSD pour scanner
  26 Mo de xz. À confirmer une fois implémenté.
- **HDD lents / NFS** : le coût pourrait être désagréable. Si la
  mesure le montre, ajout d'un cache disque dans une 0.7.x ultérieure
  (mais avec invalidation propre, pas un FTS5 caché).
- **Téléchargement systématique des `files.xml.lzma`** : à confirmer
  que `urpm media update` les fetch déjà. Si non, +26 Mo par cycle de
  sync.
- **Version de schéma** : la migration DROP doit être idempotente
  (utilisateurs déjà passés par 0.7.x à FTS, vs nouvelle install).
- **`get_files_for_package()` consommateurs** : si d'autres commandes
  l'utilisent (probable : `urpm files <pkg>` ?), elles doivent être
  réécrites en streaming aussi.
