"""Compatibility shim — package files are no longer cached in SQLite.

Schema v28 (May 2026) dropped the ``package_files`` table and its
companion FTS5 index, which were responsible for ~3.7 GB out of the
3.8 GB ``/var/lib/urpm/packages.db`` observed in the wild.  ``urpm f``
now streams each enabled medium's ``files.xml.lzma`` directly via
:func:`urpm.core.files_xml.iter_file_matches`, with no persistent
cache.

This module keeps :class:`FilesMixin` only as a no-op surface so that
callers we have not yet migrated (some daemon paths, the ``--files``
flag of ``urpm media update``, etc.) keep working without
``AttributeError``.  Each method now returns a safe default and never
touches the dropped tables.

See ``doc/TODO_SHRINK_FILES_DB.md`` for the rationale and the
follow-up cleanup that will remove these stubs along with their
remaining callers.
"""

from typing import Any, Dict, Iterator, List, Optional, Set, Tuple


class FilesMixin:
    """No-op mixin preserving the historical API surface.

    Methods preserve their old signatures and return types so callers
    in ``sync.py``, ``daemon/scheduler.py`` and elsewhere keep
    type-checking without modification, but no SQL is issued and no
    state is recorded.
    """

    # ------------------------------------------------------------------
    # Import paths (used to populate ``package_files`` from files.xml)
    # ------------------------------------------------------------------

    def import_files_xml(self, media_id: int, parser, **_) -> Tuple[int, int]:
        # Drain the parser to keep its progress callback consistent
        # with the historical contract, then drop the data.
        pkgs = files = 0
        try:
            for nevra, file_list in parser:
                pkgs += 1
                files += len(file_list)
        except TypeError:
            # Caller passed a path/iterable we cannot consume — ignore.
            pass
        return files, pkgs

    def insert_package_files_batch(self, *_args, **_kwargs) -> None:
        return None

    def clear_package_files(self, *_args, **_kwargs) -> None:
        return None

    def delete_package_files_by_nevra(self, *_args, **_kwargs) -> None:
        return None

    def import_files_to_staging(self, *_args, **_kwargs) -> Tuple[int, int]:
        return 0, 0

    def create_package_files_staging(self) -> None:
        return None

    def finalize_package_files_atomic(self) -> None:
        return None

    def abort_package_files_atomic(self) -> None:
        return None

    def update_files_xml_state_batch(self, *_args, **_kwargs) -> None:
        return None

    def set_fast_import_pragmas(self) -> Dict[str, Any]:
        return {}

    def restore_pragmas(self, *_args, **_kwargs) -> None:
        return None

    # ------------------------------------------------------------------
    # Read paths (used to be query helpers for ``urpm f`` and friends)
    # ------------------------------------------------------------------

    def search_files(self, *_args, **_kwargs) -> List[Dict[str, Any]]:
        return []

    def search_files_fts(self, *_args, **_kwargs) -> List[Dict[str, Any]]:
        return []

    def get_package_files(self, _nevra: str) -> List[str]:
        return []

    def get_files_for_package(self, _nevra: str, media_id: Optional[int] = None) -> List[str]:
        return []

    def get_package_nevras_for_media(self, _media_id: int) -> Set[str]:
        return set()

    def get_files_xml_state(self, _media_id: int) -> Optional[Dict[str, Any]]:
        return None

    def get_files_xml_ratio(self) -> Optional[float]:
        return None

    def get_files_stats(self) -> Dict[str, Any]:
        return {
            'total_files': 0,
            'total_packages': 0,
            'media_with_files': 0,
        }

    # ------------------------------------------------------------------
    # FTS introspection (the file-FTS is gone; the search-FTS
    # ``packages_fts`` survives and is managed by ``database.py``)
    # ------------------------------------------------------------------

    def is_fts_supported(self) -> bool:
        # ``packages_fts`` (used by ``urpm search``) is still present
        # so callers checking generic FTS support still get True.
        return True

    def is_fts_available(self) -> bool:
        # The file-FTS this flag historically referred to is gone.
        return False

    def is_fts_index_current(self) -> bool:
        # Nothing to rebuild for the file-FTS; report current to
        # silence callers (e.g. the daemon's startup probe).
        return True

    def get_fts_stats(self) -> Dict[str, Any]:
        return {'main_table_count': 0, 'fts_row_count': 0}

    def rebuild_fts_index(self, *_args, **_kwargs) -> int:
        return 0

    def fts_sync_delete_nevras(self, *_args, **_kwargs) -> None:
        return None

    def fts_sync_insert_nevra(self, *_args, **_kwargs) -> None:
        return None

    def fts_mark_dirty(self) -> None:
        return None
