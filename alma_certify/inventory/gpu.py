"""GPU detection with mandatory driver identification.

Sources, in order of preference:
- lspci class 03xx for enumeration (vendor/model/pci address/kernel driver)
- nvidia-smi (driver + CUDA version + VBIOS) when the NVIDIA driver is present
- rocm-smi / /opt/rocm/.info/version for ROCm runtime version
- sysfs + modinfo for in-kernel drivers (amdgpu, i915, nouveau, ast, mgag200)
"""

from __future__ import annotations

import json
import os
import re
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional

from .. import procutil
from . import pci as pci_mod

# PCI vendor ids, used only to decide which *tool* to interrogate: nvidia-smi for an NVIDIA
# card, rocm for an AMD one. That is a fact about which program can answer, not a decision about
# what the part is called - the naming is the server's, from the strings reported below.
_NVIDIA_ID = "10de"
_AMD_ID = "1002"
_VENDOR_ID_RE = re.compile(r"\[([0-9a-f]{4})\]$")


def collect(inv_dir: str) -> Dict[str, Any]:
    # Re-parse the lspci raw output the pci collector already saved if
    # possible, else run lspci ourselves (collectors are independent).
    lspci_path = os.path.join(inv_dir, "lspci.txt")
    text = procutil.read_file(lspci_path)
    if text is None:
        try:
            res = procutil.run_cmd(["lspci", "-vmmnnk"], timeout=30)
            text = res.stdout if res.ok else ""
        except procutil.CommandNotFound:
            text = ""

    gpus: List[Dict[str, Any]] = []
    for dev in pci_mod.parse_lspci_vmm(text):
        if not pci_mod.class_id(dev).startswith("03"):
            continue
        vid = _VENDOR_ID_RE.search(dev.get("vendor", ""))
        gpus.append(
            {
                "pci": dev.get("slot"),
                # Every name lspci gives, verbatim, ids included. Which of them is the product's
                # name is the server's decision - see ``gpu_identity`` there. This used to be
                # flattened here to a token ("nvidia") plus the chip name, which meant a rule
                # that turned out wrong could only be fixed for future runs. The NIC collector
                # made the same mistake and produced a component called "Device".
                "pci_ids": {
                    name: dev[key]
                    for name, key in (
                        ("vendor", "vendor"),
                        ("device", "device"),
                        ("subsystem_vendor", "svendor"),
                        ("subsystem_device", "sdevice"),
                    )
                    if dev.get(key)
                },
                # Kept internal to this function; stripped before the report is written.
                "_vendor_id": vid.group(1) if vid else "",
                "driver": dev.get("driver") or None,
                "driver_version": None,
                "runtime": {},
                "vbios": None,
            }
        )

    nvidia = _nvidia_info(inv_dir)
    rocm_version = _rocm_version(inv_dir)
    mesa_version = _mesa_version()

    for gpu in gpus:
        if gpu["_vendor_id"] == _NVIDIA_ID and nvidia:
            gpu["driver"] = gpu["driver"] or "nvidia"
            gpu["driver_version"] = nvidia.get("driver_version")
            if nvidia.get("cuda_version"):
                gpu["runtime"]["cuda"] = nvidia["cuda_version"]
            by_pci = nvidia.get("by_pci", {})
            for pci_addr, extra in by_pci.items():
                if gpu["pci"] and pci_addr.lower().endswith(gpu["pci"].lower()):
                    gpu["vbios"] = extra.get("vbios")
                    # Its own field rather than overwriting a name from lspci. nvidia-smi gives
                    # the marketing name ("NVIDIA GeForce RTX 4090") where lspci gives the die
                    # ("AD102 [GeForce RTX 4090]"), and both are worth having: the server prefers
                    # the marketing name and can change its mind later, which it cannot do if one
                    # source has already overwritten the other.
                    if extra.get("model"):
                        gpu["smi_name"] = extra["model"]
        elif gpu["driver"]:
            gpu["driver_version"] = _modinfo_version(inv_dir, gpu["driver"])
            if gpu["_vendor_id"] == _AMD_ID and rocm_version:
                gpu["runtime"]["rocm"] = rocm_version
            if mesa_version:
                gpu["runtime"]["mesa"] = mesa_version

    for gpu in gpus:
        del gpu["_vendor_id"]
    return {"gpus": gpus}


def _nvidia_info(inv_dir: str) -> Optional[Dict[str, Any]]:
    try:
        res = procutil.run_cmd(
            ["nvidia-smi", "-q", "-x"],
            timeout=60,
            tee_path=os.path.join(inv_dir, "nvidia-smi.xml.txt"),
        )
    except procutil.CommandNotFound:
        return None
    if not res.ok:
        return None
    try:
        root = ET.fromstring(res.stdout)
    except ET.ParseError:
        return None
    info: Dict[str, Any] = {
        "driver_version": _xml_text(root, "driver_version"),
        "cuda_version": _xml_text(root, "cuda_version"),
        "by_pci": {},
    }
    for gpu_el in root.findall("gpu"):
        pci_addr = (gpu_el.get("id") or "").strip()
        info["by_pci"][pci_addr] = {
            "model": _xml_text(gpu_el, "product_name"),
            "vbios": _xml_text(gpu_el, "vbios_version"),
        }
    return info


def _xml_text(el: ET.Element, tag: str) -> Optional[str]:
    child = el.find(tag)
    if child is not None and child.text and child.text.strip() not in ("N/A", ""):
        return child.text.strip()
    return None


def _rocm_version(inv_dir: str) -> Optional[str]:
    version = procutil.read_file("/opt/rocm/.info/version")
    if version:
        return version.splitlines()[0].strip()
    try:
        res = procutil.run_cmd(
            ["rocm-smi", "--showdriverversion", "--json"],
            timeout=30,
            tee_path=os.path.join(inv_dir, "rocm-smi.json.txt"),
        )
        if res.ok:
            data = json.loads(res.stdout)
            for value in data.values():
                if isinstance(value, dict) and value.get("Driver version"):
                    return value["Driver version"]
    except (procutil.CommandNotFound, json.JSONDecodeError):
        pass
    return None


def _mesa_version() -> Optional[str]:
    res = procutil.run_cmd(["rpm", "-q", "--qf", "%{VERSION}", "mesa-dri-drivers"], timeout=15)
    return res.stdout.strip() if res.ok else None


def _modinfo_version(inv_dir: str, module: str) -> Optional[str]:
    try:
        res = procutil.run_cmd(
            ["modinfo", "-F", "version", module],
            timeout=15,
            tee_path=os.path.join(inv_dir, "modinfo-%s.txt" % module),
        )
        version = res.stdout.strip()
        if version:
            return version
    except procutil.CommandNotFound:
        pass
    # in-tree modules often have no version field; the kernel version is the
    # meaningful driver version then
    return "kernel:%s" % os.uname().release
