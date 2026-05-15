"""
AppStream metadata management for urpm-ng.

This module handles:
- Downloading AppStream data from mirrors (when available)
- Generating degraded AppStream from package metadata (fallback)
- Merging per-media AppStream files into unified catalog
- Refreshing system AppStream cache
"""

import gzip
import logging
import lzma
import json
import subprocess
import os
import re
import tempfile
import shutil
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple, Iterator
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

if TYPE_CHECKING:
    from .database import PackageDatabase

logger = logging.getLogger(__name__)

# RPM groups that indicate desktop applications
DESKTOP_GROUPS = {
    # Games
    'games', 'games/arcade', 'games/boards', 'games/cards', 'games/puzzles',
    'games/sports', 'games/strategy', 'games/adventure', 'games/rpg',
    # Graphical desktop applications
    'graphical desktop/gnome', 'graphical desktop/kde', 'graphical desktop/xfce',
    'graphical desktop/other',
    # Office & productivity
    'office', 'office/suite', 'office/wordprocessor', 'office/spreadsheet',
    'office/presentation', 'office/database', 'office/finance',
    # Graphics
    'graphics', 'graphics/viewer', 'graphics/editor', 'graphics/3d',
    'graphics/photography', 'graphics/scanning',
    # Multimedia
    'video', 'video/players', 'video/editors',
    'sound', 'sound/players', 'sound/editors', 'sound/mixers',
    # Networking / Internet
    'networking/www', 'networking/mail', 'networking/chat',
    'networking/instant messaging', 'networking/news', 'networking/ftp',
    'networking/file transfer', 'networking/remote access',
    # Education & Science
    'education', 'sciences', 'sciences/astronomy', 'sciences/chemistry',
    'sciences/mathematics', 'sciences/physics',
    # Development (IDEs only)
    'development/ide',
    # Accessibility
    'accessibility',
    # Archiving
    'archiving/compression',
    # Editors
    'editors',
    # Emulators
    'emulators',
    # File tools
    'file tools',
    # Terminals
    'terminals',
}

# Map RPM groups to freedesktop categories
GROUP_TO_CATEGORY = {
    'games': 'Game', 'games/arcade': 'Game', 'games/boards': 'Game',
    'games/cards': 'Game', 'games/puzzles': 'Game', 'games/sports': 'Game',
    'games/strategy': 'Game', 'games/adventure': 'Game', 'games/rpg': 'Game',
    'office': 'Office', 'office/suite': 'Office', 'office/wordprocessor': 'Office',
    'office/spreadsheet': 'Office', 'office/presentation': 'Office',
    'office/database': 'Office', 'office/finance': 'Office',
    'graphics': 'Graphics', 'graphics/viewer': 'Graphics', 'graphics/editor': 'Graphics',
    'graphics/3d': 'Graphics', 'graphics/photography': 'Graphics', 'graphics/scanning': 'Graphics',
    'video': 'AudioVideo', 'video/players': 'AudioVideo', 'video/editors': 'AudioVideo',
    'sound': 'AudioVideo', 'sound/players': 'AudioVideo', 'sound/editors': 'AudioVideo',
    'sound/mixers': 'AudioVideo',
    'networking/www': 'Network', 'networking/mail': 'Network', 'networking/chat': 'Network',
    'networking/instant messaging': 'Network', 'networking/news': 'Network',
    'networking/ftp': 'Network', 'networking/file transfer': 'Network',
    'networking/remote access': 'Network',
    'education': 'Education', 'sciences': 'Science', 'sciences/astronomy': 'Science',
    'sciences/chemistry': 'Science', 'sciences/mathematics': 'Science',
    'sciences/physics': 'Science',
    'development/ide': 'Development',
    'accessibility': 'Accessibility',
    'archiving/compression': 'Utility',
    'editors': 'TextEditor',
    'emulators': 'Game',
    'file tools': 'Utility',
    'terminals': 'TerminalEmulator',
    'graphical desktop/gnome': 'GNOME', 'graphical desktop/kde': 'KDE',
    'graphical desktop/xfce': 'XFCE', 'graphical desktop/other': 'Utility',
}


@dataclass
class AppStreamSyncResult:
    """Result of syncing AppStream for a media."""
    media_name: str
    success: bool
    source: str  # 'upstream', 'generated', 'failed'
    component_count: int
    error: Optional[str] = None


ICONS_SUBDIR        = "icons"  # Subdirectory to store extracted icons

ORG = "org.mageia"


class AppStreamManager:
    """Manages AppStream metadata for all media."""

    METAINFO_PREFIX = "/usr/share/metainfo/"
    METAINFO_SUFFIXES = (".appdata.xml", ".metainfo.xml")
    BIN_PREFIX = "/usr/bin/"

    CACHE_DIR = ".genhdlist"
    STATE_FILENAME = "state.json"
    # store information for packages after extraction
    results = {}

    def __init__(self, db: 'PackageDatabase', base_dir: Optional[Path] = None):
        """
        Initialize AppStream manager.

        Args:
            db: Package database instance
            base_dir: Base directory for urpm data (default: /var/lib/urpm)
        """
        self.db = db
        if base_dir is None:
            base_dir = Path('/var/lib/urpm')
        self.appstream_dir = base_dir / 'appstream'
        self.catalog_path = Path('/var/cache/swcatalog/xml/mageia-urpm.xml.gz')

    def _ensure_dirs(self) -> None:
        """Ensure required directories exist."""
        self.appstream_dir.mkdir(parents=True, exist_ok=True)
        self.catalog_path.parent.mkdir(parents=True, exist_ok=True)

    def _sanitize_filename(self, name: str) -> str:
        """Convert media name to safe filename."""
        return name.lower().replace(' ', '-').replace('/', '-')

    def get_media_appstream_path(self, media_name: str) -> Path:
        """Get path for a media's AppStream file."""
        return self.appstream_dir / f"{self._sanitize_filename(media_name)}.xml"

    def generate_for_media(
        self,
        media_id: int,
        media_name: str,
        origin: Optional[str] = None
    ) -> Tuple[str, int]:
        """
        Generate AppStream XML for a single media from package metadata.

        This creates a "degraded" AppStream that only includes basic info
        from the synthesis (name, summary, description, group).

        Args:
            media_id: Database ID of the media
            media_name: Name of the media (for origin attribute)
            origin: Origin string for the catalog (default: media name)

        Returns:
            Tuple of (xml_string, component_count)
        """
        if origin is None:
            origin = f"mageia-{self._sanitize_filename(media_name)}"

        # Create root element
        root = ET.Element('components')
        root.set('version', '0.16')
        root.set('origin', origin)

        conn = self.db._get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT DISTINCT
                p.name, p.version, p.release, p.arch,
                p.summary, p.description, p.url, p.license,
                p.size, p.group_name
            FROM packages p
            WHERE p.media_id = ?
            ORDER BY p.name
        ''', (media_id,))

        pkg_count = 0
        for row in cursor.fetchall():
            name, ver, release, arch, summary, description, url, license_, size, group_name = row

            # Skip non-application packages
            if name.endswith(('-debug', '-debuginfo', '-devel', '-static', '-doc', '-docs')):
                continue
            if name.startswith(('lib', 'perl-', 'python-', 'python3-', 'ruby-', 'golang-', 'rust-')):
                continue
            if name.endswith(('-libs', '-common', '-data', '-lang', '-l10n', '-i18n')):
                continue

            # Filter by group - only desktop applications
            group_lower = (group_name or '').lower()
            if not any(group_lower.startswith(g) or group_lower == g for g in DESKTOP_GROUPS):
                continue

            # Create component as desktop-application
            component = ET.SubElement(root, 'component')
            component.set('type', 'desktop-application')

            # Desktop ID (AppStream spec requires .desktop suffix)
            desktop_id = f'{name}.desktop'
            ET.SubElement(component, 'id').text = desktop_id
            ET.SubElement(component, 'pkgname').text = name
            ET.SubElement(component, 'name').text = name
            ET.SubElement(component, 'summary').text = summary or f'{name} application'

            # Launchable (desktop file reference)
            launchable = ET.SubElement(component, 'launchable')
            launchable.set('type', 'desktop-id')
            launchable.text = desktop_id

            if description:
                desc_elem = ET.SubElement(component, 'description')
                p_elem = ET.SubElement(desc_elem, 'p')
                p_elem.text = description[:500]

            if url:
                url_elem = ET.SubElement(component, 'url')
                url_elem.set('type', 'homepage')
                url_elem.text = url

            if license_:
                ET.SubElement(component, 'project_license').text = license_

            # Category from group mapping
            categories = ET.SubElement(component, 'categories')
            category = GROUP_TO_CATEGORY.get(group_lower, 'Utility')
            ET.SubElement(categories, 'category').text = category

            # Icon - use package name as stock icon
            icon = ET.SubElement(component, 'icon')
            icon.set('type', 'stock')
            icon.text = name

            pkg_count += 1

        # Generate XML string
        xml_str = ET.tostring(root, encoding='unicode')
        xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

        return xml_str, pkg_count

    def sync_media_appstream(
        self,
        media_id: int,
        media_name: str,
        media_url: str,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> AppStreamSyncResult:
        """
        Sync AppStream data for a media.

        Tries to download appstream.xml.lzma from the mirror.
        If not available (404), generates degraded AppStream from synthesis.

        Args:
            media_id: Database ID of the media
            media_name: Display name of the media
            media_url: Base URL of the media (e.g., .../media/core/release/)
            progress_callback: Optional callback for status messages

        Returns:
            AppStreamSyncResult with sync outcome
        """
        self._ensure_dirs()
        output_path = self.get_media_appstream_path(media_name)

        def log(msg: str):
            if progress_callback:
                progress_callback(msg)
            logger.info(msg)

        # Try to download from upstream
        appstream_url = media_url.rstrip('/') + '/media_info/appstream.xml.lzma'

        try:
            log(f"Checking {appstream_url}")
            req = Request(appstream_url, method='HEAD')
            req.add_header('User-Agent', 'urpm-ng/1.0')

            with urlopen(req, timeout=10) as response:
                if response.status == 200:
                    # File exists, download it
                    log(f"Downloading AppStream for {media_name}")
                    req = Request(appstream_url)
                    req.add_header('User-Agent', 'urpm-ng/1.0')

                    with urlopen(req, timeout=60) as response:
                        compressed_data = response.read()

                    # Decompress LZMA
                    xml_data = lzma.decompress(compressed_data).decode('utf-8')

                    # Count components
                    try:
                        root = ET.fromstring(xml_data)
                        component_count = len(root.findall('.//component'))
                    except ET.ParseError:
                        component_count = 0

                    # Save to file
                    with open(output_path, 'w', encoding='utf-8') as f:
                        f.write(xml_data)

                    log(f"Downloaded AppStream for {media_name}: {component_count} components")
                    return AppStreamSyncResult(
                        media_name=media_name,
                        success=True,
                        source='upstream',
                        component_count=component_count
                    )

        except HTTPError as e:
            if e.code == 404:
                log(f"No upstream AppStream for {media_name}, generating...")
            else:
                logger.warning(f"HTTP error fetching AppStream for {media_name}: {e}")
        except URLError as e:
            logger.warning(f"Network error fetching AppStream for {media_name}: {e}")
        except lzma.LZMAError as e:
            logger.warning(f"Failed to decompress AppStream for {media_name}: {e}")
        except Exception as e:
            logger.warning(f"Error fetching AppStream for {media_name}: {e}")

        # Fallback: generate from synthesis
        try:
            xml_str, component_count = self.generate_for_media(media_id, media_name)

            with open(output_path, 'w', encoding='utf-8') as f:
                f.write(xml_str)

            log(f"Generated AppStream for {media_name}: {component_count} components")
            return AppStreamSyncResult(
                media_name=media_name,
                success=True,
                source='generated',
                component_count=component_count
            )

        except Exception as e:
            logger.error(f"Failed to generate AppStream for {media_name}: {e}")
            return AppStreamSyncResult(
                media_name=media_name,
                success=False,
                source='failed',
                component_count=0,
                error=str(e)
            )

    def merge_all_catalogs(
        self,
        progress_callback: Optional[Callable[[str], None]] = None
    ) -> Tuple[int, int]:
        """
        Merge all per-media AppStream files into unified catalog.

        Args:
            progress_callback: Optional callback for status messages

        Returns:
            Tuple of (total_components, media_count)
        """
        self._ensure_dirs()

        def log(msg: str):
            if progress_callback:
                progress_callback(msg)
            logger.info(msg)

        # Get system version for origin
        try:
            from .config import get_system_version
            version = get_system_version() or 'unknown'
        except ImportError:
            version = 'unknown'

        # Create merged root
        merged_root = ET.Element('components')
        merged_root.set('version', '0.16')
        merged_root.set('origin', f'mageia-{version}')

        total_components = 0
        media_count = 0

        # Find all per-media AppStream files
        if not self.appstream_dir.exists():
            log("No AppStream directory found")
            return 0, 0

        for xml_file in sorted(self.appstream_dir.glob('*.xml')):
            try:
                tree = ET.parse(xml_file)
                root = tree.getroot()

                # Copy all components
                for component in root.findall('component'):
                    merged_root.append(component)
                    total_components += 1

                media_count += 1
                logger.debug(f"Merged {xml_file.name}")

            except ET.ParseError as e:
                logger.warning(f"Failed to parse {xml_file}: {e}")
            except Exception as e:
                logger.warning(f"Error processing {xml_file}: {e}")

        if total_components == 0:
            log("No components to merge")
            return 0, 0

        # Write merged catalog (gzipped)
        xml_str = ET.tostring(merged_root, encoding='unicode')
        xml_str = '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_str

        with gzip.open(self.catalog_path, 'wt', encoding='utf-8') as f:
            f.write(xml_str)

        log(f"Merged {total_components} components from {media_count} media into {self.catalog_path}")
        return total_components, media_count

    def refresh_system_cache(self) -> bool:
        """
        Refresh system AppStream cache using appstreamcli.

        Returns:
            True if successful, False otherwise
        """
        try:
            result = subprocess.run(
                ['appstreamcli', 'refresh-cache', '--force'],
                capture_output=True,
                text=True,
                timeout=120
            )
            if result.returncode == 0:
                logger.info("AppStream cache refreshed successfully")
                return True
            else:
                logger.warning(f"appstreamcli returned {result.returncode}: {result.stderr}")
                return False
        except FileNotFoundError:
            logger.info("appstreamcli not installed, skipping cache refresh")
            return True  # Not an error if not installed
        except subprocess.TimeoutExpired:
            logger.warning("appstreamcli timed out")
            return False
        except Exception as e:
            logger.warning(f"Failed to refresh AppStream cache: {e}")
            return False

    def get_status(self) -> List[Dict]:
        """
        Get AppStream status for all media.

        Returns:
            List of dicts with media_name, source, component_count, last_updated
        """
        status = []

        # Get all enabled media from database
        conn = self.db._get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id, name FROM media WHERE enabled = 1 ORDER BY name')

        for media_id, media_name in cursor.fetchall():
            xml_path = self.get_media_appstream_path(media_name)

            if xml_path.exists():
                try:
                    tree = ET.parse(xml_path)
                    root = tree.getroot()
                    component_count = len(root.findall('component'))

                    # Check origin to determine source
                    origin = root.get('origin', '')
                    if 'generated' in origin or origin.startswith('mageia-'):
                        source = 'generated'
                    else:
                        source = 'upstream'

                    mtime = xml_path.stat().st_mtime

                except Exception:
                    source = 'error'
                    component_count = 0
                    mtime = 0
            else:
                source = 'missing'
                component_count = 0
                mtime = 0

            status.append({
                'media_id': media_id,
                'media_name': media_name,
                'source': source,
                'component_count': component_count,
                'last_updated': mtime
            })

        return status

    def remove_media_appstream(self, media_name: str) -> bool:
        """
        Remove AppStream file for a media.

        Args:
            media_name: Name of the media

        Returns:
            True if removed, False if didn't exist
        """
        xml_path = self.get_media_appstream_path(media_name)
        if xml_path.exists():
            xml_path.unlink()
            return True
        return False

    # ─── Generation API (used by urpm.genmedia) ──────────────

    def extract_from_rpm(self, metadata, cache_dir: Path, state_sha256: str, force: bool = False) -> Optional[str]:
        """Extract AppStream metainfo XML from a single RPM.

        Looks for embedded ``/usr/share/metainfo/*.metainfo.xml`` or
        ``*.appdata.xml`` files inside the RPM.  If none found, generates
        a minimal component from RPM header fields (name, summary,
        description, group, url).

        Also handles ``.desktop`` file parsing and icon extraction.

        Args:
            metadata: A :class:`~urpm.genmedia.RpmMetadata` instance.
            cache_dir: Directory to cache extracted metainfo and icons.

        Returns:
            Path to the cached metainfo XML file, or None on failure.
        """

        pkg_result = {"extracted": [], "generated": None, "error": None, "skipped": False}
        # SHA-256 is already computed on hdr.unload() in add_pkg()
        pkg_result["sha256"] = metadata.header_sha256
        if state_sha256 == pkg_result["sha256"] and not force:
            pkg_result["skipped"] = True
            return pkg_result
        rpm_name = os.path.basename(metadata.filename)
        # package_info = self.synthesis[rpm_name]
        rpm_path = cache_dir / rpm_name
        file_list = metadata.files
        # ── Filtrage metainfo ──────────────────────────────────────────
        metainfo_targets = [
            f for f in file_list
            if f.startswith(self.METAINFO_PREFIX)
            and f.endswith(self.METAINFO_SUFFIXES)
        ]

        # ── Filtrage /usr/bin ──────────────────────────────────────────
        bin_files = [
            f for f in file_list
            if f.startswith(self.BIN_PREFIX)
            and not f.endswith("/")   # exclude directory itself
        ]
        if bin_files:
            print(f"  → {len(bin_files)} /usr/bin binary(ies): "
                  f"{[Path(b).name for b in bin_files]}")

        # ── Filtrage fichiers .desktop ─────────────────────────────────
        DESKTOP_PATHS = [
            "/usr/share/applications/",
            "/usr/local/share/applications/",
            "/usr/share/wayland-sessions/",
            "/usr/share/xsessions/",
        ]

        desktop_files = [
            f for f in file_list
            if any(f.startswith(path) for path in DESKTOP_PATHS)
            and f.endswith(".desktop")
        ]

        desktop_info = None
        if desktop_files:
            print(f"  → {len(desktop_files)} .desktop file(s) found")
            # Parse first found .desktop file
            with tempfile.TemporaryDirectory() as tmp_dir:
                try:
                    self._extract_rpm_to_dir(Path(metadata.filename), tmp_dir)
                    first_desktop = desktop_files[0].lstrip("/")
                    desktop_path = Path(tmp_dir) / first_desktop
                    if desktop_path.exists():
                        desktop_info = self._parse_desktop_file(desktop_path)
                        print(f"     Type: {desktop_info.get('type')}, "
                              f"Icon: {desktop_info.get('icon')}, "
                              f"Categories: {desktop_info.get('categories')}")
                except Exception as e:
                    print(f"  ⚠  .desktop parsing error: {e}")

        # ── Recherche et extraction icône ──────────────────────────────
        ICON_PATHS = [
            "/usr/share/icons/hicolor/128x128/apps/",
            "/usr/share/icons/hicolor/64x64/apps/",
            "/usr/share/icons/hicolor/96x96/apps/",
            "/usr/share/icons/hicolor/48x48/apps/",
            "/usr/share/icons/hicolor/scalable/apps/",
            "/usr/share/pixmaps/",
        ]
        ICON_EXTENSIONS = (".png", ".svg", ".xpm")

        icon_path = None
        if desktop_info and desktop_info.get("icon"):
            icon_name = desktop_info["icon"]
            icon_in_rpm = self._find_icon_in_rpm(
                file_list, icon_name, ICON_PATHS, ICON_EXTENSIONS
            )
            if icon_in_rpm:
                print(f"  → Found icon: {icon_in_rpm}")
                # Calculate app_id to name the icon
                app_id = self._sanitize_id(rpm_path.stem)
                icon_path = self._extract_icon(
                    Path(metadata.filename), icon_in_rpm, app_id, cache_dir
                )

        # ── Cleaning previous directory in mode forced ───────
        pkg_cache = cache_dir / rpm_path.stem
        if force and pkg_cache.exists():
            shutil.rmtree(pkg_cache)

        if metainfo_targets:
            # ── Case 1 : embedded metainfo files → extraction ───────
            print(f"  → {len(metainfo_targets)} metainfo file(s)"
                  f"detected : {metainfo_targets}")
            try:
                copied = self._extract_metainfo_files(
                    rpm_path, metainfo_targets, cache_dir
                )
                pkg_result["extracted"] = copied
            except subprocess.CalledProcessError as e:
                pkg_result["error"] = f"  ✗ Extraction failed (rpm2cpio/cpio): {e}"
                return pkg_result
            except Exception as e:
                pkg_result["error"] = f"  ✗ Unexpected error: {e}"
                return pkg_result

        else:
            # ── Case 2 : no metainfo file → AppStream génération ──
            # print("  → No metainfo file found, generating AppStream XML.")

            try:
                pkg_result["generated"] = self._generate_appstream_xml(
                    metadata,
                    pkg_stem=rpm_path.stem,
                    bin_files=bin_files,
                    dest_dir=cache_dir / rpm_path.stem,
                    desktop_info=desktop_info,
                    icon_path=icon_path,
                )
            except Exception as e:
                logging.warning(f"Failed to generate AppStream XML for {metadata.name}: {str(e)}")
        self.results[rpm_name] = pkg_result
        return pkg_result

    def build_catalog(
        self,
        cache_dir: Path,
        output_path: Path,
        *,
        compression_filter: str = 'xz -7',
    ) -> int | None:
        """Build an AppStream catalog from cached metainfo files.

        Collects all ``*.xml`` files from *cache_dir*, parses each into
        a ``<component>`` element, wraps them in a
        ``<components version="0.15">`` root, and compresses the result
        to *output_path* (typically ``appstream.xml.lzma``).

        Uses :data:`GROUP_TO_CATEGORY` for RPM group → freedesktop
        category mapping (shared with :meth:`generate_for_media`).

        Args:
            cache_dir: Directory containing per-RPM metainfo XML files.
            output_path: Destination file (e.g.
                ``media_info/appstream.xml.lzma``).
            compression_filter: Compressor and level.

        Returns:
            Number of components included in the catalog.
        """
        xml_files = list(self._iter_xml_files(cache_dir))
        if not xml_files:
            print("\n⚠  No XML file found in cache, catalogue not generated.")
            return None

        print(f"\n{'─' * 52}")
        print(f"CATALOG — assembling {len(xml_files)} XML file(s)")
        print(f"{'─' * 52}")

        components_el = ET.Element("components", {
            "version": "0.15",
            "origin":  "local",
        })

        ok_count = 0
        err_count = 0

        for xml_file in xml_files:
            component = self._parse_component(xml_file)
            if component is None:
                err_count += 1
                continue
            components_el.append(component)
            ok_count += 1
            # print(f"  +  {xml_file.relative_to(cache_dir)}")

        if ok_count == 0:
            print("⚠  No valid components, catalog not generated.")
            return None

        tree = ET.ElementTree(components_el)
        ET.indent(tree, space="  ", level=0)
        formatted = ET.tostring(
                        components_el,
                        encoding="unicode",
                        xml_declaration=False
                        )

        # Insert headser comment after XML declaration
        header_comment = (
            f"<!-- AppStream catalog — generated by urpm\n"
            f"     {self._now_iso()} — {ok_count} component(s) -->\n"
        )
        final_xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + header_comment + formatted + "\n"

        # ── Writing XML file in compressed format  ───────────────────────────

        # gz_path = cache_dir / CATALOG_GZ_FILENAME
        format, level = compression_filter.split(" ")
        print(output_path)
        if format == "xz":
            with lzma.open(output_path, 'wt') as f:
                f.write(final_xml)
        return ok_count

    # ─────────────────────────────────────────────
    # RPM extraction
    # ─────────────────────────────────────────────

    def _extract_rpm_to_dir(self, rpm_path: Path, dest_dir: str) -> None:
        """
        Extract RPM contents to dest_dir via rpm2cpio + cpio.
        These two tools are available on all RPM-based distributions.
        """
        rpm2cpio = subprocess.run(
            ["rpm2cpio", str(rpm_path)],
            capture_output=True,
            check=True,
        )
        subprocess.run(
            ["cpio", "--extract", "--make-directories", "--quiet"],
            input=rpm2cpio.stdout,
            cwd=dest_dir,
            check=True,
        )

    def _extract_metainfo_files(
            self,
            rpm_path: Path,
            targets: list[str],
            cache_path: Path,
            ) -> list[str]:
        """
        Extract target files from RPM to cache.
        Return the list of actually copied paths.
        """
        copied = []

        with tempfile.TemporaryDirectory() as tmp_dir:
            self._extract_rpm_to_dir(rpm_path, tmp_dir)

            pkg_name = rpm_path.stem
            dest_dir = cache_path / pkg_name
            dest_dir.mkdir(parents=True, exist_ok=True)

            for target in targets:
                relative = target.lstrip("/")
                extracted = Path(tmp_dir) / relative

                if not extracted.exists():
                    print(f"  ⚠  File not found after extraction: {relative}")
                    continue

                dest_file = dest_dir / extracted.name
                shutil.copy2(extracted, dest_file)
                copied.append(str(dest_file))
                # print(f"  ✔  Copied: {dest_file}")

        return copied

    # ─────────────────────────────────────────────
    # Individual AppStream XML generation
    # ─────────────────────────────────────────────

    # Mapping des groupes RPM vers les catégories AppStream
    # Voir https://specifications.freedesktop.org/menu-spec/latest/apa.html
    RPM_GROUP_TO_APPSTREAM_CATEGORIES = {
        # Accessibility
        'Accessibility': ['Utility', 'Accessibility'],

        # Archiving
        'Archiving/Backup': ['Utility', 'Archiving'],
        'Archiving/Cd burning': ['Utility', 'DiscBurning'],
        'Archiving/Compression': ['Utility', 'Compression'],
        'Archiving/Other': ['Utility', 'Archiving'],

        # Communications
        'Communications/Bluetooth': ['Network'],
        'Communications/Dial-Up': ['Network', 'Dialup'],
        'Communications/Fax': ['Office'],
        'Communications/Mobile': ['Network'],
        'Communications/Radio': ['AudioVideo', 'HamRadio'],
        'Communications/Serial': ['Network'],
        'Communications/Telephony': ['Network', 'Telephony'],

        # Databases
        'Databases': ['Development', 'Database'],

        # Development
        'Development/Basic': ['Development'],
        'Development/C': ['Development'],
        'Development/C++': ['Development'],
        'Development/C#': ['Development'],
        'Development/Databases': ['Development', 'Database'],
        'Development/Erlang': ['Development'],
        'Development/Golang': ['Development'],
        'Development/GNOME and GTK+': ['Development', 'GTK'],
        'Development/Java': ['Development', 'Java'],
        'Development/KDE and Qt': ['Development', 'Qt'],
        'Development/Kernel': ['Development'],
        'Development/OCaml': ['Development'],
        'Development/Other': ['Development'],
        'Development/Perl': ['Development'],
        'Development/PHP': ['Development', 'WebDevelopment'],
        'Development/Python': ['Development'],
        'Development/Ruby': ['Development'],
        'Development/Rust': ['Development'],
        'Development/Tools': ['Development'],
        'Development/Wayland': ['Development'],
        'Development/X11': ['Development'],

        # Documentation
        'Documentation': ['Documentation'],

        # Editors
        'Editors': ['Utility', 'TextEditor'],

        # Education
        'Education': ['Education'],

        # Emulators
        'Emulators': ['System', 'Emulator'],

        # File tools
        'File tools': ['Utility', 'FileTools'],

        # Games
        'Games/Adventure': ['Game', 'AdventureGame'],
        'Games/Arcade': ['Game', 'ArcadeGame'],
        'Games/Boards': ['Game', 'BoardGame'],
        'Games/Cards': ['Game', 'CardGame'],
        'Games/Other': ['Game'],
        'Games/Puzzles': ['Game', 'LogicGame'],
        'Games/Shooter': ['Game', 'ActionGame'],
        'Games/Simulation': ['Game', 'Simulation'],
        'Games/Sports': ['Game', 'SportsGame'],
        'Games/Strategy': ['Game', 'StrategyGame'],

        # Geography
        'Geography': ['Education', 'Geoscience'],

        # Graphical desktop
        'Graphical desktop/Cinnamon': ['System', 'DesktopSettings'],
        'Graphical desktop/Enlightenment': ['System', 'DesktopSettings'],
        'Graphical desktop/FVWM based': ['System', 'DesktopSettings'],
        'Graphical desktop/GNOME': ['System', 'DesktopSettings', 'GNOME'],
        'Graphical desktop/Icewm': ['System', 'DesktopSettings'],
        'Graphical desktop/KDE': ['System', 'DesktopSettings', 'KDE'],
        'Graphical desktop/MATE': ['System', 'DesktopSettings', 'MATE'],
        'Graphical desktop/Other': ['System', 'DesktopSettings'],
        'Graphical desktop/Sawfish': ['System', 'DesktopSettings'],
        'Graphical desktop/WindowMaker': ['System', 'DesktopSettings'],
        'Graphical desktop/Xfce': ['System', 'DesktopSettings', 'XFCE'],

        # Graphics
        'Graphics/3D': ['Graphics', '3DGraphics'],
        'Graphics/Editors and Converters': ['Graphics', 'RasterGraphics'],
        'Graphics/Utilities': ['Graphics'],
        'Graphics/Photography': ['Graphics', 'Photography'],
        'Graphics/Scanning': ['Graphics', 'Scanning'],
        'Graphics/Viewers': ['Graphics', 'Viewer'],

        # Monitoring
        'Monitoring': ['System', 'Monitor'],

        # Networking
        'Networking/Chat': ['Network', 'Chat'],
        'Networking/File transfer': ['Network', 'FileTransfer'],
        'Networking/IRC': ['Network', 'IRCClient'],
        'Networking/Instant messaging': ['Network', 'InstantMessaging'],
        'Networking/Mail': ['Network', 'Email'],
        'Networking/News': ['Network', 'News'],
        'Networking/Other': ['Network'],
        'Networking/Remote access': ['Network', 'RemoteAccess'],
        'Networking/WWW': ['Network', 'WebBrowser'],

        # Office
        'Office/Dictionary': ['Office', 'Dictionary'],
        'Office/Finance': ['Office', 'Finance'],
        'Office/Management': ['Office', 'ProjectManagement'],
        'Office/Organizer': ['Office', 'Calendar'],
        'Office/Utilities': ['Office'],
        'Office/Spreadsheet': ['Office', 'Spreadsheet'],
        'Office/Suite': ['Office'],
        'Office/Word processor': ['Office', 'WordProcessor'],

        # Publishing
        'Publishing': ['Office', 'Publishing'],

        # Sciences
        'Sciences/Astronomy': ['Education', 'Astronomy'],
        'Sciences/Biology': ['Education', 'Biology'],
        'Sciences/Chemistry': ['Education', 'Chemistry'],
        'Sciences/Computer science': ['Education', 'ComputerScience'],
        'Sciences/Geosciences': ['Education', 'Geoscience'],
        'Sciences/Mathematics': ['Education', 'Math'],
        'Sciences/Other': ['Education', 'Science'],
        'Sciences/Physics': ['Education', 'Physics'],

        # Security
        'Security': ['System', 'Security'],

        # Shells
        'Shells': ['System', 'TerminalEmulator'],

        # Sound
        'Sound/Editors and Converters': ['AudioVideo', 'AudioVideoEditing'],
        'Sound/Midi': ['AudioVideo', 'Midi'],
        'Sound/Mixers': ['AudioVideo', 'Mixer'],
        'Sound/Players': ['AudioVideo', 'Player'],
        'Sound/Utilities': ['AudioVideo'],

        # System
        'System/Base': ['System'],
        'System/Boot and Init': ['System'],
        'System/Cluster': ['System'],
        'System/Configuration': ['Settings'],
        'System/Fonts/Console': ['System'],
        'System/Fonts/True type': ['System'],
        'System/Fonts/Type1': ['System'],
        'System/Fonts/X11 bitmap': ['System'],
        'System/Internationalization': ['System'],
        'System/Kernel and hardware': ['System'],
        'System/Libraries': ['System'],
        'System/Networking': ['System', 'Network'],
        'System/Packaging': ['System', 'PackageManager'],
        'System/Printing': ['System', 'Printing'],
        'System/Servers': ['Network'],
        'System/Wayland': ['System'],
        'System/X11': ['System'],

        # Terminals
        'Terminals': ['System', 'TerminalEmulator'],

        # Text tools
        'Text tools': ['Utility', 'TextTools'],

        # Toys
        'Toys': ['Game'],

        # Video
        'Video/Editors and Converters': ['AudioVideo', 'VideoEditing'],
        'Video/Players': ['AudioVideo', 'Player'],
        'Video/Television': ['AudioVideo', 'TV'],
        'Video/Utilities': ['AudioVideo'],
    }

    def _sanitize_id(self, name: str) -> str:
        """
        Build a valid AppStream ID from RPM package name.
        Format : org.mageia.nom-du-paquet
        Ex: "my-app-1.0-1.x86_64"  →  "org.mageia.my-app"
        """
        clean = re.sub(r"-\d.*$", "", name)                   # retire NVR/arch
        clean = re.sub(r"[^a-zA-Z0-9]", "_", clean)       # caractères invalides
        clean = clean.strip("-")
        clean = clean.strip("_")
        return f"{ORG}.{clean}"

    def _generate_appstream_xml(
            self, 
            package_info,
            pkg_stem: str,
            bin_files: list[str],
            dest_dir: Path,
            desktop_info: dict | None = None,
            icon_path: str | None = None,
        ) -> str:
        """
        Generate a minimal AppStream .metainfo.xml file for a package
        that does not contain an embedded metainfo file.

        Args:
            package_info: collected info for RPM
            pkg_stem:     Filename without extension (ex: "monapp-1.0-1.x86_64")
            bin_files:    List of files under /usr/bin/ present in the RPM
            dest_dir:     Destination directory in cache
            desktop_info: Desktop file metadata (if found)
            icon_path:    Relative path of extracted icon (if found)

        Returns:
            Absolute path of generated XML file.
        """
        app_id   = self._sanitize_id(pkg_stem)

        # Use .desktop info if available, otherwise fallback to RPM
        if desktop_info and desktop_info.get("name"):
            name = desktop_info["name"]
        else:
            name = package_info.name

        if desktop_info and desktop_info.get("comment"):
            summary = desktop_info["comment"]
        else:
            summary = package_info.summary

        desc_raw = package_info.summary
        license_ = package_info.license
        version  = package_info.version
        group    = package_info.group

        # RPM build date → AppStream release date (YYYY-MM-DD)
        build_time = package_info.buildtime
        if build_time:
            release_date = datetime.fromtimestamp(
                int(build_time), tz=timezone.utc
            ).strftime("%Y-%m-%d")
        else:
            release_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        # Component type
        if desktop_info and desktop_info.get("type") == "Application":
            component_type = "desktop-application"
        elif bin_files:
            component_type = "console-application"
        else:
            component_type = "generic"

        # Categories: priority to .desktop, otherwise RPM group, otherwise fallback
        categories = []
        if desktop_info and desktop_info.get("categories"):
            # Desktop file categories are already in AppStream format
            categories = desktop_info["categories"]
        elif group:
            categories = self.RPM_GROUP_TO_APPSTREAM_CATEGORIES.get(group, [])
            if not categories:
                print(f"  ⚠  RPM group '{group}' not mapped, no AppStream category. {version} {license} {summary}")

        # Default fallback if still empty
        if not categories:
            if bin_files:
                categories = ['System', 'ConsoleOnly']
            else:
                categories = ['System']

        # ── Construction de l'arbre XML ────────────────────────────────────
        component = ET.Element("component", type=component_type)
        ET.SubElement(component, "id").text               = app_id
        ET.SubElement(component, "name").text             = name
        # metadata_license : use RPM license; recommended value
        # for metadata alone is "CC0-1.0" or "FSFAP".
        ET.SubElement(component, "metadata_license").text = license_
        ET.SubElement(component, "summary").text          = summary
        ET.SubElement(component, "pkgname").text          = name  # RPM package name

        # Description: AppStream requires tagged XML (<p>, <ul>...)
        # Lines not ending with a period are joined with a space
        desc_el = ET.SubElement(component, "description")
        paragraphs = []
        current_para = []

        for line in desc_raw.strip().splitlines():
            line = line.strip()
            if not line:
                # Empty line: end current paragraph
                if current_para:
                    paragraphs.append(" ".join(current_para))
                    current_para = []
                continue

            current_para.append(line)

            # If line ends with a period, end the paragraph
            if line.endswith("."):
                paragraphs.append(" ".join(current_para))
                current_para = []

        # Add last paragraph if not empty
        if current_para:
            paragraphs.append(" ".join(current_para))

        # Create <p> elements
        for para in paragraphs:
            if para:
                ET.SubElement(desc_el, "p").text = para

        # Releases
        releases_el = ET.SubElement(component, "releases")
        ET.SubElement(releases_el, "release", version=version, date=release_date)

        # Categories
        if categories:
            categories_el = ET.SubElement(component, "categories")
            for cat in categories:
                ET.SubElement(categories_el, "category").text = cat

        # Icon
        if icon_path:
            # icon_path already contains just the name without path or extension
            icon_el = ET.SubElement(component, "icon", type="cached")
            icon_el.text = icon_path

        # Provides / binaries
        if bin_files:
            provides_el = ET.SubElement(component, "provides")
            for bin_path in bin_files:
                ET.SubElement(provides_el, "binary").text = Path(bin_path).name

        # ── Sérialisation compacte ────────────────────────────────────────
        ET.tostring(component, encoding="unicode", xml_declaration=False)

        # Parser et reformater sans minidom
        # On utilise ET.indent (Python 3.9+)
        tree = ET.ElementTree(component)
        ET.indent(tree, space="  ", level=0)
        formatted = ET.tostring(component, encoding="unicode", xml_declaration=False)

        header_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            "<!-- Generated by extract_metainfo.py — no upstream metainfo found -->",
        ]
        final_xml = "\n".join(header_lines) + "\n" + formatted + "\n"

        dest_dir.mkdir(parents=True, exist_ok=True)
        out_path = dest_dir / f"{app_id}.metainfo.xml"
        out_path.write_text(final_xml, encoding="utf-8")
        # print(f"  ✎  Generated AppStream XML: {out_path}")
        return str(out_path)

    # ─────────────────────────────────────────────
    # AppStream catalog — concatenation + gzip
    # ─────────────────────────────────────────────

    def _iter_xml_files(self, cache_path: Path) -> Iterator[Path]:
        """
        Recursively traverse cache directory and yield each file
        .appdata.xml or .metainfo.xml excluding the catalog itself.
        """
        exclude = {self.STATE_FILENAME}
        for xml_file in sorted(cache_path.rglob("*.xml")):
            if xml_file.name not in exclude:
                yield xml_file

    def _strip_ns(self, element: ET.Element) -> ET.Element:
        """
        Recursively remove XML namespaces from tags and attributes
        from an element to avoid ns0: prefixes in the catalog.
        """
        for el in element.iter():
            if "}" in el.tag:
                el.tag = el.tag.split("}", 1)[1]
            el.attrib = {
                (k.split("}", 1)[1] if "}" in k else k): v
                for k, v in el.attrib.items()
            }
        return element

    def _parse_component(self, xml_file: Path) -> ET.Element | None:
        """
        Parse a metainfo file and return the <component> element.
        Handle files where root is directly <component>
        or nested in another tag (e.g. <application>).
        Return None if file is invalid or does not contain a component.
        """
        try:
            tree = ET.parse(xml_file)
            root = tree.getroot()
        except ET.ParseError as e:
            print(f"  ⚠  Invalid XML, ignored ({xml_file.name}) : {e}")
            return None

        def _local(tag: str) -> str:
            return tag.split("}", 1)[1] if "}" in tag else tag

        if _local(root.tag) == "component":
            return self._strip_ns(root)

        # Search for first <component> descendant
        for child in root.iter():
            if _local(child.tag) == "component":
                return self._strip_ns(child)

        print(f"  ⚠  No <component> in {xml_file.name}, ignored.")
        return None

    # ─────────────────────────────────────────────
    # Desktop file parsing and icon extraction
    # ─────────────────────────────────────────────

    def _parse_desktop_file(self, desktop_path: Path) -> dict:
        """
        Parse a .desktop file and extract relevant metadata.

        Returns:
            dict with: icon, categories, type, name, comment
        """
        result = {
            "icon": None,
            "categories": [],
            "type": None,
            "name": None,
            "comment": None,
        }

        try:
            content = desktop_path.read_text(encoding="utf-8", errors="ignore")
        except Exception as e:
            print(f"  ⚠  Cannot read {desktop_path.name}: {e}")
            return result

        in_desktop_entry = False

        for line in content.splitlines():
            line = line.strip()

            # Detect [Desktop Entry] section
            if line == "[Desktop Entry]":
                in_desktop_entry = True
                continue
            elif line.startswith("[") and line.endswith("]"):
                in_desktop_entry = False
                continue

            if not in_desktop_entry or not line or line.startswith("#"):
                continue

            if "=" not in line:
                continue

            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()

            if key == "Icon":
                result["icon"] = value
            elif key == "Categories":
                # Format: "Category1;Category2;Category3;"
                result["categories"] = [
                    c.strip() for c in value.split(";")
                    if c.strip()
                ]
            elif key == "Type":
                result["type"] = value
            elif key == "Name" and not result["name"]:
                # Take the first Name (without locale)
                result["name"] = value
            elif key == "Comment" and not result["comment"]:
                result["comment"] = value

        return result

    def _find_icon_in_rpm(
        self,
        file_list: list[str],
        icon_name: str,
        icon_paths: list[str],
        icon_extensions: tuple[str, ...],
    ) -> str | None:
        """
        Search for an icon in the RPM file list.

        Args:
            file_list: Complete list of files in the RPM
            icon_name: Icon name (without extension or path)
            icon_paths: Paths to search (in order of preference)
            icon_extensions: Allowed extensions

        Returns:
            Full path of found icon, or None
        """
        # If icon_name is already an absolute path, search for it directly
        if icon_name.startswith("/"):
            if icon_name in file_list:
                return icon_name
            # Try with different extensions
            for ext in icon_extensions:
                if not icon_name.endswith(ext):
                    test_path = icon_name + ext
                    if test_path in file_list:
                        return test_path
            return None

        # Remove extension if present
        icon_base = icon_name
        for ext in icon_extensions:
            if icon_base.endswith(ext):
                icon_base = icon_base[:-len(ext)]
                break

        # Search in standard paths by order of preference
        for base_path in icon_paths:
            for ext in icon_extensions:
                candidate = f"{base_path}{icon_base}{ext}"
                if candidate in file_list:
                    return candidate

        return None

    def _extract_icon(
        self,
        rpm_path: Path,
        icon_path_in_rpm: str,
        app_id: str,
        cache_path: Path,
    ) -> str | None:
        """
        Extract an icon from RPM to cache icons/ directory.

        Args:
            rpm_path: Path to RPM file
            icon_path_in_rpm: Icon path in the RPM
            app_id: AppStream ID of the component (used as base name)
            cache_path: Root cache directory

        Returns:
            Icon name without path or extension (ex: "org.mageia.firefox"), or None
        """
        icons_dir = cache_path / ICONS_SUBDIR
        icons_dir.mkdir(parents=True, exist_ok=True)

        # Determine extension from source file
        ext = Path(icon_path_in_rpm).suffix or ".png"

        # Filename: app_id + extension (ex: org.mageia.firefox.png)
        dest_filename = f"{app_id}{ext}"
        dest_path = icons_dir / dest_filename

        # If already extracted, return name without extension
        if dest_path.exists():
            return app_id

        with tempfile.TemporaryDirectory() as tmp_dir:
            try:
                self._extract_rpm_to_dir(rpm_path, tmp_dir)

                relative = icon_path_in_rpm.lstrip("/")
                extracted = Path(tmp_dir) / relative

                if not extracted.exists():
                    print(f"  ⚠  Icon not found after extraction: {relative}")
                    return None

                shutil.copy2(extracted, dest_path)
                print(f"  🖼  Extracted icon: {dest_filename}")
                # Return only the name without extension
                return app_id

            except Exception as e:
                print(f"  ✗ Icon extraction error: {e}")
                return None


    # ─────────────────────────────────────────────
    # Utilities
    # ─────────────────────────────────────────────

    def _now_iso(self, ) -> str:
        """Return current timestamp in ISO-8601 format."""
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    def _save_state(self, state: dict) -> None:
        """Save unified state to .genhdlist/state.json."""
        cache_path = Path(self.CACHE_DIR)
        if cache_path is None:
            return
        cache_path.mkdir(parents=True, exist_ok=True)
        state_file = cache_path / self.STATE_FILENAME
        state_file.write_text(
            json.dumps(state, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

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
        state_file = Path(self.CACHE_DIR) / self.STATE_FILENAME
        if state_file.exists():
            try:
                return json.loads(state_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                print("Warning: corrupted state file, falling back to full rebuild.")
        return {}

    def _purge_missing_rpms(self, state: dict) -> None:
        """
        Remove from state (and cache) entries corresponding
        to RPMs that no longer exist in source directory.
        """
        cache_path = Path(self.CACHE_DIR)
        missing = [name for name in list(state) if name not in self.results.keys()]

        for name in missing:
            print(f"\n[PURGE] {name} no longer present, removing from state.")
            if cache_path is not None:
                pkg_dir = cache_path / Path(name).stem
                if pkg_dir.exists():
                    shutil.rmtree(pkg_dir)
                    print(f"  ✔  Cache directory removed: {pkg_dir}")
            del state[name]

        if missing:
            self._save_state(state)

    def _print_summary(
            self,
            results:     dict,
            skipped:     list,
            generated:   list,
            errors:      list,
            ) -> None:
        """Display execution summary."""
        extracted_count = sum(len(v["extracted"]) for v in results.values())
        print("\n" + "═" * 52)
        print("SUMMARY")
        print("═" * 52)
        print(f"  Processed packages           : {len(results)}")
        print(f"  Extracted metainfo files     : {extracted_count}")
        print(f"  Generated AppStream files    : {len(generated)}")
        print(f"  Skipped packages (unchanged) : {len(skipped)}")
        print(f"  Errors                       : {len(errors)}")
        if errors:
            print(f"  Packages with errors: {', '.join(errors)}")
        # if catalog_xml:
        #     print(f"  XML catalog      : {catalog_xml}")
        # if catalog_gz:
        #     print(f"  Gzip catalog     : {catalog_gz}")
        print("═" * 52)
