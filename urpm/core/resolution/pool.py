"""Pool creation and loading operations."""

import tempfile
from pathlib import Path
from typing import Dict, List

import solv

try:
    import rpm
    HAS_RPM = True
except ImportError:
    HAS_RPM = False


class PoolMixin:
    """Mixin providing pool creation and loading operations.

    Requires:
        - self.db: PackageDatabase instance
        - self.arch: str architecture
        - self.root: Optional[str] chroot path
        - self.urpm_root: Optional[str] urpm root path
        - self.ignore_installed: bool
        - self.allowed_arches: set of allowed architectures
        - self._solvable_to_pkg: dict mapping solvable IDs to package info
        - self._installed_count: int
    """

    def _create_pool(self) -> solv.Pool:
        """Create and populate libsolv Pool from database.

        Uses native libsolv methods for optimal performance:
        - add_rpmdb() for installed packages
        - add_mdk() for loading synthesis files directly
        """
        from ..config import get_media_local_path, get_base_dir, get_system_version
        from ..compression import decompress_stream
        from ..resolver import get_solver_debug, VersionConflictError

        debug = get_solver_debug()

        pool = solv.Pool()
        pool.setdisttype(solv.Pool.DISTTYPE_RPM)
        pool.setarch(self.arch)
        debug.log(f"Creating pool for arch={self.arch}, root={self.root}, urpm_root={self.urpm_root}")

        # Set root directory for chroot installations
        if self.root:
            pool.set_rootdir(self.root)

        # Load installed packages from rpmdb (skip if ignore_installed for download-only)
        if self.ignore_installed:
            debug.log("ignore_installed=True: skipping rpmdb loading")
            self._installed_count = 0
        else:
            installed = pool.add_repo("@System")
            installed.appdata = {"type": "installed"}
            pool.installed = installed

            if HAS_RPM:
                # Note: libsolv's add_rpmdb() doesn't respect set_rootdir()
                # For chroot installs, we need to use rpm module directly
                if self.root:
                    # Use rpm module to read from chroot's rpmdb
                    ts = rpm.TransactionSet(self.root or '/')
                    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES | rpm._RPMVSF_NODIGESTS)

                    # Map RPM flags to libsolv relation flags
                    def rpm_flags_to_solv(flags):
                        rel = 0
                        if flags & rpm.RPMSENSE_LESS:
                            rel |= solv.REL_LT
                        if flags & rpm.RPMSENSE_GREATER:
                            rel |= solv.REL_GT
                        if flags & rpm.RPMSENSE_EQUAL:
                            rel |= solv.REL_EQ
                        return rel

                    for hdr in ts.dbMatch():
                        s = installed.add_solvable()
                        s.name = hdr[rpm.RPMTAG_NAME]
                        epoch = hdr[rpm.RPMTAG_EPOCH] or 0
                        version = hdr[rpm.RPMTAG_VERSION]
                        release = hdr[rpm.RPMTAG_RELEASE]
                        s.evr = f"{epoch}:{version}-{release}" if epoch else f"{version}-{release}"
                        s.arch = hdr[rpm.RPMTAG_ARCH] or "noarch"

                        # Add provides
                        prov_names = hdr[rpm.RPMTAG_PROVIDENAME] or []
                        prov_flags = hdr[rpm.RPMTAG_PROVIDEFLAGS] or []
                        prov_vers = hdr[rpm.RPMTAG_PROVIDEVERSION] or []
                        for i, pname in enumerate(prov_names):
                            pflags = prov_flags[i] if i < len(prov_flags) else 0
                            pver = prov_vers[i] if i < len(prov_vers) else ''
                            if pver and pflags:
                                dep_id = pool.rel2id(
                                    pool.str2id(pname),
                                    pool.str2id(pver),
                                    rpm_flags_to_solv(pflags)
                                )
                            else:
                                dep_id = pool.str2id(pname)
                            s.add_deparray(solv.SOLVABLE_PROVIDES, dep_id)

                    self._installed_count = installed.nsolvables
                    debug.log(f"Loaded {self._installed_count} installed packages from chroot rpmdb")
                else:
                    # Normal case: use libsolv's native method
                    installed.add_rpmdb()
                    self._installed_count = installed.nsolvables
            else:
                self._installed_count = 0

        # Load available packages from synthesis files (much faster than SQLite)
        # Use urpm_root for config paths if specified
        base_dir = get_base_dir(urpm_root=self.urpm_root)
        debug.log(f"Base dir for synthesis: {base_dir}")

        # Get system version for media filtering (partition by distro version)
        system_version = get_system_version(self.root)
        debug.log(f"System version: {system_version}")

        # Determine which versions to accept (handles cauldron vs numeric)
        from ..config import get_accepted_versions
        accepted_versions, needs_choice, conflict_info = get_accepted_versions(self.db, system_version)

        if needs_choice:
            # User needs to choose between system version and cauldron
            raise VersionConflictError(
                f"Ambiguous media configuration: both {system_version} and cauldron media are enabled. "
                f"Use 'urpm config set version-mode <system|cauldron>' to choose.",
                conflict_info
            )

        debug.log(f"Accepted versions: {accepted_versions}")

        media_list = self.db.list_media()
        debug.log(f"Found {len(media_list)} media in database")

        for media in media_list:
            if not media['enabled']:
                debug.log(f"Skipping disabled media: {media['name']}")
                continue

            # Filter by Mageia version using smart version detection
            media_version = media.get('mageia_version')
            if accepted_versions and media_version and media_version not in accepted_versions:
                debug.log(f"Skipping media {media['name']}: version {media_version} not in {accepted_versions}")
                continue

            # Filter by architecture - only load media matching allowed architectures
            media_arch = media.get('architecture')
            if media_arch and media_arch not in self.allowed_arches:
                debug.log(f"Skipping media {media['name']}: architecture {media_arch} not in {self.allowed_arches}")
                continue

            repo = pool.add_repo(media['name'])
            repo.appdata = {"type": "available", "media": media}

            # Try to load from synthesis file first
            media_path = get_media_local_path(media, base_dir)
            synthesis_path = media_path / "media_info" / "synthesis.hdlist.cz"
            debug.log(f"Media {media['name']}: looking for {synthesis_path} (exists: {synthesis_path.exists()})")

            if synthesis_path.exists():
                try:
                    # Decompress and load with add_mdk
                    stream = decompress_stream(synthesis_path)
                    data = stream.read()

                    with tempfile.NamedTemporaryFile(suffix='.hdlist', delete=False) as tmp:
                        tmp.write(data)
                        tmp_path = tmp.name

                    f = solv.xfopen(tmp_path)
                    repo.add_mdk(f)
                    f.close()
                    Path(tmp_path).unlink()

                    # Populate _solvable_to_pkg mapping for the loaded packages
                    for s in repo.solvables:
                        self._solvable_to_pkg[s.id] = {
                            'name': s.name,
                            'evr': s.evr,
                            'arch': s.arch,
                            'nevra': f"{s.name}-{s.evr}.{s.arch}",
                            'summary': s.lookup_str(solv.SOLVABLE_SUMMARY) or "",
                            'size': s.lookup_num(solv.SOLVABLE_INSTALLSIZE) or 0,
                            'filesize': s.lookup_num(solv.SOLVABLE_DOWNLOADSIZE) or 0,
                            'media_name': repo.name,
                        }
                    debug.log(f"Loaded {repo.nsolvables} packages from synthesis")
                except Exception as e:
                    debug.log(f"Failed to load synthesis for {media['name']}: {e}")
                    # Fallback to SQLite loading
                    self._load_repo_packages(pool, repo, media['id'])
            else:
                # No synthesis file, fallback to SQLite
                debug.log(f"No synthesis, falling back to SQLite for {media['name']}")
                self._load_repo_packages(pool, repo, media['id'])

        pool.createwhatprovides()
        debug.log_pool_stats(pool)
        return pool

    def _load_repo_packages(self, pool: solv.Pool, repo: solv.Repo, media_id: int):
        """Load packages from database into libsolv repo.

        Uses bulk loading for performance.
        """
        from ..resolver import parse_capability

        # Load all packages first
        cursor = self.db.conn.execute("""
            SELECT id, name, epoch, version, release, arch, nevra, summary, size, filesize
            FROM packages WHERE media_id = ?
        """, (media_id,))

        pkg_id_to_solvable = {}

        for row in cursor:
            pkg_id, name, epoch, version, release, arch, nevra, summary, size, filesize = row

            # Skip src packages
            if arch in ('src', 'nosrc'):
                continue

            s = repo.add_solvable()
            s.name = name
            if epoch and epoch > 0:
                s.evr = f"{epoch}:{version}-{release}"
            else:
                s.evr = f"{version}-{release}"
            s.arch = arch

            # Versioned self-provide (essential for version comparison in conflicts)
            s.add_deparray(solv.SOLVABLE_PROVIDES,
                pool.Dep(name).Rel(solv.REL_EQ, pool.Dep(s.evr)))

            pkg_id_to_solvable[pkg_id] = s

            self._solvable_to_pkg[s.id] = {
                'id': pkg_id,
                'name': name,
                'evr': s.evr,
                'arch': arch,
                'nevra': nevra,
                'summary': summary or "",
                'size': size or 0,
                'filesize': filesize or 0,
                'media_name': repo.name,
            }

        # Bulk load provides
        cursor = self.db.conn.execute("""
            SELECT p.pkg_id, p.capability
            FROM provides p
            JOIN packages pkg ON p.pkg_id = pkg.id
            WHERE pkg.media_id = ?
        """, (media_id,))
        for pkg_id, cap in cursor:
            s = pkg_id_to_solvable.get(pkg_id)
            if s:
                s.add_deparray(solv.SOLVABLE_PROVIDES, parse_capability(pool, cap))

        # Bulk load requires
        cursor = self.db.conn.execute("""
            SELECT r.pkg_id, r.capability
            FROM requires r
            JOIN packages pkg ON r.pkg_id = pkg.id
            WHERE pkg.media_id = ?
        """, (media_id,))
        for pkg_id, cap in cursor:
            # Skip rpmlib() deps - handled by rpm itself
            # Skip file deps - not in synthesis, assume installed system provides them
            if cap.startswith("rpmlib(") or cap.startswith("/"):
                continue
            s = pkg_id_to_solvable.get(pkg_id)
            if s:
                s.add_deparray(solv.SOLVABLE_REQUIRES, parse_capability(pool, cap))

        # Bulk load conflicts
        cursor = self.db.conn.execute("""
            SELECT c.pkg_id, c.capability
            FROM conflicts c
            JOIN packages pkg ON c.pkg_id = pkg.id
            WHERE pkg.media_id = ?
        """, (media_id,))
        for pkg_id, cap in cursor:
            s = pkg_id_to_solvable.get(pkg_id)
            if s:
                s.add_deparray(solv.SOLVABLE_CONFLICTS, parse_capability(pool, cap))

        # Bulk load obsoletes
        cursor = self.db.conn.execute("""
            SELECT o.pkg_id, o.capability
            FROM obsoletes o
            JOIN packages pkg ON o.pkg_id = pkg.id
            WHERE pkg.media_id = ?
        """, (media_id,))
        for pkg_id, cap in cursor:
            s = pkg_id_to_solvable.get(pkg_id)
            if s:
                s.add_deparray(solv.SOLVABLE_OBSOLETES, parse_capability(pool, cap))

        # Bulk load weak dependencies (RPM 4.12+)
        for table, solv_type in [
            ('recommends', solv.SOLVABLE_RECOMMENDS),
            ('suggests', solv.SOLVABLE_SUGGESTS),
            ('supplements', solv.SOLVABLE_SUPPLEMENTS),
            ('enhances', solv.SOLVABLE_ENHANCES),
        ]:
            cursor = self.db.conn.execute(f"""
                SELECT d.pkg_id, d.capability
                FROM {table} d
                JOIN packages pkg ON d.pkg_id = pkg.id
                WHERE pkg.media_id = ?
            """, (media_id,))
            for pkg_id, cap in cursor:
                s = pkg_id_to_solvable.get(pkg_id)
                if s:
                    s.add_deparray(solv_type, parse_capability(pool, cap))

    def _load_rpmdb(self, pool: solv.Pool, repo: solv.Repo) -> int:
        """Load installed packages from rpmdb into libsolv repo.

        Args:
            pool: libsolv Pool
            repo: libsolv Repo to populate (@System)

        Returns:
            Number of packages loaded
        """
        if not HAS_RPM:
            return 0

        count = 0
        ts = rpm.TransactionSet(self.root or '/')

        # Iterate over all installed packages
        for hdr in ts.dbMatch():
            name = hdr[rpm.RPMTAG_NAME]
            epoch = hdr[rpm.RPMTAG_EPOCH] or 0
            version = hdr[rpm.RPMTAG_VERSION]
            release = hdr[rpm.RPMTAG_RELEASE]
            arch = hdr[rpm.RPMTAG_ARCH] or "noarch"
            size = hdr[rpm.RPMTAG_SIZE] or 0

            # Skip gpg-pubkey pseudo-packages
            if name == "gpg-pubkey":
                continue

            s = repo.add_solvable()
            s.name = name
            if epoch and epoch > 0:
                s.evr = f"{epoch}:{version}-{release}"
            else:
                s.evr = f"{version}-{release}"
            s.arch = arch

            # Versioned self-provide
            s.add_deparray(solv.SOLVABLE_PROVIDES,
                pool.Dep(name).Rel(solv.REL_EQ, pool.Dep(s.evr)))

            # Add provides
            provides = hdr[rpm.RPMTAG_PROVIDENAME] or []
            provide_vers = hdr[rpm.RPMTAG_PROVIDEVERSION] or []
            provide_flags = hdr[rpm.RPMTAG_PROVIDEFLAGS] or []

            for i, prov in enumerate(provides):
                if i < len(provide_vers) and provide_vers[i]:
                    flags = provide_flags[i] if i < len(provide_flags) else 0
                    solv_flags = self._rpm_flags_to_solv(flags)
                    if solv_flags:
                        dep = pool.Dep(prov).Rel(solv_flags, pool.Dep(provide_vers[i]))
                    else:
                        dep = pool.Dep(prov)
                else:
                    dep = pool.Dep(prov)
                s.add_deparray(solv.SOLVABLE_PROVIDES, dep)

            # Add requires
            requires = hdr[rpm.RPMTAG_REQUIRENAME] or []
            require_vers = hdr[rpm.RPMTAG_REQUIREVERSION] or []
            require_flags = hdr[rpm.RPMTAG_REQUIREFLAGS] or []

            for i, req in enumerate(requires):
                # Skip rpmlib() and file deps
                if req.startswith("rpmlib(") or req.startswith("/"):
                    continue

                if i < len(require_vers) and require_vers[i]:
                    flags = require_flags[i] if i < len(require_flags) else 0
                    solv_flags = self._rpm_flags_to_solv(flags)
                    if solv_flags:
                        dep = pool.Dep(req).Rel(solv_flags, pool.Dep(require_vers[i]))
                    else:
                        dep = pool.Dep(req)
                else:
                    dep = pool.Dep(req)
                s.add_deparray(solv.SOLVABLE_REQUIRES, dep)

            # Add conflicts
            conflicts = hdr[rpm.RPMTAG_CONFLICTNAME] or []
            conflict_vers = hdr[rpm.RPMTAG_CONFLICTVERSION] or []
            conflict_flags = hdr[rpm.RPMTAG_CONFLICTFLAGS] or []

            for i, conf in enumerate(conflicts):
                if i < len(conflict_vers) and conflict_vers[i]:
                    flags = conflict_flags[i] if i < len(conflict_flags) else 0
                    solv_flags = self._rpm_flags_to_solv(flags)
                    if solv_flags:
                        dep = pool.Dep(conf).Rel(solv_flags, pool.Dep(conflict_vers[i]))
                    else:
                        dep = pool.Dep(conf)
                else:
                    dep = pool.Dep(conf)
                s.add_deparray(solv.SOLVABLE_CONFLICTS, dep)

            # Add obsoletes
            obsoletes = hdr[rpm.RPMTAG_OBSOLETENAME] or []
            obsolete_vers = hdr[rpm.RPMTAG_OBSOLETEVERSION] or []
            obsolete_flags = hdr[rpm.RPMTAG_OBSOLETEFLAGS] or []

            for i, obs in enumerate(obsoletes):
                if i < len(obsolete_vers) and obsolete_vers[i]:
                    flags = obsolete_flags[i] if i < len(obsolete_flags) else 0
                    solv_flags = self._rpm_flags_to_solv(flags)
                    if solv_flags:
                        dep = pool.Dep(obs).Rel(solv_flags, pool.Dep(obsolete_vers[i]))
                    else:
                        dep = pool.Dep(obs)
                else:
                    dep = pool.Dep(obs)
                s.add_deparray(solv.SOLVABLE_OBSOLETES, dep)

            # Add weak dependencies (RPM 4.12+)
            weak_deps = [
                (rpm.RPMTAG_RECOMMENDNAME, rpm.RPMTAG_RECOMMENDVERSION,
                 rpm.RPMTAG_RECOMMENDFLAGS, solv.SOLVABLE_RECOMMENDS),
                (rpm.RPMTAG_SUGGESTNAME, rpm.RPMTAG_SUGGESTVERSION,
                 rpm.RPMTAG_SUGGESTFLAGS, solv.SOLVABLE_SUGGESTS),
                (rpm.RPMTAG_SUPPLEMENTNAME, rpm.RPMTAG_SUPPLEMENTVERSION,
                 rpm.RPMTAG_SUPPLEMENTFLAGS, solv.SOLVABLE_SUPPLEMENTS),
                (rpm.RPMTAG_ENHANCENAME, rpm.RPMTAG_ENHANCEVERSION,
                 rpm.RPMTAG_ENHANCEFLAGS, solv.SOLVABLE_ENHANCES),
            ]

            for name_tag, ver_tag, flag_tag, solv_type in weak_deps:
                names = hdr[name_tag] or []
                versions = hdr[ver_tag] or []
                flags_list = hdr[flag_tag] or []

                for i, dep_name in enumerate(names):
                    if i < len(versions) and versions[i]:
                        flags = flags_list[i] if i < len(flags_list) else 0
                        solv_flags = self._rpm_flags_to_solv(flags)
                        if solv_flags:
                            dep = pool.Dep(dep_name).Rel(solv_flags, pool.Dep(versions[i]))
                        else:
                            dep = pool.Dep(dep_name)
                    else:
                        dep = pool.Dep(dep_name)
                    s.add_deparray(solv_type, dep)

            # Store mapping
            self._solvable_to_pkg[s.id] = {
                'name': name,
                'evr': s.evr,
                'arch': arch,
                'nevra': f"{name}-{s.evr}.{arch}",
                'size': size,
                'filesize': size,
                'media_name': '@System',
            }
            count += 1

        return count

    def _rpm_flags_to_solv(self, flags: int) -> int:
        """Convert RPM dependency flags to libsolv flags."""
        solv_flags = 0
        if flags & rpm.RPMSENSE_LESS:
            solv_flags |= solv.REL_LT
        if flags & rpm.RPMSENSE_GREATER:
            solv_flags |= solv.REL_GT
        if flags & rpm.RPMSENSE_EQUAL:
            solv_flags |= solv.REL_EQ
        return solv_flags

    def add_local_rpms(self, rpm_infos: List[Dict]) -> None:
        """Add local RPM files to the resolver pool.

        This creates or reuses a special @LocalRPMs repository containing the local
        packages so they can be resolved alongside repository packages.

        Args:
            rpm_infos: List of dicts from read_rpm_header() with package metadata
        """
        from ..resolver import parse_capability

        if self.pool is None:
            self.pool = self._create_pool()

        # Find existing @LocalRPMs repo or create new one
        local_repo = None
        for repo in self.pool.repos:
            if repo.name == '@LocalRPMs':
                local_repo = repo
                break
        if local_repo is None:
            local_repo = self.pool.add_repo("@LocalRPMs")
            local_repo.appdata = {"type": "local"}

        for info in rpm_infos:
            s = local_repo.add_solvable()
            s.name = info['name']
            epoch = info.get('epoch', 0) or 0
            if epoch:
                s.evr = f"{epoch}:{info['version']}-{info['release']}"
            else:
                s.evr = f"{info['version']}-{info['release']}"
            s.arch = info['arch']

            # Versioned self-provide - use Dep with relation ID
            name_id = self.pool.str2id(info['name'], True)
            evr_id = self.pool.str2id(s.evr, True)
            rel_id = self.pool.rel2id(name_id, evr_id, solv.REL_EQ, True)
            s.add_deparray(solv.SOLVABLE_PROVIDES, solv.Dep(self.pool, rel_id))

            # Add provides
            for cap in info.get('provides', []):
                if cap and not cap.startswith('rpmlib('):
                    s.add_deparray(solv.SOLVABLE_PROVIDES, parse_capability(self.pool, cap))

            # Add requires
            for cap in info.get('requires', []):
                if cap and not cap.startswith('rpmlib(') and not cap.startswith('/'):
                    s.add_deparray(solv.SOLVABLE_REQUIRES, parse_capability(self.pool, cap))

            # Add conflicts
            for cap in info.get('conflicts', []):
                if cap:
                    s.add_deparray(solv.SOLVABLE_CONFLICTS, parse_capability(self.pool, cap))

            # Add obsoletes
            for cap in info.get('obsoletes', []):
                if cap:
                    s.add_deparray(solv.SOLVABLE_OBSOLETES, parse_capability(self.pool, cap))

            # Add weak dependencies
            for cap in info.get('recommends', []):
                if cap:
                    s.add_deparray(solv.SOLVABLE_RECOMMENDS, parse_capability(self.pool, cap))
            for cap in info.get('suggests', []):
                if cap:
                    s.add_deparray(solv.SOLVABLE_SUGGESTS, parse_capability(self.pool, cap))

            # Store metadata including the local path
            self._solvable_to_pkg[s.id] = {
                'name': info['name'],
                'evr': s.evr,
                'arch': info['arch'],
                'nevra': info['nevra'],
                'size': info.get('size', 0),
                'filesize': info.get('filesize', 0),
                'media_name': '@LocalRPMs',
                'local_path': info['path'],  # Critical: path to the RPM file
            }

        # Rebuild whatprovides index
        self.pool.createwhatprovides()
