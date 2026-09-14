"""Package manager: transaction isolation and repository setup.

The regression these guard against: dnf installs a transaction atomically, so
batching a group's packages into one `dnf install` means a single unavailable
name (say a benchmark tool absent from this EPEL release) prevents every other
package in the batch from installing. That made staples like gcc and make get
reported as "unavailable in the configured repos".
"""

import pytest

from alma_certify import pkg as pkg_mod
from alma_certify.pkg import PackageManager, crb_repo_name


class FakeDnf:
    """Stands in for rpm/dnf. ``available`` is what the repos can supply.

    ``--enablerepo`` is honored the way dnf honors it, which this did not do and which is why a
    real bug lived here: an id no repository has is ``Error: Unknown repo`` and dnf installs
    nothing at all, while this fake dropped every argument starting with ``-`` and cheerfully
    installed the packages. Every test passed against ``--enablerepo=crb`` on an el8 machine, where
    the repository is called ``powertools``.
    """

    def __init__(self, available, installed=(), atomic=True, known_repos=None):
        self.available = set(available)
        self.installed = set(installed)
        self.atomic = atomic          # dnf's real all-or-nothing behavior
        self.install_calls = []
        self.enable_flags = []        # the --enablerepo ids each install asked for
        self.enabled_repos = {"baseos", "appstream"}
        # What this machine *has*, enabled or not. ``--enablerepo`` naming anything else is fatal.
        self.known_repos = set(
            known_repos if known_repos is not None
            else {"baseos", "appstream", "extras", "crb"}
        )

    def run_cmd(self, argv, timeout=None, tee_path=None, check=False, **kw):
        from alma_certify.procutil import CmdResult

        if argv[:2] == ["rpm", "-q"]:
            name = argv[-1]
            ok = name in self.installed
            return CmdResult(argv, 0 if ok else 1,
                             "%s-1.0-1\n" % name if ok else "not installed", "")
        if argv[:2] == ["dnf", "install"]:
            names = [a for a in argv[2:] if not a.startswith("-")]
            self.install_calls.append(names)
            asked = [a.split("=", 1)[1] for a in argv[2:]
                     if a.startswith("--enablerepo=")]
            self.enable_flags.append(asked)
            unknown = [r for r in asked if r not in self.known_repos]
            if unknown:
                # dnf refuses the whole transaction rather than ignoring the flag.
                return CmdResult(argv, 1, "", "Error: Unknown repo: %r" % unknown[0])
            unavailable = [n for n in names if n not in self.available]
            if unavailable and self.atomic:
                return CmdResult(argv, 1, "", "No match for argument")
            for n in names:
                if n in self.available:
                    self.installed.add(n)
            return CmdResult(argv, 1 if unavailable else 0, "", "")
        if argv[:2] == ["dnf", "repolist"]:
            body = "repo id  repo name\n" + "".join(
                "%s  %s\n" % (r, r) for r in sorted(self.enabled_repos)
            )
            return CmdResult(argv, 0, body, "")
        if argv[:2] == ["dnf", "config-manager"]:
            for token in argv[2:]:
                if token.startswith("--set-enabled"):
                    continue
                name = token.split(".")[0].replace("--set-enabled", "").strip()
                if name and not name.startswith("-") and name != "setopt":
                    self.enabled_repos.add(name)
            return CmdResult(argv, 0, "", "")
        return CmdResult(argv, 0, "", "")


@pytest.fixture
def fake(monkeypatch):
    def _make(version_id="9.6", **kw):
        f = FakeDnf(**kw)
        monkeypatch.setattr(pkg_mod.procutil, "run_cmd", f.run_cmd)
        monkeypatch.setattr(
            pkg_mod.procutil, "read_file",
            lambda path, default=None: 'VERSION_ID="%s"' % version_id
            if path == "/etc/os-release" else default,
        )
        return f
    return _make


def test_one_unavailable_package_does_not_block_the_rest(fake):
    """The gcc/make regression: an unavailable tool must not starve the
    packages that are perfectly installable."""
    f = fake(available={"gcc", "make", "fio"})
    manager = PackageManager()

    still_missing = manager.missing(["gcc", "make", "fio", "not-in-any-repo"])

    assert still_missing == ["not-in-any-repo"]
    assert {"gcc", "make", "fio"} <= f.installed
    installed_names = {e["name"] for e in manager.newly_installed}
    assert {"gcc", "make", "fio"} <= installed_names


def test_batch_is_attempted_before_individual_installs(fake):
    """Batching is the fast path; per-package retry is only the fallback."""
    f = fake(available={"gcc", "make"})
    PackageManager().missing(["gcc", "make"])
    assert f.install_calls[0] == ["gcc", "make"]
    assert len(f.install_calls) == 1  # no retries needed


def test_individual_retry_only_happens_when_the_batch_fails(fake):
    f = fake(available={"gcc"})
    PackageManager().missing(["gcc", "bogus"])
    assert f.install_calls[0] == ["gcc", "bogus"]   # batch attempt
    assert ["gcc"] in f.install_calls[1:]           # then one at a time
    assert ["bogus"] in f.install_calls[1:]


def test_already_installed_packages_are_not_reinstalled(fake):
    f = fake(available={"gcc"}, installed={"gcc"})
    assert PackageManager().missing(["gcc"]) == []
    assert f.install_calls == []


def test_epel_and_crb_are_enabled_automatically(fake):
    f = fake(available={"epel-release", "sysbench"})
    manager = PackageManager()

    outcome = manager.enable_extra_repos()

    assert outcome["epel"] == "installed"
    assert outcome["crb"] == "crb"
    assert "epel-release" in f.installed
    assert "crb" in f.enabled_repos
    assert set(manager.enabled_repos) == {"crb", "epel"}


def test_repo_setup_is_idempotent(fake):
    f = fake(available={"epel-release"}, installed={"epel-release"})
    manager = PackageManager()
    first = manager.enable_extra_repos()
    before = len(f.install_calls)
    second = manager.enable_extra_repos()
    assert first["epel"] == "already installed"
    assert second["epel"] == "already-prepared"
    assert len(f.install_calls) == before  # no repeated work


def test_missing_epel_release_is_reported_not_raised(fake):
    """An offline machine should still run whatever tools it already has."""
    f = fake(available=set())
    outcome = PackageManager().enable_extra_repos()
    assert outcome["epel"] == "failed"
    assert "epel-release" not in f.installed


@pytest.mark.parametrize(
    "major,expected",
    [(8, "powertools"), (9, "crb"), (10, "crb"), (None, "crb")],
)
def test_crb_repo_name_per_release(major, expected):
    """CodeReady Builder is named powertools on AlmaLinux 8."""
    assert crb_repo_name(major) == expected


def test_os_major_parsed_from_os_release(fake):
    fake(available=set())
    assert PackageManager().os_major() == 9


def test_no_epel_is_enforced_in_the_manager(fake):
    """--no-epel must hold even when a test declares an EPEL dependency, so
    no code path can add a third-party repo behind the operator's back."""
    f = fake(available={"epel-release", "sysbench"})
    manager = PackageManager(allow_extra_repos=False)

    outcome = manager.enable_extra_repos()
    assert "disabled" in outcome["epel"]

    # a test declaring repos=("epel",) still must not trigger the install
    manager.missing(["sysbench"], repos=("epel",))
    assert "epel-release" not in f.installed
    assert manager.enabled_repos == []


# --- naming the repository dnf actually has -------------------------------------------
#
# Reported as ``ocl-icd-devel`` and ``opencl-headers`` being missing on AlmaLinux 8. Neither is:
# both are in PowerTools, verified against the real repositories in an ``almalinux:8`` container,
# and this code had already enabled PowerTools one step earlier. What refused it was the flag
# naming it, because a test asks for ``crb`` and el8 has no such repository. Confirmed against real
# dnf: ``dnf install --enablerepo=crb opencl-headers`` prints ``Error: Unknown repo: 'crb'`` and
# installs nothing, while ``--enablerepo=powertools`` installs both.
#
# It was not only OpenCL. Every caller naming a repository was affected on el8, which includes the
# clpeak build dependencies, so the GPU benchmark could not build there either.


def _flags_for(fake_dnf, package):
    """The ``--enablerepo`` ids of the install that carried ``package``.

    By package rather than by position, because ``enable_extra_repos`` installs ``epel-release``
    first and that call names no repository - so index 0 is always empty and an assertion on it
    passes whatever the real flag was.
    """
    for names, flags in zip(fake_dnf.install_calls, fake_dnf.enable_flags):
        if package in names:
            return flags
    raise AssertionError("no install call carried %r" % package)


@pytest.mark.parametrize("version_id,expected", [
    ("8.10", "powertools"),
    ("9.6", "crb"),
    ("10.2", "crb"),
])
def test_the_enablerepo_flag_uses_this_releases_repo_id(fake, version_id, expected):
    f = fake(version_id=version_id, available={"ocl-icd-devel", "opencl-headers"},
             known_repos={"baseos", "appstream", expected})
    manager = PackageManager(assume_yes=True)

    assert manager.ensure(("opencl-headers", "ocl-icd-devel"), repos=("crb",)) is True
    assert _flags_for(f, "ocl-icd-devel") == [expected]


def test_the_el8_install_actually_lands(fake):
    """The end the reporter saw. Before the translation this returned False with both packages
    still absent, and the OpenCL validation skipped itself with "could not install what the OpenCL
    probe needs to build"."""
    f = fake(version_id="8.10", available={"ocl-icd-devel", "opencl-headers"},
             known_repos={"baseos", "appstream", "powertools"})
    manager = PackageManager(assume_yes=True)

    installed = manager.ensure(("opencl-headers", "ocl-icd-devel"), repos=("crb",))

    assert installed is True
    assert f.installed >= {"opencl-headers", "ocl-icd-devel"}


def test_epel_is_never_named_to_enablerepo(fake):
    """It is installed as ``epel-release`` rather than enabled, so naming it would be the same
    fatal unknown-repo error by another route."""
    f = fake(available={"stress-ng"}, known_repos={"baseos", "appstream", "crb"})
    manager = PackageManager(assume_yes=True)

    manager.ensure(("stress-ng",), repos=("epel", "crb"))

    assert _flags_for(f, "stress-ng") == ["crb"]


def test_the_report_names_a_repository_that_exists(fake):
    """``installed_packages[].repo`` is part of the comparability context in docs/schema.md, and an
    el8 report said the package came from "crb"."""
    fake(version_id="8.10", available={"ocl-icd-devel"},
         known_repos={"baseos", "appstream", "powertools"})
    manager = PackageManager(assume_yes=True)

    manager.ensure(("ocl-icd-devel",), repos=("crb",))

    assert [entry["repo"] for entry in manager.newly_installed] == ["powertools"]


def test_an_unknown_repo_is_fatal_in_the_fake_too(fake):
    """The harness property this rests on. Without it every assertion above passes against the bug:
    the fake dropped any argument starting with "-" and installed the packages anyway."""
    f = fake(available={"opencl-headers"}, known_repos={"baseos", "appstream"})
    manager = PackageManager(assume_yes=True)

    assert manager.ensure(("opencl-headers",), repos=("crb",)) is False
    assert f.installed == set()


def test_every_recorded_package_has_the_keys_the_schema_documents(fake):
    """``docs/schema.md`` documents ``installed_packages`` entries as ``{name, version, repo}``, and
    this list is where they come from. The ``epel-release`` entry carried only two of the three, so
    every report from a run that installed EPEL had one malformed row in the list a reader uses to
    tell two runs apart. Asserted over the whole list rather than on one entry, because the defect
    was one writer of it disagreeing with the other."""
    fake(available={"epel-release", "stress-ng"}, known_repos={"baseos", "appstream", "crb"})
    manager = PackageManager(assume_yes=True)

    manager.ensure(("stress-ng",), repos=("epel", "crb"))

    assert manager.newly_installed, "nothing was recorded, so this proves nothing"
    for entry in manager.newly_installed:
        assert set(entry) == {"name", "version", "repo"}, entry
        assert entry["repo"], entry
