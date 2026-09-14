# Packaging helpers. The suite itself needs no build step - it is stdlib-only
# Python - so these targets only produce a source tarball and drive rpmbuild.
#
#   make srpm                 source RPM from the working tree
#   make rpm                  binary RPM for this machine's distribution
#   make mock-rpm DIST=alma+epel-9-x86_64
#                             clean-room build for an AlmaLinux target
#   make mock-all             build for AlmaLinux 8, 9, and 10 (x86_64)
#   make copr COPR_PROJECT=<user>/alma-certify
#                             submit that SRPM to Copr (asks for the project if unset)
#   make clpeak-source        fetch the pinned clpeak source (rpm/tarball do this for you)
#   make test lint            what CI runs

NAME    := alma-certify
VERSION := $(shell sed -n 's/^__version__ = "\(.*\)"/\1/p' alma_certify/__init__.py)
SPEC    := packaging/$(NAME).spec
DIST    ?= alma+epel-9-x86_64
TOPDIR  := $(CURDIR)/build/rpm
SOURCEDIR := $(TOPDIR)/SOURCES
# .tar.gz, and the same name and inner directory GitHub's archive of the v$(VERSION) tag produces,
# because that archive is Source0. rpmbuild finds it by basename, so the two have to agree.
TARBALL := $(SOURCEDIR)/$(NAME)-$(VERSION).tar.gz

ALMA_DISTS := alma+epel-8-x86_64 alma+epel-9-x86_64 alma+epel-10-x86_64

# The SRPM `srpm` writes, named rather than found. Every target below used `find ... | head -1`,
# which picks whichever one is on the shelf: with `copr` that is a stale version published under
# its own name, and nothing about it looks wrong until somebody installs it. %dist is cleared in
# the recipe, so this has to clear it here too or the names disagree on a Fedora workstation.
SRPM_RELEASE := $(shell rpmspec -P $(SPEC) --define 'dist %{nil}' 2>/dev/null \
                        | awk '/^Release:/ {print $$2; exit}')
SRPM := $(TOPDIR)/SRPMS/$(NAME)-$(VERSION)-$(SRPM_RELEASE).src.rpm

# Copr, for the builds that are not a release: a branch somebody needs on real hardware today, or
# an el8 fix nobody wants to tag for. `copr-cli` takes the SRPM `srpm` already produced, which is
# the point of doing it this way - the SRPM carries clpeak as Source1, so Copr builds the same
# bytes that were proved locally and needs no network of its own to find them.
#
# Which chroots it builds for belongs to the project, not to this command: they are configured once
# on the Copr project and every build uses them. COPR_CHROOTS narrows one build to some of them.
#
# COPR_PROJECT has no default, and a default is not an oversight to fix. This uploads to somebody's
# repository, and a project named here is the one a bare `make copr` would publish to for the rest
# of the project's life, whether or not that is still where these builds belong. So the destination
# is stated every time: asked for at a terminal, once the SRPM exists and the prompt can say what
# is about to go where, and refused outright where there is nobody to ask.
#
# This publishes the working tree, as every target here does. That is usually what a Copr build is
# for, so it is not blocked, but the NVR is printed before the upload starts because "which commit
# is in that repo" is otherwise unanswerable afterwards.
COPR_PROJECT ?=
COPR_CHROOTS ?=
COPR_CONFIG  ?=
COPR_ARGS    ?=

# clpeak is built on the machine under test, from source shipped inside this package, so its
# backends match that machine's hardware. It is Source1, fetched here at packaging time on a
# maintainer's machine with a network: nothing in the suite downloads at run time, and a benchmark
# built against whatever a branch said today is not comparable with one built last month.
#
# Both the version and the URL come from the spec, which is the only place either is stated. They
# were duplicated here once and drifted: the spec moved to 2.1.4 while this still said 2.0.18, so
# `make` fetched one tarball and rpmbuild asked for another.
CLPEAK_URL := $(shell rpmspec -P $(SPEC) 2>/dev/null | awk '/^Source1:/ {print $$2}')
CLPEAK_TARBALL := $(SOURCEDIR)/$(notdir $(CLPEAK_URL))

.PHONY: all tarball vendored-tarball clpeak-in-tree srpm rpm mock-rpm mock-all copr test lint \
        clean version clpeak-source

all: rpm

version:
	@echo $(VERSION)

# Packs the working tree, not HEAD: building an uncommitted change is the
# normal case while developing, and in CI the checkout *is* the tag, so the
# result is identical there. --sort=name plus a fixed mtime keeps it
# reproducible.
# --exclude-vcs-ignores honors .gitignore, which is what keeps a stray local file out of a
# release tarball. The explicit list is not enough on its own: this packs the working tree
# rather than HEAD, so anything gitignored but present still shipped. A coverage database did.
EXCLUDES := --exclude-vcs --exclude-vcs-ignores --exclude=build \
            --exclude=__pycache__ --exclude='*.pyc'

# A real file target, so a build that needs it fetches it and a build that has it does not refetch.
# That is also the offline story: drop the tarball into build/rpm/SOURCES yourself and nothing
# reaches for the network. There is no build-without-it switch any more, because clpeak is Source1
# and rpmbuild will not start without every source present.
#
# ``tarball`` depends on this rather than warning about it. It was a separate step somebody had to
# remember, and the first person to forget shipped a package whose GPU compute benchmark silently
# skipped: the warning scrolled past in the middle of an rpmbuild and the run said only that the
# source was missing. A packaging step that has to be remembered will be forgotten.
#
# The no-download rule this appears to bend is about the machine under test, and that is untouched.
# A package build is a maintainer action on a networked machine which already downloads plenty, and
# what ships is still a pinned tarball.
SOURCE_SUMS := $(CURDIR)/packaging/sources.sha512

$(CLPEAK_TARBALL):
	@mkdir -p $(SOURCEDIR)
	@echo "fetching $(notdir $(CLPEAK_URL)) (Source1)"
	@curl -fsSL -o $(CLPEAK_TARBALL).part $(CLPEAK_URL)
	@cd $(SOURCEDIR) && mv $(notdir $(CLPEAK_TARBALL)).part $(notdir $(CLPEAK_TARBALL)) && \
	  sha512sum -c --ignore-missing --quiet $(SOURCE_SUMS) || { \
	    echo "checksum mismatch for $(notdir $(CLPEAK_TARBALL)); refusing it." >&2; \
	    echo "Either upstream moved the tag, or GitHub regenerated the archive: it does not" >&2; \
	    echo "promise byte-stable /archive/ tarballs and has changed them before. Check the tag" >&2; \
	    echo "against upstream, then regenerate packaging/sources.sha512 if it is genuinely the" >&2; \
	    echo "same source." >&2; \
	    rm -f $(CLPEAK_TARBALL); exit 1; }

# For running the suite from a checkout, as CI does: it looks for clpeak beside its own code rather
# than in the RPM's source directory. Verified by the target above before it is copied anywhere.
clpeak-in-tree: $(CLPEAK_TARBALL)
	@cp $(CLPEAK_TARBALL) alma_certify/data/
	@echo "alma_certify/data/$(notdir $(CLPEAK_TARBALL))"

clpeak-source: $(CLPEAK_TARBALL)
	@echo "$(CLPEAK_TARBALL)"

# Both sources staged where rpmbuild looks for them, so `make srpm` and `make rpm` need nothing
# prepared by hand.
tarball: $(CLPEAK_TARBALL)
	@mkdir -p $(SOURCEDIR)
	@tar $(EXCLUDES) --sort=name --mtime='@0' \
	     --owner=0 --group=0 --numeric-owner \
	     --transform 's,^\.,$(NAME)-$(VERSION),' \
	     -czf $(TARBALL) .
	@echo "$(TARBALL)"

# A second, self-contained artifact for anyone who is not going through dist-git: the same source
# tree with clpeak already in alma_certify/data/, where the suite looks for it at run time. Unpack
# and run, with the GPU compute benchmark working and nothing to fetch.
#
# Named alma-certify-vendored, inside and out, so it cannot be mistaken for Source0. Source0 is
# GitHub's own archive of the tag, and the spec takes clpeak separately as Source1 so that
# `Provides: bundled(clpeak)` stays verifiable against its real upstream. This one is a
# point-in-time convenience published beside them, and nothing here consumes it.
#
# zstd at --ultra -22: it carries clpeak's source on top of a vendored Textual stack, it is built
# once per release, and whoever downloads it pays for the size.
VENDORED_NAME := $(NAME)-vendored-$(VERSION)
VENDORED := $(SOURCEDIR)/$(VENDORED_NAME).tar.zst

vendored-tarball: tarball
	@rm -rf $(TOPDIR)/vendored && mkdir -p $(TOPDIR)/vendored
	@tar xf $(TARBALL) -C $(TOPDIR)/vendored
	@mv $(TOPDIR)/vendored/$(NAME)-$(VERSION) $(TOPDIR)/vendored/$(VENDORED_NAME)
	@cp $(CLPEAK_TARBALL) $(TOPDIR)/vendored/$(VENDORED_NAME)/alma_certify/data/
	@tar --sort=name --mtime='@0' --owner=0 --group=0 --numeric-owner \
	     -I 'zstd --ultra -22 -T0' \
	     -cf $(VENDORED) -C $(TOPDIR)/vendored $(VENDORED_NAME)
	@rm -rf $(TOPDIR)/vendored
	@echo "$(VENDORED)"

# %dist is cleared so the SRPM is not stamped with the build host's own
# distribution: mock stamps the real target (.el8/.el9/.el10) on rebuild.
srpm: tarball
	rpmbuild -bs $(SPEC) \
	    --define "_topdir $(TOPDIR)" \
	    --define "_sourcedir $(SOURCEDIR)" \
	    --define "dist %{nil}"
	@find $(TOPDIR)/SRPMS -name '*.src.rpm' -printf '%p\n'

# RPMBUILD_EXTRA=--nocheck skips %check, for a caller that has already run the tests elsewhere.
rpm: tarball
	rpmbuild -bb $(SPEC) $(RPMBUILD_EXTRA) \
	    --define "_topdir $(TOPDIR)" \
	    --define "_sourcedir $(SOURCEDIR)"
	@find $(TOPDIR)/RPMS -name '*.rpm' -printf '%p\n'

# Builds against a real AlmaLinux root, so el8's python3.12 dependency and
# el9/el10 differences are exercised rather than assumed.
#
# --resultdir puts the output under build/ next to `make rpm`, rather than
# leaving it in /var/lib/mock where it is easy to miss, and the recipe names
# the package it produced: mock's own output is long enough that a bare
# "done" tells you nothing about whether a binary RPM actually appeared.
mock-rpm: srpm
	@mkdir -p $(TOPDIR)/RPMS/$(DIST)
	mock -r $(DIST) --resultdir=$(TOPDIR)/RPMS/$(DIST) --rebuild $(SRPM)
	@echo
	@ls $(TOPDIR)/RPMS/$(DIST)/*.noarch.rpm 2>/dev/null \
	  || { echo "no binary RPM produced; see $(TOPDIR)/RPMS/$(DIST)/build.log" >&2; \
	       exit 1; }

mock-all: srpm
	@rc=0; for d in $(ALMA_DISTS); do \
	    printf '=== %s ... ' "$$d"; \
	    mkdir -p $(TOPDIR)/RPMS/$$d; \
	    if mock -r $$d --resultdir=$(TOPDIR)/RPMS/$$d --rebuild $(SRPM) \
	         >$(TOPDIR)/RPMS/$$d/mock.out 2>&1; then \
	        echo "ok"; ls $(TOPDIR)/RPMS/$$d/*.noarch.rpm | sed 's/^/    /'; \
	    else \
	        echo "FAILED"; \
	        echo "    see $(TOPDIR)/RPMS/$$d/build.log" >&2; rc=1; \
	    fi; \
	done; exit $$rc

# The checks are here because copr-cli's own failures for them arrive after the SRPM has been
# uploaded and read like server errors rather than like something the caller left out.
copr: srpm
	@command -v copr-cli >/dev/null 2>&1 || { \
	    echo "copr-cli not found: dnf install copr-cli" >&2; exit 1; }
	@test -n "$(COPR_CONFIG)" -o -r "$(HOME)/.config/copr" || { \
	    echo "no Copr credentials in ~/.config/copr." >&2; \
	    echo "Log in at https://copr.fedorainfracloud.org/api/ and save the block it shows," >&2; \
	    echo "or point COPR_CONFIG at one elsewhere." >&2; exit 1; }
	@project='$(COPR_PROJECT)'; \
	if [ -z "$$project" ] && [ -t 0 ]; then \
	    echo "about to submit $(notdir $(SRPM)) to Copr."; \
	    printf 'Project (project, user/project, or @group/project): '; \
	    read -r project || project=; \
	fi; \
	if [ -z "$$project" ]; then \
	    echo "no Copr project given, so nothing was submitted. Name one:" >&2; \
	    echo "    make copr COPR_PROJECT=<user>/$(NAME)" >&2; \
	    echo "or export COPR_PROJECT to keep it for the session." >&2; \
	    exit 1; \
	fi; \
	echo "submitting $(notdir $(SRPM)) to $$project"; \
	copr-cli $(if $(COPR_CONFIG),--config $(COPR_CONFIG)) build "$$project" \
	    $(foreach c,$(COPR_CHROOTS),-r $(c)) $(COPR_ARGS) $(SRPM)

test:
	python3 -m pytest tests -ra

lint:
	ruff check alma_certify tests

clean:
	rm -rf build
