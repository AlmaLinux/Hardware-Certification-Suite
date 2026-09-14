"""Memory: total from /proc/meminfo; DIMM topology comes from the DMI collector."""

from __future__ import annotations

import os
from typing import Any, Dict

from .. import procutil


def collect(inv_dir: str) -> Dict[str, Any]:
    meminfo = procutil.read_file("/proc/meminfo", "") or ""
    with open(os.path.join(inv_dir, "meminfo.txt"), "w", encoding="utf-8") as fh:
        fh.write(meminfo)

    total_kb = 0
    for line in meminfo.splitlines():
        if line.startswith("MemTotal:"):
            try:
                total_kb = int(line.split()[1])
            except (IndexError, ValueError):
                pass
            break

    numa_nodes = None
    try:
        res = procutil.run_cmd(
            ["numactl", "--hardware"],
            timeout=15,
            tee_path=os.path.join(inv_dir, "numactl.txt"),
        )
        if res.ok and res.stdout.startswith("available:"):
            numa_nodes = int(res.stdout.split()[1])
    except (procutil.CommandNotFound, ValueError, IndexError):
        pass

    return {"total_bytes": total_kb * 1024, "numa_nodes": numa_nodes}
