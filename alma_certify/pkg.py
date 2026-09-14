"""dnf package handling.

Packages needed by selected tests are installed up front. We record exactly
what *we* installed (for the report, and for optional cleanup) and never
remove anything that was already present.

Most benchmark tools (sysbench, stress-ng, clpeak) live in
EPEL, and a good number of EPEL packages depend on CodeReady Builder, so
``enable_extra_repos`` sets both up before any install is attempted. It is
skipped with ``--no-epel`` for labs that forbid third-party repositories.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List, Optional, Set

from . import hostos, procutil

EPEL_RELEASE = "epel-release"


def crb_repo_name(os_major: Optional[int]) -> str:
    """CodeReady Builder is called ``powertools`` on el8 and ``crb`` after."""
    return "powertools" if os_major == 8 else "crb"


class PackageManager:
    def __init__(self, assume_yes: bool = True, allow_extra_repos: bool = True):
        self.newly_installed: List[Dict[str, str]] = []
        self.enabled_repos: List[str] = []
        self._assume_yes = assume_yes
        # False for --no-epel. Enforced here rather than at the call site so
        # no code path can quietly add a third-party repo behind the operator.
        self._allow_extra_repos = allow_extra_repos
        self._repos_prepared = False

    def _rpm_installed(self, name: str) -> bool:
        res = procutil.run_cmd(["rpm", "-q", name], timeout=30)
        return res.returncode == 0

    def _nevra(self, name: str) -> Dict[str, str]:
        res = procutil.run_cmd(
            ["rpm", "-q", "--qf", "%{NAME} %{VERSION}-%{RELEASE}", name], timeout=30
        )
        if res.ok:
            parts = res.stdout.strip().split(None, 1)
            if len(parts) == 2:
                return {"name": parts[0], "version": parts[1]}
        return {"name": name, "version": "unknown"}

    # -- repository setup ---------------------------------------------------
    def os_major(self) -> Optional[int]:
        """The major from ``VERSION_ID``, or None.

        Parsed by ``hostos`` rather than here. Stripping only double quotes made
        ``VERSION_ID='8.10'`` return None, and None picks the wrong CodeReady Builder
        repository name - ``crb`` instead of el8's ``powertools`` - so package setup
        would fail on exactly the release that needs the older name.
        """
        fields = hostos.parse_os_release(
            procutil.read_file("/etc/os-release", "") or ""
        )
        try:
            return int(fields.get("VERSION_ID", "").split(".")[0])
        except ValueError:
            return None

    def _enabled_repo_ids(self) -> Set[str]:
        res = procutil.run_cmd(["dnf", "repolist", "--enabled"], timeout=120)
        ids = set()
        for line in res.stdout.splitlines()[1:]:
            token = line.split()
            if token:
                ids.add(token[0].lower())
        return ids

    def _set_repo_enabled(self, repo: str, timeout: float) -> bool:
        """Enable a repo across dnf4 and dnf5, whose syntaxes differ."""
        if repo.lower() in self._enabled_repo_ids():
            return True
        attempts = [
            ["dnf", "config-manager", "--set-enabled", repo],   # dnf4
            ["dnf", "config-manager", "setopt", "%s.enabled=1" % repo],  # dnf5
        ]
        for argv in attempts:
            res = procutil.run_cmd(argv, timeout=timeout)
            if res.ok and repo.lower() in self._enabled_repo_ids():
                return True
            if "config-manager" in (res.stderr + res.stdout) and "not" in res.stderr:
                # plugin absent on minimal el8 installs
                procutil.run_cmd(
                    ["dnf", "install", "-y", "dnf-plugins-core"], timeout=timeout
                )
        return repo.lower() in self._enabled_repo_ids()

    def enable_extra_repos(
        self, log: Optional[Callable[[str], None]] = None, timeout: float = 900
    ) -> Dict[str, Any]:
        """Install epel-release and enable CodeReady Builder.

        Idempotent and safe to call when they are already set up. Failures are
        reported rather than raised: an offline machine should still run the
        tests whose tools are already present.
        """
        log = log or (lambda msg: None)
        if not self._allow_extra_repos:
            return {"epel": "disabled by --no-epel", "crb": "disabled by --no-epel"}
        if self._repos_prepared:
            return {"epel": "already-prepared", "crb": "already-prepared"}
        self._repos_prepared = True

        outcome: Dict[str, Any] = {}
        major = self.os_major()

        # CRB first: some EPEL packages need it at install time.
        crb = crb_repo_name(major)
        if self._set_repo_enabled(crb, timeout):
            outcome["crb"] = crb
            self.enabled_repos.append(crb)
            log("enabled %s repository" % crb)
        else:
            outcome["crb"] = "failed: %s could not be enabled" % crb
            log("warning: could not enable %s; some EPEL packages may be "
                "uninstallable" % crb)

        if self._rpm_installed(EPEL_RELEASE):
            outcome["epel"] = "already installed"
        else:
            log("installing %s" % EPEL_RELEASE)
            res = procutil.run_cmd(
                ["dnf", "install", "-y", EPEL_RELEASE], timeout=timeout
            )
            if res.ok and self._rpm_installed(EPEL_RELEASE):
                # ``repo`` as well as name and version. ``docs/schema.md`` documents every
                # ``installed_packages`` entry as ``{name, version, repo}``, and this one carried
                # two keys, so a report from any run that installed EPEL had one malformed entry in
                # a list a reader compares runs with. "base" is what ``missing`` records when no
                # repository was named, which is exactly the case here: the command above enables
                # nothing and takes ``epel-release`` from whatever is already configured.
                entry = self._nevra(EPEL_RELEASE)
                entry["repo"] = "base"
                self.newly_installed.append(entry)
                outcome["epel"] = "installed"
            else:
                outcome["epel"] = "failed"
                log("warning: could not install %s; benchmark tools from EPEL "
                    "will be skipped" % EPEL_RELEASE)
        if outcome.get("epel") in ("installed", "already installed"):
            self.enabled_repos.append("epel")
        return outcome

    # -- package installation ------------------------------------------------
    def _repo_id(self, name: str) -> str:
        """The id dnf knows, for a repository a caller named by role.

        Callers ask for ``crb`` because that is what CodeReady Builder is called on 9 and 10, and
        on 8 it is ``powertools``. ``enable_extra_repos`` has always translated it;
        ``--enablerepo`` did not, so on AlmaLinux 8 every install naming a repo ran ``dnf install
        --enablerepo=crb`` and got ``Error: Unknown repo: 'crb'``. dnf treats that as fatal, so
        nothing in the transaction landed, and the per-package retry repeated the same flag.

        Reported as ``ocl-icd-devel`` and ``opencl-headers`` missing on AlmaLinux 8. Both are in
        PowerTools, and this code had already enabled it one step earlier: the packages were
        reachable and the flag naming the repository is what refused the transaction. It took the
        clpeak build dependencies down with it, since those are requested the same way.
        """
        if name.lower() == "crb":
            return crb_repo_name(self.os_major())
        return name

    def _repo_ids(self, repos: Set[str]) -> List[str]:
        """The ids to pass to ``--enablerepo``, translated and sorted.

        EPEL is excluded because it is installed as ``epel-release`` rather than enabled, so naming
        it here would be the same fatal unknown-repo error by another route.
        """
        return sorted({self._repo_id(repo) for repo in repos - {"epel"}})

    def _dnf_install(
        self, packages: List[str], repos: Set[str], timeout: float
    ) -> None:
        argv = ["dnf", "install"]
        if self._assume_yes:
            argv.append("-y")
        for repo in self._repo_ids(repos):
            argv.append("--enablerepo=%s" % repo)
        argv += packages
        procutil.run_cmd(argv, timeout=timeout)

    def missing(
        self,
        packages: Iterable[str],
        repos: Iterable[str] = (),
        timeout: float = 900,
    ) -> List[str]:
        """Install what is absent; return the packages still missing after.

        Returning the names (rather than a bare bool) lets callers say
        *which* package was unavailable, so a failed run is self-diagnosing
        instead of just "packages could not be installed".
        """
        absent = [p for p in packages if not self._rpm_installed(p)]
        if not absent:
            return []

        repos = set(repos)
        if repos:
            # Safety net for direct API callers; a no-op once the run has
            # already prepared repos, or when --no-epel forbids it.
            self.enable_extra_repos(timeout=timeout)

        # Batch first because it is much faster. dnf installs a transaction
        # atomically, so one unavailable package would otherwise block every
        # other package in the batch - which previously made staples like gcc
        # look "unavailable" because an unrelated name failed to resolve.
        self._dnf_install(absent, repos, timeout)
        still_missing = [p for p in absent if not self._rpm_installed(p)]
        if still_missing and len(absent) > 1:
            for name in still_missing:
                self._dnf_install([name], repos, timeout)

        # Trust rpm, not dnf's exit status, for what actually landed.
        for name in absent:
            if self._rpm_installed(name):
                entry = self._nevra(name)
                # The real ids, so ``installed_packages[].repo`` in the report names a repository
                # that exists on the machine the run happened on. "crb" on an el8 report named one
                # that does not.
                entry["repo"] = ",".join(self._repo_ids(repos)) if repos else "base"
                self.newly_installed.append(entry)
        return [p for p in absent if not self._rpm_installed(p)]

    def ensure(
        self,
        packages: Iterable[str],
        repos: Iterable[str] = (),
        timeout: float = 900,
    ) -> bool:
        """Install any missing packages. Returns True if all are present."""
        return not self.missing(packages, repos=repos, timeout=timeout)

    def cleanup(self) -> None:
        """Remove only what this run newly installed."""
        names: Set[str] = {p["name"] for p in self.newly_installed}
        if not names:
            return
        procutil.run_cmd(["dnf", "remove", "-y"] + sorted(names), timeout=900)
