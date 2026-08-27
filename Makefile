NAME = urpm-ng
VERSION = $(shell /usr/bin/cat VERSION)
RELEASE = $(shell /usr/bin/cat RELEASE)

CAT = /usr/bin/cat
SED = /usr/bin/sed
TAR = /usr/bin/tar
RM = /usr/bin/rm
BM = /usr/bin/bm
MKDIR = /usr/bin/mkdir

version:
	$(SED) -i 's/^__version__ = .*/__version__ = "$(VERSION)"/' urpm/__init__.py
	$(SED) -i 's/^version = .*/version = "$(VERSION)"/' pyproject.toml
	$(SED) -i 's/^%define version .*/%define version $(VERSION)/' rpmbuild/SPECS/urpm-ng.spec
	$(SED) -i 's/^%define release .*/%define release $(RELEASE)/' rpmbuild/SPECS/urpm-ng.spec
	echo $(VERSION) > rpmdrake/VERSION
	$(MAKE) -C rpmdrake version RELEASE=$(RELEASE)

tarball: version tarball-rpmdrake
	$(SED) -i 's/^%define version.*/%define version $(VERSION)/' rpmbuild/SPECS/$(NAME).spec
	$(SED) -i 's/^%define release.*/%define release $(RELEASE)/' rpmbuild/SPECS/$(NAME).spec
	$(MKDIR) -p rpmbuild/SOURCES
	# Main tarball (Python package)
	$(TAR) czf rpmbuild/SOURCES/$(NAME)-$(VERSION).tar.gz \
		--transform "s,^,$(NAME)-$(VERSION)/," \
		--exclude='urpm/tests' \
		urpm pyproject.toml README.md QUICKSTART.md CHANGELOG.md LICENSE doc completion man data VERSION po
	# PackageKit backend tarball — versioned alongside the main tarball
	# so SOURCES/ keeps a per-release archive trail rather than
	# overwriting the previous one in place.
	$(TAR) czf rpmbuild/SOURCES/pk-backend-urpm-$(VERSION).tar.gz \
		--transform "s,^pk-backend-urpm/,pk-backend-urpm/," \
		pk-backend-urpm/pk-backend-urpm.c \
		pk-backend-urpm/pk-backend.h \
		pk-backend-urpm/pk-backend-job.h \
		pk-backend-urpm/pk-shared.h \
		pk-backend-urpm/meson.build \
		pk-backend-urpm/meson_options.txt

# Rpmdrake-NG sibling tarball — produced by descending into rpmdrake/.
# Symmetric with rpm-rpmdrake; chained from the top-level tarball so
# a single ``make tarball`` builds everything (the previous behaviour
# silently skipped rpmdrake's tarball).
tarball-rpmdrake:
	$(MAKE) -C rpmdrake tarball RELEASE=$(RELEASE)

install-completion:
	install -D -m 644 completion/urpm.bash /etc/bash_completion.d/urpm

rpm: tarball
	cd rpmbuild && $(BM) -l SPECS/$(NAME).spec

rpm-rpmdrake:
	$(MAKE) -C rpmdrake rpm RELEASE=$(RELEASE)

rpm-all: rpm rpm-rpmdrake

# ============================================================================
# Install targets — install the RPMs freshly built by ``make rpm''/``rpm-all''
# to match the current VERSION and RELEASE.  Same fallback logic as
# geturpm.sh: use ``urpm i --reinstall'' when urpm-ng-core is already
# on the box, ``urpmi'' otherwise (bootstrap path).  All privileged
# commands are wrapped in a single ``su -c'' so the root password is
# asked once at most.
#
# install-core → urpm-ng-core only
# install      → urpm-ng-core + urpm-ng-daemon + urpm-ng meta
# install-all  → every urpm-ng sub-package (including -all) + rpmdrake-ng
# ============================================================================

INSTALL_VR = $(VERSION)-$(RELEASE)

# Disttag of the current host — ``rpm --eval "%{?dist}"`` returns
# ``.mga10`` on mga10, ``.mga9`` on mga9, etc.  Used to filter the
# install ``find`` patterns so a machine with mga9 and mga10 RPMs
# co-located under rpmbuild/RPMS/ never installs the wrong ones.
INSTALL_DIST := $(shell rpm --eval "%{?dist}")

# Shared body — expects $$META (the top-level RPMs to hand to
# ``urpm i'', which then sibling-scans for subpackages).  On a
# box that has no urpm-ng yet we bootstrap by ``urpmi''-ing just
# urpm-ng-core, then hand the remaining META RPMs off to the now-
# installed ``urpm i''.  If META already IS urpm-ng-core (the
# install-core case), we skip the redundant second step.  One su
# prompt whichever path we take.
INSTALL_CMD = \
	if [ -z "$$META" ]; then \
		echo "no RPMs matching $(INSTALL_VR) -- run 'make rpm' or 'make rpm-all' first" >&2; \
		exit 1; \
	fi; \
	CORE=$$(find rpmbuild/RPMS -name "urpm-ng-core-$(INSTALL_VR)$(INSTALL_DIST).*.rpm" \
	              ! -name "*-debuginfo-*" ! -name "*-debugsource-*" | head -1); \
	if rpm -q urpm-ng-core >/dev/null 2>&1; then \
		echo "==> urpm-ng-core present; urpm i --auto --reinstall (sibling scan picks the rest)"; \
		echo "$$META" | tr ' ' '\n' | sed '/^$$/d; s|^|  |'; \
		su -c "urpm i --auto --reinstall $$META"; \
	elif [ "$$(echo $$META | tr -s ' ' | sed 's/^ //; s/ $$//')" = "$$CORE" ]; then \
		echo "==> urpm-ng not installed; urpmi bootstraps urpm-ng-core"; \
		echo "  $$CORE"; \
		su -c "urpmi --auto $$CORE"; \
	else \
		echo "==> urpm-ng not installed; urpmi urpm-ng-core, then urpm i for the rest"; \
		echo "  step 1: $$CORE"; \
		echo "$$META" | tr ' ' '\n' | sed '/^$$/d; s|^|  step 2: |'; \
		su -c "urpmi --auto $$CORE && urpm i --auto --reinstall $$META"; \
	fi

install-core:
	@META=$$(find rpmbuild/RPMS \
	              -name "urpm-ng-core-$(INSTALL_VR)$(INSTALL_DIST).*.rpm" \
	              ! -name "*-debuginfo-*" ! -name "*-debugsource-*" \
	              | tr '\n' ' '); \
	$(INSTALL_CMD)

install:
	@META=$$(find rpmbuild/RPMS \
	              -name "urpm-ng-$(INSTALL_VR)$(INSTALL_DIST).*.rpm" \
	              ! -name "*-debuginfo-*" ! -name "*-debugsource-*" \
	              | tr '\n' ' '); \
	$(INSTALL_CMD)

install-all:
	@META="$$(find rpmbuild/RPMS -name "urpm-ng-all-$(INSTALL_VR)$(INSTALL_DIST).*.rpm" | tr '\n' ' ')$$(find rpmdrake/rpmbuild/RPMS -name "rpmdrake-ng-$(INSTALL_VR)$(INSTALL_DIST).*.rpm" | tr '\n' ' ')"; \
	$(INSTALL_CMD)

clean:
	$(RM) -f rpmbuild/SOURCES/$(NAME)-*.tar.gz
	$(RM) -f rpmbuild/SOURCES/pk-backend-urpm-*.tar.gz
	$(MAKE) -C rpmdrake clean

# ============================================================================
# Internationalization (i18n)
# ============================================================================

XGETTEXT = /usr/bin/xgettext
MSGMERGE = /usr/bin/msgmerge
MSGFMT = /usr/bin/msgfmt
PO_DIR = po
DOMAIN = urpm
LINGUAS = fr de es pt nl it

pot:
	$(XGETTEXT) --language=Python --keyword=_ --keyword=N_ \
		--keyword=ngettext:1,2 --from-code=UTF-8 --force-po \
		--package-name=$(NAME) --package-version=$(VERSION) \
		--msgid-bugs-address=i18n@mageia.org \
		--copyright-holder="Mageia" \
		--output=$(PO_DIR)/$(DOMAIN).pot \
		$$($(CAT) $(PO_DIR)/POTFILES.in)

po-update: pot
	@for lang in $(LINGUAS); do \
		if [ -f $(PO_DIR)/$$lang.po ]; then \
			echo "Updating $$lang.po..."; \
			$(MSGMERGE) --update --backup=none $(PO_DIR)/$$lang.po $(PO_DIR)/$(DOMAIN).pot; \
		else \
			echo "Creating $$lang.po..."; \
			msginit --no-translator --locale=$$lang \
				--input=$(PO_DIR)/$(DOMAIN).pot --output=$(PO_DIR)/$$lang.po; \
		fi \
	done

mo:
	@for lang in $(LINGUAS); do \
		$(MKDIR) -p $(PO_DIR)/locale/$$lang/LC_MESSAGES; \
		echo "Compiling $$lang.mo..."; \
		$(MSGFMT) -o $(PO_DIR)/locale/$$lang/LC_MESSAGES/$(DOMAIN).mo \
			$(PO_DIR)/$$lang.po; \
	done

po-stats:
	@for lang in $(LINGUAS); do \
		echo "$$lang:"; \
		msgfmt --statistics $(PO_DIR)/$$lang.po 2>&1 | sed 's/^/  /'; \
	done

clean-i18n:
	$(RM) -rf $(PO_DIR)/locale

.PHONY: version tarball install-completion rpm rpm-rpmdrake rpm-all \
        install-core install install-all \
        clean pot po-update mo po-stats clean-i18n
