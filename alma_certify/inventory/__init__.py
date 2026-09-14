"""Hardware inventory collection.

Each collector module exposes ``collect(inv_dir) -> dict`` returning parsed
data and writing its raw tool output under ``inv_dir``. ``collect_all``
orchestrates them and produces the ``{"summary": ..., "raw": ...}`` object
embedded in report.json (see docs/schema.md).
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable, Dict, Optional

from . import cpu, dmi, gpu, memory, network, osinfo, pci, storage, usb
from .normalize import build_summary

COLLECTORS: Dict[str, Callable[[str], Dict[str, Any]]] = {
    "osinfo": osinfo.collect,
    "dmi": dmi.collect,
    "cpu": cpu.collect,
    "memory": memory.collect,
    "pci": pci.collect,
    "usb": usb.collect,
    "storage": storage.collect,
    "network": network.collect,
    "gpu": gpu.collect,
}


def collect_all(
    run_dir: str,
    redact: bool = False,
    log: Optional[Callable[[str], None]] = None,
    status: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Run every collector, writing raw artifacts and a normalized summary.

    ``status`` names the collector now running, for a front-end that shows one live line. It is
    separate from ``log`` because these are not events worth a line in the run log each - the log
    already has them - but they are what somebody watching needs: the storage collector runs one
    ``smartctl`` per drive at up to a minute each, and on a machine with a shelf of disks that is
    several minutes during which a single "collecting hardware inventory" line says nothing about
    whether it is progressing.
    """
    inv_dir = os.path.join(run_dir, "inventory")
    os.makedirs(inv_dir, exist_ok=True)
    log = log or (lambda msg: None)
    status = status or (lambda msg: None)

    parsed: Dict[str, Any] = {}
    for name, collector in COLLECTORS.items():
        status("collecting hardware inventory: %s" % name)
        log("collecting %s" % name)
        try:
            parsed[name] = collector(inv_dir)
        except Exception as exc:
            log("warning: %s collector failed: %s" % (name, exc))
            parsed[name] = {"error": str(exc)}

    summary = build_summary(parsed, redact=redact)
    # /etc/machine-id: the OS-install identity. A hardware-survey dedup fallback when
    # firmware serials/UUID are blank or a vendor default. Identity, so redacted with
    # the serials and UUID when --redact is given.
    summary["machine_id"] = None if redact else _machine_id()
    raw = {
        name: os.path.join("inventory", fname)
        for name, fname in _raw_files(inv_dir).items()
    }
    inventory = {"summary": summary, "raw": raw}

    with open(os.path.join(inv_dir, "inventory.json"), "w", encoding="utf-8") as fh:
        json.dump(inventory, fh, indent=2, sort_keys=True)
        fh.write("\n")
    return inventory


def _machine_id() -> Optional[str]:
    """``/etc/machine-id``, or None if unreadable (a stripped or minimal image)."""
    try:
        with open("/etc/machine-id", encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _raw_files(inv_dir: str) -> Dict[str, str]:
    out = {}
    for fname in sorted(os.listdir(inv_dir)):
        if fname == "inventory.json":
            continue
        if os.path.isfile(os.path.join(inv_dir, fname)):
            out[os.path.splitext(fname)[0]] = fname
    return out
