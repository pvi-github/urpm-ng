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

clean:
	$(RM) -f rpmbuild/SOURCES/$(NAME)-*.tar.gz
	$(RM) -f rpmbuild/SOURCES/pk-backend-urpm.tar.gz

.PHONY: version tarball install-completion rpm clean
