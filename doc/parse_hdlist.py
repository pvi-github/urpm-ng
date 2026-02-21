#!/usr/bin/env python3
"""
Exemple pédagogique : parsing du format hdlist.cz

Ce script montre comment lire les headers RPM contenus dans un fichier
hdlist.cz (format Mageia/Mandriva). Il est destiné aux développeurs
souhaitant comprendre le format binaire des métadonnées RPM.

Le format hdlist.cz contient une suite de headers RPM compressés.
Chaque header suit la structure standard RPM :
  - Magic: 0x8eade8
  - Version: 1 octet
  - Reserved: 4 octets
  - nindex: nombre d'entrées (4 octets big-endian)
  - hsize: taille des données (4 octets big-endian)
  - index[]: tableau de (tag, type, offset, count)
  - data[]: données brutes

Usage:
    python3 parse_hdlist.py /var/lib/urpmi/hdlist.*.cz

Voir aussi: urpm/core/synthesis.py pour le parsing du format synthesis
(métadonnées légères) utilisé en production.
"""
import struct
import sys

RPM_HEADER_MAGIC = b'\x8e\xad\xe8'

RPMTAG_NAME = 1000
RPMTAG_VERSION = 1001
RPMTAG_RELEASE = 1002
RPMTAG_ARCH = 1022

RPM_STRING = 6

def read_header(f):
    """Lit un header RPM depuis un flux"""
    magic = f.read(3)
    
    if not magic or len(magic) < 3:
        return None
    
    if magic != RPM_HEADER_MAGIC:
        # Pas un header RPM, probablement le trailer
        return None
    
    _version = f.read(1)
    _reserved = f.read(4)
    
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
        if tag == RPMTAG_NAME and typ == RPM_STRING:
            info['name'] = get_string_from_store(hdr['store'], offset)
        elif tag == RPMTAG_VERSION and typ == RPM_STRING:
            info['version'] = get_string_from_store(hdr['store'], offset)
        elif tag == RPMTAG_RELEASE and typ == RPM_STRING:
            info['release'] = get_string_from_store(hdr['store'], offset)
        elif tag == RPMTAG_ARCH and typ == RPM_STRING:
            info['arch'] = get_string_from_store(hdr['store'], offset)
    
    return info

def main(hdlist_file):
    count = 0
    with open(hdlist_file, 'rb') as f:
        while True:
            hdr = read_header(f)
            if hdr is None:
                break
            
            count += 1
            info = extract_package_info(hdr)
            
            if all(k in info for k in ['name', 'version', 'release', 'arch']):
                print(f"{info['name']}-{info['version']}-{info['release']}.{info['arch']}")
            else:
                print(f"# Package {count}: incomplete info {info}", file=sys.stderr)
    
    print(f"# Total packages: {count}", file=sys.stderr)

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 parse_hdlist.py <hdlist_file>", file=sys.stderr)
        sys.exit(1)
    
    main(sys.argv[1])
