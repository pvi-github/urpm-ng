"""
HDList parser for urpm

Parses hdlist.cz files containing concatenated RPM headers.
Format validated with real Mageia files.
"""

import struct
import os
import gzip
import lzma
import json
from pathlib import Path
from typing import BinaryIO, Dict, Iterator, List, Optional, Any, IO
from .compression import decompress_stream
from struct import pack

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


# ─── Write API (used by urpm.genmedia) ────────────────────────────


def write_hdlist(
    output_path: Path,
    packages,
    *,
    compression_filter: str = 'gzip',
    compression_level: int = 9,
    block_size: int = 400 * 1024,
    incremental: bool = False,
    old_hdlist_path: Optional[Path] = None,
) -> int:
    """Write an hdlist.cz archive from RPM metadata.

    Each package's ``header_bytes`` (raw ``hdr.unload()`` output) is
    accumulated into blocks of *block_size* bytes.  Blocks are compressed
    individually with *compression_filter*.  A table of contents (TOC) is
    appended at the end of the file so readers can seek to individual
    headers.

    In incremental mode, blocks whose every member is unchanged (same
    ``header_sha256``) are copied verbatim from *old_hdlist_path*.

    Args:
        output_path: Destination file (e.g. ``media_info/tmp/hdlist.cz``).
        packages: Iterable of :class:`~urpm.genmedia.RpmMetadata`.
        compression_filter: Compressor, e.g. 'gzip'.
        compression_level: level, e.g. 9.
        block_size: Maximum uncompressed bytes per block.
        incremental: If True, reuse unchanged blocks from *old_hdlist_path*.
        old_hdlist_path: Path to the previous hdlist (required when
            *incremental* is True).

    Returns:
        Number of packages written.

    Implementation notes:

    - Block format: concatenated raw header bytes, compressed as a unit.
    - TOC format at end of file: directory entries, symlink entries,
      file entries with ``(coff, csize, off, size)`` packed as
      big-endian ``>4i``, then a footer ``cz[0...0]cz``.
    - Use :func:`parse_hdlist` to read *old_hdlist_path* for incremental.
    """
    writer = HdlistWriter(
            output_path,
            packages,
            compression_filter=compression_filter,
            compression_level=compression_level,
            block_size=block_size,
            incremental=incremental,
            old_hdlist_path=old_hdlist_path,
            )
    wrotten = writer.run()
    return wrotten


class HdlistWriter():
    def __init__(
            self,
            output_path: Path,
            packages,
            compression_filter: str = 'gzip',
            compression_level: int = 9,
            block_size: int = 400 * 1024,
            incremental: bool = False,
            old_hdlist_path: Optional[Path] = None,
            ):
        self.output_path = output_path
        self.packages = packages
        self.filter = compression_filter
        self.level = compression_level
        self.block_size = block_size
        self.incremental = incremental
        self.old_hdlist_path = old_hdlist_path
        self.files: dict = {}
        self.dir: dict = {}
        self.symlink: dict = {}
        self.coff: int = 0
        self.current_block_data: bytes = b""
        self.current_block_files: List = []
        self.current_block_csize: int = 0
        self.current_block_coff: int = 0
        self.current_block_off: int = 0
        # self.ustream_data: Any = None
        self.toc_f_count: int = 0
        self.handle: IO = open(self.output_path, 'wb')
        if self.filter == "gzip":
            self.uncompress = b"gzip -d"
        elif self.filter in ("xz", "lzma"):
            self.uncompress = b"xz -d"
        else:
            raise ValueError("Compression filter should be one of gzip, xz, lzma")

    def run(self):
        """Write hdlist, either fully or incrementally."""
        self.current_block_off = 0
        self.current_block_coff = 0

        if self.incremental and self.old_hdlist_path is not None:
            _, _, hdlist_table = self._read_toc(self.old_hdlist_path)
            if hdlist_table:
                wrotten = self._write_incremental(hdlist_table)
            else:
                # No prior hdlist — fall back to full write
                wrotten = self._write_full()
        else:
            wrotten = self._write_full()

        self._build_toc()
        self.handle.close()
        self.destroyed = True
        print("File wrotten: ", self.output_path)
        return wrotten

    def _append_header(self, rpm_name: str, header_bytes: bytes) -> None:
        """Append a single RPM header to the current block, flushing if needed."""
        length = len(header_bytes)
        # coff and off are captured before appending
        block_coff = self.current_block_coff
        block_off  = self.current_block_off
        self.current_block_files.append(rpm_name)
        self.current_block_off += length
        self.current_block_data += header_bytes
        self.files[rpm_name] = {
            'size':  length,
            'off':   block_off,
            'csize': -1,
            'coff':  block_coff,
        }
        if len(self.current_block_data) >= self.block_size:
            self._end_block()

    def _copy_block(self, coff: int, csize: int,
                    rpms_in_block: list[str], state: dict) -> None:
        """Copy a compressed block verbatim from the old hdlist,
        and update self.files with the new coff for each RPM in the block."""
        new_coff = self.coff
        with open(self.old_hdlist_path, 'rb') as f:
            f.seek(coff)
            block_data = f.read(csize)
        self.handle.seek(new_coff)
        self.handle.write(block_data)
        self.coff += csize
        self.current_block_coff = self.coff
        for rpm_name in rpms_in_block:
            old_entry = state[rpm_name]
            self.files[rpm_name] = {
                'size':  len(self.packages[self.indexes[rpm_name]].header_bytes),
                'off':   old_entry['off'],
                'csize': csize,
                'coff':  new_coff,
            }
        return len(rpms_in_block)

    def _write_incremental(self, state: dict) -> None:
        """Write hdlist incrementally, reusing unchanged compressed blocks."""
        self.indexes = {os.path.basename(pkg.filename):index for index, pkg in enumerate(self.packages)}
        present = set(os.path.basename(a.filename) for a in self.packages)
        # Only RPMs that have a block_id were part of the last written hdlist.
        # Entries without block_id come from extract_appstream only and must be
        # treated as new regardless of presence.
        in_hdlist = {name for name, entry in state.items() if 'coff' in entry}
        removed = in_hdlist - present

        # Classify each present RPM
        unchanged: set[str] = set()
        new_rpms:  set[str] = set()
        for rpm_name in present:
            print(rpm_name)
            if rpm_name not in in_hdlist:
                new_rpms.add(rpm_name)
            else:
                unchanged.add(rpm_name)

        print(f"Incremental: {len(removed)} removed, "
              f"{len(new_rpms)} new, {len(unchanged)} unchanged.")

        # Group in_hdlist RPMs by coff
        blocks: dict[int, list[str]] = {}
        for rpm_name in in_hdlist:
            bid = state[rpm_name]['coff']
            blocks.setdefault(bid, []).append(rpm_name)

        # Process existing blocks in order
        for bid in sorted(blocks.keys()):
            rpms_in_block = blocks[bid]
            # Survivors: RPMs still present in this block
            survivors = [r for r in rpms_in_block if r not in removed]
            if not survivors:
                # Entire block was removed, skip it
                continue
            # Check whether the block can be copied verbatim
            block_intact = all(r in unchanged for r in survivors) and len(survivors) == len(rpms_in_block)
            if block_intact:
                coff  = state[rpms_in_block[0]]['coff']
                csize = state[rpms_in_block[0]]['csize']
                self._copy_block(coff, csize, survivors, state)
            else:
                # Rebuild the block from headers in memory
                for rpm_name in survivors:
                    self._append_header(rpm_name, self.packages[rpm_name]['header'])
                self._end_block()

    def _write_full(self):
        for rpm in self.packages:
            name = os.path.basename(rpm.filename)
            self.current_block_files.append(name)
            data = rpm.header_bytes
            length = len(data)
            self.current_block_off += length
            self.current_block_data += data
            self.files[name] = {
                'size': length,
                'off': self.current_block_off,
                'csize': -1,
                'coff': self.current_block_coff,
            }
            if len(self.current_block_data) >= self.block_size:
                self._end_block()
        self._end_block()
        return len(self.packages)

    def _build_toc(self) -> bool:
        self._end_block()
        self._end_seek()
        toc_length = 0

        coff = self.coff
        toc_sizes_offsets = b""

        toc_str = b""
        for entry in self.dir:
            toc_str += entry + b"\n"
            toc_length += len(entry + "\n")
        for entry, link in self.symlink.items():
            toc_str += entry + b"\n" + link + b"\n"
            toc_length += len(entry + "\n" + link + "\n")
        for entry in sorted(self.files.keys()):
            toc_length += len(entry + "\n")
        for entry in sorted(self.files.keys()):
            coff, csize, off, size = self.files[entry].values()
            print(entry, coff, csize, off, size)
            toc_str += entry.encode("utf-8") + b"\n"
            toc_sizes_offsets += pack(">4i", coff, csize, off, size)
            toc_length += len(pack(">4i", coff, csize, off, size))

        toc_str += toc_sizes_offsets
        self.coff += toc_length
        toc_header = b"cz[0"
        toc_footer = b"0]cz"
        toc_str += pack(b">4s4i40s4s", toc_header, len(self.dir), len(self.symlink), len(self.files), toc_length, self.uncompress, toc_footer)
        self.handle.seek(self.coff, os.SEEK_SET)
        self.handle.write(toc_str)
        self.toc_f_count = len(self.files)
        return True

    def _read_toc(self, filename: Path) -> (list[str], dict[str: str], dict[dict[str: int]]):
        with open(filename, "rb") as hdlist:
            hdlist.seek(-64, os.SEEK_END)  # 64 bytes before end
            header, toc_d_count, toc_l_count, toc_f_count, toc_str_size, uncompress, trailer = struct.unpack(">4s4i40s4s", hdlist.read(64))
            """ cz[0
                number of directory, 4 bytes
                number of symlinks, 4 bytes
                number of files, 4 bytes
                the toc size, 4 bytes
                uncompress command
                0]cz
            """
            print(header, toc_d_count, toc_l_count, toc_f_count, toc_str_size, uncompress, trailer)
            if header != b"cz[0" and trailer != b"0]cz":
                raise ValueError("Error reading toc: wrong header/trailer")
            hdlist.seek(-64 - (toc_str_size + 16 * toc_f_count), os.SEEK_END)
            fileslist = hdlist.read(toc_str_size)
            filenames = [x.decode('utf-8') for x in fileslist.split(b"\n")]
            if filenames[-1] == "":
                del filenames[-1]
            index = 0
            dir_list = []
            links_list = {}
            files = {}
            link_flag = False
            iter_toc = struct.iter_unpack(">4i", hdlist.read(16 * toc_f_count))
            """ Interpret bytes as packed binary data
            > : big-endian
            4: number of values
            i: integer
            iter_toc is an iterator
            """
            uncompressed_size = 0
            for f in filenames:
                index += 1
                if index <= toc_d_count:  # directories listed first
                    dir_list.append(f)
                    continue
                if index <= toc_d_count + toc_l_count:  # symlinks listed then
                    if not link_flag:
                        symlink = f
                        link_flag = True
                    else:
                        links_list[symlink] = f
                        link_flag = False
                else:
                    coff, csize, off, size = next(iter_toc)
                    files[f] = {
                        "coff": coff,
                        "csize": csize,
                        "off": off,
                        "size": size
                        }
                    uncompressed_size += size
            return dir_list, links_list, files

    def _end_seek(self):
        seekvalue = self.coff
        r = self.handle.seek(seekvalue, os.SEEK_SET)
        return r == seekvalue

    def _end_block(self):
        # self.log(f"writing block with {len(self.current_block_data)} bytes")
        if not self.current_block_data:
            return
        if not self._end_seek():
            return
        # TODO Use compress_open from compression module?
        if self.filter == "gzip":
            import io
            self.uncompress = b"gzip -d"
            buf = io.BytesIO()
            with gzip.GzipFile(fileobj=buf, mode='w', compresslevel=self.level) as gzip_file:
                gzip_file.write(self.current_block_data)
            cdata = buf.getvalue()
        elif self.filter in ("xz", "lzma"):
            cdata = lzma.compress(self.current_block_data, preset=self.level)
            self.uncompress = b"xz -d"
        outsize = len(cdata)
        self.handle.write(cdata)
        for pkg in self.current_block_files:
            self.files[pkg]["csize"] = outsize
        self.coff += outsize
        self.current_block_coff = self.coff
        self.current_block_csize = 0
        self.current_block_files = []
        self.current_block_off = 0
        self.current_block_data = b""
    # ─────────────────────────────────────────────
    # Hdlist state — delegates to unified _load/_save_state
    # ─────────────────────────────────────────────

    def _load_hdlist_state(self) -> dict:
        """Return hdlist-relevant entries from the unified state."""
        return self._load_state()

    def _save_hdlist_state(self, new_entries: dict) -> None:
        """Merge hdlist entries into the unified state and persist."""
        state = self._load_state()
        for rpm_name, hdlist_data in new_entries.items():
            entry = state.get(rpm_name, {})
            entry.update(hdlist_data)
            state[rpm_name] = entry
        self._save_state(state)

    # ─────────────────────────────────────────────
    # State persistence
    # ─────────────────────────────────────────────

    def _load_state(self) -> dict:
        """Load unified state from .genhdlist/state.json.

        Each entry is keyed by RPM filename and holds all persistence data:
        hdlist block layout, appstream extraction results, and header SHA-256.

        {
            "firefox-120.0-1.mga9.x86_64.rpm": {
                "sha256":       "abc123...",   # SHA-256 of hdr.unload()
                "block_id":     0,             # coff of the block (hdlist)
                "coff":         0,
                "csize":        12800,
                "off":          0,
                "extracted":    [...],         # appstream: extracted metainfo paths
                "generated":    null,          # appstream: generated metainfo path
                "processed_at": "2024-..."
            }, ...
        }
        """
        if self.cache_path is None:
            return {}
        state_file = self.cache_path / self.STATE_FILENAME
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                print("Warning: corrupted state file, falling back to full rebuild.")
        return {}

    def _save_state(self, state: dict) -> None:
        """Save unified state to .genhdlist/state.json."""
        if self.cache_path is None:
            return
        self.cache_path.mkdir(parents=True, exist_ok=True)
        state_file = self.cache_path / self.STATE_FILENAME
        state_file.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
