"""OS release, kernel, architecture."""

from __future__ import annotations

import os
from typing import Any, Dict

from .. import hostos, procutil


def collect(inv_dir: str) -> Dict[str, Any]:
    os_release_raw = procutil.read_file("/etc/os-release", "") or ""
    with open(os.path.join(inv_dir, "os-release.txt"), "w", encoding="utf-8") as fh:
        fh.write(os_release_raw)

    # One parser, in hostos: this copy stripped only double quotes, so a
    # single-quoted os-release put literal quotes in the collected inventory.
    fields: Dict[str, str] = hostos.parse_os_release(os_release_raw)

    uname = os.uname()
    procutil.run_cmd(
        ["uname", "-a"], timeout=10, tee_path=os.path.join(inv_dir, "uname.txt")
    )
    return {
        "id": fields.get("ID", "unknown"),
        "version_id": fields.get("VERSION_ID", "unknown"),
        "pretty_name": fields.get("PRETTY_NAME", ""),
        "kernel": uname.release,
        "arch": uname.machine,
        "hostname": uname.nodename,
    }
