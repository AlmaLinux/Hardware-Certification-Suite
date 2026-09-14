"""Every bundled dependency's license text reaches the RPM.

The suite's TUI vendors nine third-party packages, and MIT and PSF-2.0 both require the notice to
travel with the binary. The texts used to ship as ordinary payload inside ``_vendor/licenses/``,
which satisfied that but left ``rpm -q --licensefiles`` reporting only this project's own MIT - so
an automated license audit saw none of them. They are staged into ``bundled/`` and marked
``%license`` now.

What can go wrong from here is quiet: a version bump whose wheel stops shipping a license file, or a
new dependency added to the manifest, either of which drops a text from the package without failing
anything. These compare the three things that have to agree - the vendor lock, the vendored tree,
and the spec.

Skipped when ``packaging/`` is not beside the tests: the source tarball leaves it out, and this
suite runs from that tarball in the package's own ``%check``.
"""

import json
import pathlib
import re
import sys

import pytest

REPO = pathlib.Path(__file__).resolve().parent.parent
LOCK = REPO / "packaging" / "vendor" / "vendor.lock.json"
SPEC = REPO / "packaging" / "alma-certify.spec"
VENDOR = REPO / "alma_certify_tui" / "_vendor"


def _lock():
    if not LOCK.is_file():
        pytest.skip("no vendor lock here; this runs from a tarball that omits packaging/")
    return json.loads(LOCK.read_text(encoding="utf-8"))


def _spec():
    if not SPEC.is_file():
        pytest.skip("no spec here; this runs from a tarball that omits packaging/")
    return SPEC.read_text(encoding="utf-8")


def test_every_vendored_package_has_a_license_text():
    """A wheel that ships no license file is the one case that silently ships nothing: the copy
    step finds nothing, the list comes out short, and the package is built and signed anyway."""
    bare = [r["name"] for r in _lock()["packages"] if not r.get("license_files")]

    assert bare == [], "no license text vendored for: %s" % ", ".join(bare)


def test_every_declared_bundle_has_its_text_on_disk():
    """The ``Provides: bundled(...)`` list is the package's claim about what is inside it. Each
    entry needs a text in ``_vendor/licenses/`` for %%install to stage."""
    declared = set(re.findall(r"bundled\(python3dist\(([^)]+)\)\)", _spec()))
    assert declared, "no bundled Provides found; has the spec moved?"

    for name in sorted(declared):
        directory = VENDOR / "licenses" / name
        assert directory.is_dir(), "%s is declared bundled but has no licenses/%s/" % (name, name)
        assert any(directory.iterdir()), "licenses/%s/ is empty" % name


def test_the_lock_and_the_spec_agree_on_what_is_bundled():
    """Two hand-updated lists, and the vendoring script prints the spec's. They drift when somebody
    pastes half of it."""
    declared = set(re.findall(r"bundled\(python3dist\(([^)]+)\)\)", _spec()))
    vendored = {r["name"] for r in _lock()["packages"]}

    assert declared == vendored, "spec %s / lock %s" % (
        sorted(declared - vendored), sorted(vendored - declared)
    )


def test_notices_inside_a_vendored_package_are_collected_too():
    """mdit-py-plugins keeps a per-plugin notice for code it vendored in turn - separate
    copyrights, in the package directory rather than in ``licenses/``. The spec collects them by
    glob, and the vendoring script finds them by scanning, so neither needs a hand-kept list; this
    pins that they are still found and still shipped."""
    sys.path.insert(0, str(REPO / "packaging" / "vendor"))
    if not (REPO / "packaging" / "vendor" / "update_vendor.py").is_file():
        pytest.skip("no vendoring script here")
    import update_vendor

    found = update_vendor._in_tree_licenses(VENDOR)

    assert found, "the scan found none; it silently stopped matching"
    assert all(not name.endswith(".py") for name in found), "a source file matched on its name"
    assert "mdit_py_plugins/*/LICENSE" in _spec(), "the spec no longer collects them"


def test_the_spec_marks_the_staged_tree_as_license():
    """``%license bundled/`` is the whole point: without it the texts are ordinary payload and
    ``rpm -q --licensefiles`` reports only this project's own."""
    spec = _spec()

    assert "%license bundled/" in spec
    assert "cp -a alma_certify_tui/_vendor/licenses/. bundled/" in spec


def test_each_bundled_provides_names_its_license():
    """The subpackage's own ``License:`` tag is one expression over the whole bundled set, so it
    says PSF-2.0 is in there without saying which package brings it. The comment above each
    ``Provides`` says which, and has to keep saying the right thing when a version bump relicenses
    something under us."""
    recorded = {r["name"]: r["license"] for r in _lock()["packages"]}
    lines = _spec().splitlines()

    seen = {}
    for index, line in enumerate(lines):
        match = re.match(r"Provides:\s+bundled\(python3dist\(([^)]+)\)\)", line)
        if not match:
            continue
        name = match.group(1)
        above = lines[index - 1].strip()
        assert above.startswith("#"), "%s has no license comment above it" % name
        seen[name] = above.lstrip("# ").strip()

    assert seen, "no bundled Provides found; has the spec moved?"
    wrong = {n: (got, recorded[n]) for n, got in seen.items() if got != recorded[n]}
    assert not wrong, "comment disagrees with the vendor lock: %s" % wrong
