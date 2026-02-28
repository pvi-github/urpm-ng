%define name rpmdrake-ng
%define version 0.4.1
%define release 1

Name:           %{name}
Version:        %{version}
Release:        %mkrel %{release}
Summary:        Modern graphical package manager for Mageia Linux
Summary(fr):    Gestionnaire de paquets graphique moderne pour Mageia Linux
License:        GPLv3+
Group:          System/Configuration/Packaging
URL:            https://github.com/pvi-github/urpm-ng
Source0:        %{name}-%{version}.tar.gz
BuildArch:      noarch

# Python build requirements
BuildRequires:  pyproject-rpm-macros
BuildRequires:  python3-devel
BuildRequires:  python3-wheel
BuildRequires:  python3-setuptools

# Runtime dependencies
Requires:       urpm-ng-core >= 0.3.0
Requires:       python3
# Only the PySide6 modules actually used (not the full meta-package)
Requires:       python3-pyside6-core
Requires:       python3-pyside6-gui
Requires:       python3-pyside6-widgets
Requires:       polkit

# Can coexist with old rpmdrake during transition

%description
rpmdrake-ng is a modern Qt6 graphical interface for managing packages on
Mageia Linux. It provides:
- Fast package search with incremental filtering
- Package installation, removal and updates
- System upgrade management
- Media configuration

Built on urpm-ng core library for reliable dependency resolution.

%description -l fr
rpmdrake-ng est une interface graphique Qt6 moderne pour la gestion des
paquets sur Mageia Linux. Il offre :
- Recherche rapide de paquets avec filtrage incrémental
- Installation, suppression et mise à jour de paquets
- Gestion des mises à jour système
- Configuration des médias

Basé sur la bibliothèque urpm-ng pour une résolution fiable des dépendances.

# ============================================================================
# Prep
# ============================================================================
%prep
%setup -q

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
%pyproject_wheel

# ============================================================================
# Install
# ============================================================================
%install
%pyproject_install
%pyproject_save_files rpmdrake

# Install desktop file
install -Dm644 data/rpmdrake-ng.desktop %{buildroot}%{_datadir}/applications/rpmdrake-ng.desktop

# Install icons
install -Dm644 data/icons/rpmdrake-ng.svg %{buildroot}%{_datadir}/icons/hicolor/scalable/apps/rpmdrake-ng.svg
install -Dm644 data/icons/rpmdrake-ng-48.png %{buildroot}%{_datadir}/icons/hicolor/48x48/apps/rpmdrake-ng.png

# Install PolicyKit policy for GUI authentication
install -Dm644 data/org.mageia.rpmdrake.policy %{buildroot}%{_datadir}/polkit-1/actions/org.mageia.rpmdrake.policy

# Install documentation
install -dm755 %{buildroot}%{_docdir}/%{name}
install -m644 README.md %{buildroot}%{_docdir}/%{name}/

# Install man pages (English)
install -Dm644 man/en/man1/rpmdrake-ng.1 %{buildroot}%{_mandir}/man1/rpmdrake-ng.1

# Install man pages (French)
install -Dm644 man/fr/man1/rpmdrake-ng.1 %{buildroot}%{_mandir}/fr/man1/rpmdrake-ng.1

# Install transaction helper to libexec (called via pkexec)
install -Dm755 bin/rpmdrake-ng-helper %{buildroot}%{_libexecdir}/rpmdrake-ng-helper

# ============================================================================
# Post-install scripts
# ============================================================================
%post
# Update icon cache
/usr/bin/gtk-update-icon-cache -f %{_datadir}/icons/hicolor 2>/dev/null || :
/usr/bin/update-desktop-database %{_datadir}/applications 2>/dev/null || :

%postun
if [ $1 -eq 0 ]; then
    /usr/bin/gtk-update-icon-cache -f %{_datadir}/icons/hicolor 2>/dev/null || :
    /usr/bin/update-desktop-database %{_datadir}/applications 2>/dev/null || :
fi

# ============================================================================
# Files
# ============================================================================
%files -f %{pyproject_files}
%license LICENSE
%doc %{_docdir}/%{name}
%{_bindir}/rpmdrake-ng
%{_libexecdir}/rpmdrake-ng-helper
%{_datadir}/applications/rpmdrake-ng.desktop
%{_datadir}/icons/hicolor/scalable/apps/rpmdrake-ng.svg
%{_datadir}/icons/hicolor/48x48/apps/rpmdrake-ng.png
%{_datadir}/polkit-1/actions/org.mageia.rpmdrake.policy
%{_mandir}/man1/rpmdrake-ng.1*
%{_mandir}/fr/man1/rpmdrake-ng.1*

%changelog
