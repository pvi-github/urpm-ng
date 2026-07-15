"""AppStream scan cache database operations.

Persists the result of :func:`urpm.core.appstream_scan.scan_media_appstream_candidates`
so a media's ``files.xml.lzma`` is only re-parsed when it has actually
changed on disk.  The freshness key is ``(mtime, size)`` of the .lzma
file — the archive itself is already checksum-validated at sync time
against ``MD5SUM``, so an unchanged mtime+size means unchanged content.
"""

import json
import time
from typing import Dict, List, Optional, Tuple


class AppStreamMixin:
    """Mixin providing AppStream scan cache CRUD.

    Requires:
        - ``self._get_connection()``: thread-safe connection accessor.
        - ``self._lock``: :class:`threading.RLock` for writes.
    """

    def get_appstream_scan(
        self, media_id: int,
    ) -> Optional[Tuple[int, int, Dict[str, List[str]]]]:
        """Return the cached scan for a media, if any.

        Args:
            media_id: Row id in the ``media`` table.

        Returns:
            ``(files_xml_mtime, files_xml_size, candidates)`` where
            ``candidates`` maps NEVRA to the list of AppStream signal
            paths — or ``None`` if no scan has been persisted yet.
        """
        conn = self._get_connection()
        row = conn.execute(
            'SELECT files_xml_mtime, files_xml_size, candidates_json '
            'FROM appstream_scan_cache WHERE media_id = ?',
            (media_id,),
        ).fetchone()
        if row is None:
            return None
        return row[0], row[1], json.loads(row[2])

    def set_appstream_scan(
        self,
        media_id: int,
        files_xml_mtime: int,
        files_xml_size: int,
        candidates: Dict[str, List[str]],
    ) -> None:
        """Insert or replace the scan cache entry for a media.

        Args:
            media_id: Row id in the ``media`` table.
            files_xml_mtime: ``files.xml.lzma`` mtime as a Unix timestamp
                (integer seconds).
            files_xml_size: ``files.xml.lzma`` size in bytes.
            candidates: NEVRA → list of AppStream signal paths, as
                returned by
                :func:`urpm.core.appstream_scan.scan_media_appstream_candidates`.
        """
        with self._lock:
            conn = self._get_connection()
            conn.execute(
                'INSERT OR REPLACE INTO appstream_scan_cache '
                '(media_id, files_xml_mtime, files_xml_size, '
                ' candidates_json, scanned_at) '
                'VALUES (?, ?, ?, ?, ?)',
                (
                    media_id,
                    files_xml_mtime,
                    files_xml_size,
                    json.dumps(candidates, separators=(',', ':')),
                    int(time.time()),
                ),
            )
            conn.commit()

    def clear_appstream_scan(self, media_id: int) -> None:
        """Drop the cache entry for a media (e.g. after ``urpm media remove``)."""
        with self._lock:
            conn = self._get_connection()
            conn.execute(
                'DELETE FROM appstream_scan_cache WHERE media_id = ?',
                (media_id,),
            )
            conn.commit()
