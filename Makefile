NAME = urpm-ng
VERSION = $(shell /usr/bin/cat VERSION)

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
	$(MAKE) -C rpmdrake version

tarball: version
	$(SED) -i 's/^%define version.*/%define version $(VERSION)/' rpmbuild/SPECS/$(NAME).spec
	$(MKDIR) -p rpmbuild/SOURCES
	# Main tarball (Python package)
	$(TAR) czf rpmbuild/SOURCES/$(NAME)-$(VERSION).tar.gz \
		--transform "s,^,$(NAME)-$(VERSION)/," \
		urpm pyproject.toml README.md QUICKSTART.md CHANGELOG.md LICENSE doc completion man data VERSION
	# PackageKit backend tarball
	$(TAR) czf rpmbuild/SOURCES/pk-backend-urpm.tar.gz \
		--transform "s,^pk-backend-urpm/,pk-backend-urpm/," \
		pk-backend-urpm/pk-backend-urpm.c \
		pk-backend-urpm/pk-backend.h \
		pk-backend-urpm/pk-backend-job.h \
		pk-backend-urpm/pk-shared.h \
		pk-backend-urpm/meson.build \
		pk-backend-urpm/meson_options.txt

install-completion:
	install -D -m 644 completion/urpm.bash /etc/bash_completion.d/urpm

rpm: tarball
	cd rpmbuild && $(BM) -l SPECS/$(NAME).spec

rpm-rpmdrake:
	$(MAKE) -C rpmdrake rpm

rpm-all: rpm rpm-rpmdrake

clean:
	$(RM) -f rpmbuild/SOURCES/$(NAME)-*.tar.gz
	$(RM) -f rpmbuild/SOURCES/pk-backend-urpm.tar.gz
	$(MAKE) -C rpmdrake clean

# ============================================================================
# Internationalization (i18n)
# ============================================================================

XGETTEXT = /usr/bin/xgettext
MSGMERGE = /usr/bin/msgmerge
MSGFMT = /usr/bin/msgfmt
PO_DIR = po
DOMAIN = urpm
LINGUAS = fr de es pt

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

.PHONY: version tarball install-completion rpm rpm-rpmdrake rpm-all clean pot po-update mo po-stats clean-i18n
