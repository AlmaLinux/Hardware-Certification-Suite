"""USB devices via lsusb."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from .. import procutil

_LSUSB_RE = re.compile(
    r"^Bus (\d+) Device (\d+): ID ([0-9a-f]{4}):([0-9a-f]{4})\s*(.*)$"
)


def collect(inv_dir: str) -> Dict[str, Any]:
    try:
        res = procutil.run_cmd(
            ["lsusb"], timeout=30, tee_path=os.path.join(inv_dir, "lsusb.txt")
        )
    except procutil.CommandNotFound:
        return {"devices": [], "available": False}
    devices: List[Dict[str, str]] = []
    for line in res.stdout.splitlines():
        m = _LSUSB_RE.match(line.strip())
        if m:
            devices.append(
                {
                    "bus": m.group(1),
                    "device": m.group(2),
                    "vendor_id": m.group(3),
                    "product_id": m.group(4),
                    "description": m.group(5),
                }
            )
    return {"devices": devices, "available": res.ok}
