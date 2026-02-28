NAME = rpmdrake-ng
VERSION = $(shell /usr/bin/cat VERSION)

CAT = /usr/bin/cat
SED = /usr/bin/sed
TAR = /usr/bin/tar
RM = /usr/bin/rm
BM = /usr/bin/bm
MKDIR = /usr/bin/mkdir
CP = /usr/bin/cp

version:
	$(SED) -i 's/^__version__ = .*/__version__ = "$(VERSION)"/' rpmdrake/__init__.py
	$(SED) -i 's/^version = .*/version = "$(VERSION)"/' pyproject.toml
	$(SED) -i 's/^%define version .*/%define version $(VERSION)/' rpmbuild/SPECS/rpmdrake-ng.spec

tarball: version
	$(SED) -i 's/^%define version.*/%define version $(VERSION)/' rpmbuild/SPECS/$(NAME).spec
	$(MKDIR) -p rpmbuild/SOURCES
	# Copy LICENSE from parent if not present
	@if [ ! -f LICENSE ]; then $(CP) ../LICENSE . 2>/dev/null || true; fi
	# Create tarball with proper structure:
	# rpmdrake-ng-X.Y.Z/rpmdrake/ contains Python package
	# rpmdrake-ng-X.Y.Z/ contains build files (pyproject.toml, etc.)
	cd .. && $(TAR) czf rpmdrake/rpmbuild/SOURCES/$(NAME)-$(VERSION).tar.gz \
		--transform "s,^rpmdrake/rpmdrake,$(NAME)-$(VERSION)/rpmdrake," \
		--transform "s,^rpmdrake/\(pyproject\.toml\|README\.md\|LICENSE\|VERSION\|man\|data\|bin\|doc\),$(NAME)-$(VERSION)/\1," \
		--exclude='rpmdrake/rpmbuild' \
		--exclude='rpmdrake/Makefile' \
		--exclude='rpmdrake/po' \
		--exclude='__pycache__' \
		--exclude='*.pyc' \
		--exclude='.omc' \
		rpmdrake/rpmdrake \
		rpmdrake/pyproject.toml \
		rpmdrake/README.md \
		rpmdrake/LICENSE \
		rpmdrake/VERSION \
		rpmdrake/man \
		rpmdrake/data \
		rpmdrake/bin \
		rpmdrake/doc

rpm: tarball
	cd rpmbuild && $(BM) -l SPECS/$(NAME).spec

clean:
	$(RM) -f rpmbuild/SOURCES/$(NAME)-*.tar.gz
	$(RM) -rf build dist *.egg-info

# ============================================================================
# Internationalization (i18n)
# ============================================================================

XGETTEXT = /usr/bin/xgettext
MSGMERGE = /usr/bin/msgmerge
MSGFMT = /usr/bin/msgfmt
PO_DIR = po
DOMAIN = rpmdrake
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

.PHONY: version tarball rpm clean pot po-update mo po-stats clean-i18n
