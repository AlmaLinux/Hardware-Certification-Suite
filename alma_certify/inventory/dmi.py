"""DMI/SMBIOS via dmidecode: system, baseboard, BIOS, processor, memory."""

from __future__ import annotations

import os
from typing import Any, Dict, List

from .. import procutil

# type -> human name; the set the old hw_detection test cared about
DMI_TYPES = {
    0: "bios",
    1: "system",
    2: "baseboard",
    # Chassis form factor: catalog data (rack, tower, laptop), not a classifier.
    # ``_system_kind`` reads only the system and baseboard tables and never sees
    # this, which is deliberate - see _chassis's own docstring and
    # tests/test_parsers.py::test_chassis_type_does_not_decide_how_a_machine_was_built.
    3: "chassis",
    4: "processor",
    16: "memory_array",
    17: "memory_device",
    # IPMI Device Information: the presence of a BMC, without needing a driver
    # loaded or ipmitool installed to find out.
    38: "ipmi",
}


def parse_dmidecode(text: str) -> List[Dict[str, Any]]:
    """Parse dmidecode output into a list of records with handle/name/props."""
    records: List[Dict[str, Any]] = []
    current: Dict[str, Any] = {}
    for line in text.splitlines():
        if line.startswith("Handle "):
            if current:
                records.append(current)
            # "Handle 0x0400, DMI type 4, 48 bytes"
            parts = line.split(",")
            dmi_type = None
            for part in parts:
                part = part.strip()
                if part.startswith("DMI type"):
                    try:
                        dmi_type = int(part.split()[2])
                    except (IndexError, ValueError):
                        pass
            current = {"dmi_type": dmi_type, "name": None, "props": {}}
        elif current and current["name"] is None and line and not line.startswith("\t"):
            current["name"] = line.strip()
        elif current and line.startswith("\t") and ":" in line:
            # only top-level "\tKey: Value" props; nested lists are ignored
            if line.startswith("\t\t"):
                continue
            key, _, value = line.strip().partition(":")
            current["props"][key.strip()] = value.strip()
    if current:
        records.append(current)
    return records


def collect(inv_dir: str) -> Dict[str, Any]:
    type_args = []
    for t in DMI_TYPES:
        type_args += ["-t", str(t)]
    try:
        res = procutil.run_cmd(
            ["dmidecode"] + type_args,
            timeout=60,
            tee_path=os.path.join(inv_dir, "dmidecode.txt"),
        )
    except procutil.CommandNotFound:
        return {"records": [], "available": False}
    if not res.ok:
        return {"records": [], "available": False}
    return {"records": parse_dmidecode(res.stdout), "available": True}
