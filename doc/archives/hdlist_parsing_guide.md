# Parsing des fichiers hdlist.cz

> **VALIDÉ** : Le format hdlist a été vérifié avec `parse_hdlist.py` sur de vrais fichiers Mageia.
> La structure décrite ci-dessous est confirmée.

## Vue d'ensemble

Les fichiers **hdlist** (header list) contiennent une succession de **headers RPM complets** extraits des packages. URPM utilise **librpm** pour les parser, ce qui est beaucoup plus fiable que de parser le format binaire manuellement.

## Architecture du parsing dans URPM

```
hdlist.cz (zstd compressé - format actuel Mageia)
    │
    ├─ zstdcat → hdlist (raw)
    │
    ├─ URPM::parse_hdlist() (Perl)
    │
    ├─ parse_hdlist__XS() (binding XS)
    │
    └─ Code C utilisant librpm
         ├─ Fdopen() : ouvrir le fichier
         ├─ headerRead() : lire chaque header
         ├─ headerGet() : extraire les tags
         └─ headerFree() : libérer la mémoire
```

## Structure d'un fichier hdlist

Un hdlist décompressé est une **concaténation de headers RPM** :

```
┌────────────────────────────────────┐
│ Header RPM #1                      │
│   - Magic: 0x8EADE801              │
│   - Index entries (N × 16 bytes)   │
│   - Data store                     │
├────────────────────────────────────┤
│ Header RPM #2                      │
├────────────────────────────────────┤
│ Header RPM #3                      │
│   ...                              │
└────────────────────────────────────┘
```

Chaque header a exactement le même format qu'un header extrait d'un fichier .rpm complet.

## Format d'un header RPM (détaillé)

### Structure générale (VALIDÉ avec parse_hdlist.py)

```c
struct rpmHeader {
    uint8_t  magic[3];      // 0x8E 0xAD 0xE8
    uint8_t  version;       // Version (généralement 0x01)
    uint8_t  reserved[4];   // Réservé (zéros)
    uint32_t nindex;        // Nombre d'entrées d'index (big-endian)
    uint32_t hsize;         // Taille du data store en bytes (big-endian)

    // Index entries (nindex × 16 bytes)
    struct {
        uint32_t tag;       // Tag ID (big-endian)
        uint32_t type;      // Type de données (big-endian)
        uint32_t offset;    // Offset dans le data store (big-endian)
        uint32_t count;     // Nombre d'éléments (big-endian)
    } index[nindex];

    // Data store (hsize bytes)
    uint8_t store[hsize];   // Strings null-terminated, etc.
};
```

### Lecture du header magic

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

Décodage :
- Offset 0x00-0x07 : Magic (8E AD E8 01 00 00 00 00)
- Offset 0x08-0x0B : nindex = 0x0000003F (63 entrées)
- Offset 0x0C-0x0F : hsize = 0x00001C10 (7184 bytes de data)
- Offset 0x10+    : Index entries (63 × 16 = 1008 bytes)
- Après l'index   : Data store (7184 bytes)
```

## Utilisation de librpm pour parser un hdlist

### Méthode 1 : En C avec librpm (recommandé)

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
    
    // Ouvrir le fichier hdlist (décompressé)
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
        
        // Libérer le header
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
# D'abord décompresser (zstd = format actuel Mageia)
zstdcat hdlist.cz > hdlist.raw

# Parser
./parse_hdlist hdlist.raw
```

### Méthode 2 : Avec l'outil rpm en ligne de commande

```bash
# Lire un hdlist décompressé avec rpm
zstdcat hdlist.cz > /tmp/hdlist.raw
rpm -qp --qf '%{NAME}-%{VERSION}-%{RELEASE}.%{ARCH}\n' /tmp/hdlist.raw
```

### Méthode 3 : Script Python (VALIDÉ)

Voir `parse_hdlist.py` dans le projet - testé et fonctionnel sur vrais fichiers Mageia.

```python
#!/usr/bin/env python3
"""Parser hdlist - VALIDÉ sur fichiers Mageia réels"""
import struct
import sys

RPM_HEADER_MAGIC = b'\x8e\xad\xe8'  # 3 bytes seulement !

RPMTAG_NAME = 1000
RPMTAG_VERSION = 1001
RPMTAG_RELEASE = 1002
RPMTAG_ARCH = 1022
RPM_STRING = 6

def read_header(f):
    """Lit un header RPM depuis un flux"""
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
    """Extrait une string null-terminated du store"""
    end = store.find(b'\x00', offset)
    if end == -1:
        return store[offset:].decode('utf-8', errors='replace')
    return store[offset:end].decode('utf-8', errors='replace')

def extract_package_info(hdr):
    """Extrait NAME-VERSION-RELEASE.ARCH"""
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

# Usage: zstdcat hdlist.cz > hdlist.raw && python3 parse_hdlist.py hdlist.raw
```

## Tags RPM importants pour l'analyse

### Tags basiques

```c
#define RPMTAG_NAME           1000  // Nom du package
#define RPMTAG_VERSION        1001  // Version
#define RPMTAG_RELEASE        1002  // Release
#define RPMTAG_EPOCH          1003  // Epoch
#define RPMTAG_SUMMARY        1004  // Description courte
#define RPMTAG_DESCRIPTION    1005  // Description complète
#define RPMTAG_SIZE           1009  // Taille installée
#define RPMTAG_LICENSE        1014  // Licence
#define RPMTAG_GROUP          1016  // Groupe
#define RPMTAG_URL            1020  // URL du projet
#define RPMTAG_ARCH           1022  // Architecture
```

### Tags de dépendances

```c
#define RPMTAG_PROVIDENAME    1047  // Ce que le package fournit
#define RPMTAG_REQUIREFLAGS   1048  // Flags des dépendances
#define RPMTAG_REQUIRENAME    1049  // Dépendances requises
#define RPMTAG_REQUIREVERSION 1050  // Versions requises
#define RPMTAG_CONFLICTNAME   1054  // Conflits
#define RPMTAG_OBSOLETENAME   1090  // Packages rendus obsolètes
```

### Tags de fichiers

```c
#define RPMTAG_BASENAMES      1117  // Noms de fichiers
#define RPMTAG_DIRNAMES       1118  // Noms de répertoires
#define RPMTAG_DIRINDEXES     1119  // Index des répertoires
#define RPMTAG_FILESIZES      1028  // Tailles des fichiers
#define RPMTAG_FILEMODES      1030  // Permissions des fichiers
```

## Exemple d'extraction avancée avec librpm

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
    
    // Tag numérique
    headerGetNumber(h, RPMTAG_SIZE, &size);
    
    printf("========================================\n");
    printf("Nom: %s\n", name);
    printf("Version: %s-%s\n", version, release);
    printf("Architecture: %s\n", arch);
    printf("Taille: %lu bytes (%.2f MB)\n", size, size / 1024.0 / 1024.0);
    printf("Description: %s\n", summary);
    
    // Extraire les dépendances (array)
    if (headerGet(h, RPMTAG_REQUIRENAME, &td, HEADERGET_MINMEM)) {
        printf("\nDépendances:\n");
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

## Différences clés hdlist vs synthesis

| Aspect | hdlist | synthesis |
|--------|--------|-----------|
| **Format** | Binaire (headers RPM) | Texte (@ tags) |
| **Parser** | librpm requis | grep/awk suffit |
| **Taille** | 15-50 MB décompressé | 500 KB - 2 MB |
| **Contenu** | Headers RPM complets | Métadonnées essentielles |
| **Filelists** | ✓ Oui | ✗ Non |
| **Changelog** | ✓ Oui | ✗ Non |
| **Rapidité** | Plus lent à parser | Très rapide |

## Comment URPM.xs parse réellement

Basé sur l'analyse du code URPM, voici le pseudo-code de `parse_hdlist__XS` :

```c
SV *parse_hdlist__XS(char *filename, int packing, SV *callback) {
    FD_t fd;
    Header h;
    int count = 0;
    
    // Ouvrir le fichier
    fd = open_archive(filename);  // Gère XZ automatiquement
    
    // Lire chaque header
    while ((h = headerRead(fd, HEADER_MAGIC_YES)) != NULL) {
        // Créer un objet URPM::Package
        SV *pkg = create_package_from_header(h);
        
        // Appeler le callback Perl si fourni
        if (callback) {
            call_perl_callback(callback, pkg);
        }
        
        // Stocker dans $urpm->{depslist}
        add_to_depslist(pkg);
        
        // Si packing, compresser les données en mémoire
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

## Outils pratiques

### Comparer hdlist et synthesis

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

### Parser avec Perl et URPM

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

print "Parsé $end packages\n";
```

## Ressources

- **librpm documentation** : http://ftp.rpm.org/api/
- **Format RPM** : https://rpm-software-management.github.io/rpm/manual/format.html
- **URPM source** : https://github.com/OpenMandrivaSoftware/perl-URPM
- **RPM tags** : `/usr/include/rpm/rpmtag.h`

## Conclusion

Pour parser un hdlist :
1. **Décompresser** avec `xzcat`
2. **Utiliser librpm** (headerRead, headerGet) - c'est la méthode d'URPM
3. Ou utiliser `rpm -qp` en ligne de commande
4. Le format est binaire mais bien documenté et stable

Le hdlist est beaucoup plus riche que le synthesis mais aussi plus lourd à parser. C'est pourquoi urpmi utilise le synthesis par défaut et ne charge le hdlist que quand nécessaire.