# TODO — urpm genmedia : plan d'implémentation

Voir `PLAN_GENMEDIA.md` pour le plan de conception original (contexte, deltas
incrémentaux, AppStream, questions ouvertes).

---

## Principe : coquille vide

L'architecture et les contrats d'interface sont définis par urpm-ng.
Les implémentations (lecture/écriture des formats media) sont à remplir par
le contributeur (papoteur) à partir du prototype `upanier.py`.

**Règle** : on ne duplique pas le code. Les classes de core qui lisent déjà
un format (hdlist, synthesis, files.xml, appstream) sont étendues pour
l'écrire aussi. `urpm/genmedia/` orchestre et scanne, `urpm/core/` lit et écrit.

**RPM séparé** : le module `urpm.genmedia` sera packagé dans un RPM distinct
(`urpm-ng-genmedia`) — import optionnel, zéro impact sur urpm-ng de base.

---

## Architecture des modules

### Nouveaux fichiers (`urpm/genmedia/`)

```
urpm/genmedia/
  __init__.py       — API publique, exports, RpmMetadata dataclass
  generator.py      — MediaGenerator : orchestrateur principal
  scanner.py        — RpmScanner : scan répertoire RPMs → RpmMetadata
  compress.py       — Utilitaires de compression en écriture (gzip, xz, lzma)
```

### Fichiers core étendus (ajout méthodes d'écriture)

```
urpm/core/hdlist.py       — ajout : HdlistWriter ou méthodes write/add_header
urpm/core/synthesis.py    — ajout : méthodes write/add_package
urpm/core/files_xml.py    — ajout : méthodes write/add_package
urpm/core/appstream.py    — ajout : extract depuis RPM + build_catalog
```

### CLI

```
urpm/cli/commands/genmedia.py  — cmd_genmedia()
```

---

## Dataclass partagée

```python
@dataclass
class RpmMetadata:
    """Métadonnées extraites d'un RPM, consommées par tous les writers."""
    filename: str           # ex: "foo-1.0-1.mga10.x86_64.rpm"
    name: str
    epoch: int
    version: str
    release: str
    arch: str
    summary: str
    description: str
    group: str
    license: str
    url: str
    sourcerpm: str
    size: int               # taille installée
    filesize: int           # taille du .rpm sur disque
    buildtime: int
    requires: list[str]     # avec contraintes : "foo[>= 1.0]"
    provides: list[str]
    conflicts: list[str]
    obsoletes: list[str]
    suggests: list[str]
    files: list[str]        # chemins complets
    changelog: list[tuple]  # (timestamp, author, text)
    header_bytes: bytes     # hdr.unload() brut pour hdlist
    header_sha256: str      # pour le mode incrémental
```

---

## Contrats d'interface

### RpmScanner (`urpm/genmedia/scanner.py`)

```python
class RpmScanner:
    """Scan un répertoire de RPMs et extrait les métadonnées."""

    def scan(self, rpms_dir: Path) -> Iterator[RpmMetadata]:
        """Yield RpmMetadata pour chaque .rpm dans rpms_dir.

        Utilise rpm.TransactionSet pour lire les headers.
        Voir urpm.core.rpm.read_rpm_header() comme référence.
        """
        raise NotImplementedError
```

### MediaGenerator (`urpm/genmedia/generator.py`)

```python
class MediaGenerator:
    """Orchestrateur de génération des métadonnées media."""

    def __init__(self, rpms_dir: Path,
                 media_info_dir: Path | None = None,
                 lock: bool = True,
                 verbose: bool = False):
        ...

    def generate(self, *,
                 hdlist: bool = True,
                 synthesis: bool = True,
                 xml_info: bool = False,
                 appstream: bool = False,
                 md5sum: bool = True,
                 incremental: bool = False,
                 hdlist_filter: str = ".cz:gzip -9",
                 synthesis_filter: str = ".cz:xz -7",
                 xml_info_filter: str = ".lzma:xz -7",
                 versioned: bool = False,
                 allow_empty: bool = False) -> GenerateResult:
        """Génère les fichiers media_info/.

        Flux :
          1. Acquérir le lock (si lock=True)
          2. Scanner les RPMs (RpmScanner)
          3. Écrire hdlist        (core/hdlist)
          4. Écrire synthesis     (core/synthesis)
          5. Écrire XML info      (core/files_xml + xmlinfo)
          6. Écrire AppStream     (core/appstream)
          7. Générer MD5SUM
          8. Renommer tmp → final (atomique)
          9. Relâcher le lock
        """
        raise NotImplementedError
```

### Méthodes d'écriture dans core (à ajouter)

#### `urpm/core/hdlist.py`

```python
# Méthodes à ajouter (classe existante ou nouvelle section)

def write_hdlist(output_path: Path, packages: Iterator[RpmMetadata], *,
                 compression_filter: str = "gzip -9",
                 block_size: int = 400 * 1024,
                 incremental: bool = False,
                 old_hdlist_path: Path | None = None) -> int:
    """Écrit un hdlist.cz à partir d'un flux de RpmMetadata.

    Chaque header_bytes est accumulé dans des blocs de block_size octets.
    Les blocs sont compressés individuellement. Un TOC est écrit à la fin.

    En mode incrémental, réutilise les blocs inchangés de old_hdlist_path
    (comparaison par header_sha256).

    Retourne le nombre de paquets écrits.
    """
    raise NotImplementedError
```

#### `urpm/core/synthesis.py`

```python
def write_synthesis(output_path: Path, packages: Iterator[RpmMetadata], *,
                    compression_filter: str = "xz -7") -> int:
    """Écrit un synthesis.hdlist.cz au format @field@value.

    Champs par paquet : @requires, @suggests, @obsoletes, @conflicts,
    @provides, @summary, @filesize, @info (NEVRA@epoch@size@group).

    Retourne le nombre de paquets écrits.
    """
    raise NotImplementedError
```

#### `urpm/core/files_xml.py`

```python
def write_files_xml(output_path: Path, packages: Iterator[RpmMetadata], *,
                    compression_filter: str = "xz -7") -> int:
    """Écrit files.xml.lzma — liste des fichiers par paquet.

    Format : <media_info><files fn="pkg.rpm">file1\\nfile2\\n</files>...</media_info>

    Retourne le nombre de paquets écrits.
    """
    raise NotImplementedError


def write_info_xml(output_path: Path, packages: Iterator[RpmMetadata], *,
                   compression_filter: str = "xz -7") -> int:
    """Écrit info.xml.lzma — sourcerpm, url, license, description par paquet.

    Retourne le nombre de paquets écrits.
    """
    raise NotImplementedError


def write_changelog_xml(output_path: Path, packages: Iterator[RpmMetadata], *,
                        compression_filter: str = "xz -7") -> int:
    """Écrit changelog.xml.lzma — entrées changelog par paquet.

    Retourne le nombre de paquets écrits.
    """
    raise NotImplementedError
```

---

## CLI : `urpm genmedia`

```
urpm genmedia <rpms_dir> [options]

Options :
  --media-info-dir DIR       Répertoire de sortie (défaut: rpms_dir/media_info)
  --hdlist-filter FILTER     Compression hdlist (défaut: .cz:gzip -9)
  --synthesis-filter FILTER  Compression synthesis (défaut: .cz:xz -7)
  --xml-info-filter FILTER   Compression XML (défaut: .lzma:xz -7)
  --no-hdlist                Ne pas générer hdlist.cz
  --no-md5sum                Ne pas générer MD5SUM
  --xml-info                 Forcer la génération des XML info
  --appstream-info           Générer les métadonnées AppStream
  --incremental              Mise à jour incrémentale (défaut: reconstruction complète)
  --versioned                Préfixer les fichiers avec un timestamp
  --allow-empty-media        Autoriser un media sans RPMs
  --nolock                   Ne pas verrouiller media_info/
  --no-bad-rpm               Ignorer les RPMs invalides au lieu d'échouer
  --mageia-tree              Parcourir l'arbre sections/type Mageia
  -v, --verbose              Mode verbeux
```

---

## Réutilisation de core — récapitulatif

| Module core existant | Lecture (existe) | Écriture (à ajouter) |
|---|---|---|
| `core/hdlist.py` | `parse_hdlist()`, `RPMHeader` | `write_hdlist()` |
| `core/synthesis.py` | `parse_synthesis()`, `parse_nevra()` | `write_synthesis()` |
| `core/files_xml.py` | `parse_files_xml()`, `search_files_xml()` | `write_files_xml()`, `write_info_xml()`, `write_changelog_xml()` |
| `core/appstream.py` | `AppStreamManager.sync_media_appstream()` | `extract()`, `build_catalog()` |
| `core/compression.py` | `decompress()`, `detect_format()` | — (genmedia/compress.py pour l'écriture) |
| `core/sync_lock.py` | `SyncLock` | — (réutilisé tel quel) |
| `core/rpm.py` | `read_rpm_header()` | — (réutilisé par RpmScanner) |

---

## Bugs upanier à corriger lors de l'intégration

Les bugs identifiés dans `upanier.py` / `gen_urpm.py` à ne **pas** reproduire :

| # | Sévérité | Fichier | Description |
|---|----------|---------|-------------|
| 1 | CRITIQUE | `gen_urpm.py` `_write_incremental` | Mode incrémental : les nouveaux RPMs sont classifiés (`new_rpms` set) mais jamais écrits — aucun appel à `_append_header` pour eux après la boucle de blocs |
| 2 | CRITIQUE | `gen_urpm.py` `_find_icon_in_rpm` | Reçoit des objets `rpm.files` (itérateur de `rpm.fi`) mais les traite comme `list[str]` — `candidate in file_list` ne matche jamais, extraction d'icônes cassée |
| 3 | CRITIQUE | `gen_urpm.py` `build_toc` | `entry + "\n"` où `entry` est `bytes` (clé de `self.dir`) + `str` → TypeError à l'exécution |
| 4 | MAJEUR | `gen_urpm.py` `__init__` | Logging inversé : `self.log` affiche quand `quiet=True` et masque quand `quiet=False` |
| 5 | MAJEUR | `gen_urpm.py` `extract_appstream` | `current_sha` calculé mais jamais comparé à `state[rpm_name]['sha256']` — le skip incrémental n'est pas implémenté, chaque RPM est reprocessé à chaque run |
| 6 | MAJEUR | `gen_urpm.py` `extract_appstream` | `f.dirname` sur objets `rpm.fi` — sémantique dépend de la version de python3-rpm, fragile pour les checks `.endswith(METAINFO_SUFFIXES)` |
| 7 | MAJEUR | `gen_urpm.py` `build_appstream_catalog` | Signature déclare `-> tuple[Path \| None, Path \| None]` mais retourne un seul `Path` ou `(None, None)` |
| 8 | MOYEN | `gen_urpm.py` `encode_xml` | Retourne `None` si input est `None` alors que le type hint dit `-> str` — cause TypeError en aval dans string concatenation |
| 9 | MOYEN | `gen_urpm.py` `__init__` | `raise Exception(f"Invalid hdlist filter {hdlist_filter}")` — variable `hdlist_filter` n'existe pas (paramètre s'appelle `filter`) → NameError au lieu du message d'erreur voulu |
| 10 | MOYEN | `gen_urpm.py` `write_xml` | Chaîne de `if` au lieu de `elif` — fragile, le flux tombe dans le `else` pour files/info/changelog (fonctionne par accident) |
| 11 | MINEUR | `gen_urpm.py` `_rpm_header_str` | Méthode définie mais jamais appelée (dead code) |
| 12 | MINEUR | `gen_urpm.py` `_compress_gzip` | Méthode définie mais jamais appelée — le catalogue utilise `lzma.open` directement (dead code) |
| 13 | MINEUR | `gen_urpm.py` `build_toc` | `toc_sizes_offsets` accumulé mais jamais écrit — recalculé inline dans `toc_str` (variable morte) |
| 14 | MINEUR | `gen_urpm.py` `build_toc` | `self.files[entry].values()` suppose l'ordre d'insertion du dict — correct en Python 3.7+ mais fragile |
| 15 | MINEUR | `gen_urpm.py` `extract_appstream` | `try/except` commenté autour de `_generate_appstream_xml` — toute erreur crash le run entier |
| 16 | MINEUR | `gen_urpm.py` `file_sizes` | `def file_sizes(self, rpm_list: List=[])` — mutable default argument classique |
| 17 | MINEUR | `gen_urpm.py` `build_toc` | `pack(b">4s4i40s4s", ...)` — format string en bytes, inhabituel pour `struct.pack` |

---

## Ordre d'implémentation

```
Phase 1 — Coquille vide
  ├── Créer urpm/genmedia/ avec classes vides + contrats
  ├── Ajouter stubs write dans core/hdlist, synthesis, files_xml
  ├── CLI urpm genmedia (argparse + dispatch)
  └── Tests unitaires des contrats (interfaces)

Phase 2 — Implémentation de base
  ├── RpmScanner.scan()
  ├── write_synthesis()
  ├── write_hdlist() (mode complet)
  ├── write_files_xml(), write_info_xml(), write_changelog_xml()
  └── MediaGenerator.generate() (orchestration)

Phase 3 — Mode incrémental
  ├── State JSON (sha256 par RPM + layout blocs)
  ├── write_hdlist() mode incrémental
  └── Deltas numérotés (si décidé — voir PLAN_GENMEDIA.md §2)

Phase 4 — AppStream
  ├── Extraction metainfo depuis RPMs
  ├── Fallback depuis headers RPM
  ├── Construction catalogue
  └── Deltas AppStream

Phase 5 — Intégration avancée
  ├── --mageia-tree (parcours arbre complet)
  ├── Pruning automatique des deltas
  └── Monitoring / métriques
```
