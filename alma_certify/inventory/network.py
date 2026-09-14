"""NICs: ip -j link, ethtool (speed/link), ethtool -i (driver/firmware), lspci (identity).

The lspci join is what makes a NIC nameable. Everything ``ip`` and ``ethtool`` report is a
*local* fact - the interface name the kernel assigned, its MAC, its driver, its PCI slot - and
none of it identifies the part. "enp2s0 running r8169" is not a product anybody can look up or
certify, so the catalog had nothing to make an entry from and NICs were the one component class
that never appeared.

**Every name lspci gives is reported and none of them is chosen here.** Each NIC carries a
``pci_ids`` block holding the vendor, device, subsystem vendor, and subsystem device exactly as
lspci printed them, numeric id suffixes included. Which of those is the product's name is a
judgement, and judgements belong on the server: a bundle is written once and read for years, so a
rule that lives in the reader can be corrected for every bundle ever submitted, while one applied
here is frozen into each of them.

Not hypothetical. The first version of this chose the subsystem name at collection time, because
it names the card rather than the chip - and pci.ids has no name for most onboard subsystems, so
lspci writes the placeholder "Device [09a8]" and a real run arrived with the model "Device". Had
the choice been made server-side, fixing the reader would have fixed the bundles too.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Dict, List, Optional

from .. import procutil
from . import pci as pci_mod

_SPEED_RE = re.compile(r"Speed:\s*(\d+)\s*Mb/s")
_LINK_RE = re.compile(r"Link detected:\s*(yes|no)")
# PCI class 02xx is a network controller: 0200 ethernet, 0280 wireless, 0206 and friends.
_NETWORK_CLASS_PREFIX = "02"


def collect(inv_dir: str) -> Dict[str, Any]:
    nics: List[Dict[str, Any]] = []
    identities = _pci_identities(inv_dir)
    try:
        res = procutil.run_cmd(
            ["ip", "-j", "link"],
            timeout=30,
            tee_path=os.path.join(inv_dir, "ip-link.json.txt"),
        )
        links = json.loads(res.stdout) if res.ok else []
    except (procutil.CommandNotFound, json.JSONDecodeError):
        links = []

    for link in links:
        name = link.get("ifname", "")
        if not name or name == "lo":
            continue
        # only physical devices (they have a /sys/class/net/<if>/device)
        if not os.path.isdir("/sys/class/net/%s/device" % name):
            continue
        slot = _pci_addr(name)
        nic: Dict[str, Any] = {
            "name": name,
            "mac": link.get("address"),
            "pci": slot,
            # Empty when lspci is unavailable, or when the device is not on the PCI bus at all -
            # USB ethernet, an SoC's built-in MAC. The server treats a NIC it cannot name as one
            # it cannot catalog, which is the honest outcome.
            "pci_ids": identities.get(_normalize_slot(slot)) or {},
            "operstate": link.get("operstate"),
        }
        nic.update(_ethtool_info(inv_dir, name))
        nics.append(nic)

    return {"nics": nics}


def _normalize_slot(slot: Optional[str]) -> str:
    """A PCI address in the form lspci prints, so the two sources can be joined.

    ``/sys/class/net/<if>/device`` resolves to a fully qualified "0000:02:00.0"; lspci omits the
    domain unless asked for it. Comparing them raw matched nothing, which would have left every
    NIC unnamed while looking like lspci had simply not reported it.
    """
    if not slot:
        return ""
    parts = slot.split(":")
    return ":".join(parts[-2:]) if len(parts) > 2 else slot


def _pci_identities(inv_dir: str) -> Dict[str, Dict[str, str]]:
    """``{slot: {vendor, device, subsystem_vendor, subsystem_device}}`` for network devices.

    Verbatim, including the "[10ec]" suffixes ``-nn`` appends. Stripping them is one line for the
    reader and unrecoverable here: the numeric ids are the only stable identifier a device has,
    and a reader that distrusts a name can still look one up.

    Keys renamed from lspci's abbreviations, which is the one liberty taken. "svendor" is not
    self-explanatory to anybody reading a bundle a year from now.
    """
    text = procutil.read_file(os.path.join(inv_dir, "lspci.txt"))
    if text is None:
        # The collectors are independent and may run in any order, so this cannot assume the
        # pci collector has already saved its output.
        try:
            res = procutil.run_cmd(["lspci", "-vmmnnk"], timeout=30)
            text = res.stdout if res.ok else ""
        except procutil.CommandNotFound:
            text = ""

    identities: Dict[str, Dict[str, str]] = {}
    for dev in pci_mod.parse_lspci_vmm(text):
        if not pci_mod.class_id(dev).startswith(_NETWORK_CLASS_PREFIX):
            continue
        slot = _normalize_slot(dev.get("slot"))
        if not slot:
            continue
        identities[slot] = {
            name: dev[key]
            for name, key in (
                ("vendor", "vendor"),
                ("device", "device"),
                ("subsystem_vendor", "svendor"),
                ("subsystem_device", "sdevice"),
            )
            if dev.get(key)
        }
    return identities


def _pci_addr(name: str) -> Optional[str]:
    try:
        dev = os.readlink("/sys/class/net/%s/device" % name)
        return os.path.basename(dev)
    except OSError:
        return None


def _ethtool_info(inv_dir: str, name: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "driver": None,
        "driver_version": None,
        "firmware": None,
        "speed_mbps": None,
        "link": None,
    }
    try:
        res = procutil.run_cmd(
            ["ethtool", name],
            timeout=30,
            tee_path=os.path.join(inv_dir, "ethtool-%s.txt" % name),
        )
        if res.ok:
            m = _SPEED_RE.search(res.stdout)
            if m:
                out["speed_mbps"] = int(m.group(1))
            m = _LINK_RE.search(res.stdout)
            if m:
                out["link"] = m.group(1) == "yes"
        res = procutil.run_cmd(
            ["ethtool", "-i", name],
            timeout=30,
            tee_path=os.path.join(inv_dir, "ethtool-i-%s.txt" % name),
        )
        if res.ok:
            for line in res.stdout.splitlines():
                key, _, value = line.partition(":")
                key, value = key.strip(), value.strip()
                if key == "driver" and value:
                    out["driver"] = value
                elif key == "version" and value:
                    out["driver_version"] = value
                elif key == "firmware-version" and value:
                    out["firmware"] = value
    except procutil.CommandNotFound:
        pass
    return out
