"""report.json assembly: manifest, canonical self-hash, environment capture.

The schema is documented in docs/schema.md and versioned via
``alma_certify.SCHEMA_VERSION``.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
from typing import Any, Dict, List, Optional

from . import SCHEMA_VERSION, __version__, gpusetup, hostos, procutil, virt


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def canonical_dumps(report: Dict[str, Any]) -> str:
    return json.dumps(report, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_report_hash(report: Dict[str, Any]) -> str:
    """Self-hash over the canonical form with the hash field nulled."""
    clone = json.loads(json.dumps(report))
    clone.setdefault("integrity", {})["report_sha256"] = None
    return hashlib.sha256(canonical_dumps(clone).encode("utf-8")).hexdigest()


def build_manifest(run_dir: str) -> List[Dict[str, Any]]:
    entries = []
    for sub in ("inventory", "artifacts"):
        base = os.path.join(run_dir, sub)
        for path in sorted(glob.glob(os.path.join(base, "**"), recursive=True)):
            if not os.path.isfile(path):
                continue
            rel = os.path.relpath(path, run_dir)
            entries.append(
                {"path": rel, "sha256": sha256_file(path), "size": os.path.getsize(path)}
            )
    return entries


# The x86-64 microarchitecture levels, by the flags /proc/cpuinfo names for each. A
# level's set is cumulative; sse3 (reported as "pni") and lzcnt ("abm") are omitted
# because the remaining required flags discriminate a level without their naming
# ambiguity. Reported for the hardware survey: the field level of the CPUs in the
# fleet is direct input to AlmaLinux's baseline-ISA decisions.
_X86_64_V2 = {"cx16", "lahf_lm", "popcnt", "sse4_1", "sse4_2", "ssse3"}
_X86_64_V3 = _X86_64_V2 | {"avx", "avx2", "bmi1", "bmi2", "f16c", "fma", "movbe"}
_X86_64_V4 = _X86_64_V3 | {"avx512f", "avx512bw", "avx512cd", "avx512dq", "avx512vl"}


def _x86_64_level() -> str:
    """The microarchitecture level the CPU supports (v2/v3/v4), or "" off x86_64."""
    if os.uname().machine != "x86_64":
        return ""
    flags: set = set()
    for line in (procutil.read_file("/proc/cpuinfo", "") or "").splitlines():
        if line.startswith("flags"):
            flags = set(line.split(":", 1)[1].split())
            break
    if not flags:
        return ""
    if _X86_64_V4 <= flags:
        return "v4"
    if _X86_64_V3 <= flags:
        return "v3"
    if _X86_64_V2 <= flags:
        return "v2"
    return "v1"


def capture_environment(
    installed_packages: List[Dict[str, str]],
    enabled_repos: Optional[List[str]] = None,
) -> Dict[str, Any]:
    os_release = _parse_os_release()
    uname = procutil.run_cmd(["uname", "-rm"], timeout=10)
    kernel, _, arch = (uname.stdout.strip().partition(" ") if uname.ok else ("", "", ""))

    env: Dict[str, Any] = {
        "os": {
            "id": os_release.get("ID", "unknown"),
            "version_id": os_release.get("VERSION_ID", "unknown"),
            # Verbatim, because it is the only field that distinguishes AlmaLinux Kitten
            # from the stable release of the same major: Kitten reports ID="almalinux"
            # and VERSION_ID="10" exactly as AlmaLinux 10 does, and says "Kitten" only
            # here. The catalog gates hardware proved on Kitten until the minor its
            # enablement lands in has shipped, and could not tell the two apart at all
            # before this field was recorded.
            #
            # The string, not a flag we computed. How to read it is a judgement that may
            # need revising - Kitten's naming is not ours to control - and a stored
            # boolean freezes today's guess into every bundle ever submitted.
            "pretty_name": os_release.get("PRETTY_NAME", ""),
            "kernel": kernel or "unknown",
            "arch": arch.strip() or os.uname().machine,
            # What the CPU actually supports, distinct from the arch it was built for.
            "x86_64_level": _x86_64_level(),
        },
        "selinux": _selinux_state(),
        "secure_boot": _secure_boot_state(),
        "kernel_taint": int(procutil.read_file("/proc/sys/kernel/tainted", "0") or 0),
        "tuned_profile": _tuned_profile(),
        # The power-profiles-daemon profile (power-profiles-daemon on 9, tuned's ppd shim on 10).
        # Recorded beside the governor because on an active-mode pstate driver it, not the governor
        # string, is what decides clock: a benchmark can run at full speed with the governor still
        # reading "powersave" but the profile on "performance". cpugov forces this for the run.
        "power_profile": _power_profile(),
        "cpu_governor": procutil.read_file(
            "/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor"
        ),
        "smt": _smt_active(),
        "mitigations": _cpu_mitigations(),
        "installed_packages": installed_packages,
        # Which extra repos were active decides which tool versions were
        # available, so it is part of a result's comparability context.
        "enabled_repos": sorted(enabled_repos or []),
    }
    # Only where there is something to say, which is a machine with an NVIDIA card. Absent on every
    # other machine, so nothing changes in the reports this cannot apply to.
    #
    # Additive within schema 1.1 rather than a new version. Lumina's allowlist accepts 1.0 and 1.1
    # and validates the sections it needs by presence, so an extra key inside ``environment`` is
    # ingested by servers that predate it and simply not shown. Bumping instead would have every
    # existing deployment reject every bundle from an updated suite, to gain nothing.
    driver = gpusetup.load_record()
    if driver is not None:
        env["nvidia_driver"] = driver
    # Raw signals, no verdict: lumina derives its own answer from the same facts. Additive within
    # the schema for the same reason nvidia_driver is.
    env["virtualization"] = virt.detect()
    return env


def assemble(
    state: Any,  # RunState
    inventory: Dict[str, Any],
    results: List[Dict[str, Any]],
    environment: Dict[str, Any],
) -> Dict[str, Any]:
    meta = state.meta
    report: Dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run": {
            "run_id": state.run_id,
            "suite_version": __version__,
            "suite_git_commit": _git_commit(),
            "run_types": meta.get("run_types", []),
            "target_type": meta.get("target_type", "hardware"),
            # What this run is a claim *about*, as opposed to which tests it happened to run.
            #
            # This was missing, and it was the whole feature quietly not working. ``--scope gpu``
            # recorded the claim in the run's journal, narrowed the tests, printed it in the
            # listing, and documented itself in docs/schema.md; lumina's ingest read
            # ``run.claim_scope`` and validated it. The one place that had to carry it between them
            # did not, so every scoped run reached the server as a whole-machine claim and its
            # review page asked for the machine's details. Reported from run 3c2a5873, submitted
            # with --scope gpu and stored with an empty scope.
            "claim_scope": list(meta.get("claim_scope") or []),
            "hostname": meta.get("hostname", os.uname().nodename),
            "started_at": meta.get("started_at"),
            "finished_at": meta.get("finished_at"),
            "pre_release": bool(meta.get("pre_release", False)),
            "publish_after": meta.get("publish_after"),
            "interactive_included": bool(meta.get("interactive_included", False)),
            # Whether the machine was stopped from suspending. False on a host with no logind, and a
            # condition the results were produced under either way.
            "sleep_inhibited": bool(meta.get("sleep_inhibited", False)),
            # Whether the CPU governor was forced to performance for the benchmark. False for a
            # validate-only run, and on a host with no cpufreq to set. Part of the conditions behind
            # any benchmark number.
            "cpu_governor_forced": bool(meta.get("cpu_governor_forced", False)),
            "resumed": bool(state.data.get("resumed", False)),
        },
        "environment": environment,
        "inventory": inventory,
        "results": results,
        "artifact_manifest": build_manifest(state.run_dir),
        "integrity": {"report_sha256": None},
    }
    report["integrity"]["report_sha256"] = compute_report_hash(report)
    return report


def write(state: Any, report: Dict[str, Any]) -> str:
    path = os.path.join(state.run_dir, "report.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return path


def verdict(report: Dict[str, Any]) -> Optional[bool]:
    """Certification verdict for a validate run; None if no validate results."""
    validate = [r for r in report["results"] if r["run_type"] == "validate"]
    if not validate:
        return None
    for r in validate:
        if r.get("severity") == "informational":
            continue
        if r["status"] in ("fail", "error"):
            return False
    return True


# -- environment helpers ------------------------------------------------------

def _parse_os_release() -> Dict[str, str]:
    """Delegates to ``hostos``, which is the one parser that handles both quote styles.

    This used to strip only double quotes, and it is the copy whose output becomes
    ``environment.os.id`` - the field the submit gate and Lumina both read. On a
    spec-legal ``ID='almalinux'`` the run therefore started fine (``hostos.detect``
    strips single quotes) and then recorded ``"'almalinux'"``, so a genuine AlmaLinux
    run was refused at upload and would have been quarantined server-side.
    """
    return hostos.parse_os_release(procutil.read_file("/etc/os-release", "") or "")


def _selinux_state() -> str:
    try:
        res = procutil.run_cmd(["getenforce"], timeout=10)
        return res.stdout.strip().lower() if res.ok else "unknown"
    except procutil.CommandNotFound:
        return "unknown"


def _secure_boot_state() -> str:
    try:
        res = procutil.run_cmd(["mokutil", "--sb-state"], timeout=10)
        if res.ok:
            return "enabled" if "enabled" in res.stdout.lower() else "disabled"
    except procutil.CommandNotFound:
        pass
    if not os.path.isdir("/sys/firmware/efi"):
        return "disabled"
    return "unknown"


def _tuned_profile() -> Optional[str]:
    return procutil.read_file("/etc/tuned/active_profile")


def _power_profile() -> Optional[str]:
    """The active power-profiles-daemon profile, or None where no such daemon answers."""
    from . import cpugov

    return cpugov._ppd_get()


def _smt_active() -> Optional[bool]:
    val = procutil.read_file("/sys/devices/system/cpu/smt/active")
    if val is None:
        return None
    return val == "1"


def _cpu_mitigations() -> Dict[str, str]:
    out: Dict[str, str] = {}
    base = "/sys/devices/system/cpu/vulnerabilities"
    if os.path.isdir(base):
        for name in sorted(os.listdir(base)):
            val = procutil.read_file(os.path.join(base, name))
            if val is not None:
                out[name] = val
    return out


def _git_commit() -> Optional[str]:
    # Baked in at RPM build time; falls back to git for checkout installs.
    here = os.path.dirname(os.path.abspath(__file__))
    buildinfo = os.path.join(here, "data", "BUILDINFO")
    commit = procutil.read_file(buildinfo)
    if commit:
        return commit
    try:
        res = procutil.run_cmd(
            ["git", "-C", os.path.dirname(here), "rev-parse", "--short", "HEAD"],
            timeout=10,
        )
        return res.stdout.strip() if res.ok else None
    except procutil.CommandNotFound:
        return None
