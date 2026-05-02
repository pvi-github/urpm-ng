%define name urpm-ng
%define version 0.7.11
%define release 1

Name:           %{name}
Version:        %{version}
Release:        %mkrel %{release}
Summary:        Modern package manager for Mageia Linux
License:        GPLv3+
Group:          System/Configuration/Packaging
URL:            https://github.com/pvi-github/urpm-ng
Source0:        %{name}-%{version}.tar.gz
Source1:        pk-backend-urpm-%{version}.tar.gz

# Note: No BuildArch:noarch because we also build the C backend

# Python build requirements
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-wheel
BuildRequires:  python3-setuptools
BuildRequires:  python3-solv
BuildRequires:  python3-rpm
BuildRequires:  python3-zstandard
BuildRequires:  meson
BuildRequires:  gettext-devel

# C build requirements for PackageKit backend
BuildRequires:  gcc
BuildRequires:  pkgconfig(glib-2.0) >= 2.56
BuildRequires:  pkgconfig(gio-2.0)
BuildRequires:  pkgconfig(json-glib-1.0)
BuildRequires:  pkgconfig(packagekit-glib2) >= 1.0

# ============================================================================
# Main package: urpm-ng (meta-package)
# ============================================================================
Requires:       %{name}-core = %{version}-%{release}
Requires:       %{name}-daemon = %{version}-%{release}

%description
urpm-ng is a complete rewrite of the classic urpmi toolset, providing:
- Fast dependency resolution using libsolv
- P2P package sharing between LAN machines
- Modern CLI with intuitive commands
- Background daemon for intelligent caching

This meta-package installs the core CLI and background daemon.
For desktop integration (Discover, GNOME Software), install urpm-ng-desktop.

# ============================================================================
# Subpackage: urpm-ng-core (CLI, database, resolver)
# ============================================================================
%package core
Summary:        Core CLI and resolver for urpm-ng
Group:          System/Configuration/Packaging

Requires:       python3
Requires:       python3-solv
Requires:       python3-rpm
Requires:       python3-zstandard
Requires:       python3-pyyaml
Requires:       python3-curl
Requires:       gnupg2

%description core
Core components of urpm-ng package manager:
- Command-line interface (urpm)
- Package database management
- Dependency resolution using libsolv
- Repository synchronization

This is the minimal package for systems that don't need the daemon or
desktop integration. Useful for container images and minimal installs.

# ============================================================================
# Subpackage: urpm-ng-daemon (background service + P2P)
# ============================================================================
%package daemon
Summary:        Background daemon and P2P sharing for urpm-ng
Group:          System/Configuration/Packaging

Requires:       %{name}-core = %{version}-%{release}
Requires(post):   systemd
Requires(preun):  systemd
Requires(postun): systemd

%description daemon
Background daemon for urpm-ng providing:
- Intelligent package caching
- P2P package sharing between LAN machines
- Automatic metadata updates

# ============================================================================
# Subpackage: urpm-ng-appstream (AppStream metadata)
# ============================================================================
%package appstream
Summary:        AppStream integration for urpm-ng
Group:          System/Configuration/Packaging

Requires:       %{name}-core = %{version}-%{release}

%description appstream
AppStream metadata configuration for urpm-ng.
Enables application metadata for software centers.

# ============================================================================
# Subpackage: urpm-ng-packagekit-backend (PackageKit integration)
# ============================================================================
%package packagekit-backend
Summary:        PackageKit backend for urpm-ng
Group:          System/Configuration/Packaging

Requires:       %{name}-core = %{version}-%{release}
Requires:       %{name}-daemon = %{version}-%{release}
Requires:       python3-gobject
Requires:       polkit
Requires:       PackageKit
Obsoletes:      pk-backend-urpm < 0.3
Provides:       pk-backend-urpm = %{version}-%{release}

%description packagekit-backend
PackageKit backend that uses urpm-ng for package management on Mageia Linux.
This allows GNOME Software and KDE Discover to manage packages via urpm-ng.

Includes D-Bus service and PolicyKit integration.

# ============================================================================
# Subpackage: urpm-ng-desktop (meta-package for desktop users)
# ============================================================================
%package desktop
Summary:        Desktop integration for urpm-ng
Group:          System/Configuration/Packaging

Requires:       %{name} = %{version}-%{release}
Requires:       %{name}-packagekit-backend = %{version}-%{release}
Requires:       %{name}-appstream = %{version}-%{release}

%description desktop
Meta-package for desktop users that installs urpm-ng with full
GUI integration for KDE Discover and GNOME Software.

Includes: core CLI, daemon, PackageKit backend, and AppStream support.

# ============================================================================
# Subpackage: urpm-ng-build (container image building)
# ============================================================================
%package build
Summary:        Container image building tools for urpm-ng
Group:          System/Configuration/Packaging

Requires:       %{name}-core = %{version}-%{release}

%description build
Tools for building minimal container images for RPM packaging:
- mkimage: Create Docker/Podman images for builds
- build: Build packages in containers

Requires Docker or Podman to function.

# ============================================================================
# Subpackage: urpm-ng-all (everything)
# ============================================================================
%package all
Summary:        Complete urpm-ng installation
Group:          System/Configuration/Packaging

Requires:       %{name}-desktop = %{version}-%{release}
Requires:       %{name}-build = %{version}-%{release}

%description all
Meta-package that installs all urpm-ng components:
- Core CLI and resolver
- Background daemon with P2P sharing
- Desktop integration (PackageKit, AppStream)
- Container image building tools

# ============================================================================
# Prep
# ============================================================================
%prep
%setup -q
%setup -q -T -D -a 1

# Check if setuptools < 77.0.0 (old way in mga9)
if ! python3 -c "import setuptools; from packaging.version import parse; exit(0 if parse(setuptools.__version__) >= parse('77.0.0') else 1)" 2>/dev/null; then
    echo "Adapting pyproject.toml for old setuptools (< 77)"
    sed -i '/^[[:space:]]*license-files[[:space:]]*=/d' pyproject.toml
    sed -i -E 's/^([[:space:]]*)license = "([^"]*)"/\1license = { text = "\2" }/' pyproject.toml
fi

# ============================================================================
# Build
# ============================================================================
%build
# Build Python wheel
%pyproject_wheel

# Build PackageKit backend
cd pk-backend-urpm
%meson
%meson_build
cd ..

# Compile translations
for lang in fr de es pt nl; do
    mkdir -p po/locale/$lang/LC_MESSAGES
    msgfmt -o po/locale/$lang/LC_MESSAGES/urpm.mo po/$lang.po
done

# ============================================================================
# Install
# ============================================================================
%install
# Install Python package
%pyproject_install
%pyproject_save_files urpm

# Install PackageKit backend
cd pk-backend-urpm
%meson_install
cd ..

# Install systemd services
install -Dm644 data/urpmd.service %{buildroot}%{_unitdir}/urpmd.service

# Install shorewall rules for P2P sharing
install -Dm644 data/rules.urpm-ng %{buildroot}%{_sysconfdir}/shorewall/rules.urpm-ng
install -Dm644 data/urpm-dbus.service %{buildroot}%{_unitdir}/urpm-dbus.service

# Install D-Bus service and policy
install -Dm644 data/org.mageia.Urpm.v1.service %{buildroot}%{_datadir}/dbus-1/system-services/org.mageia.Urpm.v1.service
install -Dm644 data/org.mageia.Urpm.v1.conf %{buildroot}%{_sysconfdir}/dbus-1/system.d/org.mageia.Urpm.v1.conf

# Install configuration files
install -dm755 %{buildroot}%{_sysconfdir}/urpm/conf.d
install -Dm644 data/etc/urpm/urpm.cfg %{buildroot}%{_sysconfdir}/urpm/urpm.cfg
if [ -d data/etc/urpm/conf.d ]; then
    install -m644 data/etc/urpm/conf.d/*.cfg %{buildroot}%{_sysconfdir}/urpm/conf.d/ 2>/dev/null || :
fi

# Install PolicyKit policy
install -Dm644 data/org.mageia.urpm.policy %{buildroot}%{_datadir}/polkit-1/actions/org.mageia.urpm.policy

# Install AppStream configuration
install -Dm644 data/appstream-mageia.conf %{buildroot}%{_datadir}/appstream/appstream.conf.d/mageia.conf

# Install OS metainfo for Discover/GNOME Software
install -Dm644 data/mageia.metainfo.xml %{buildroot}%{_datadir}/metainfo/mageia.metainfo.xml

# Install D-Bus service executable
install -Dm755 /dev/null %{buildroot}%{_libexecdir}/urpm-dbus-service
cat > %{buildroot}%{_libexecdir}/urpm-dbus-service << 'EOFSCRIPT'
#!/usr/bin/python3
from urpm.dbus.service import main
main()
EOFSCRIPT

# Install documentation
install -dm755 %{buildroot}%{_docdir}/%{name}
install -m644 README.md %{buildroot}%{_docdir}/%{name}/
install -m644 QUICKSTART.md %{buildroot}%{_docdir}/%{name}/
install -m644 CHANGELOG.md %{buildroot}%{_docdir}/%{name}/
install -m644 doc/*.md %{buildroot}%{_docdir}/%{name}/

# Install bash completion
install -Dm644 completion/urpm.bash %{buildroot}%{_sysconfdir}/bash_completion.d/urpm

# Install locale files
for lang in fr de es pt nl; do
    install -Dm644 po/locale/$lang/LC_MESSAGES/urpm.mo \
        %{buildroot}%{_datadir}/locale/$lang/LC_MESSAGES/urpm.mo
done

# Install man pages (all languages)
for lang in en de es fr nl pt; do
    install -Dm644 man/$lang/man1/urpm.1 %{buildroot}%{_mandir}/$lang/man1/urpm.1
    install -Dm644 man/$lang/man8/urpmd.8 %{buildroot}%{_mandir}/$lang/man8/urpmd.8
done
# English goes to the default (unlocalized) mandir too
install -Dm644 man/en/man1/urpm.1 %{buildroot}%{_mandir}/man1/urpm.1
install -Dm644 man/en/man8/urpmd.8 %{buildroot}%{_mandir}/man8/urpmd.8

# Install mkimage profiles
install -dm755 %{buildroot}%{_datadir}/urpm/profiles
install -m644 data/profiles/*.yaml %{buildroot}%{_datadir}/urpm/profiles/

# ============================================================================
# Scripts for urpm-ng-daemon
# ============================================================================
%post daemon
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :

# --- Shorewall firewall configuration ---
# We ship /etc/shorewall/rules.urpm-ng as an include file.
# The %post adds "INCLUDE rules.urpm-ng" to the main rules file,
# and migrates old inline rules from previous versions.
if [ -f /etc/shorewall/rules ]; then
    _changed=0

    # Migrate: remove old inline rules from versions < 0.7.5
    if /usr/bin/grep -q '# urpm-ng P2P sharing' /etc/shorewall/rules 2>/dev/null; then
        /usr/bin/sed -i '/# urpm-ng P2P sharing/d; /urpmd HTTP server/d; /urpmd P2P discovery/d' \
            /etc/shorewall/rules 2>/dev/null || :
        # Clean up blank lines left behind (at most one)
        /usr/bin/sed -i '/^$/N;/^\n$/d' /etc/shorewall/rules 2>/dev/null || :
        _changed=1
    fi

    # Add INCLUDE right after INCLUDE rules.drakx, or before the first
    # non-comment line if rules.drakx is absent.
    if ! /usr/bin/grep -q 'rules\.urpm-ng' /etc/shorewall/rules 2>/dev/null; then
        if /usr/bin/grep -q 'INCLUDE rules\.drakx' /etc/shorewall/rules 2>/dev/null; then
            /usr/bin/sed -i '/INCLUDE rules\.drakx/a INCLUDE rules.urpm-ng' \
                /etc/shorewall/rules 2>/dev/null || :
        else
            /usr/bin/sed -i '0,/^[^#]/{/^[^#]/i INCLUDE rules.urpm-ng
            }' /etc/shorewall/rules 2>/dev/null || :
        fi
        _changed=1
    fi

    if [ "$_changed" -eq 1 ]; then
        echo "Firewall: urpmd P2P rules installed (rules.urpm-ng)."
        echo "  Run 'shorewall reload' to apply."
    fi
fi

if [ $1 -eq 1 ]; then
    # First install
    /usr/bin/systemctl enable urpmd.service >/dev/null 2>&1 || :
    /usr/bin/systemctl start urpmd.service >/dev/null 2>&1 || :
fi

if [ $1 -ge 2 ]; then
    # Upgrade: restart only if was running
    /usr/bin/systemctl try-restart urpmd.service >/dev/null 2>&1 || :
fi

%preun daemon
if [ $1 -eq 0 ]; then
    # Uninstall
    /usr/bin/systemctl stop urpmd.service >/dev/null 2>&1 || :
    /usr/bin/systemctl disable urpmd.service >/dev/null 2>&1 || :
fi

%postun daemon
if [ $1 -eq 0 ]; then
    # Uninstall
    /usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :

    # Remove INCLUDE line from shorewall rules
    # (the rules.urpm-ng file itself is removed by RPM)
    if [ -f /etc/shorewall/rules ]; then
        /usr/bin/sed -i '/INCLUDE rules\.urpm-ng/d' \
            /etc/shorewall/rules 2>/dev/null || :
        echo "Firewall: urpmd P2P rules removed. Run 'shorewall reload' to apply."
    fi
fi

# ============================================================================
# Scripts for urpm-ng-core (first install message)
# ============================================================================
%post core
# Enforce auto-upgrade policy: disable unattended upgrades from
# gnome-software, KDE Discover, PackageKit offline, and dnf-automatic.
# This runs on every install/upgrade to catch newly installed components.
/usr/bin/python3 -c "from urpm.core.auto_upgrade_policy import enforce_all; enforce_all()" 2>/dev/null || :

if [ $1 -eq 1 ]; then
    # First install: import media from urpmi and auto-configure servers
    echo ""
    echo "=== urpm-ng: importing media configuration ==="
    /usr/bin/urpm media import -y 2>/dev/null || :
    echo ""
    echo "=== urpm-ng: auto-configuring mirror servers (please wait) ==="
    /usr/bin/urpm server autoconfig 2>/dev/null || :

    # Show get-started message
    echo ""
    echo "=== urpm-ng installed ==="
    echo ""
    echo "Quick start:"
    echo "  sudo urpm install <package>"
    echo ""
    echo "Documentation: /usr/share/doc/urpm-ng/QUICKSTART.md"
    echo ""
fi

# ============================================================================
# Scripts for urpm-ng-packagekit-backend
# ============================================================================
%post packagekit-backend
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :

CONFIG_FILE=/etc/PackageKit/PackageKit.conf

# Only for new install, not upgrade
if [ "$1" -eq 1 ]; then
    if [ -f "$CONFIG_FILE" ]; then
        # Check if already configured correctly
        if grep -q "^DefaultBackend=urpm$" "$CONFIG_FILE"; then
            : # Already configured
        elif grep -q "^DefaultBackend=" "$CONFIG_FILE"; then
            # Replace existing uncommented line
            sed -i 's/^DefaultBackend=.*/DefaultBackend=urpm/' "$CONFIG_FILE"
        elif grep -q "^#DefaultBackend=" "$CONFIG_FILE"; then
            # Add after commented line
            sed -i '/^#DefaultBackend=/a DefaultBackend=urpm' "$CONFIG_FILE"
        fi
    fi

    # Enable D-Bus service
    /usr/bin/systemctl enable urpm-dbus.service >/dev/null 2>&1 || :
fi

# Restart PackageKit to pick up the new backend
/usr/bin/systemctl try-restart packagekit.service >/dev/null 2>&1 || :

%preun packagekit-backend
if [ $1 -eq 0 ]; then
    # Uninstall
    /usr/bin/systemctl stop urpm-dbus.service >/dev/null 2>&1 || :
fi

%postun packagekit-backend
if [ $1 -eq 0 ]; then
    # Uninstall: restart PackageKit
    /usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :
    /usr/bin/systemctl try-restart packagekit.service >/dev/null 2>&1 || :
fi

# ============================================================================
# Files for urpm-ng (meta-package - empty, just dependencies)
# ============================================================================
%files
# Meta-package, no files

# ============================================================================
# Files for urpm-ng-core
# ============================================================================
%files core -f %{pyproject_files}
%license LICENSE
%doc %{_docdir}/%{name}
%{_bindir}/urpm
%{_sysconfdir}/bash_completion.d/urpm
%dir %{_sysconfdir}/urpm
%dir %{_sysconfdir}/urpm/conf.d
%config(noreplace) %{_sysconfdir}/urpm/urpm.cfg
%{_sysconfdir}/urpm/conf.d/*.cfg
%{_mandir}/man1/urpm.1*
%{_mandir}/*/man1/urpm.1*
%dir %{_datadir}/urpm
%dir %{_datadir}/urpm/profiles
%{_datadir}/urpm/profiles/*.yaml
# Locale files
%{_datadir}/locale/*/LC_MESSAGES/urpm.mo

# ============================================================================
# Files for urpm-ng-daemon
# ============================================================================
%files daemon
%{_bindir}/urpmd
%{_unitdir}/urpmd.service
%config(noreplace) %{_sysconfdir}/shorewall/rules.urpm-ng
%{_mandir}/man8/urpmd.8*
%{_mandir}/*/man8/urpmd.8*

# ============================================================================
# Files for urpm-ng-appstream
# ============================================================================
%files appstream
%{_datadir}/appstream/appstream.conf.d/mageia.conf
%{_datadir}/metainfo/mageia.metainfo.xml

# ============================================================================
# Files for urpm-ng-packagekit-backend
# ============================================================================
%files packagekit-backend
%{_bindir}/urpm-dbus-service
%{_libexecdir}/urpm-dbus-service
%{_unitdir}/urpm-dbus.service
%{_datadir}/dbus-1/system-services/org.mageia.Urpm.v1.service
%config(noreplace) %{_sysconfdir}/dbus-1/system.d/org.mageia.Urpm.v1.conf
%{_datadir}/polkit-1/actions/org.mageia.urpm.policy
%{_libdir}/packagekit-backend/libpk_backend_urpm.so

# ============================================================================
# Files for urpm-ng-desktop (meta-package - empty)
# ============================================================================
%files desktop
# Meta-package, no files

# ============================================================================
# Files for urpm-ng-build (meta-package for now, tools are in core)
# ============================================================================
%files build
# Build commands (mkimage, build) are part of the CLI in urpm-ng-core
# This package just pulls in urpm-ng-core for users who only need build tools

# ============================================================================
# Files for urpm-ng-all (meta-package - empty)
# ============================================================================
%files all
# Meta-package, no files

%changelog
