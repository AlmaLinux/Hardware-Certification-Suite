"""Report assembly, canonical hashing, verdict, and bundle round-trip."""

import hashlib
import json
import os
import subprocess
import tarfile

import pytest

from alma_certify import bundle, report
from alma_certify.state import RunState


def test_power_profile_field_comes_from_the_power_profiles_daemon(monkeypatch):
    """Recorded beside the governor so the bundle is coherent on an active-mode pstate host, where
    performance shows as a profile while the governor may still read powersave."""
    from alma_certify import cpugov

    monkeypatch.setattr(cpugov, "_ppd_get", lambda: "performance")
    assert report._power_profile() == "performance"
    monkeypatch.setattr(cpugov, "_ppd_get", lambda: None)
    assert report._power_profile() is None


@pytest.fixture
def run_state(tmp_path):
    state = RunState.create(
        str(tmp_path), "11111111-2222-3333-4444-555555555555",
        {
            "run_types": ["collect", "validate"],
            "target_type": "hardware",
            "hostname": "sut.example",
            "started_at": "2026-07-27T10:00:00Z",
            "finished_at": "2026-07-27T10:30:00Z",
            "pre_release": True,
            "publish_after": "2026-09-01",
            "interactive_included": False,
        },
    )
    # a raw inventory artifact so the manifest has content
    with open(state.inventory_path("dmidecode.txt"), "w") as fh:
        fh.write("BIOS Information\n")
    return state


INVENTORY = {"summary": {"system": {"vendor": "ACME"}}, "raw": {}}


def _results(status="pass", severity="required"):
    return [
        {
            "id": "validate.cpu.functional", "run_type": "validate",
            "category": "cpu", "severity": severity, "status": status,
            "reason": None, "started_at": None, "duration_s": 1.0,
            "metrics": [], "details": {}, "artifacts": [],
        }
    ]


def test_assemble_and_self_hash_round_trip(run_state):
    env = {"os": {"id": "almalinux"}}
    rep = report.assemble(run_state, INVENTORY, _results(), env)
    assert rep["schema_version"] == "1.2"
    assert rep["run"]["run_id"] == run_state.run_id
    assert rep["run"]["pre_release"] is True
    assert rep["run"]["publish_after"] == "2026-09-01"
    assert rep["run"]["target_type"] == "hardware"

    path = report.write(run_state, rep)
    loaded = json.load(open(path))
    assert (
        report.compute_report_hash(loaded)
        == loaded["integrity"]["report_sha256"]
    )


def test_manifest_covers_inventory_files(run_state):
    rep = report.assemble(run_state, INVENTORY, [], {})
    paths = [e["path"] for e in rep["artifact_manifest"]]
    assert "inventory/dmidecode.txt" in paths
    entry = rep["artifact_manifest"][paths.index("inventory/dmidecode.txt")]
    assert entry["size"] == len("BIOS Information\n")
    assert len(entry["sha256"]) == 64


@pytest.mark.parametrize(
    "status,severity,expected",
    [
        ("pass", "required", True),
        ("fail", "required", False),
        ("error", "conditional", False),
        ("fail", "informational", True),   # informational never gates
        ("skip", "conditional", True),     # skip never gates
    ],
)
def test_verdict(run_state, status, severity, expected):
    rep = report.assemble(run_state, INVENTORY, _results(status, severity), {})
    assert report.verdict(rep) is expected


def test_verdict_none_without_validate_results(run_state):
    rep = report.assemble(run_state, INVENTORY, [], {})
    assert report.verdict(rep) is None


def _open_bundle(path: str, tmp_path) -> tarfile.TarFile:
    """Decompress a .tar.zst via the zstd CLI (mirrors what a consumer on
    an older Python would do) and open the tar."""
    tar_path = str(tmp_path / "bundle.tar")
    subprocess.run(["zstd", "-d", "-q", "-f", path, "-o", tar_path], check=True)
    return tarfile.open(tar_path)


def test_bundle_round_trip(run_state, tmp_path):
    rep = report.assemble(run_state, INVENTORY, _results(), {})
    report.write(run_state, rep)
    out = bundle.bundle_run(run_state.run_dir, str(tmp_path / "b.tar.zst"))

    with _open_bundle(out, tmp_path) as tar:
        names = tar.getnames()
        assert "report.json" in names
        assert "SHA256SUMS" in names
        assert "inventory/dmidecode.txt" in names
        # all members are plain relative files
        for member in tar.getmembers():
            assert member.isfile()
            assert not member.name.startswith("/")
            assert ".." not in member.name


def test_bundle_is_deterministic(run_state, tmp_path):
    """Re-bundling an unchanged run must be byte-identical, so a resubmission
    is recognized as a duplicate instead of a run-id conflict."""
    rep = report.assemble(run_state, INVENTORY, _results(), {})
    report.write(run_state, rep)

    def digest(path):
        return hashlib.sha256(open(path, "rb").read()).hexdigest()

    first = bundle.bundle_run(run_state.run_dir, str(tmp_path / "a.tar.zst"))
    second = bundle.bundle_run(run_state.run_dir, str(tmp_path / "b.tar.zst"))
    assert digest(first) == digest(second)


def test_bundle_requires_report(tmp_path):
    os.makedirs(tmp_path / "empty" / "inventory")
    with pytest.raises(FileNotFoundError):
        bundle.bundle_run(str(tmp_path / "empty"))
