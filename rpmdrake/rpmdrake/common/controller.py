"""Controller for rpmdrake-ng.

UI-agnostic controller that handles:
- Package list loading and filtering
- Search with debouncing
- Selection management
- Transaction coordination
"""

from concurrent.futures import ThreadPoolExecutor, Future
from dataclasses import dataclass
from threading import Timer, Lock
from typing import TYPE_CHECKING, List, Set, Optional, Callable, Dict, Any
import platform

if TYPE_CHECKING:
    from urpm.core.database import PackageDatabase
    from urpm.core.resolver import Resolver, Resolution
    from urpm.core.operations import PackageOperations
    from .interfaces import ViewInterface

from .models import PackageDisplayInfo, FilterState, PackageState

__all__ = ["Controller", "ControllerConfig"]


@dataclass
class ControllerConfig:
    """Controller configuration."""
    debounce_ms: int = 300
    search_limit: int = 500
    arch: str = ""

    def __post_init__(self):
        if not self.arch:
            machine = platform.machine()
            self.arch = "x86_64" if machine == "x86_64" else machine


class Controller:
    """UI-agnostic controller for rpmdrake-ng.

    Handles business logic and state management. Communicates with
    the view through the ViewInterface abstraction.
    """

    def __init__(
        self,
        db: 'PackageDatabase',
        view: 'ViewInterface',
        config: Optional[ControllerConfig] = None
    ):
        """Initialize controller.

        Args:
            db: Package database instance.
            view: View interface implementation.
            config: Optional configuration.
        """
        self.db = db
        self.view = view
        self.config = config or ControllerConfig()

        # Operations helper (lazy import to avoid circular deps)
        from urpm.core.operations import PackageOperations
        self.ops = PackageOperations(self.db)

        # State
        self.filter_state = FilterState()
        self.selection: Set[str] = set()  # Selected package names
        self._packages: List[PackageDisplayInfo] = []
        self._installed_cache: Dict[str, str] = {}  # name -> version
        self._installed_packages: List[dict] = []  # Full list for filtering
        self._dependency_packages: Set[str] = set()  # Packages installed as deps
        self._orphan_packages: Set[str] = set()  # Orphan packages (deps no longer needed)
        self._upgradeable_packages: Set[str] = set()  # Packages with available updates
        self._available_groups: List[str] = []  # Available package groups/categories

        # Async search
        self._search_executor = ThreadPoolExecutor(max_workers=1)
        self._debounce_timer: Optional[Timer] = None
        self._pending_future: Optional[Future] = None
        self._lock = Lock()

        # Cache for incremental filtering
        self._cache_term: str = ""
        self._cache_results: List[dict] = []

        # Current transaction helper (for cancel)
        self._current_helper = None

    # =========================================================================
    # Package List Management
    # =========================================================================

    def load_initial(self) -> None:
        """Load initial package list based on filter state."""
        self._load_installed_cache()

        # Check if there are upgrades available
        if not self._upgradeable_packages:
            # No upgrades - clear all state filters
            self.filter_state.states.clear()
            # Notify view to update filter checkboxes
            if hasattr(self.view, 'on_filter_state_changed'):
                self.view.on_filter_state_changed()

        self._refresh_packages()

    def refresh_after_transaction(self) -> None:
        """Refresh package list after a transaction completes."""
        self._load_installed_cache()
        self._invalidate_cache()
        self._refresh_packages()
        self.clear_selection()

    def _load_installed_cache(self) -> None:
        """Load installed packages into cache."""
        try:
            self._installed_packages = self.ops.get_installed_packages()
            self._installed_cache = {
                p['name']: f"{p['version']}-{p['release']}"
                for p in self._installed_packages
            }
            # Load dependency packages list
            self._dependency_packages = self._load_dependency_packages()
            # Load orphan packages list
            self._orphan_packages = self._load_orphan_packages()
            # Load upgradeable packages list
            self._upgradeable_packages = self._load_upgradeable_packages()
            # Load available groups
            self._available_groups = self._load_available_groups()
        except Exception as e:
            self.view.show_error("Erreur", f"Impossible de charger les paquets installés: {e}")

    def _load_dependency_packages(self) -> Set[str]:
        """Load list of packages installed as dependencies."""
        from pathlib import Path
        deps_file = Path("/var/lib/rpm/installed-through-deps.list")
        deps = set()
        if deps_file.exists():
            try:
                content = deps_file.read_text()
                for line in content.splitlines():
                    line = line.strip()
                    if line and not line.startswith('#'):
                        parts = line.split()
                        if parts:
                            deps.add(parts[0].lower())
            except (IOError, OSError):
                pass
        return deps

    def _load_orphan_packages(self) -> Set[str]:
        """Load list of orphan packages using Resolver."""
        from urpm.core.resolver import Resolver
        orphans = set()
        try:
            resolver = Resolver(self.db, arch=self.config.arch)
            orphan_actions = resolver.find_all_orphans()
            for action in orphan_actions:
                orphans.add(action.name.lower())
        except Exception:
            pass
        return orphans

    def _load_upgradeable_packages(self) -> Set[str]:
        """Load list of packages with available updates.

        Uses a fresh subprocess to query the RPM database to avoid any
        caching issues that might show stale data after a transaction.
        """
        import gc
        import subprocess

        # Force garbage collection to release any stale rpm connections
        gc.collect()

        upgradeable = set()

        try:
            # Get installed packages via subprocess (guaranteed fresh)
            result = subprocess.run(
                ['rpm', '-qa', '--qf', '%{NAME}\\t%{EPOCH}\\t%{VERSION}\\t%{RELEASE}\\t%{ARCH}\\n'],
                capture_output=True,
                timeout=60
            )
            installed = {}
            for line in result.stdout.decode(errors='replace').splitlines():
                parts = line.split('\t')
                if len(parts) >= 5:
                    name, epoch, version, release, arch = parts[:5]
                    epoch = '0' if epoch == '(none)' else epoch
                    installed[name.lower()] = {
                        'epoch': int(epoch),
                        'version': version,
                        'release': release,
                        'arch': arch,
                    }

            # Compare with available packages from synthesis database
            import sqlite3
            try:
                import rpm
                def compare_evr(evr1, evr2):
                    """Compare two (epoch, version, release) tuples.
                    Returns: >0 if evr1 > evr2, <0 if evr1 < evr2, 0 if equal.
                    """
                    e1, v1, r1 = evr1
                    e2, v2, r2 = evr2
                    return rpm.labelCompare(
                        (str(e1 or 0), v1, r1),
                        (str(e2 or 0), v2, r2)
                    )
            except ImportError:
                # Fallback: simple string comparison (less accurate)
                def compare_evr(evr1, evr2):
                    e1, v1, r1 = evr1
                    e2, v2, r2 = evr2
                    t1 = (int(e1 or 0), v1, r1)
                    t2 = (int(e2 or 0), v2, r2)
                    if t1 > t2:
                        return 1
                    elif t1 < t2:
                        return -1
                    return 0

            conn = sqlite3.connect(self.db.db_path)
            cur = conn.cursor()

            # Query available packages from our media database
            cur.execute('''
                SELECT p.name, p.epoch, p.version, p.release, p.arch
                FROM packages p
                JOIN media m ON p.media_id = m.id
                WHERE m.enabled = 1
            ''')

            for row in cur.fetchall():
                name, epoch, version, release, arch = row
                name_lower = name.lower()

                if name_lower not in installed:
                    continue  # Not installed

                inst = installed[name_lower]

                # Skip if arch doesn't match (except noarch)
                if inst['arch'] != arch and arch != 'noarch' and inst['arch'] != 'noarch':
                    continue

                # Compare versions
                inst_evr = (inst['epoch'], inst['version'], inst['release'])
                avail_evr = (epoch or 0, version, release)

                if compare_evr(avail_evr, inst_evr) > 0:
                    upgradeable.add(name_lower)

            conn.close()

        except Exception:
            # Fallback to resolver-based detection
            try:
                from urpm.core.resolver import Resolver
                resolver = Resolver(self.db, arch=self.config.arch)
                resolution = resolver.resolve_upgrade()
                if resolution.success:
                    for action in resolution.actions:
                        if action.action.name == 'UPGRADE':
                            upgradeable.add(action.name.lower())
            except Exception:
                pass

        return upgradeable

    def _load_available_groups(self) -> List[str]:
        """Load list of available package groups from database."""
        import sqlite3
        groups = []
        try:
            conn = sqlite3.connect(self.db.db_path)
            cur = conn.cursor()
            cur.execute('''
                SELECT DISTINCT group_name FROM packages
                WHERE group_name IS NOT NULL AND group_name != ''
                ORDER BY group_name
            ''')
            groups = [row[0] for row in cur.fetchall()]
            conn.close()
        except Exception:
            pass
        return groups

    def get_available_groups(self) -> List[str]:
        """Return list of available package groups."""
        return self._available_groups

    def _refresh_packages(self) -> None:
        """Refresh package list based on current filters."""
        self.view.show_loading(True)

        future = self._search_executor.submit(self._query_packages_sync)
        future.add_done_callback(self._on_query_done)

    def _query_packages_sync(self) -> List[dict]:
        """Query packages synchronously (runs in thread).

        Logic :
        - Avec terme de recherche : chercher dans la BDD, filtrer par état
        - Avec catégorie : charger les paquets de cette catégorie
        - Sans terme ni catégorie : selon le filtre d'état actif
        """
        term = self.filter_state.search_term
        states = self.filter_state.states
        category = self.filter_state.category

        if term:
            # Recherche dans la BDD (nom + provides)
            results = self._search_with_cache(term)
        elif category:
            # Catégorie sélectionnée : charger tous les paquets de cette catégorie
            results = self._get_packages_by_category(category)
        elif PackageState.UPGRADES in states:
            # Mises à jour
            results = self._get_upgrade_packages()
            results = self._apply_display_filters(results)
            return results
        elif PackageState.AVAILABLE in states and PackageState.INSTALLED not in states:
            # "Disponibles" seul sans recherche ni catégorie : trop de paquets
            return []
        else:
            # Par défaut : paquets installés
            results = [dict(p) for p in self._installed_packages]

        # Enrichir avec statut d'installation
        results = self._enrich_packages(results)

        # Filtrer par état si des filtres sont actifs
        if states:
            results = self._apply_state_filters(results, states)

        # Filtres d'affichage (libs, devel, etc.)
        results = self._apply_display_filters(results)

        return results

    def _get_packages_by_category(self, category: str) -> List[dict]:
        """Get all packages in a category (prefix match)."""
        import sqlite3
        results = []
        try:
            conn = sqlite3.connect(self.db.db_path)
            cur = conn.cursor()
            cur.execute('''
                SELECT name, version, release, arch, summary, group_name
                FROM packages
                WHERE group_name LIKE ? OR group_name = ?
                ORDER BY name
                LIMIT ?
            ''', (category + '/%', category, self.config.search_limit))
            for row in cur.fetchall():
                results.append({
                    'name': row[0],
                    'version': row[1],
                    'release': row[2],
                    'arch': row[3],
                    'summary': row[4],
                    'group': row[5],
                })
            conn.close()
        except Exception:
            pass
        return results

    def _get_upgrade_packages(self) -> List[dict]:
        """Get packages with available upgrades."""
        results = []
        for name_lower in self._upgradeable_packages:
            for p in self._installed_packages:
                if p['name'].lower() == name_lower:
                    pkg = dict(p)
                    pkg['installed'] = True
                    pkg['has_update'] = True
                    pkg['install_reason'] = 'explicit'  # Simplification
                    results.append(pkg)
                    break
        return results

    def _enrich_packages(self, packages: List[dict]) -> List[dict]:
        """Enrich packages with install status, install reason, and update status."""
        for p in packages:
            name = p['name']
            name_lower = name.lower()

            # Install status
            p['installed'] = name in self._installed_cache

            # Update status
            p['has_update'] = name_lower in self._upgradeable_packages

            # Install reason (only for installed packages)
            if p['installed']:
                # Check orphan first (orphans are a subset of dependencies)
                if name_lower in self._orphan_packages:
                    p['install_reason'] = 'orphan'
                elif name_lower in self._dependency_packages:
                    p['install_reason'] = 'dependency'
                else:
                    p['install_reason'] = 'explicit'
            else:
                p['install_reason'] = None
        return packages

    def _apply_state_filters(self, packages: List[dict], states: Set[PackageState]) -> List[dict]:
        """Apply state filters (installed, available, upgrades)."""
        if not states:
            # No state filters - return all
            return packages

        filtered = []
        for p in packages:
            dominated_by = p.get('install_reason')
            installed = p.get('installed', False)
            has_update = p.get('has_update', False)

            # Check if package matches any active state filter
            matches = False

            if PackageState.UPGRADES in states and has_update:
                matches = True
            if PackageState.INSTALLED in states and installed:
                matches = True
            if PackageState.AVAILABLE in states and not installed:
                matches = True

            if matches:
                filtered.append(p)

        return filtered

    def _search_with_cache(self, term: str) -> List[dict]:
        """Search with incremental cache."""
        # Check if we can filter cached results
        if (
            self._cache_term
            and term.startswith(self._cache_term)
            and len(term) > len(self._cache_term)
        ):
            # Filter cached results
            term_lower = term.lower()
            results = [
                dict(p) for p in self._cache_results
                if term_lower in p['name'].lower()
            ]
        else:
            # Full database query
            if term:
                results = self.db.search(
                    term,
                    limit=self.config.search_limit,
                    search_provides=True
                )
            else:
                # No term - get updates by default
                results = []

            # Update cache (store originals)
            self._cache_term = term
            self._cache_results = results
            # Return copies to avoid modifying cache
            results = [dict(p) for p in results]

        return results

    def _apply_display_filters(self, packages: List[dict]) -> List[dict]:
        """Apply display filters (libs, devel, debug, i586, install reason, category, tasks)."""
        fs = self.filter_state
        filtered = []

        for p in packages:
            name = p['name']
            arch = p.get('arch', '')
            group = p.get('group', '')
            install_reason = p.get('install_reason')

            # Exclusive filter: only show task-* meta-packages
            if fs.show_tasks and not name.startswith('task-'):
                continue

            # Filter by category (prefix match for hierarchy)
            if fs.category:
                if not group or not group.startswith(fs.category):
                    continue

            # Filter by install reason (only for installed packages)
            if install_reason:
                if install_reason == 'explicit' and not fs.show_explicit:
                    continue
                if install_reason == 'dependency' and not fs.show_dependencies:
                    continue
                if install_reason == 'orphan' and not fs.show_orphans:
                    continue

            # Filter libraries (packages starting with 'lib', excluding libreoffice)
            if not fs.show_libs and name.startswith('lib') and not name.startswith('libreoffice'):
                continue

            # Filter devel packages
            if not fs.show_devel and name.endswith('-devel'):
                continue

            # Filter debug packages
            if not fs.show_debug and ('-debug' in name or name.endswith('-debuginfo')):
                continue

            # Filter 32-bit on 64-bit system
            if not fs.show_i586 and arch == 'i586' and self.config.arch == 'x86_64':
                continue

            # Filter by language
            # Check if package is a language pack (ends with -XX where XX is lang code)
            if '-' in name:
                suffix = name.rsplit('-', 1)[-1]
                if len(suffix) == 2 and suffix.isalpha():
                    # Looks like a language code
                    if suffix not in fs.languages and suffix not in ('en',):
                        continue

            filtered.append(p)

        return filtered

    def _on_query_done(self, future: Future) -> None:
        """Handle query completion."""
        try:
            results = future.result()
            packages = self._convert_to_display_info(results)
            self._packages = packages
            self.view.on_package_list_update(packages)
        except Exception as e:
            self.view.show_error("Erreur", f"Erreur lors de la recherche: {e}")
        finally:
            self.view.show_loading(False)

    def _convert_to_display_info(self, packages: List[dict]) -> List[PackageDisplayInfo]:
        """Convert database results to display info."""
        from .models import InstallReason

        # Map string reasons to enum
        reason_map = {
            'explicit': InstallReason.EXPLICIT,
            'dependency': InstallReason.DEPENDENCY,
            'orphan': InstallReason.ORPHAN,
        }

        result = []
        for i, p in enumerate(packages, start=1):
            installed_version = self._installed_cache.get(p['name'])
            is_installed = p.get('installed', False)

            # Get install reason from enriched data
            reason_str = p.get('install_reason')
            install_reason = reason_map.get(reason_str) if reason_str else None

            result.append(PackageDisplayInfo(
                name=p['name'],
                version=p.get('version', ''),
                release=p.get('release', ''),
                arch=p.get('arch', 'x86_64'),
                summary=p.get('summary', ''),
                installed=is_installed,
                installed_version=installed_version,
                has_update=p.get('has_update', False),
                install_reason=install_reason,
                selected=p['name'] in self.selection,
                row_number=i,
            ))
        return result

    # =========================================================================
    # Search with Debounce
    # =========================================================================

    def set_search_term(self, term: str) -> None:
        """Set search term with debouncing.

        Args:
            term: Search term.
        """
        with self._lock:
            # Cancel pending timer
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()

            # Update filter state immediately
            self.filter_state.search_term = term

            # Start new timer
            self._debounce_timer = Timer(
                self.config.debounce_ms / 1000.0,
                self._execute_search
            )
            self._debounce_timer.start()

    def _execute_search(self) -> None:
        """Execute search after debounce delay."""
        with self._lock:
            self._debounce_timer = None

        # Cancel any pending query
        if self._pending_future and not self._pending_future.done():
            self._pending_future.cancel()

        self._refresh_packages()

    # =========================================================================
    # Filter Management
    # =========================================================================

    def toggle_state_filter(self, state: PackageState) -> None:
        """Toggle a state filter.

        Args:
            state: State to toggle.
        """
        if state in self.filter_state.states:
            # Don't allow removing the last state
            if len(self.filter_state.states) > 1:
                self.filter_state.states.remove(state)
        else:
            self.filter_state.states.add(state)

        self._invalidate_cache()
        self._refresh_packages()

    def set_display_filter(self, filter_name: str, value: bool) -> None:
        """Set a display filter.

        Args:
            filter_name: One of 'libs', 'devel', 'debug', 'i586'.
            value: True to show, False to hide.
        """
        attr_name = f"show_{filter_name}"
        if hasattr(self.filter_state, attr_name):
            setattr(self.filter_state, attr_name, value)
            self._refresh_packages()

    def toggle_display_filter(self, filter_name: str) -> None:
        """Toggle a display filter.

        Args:
            filter_name: One of 'libs', 'devel', 'debug', 'i586'.
        """
        attr_name = f"show_{filter_name}"
        if hasattr(self.filter_state, attr_name):
            current = getattr(self.filter_state, attr_name)
            setattr(self.filter_state, attr_name, not current)
            self._refresh_packages()

    def set_category_filter(self, category: Optional[str]) -> None:
        """Set category filter.

        Args:
            category: Category prefix to filter by, or None for all.
        """
        self.filter_state.category = category
        self._refresh_packages()

    def _invalidate_cache(self) -> None:
        """Invalidate search cache."""
        self._cache_term = ""
        self._cache_results = []

    # =========================================================================
    # Selection Management
    # =========================================================================

    def select_package(self, name: str) -> None:
        """Add package to selection.

        Args:
            name: Package name.
        """
        self.selection.add(name)
        self._update_selection_display()

    def unselect_package(self, name: str) -> None:
        """Remove package from selection.

        Args:
            name: Package name.
        """
        self.selection.discard(name)
        self._update_selection_display()

    def toggle_selection(self, name: str) -> None:
        """Toggle package selection.

        Args:
            name: Package name.
        """
        if name in self.selection:
            self.selection.discard(name)
        else:
            self.selection.add(name)
        self._update_selection_display()

    def select_all(self) -> None:
        """Select all visible packages."""
        for pkg in self._packages:
            self.selection.add(pkg.name)
        self._update_selection_display()

    def clear_selection(self) -> None:
        """Clear selection."""
        self.selection.clear()
        self._update_selection_display()

    def _update_selection_display(self) -> None:
        """Update package list with current selection state."""
        for pkg in self._packages:
            pkg.selected = pkg.name in self.selection
        self.view.on_package_list_update(self._packages)

    # =========================================================================
    # Actions
    # =========================================================================

    def install_selection(self) -> None:
        """Install selected packages."""
        if not self.selection:
            return
        self._execute_action('install', list(self.selection))

    def erase_selection(self) -> None:
        """Remove selected packages."""
        if not self.selection:
            return
        self._execute_action('erase', list(self.selection))

    def upgrade_selection(self) -> None:
        """Upgrade selected packages."""
        if not self.selection:
            return
        self._execute_action('upgrade', list(self.selection))

    def upgrade_all(self) -> None:
        """Upgrade all packages with updates."""
        self._execute_action('upgrade', ['__all__'])

    def _execute_action(self, action: str, packages: List[str]) -> None:
        """Execute an action on packages.

        Args:
            action: One of 'install', 'erase', 'upgrade'.
            packages: List of package names.
        """
        from urpm.core.resolver import Resolver

        # Resolve first to show detailed confirmation
        self.view.show_loading(True)
        self.view.on_progress('status', 'Résolution des dépendances...', 0, 0)

        try:
            resolver = Resolver(self.db, arch=self.config.arch)
            choices: Dict[str, str] = {}  # capability -> chosen package

            # Resolution loop: handle alternatives by asking user
            max_iterations = 20  # Prevent infinite loops
            for _ in range(max_iterations):
                if action == 'install':
                    resolution = resolver.resolve_install(packages, choices=choices)
                elif action == 'erase':
                    resolution = resolver.resolve_remove(packages)
                    break  # No alternatives for erase
                elif action == 'upgrade':
                    if packages == ['__all__']:
                        resolution = resolver.resolve_upgrade()
                        break  # Full upgrade doesn't support alternatives loop
                    else:
                        resolution = resolver.resolve_install(packages, choices=choices)
                else:
                    self.view.show_loading(False)
                    return

                # Check if we have alternatives that need user choice
                if resolution.alternatives:
                    self.view.show_loading(False)

                    for alt in resolution.alternatives:
                        # Ask user to choose
                        choice = self.view.show_alternative_choice(
                            alt.capability,
                            alt.required_by,
                            alt.providers
                        )

                        if not choice:
                            # User cancelled
                            return

                        choices[alt.capability] = choice

                    # Re-resolve with new choices
                    self.view.show_loading(True)
                    self.view.on_progress('status', 'Résolution des dépendances...', 0, 0)
                    continue

                # No more alternatives, we're done resolving
                break

            self.view.show_loading(False)

            if not resolution.success:
                problems = "; ".join(resolution.problems) if resolution.problems else "Échec de la résolution"
                self.view.show_error("Erreur de résolution", problems)
                return

            if not resolution.actions:
                self.view.show_error("Information", "Rien à faire.")
                return

            # Build detailed summary for confirmation
            requested_set = {p.lower() for p in packages} if packages != ['__all__'] else set()
            summary = self._build_resolution_summary(resolution, action, requested_set)

            # Show detailed confirmation
            confirmed = self.view.show_transaction_confirmation(action, summary)
            if not confirmed:
                return

        except Exception as e:
            self.view.show_loading(False)
            self.view.show_error("Erreur", f"Résolution impossible: {e}")
            return

        from .helper_client import HelperClient, TransactionResult, DownloadSlotInfo

        self.view.show_loading(True)
        self.view.start_transaction(action)

        def on_status(message: str):
            self.view.on_progress('status', message, 0, 0)

        def on_download_progress(
            name: str,
            current: int,
            total: int,
            bytes_done: int,
            bytes_total: int,
            slots: list
        ):
            # Convert DownloadSlotInfo to dicts for signal passing
            slot_dicts = []
            for s in slots:
                if isinstance(s, DownloadSlotInfo):
                    slot_dicts.append({
                        'slot': s.slot,
                        'name': s.name,
                        'bytes_done': s.bytes_done,
                        'bytes_total': s.bytes_total,
                        'source': s.source,
                        'source_type': s.source_type,
                    })
                else:
                    slot_dicts.append(s)
            self.view.on_download_progress(current, total, bytes_done, bytes_total, slot_dicts)

        def on_install_progress(name: str, current: int, total: int):
            # Use appropriate method based on action type
            if action == 'erase':
                self.view.on_erase_progress(name, current, total)
            else:
                self.view.on_install_progress(name, current, total)

        def on_error(message: str):
            self._current_helper = None
            self.view.show_loading(False)
            self.view.finish_transaction()
            self.view.show_error("Erreur", message)

        def on_done(result: TransactionResult):
            self._current_helper = None
            self.view.show_loading(False)
            self.view.finish_transaction()
            self.view.on_transaction_complete(
                result.success,
                {
                    'installed': result.count if action in ('install', 'upgrade') else 0,
                    'removed': result.count if action == 'erase' else 0,
                    'message': result.message,
                    'errors': [result.error] if result.error else [],
                }
            )

        client = HelperClient(
            on_status=on_status,
            on_download_progress=on_download_progress,
            on_install_progress=on_install_progress,
            on_error=on_error,
            on_done=on_done,
        )
        self._current_helper = client

        if action == 'install':
            client.install(packages, choices=choices)
        elif action == 'erase':
            client.erase(packages)
        elif action == 'upgrade':
            if packages == ['__all__']:
                client.upgrade_all(choices=choices)
            else:
                client.upgrade(packages, choices=choices)

    def cancel_transaction(self) -> None:
        """Cancel the current transaction if any."""
        if self._current_helper:
            self._current_helper.cancel()
            self._current_helper = None
            self.view.finish_transaction()
            self.view.show_loading(False)

    def _build_resolution_summary(self, resolution, action: str, requested_set: Set[str]) -> dict:
        """Build detailed summary of resolution for confirmation dialog.

        Returns dict with:
            - requested: packages explicitly requested
            - install_deps: dependencies to install (not requested)
            - upgrade: packages to upgrade
            - remove: packages to remove
            - remove_deps: reverse dependencies being removed (not requested)
            - orphans_created: packages that will become orphans
        """
        summary = {
            'requested': [],
            'install_deps': [],
            'upgrade': [],
            'remove': [],
            'remove_deps': [],
            'orphans_created': [],
        }

        for a in resolution.actions:
            name = a.name
            name_lower = name.lower()
            is_requested = name_lower in requested_set

            if a.action.name == 'INSTALL':
                if is_requested or action == 'install' and not requested_set:
                    summary['requested'].append(name)
                else:
                    summary['install_deps'].append(name)
            elif a.action.name == 'UPGRADE':
                summary['upgrade'].append(name)
            elif a.action.name == 'REMOVE':
                if is_requested:
                    summary['remove'].append(name)
                else:
                    summary['remove_deps'].append(name)

        # For erase action, check which installed packages will become orphans
        if action == 'erase':
            # Get current deps before removal
            current_deps = self._dependency_packages.copy()
            # Packages being removed
            removed_set = {n.lower() for n in summary['remove'] + summary['remove_deps']}
            # Check if any remaining deps will become orphans
            # This is a simplified check - real orphan detection would need to re-run resolver
            for dep in current_deps:
                if dep not in removed_set:
                    # This dep will remain - but might become orphan
                    # For now we skip detailed orphan prediction
                    pass

        return summary

    # =========================================================================
    # Cleanup
    # =========================================================================

    def shutdown(self) -> None:
        """Clean up resources."""
        if self._debounce_timer:
            self._debounce_timer.cancel()
        self._search_executor.shutdown(wait=False)
