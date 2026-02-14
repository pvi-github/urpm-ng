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
import subprocess
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Tuple
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


class AppStreamManager:
    """Manages AppStream metadata for all media."""

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
