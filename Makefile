NAME = urpm-ng
VERSION = $(shell /usr/bin/cat VERSION)

CAT = /usr/bin/cat
SED = /usr/bin/sed
TAR = /usr/bin/tar
RM = /usr/bin/rm
BM = /usr/bin/bm

version:
	$(SED) -i 's/^__version__ = .*/__version__ = "$(VERSION)"/' urpm/__init__.py

tarball: version
	$(SED) -i 's/^%define version.*/%define version $(VERSION)/' rpmbuild/SPECS/$(NAME).spec
	$(TAR) czf rpmbuild/SOURCES/$(NAME)-$(VERSION).tar.gz \
		--transform "s,^,$(NAME)-$(VERSION)/," \
		urpm pyproject.toml README.md QUICKSTART.md CHANGELOG.md LICENSE doc VERSION

rpm: tarball
	cd rpmbuild && $(BM) -l SPECS/$(NAME).spec

clean:
	$(RM) -f rpmbuild/SOURCES/$(NAME)-*.tar.gz
