# Guide d'extraction manuelle des fichiers URPM

## 1. La compression : C'est du zstd !

### Format de compression

**IMPORTANT** : Malgré l'extension `.cz`, ces fichiers utilisent la compression **zstd** (Zstandard).

Le PoC gère aussi d'autres formats (xz, gzip, bzip2) par robustesse, mais le format actuel de Mageia est **zstd**.

```bash
# Vérifier le type réel du fichier :
file synthesis.hdlist.cz
# Output: Zstandard compressed data

# Décompresser avec zstd :
zstdcat synthesis.hdlist.cz > synthesis.txt
zstdcat hdlist.cz > hdlist.raw
```

### Solution Python avec auto-détection (RECOMMANDÉ)

```python
#!/usr/bin/env python3
"""Auto-détection du format - extrait du PoC urpmi_ng_prototype.py"""

def decompress_file(filename: str) -> str:
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
```

### Pourquoi zstd ?

**zstd (Zstandard)** offre :
- **Excellent compromis** compression/vitesse de décompression
- Plus rapide que xz à décompresser
- Meilleure compression que gzip
- Standard moderne (Facebook, kernel Linux, etc.)

**Historique** :
- Anciennes versions : gzip (d'où le `.cz` = compressed)
- Versions intermédiaires : xz pour meilleure compression
- Versions actuelles : zstd pour le meilleur compromis
- L'extension `.cz` a été conservée pour compatibilité

## 2. Structure de hdlist (décompressé)

### Format : Succession de headers RPM binaires

Le fichier décompressé est une **concaténation de headers RPM** :

```
┌─────────────────────────────────┐
│ Header RPM 1 (format binaire)  │  ← Structure rpmHeader
│   Magic: 0x8EADE801             │
│   Index entries                 │
│   Data store                    │
├─────────────────────────────────┤
│ Header RPM 2                    │
├─────────────────────────────────┤
│ Header RPM 3                    │
└─────────────────────────────────┘
```

### Structure d'un header RPM (format RPM v3/v4) - VALIDÉ

```
Offset  Size  Description
------  ----  -----------
0x00    3     Magic: 0x8E 0xAD 0xE8
0x03    1     Version (0x01)
0x04    4     Reserved (zéros)
0x08    4     Number of index entries (N) - big-endian
0x0C    4     Size of data store (S bytes) - big-endian

--- Index Section (16 bytes × N entries) ---
Pour chaque entrée (big-endian) :
  +0    4     Tag ID
  +4    4     Type
  +8    4     Offset dans data store
  +12   4     Count

--- Data Store (S bytes) ---
  Les valeurs (strings null-terminated, etc.)
```

### Types de données RPM

| Type | ID | Description |
|------|-----|-------------|
| NULL | 0 | Pas de données |
| CHAR | 1 | char |
| INT8 | 2 | int8_t |
| INT16 | 3 | int16_t |
| INT32 | 4 | int32_t |
| INT64 | 5 | int64_t |
| STRING | 6 | char* (null-terminated) |
| BIN | 7 | Données binaires |
| STRING_ARRAY | 8 | char*[] |
| I18NSTRING | 9 | Chaînes internationalisées |

### Tags RPM importants

```c
#define RPMTAG_NAME           1000
#define RPMTAG_VERSION        1001
#define RPMTAG_RELEASE        1002
#define RPMTAG_EPOCH          1003
#define RPMTAG_SUMMARY        1004
#define RPMTAG_DESCRIPTION    1005
#define RPMTAG_BUILDTIME      1006
#define RPMTAG_SIZE           1009
#define RPMTAG_DISTRIBUTION   1010
#define RPMTAG_VENDOR         1011
#define RPMTAG_LICENSE        1014
#define RPMTAG_PACKAGER       1015
#define RPMTAG_GROUP          1016
#define RPMTAG_URL            1020
#define RPMTAG_OS             1021
#define RPMTAG_ARCH           1022

#define RPMTAG_FILESIZES      1028
#define RPMTAG_FILESTATES     1029
#define RPMTAG_FILEMODES      1030
#define RPMTAG_FILEDIGESTS    1035
#define RPMTAG_FILELINKTOS    1036
#define RPMTAG_FILEFLAGS      1037
#define RPMTAG_FILEUSERNAME   1039
#define RPMTAG_FILEGROUPNAME  1040
#define RPMTAG_BASENAMES      1117
#define RPMTAG_DIRNAMES       1118
#define RPMTAG_DIRINDEXES     1119

#define RPMTAG_PROVIDENAME    1047
#define RPMTAG_REQUIREFLAGS   1048
#define RPMTAG_REQUIRENAME    1049
#define RPMTAG_REQUIREVERSION 1050
#define RPMTAG_CONFLICTFLAGS  1053
#define RPMTAG_CONFLICTNAME   1054
#define RPMTAG_OBSOLETENAME   1090
```

## 3. Structure de synthesis (décompressé)

### Format : Texte structuré avec délimiteurs @

Le fichier synthesis est **beaucoup plus simple** : c'est du texte avec un format spécial.

**IMPORTANT** : Les tags (@provides, @requires, etc.) viennent **AVANT** la ligne @info.
La ligne @info **termine** la définition d'un paquet.

```
@provides@capability1@capability2[==version]@capability3
@requires@dep1@dep2[>=version]@dep3
@obsoletes@old_pkg1@old_pkg2
@conflicts@conflict1
@summary@Description courte du package
@info@nom-version-release.arch@epoch@size@group   ← FIN du paquet
```

### Exemple RÉEL extrait d'un synthesis

Structure réelle : tous les tags d'un paquet précèdent sa ligne @info.

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

**Lecture** : Les tags (@requires, @summary, etc.) sont accumulés jusqu'à rencontrer @info,
qui marque la fin du paquet et permet de lui associer tous les tags précédents.

### Format détaillé de chaque ligne

#### Ligne @info@
```
@info@coreutils-6.9-5mdv2008.0.src@0@5553930@System/Base
      └────────┬──────────────┘  │ └──┬──┘ └───┬────┘
               │                 │    │        └─ Groupe RPM
               │                 │    └─ Taille en bytes (5.5 MB)
               │                 └─ Epoch (0 si absent)
               └─ NEVRA (Name-Epoch-Version-Release.Arch)
```

**Structure exacte** : `@info@NEVRA@epoch@size@group`

#### Ligne @requires@
```
@requires@texinfo[>= 4.3]@automake[== 1.10]@libacl-devel
          └────────┬──────┘ └──────┬────────┘
                   │                └─ Version égale à 1.10
                   └─ Version >= 4.3
```

#### Ligne @provides@
```
@provides@name1@name2==2.0@name3>=1.5-2@/usr/bin/foo
          ^     ^           ^             ^
          |     |           |             └─ Fichier fourni
          |     |           └─ Avec version et flags
          |     └─ Avec version exacte
          └─ Capability simple
```

#### Ligne @summary@
```
@summary@The GNU core utilities: a set of tools commonly used in shell scripts
```

La description courte (summary) du package.

### Flags de comparaison

Les flags de version utilisés :
- `==` : égalité stricte
- `>=` : supérieur ou égal
- `<=` : inférieur ou égal
- `>` : strictement supérieur
- `<` : strictement inférieur

## 4. Scripts d'extraction pratiques

### Extraire les noms de packages du synthesis

```bash
#!/bin/bash
# extract_packages.sh

zcat synthesis.hdlist.cz | \
    grep '^@info@' | \
    cut -d'@' -f3 | \
    cut -d'@' -f1
```

### Parser le synthesis en Python

```python
#!/usr/bin/env python3
"""
Parser synthesis - IMPORTANT : les tags précèdent @info !
@info marque la FIN d'un paquet, pas le début.
"""
import sys

def parse_synthesis(filename):
    """Parse un fichier synthesis et retourne une liste de packages"""
    packages = []
    current_tags = {}  # Accumuler les tags jusqu'au prochain @info

    # Utiliser la fonction decompress_file() définie plus haut
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
            # @info TERMINE le paquet - créer avec les tags accumulés
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
    print(f"Trouvé {len(packages)} packages")
```

### Parser le hdlist avec Python

```python
#!/usr/bin/env python3
"""Parser hdlist.cz - headers RPM binaires"""
import struct
import sys

RPM_HEADER_MAGIC = b'\x8e\xad\xe8\x01\x00\x00\x00\x00'

def decompress_file_binary(filename: str) -> bytes:
    """Décompresse un fichier en mode binaire (auto-détection)"""
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

def parse_hdlist(filename):
    """Parse un fichier hdlist et compte les packages"""
    data = decompress_file_binary(filename)
    offset = 0
    count = 0

    while offset < len(data):
        if data[offset:offset+8] != RPM_HEADER_MAGIC:
            offset += 1
            continue

        # Lire taille du header
        nindex = struct.unpack('>I', data[offset+8:offset+12])[0]
        hsize = struct.unpack('>I', data[offset+12:offset+16])[0]
        total_size = 16 + (nindex * 16) + hsize

        count += 1
        offset += total_size

    return count

if __name__ == '__main__':
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} hdlist.cz")
        sys.exit(1)
    print(f"Nombre de packages: {parse_hdlist(sys.argv[1])}")
```

## 5. Outils recommandés

### rpm2cpio + cpio
```bash
# Pour extraire un RPM complet
rpm2cpio package.rpm | cpio -idmv
```

### rpm --query
```bash
# Interroger le header d'un RPM
rpm -qp --queryformat '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' package.rpm
```

### Avec librpm directement (C)
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

## 6. Différences clés à retenir

| Aspect | hdlist.cz | synthesis.hdlist.cz |
|--------|-----------|---------------------|
| **Format après décompression** | Binaire (headers RPM) | Texte (@ tags) |
| **Parsing** | Complexe, nécessite librpm | Simple, grep/awk suffisent |
| **Information complète** | Oui (tous les tags) | Non (tags essentiels) |
| **Taille décompressée** | 15-50 MB | 500 KB - 2 MB |
| **Use case** | Requêtes détaillées | Résolution dépendances |

## 7. Troubleshooting

### Problème : La décompression échoue
```bash
# Vérifier le format réel
file hdlist.cz
# Devrait afficher: "Zstandard compressed data" (format actuel Mageia)

# Décompresser avec zstd
zstdcat hdlist.cz > hdlist.raw

# Si c'est un autre format, adapter la commande :
# xzcat (xz), zcat (gzip), bzcat (bzip2)
```

### Problème : Format inconnu
```bash
# Identifier par magic bytes
xxd hdlist.cz | head -1
# 28 b5 2f fd = zstd
# fd 37 7a 58 = xz
# 1f 8b       = gzip
# 42 5a       = bzip2
```

### Problème : Impossible de parser le hdlist
```bash
# Vérifier qu'on a bien des headers RPM après décompression
zstdcat hdlist.cz | hexdump -C | head -20
# Chercher le magic: 8e ad e8 01
```

## 8. Ressources additionnelles

- **Format RPM officiel**: https://rpm-software-management.github.io/rpm/manual/format.html
- **Source URPM**: https://github.com/OpenMandrivaSoftware/perl-URPM
- **rpm.org documentation**: http://ftp.rpm.org/api/4.4.2.2/
- **Maximum RPM book**: http://ftp.rpm.org/max-rpm/

---

**Note**: Pour une rétro-ingénierie complète, le mieux est d'analyser le code source de URPM.xs qui contient toute la logique de parsing en C avec les appels à librpm.