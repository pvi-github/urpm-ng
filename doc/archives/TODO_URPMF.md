# Plan urpmf / urpm find

## Contexte

`urpm find` ne cherche actuellement que dans les paquets **installés** (via rpm).
Pour chercher dans les paquets **disponibles**, il faut parser `files.xml.lzma` des media_info.

## Format files.xml.lzma

- Fichier compressé lzma
- ~7 millions de lignes pour un media complet (Core Release)
- Structure XML :

```xml
<?xml version="1.0" encoding="utf-8"?>
<media_info>
  <files fn="package-nevra">
    /chemin/fichier1
    /chemin/fichier2
  </files>
  <files fn="autre-package">
    ...
  </files>
</media_info>
```

## Etat actuel (implémenté)

### Fichiers modifiés

- `urpm/core/files_xml.py` - parser streaming avec iterparse
- `urpm/core/database.py` :
  - Table `package_files` avec colonnes `dir_path` + `filename` (split pour index efficace)
  - Table `files_xml_state` pour tracker MD5, file_count, compressed_size par media
  - Méthodes: `import_files_xml`, `search_files`, `get_files_xml_state`, `get_files_xml_ratio`
  - Méthodes staging: `create_package_files_staging`, `import_files_to_staging`, `finalize_package_files_atomic`
  - PRAGMAs rapides: `set_fast_import_pragmas`, `restore_pragmas`
- `urpm/core/sync.py` :
  - `sync_files_xml` - sync un media
  - `sync_all_files_xml` - sync parallèle tous les media avec:
    - Phase 1: Check MD5 en parallèle
    - Phase 2: Downloads parallèles (4 workers)
    - Phase 3: Import séquentiel dans staging table
    - Phase 4: Atomic swap
  - Filtre par version/arch depuis `/etc/mageia-release`
- `urpm/cli/main.py` :
  - Option `--files` sur `urpm media update`
  - `cmd_find` cherche dans package_files

### Schema actuel

```sql
CREATE TABLE package_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id INTEGER NOT NULL,
    pkg_nevra TEXT NOT NULL,
    dir_path TEXT NOT NULL,     -- '/usr/lib64/httpd/modules'
    filename TEXT NOT NULL      -- 'mod_ssl.so'
);
CREATE INDEX idx_pf_filename ON package_files(filename);
CREATE INDEX idx_pf_dir_filename ON package_files(dir_path, filename);
CREATE INDEX idx_pf_media ON package_files(media_id);

CREATE TABLE files_xml_state (
    media_id INTEGER PRIMARY KEY,
    files_md5 TEXT,
    last_sync INTEGER,
    file_count INTEGER,
    pkg_count INTEGER,
    compressed_size INTEGER
);
```

### Problème de performance

L'import complet prend **11 minutes** pour 7M fichiers (6 media x86_64).
Le bottleneck est la **création des index** sur 7M lignes après l'atomic swap.

## NOUVELLE APPROCHE: Import différentiel

Au lieu de tout réimporter, faire un diff:

### Algorithme

```
1. Extraire pkg_nevra depuis DB actuelle:
   SELECT DISTINCT pkg_nevra FROM package_files WHERE media_id = ?

2. Extraire pkg_nevra depuis nouveau files.xml (rapide ~2s):
   sed -ne 's/^.*fn="\([^"]*\)".*$/\1/p' files.xml | sort

3. Calculer le diff:
   - supprimés = anciens - nouveaux
   - ajoutés = nouveaux - anciens

4. DELETE FROM package_files WHERE media_id = ? AND pkg_nevra IN (supprimés)

5. Parser files.xml mais INSERT seulement pour pkg_nevra IN (ajoutés)
```

### Avantages

- Pas de recréation d'index (ils restent en place)
- Traitement uniquement des changements
- Entre deux updates, très peu de paquets changent = très rapide

### Implémentation suggérée

```python
def sync_files_xml_incremental(db, media_id, files_xml_path):
    """Import différentiel des fichiers."""

    # 1. NEVRA existants en DB
    existing = db.get_package_nevras_for_media(media_id)  # Set[str]

    # 2. NEVRA dans le nouveau XML (parsing rapide, juste les attributs fn)
    new_nevras = extract_nevras_from_files_xml(files_xml_path)  # Set[str]

    # 3. Diff
    to_remove = existing - new_nevras
    to_add = new_nevras - existing

    # 4. Supprimer les paquets disparus
    if to_remove:
        db.delete_package_files_by_nevra(media_id, to_remove)

    # 5. Ajouter les nouveaux (parsing complet mais filtré)
    if to_add:
        for nevra, files in parse_files_xml(files_xml_path):
            if nevra in to_add:
                db.insert_package_files(media_id, nevra, files)

    return len(to_remove), len(to_add)
```

### Méthodes DB à ajouter

```python
def get_package_nevras_for_media(self, media_id: int) -> Set[str]:
    """Retourne tous les pkg_nevra distincts pour un media."""

def delete_package_files_by_nevra(self, media_id: int, nevras: Set[str]):
    """Supprime les fichiers des paquets spécifiés."""

def insert_package_files(self, media_id: int, nevra: str, files: List[str]):
    """Insère les fichiers d'un paquet."""
```

### Extraction rapide des NEVRA

```python
def extract_nevras_from_files_xml(path: Path) -> Set[str]:
    """Extrait uniquement les attributs fn= sans parser tout le XML."""
    import re
    import lzma

    nevras = set()
    pattern = re.compile(rb'fn="([^"]+)"')

    with lzma.open(path, 'rb') as f:
        for line in f:
            match = pattern.search(line)
            if match:
                nevras.add(match.group(1).decode('utf-8'))

    return nevras
```

## TODO

- [x] Implémenter `extract_nevras_from_files_xml()` dans files_xml.py
- [x] Ajouter `get_package_nevras_for_media()` dans database.py
- [x] Ajouter `delete_package_files_by_nevra()` dans database.py
- [x] Créer `sync_files_xml_incremental()` dans sync.py
- [x] Modifier `sync_all_files_xml()` pour utiliser l'approche incrémentale
- [x] Garder l'approche full-import pour le premier import (table vide)
- [x] Corriger l'affichage des progress (lignes parasites)
- [x] Fix `force=False` pour respecter les checks MD5

## Résultats de performance

| Scénario | Temps |
|----------|-------|
| Premier import complet (11m) | ~1m44s (10x plus rapide grâce à l'optimisation) |
| Re-sync immédiat (rien changé) | ~2-5s (check MD5 seulement) |
| Update incrémental (quelques paquets) | ~30-40s |

## Notes

- Le premier import crée les index (lent mais une seule fois)
- Les updates suivants utilisent le mode incrémental (diff sur pkg_nevra)
- L'affichage utilise `\r` pour écraser la ligne (propre)

---

## PROCHAINES ÉTAPES

### 1. Option `sync_files` par media (off par défaut)

Ajouter une option dans la table `media` pour activer/désactiver le sync automatique des files.xml :

```sql
ALTER TABLE media ADD COLUMN sync_files INTEGER DEFAULT 0;
```

**Commande:** `urpm media set <media> --sync-files` / `--no-sync-files`

**Pourquoi off par défaut:**
- Économise l'espace disque (~500 Mo pour tous les media)
- Évite du trafic réseau inutile si pas utilisé
- Petites configs (VM, conteneurs) ne veulent pas ça

### 2. Prompt au premier `urpm find`

Quand l'utilisateur fait `urpm find <pattern>` et que :
- Aucun media n'a `sync_files=1`
- OU la table `package_files` est vide

Afficher :
```
La recherche dans les paquets disponibles nécessite le téléchargement
des fichiers files.xml.lzma (~XXX Mo, ~10-15 minutes la première fois).

Voulez-vous activer cette fonctionnalité ? [o/N]

Si oui :
  1. Active sync_files sur tous les media activés
  2. Lance `urpm media update --files`
  3. Explique que les mises à jour seront automatiques (urpmd)
```

### 3. urpmd: sync files.xml automatique (idle)

**Logique (PAS un cron bête):**
```python
def should_sync_files():
    # Vérifier si au moins un media a sync_files=1
    if not any_media_with_sync_files():
        return False

    # Vérifier si dernière sync > 24h
    last_sync = get_oldest_files_xml_sync()
    if last_sync and (now - last_sync) < 24 * 3600:
        return False

    # Vérifier si machine idle (load < 0.5, pas d'activité utilisateur)
    if not is_machine_idle():
        return False

    return True
```

**Où ajouter ça:**
- Dans `urpm/daemon/scheduler.py` (ou équivalent)
- Même logique que le pre-cache des paquets populaires

### 4. Estimation espace disque

Fonction pour estimer l'espace requis avant activation :
```python
def estimate_files_xml_size(db) -> int:
    """Estime la taille totale des files.xml.lzma en bytes."""
    # Basé sur compressed_size des media déjà syncés
    # OU estimation fixe (~80 Mo par media core)
```

### 5. Commandes à implémenter

```bash
# Activer sync_files sur un media
urpm media set "Core Release" --sync-files

# Désactiver
urpm media set "Core Release" --no-sync-files

# Activer sur tous les media
urpm media set --all --sync-files

# Voir l'état
urpm media list  # Ajouter colonne [F] pour sync_files
```

### 6. Fichiers à modifier

| Fichier | Modification |
|---------|--------------|
| `urpm/core/database.py` | Ajouter colonne `sync_files`, migration schema |
| `urpm/cli/main.py` | `cmd_media_set` + option `--sync-files` |
| `urpm/cli/main.py` | `cmd_find` prompt si files.xml non dispo |
| `urpm/daemon/scheduler.py` | Task sync_files_xml périodique |
| `urpm/cli/main.py` | `cmd_media_list` afficher colonne [F] |

### 7. UX Flow

```
$ urpm find pg_hba.conf

Installed:
  (aucun résultat)

Pour chercher dans les paquets disponibles, la base files.xml doit être activée.
Cela nécessite ~500 Mo d'espace disque et ~10-15 minutes pour le premier téléchargement.

Activer la recherche dans les paquets disponibles ? [o/N] o

Activation de sync_files sur 6 media...
Téléchargement des files.xml (première fois, ~10-15 min)...
  Core Release: downloading 45%
  ...

Recherche terminée. Les mises à jour seront automatiques (urpmd, 1x/jour).

Available (not installed):
  postgresql16-server-16.2-1.mga10.x86_64: /var/lib/pgsql/data/pg_hba.conf
```


=====


# urpm-ng – Plan d'optimisation de la recherche de fichiers (SQLite + FTS5)

**Date** : février 2026  
**Objectif** : Passer les recherches de fichiers (LIKE '%motif%') de 2–20 secondes à < 0,5–1 s, même sur HDD, sans exiger de matériel moderne.  
**Contexte** : Table `package_files` avec ~7–8 millions de lignes. Recherches sur `filename` et `dir_path`.


urpm-gn[](https://github.com/pvi-github/urpm-ng) intègre depuis peu la recherche de fichiers.  
Il récupère les métadonnées des miroirs (files.xml.lzma) et les intègre dans une table de sa base sqlite.  
L'intégration est maintenant différentielle : lors des mises à jour on intègre que le delta pour booster les mises à jour de paquets (suppression des fichiers associés à un paquet mis à jour puis injection de la nouvelle liste de fichiers associés)... pour 7 à 8 millions de lignes il y avait intérêt.  

Sauf que la recherche dans la base (et malgré les index ajoutés) prend entre 2 et 20 secondes... alors que je voudrais que ça prenne une demi-seconde...  

Comment je pourrais optimiser ça ?

## Schéma actuel

```sql
CREATE TABLE IF NOT EXISTS package_files (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    media_id  INTEGER NOT NULL,
    pkg_nevra TEXT    NOT NULL,
    dir_path  TEXT    NOT NULL,
    filename  TEXT    NOT NULL
);

CREATE INDEX idx_pf_filename     ON package_files(filename);
CREATE INDEX idx_pf_dir_filename ON package_files(dir_path, filename);
CREATE INDEX idx_pf_media        ON package_files(media_id);


Problème principal : LIKE '%motif%' → full table scan (index B-tree inutiles pour wildcard au début).

Solution a discuter avant implémentation : FTS5 external content + trigram + synchro incrémentale par NEVRA
1. Création de la table FTS5 (external content – pas de duplication)

CREATE VIRTUAL TABLE IF NOT EXISTS package_files_fts USING fts5(
    dir_path,
    filename,
    tokenize = 'trigram',
    detail   = 'none',
    content       = 'package_files',
    content_rowid = 'id'
);

    trigram : accélère nativement LIKE '%...%' et GLOB '*...*' sur dir_path et filename.
    external content : FTS ne stocke que l'index → gain énorme d'espace disque.
    detail = 'none' : réduit encore la taille de l'index (~30–50 %).

2. Index recommandé sur la table principale (pour accélérer les DELETE/INSERT par nevra)

CREATE INDEX IF NOT EXISTS idx_package_files_nevra ON package_files(pkg_nevra);

3. Migration initiale

idée à travailler :

def migrate_db(db_path: str):
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()

    # ... autres migrations (user_version, etc.)

    # Création FTS5 si absente
    cur.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS package_files_fts USING fts5(
            dir_path, filename,
            tokenize = 'trigram', detail = 'none',
            content = 'package_files', content_rowid = 'id'
        );
    """)

    # Population initiale seulement si vide
    cur.execute("SELECT COUNT(*) FROM package_files_fts;")
    if cur.fetchone()[0] == 0:
        conn.execute("PRAGMA synchronous = OFF;")
        cur.execute("INSERT INTO package_files_fts(package_files_fts) VALUES('rebuild');")
        conn.execute("PRAGMA synchronous = NORMAL;")
        conn.commit()

    # ... fin migration

4. Synchronisation incrémentale (lors des updates de paquets)

Principe :

Chaque évolution/suppression de paquet = nouveau/suppression de NEVRA.
→ On supprime tous les fichiers de l'ancien NEVRA et on ajoute tous les fichiers du nouveau NEVRA.

Code dans la transaction / post-update :

# nevra_to_remove : liste des NEVRA supprimés dans cette transaction
# nevra_to_add   : liste des NEVRA ajoutés dans cette transaction

with conn:
    # 1. Supprimer les entrées FTS des paquets disparus
    for nevra in nevra_to_remove:
        cur.execute("""
            DELETE FROM package_files_fts
            WHERE rowid IN (
                SELECT id FROM package_files WHERE pkg_nevra = ?
            )
        """, (nevra,))

    # 2. Ajouter les entrées FTS des nouveaux paquets
    for nevra in nevra_to_add:
        cur.execute("""
            INSERT INTO package_files_fts(rowid, dir_path, filename)
            SELECT id, dir_path, filename
            FROM package_files
            WHERE pkg_nevra = ?
        """, (nevra,))

5. Optimisations SQLite globales (à appliquer au démarrage de connexion)

conn.execute("PRAGMA journal_mode = WAL;")
conn.execute("PRAGMA synchronous = NORMAL;")
conn.execute("PRAGMA cache_size = -40000;")       # ~40 Mo
conn.execute("PRAGMA mmap_size = 268435456;")     # 256 Mo si RAM dispo

6. Exemples de requêtes de recherche (rapides grâce au trigram)

-- Recherche basique (sur nom OU chemin)
SELECT p.pkg_nevra, p.dir_path || '/' || p.filename AS full_path
FROM package_files_fts fts
JOIN package_files p ON p.id = fts.rowid
WHERE fts MATCH 'openssl*' OR dir_path MATCH 'bin*'
LIMIT 300;

-- Ou avec LIKE (SQLite utilise trigram automatiquement quand possible)
SELECT p.pkg_nevra, p.dir_path || '/' || p.filename
FROM package_files_fts fts
JOIN package_files p ON p.id = fts.rowid
WHERE filename LIKE '%openssl%' OR dir_path LIKE '%openssl%'
LIMIT 300;

7. Maintenance occasionnelle (rare)

    Rebuild complet (après migration majeure ou incohérence détectée) :

    cur.execute("INSERT INTO package_files_fts(package_files_fts) VALUES('rebuild');")

    Nettoyage orphelins (très rare) :

    DELETE FROM package_files_fts
    WHERE rowid NOT IN (SELECT id FROM package_files);

    Optimisation index FTS (périodique ou après gros batch) :

    INSERT INTO package_files_fts(package_files_fts) VALUES('optimize');

8. Avantages attendus

    Temps de recherche : < 0,5–1 s même sur HDD (vs 2–20 s aujourd'hui)
    Overhead update : très faible (O(nb fichiers du paquet modifié))
    Espace disque : minimal (FTS = index seul)
    Pas de table de tracking supplémentaire
    Robuste aux crashes (synchro explicite dans transaction)

9. Points à tester

    Temps d'un rebuild initial sur base réelle => point d'inquiétude
    Perf DELETE + INSERT sur un paquet avec 500+ fichiers
    EXPLAIN QUERY PLAN sur les requêtes de recherche ?
    Comportement WAL + cache sur vieux HDD à voir ?

