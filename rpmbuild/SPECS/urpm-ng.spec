%define name urpm-ng
%define version 0.1.23
%define release 1

Name:           %{name}
Version:        %{version}
Release:        %mkrel %{release}
Summary:        Modern package manager for Mageia Linux
License:        GPLv3+
Group:          System/Configuration/Packaging
URL:            https://github.com/pvi-github/urpm-ng
Source0:        %{name}-%{version}.tar.gz
Source1:        urpmd.service

BuildArch:      noarch
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
Requires:       gnupg2

Suggests:       fakeroot

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

%prep
%setup -q

%build
%pyproject_wheel

%install
%pyproject_install
%pyproject_save_files urpm

# Install systemd service
install -Dm644 %{SOURCE1} %{buildroot}%{_unitdir}/urpmd.service

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
fi

%preun
if [ $1 -eq 0 ]; then
    # Uninstall
    /usr/bin/systemctl stop urpmd.service >/dev/null 2>&1 || :
    /usr/bin/systemctl disable urpmd.service >/dev/null 2>&1 || :
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

%files -f %{pyproject_files}
%license LICENSE
%doc %{_docdir}/%{name}
%{_bindir}/urpm
%{_bindir}/urpmd
%{_unitdir}/urpmd.service
%{_sysconfdir}/bash_completion.d/urpm
%{_mandir}/man1/urpm.1*
%{_mandir}/man8/urpmd.8*
%{_mandir}/fr/man1/urpm.1*
%{_mandir}/fr/man8/urpmd.8*

%changelog
