"""
Cache management for urpm with quotas and retention.

Handles:
- Tracking cached RPM files
- Enforcing per-media and global quotas
- Retention policy (max age)
- Eviction of unreferenced and old files
"""

import logging
from pathlib import Path
from typing import Dict, List, Tuple, Any

from .database import PackageDatabase
from .config import get_base_dir

logger = logging.getLogger(__name__)


class CacheManager:
    """Manages the RPM cache with quotas and retention policies."""

    def __init__(self, db: PackageDatabase, base_dir: Path = None):
        """Initialize cache manager.

        Args:
            db: Database instance
            base_dir: Base urpm directory (auto-detected if None)
        """
        self.db = db
        self.base_dir = base_dir or get_base_dir()
        self.medias_dir = self.base_dir / "medias"

    # =========================================================================
    # File registration
    # =========================================================================

    def register_file(self, filename: str, media_id: int, file_path: str,
                      file_size: int = None) -> int:
        """Register a downloaded file in the cache.

        If file_size is not provided, it will be read from the filesystem.

        Args:
            filename: RPM filename
            media_id: Associated media ID
            file_path: Relative path from medias/ directory
            file_size: File size in bytes (optional)

        Returns:
            Cache file ID
        """
        if file_size is None:
            full_path = self.medias_dir / file_path
            if full_path.exists():
                file_size = full_path.stat().st_size
            else:
                file_size = 0

        return self.db.register_cache_file(filename, media_id, file_path, file_size)

    def update_access(self, filename: str, media_id: int = None):
        """Update last accessed time for a file (for LRU tracking)."""
        self.db.update_cache_file_access(filename, media_id)

    # =========================================================================
    # Statistics
    # =========================================================================

    def get_usage(self, media_id: int = None) -> Dict[str, Any]:
        """Get cache usage statistics.

        Args:
            media_id: Filter by media (None = global)

        Returns:
            Dict with total_size, total_files, referenced/unreferenced counts, etc.
        """
        stats = self.db.get_cache_stats(media_id)

        # Add quota info
        if media_id:
            media = self.db.get_media_by_id(media_id)
            if media and media.get('quota_mb'):
                stats['quota_bytes'] = media['quota_mb'] * 1024 * 1024
                stats['quota_used_pct'] = (
                    stats['total_size'] / stats['quota_bytes'] * 100
                    if stats['quota_bytes'] > 0 else 0
                )
        else:
            # Global quota
            global_quota = self.db.get_mirror_config('global_quota_mb')
            if global_quota:
                stats['quota_bytes'] = int(global_quota) * 1024 * 1024
                stats['quota_used_pct'] = (
                    stats['total_size'] / stats['quota_bytes'] * 100
                    if stats['quota_bytes'] > 0 else 0
                )

        return stats

    def get_media_usage(self, media_id: int) -> int:
        """Get total cache size for a specific media in bytes."""
        stats = self.db.get_cache_stats(media_id)
        return stats.get('total_size', 0)

    def get_total_usage(self) -> int:
        """Get total cache size in bytes."""
        stats = self.db.get_cache_stats()
        return stats.get('total_size', 0)

    def get_disk_usage(self, media_id: int = None) -> Dict[str, int]:
        """Get actual disk usage by scanning the filesystem.

        This is more accurate than database stats when files are manually
        deleted or the database is out of sync.

        Args:
            media_id: Filter by media (None = all media)

        Returns:
            Dict with 'total_size' (bytes) and 'file_count'
        """
        from .config import get_media_local_path

        total_size = 0
        file_count = 0

        if media_id:
            media = self.db.get_media_by_id(media_id)
            if media:
                media_path = get_media_local_path(media, self.base_dir)
                if media_path.exists():
                    for rpm_file in media_path.glob('*.rpm'):
                        try:
                            total_size += rpm_file.stat().st_size
                            file_count += 1
                        except OSError:
                            pass
        else:
            # Scan all media directories
            for media in self.db.list_media():
                media_path = get_media_local_path(media, self.base_dir)
                if media_path.exists():
                    for rpm_file in media_path.glob('*.rpm'):
                        try:
                            total_size += rpm_file.stat().st_size
                            file_count += 1
                        except OSError:
                            pass

        return {'total_size': total_size, 'file_count': file_count}

    # =========================================================================
    # Quota enforcement
    # =========================================================================

    def enforce_quotas(self, dry_run: bool = False) -> Dict[str, Any]:
        """Enforce all quotas and retention policies.

        Order of operations:
        1. Remove unreferenced files (no longer in any synthesis)
        2. Apply retention policy (files older than retention_days)
        3. Apply per-media quotas
        4. Apply global quota

        Args:
            dry_run: If True, don't actually delete files

        Returns:
            Dict with statistics about what was (or would be) deleted
        """
        result = {
            'unreferenced_deleted': 0,
            'unreferenced_bytes': 0,
            'retention_deleted': 0,
            'retention_bytes': 0,
            'quota_deleted': 0,
            'quota_bytes': 0,
            'total_deleted': 0,
            'total_bytes': 0,
            'errors': [],
        }

        # Phase 1: Remove unreferenced files
        unreferenced = self.db.list_cache_files(referenced_only=False)
        unreferenced = [f for f in unreferenced if not f['is_referenced']]

        for f in unreferenced:
            if self._delete_file(f, dry_run):
                result['unreferenced_deleted'] += 1
                result['unreferenced_bytes'] += f['file_size']
            else:
                result['errors'].append(f"Failed to delete {f['file_path']}")

        # Phase 2: Apply retention policy per media
        for media in self.db.list_media():
            retention_days = media.get('retention_days', 30)
            if retention_days and retention_days > 0:
                old_files = self.db.get_files_to_evict(
                    media_id=media['id'],
                    max_age_days=retention_days
                )
                for f in old_files:
                    if f['is_referenced']:
                        continue  # Don't delete referenced files for retention
                    if self._delete_file(f, dry_run):
                        result['retention_deleted'] += 1
                        result['retention_bytes'] += f['file_size']

        # Phase 3: Apply per-media quotas
        for media in self.db.list_media():
            quota_mb = media.get('quota_mb')
            if not quota_mb:
                continue

            quota_bytes = quota_mb * 1024 * 1024
            current_size = self.get_media_usage(media['id'])

            if current_size > quota_bytes:
                excess = current_size - quota_bytes
                files_to_evict = self.db.get_files_to_evict(
                    media_id=media['id'],
                    max_bytes=excess
                )
                for f in files_to_evict:
                    if self._delete_file(f, dry_run):
                        result['quota_deleted'] += 1
                        result['quota_bytes'] += f['file_size']

        # Phase 4: Apply global quota
        global_quota_str = self.db.get_mirror_config('global_quota_mb')
        if global_quota_str:
            global_quota_bytes = int(global_quota_str) * 1024 * 1024
            current_size = self.get_total_usage()

            if current_size > global_quota_bytes:
                excess = current_size - global_quota_bytes
                files_to_evict = self.db.get_files_to_evict(max_bytes=excess)
                for f in files_to_evict:
                    if self._delete_file(f, dry_run):
                        result['quota_deleted'] += 1
                        result['quota_bytes'] += f['file_size']

        # Totals
        result['total_deleted'] = (
            result['unreferenced_deleted'] +
            result['retention_deleted'] +
            result['quota_deleted']
        )
        result['total_bytes'] = (
            result['unreferenced_bytes'] +
            result['retention_bytes'] +
            result['quota_bytes']
        )

        return result

    def evict_for_space(self, needed_bytes: int, media_id: int = None,
                        dry_run: bool = False) -> Tuple[bool, int]:
        """Try to free up space for a new download.

        Args:
            needed_bytes: Bytes needed
            media_id: Prefer evicting from this media first
            dry_run: If True, don't actually delete

        Returns:
            Tuple of (success, bytes_freed)
        """
        freed = 0

        # First try to evict from the specific media
        if media_id:
            files = self.db.get_files_to_evict(media_id=media_id, max_bytes=needed_bytes)
            for f in files:
                if self._delete_file(f, dry_run):
                    freed += f['file_size']
                    if freed >= needed_bytes:
                        return True, freed

        # If not enough, evict globally
        remaining = needed_bytes - freed
        if remaining > 0:
            files = self.db.get_files_to_evict(max_bytes=remaining)
            for f in files:
                if self._delete_file(f, dry_run):
                    freed += f['file_size']
                    if freed >= needed_bytes:
                        return True, freed

        return freed >= needed_bytes, freed

    # =========================================================================
    # Reference tracking
    # =========================================================================

    def mark_unreferenced(self, media_id: int, current_filenames: List[str]):
        """Mark files as unreferenced after a sync.

        Call this after syncing a media to mark files that are no longer
        in the synthesis as unreferenced (candidates for eviction).

        Args:
            media_id: Media ID
            current_filenames: List of filenames currently in synthesis
        """
        self.db.mark_cache_files_unreferenced(media_id, current_filenames)

    # =========================================================================
    # Scanning and reconciliation
    # =========================================================================

    def scan_media_directory(self, media_id: int, media_path: Path) -> Dict[str, Any]:
        """Scan a media directory and register any untracked files.

        Useful for initial setup or after manual file additions.

        Args:
            media_id: Media ID
            media_path: Full path to media directory

        Returns:
            Dict with 'found', 'registered', 'already_tracked'
        """
        result = {
            'found': 0,
            'registered': 0,
            'already_tracked': 0,
        }

        if not media_path.exists():
            return result

        # Find all RPM files
        for rpm_path in media_path.rglob("*.rpm"):
            if not rpm_path.is_file():
                continue

            result['found'] += 1
            filename = rpm_path.name

            # Check if already tracked
            existing = self.db.get_cache_file(filename, media_id)
            if existing:
                result['already_tracked'] += 1
                continue

            # Register it
            try:
                rel_path = str(rpm_path.relative_to(self.medias_dir))
                file_size = rpm_path.stat().st_size
                self.db.register_cache_file(filename, media_id, rel_path, file_size)
                result['registered'] += 1
            except Exception as e:
                logger.warning(f"Failed to register {rpm_path}: {e}")

        return result

    def reconcile(self) -> Dict[str, Any]:
        """Reconcile database with filesystem.

        - Remove DB entries for files that don't exist
        - Add DB entries for untracked files

        Returns:
            Dict with statistics
        """
        result = {
            'orphan_records_removed': 0,
            'untracked_files_added': 0,
        }

        # Check for orphan DB records
        for cache_file in self.db.list_cache_files():
            full_path = self.medias_dir / cache_file['file_path']
            if not full_path.exists():
                self.db.delete_cache_file(cache_file['filename'], cache_file['media_id'])
                result['orphan_records_removed'] += 1

        # Scan for untracked files
        for media in self.db.list_media():
            from .config import get_media_local_path
            media_path = get_media_local_path(media, self.base_dir)
            scan_result = self.scan_media_directory(media['id'], media_path)
            result['untracked_files_added'] += scan_result['registered']

        return result

    # =========================================================================
    # Internal helpers
    # =========================================================================

    def _delete_file(self, cache_file: Dict, dry_run: bool = False) -> bool:
        """Delete a cached file (both filesystem and DB record).

        Args:
            cache_file: Cache file dict from database
            dry_run: If True, don't actually delete

        Returns:
            True if successful (or would be successful in dry_run)
        """
        file_path = self.medias_dir / cache_file['file_path']

        if dry_run:
            logger.debug(f"Would delete: {file_path}")
            return True

        try:
            if file_path.exists():
                file_path.unlink()
                logger.debug(f"Deleted: {file_path}")

            # Always remove DB record
            self.db.delete_cache_file(cache_file['filename'], cache_file['media_id'])
            return True

        except OSError as e:
            logger.warning(f"Failed to delete {file_path}: {e}")
            return False


def format_size(size_bytes: int) -> str:
    """Format size in bytes as human-readable string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    else:
        return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"
