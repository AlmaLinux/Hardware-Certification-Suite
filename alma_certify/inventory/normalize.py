"""Build the normalized inventory summary from parsed collector output.

This is the cross-tool reconciliation layer: one record per CPU package,
DIMM, disk, NIC, and GPU, with fields merged from multiple sources. The
shape is part of the schema contract (docs/schema.md).
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from . import pci as pci_mod


def build_summary(parsed: Dict[str, Any], redact: bool = False) -> Dict[str, Any]:
    dmi_records = parsed.get("dmi", {}).get("records", [])
    by_type: Dict[int, List[Dict[str, Any]]] = {}
    for record in dmi_records:
        by_type.setdefault(record.get("dmi_type"), []).append(record)

    system = _system(by_type, redact)
    baseboard = _baseboard(by_type, redact)
    # No ``kind`` here. Classifying a machine as prebuilt, custom, or unknown is a guess about
    # what firmware authors meant, and guesses belong in the reader: Lumina's
    # ``inventory_extract.system_kind`` applies the same rule, so it can be revised for bundles
    # already submitted. A 1.0 report that predates the classifier gets classified too, which is
    # the whole point - nobody can go back and re-collect those.
    cpus = _cpus(parsed.get("cpu", {}), by_type)
    # Reported as observed. An AMD APU's iGPU used to be renamed here from the CPU's brand
    # string, because pci.ids has no marketing name for those dies - a real problem, solved in
    # the wrong place. The bundle carries the CPU string and the die codename; Lumina's
    # ``tieable_gpus`` applies the same rule, where it can be corrected for bundles already
    # submitted rather than only for future runs.
    gpus = parsed.get("gpu", {}).get("gpus", [])
    summary = {
        "system": system,
        "baseboard": baseboard,
        "chassis": _chassis(by_type),
        "bmc": _bmc(by_type),
        "cpus": cpus,
        "memory": _memory(parsed.get("memory", {}), by_type),
        "disks": _disks(parsed.get("storage", {}), redact),
        # ``nics`` and ``gpus`` are runtime *views*: which interfaces the kernel brought up (with
        # their MAC/speed/driver) and which display devices lspci named. What counts as a NIC or a
        # GPU is a categorization, and categorization is the server's job - so ``pci_devices`` below
        # carries the raw lspci enumeration and Lumina decides from it (a class-0200 card is a NIC
        # whether or not a driver bound a netdev, which these views miss). The views stay as
        # enrichment, joined to a device by its ``pci`` slot, and as the fallback for bundles from
        # before ``pci_devices`` existed.
        "nics": parsed.get("network", {}).get("nics", []),
        "gpus": gpus,
        # The authoritative hardware enumeration, kept raw and reprocessable. Additive within schema
        # 1.1 (see report.assemble): a server that predates it ignores it and reads the views above.
        "pci_devices": _pci_devices(parsed),
        "drivers": {"kernel": os.uname().release},
    }
    return summary


def _pci_devices(parsed: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Every PCI device ``lspci`` enumerated, passed through for the server to categorize.

    The named fields mirror the ``pci_ids`` block the nic/gpu views carry, so a reader has one
    shape; ``class_id`` is the 4-hex class the server keys categorization on (``02`` network,
    ``03`` display, ...); ``pci`` is the slot that joins a device to its runtime enrichment. Kept
    verbatim otherwise - the whole point is that no judgement about what these devices *are* is made
    here.
    """
    devices: List[Dict[str, Any]] = []
    for dev in parsed.get("pci", {}).get("devices", []):
        pci_ids = {
            name: dev[key]
            for name, key in (
                ("vendor", "vendor"),
                ("device", "device"),
                ("subsystem_vendor", "svendor"),
                ("subsystem_device", "sdevice"),
            )
            if dev.get(key)
        }
        devices.append({
            "pci": dev.get("slot"),
            "class": dev.get("class"),
            "class_id": pci_mod.class_id(dev),
            "pci_ids": pci_ids,
            "driver": dev.get("driver") or None,
        })
    return devices


def _prop(records: List[Dict[str, Any]], key: str) -> Optional[str]:
    for record in records:
        value = record.get("props", {}).get(key)
        if value and value not in ("Not Specified", "Not Provided", "None", "Unknown"):
            return value
    return None


# An opaque machine-type code: all caps and digits, no spaces. Lenovo puts one
# of these in Product Name ("21K9001NUS") and the readable model in Version
# ("ThinkBook 14 G6+ ABP"), which is the field lshw shows as the system's
# "version". Dell and HP need none of this: "PowerEdge R720" and "ProLiant
# DL380 Gen9" are already the readable name.
_MODEL_CODE_RE = re.compile(r"^[0-9A-Z][0-9A-Z]{4,15}$")


def _looks_like_model_code(value: Optional[str]) -> bool:
    return bool(value) and bool(_MODEL_CODE_RE.match(value.strip()))


def _looks_like_product_name(value: Optional[str]) -> bool:
    """A marketing name has a space and mixed case: "ThinkBook 14 G6+ ABP".

    Requiring a lowercase letter is what keeps firmware and board revision
    strings out: "SDK0K17763 WIN" has a space but is not a product name.
    """
    if _is_placeholder(value):
        return False
    text = str(value).strip()
    return " " in text and any(char.islower() for char in text)


def _system(by_type: Dict[int, List[Dict[str, Any]]], redact: bool) -> Dict[str, Any]:
    system = by_type.get(1, [])
    bios = by_type.get(0, [])
    code = _prop(system, "Product Name")
    version = _prop(system, "Version")
    family = _prop(system, "Family")

    # Prefer a readable model when Product Name is only a code. The code is
    # kept as model_number: it is what a support call and a parts lookup need.
    product, model_number = code, None
    if _looks_like_model_code(code):
        for candidate in (version, family):
            if _looks_like_product_name(candidate):
                product, model_number = str(candidate).strip(), code
                break

    return {
        # Placeholders are dropped rather than passed on. "OEM", "Default
        # string", and "To Be Filled By O.E.M." are what a vendor leaves when
        # nobody filled the field in, and forwarding them makes Lumina create
        # a manufacturer called OEM. A null says "unknown", which is the truth
        # and is what makes the submitter get asked.
        "vendor": _real(_prop(system, "Manufacturer")),
        "product": _real(product),
        "model_number": _real(model_number),
        "version": _real(version),
        "family": _real(family),
        "serial": None if redact else _real(_prop(system, "Serial Number")),
        "uuid": None if redact else _prop(system, "UUID"),
        "bios": {
            "vendor": _prop(bios, "Vendor"),
            "version": _prop(bios, "Version"),
            "date": _prop(bios, "Release Date"),
        },
    }


# AMD names the die through lspci for integrated graphics and never the
# product: "Phoenix1", "Renoir", "Cezanne", "Raphael". The product name lives
# in the CPU brand string, because the GPU is on the same package -
# "AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics". Short of opening a GL
# context (which needs a display, a Mesa driver, and glxinfo installed),
# nothing else on the machine knows that the die is a Radeon 780M, so this is
# the only source available to a stdlib-only collector.
def _chassis(by_type: Dict[int, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Form factor, recorded but never used to guess how a machine was built.

    "Notebook" or "Rack Mount Chassis" says nothing about who assembled it:
    Framework ships laptops as kits and barebones rack servers are a market of
    their own. This is catalog data for filtering, not a heuristic input.
    """
    chassis = by_type.get(3, [])
    return {
        "type": _prop(chassis, "Type"),
        "vendor": _prop(chassis, "Manufacturer"),
    }


def _bmc(by_type: Dict[int, List[Dict[str, Any]]]) -> Dict[str, Any]:
    """Baseboard management controller, from SMBIOS type 38.

    Presence is known without loading an IPMI driver or installing ipmitool,
    which is what lets the IPMI tests skip on hardware that has no BMC without
    probing anything. Network configuration is deliberately absent: a
    management interface address is a live attack surface and these reports get
    published, so it is never collected.
    """
    ipmi = by_type.get(38, [])
    if not ipmi:
        return {"present": False}
    return {
        "present": True,
        "interface": _prop(ipmi, "Interface Type"),
        "ipmi_version": _prop(ipmi, "Specification Version"),
        "i2c_address": _prop(ipmi, "I2C Slave Address"),
        "nv_storage": _prop(ipmi, "NV Storage Device"),
    }


def _baseboard(by_type: Dict[int, List[Dict[str, Any]]], redact: bool) -> Dict[str, Any]:
    board = by_type.get(2, [])
    return {
        "vendor": _real(_prop(board, "Manufacturer")),
        "product": _real(_prop(board, "Product Name")),
        "version": _real(_prop(board, "Version")),
        "serial": None if redact else _real(_prop(board, "Serial Number")),
    }


# Strings vendors leave in DMI when nobody filled the field in. Matching is
# case-insensitive.
_DMI_PLACEHOLDERS = {
    "", "to be filled by o.e.m.", "default string", "system product name",
    "system manufacturer", "system version", "not specified", "not applicable",
    "no enclosure", "none", "oem", "o.e.m.", "unknown", "invalid",
    "0123456789", "12345678", "empty", "type1productconfigid", "...", "-",
}


def _is_placeholder(value: Optional[str]) -> bool:
    return not value or value.strip().lower() in _DMI_PLACEHOLDERS


def _real(value: Optional[str]) -> Optional[str]:
    """The value, or None if it is one of the strings vendors leave behind."""
    return None if _is_placeholder(value) else value


def _cpus(cpu: Dict[str, Any], by_type: Dict[int, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    sockets = cpu.get("sockets") or 1
    cores = (cpu.get("cores_per_socket") or 0) * sockets
    entry = {
        "model": cpu.get("model") or _prop(by_type.get(4, []), "Version"),
        "vendor": cpu.get("vendor"),
        "sockets": sockets,
        "cores": cores or None,
        "threads": cpu.get("threads_total"),
        "max_mhz": cpu.get("max_mhz"),
        # Informational: the full advertised feature set, so a reader can tell
        # whether a machine has avx512f or sev_snp without running anything.
        # Defaulted to a list so a consumer never has to guard the field.
        "flags": cpu.get("flags") or [],
        "flags_virt": cpu.get("flags_virt", ""),
    }
    return [entry]


# dmidecode has always meant powers of 1024 here; what changed is how it spells
# them. 3.5 switched from "16 GB" to "16 GiB", so a map of only the old spellings
# silently dropped every DIMM size on any machine with a current dmidecode - a
# ThinkPad reporting "Size: 16 GiB" produced four modules of unknown capacity.
_SIZE_UNITS = {
    "KB": 1024, "MB": 1024 ** 2, "GB": 1024 ** 3, "TB": 1024 ** 4,
    "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
    "BYTES": 1,
}


def _dimm_bytes(size_str: Optional[str]) -> Optional[int]:
    if not size_str:
        return None
    parts = size_str.split()
    if len(parts) != 2:
        return None
    try:
        return int(parts[0]) * _SIZE_UNITS.get(parts[1].upper(), 0) or None
    except ValueError:
        return None


def _speed_mts(value: Optional[str]) -> Optional[int]:
    """The leading integer of a dmidecode speed, e.g. "6400 MT/s" -> 6400."""
    try:
        return int(str(value).split()[0])
    except (AttributeError, ValueError, IndexError):
        return None


def _as_int(value: Optional[str]) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (AttributeError, ValueError):
        return None


def _memory(mem: Dict[str, Any], by_type: Dict[int, List[Dict[str, Any]]]) -> Dict[str, Any]:
    dimms = []
    for record in by_type.get(17, []):
        props = record.get("props", {})
        size = props.get("Size", "")
        if not size or "No Module" in size:
            continue
        speed_mts = _speed_mts(
            props.get("Configured Memory Speed") or props.get("Speed")
        )
        dimms.append(
            {
                "locator": props.get("Locator"),
                # Which channel a module sits in decides whether the machine
                # runs dual/quad channel, which moves bandwidth more than
                # capacity does. Reported as "P0 CHANNEL A".
                "bank_locator": props.get("Bank Locator"),
                "size_bytes": _dimm_bytes(size),
                "type": props.get("Type"),
                "speed_mts": speed_mts,
                # Rated speed as well as configured: a DDR5-5600 module clocked
                # to 4800 by the platform is a fact about the platform, and
                # comparing benchmark results without it invites confusion.
                "rated_speed_mts": _speed_mts(props.get("Speed")),
                "rank": _as_int(props.get("Rank")),
                "manufacturer": props.get("Manufacturer"),
                "part_number": props.get("Part Number"),
            }
        )
    slots_total = len(by_type.get(17, []))
    return {
        "total_bytes": mem.get("total_bytes", 0),
        "dimms": dimms,
        # Populated versus available slots: room to expand is part of what a
        # reader wants from a server listing.
        "slots_total": slots_total or None,
        "slots_populated": len(dimms) or None,
    }


def _disks(storage: Dict[str, Any], redact: bool) -> List[Dict[str, Any]]:
    disks = []
    for disk in storage.get("disks", []):
        entry = dict(disk)
        if redact:
            entry["serial"] = None
        entry.pop("smart_passed", None)  # health lives in validate results
        disks.append(entry)
    return disks
