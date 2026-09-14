"""Bundle a finished run into a submittable tarball.

Bundles are zstd-compressed tars (``.tar.zst``). The runtime is stdlib-only
with a Python 3.9 floor and stdlib zstd only exists in 3.14, so compression
shells out to the ``zstd`` binary - which ships in AlmaLinux BaseOS and is a
hard RPM requirement of this package.

Bundling is deterministic: normalized tar metadata (mtime=0, root ownership,
0644) and zstd's content-derived framing, so re-bundling an unchanged run
produces byte-identical output and a resubmission is recognized server-side
as a duplicate instead of a conflict.
"""

from __future__ import annotations

import json
import os
import tarfile
import tempfile
from typing import Optional

from . import procutil
from .report import sha256_file


def bundle_run(run_dir: str, output: Optional[str] = None) -> str:
    report_path = os.path.join(run_dir, "report.json")
    if not os.path.isfile(report_path):
        raise FileNotFoundError("no report.json in %s - run has not finished" % run_dir)
    if procutil.which("zstd") is None:
        raise RuntimeError("the 'zstd' command is required to build bundles "
                           "(dnf install zstd)")

    with open(report_path, encoding="utf-8") as fh:
        report = json.load(fh)
    hostname = report["run"].get("hostname", "unknown")
    run_id = report["run"]["run_id"]

    if output is None:
        output = os.path.join(os.getcwd(), "alma-certify-%s-%s.tar.zst" % (hostname, run_id))

    members = ["report.json"] + [e["path"] for e in report["artifact_manifest"]]

    sums_path = os.path.join(run_dir, "SHA256SUMS")
    with open(sums_path, "w", encoding="utf-8") as fh:
        for rel in members:
            fh.write("%s  %s\n" % (sha256_file(os.path.join(run_dir, rel)), rel))

    fd, tar_path = tempfile.mkstemp(suffix=".tar", dir=os.path.dirname(output) or ".")
    os.close(fd)
    try:
        with tarfile.open(tar_path, mode="w") as tar:
            for rel in members + ["SHA256SUMS"]:
                path = os.path.join(run_dir, rel)
                info = tar.gettarinfo(path, arcname=rel)
                info.uid = info.gid = 0
                info.uname = info.gname = "root"
                info.mtime = 0
                info.mode = 0o644
                with open(path, "rb") as fh:
                    tar.addfile(info, fh)
        procutil.run_cmd(
            ["zstd", "-q", "-T0", "-f", tar_path, "-o", output],
            timeout=600,
            check=True,
        )
    finally:
        try:
            os.unlink(tar_path)
        except OSError:
            pass
    return output
