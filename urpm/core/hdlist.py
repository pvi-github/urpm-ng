"""
HDList parser for urpm

Parses hdlist.cz files containing concatenated RPM headers.
Format validated with real Mageia files.
"""

import struct
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, List, Optional, Any

from .compression import decompress_stream

# RPM Header magic (3 bytes)
RPM_HEADER_MAGIC = b'\x8e\xad\xe8'

# RPM Tag IDs
RPMTAG_NAME = 1000
RPMTAG_VERSION = 1001
RPMTAG_RELEASE = 1002
RPMTAG_EPOCH = 1003
RPMTAG_SUMMARY = 1004
RPMTAG_DESCRIPTION = 1005
RPMTAG_BUILDTIME = 1006
RPMTAG_SIZE = 1009
RPMTAG_LICENSE = 1014
RPMTAG_GROUP = 1016
RPMTAG_URL = 1020
RPMTAG_ARCH = 1022
RPMTAG_FILESIZES = 1028
RPMTAG_FILEMODES = 1030
RPMTAG_BASENAMES = 1117
RPMTAG_DIRNAMES = 1118
RPMTAG_DIRINDEXES = 1119
RPMTAG_PROVIDENAME = 1047
RPMTAG_PROVIDEVERSION = 1113
RPMTAG_PROVIDEFLAGS = 1112
RPMTAG_REQUIRENAME = 1049
RPMTAG_REQUIREVERSION = 1050
RPMTAG_REQUIREFLAGS = 1048
RPMTAG_CONFLICTNAME = 1054
RPMTAG_CONFLICTVERSION = 1055
RPMTAG_CONFLICTFLAGS = 1053
RPMTAG_OBSOLETENAME = 1090
RPMTAG_OBSOLETEVERSION = 1115
RPMTAG_OBSOLETEFLAGS = 1114
RPMTAG_RECOMMENDNAME = 5046
RPMTAG_SUGGESTNAME = 5049

# RPM Data types
RPM_NULL = 0
RPM_CHAR = 1
RPM_INT8 = 2
RPM_INT16 = 3
RPM_INT32 = 4
RPM_INT64 = 5
RPM_STRING = 6
RPM_BIN = 7
RPM_STRING_ARRAY = 8
RPM_I18NSTRING = 9


class RPMHeader:
    """Represents a parsed RPM header."""
    
    def __init__(self, index: List[tuple], store: bytes):
        self.index = index  # List of (tag, type, offset, count)
        self.store = store  # Raw data store
        self._cache: Dict[int, Any] = {}
    
    def get_string(self, tag: int) -> Optional[str]:
        """Get a string tag value."""
        if tag in self._cache:
            return self._cache[tag]
        
        for t, typ, offset, count in self.index:
            if t == tag and typ == RPM_STRING:
                end = self.store.find(b'\x00', offset)
                if end == -1:
                    value = self.store[offset:].decode('utf-8', errors='replace')
                else:
                    value = self.store[offset:end].decode('utf-8', errors='replace')
                self._cache[tag] = value
                return value
        return None
    
    def get_int32(self, tag: int) -> Optional[int]:
        """Get an int32 tag value."""
        if tag in self._cache:
            return self._cache[tag]
        
        for t, typ, offset, count in self.index:
            if t == tag and typ == RPM_INT32:
                value = struct.unpack('>I', self.store[offset:offset+4])[0]
                self._cache[tag] = value
                return value
        return None
    
    def get_string_array(self, tag: int) -> List[str]:
        """Get a string array tag value."""
        if tag in self._cache:
            return self._cache[tag]
        
        for t, typ, offset, count in self.index:
            if t == tag and typ == RPM_STRING_ARRAY:
                strings = []
                pos = offset
                for _ in range(count):
                    end = self.store.find(b'\x00', pos)
                    if end == -1:
                        strings.append(self.store[pos:].decode('utf-8', errors='replace'))
                        break
                    strings.append(self.store[pos:end].decode('utf-8', errors='replace'))
                    pos = end + 1
                self._cache[tag] = strings
                return strings
        return []
    
    def get_int32_array(self, tag: int) -> List[int]:
        """Get an int32 array tag value."""
        if tag in self._cache:
            return self._cache[tag]
        
        for t, typ, offset, count in self.index:
            if t == tag and typ == RPM_INT32:
                values = []
                for i in range(count):
                    val = struct.unpack('>I', self.store[offset+i*4:offset+i*4+4])[0]
                    values.append(val)
                self._cache[tag] = values
                return values
        return []
    
    @property
    def name(self) -> str:
        return self.get_string(RPMTAG_NAME) or ''
    
    @property
    def version(self) -> str:
        return self.get_string(RPMTAG_VERSION) or ''
    
    @property
    def release(self) -> str:
        return self.get_string(RPMTAG_RELEASE) or ''
    
    @property
    def epoch(self) -> int:
        return self.get_int32(RPMTAG_EPOCH) or 0
    
    @property
    def arch(self) -> str:
        return self.get_string(RPMTAG_ARCH) or 'noarch'
    
    @property
    def summary(self) -> str:
        return self.get_string(RPMTAG_SUMMARY) or ''
    
    @property
    def description(self) -> str:
        return self.get_string(RPMTAG_DESCRIPTION) or ''
    
    @property
    def group(self) -> str:
        return self.get_string(RPMTAG_GROUP) or ''
    
    @property
    def size(self) -> int:
        return self.get_int32(RPMTAG_SIZE) or 0
    
    @property
    def url(self) -> str:
        return self.get_string(RPMTAG_URL) or ''
    
    @property
    def license(self) -> str:
        return self.get_string(RPMTAG_LICENSE) or ''
    
    @property
    def nevra(self) -> str:
        """Full Name-Epoch-Version-Release.Arch string."""
        if self.epoch:
            return f"{self.name}-{self.epoch}:{self.version}-{self.release}.{self.arch}"
        return f"{self.name}-{self.version}-{self.release}.{self.arch}"
    
    @property
    def provides(self) -> List[str]:
        return self.get_string_array(RPMTAG_PROVIDENAME)
    
    @property
    def requires(self) -> List[str]:
        return self.get_string_array(RPMTAG_REQUIRENAME)
    
    @property
    def conflicts(self) -> List[str]:
        return self.get_string_array(RPMTAG_CONFLICTNAME)
    
    @property
    def obsoletes(self) -> List[str]:
        return self.get_string_array(RPMTAG_OBSOLETENAME)
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for storage."""
        return {
            'name': self.name,
            'version': self.version,
            'release': self.release,
            'epoch': self.epoch,
            'arch': self.arch,
            'nevra': self.nevra,
            'summary': self.summary,
            'description': self.description,
            'group': self.group,
            'size': self.size,
            'url': self.url,
            'license': self.license,
            'provides': self.provides,
            'requires': self.requires,
            'conflicts': self.conflicts,
            'obsoletes': self.obsoletes,
        }


def read_header(f: BinaryIO) -> Optional[RPMHeader]:
    """Read a single RPM header from a binary stream.
    
    Args:
        f: Binary file stream positioned at header start
        
    Returns:
        RPMHeader object or None if no more headers
    """
    magic = f.read(3)
    
    if not magic or len(magic) < 3:
        return None
    
    if magic != RPM_HEADER_MAGIC:
        return None
    
    # Skip version (1 byte) and reserved (4 bytes)
    f.read(5)
    
    # Read index count and data store size
    nindex = struct.unpack('>I', f.read(4))[0]
    hsize = struct.unpack('>I', f.read(4))[0]
    
    # Read index entries
    index = []
    for _ in range(nindex):
        tag = struct.unpack('>I', f.read(4))[0]
        typ = struct.unpack('>I', f.read(4))[0]
        offset = struct.unpack('>I', f.read(4))[0]
        count = struct.unpack('>I', f.read(4))[0]
        index.append((tag, typ, offset, count))
    
    # Read data store
    store = f.read(hsize)
    
    return RPMHeader(index, store)


def parse_hdlist(filename: Path) -> Iterator[RPMHeader]:
    """Parse an hdlist file and yield RPM headers.
    
    Args:
        filename: Path to hdlist.cz file (compressed or raw)
        
    Yields:
        RPMHeader objects for each package
    """
    with decompress_stream(filename) as f:
        while True:
            header = read_header(f)
            if header is None:
                break
            yield header


def parse_hdlist_to_list(filename: Path) -> List[Dict[str, Any]]:
    """Parse an hdlist file and return list of package dicts.
    
    Args:
        filename: Path to hdlist.cz file
        
    Returns:
        List of package dictionaries
    """
    return [hdr.to_dict() for hdr in parse_hdlist(filename)]
