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
	$(SED) -i 's/^__version__ = .*/__version__ = "$(VERSION)"/' __init__.py
	$(SED) -i 's/^version = .*/version = "$(VERSION)"/' pyproject.toml
	$(SED) -i 's/^%define version .*/%define version $(VERSION)/' rpmbuild/SPECS/rpmdrake-ng.spec

tarball: version
	$(SED) -i 's/^%define version.*/%define version $(VERSION)/' rpmbuild/SPECS/$(NAME).spec
	$(MKDIR) -p rpmbuild/SOURCES
	# Copy LICENSE from parent if not present
	@if [ ! -f LICENSE ]; then $(CP) ../LICENSE . 2>/dev/null || true; fi
	# Create tarball with proper structure:
	# rpmdrake-ng-0.1.0/rpmdrake/ contains Python package
	# rpmdrake-ng-0.1.0/ contains build files (pyproject.toml, etc.)
	cd .. && $(TAR) czf rpmdrake/rpmbuild/SOURCES/$(NAME)-$(VERSION).tar.gz \
		--transform "s,^rpmdrake/\(__init__\.py\|common\|qt\|helper\|doc\|bin\),$(NAME)-$(VERSION)/rpmdrake/\1," \
		--transform "s,^rpmdrake/\(pyproject\.toml\|README\.md\|LICENSE\|VERSION\|man\|data\),$(NAME)-$(VERSION)/\1," \
		--exclude='rpmdrake/rpmbuild' \
		--exclude='rpmdrake/Makefile' \
		--exclude='__pycache__' \
		--exclude='*.pyc' \
		rpmdrake/__init__.py \
		rpmdrake/common \
		rpmdrake/qt \
		rpmdrake/helper \
		rpmdrake/doc \
		rpmdrake/pyproject.toml \
		rpmdrake/README.md \
		rpmdrake/LICENSE \
		rpmdrake/VERSION \
		rpmdrake/man \
		rpmdrake/data \
		rpmdrake/bin

rpm: tarball
	cd rpmbuild && $(BM) -l SPECS/$(NAME).spec

clean:
	$(RM) -f rpmbuild/SOURCES/$(NAME)-*.tar.gz
	$(RM) -rf build dist *.egg-info

.PHONY: version tarball rpm clean
