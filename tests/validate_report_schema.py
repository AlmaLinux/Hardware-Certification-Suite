#!/usr/bin/env python3
"""Standalone schema check for report.json (used by the smoke workflow).

Usage: validate_report_schema.py <run-dir-base>

Validates every report.json under the base directory against the contract in
docs/schema.md, including manifest checksums. Exits non-zero on any problem.
"""

from __future__ import annotations

import glob
import hashlib
import json
import os
import sys

REQUIRED_RUN_KEYS = {
    "run_id", "suite_version", "run_types", "target_type", "hostname",
    "started_at", "finished_at", "pre_release", "publish_after",
    "interactive_included", "resumed",
}
VALID_STATUS = {"pass", "fail", "skip", "error"}
VALID_DIRECTION = {"higher_is_better", "lower_is_better", "info"}
VALID_SEVERITY = {"required", "conditional", "informational", None}


def check(report_path: str) -> list:
    problems = []
    run_dir = os.path.dirname(report_path)
    with open(report_path, encoding="utf-8") as fh:
        rep = json.load(fh)

    def err(msg):
        problems.append("%s: %s" % (report_path, msg))

    if rep.get("schema_version") not in ("1.0", "1.1"):
        err("unexpected schema_version %r" % rep.get("schema_version"))
    for key in ("run", "environment", "inventory", "results",
                "artifact_manifest", "integrity"):
        if key not in rep:
            err("missing top-level key %r" % key)
    if problems:
        return problems

    missing = REQUIRED_RUN_KEYS - set(rep["run"])
    if missing:
        err("run is missing keys: %s" % ", ".join(sorted(missing)))
    if rep["run"]["target_type"] not in ("hardware", "cloud_instance"):
        err("invalid target_type %r" % rep["run"]["target_type"])

    summary = rep["inventory"].get("summary", {})
    for key in ("system", "cpus", "memory", "disks", "nics", "gpus", "drivers"):
        if key not in summary:
            err("inventory.summary missing %r" % key)
    if rep["schema_version"] != "1.0":
        if "baseboard" not in summary:
            err("inventory.summary missing 'baseboard'")
        if summary.get("system", {}).get("kind") not in ("prebuilt", "custom", "unknown"):
            err("system.kind is %r" % summary.get("system", {}).get("kind"))
    for gpu in summary.get("gpus", []):
        if gpu.get("driver") and not gpu.get("driver_version"):
            err("GPU %r has a driver but no driver_version" % gpu.get("model"))

    for result in rep["results"]:
        rid = result.get("id", "?")
        if result.get("status") not in VALID_STATUS:
            err("%s: invalid status %r" % (rid, result.get("status")))
        if result.get("severity") not in VALID_SEVERITY:
            err("%s: invalid severity %r" % (rid, result.get("severity")))
        for metric in result.get("metrics", []):
            if metric.get("direction") not in VALID_DIRECTION:
                err("%s: invalid metric direction %r" % (rid, metric.get("direction")))
            if not isinstance(metric.get("value"), (int, float)):
                err("%s: metric %r value is not numeric" % (rid, metric.get("name")))
        primaries = [m for m in result.get("metrics", []) if m.get("primary")]
        if len(primaries) > 1:
            err("%s: more than one primary metric" % rid)
        if result["run_type"] == "benchmark" and result["status"] == "pass":
            if not result.get("details", {}).get("benchmark_version"):
                err("%s: benchmark result has no details.benchmark_version" % rid)
            if result["category"] == "gpu" and not result["details"].get("driver_info"):
                err("%s: GPU benchmark result has no driver_info" % rid)

    # manifest integrity
    listed = set()
    for entry in rep["artifact_manifest"]:
        path = os.path.join(run_dir, entry["path"])
        listed.add(entry["path"])
        if not os.path.isfile(path):
            err("manifest references missing file %s" % entry["path"])
            continue
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if digest != entry["sha256"]:
            err("checksum mismatch for %s" % entry["path"])
        if os.path.getsize(path) != entry["size"]:
            err("size mismatch for %s" % entry["path"])

    on_disk = set()
    for sub in ("inventory", "artifacts"):
        for path in glob.glob(os.path.join(run_dir, sub, "**"), recursive=True):
            if os.path.isfile(path):
                on_disk.add(os.path.relpath(path, run_dir))
    unlisted = on_disk - listed
    if unlisted:
        err("files not in the manifest: %s" % ", ".join(sorted(unlisted)[:5]))

    # self-hash
    clone = json.loads(json.dumps(rep))
    clone["integrity"]["report_sha256"] = None
    canonical = json.dumps(clone, sort_keys=True, separators=(",", ":"),
                           ensure_ascii=False)
    expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if expected != rep["integrity"]["report_sha256"]:
        err("integrity.report_sha256 does not match the canonical report")

    return problems


def main(argv):
    if len(argv) != 2:
        print(__doc__)
        return 3
    reports = sorted(glob.glob(os.path.join(argv[1], "**", "report.json"), recursive=True))
    if not reports:
        print("no report.json found under %s" % argv[1], file=sys.stderr)
        return 2
    problems = []
    for report in reports:
        problems += check(report)
    for problem in problems:
        print("FAIL %s" % problem, file=sys.stderr)
    print("checked %d report(s), %d problem(s)" % (len(reports), len(problems)))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
