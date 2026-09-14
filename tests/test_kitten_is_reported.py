"""The report has to say when a run happened on AlmaLinux Kitten.

Kitten is indistinguishable from the stable release of the same major on every field the
report used to carry. Its ``/etc/os-release`` says ``ID=almalinux`` and ``VERSION_ID=10``,
exactly as AlmaLinux 10 does, and names itself only in ``NAME``/``PRETTY_NAME``.

That matters because the catalog treats the two differently. A pass on a stable release proves
hardware works on something people can install today. A pass on Kitten proves the major works
but the enablement lands in an upcoming minor, so the listing carries a disclaimer until that
minor ships. Nothing server-side could tell them apart before ``pretty_name`` was recorded.

The string is stored verbatim rather than reduced to a flag here. How to read it is a judgement
that may need revising - Kitten's naming is not ours to control - and a boolean computed at
collection time freezes today's guess into every bundle ever submitted.
"""
import pytest

from alma_certify import hostos, procutil, report

KITTEN = (
    'NAME="AlmaLinux Kitten"\n'
    'VERSION="10 (Purple Lion)"\n'
    'ID="almalinux"\n'
    'VERSION_ID="10"\n'
    'PRETTY_NAME="AlmaLinux Kitten 10 (Purple Lion)"\n'
)

STABLE = (
    'NAME="AlmaLinux"\n'
    'VERSION="10.0 (Purple Lion)"\n'
    'ID="almalinux"\n'
    'VERSION_ID="10.0"\n'
    'PRETTY_NAME="AlmaLinux 10.0 (Purple Lion)"\n'
)


@pytest.fixture
def _os_release(monkeypatch):
    """Every parser reads /etc/os-release through procutil.read_file."""
    def _install(content):
        real = procutil.read_file
        monkeypatch.setattr(
            procutil, "read_file",
            lambda path, default=None: (
                content if "os-release" in str(path) else real(path, default)
            ),
        )
    return _install


def test_kitten_is_named_in_the_environment(_os_release):
    _os_release(KITTEN)

    environment = report.capture_environment([])

    assert environment["os"]["pretty_name"] == "AlmaLinux Kitten 10 (Purple Lion)"


def test_kitten_is_otherwise_identical_to_the_stable_release(_os_release):
    """The reason the new field is needed at all, asserted rather than described."""
    _os_release(KITTEN)
    kitten = report.capture_environment([])["os"]

    assert kitten["id"] == "almalinux"
    assert kitten["version_id"] == "10"
    assert "kitten" in kitten["pretty_name"].lower()


def test_a_stable_release_says_nothing_about_kitten(_os_release):
    _os_release(STABLE)

    os_block = report.capture_environment([])["os"]

    assert "kitten" not in os_block["pretty_name"].lower()


def test_kitten_is_still_submittable(_os_release):
    """It is AlmaLinux, and the submit gate must keep letting it through. Holding it back at
    upload would lose the evidence entirely; the catalog gates the *claim*, not the run."""
    _os_release(KITTEN)

    environment = report.capture_environment([])

    assert hostos.report_is_almalinux({"environment": environment}) is True


def test_a_file_with_no_pretty_name_gives_an_empty_string(_os_release):
    """Minimal and hand-built images omit it. Blank reads as "not Kitten", which is the right
    default: a run is only gated on evidence that it was pre-release, never on an absence."""
    _os_release('ID="almalinux"\nVERSION_ID="9.6"\n')

    assert report.capture_environment([])["os"]["pretty_name"] == ""
