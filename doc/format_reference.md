# URPM Format Reference — hdlist & synthesis

> **Document de référence** pour les formats hdlist et synthesis utilisés par URPM/urpm-ng
> sur Mageia Linux. Fusion de `hdlist_parsing_guide.md` et `urpm_format_guide.md`.

## Table des matières

1. [Introduction](#1-introduction)
2. [Compression](#2-compression)
3. [Format hdlist](#3-format-hdlist)
4. [Format synthesis](#4-format-synthesis)
5. [hdlist vs synthesis](#5-hdlist-vs-synthesis)
6. [Exemples de parsing](#6-exemples-de-parsing)
7. [Legacy URPM Internals](#7-legacy-urpm-internals)
8. [Troubleshooting](#8-troubleshooting)
9. [Ressources](#9-ressources)

---

## 1. Introduction

URPM (et son successeur urpm-ng) utilise deux formats de métadonnées pour décrire les
packages RPM disponibles dans les médias :

- **hdlist** (`hdlist.cz`) : fichier **binaire** contenant une concaténation de headers RPM
  complets extraits des packages. Il contient toutes les informations : dépendances, filelists,
  changelog, descriptions, etc. C'est le format le plus riche, mais aussi le plus lourd
  (15-50 MB décompressé). Son parsing nécessite librpm ou une implémentation manuelle du
  format binaire RPM.

- **synthesis** (`synthesis.hdlist.cz`) : fichier **texte** structuré avec délimiteurs `@`,
  contenant uniquement les métadonnées essentielles (nom, version, dépendances, provides,
  conflicts, obsoletes, summary, group, taille). Très compact (500 KB - 2 MB), parsable avec
  grep/awk ou n'importe quel langage. C'est le format utilisé par défaut pour la résolution
  de dépendances.

**Quand utiliser lequel ?**
- **synthesis** : résolution de dépendances, recherche de packages, listing — cas d'usage courant
- **hdlist** : requêtes détaillées (filelists, changelog, descriptions complètes), recherche
  de fichiers dans les packages (`urpmf`)

Les deux fichiers portent l'extension `.cz` (historiquement "compressed") et sont compressés
en zstd sur les versions actuelles de Mageia.

---

## 2. Compression

### Format de compression actuel : zstd

**IMPORTANT** : Malgré l'extension `.cz`, ces fichiers utilisent la compression **zstd**
(Zstandard) sur les versions actuelles de Mageia.

```bash
# Vérifier le type réel du fichier :
file synthesis.hdlist.cz
# Output: Zstandard compressed data

# Décompresser avec zstd :
zstdcat synthesis.hdlist.cz > synthesis.txt
zstdcat hdlist.cz > hdlist.raw
```

### Magic bytes par format

| Format | Magic bytes (hex) | Commande de décompression |
|--------|-------------------|---------------------------|
| zstd   | `28 b5 2f fd`     | `zstdcat`                 |
| xz     | `fd 37 7a 58`     | `xzcat`                   |
| gzip   | `1f 8b`           | `zcat`                    |
| bzip2  | `42 5a`           | `bzcat`                   |

### Pourquoi zstd ?

**zstd (Zstandard)** offre :
- **Excellent compromis** compression/vitesse de décompression
- Plus rapide que xz à décompresser
- Meilleure compression que gzip
- Standard moderne (Facebook, kernel Linux, etc.)

### Historique de la compression

- Anciennes versions : gzip (d'où le `.cz` = compressed)
- Versions intermédiaires : xz pour meilleure compression
- Versions actuelles : zstd pour le meilleur compromis
- L'extension `.cz` a été conservée pour compatibilité

### Auto-détection en Python (RECOMMANDÉ)

```python
#!/usr/bin/env python3
"""Auto-détection du format de compression — extrait du PoC urpmi_ng_prototype.py"""

def decompress_file(filename: str) -> str:
    """Décompresse un fichier texte avec auto-détection du format."""
    with open(filename, 'rb') as f:
        magic = f.read(8)

    if magic[:4] == b'\x28\xb5\x2f\xfd':  # zstd (format actuel Mageia)
        import zstandard as zstd
        dctx = zstd.ZstdDecompressor()
        with open(filename, 'rb') as f:
            with dctx.stream_reader(f) as reader:
                return reader.read().decode('utf-8', errors='replace')
    elif magic[:2] == b'\x1f\x8b':  # gzip (fallback)
        import gzip
        with gzip.open(filename, 'rt', encoding='utf-8', errors='replace') as f:
            return f.read()
    elif magic[:6] == b'\xfd7zXZ\x00':  # xz (fallback)
        import lzma
        with lzma.open(filename, 'rt', encoding='utf-8', errors='replace') as f:
            return f.read()
    elif magic[:2] == b'BZ':  # bzip2 (fallback)
        import bz2
        with bz2.open(filename, 'rt', encoding='utf-8', errors='replace') as f:
            return f.read()
    else:  # non compressé
        with open(filename, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()


def decompress_file_binary(filename: str) -> bytes:
    """Décompresse un fichier en mode binaire (auto-détection)."""
    with open(filename, 'rb') as f:
        magic = f.read(8)
        f.seek(0)

        if magic[:4] == b'\x28\xb5\x2f\xfd':  # zstd
            import zstandard as zstd
            dctx = zstd.ZstdDecompressor()
            with dctx.stream_reader(f) as reader:
                return reader.read()
        elif magic[:2] == b'\x1f\x8b':  # gzip
            import gzip
            return gzip.decompress(f.read())
        elif magic[:6] == b'\xfd7zXZ\x00':  # xz
            import lzma
            return lzma.decompress(f.read())
        elif magic[:2] == b'BZ':  # bzip2
            import bz2
            return bz2.decompress(f.read())
        else:
            return f.read()
```

---

## 3. Format hdlist

### Vue d'ensemble

Les fichiers **hdlist** (header list) contiennent une succession de **headers RPM complets**
extraits des packages. URPM utilise **librpm** pour les parser, ce qui est beaucoup plus fiable
que de parser le format binaire manuellement.

> **VALIDE** : Le format hdlist a ete verifie avec `parse_hdlist.py` sur de vrais fichiers Mageia.
> La structure decrite ci-dessous est confirmee.

### Architecture du parsing dans URPM

```
hdlist.cz (zstd compresse - format actuel Mageia)
    |
    +- zstdcat -> hdlist (raw)
    |
    +- URPM::parse_hdlist() (Perl)
    |
    +- parse_hdlist__XS() (binding XS)
    |
    +- Code C utilisant librpm
         +- Fdopen()     : ouvrir le fichier
         +- headerRead() : lire chaque header
         +- headerGet()  : extraire les tags
         +- headerFree() : liberer la memoire
```

### Structure binaire d'un fichier hdlist

Un hdlist decompresse est une **concatenation de headers RPM** :

```
+------------------------------------+
| Header RPM #1                      |
|   - Magic: 0x8EADE801              |
|   - Index entries (N x 16 bytes)   |
|   - Data store                     |
+------------------------------------+
| Header RPM #2                      |
+------------------------------------+
| Header RPM #3                      |
|   ...                              |
+------------------------------------+
```

Chaque header a exactement le meme format qu'un header extrait d'un fichier .rpm complet.

### Structure d'un header RPM (C struct) -- VALIDE avec parse_hdlist.py

```c
struct rpmHeader {
    uint8_t  magic[3];      // 0x8E 0xAD 0xE8
    uint8_t  version;       // Version (generalement 0x01)
    uint8_t  reserved[4];   // Reserve (zeros)
    uint32_t nindex;        // Nombre d'entrees d'index (big-endian)
    uint32_t hsize;         // Taille du data store en bytes (big-endian)

    // Index entries (nindex x 16 bytes)
    struct {
        uint32_t tag;       // Tag ID (big-endian)
        uint32_t type;      // Type de donnees (big-endian)
        uint32_t offset;    // Offset dans le data store (big-endian)
        uint32_t count;     // Nombre d'elements (big-endian)
    } index[nindex];

    // Data store (hsize bytes)
    uint8_t store[hsize];   // Strings null-terminated, etc.
};
```

### Table des offsets

```
Offset  Taille  Description
------  ------  -----------
0x00    3       Magic: 0x8E 0xAD 0xE8
0x03    1       Version (0x01)
0x04    4       Reserved (0x00 0x00 0x00 0x00)
0x08    4       nindex (big-endian)
0x0C    4       hsize (big-endian)
0x10    ...     Index entries puis data store
```

### Exemple de header (hexdump)

```
Offset    Hex                                          ASCII
--------  -------------------------------------------  ----------------
00000000  8e ad e8 01 00 00 00 00 00 00 00 3f 00 00  ...............?
00000010  1c 10 00 00 03 e8 00 00 00 06 00 00 00 00  ................
00000020  00 00 00 01 00 00 03 e9 00 00 00 06 00 00  ................
...

Decodage :
- Offset 0x00-0x07 : Magic (8E AD E8 01 00 00 00 00)
- Offset 0x08-0x0B : nindex = 0x0000003F (63 entrees)
- Offset 0x0C-0x0F : hsize = 0x00001C10 (7184 bytes de data)
- Offset 0x10+    : Index entries (63 x 16 = 1008 bytes)
- Apres l'index   : Data store (7184 bytes)
```

### Types de donnees RPM

| Type | ID | Description |
|------|-----|-------------|
| NULL | 0 | Pas de donnees |
| CHAR | 1 | char |
| INT8 | 2 | int8_t |
| INT16 | 3 | int16_t |
| INT32 | 4 | int32_t |
| INT64 | 5 | int64_t |
| STRING | 6 | char* (null-terminated) |
| BIN | 7 | Donnees binaires |
| STRING_ARRAY | 8 | char*[] |
| I18NSTRING | 9 | Chaines internationalisees |

### Tags RPM -- reference

#### Tags basiques

```c
#define RPMTAG_NAME           1000  // Nom du package
#define RPMTAG_VERSION        1001  // Version
#define RPMTAG_RELEASE        1002  // Release
#define RPMTAG_EPOCH          1003  // Epoch
#define RPMTAG_SUMMARY        1004  // Description courte
#define RPMTAG_DESCRIPTION    1005  // Description complete
#define RPMTAG_BUILDTIME      1006  // Date de build
#define RPMTAG_SIZE           1009  // Taille installee
#define RPMTAG_DISTRIBUTION   1010  // Distribution
#define RPMTAG_VENDOR         1011  // Vendeur
#define RPMTAG_LICENSE        1014  // Licence
#define RPMTAG_PACKAGER       1015  // Packager
#define RPMTAG_GROUP          1016  // Groupe
#define RPMTAG_URL            1020  // URL du projet
#define RPMTAG_OS             1021  // Systeme d'exploitation
#define RPMTAG_ARCH           1022  // Architecture
```

#### Tags de dependances

```c
#define RPMTAG_PROVIDENAME    1047  // Ce que le package fournit
#define RPMTAG_REQUIREFLAGS   1048  // Flags des dependances
#define RPMTAG_REQUIRENAME    1049  // Dependances requises
#define RPMTAG_REQUIREVERSION 1050  // Versions requises
#define RPMTAG_CONFLICTFLAGS  1053  // Flags des conflits
#define RPMTAG_CONFLICTNAME   1054  // Conflits
#define RPMTAG_OBSOLETENAME   1090  // Packages rendus obsoletes
```

#### Tags de fichiers

```c
#define RPMTAG_FILESIZES      1028  // Tailles des fichiers
#define RPMTAG_FILESTATES     1029  // Etats des fichiers
#define RPMTAG_FILEMODES      1030  // Permissions des fichiers
#define RPMTAG_FILEDIGESTS    1035  // Digests des fichiers
#define RPMTAG_FILELINKTOS    1036  // Cibles des liens symboliques
#define RPMTAG_FILEFLAGS      1037  // Flags des fichiers
#define RPMTAG_FILEUSERNAME   1039  // Proprietaire des fichiers
#define RPMTAG_FILEGROUPNAME  1040  // Groupe des fichiers
#define RPMTAG_BASENAMES      1117  // Noms de fichiers
#define RPMTAG_DIRNAMES       1118  // Noms de repertoires
#define RPMTAG_DIRINDEXES     1119  // Index des repertoires
```

---

## 4. Format synthesis

### Vue d'ensemble

Le fichier synthesis est **beaucoup plus simple** que le hdlist : c'est du texte avec un
format structure utilisant le delimiteur `@`.

### Regle d'ordonnancement

**IMPORTANT** : Les tags (`@provides`, `@requires`, etc.) viennent **AVANT** la ligne `@info`.
La ligne `@info` **termine** la definition d'un paquet.

```
@provides@capability1@capability2[==version]@capability3
@requires@dep1@dep2[>=version]@dep3
@obsoletes@old_pkg1@old_pkg2
@conflicts@conflict1
@summary@Description courte du package
@info@nom-version-release.arch@epoch@size@group   <-- FIN du paquet
```

**Lecture** : Les tags (`@requires`, `@summary`, etc.) sont accumules jusqu'a rencontrer `@info`,
qui marque la fin du paquet et permet de lui associer tous les tags precedents.

### Anatomie de chaque champ

#### Ligne @info@

```
@info@coreutils-6.9-5mdv2008.0.src@0@5553930@System/Base
      +---------------+-----------+  |  +--+-+  +---+---+
                      |              |     |         +-- Groupe RPM
                      |              |     +-- Taille en bytes (5.5 MB)
                      |              +-- Epoch (0 si absent)
                      +-- NEVRA (Name-Epoch-Version-Release.Arch)
```

**Structure exacte** : `@info@NEVRA@epoch@size@group`

#### Ligne @requires@

```
@requires@texinfo[>= 4.3]@automake[== 1.10]@libacl-devel
          +------+-------+ +------+--------+
                 |                 +-- Version egale a 1.10
                 +-- Version >= 4.3
```

#### Ligne @provides@

```
@provides@name1@name2==2.0@name3>=1.5-2@/usr/bin/foo
          ^     ^           ^             ^
          |     |           |             +-- Fichier fourni
          |     |           +-- Avec version et flags
          |     +-- Avec version exacte
          +-- Capability simple
```

#### Ligne @summary@

```
@summary@The GNU core utilities: a set of tools commonly used in shell scripts
```

La description courte (summary) du package.

### Flags de comparaison de version

| Flag | Signification |
|------|---------------|
| `==` | Egalite stricte |
| `>=` | Superieur ou egal |
| `<=` | Inferieur ou egal |
| `>`  | Strictement superieur |
| `<`  | Strictement inferieur |

### Exemple reel extrait d'un synthesis

```
@summary@The basic directory layout for a Linux system
@info@filesystem-2.1.9-1mdv2008.0.src@0@4773@System/Base
@requires@python
@summary@Security Level management for the Mandriva Linux distribution
@info@msec-0.50.3-1mdv2007.1.src@0@152250@System/Base
@summary@The skeleton package which defines a simple Mandriva Linux system
@info@basesystem-2008.0-3mdv2008.0.src@0@7406@System/Base
@requires@ant@java-devel@junit@jpackage-utils@java-gcj-compat-devel
@summary@A parser/scanner generator for java
@info@javacc-4.0-3.5mdv2008.0.src@0@1026460@Development/Java
```

---

## 5. hdlist vs synthesis

| Aspect | hdlist (.cz) | synthesis (.hdlist.cz) |
|--------|-------------|------------------------|
| **Format apres decompression** | Binaire (headers RPM) | Texte (@ tags) |
| **Parsing** | Necessite librpm ou parsing binaire | Simple, grep/awk suffisent |
| **Information** | Complete (tous les tags RPM) | Essentielle (deps, provides, summary, group) |
| **Filelists** | Oui | Non |
| **Changelog** | Oui | Non |
| **Taille decompressee** | 15-50 MB | 500 KB - 2 MB |
| **Rapidite de parsing** | Plus lent | Tres rapide |
| **Use case principal** | Requetes detaillees, urpmf | Resolution de dependances |

C'est pourquoi urpmi utilise le synthesis par defaut et ne charge le hdlist que quand
c'est necessaire (ex : recherche de fichiers).

---

## 6. Exemples de parsing

### 6.1 Python : auto-detection + parser hdlist

Ce script combine l'auto-detection de compression et le parsing du format binaire hdlist.

```python
#!/usr/bin/env python3
"""Parser hdlist — VALIDE sur fichiers Mageia reels"""
import struct
import sys

RPM_HEADER_MAGIC = b'\x8e\xad\xe8'  # 3 bytes seulement !

RPMTAG_NAME = 1000
RPMTAG_VERSION = 1001
RPMTAG_RELEASE = 1002
RPMTAG_ARCH = 1022
RPM_STRING = 6


def decompress_file_binary(filename: str) -> bytes:
    """Decompresse un fichier en mode binaire (auto-detection)."""
    with open(filename, 'rb') as f:
        magic = f.read(8)
        f.seek(0)

        if magic[:4] == b'\x28\xb5\x2f\xfd':  # zstd
            import zstandard as zstd
            dctx = zstd.ZstdDecompressor()
            with dctx.stream_reader(f) as reader:
                return reader.read()
        elif magic[:2] == b'\x1f\x8b':  # gzip
            import gzip
            return gzip.decompress(f.read())
        elif magic[:6] == b'\xfd7zXZ\x00':  # xz
            import lzma
            return lzma.decompress(f.read())
        elif magic[:2] == b'BZ':  # bzip2
            import bz2
            return bz2.decompress(f.read())
        else:
            return f.read()


def read_header(f):
    """Lit un header RPM depuis un flux."""
    magic = f.read(3)
    if not magic or len(magic) < 3 or magic != RPM_HEADER_MAGIC:
        return None

    version = f.read(1)
    reserved = f.read(4)

    nindex = struct.unpack('>I', f.read(4))[0]
    hsize = struct.unpack('>I', f.read(4))[0]

    index = []
    for _ in range(nindex):
        tag = struct.unpack('>I', f.read(4))[0]
        typ = struct.unpack('>I', f.read(4))[0]
        offset = struct.unpack('>I', f.read(4))[0]
        count = struct.unpack('>I', f.read(4))[0]
        index.append((tag, typ, offset, count))

    store = f.read(hsize)
    return {'index': index, 'store': store}


def get_string_from_store(store, offset):
    """Extrait une string null-terminated du store."""
    end = store.find(b'\x00', offset)
    if end == -1:
        return store[offset:].decode('utf-8', errors='replace')
    return store[offset:end].decode('utf-8', errors='replace')


def extract_package_info(hdr):
    """Extrait NAME-VERSION-RELEASE.ARCH d'un header parse."""
    info = {}
    for tag, typ, offset, count in hdr['index']:
        if typ == RPM_STRING:
            if tag == RPMTAG_NAME:
                info['name'] = get_string_from_store(hdr['store'], offset)
            elif tag == RPMTAG_VERSION:
                info['version'] = get_string_from_store(hdr['store'], offset)
            elif tag == RPMTAG_RELEASE:
                info['release'] = get_string_from_store(hdr['store'], offset)
            elif tag == RPMTAG_ARCH:
                info['arch'] = get_string_from_store(hdr['store'], offset)
    return info


def parse_hdlist_binary(filename):
    """Parse un fichier hdlist compresse et compte les packages."""
    data = decompress_file_binary(filename)
    offset = 0
    count = 0
    magic_full = b'\x8e\xad\xe8\x01\x00\x00\x00\x00'

    while offset < len(data):
        if data[offset:offset+8] != magic_full:
            offset += 1
            continue

        nindex = struct.unpack('>I', data[offset+8:offset+12])[0]
        hsize = struct.unpack('>I', data[offset+12:offset+16])[0]
        total_size = 16 + (nindex * 16) + hsize

        count += 1
        offset += total_size

    return count


# Usage: zstdcat hdlist.cz > hdlist.raw && python3 parse_hdlist.py hdlist.raw
# Ou directement : python3 parse_hdlist.py hdlist.cz  (avec auto-detection)
```

### 6.2 Python : parser synthesis

```python
#!/usr/bin/env python3
"""
Parser synthesis — IMPORTANT : les tags precedent @info !
@info marque la FIN d'un paquet, pas le debut.
"""
import sys


def parse_synthesis(filename):
    """Parse un fichier synthesis et retourne une liste de packages."""
    packages = []
    current_tags = {}  # Accumuler les tags jusqu'au prochain @info

    # Utiliser la fonction decompress_file() definie en section 2
    content = decompress_file(filename)

    for line in content.split('\n'):
        line = line.strip()
        if not line or not line.startswith('@'):
            continue

        parts = line.split('@')
        if len(parts) < 2:
            continue

        tag = parts[1]

        if tag == 'info':
            # @info TERMINE le paquet — creer avec les tags accumules
            nevra = parts[2] if len(parts) > 2 else ''
            pkg = {
                'nevra': nevra,
                'epoch': parts[3] if len(parts) > 3 else '0',
                'size': int(parts[4]) if len(parts) > 4 else 0,
                'group': parts[5] if len(parts) > 5 else '',
                'summary': current_tags.get('summary', ''),
                'provides': current_tags.get('provides', []),
                'requires': current_tags.get('requires', []),
                'conflicts': current_tags.get('conflicts', []),
                'obsoletes': current_tags.get('obsoletes', []),
            }
            packages.append(pkg)
            current_tags = {}  # Reset pour le prochain paquet
        else:
            # Accumuler les tags pour le paquet qui suit
            if tag == 'summary':
                current_tags['summary'] = parts[2] if len(parts) > 2 else ''
            elif tag == 'provides':
                current_tags['provides'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'requires':
                current_tags['requires'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'conflicts':
                current_tags['conflicts'] = list(parts[2:]) if len(parts) > 2 else []
            elif tag == 'obsoletes':
                current_tags['obsoletes'] = list(parts[2:]) if len(parts) > 2 else []

    return packages


if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} synthesis.hdlist.cz")
        sys.exit(1)

    packages = parse_synthesis(sys.argv[1])
    print(f"Trouve {len(packages)} packages")
```

### 6.3 C/librpm : parser un hdlist complet

#### Exemple basique

```c
#include <stdio.h>
#include <rpm/rpmlib.h>
#include <rpm/header.h>
#include <rpm/rpmio.h>
#include <rpm/rpmts.h>

int parse_hdlist(const char *filename) {
    FD_t fd;
    Header h;
    int count = 0;

    // Initialiser RPM
    if (rpmReadConfigFiles(NULL, NULL) != 0) {
        fprintf(stderr, "Erreur: impossible de lire la config RPM\n");
        return -1;
    }

    // Ouvrir le fichier hdlist (decompresse)
    fd = Fopen(filename, "r");
    if (fd == NULL || Ferror(fd)) {
        fprintf(stderr, "Erreur: impossible d'ouvrir %s\n", filename);
        return -1;
    }

    // Lire chaque header
    while ((h = headerRead(fd, HEADER_MAGIC_YES)) != NULL) {
        const char *name = NULL;
        const char *version = NULL;
        const char *release = NULL;
        const char *arch = NULL;

        // Extraire les tags basiques
        headerGetString(h, RPMTAG_NAME, &name);
        headerGetString(h, RPMTAG_VERSION, &version);
        headerGetString(h, RPMTAG_RELEASE, &release);
        headerGetString(h, RPMTAG_ARCH, &arch);

        printf("Package: %s-%s-%s.%s\n",
               name ? name : "?",
               version ? version : "?",
               release ? release : "?",
               arch ? arch : "?");

        // Liberer le header
        headerFree(h);
        count++;
    }

    // Fermer le fichier
    Fclose(fd);

    printf("\nTotal: %d packages\n", count);
    return count;
}

int main(int argc, char *argv[]) {
    if (argc != 2) {
        fprintf(stderr, "Usage: %s hdlist_file\n", argv[0]);
        return 1;
    }

    return parse_hdlist(argv[1]) < 0 ? 1 : 0;
}
```

**Compilation** :
```bash
gcc -o parse_hdlist parse_hdlist.c $(pkg-config --cflags --libs rpm)
```

**Utilisation** :
```bash
# D'abord decompresser (zstd = format actuel Mageia)
zstdcat hdlist.cz > hdlist.raw

# Parser
./parse_hdlist hdlist.raw
```

#### Extraction avancee (dependances, fichiers)

```c
#include <stdio.h>
#include <rpm/rpmlib.h>
#include <rpm/header.h>
#include <rpm/rpmio.h>

void print_package_info(Header h) {
    const char *name, *version, *release, *arch, *summary;
    uint64_t size;
    struct rpmtd_s td;

    // Tags simples
    headerGetString(h, RPMTAG_NAME, &name);
    headerGetString(h, RPMTAG_VERSION, &version);
    headerGetString(h, RPMTAG_RELEASE, &release);
    headerGetString(h, RPMTAG_ARCH, &arch);
    headerGetString(h, RPMTAG_SUMMARY, &summary);

    // Tag numerique
    headerGetNumber(h, RPMTAG_SIZE, &size);

    printf("========================================\n");
    printf("Nom: %s\n", name);
    printf("Version: %s-%s\n", version, release);
    printf("Architecture: %s\n", arch);
    printf("Taille: %lu bytes (%.2f MB)\n", size, size / 1024.0 / 1024.0);
    printf("Description: %s\n", summary);

    // Extraire les dependances (array)
    if (headerGet(h, RPMTAG_REQUIRENAME, &td, HEADERGET_MINMEM)) {
        printf("\nDependances:\n");
        const char **requires = (const char **)td.data;
        for (int i = 0; i < td.count; i++) {
            printf("  - %s\n", requires[i]);
        }
        rpmtdFreeData(&td);
    }

    // Extraire les fichiers
    if (headerGet(h, RPMTAG_BASENAMES, &td, HEADERGET_MINMEM)) {
        printf("\nFichiers (%d):\n", td.count);
        const char **files = (const char **)td.data;
        for (int i = 0; i < (td.count < 10 ? td.count : 10); i++) {
            printf("  - %s\n", files[i]);
        }
        if (td.count > 10) {
            printf("  ... et %d autres\n", td.count - 10);
        }
        rpmtdFreeData(&td);
    }
}
```

#### Avec l'outil rpm en ligne de commande

```bash
# Lire un hdlist decompresse avec rpm
zstdcat hdlist.cz > /tmp/hdlist.raw
rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' /tmp/hdlist.raw
```

#### rpm2cpio + cpio (extraction de RPM complet)

```bash
# Pour extraire un RPM complet
rpm2cpio package.rpm | cpio -idmv

# Interroger le header d'un RPM
rpm -qp --queryformat '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' package.rpm
```

#### Avec librpm directement (C, snippet minimal)

```c
#include <rpm/rpmlib.h>
#include <rpm/header.h>
#include <rpm/rpmts.h>

// Ouvrir et lire un header
FD_t fd = Fopen(filename, "r");
Header h;
rpmReadPackageFile(NULL, fd, filename, &h);

// Extraire des tags
const char *name;
headerGetString(h, RPMTAG_NAME, &name);
```

### 6.4 Perl/URPM

```perl
#!/usr/bin/perl
use strict;
use URPM;

my $urpm = URPM->new();

# Parser le hdlist
my ($start, $end) = $urpm->parse_hdlist('hdlist.cz',
    callback => sub {
        my ($urpm, $pkg) = @_;
        printf "%s-%s-%s.%s\n",
            $pkg->name,
            $pkg->version,
            $pkg->release,
            $pkg->arch;
    }
);

print "Parse $end packages\n";
```

### 6.5 Comparaison hdlist/synthesis (script bash)

```bash
#!/bin/bash
# compare_hdlist_synthesis.sh

echo "=== Extraction depuis hdlist ==="
zstdcat hdlist.cz > /tmp/hdlist.raw
python3 parse_hdlist.py /tmp/hdlist.raw | sort > /tmp/hdlist_packages.txt

echo "=== Extraction depuis synthesis ==="
zstdcat synthesis.hdlist.cz | grep '^@info@' | cut -d'@' -f3 | sort > /tmp/synthesis_packages.txt

echo "=== Comparaison ==="
diff /tmp/hdlist_packages.txt /tmp/synthesis_packages.txt
```

### 6.6 Extraction des noms de packages du synthesis (one-liner)

```bash
zstdcat synthesis.hdlist.cz | grep '^@info@' | cut -d'@' -f3 | cut -d'@' -f1
```

---

## 7. Legacy URPM Internals

### Pseudo-code de parse_hdlist__XS

Base sur l'analyse du code URPM, voici le pseudo-code de `parse_hdlist__XS` :

```c
SV *parse_hdlist__XS(char *filename, int packing, SV *callback) {
    FD_t fd;
    Header h;
    int count = 0;

    // Ouvrir le fichier
    fd = open_archive(filename);  // Gere XZ automatiquement

    // Lire chaque header
    while ((h = headerRead(fd, HEADER_MAGIC_YES)) != NULL) {
        // Creer un objet URPM::Package
        SV *pkg = create_package_from_header(h);

        // Appeler le callback Perl si fourni
        if (callback) {
            call_perl_callback(callback, pkg);
        }

        // Stocker dans $urpm->{depslist}
        add_to_depslist(pkg);

        // Si packing, compresser les donnees en memoire
        if (packing) {
            pack_header(pkg);
        }

        headerFree(h);
        count++;
    }

    Fclose(fd);
    return count;
}
```

### Chaine d'appels XS

```
Perl: URPM::parse_hdlist($filename, %options)
  -> XS: parse_hdlist__XS(filename, packing, callback)
       -> C: open_archive(filename)     -- gere la decompression
       -> C: headerRead(fd, HEADER_MAGIC_YES)  -- boucle sur chaque header
            -> C: headerGet(h, TAG)     -- extrait les tags necessaires
            -> C: create_package_from_header(h)
            -> Perl: callback($pkg)     -- si fourni
       -> C: add_to_depslist(pkg)       -- stocke dans $urpm->{depslist}
       -> C: headerFree(h)
```

---

## 8. Troubleshooting

### Probleme : La decompression echoue

```bash
# Verifier le format reel
file hdlist.cz
# Devrait afficher: "Zstandard compressed data" (format actuel Mageia)

# Decompresser avec zstd
zstdcat hdlist.cz > hdlist.raw

# Si c'est un autre format, adapter la commande :
# xzcat (xz), zcat (gzip), bzcat (bzip2)
```

### Probleme : Format inconnu

```bash
# Identifier par magic bytes
xxd hdlist.cz | head -1
# 28 b5 2f fd = zstd
# fd 37 7a 58 = xz
# 1f 8b       = gzip
# 42 5a       = bzip2
```

### Probleme : Impossible de parser le hdlist

```bash
# Verifier qu'on a bien des headers RPM apres decompression
zstdcat hdlist.cz | hexdump -C | head -20
# Chercher le magic: 8e ad e8 01
```

---

## 9. Ressources

- **Format RPM officiel** : https://rpm-software-management.github.io/rpm/manual/format.html
- **librpm API** : http://ftp.rpm.org/api/
- **rpm.org documentation** : http://ftp.rpm.org/api/4.4.2.2/
- **Maximum RPM book** : http://ftp.rpm.org/max-rpm/
- **Source URPM (Perl)** : https://github.com/OpenMandrivaSoftware/perl-URPM
- **RPM tags (header local)** : `/usr/include/rpm/rpmtag.h`

---

**Note** : Pour une retro-ingenierie complete du parsing legacy, le mieux est d'analyser le
code source de URPM.xs qui contient toute la logique de parsing en C avec les appels a librpm.
