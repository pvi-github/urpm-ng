%define name urpm-ng
%define version 0.1.27
%define release 1

Name:           %{name}
Version:        %{version}
Release:        %mkrel %{release}
Summary:        Modern package manager for Mageia Linux
License:        GPLv3+
Group:          System/Configuration/Packaging
URL:            https://github.com/pvi-github/urpm-ng
Source0:        %{name}-%{version}.tar.gz
Source1:        pk-backend-urpm.tar.gz

# Note: No BuildArch:noarch because we also build the C backend

# Python build requirements
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-wheel
BuildRequires:  python3-setuptools
BuildRequires:  python3-solv
BuildRequires:  python3-rpm
BuildRequires:  python3-zstandard

Requires:       python3
Requires:       python3-solv
Requires:       python3-rpm
Requires:       python3-zstandard
Requires:       python3-gobject
Requires:       gnupg2
Requires:       polkit

Requires(post):   systemd
Requires(preun):  systemd
Requires(postun): systemd

%description
urpm-ng is a complete rewrite of the classic urpmi toolset, providing:
- Fast dependency resolution using libsolv
- P2P package sharing between LAN machines
- Modern CLI with intuitive commands
- Background daemon for intelligent caching
- Seed-based replication for DVD-like mirrors

# ============================================================================
# Subpackage: pk-backend-urpm (PackageKit backend)
# ============================================================================
%package -n pk-backend-urpm
Summary:        PackageKit backend for urpm-ng
Group:          System/Configuration/Packaging

# C build requirements (backend headers are bundled from PackageKit source)
BuildRequires:  meson
BuildRequires:  gcc
BuildRequires:  pkgconfig(glib-2.0) >= 2.56
BuildRequires:  pkgconfig(gio-2.0)
BuildRequires:  pkgconfig(json-glib-1.0)
BuildRequires:  pkgconfig(packagekit-glib2) >= 1.0

Requires:       %{name} = %{version}-%{release}
Requires:       PackageKit

%description -n pk-backend-urpm
PackageKit backend that uses urpm-ng for package management on Mageia Linux.
This allows GNOME Software and KDE Discover to manage packages via urpm-ng.

Install this package to use Discover or GNOME Software with urpm-ng.

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
install -Dm644 data/urpm-dbus.service %{buildroot}%{_unitdir}/urpm-dbus.service

# Install D-Bus service and policy
install -Dm644 data/org.mageia.Urpm.v1.service %{buildroot}%{_datadir}/dbus-1/system-services/org.mageia.Urpm.v1.service
install -Dm644 data/org.mageia.Urpm.v1.conf %{buildroot}%{_sysconfdir}/dbus-1/system.d/org.mageia.Urpm.v1.conf

# Install PolicyKit policy
install -Dm644 data/org.mageia.urpm.policy %{buildroot}%{_datadir}/polkit-1/actions/org.mageia.urpm.policy

# Install AppStream configuration
install -Dm644 data/appstream-mageia.conf %{buildroot}%{_datadir}/appstream/appstream.conf.d/mageia.conf

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

# Install man pages (English)
install -Dm644 man/en/man1/urpm.1 %{buildroot}%{_mandir}/man1/urpm.1
install -Dm644 man/en/man8/urpmd.8 %{buildroot}%{_mandir}/man8/urpmd.8

# Install man pages (French)
install -Dm644 man/fr/man1/urpm.1 %{buildroot}%{_mandir}/fr/man1/urpm.1
install -Dm644 man/fr/man8/urpmd.8 %{buildroot}%{_mandir}/fr/man8/urpmd.8

# ============================================================================
# Scripts for main package
# ============================================================================
%post
/usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :

if [ $1 -eq 1 ]; then
    # First install

    # Configure shorewall firewall for P2P sharing
    if [ -f /etc/shorewall/rules ]; then
        if ! /usr/bin/grep -q 'urpmd' /etc/shorewall/rules 2>/dev/null; then
            /usr/bin/cat >> /etc/shorewall/rules << 'EOF'

# urpm-ng P2P sharing (added by urpm-ng package)
ACCEPT  all     $FW     tcp     9876    # urpmd HTTP server
ACCEPT  all     $FW     udp     9878    # urpmd P2P discovery
EOF
            /usr/bin/systemctl reload shorewall >/dev/null 2>&1 || :
            echo "Firewall: ports 9876/tcp and 9878/udp opened for urpmd P2P sharing"
        fi
    fi

    # Import media from urpmi and auto-configure servers
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

    /usr/bin/systemctl enable urpmd.service >/dev/null 2>&1 || :
    /usr/bin/systemctl start urpmd.service >/dev/null 2>&1 || :
fi

if [ $1 -ge 2 ]; then
    # Upgrade: restart only if was running
    /usr/bin/systemctl try-restart urpmd.service >/dev/null 2>&1 || :
    /usr/bin/systemctl try-restart urpm-dbus.service >/dev/null 2>&1 || :
fi

%preun
if [ $1 -eq 0 ]; then
    # Uninstall
    /usr/bin/systemctl stop urpmd.service >/dev/null 2>&1 || :
    /usr/bin/systemctl disable urpmd.service >/dev/null 2>&1 || :
    /usr/bin/systemctl stop urpm-dbus.service >/dev/null 2>&1 || :
fi

%postun
if [ $1 -eq 0 ]; then
    # Uninstall
    /usr/bin/systemctl daemon-reload >/dev/null 2>&1 || :

    # Remove firewall rules added by urpm-ng
    if [ -f /etc/shorewall/rules ]; then
        /usr/bin/sed -i '/# urpm-ng P2P sharing/d; /urpmd HTTP server/d; /urpmd P2P discovery/d' /etc/shorewall/rules 2>/dev/null || :
        /usr/bin/systemctl reload shorewall >/dev/null 2>&1 || :
    fi
fi

# ============================================================================
# Scripts for pk-backend-urpm
# ============================================================================
%post -n pk-backend-urpm

CONFIG_FILE=/etc/PackageKit/PackageKit.conf

# Only for new install, not upgrade
if [ "$1" -eq 1 ]; then
    if [ -f "$CONFIG_FILE" ]; then
        if grep -q "^#\?DefaultBackend=" "$CONFIG_FILE"; then
            sed -i 's/^#\?DefaultBackend=.*/DefaultBackend=urpm/' "$CONFIG_FILE"
        else
            sed -i '/^#DefaultBackend=auto/a DefaultBackend=urpm' "$CONFIG_FILE"
        fi
    fi
fi

# Restart PackageKit to pick up the new backend
/usr/bin/systemctl try-restart packagekit.service >/dev/null 2>&1 || :

%postun -n pk-backend-urpm
if [ $1 -eq 0 ]; then
    # Uninstall: restart PackageKit
    /usr/bin/systemctl try-restart packagekit.service >/dev/null 2>&1 || :
fi

# ============================================================================
# Files
# ============================================================================
%files -f %{pyproject_files}
%license LICENSE
%doc %{_docdir}/%{name}
%{_bindir}/urpm
%{_bindir}/urpmd
%{_bindir}/urpm-dbus-service
%{_libexecdir}/urpm-dbus-service
%{_unitdir}/urpmd.service
%{_unitdir}/urpm-dbus.service
%{_datadir}/dbus-1/system-services/org.mageia.Urpm.v1.service
%config(noreplace) %{_sysconfdir}/dbus-1/system.d/org.mageia.Urpm.v1.conf
%{_datadir}/polkit-1/actions/org.mageia.urpm.policy
%{_sysconfdir}/bash_completion.d/urpm
%{_mandir}/man1/urpm.1*
%{_mandir}/man8/urpmd.8*
%{_mandir}/fr/man1/urpm.1*
%{_mandir}/fr/man8/urpmd.8*

%files -n pk-backend-urpm
%{_libdir}/packagekit-backend/libpk_backend_urpm.so
%{_datadir}/appstream/appstream.conf.d/mageia.conf

%changelog
