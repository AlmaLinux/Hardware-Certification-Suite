"""PCI devices via ``lspci -vmmnnk`` (machine-readable, with kernel driver)."""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

from .. import procutil


def parse_lspci_vmm(text: str) -> List[Dict[str, str]]:
    devices: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in text.splitlines():
        if not line.strip():
            if current:
                devices.append(current)
                current = {}
            continue
        key, _, value = line.partition(":")
        current[key.strip().lower()] = value.strip()
    if current:
        devices.append(current)
    return devices


_CLASS_ID_RE = re.compile(r"\[([0-9a-f]{4})\]$")


def class_id(device: Dict[str, str]) -> str:
    """Extract the 4-hex-digit class code from a -nn 'class' field."""
    m = _CLASS_ID_RE.search(device.get("class", ""))
    return m.group(1) if m else ""


def collect(inv_dir: str) -> Dict[str, Any]:
    try:
        res = procutil.run_cmd(
            ["lspci", "-vmmnnk"],
            timeout=30,
            tee_path=os.path.join(inv_dir, "lspci.txt"),
        )
    except procutil.CommandNotFound:
        return {"devices": [], "available": False}
    if not res.ok:
        return {"devices": [], "available": False}
    return {"devices": parse_lspci_vmm(res.stdout), "available": True}
