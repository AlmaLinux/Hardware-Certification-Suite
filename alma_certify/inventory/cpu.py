"""CPU topology via lscpu (JSON where available, text fallback)."""

from __future__ import annotations

import json
import os
from typing import Any, Dict

from .. import procutil


def _flatten_lscpu_json(data: Any) -> Dict[str, str]:
    """lscpu -J emits {"lscpu": [{"field": ..., "data": ..., "children": [...]}]}."""
    out: Dict[str, str] = {}

    def walk(entries):
        for entry in entries:
            field = (entry.get("field") or "").rstrip(":")
            if field:
                out[field] = entry.get("data") or ""
            if entry.get("children"):
                walk(entry["children"])

    walk(data.get("lscpu", []))
    return out


def _parse_lscpu_text(text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for line in text.splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            out[k.strip()] = v.strip()
    return out


# On aarch64 lscpu's "Vendor ID" is the architecture licensor decoded from MIDR, which
# is "ARM" for every Neoverse-based part no matter who built the chip, and "Model name"
# is the core design rather than the processor. An Ampere Altra reports vendor ARM,
# model Neoverse-N1 - naming neither the vendor nor the product anyone bought. The
# silicon vendor is in the BIOS fields, so where the firmware knows better, prefer it.
_ARCH_LICENSORS = frozenset({"arm", "arm limited"})


def _identity(fields: Dict[str, str]) -> tuple:
    """The silicon vendor and processor model, preferring firmware over MIDR on Arm.

    Returns ``(vendor, model)`` verbatim as reported - trademark marks and all, the
    same fidelity as every other inventory string, with normalization left to the
    consumer. x86_64 is deliberately untouched: its "Vendor ID" (GenuineIntel,
    AuthenticAMD) is the stable key consumers already group by, and substituting the
    BIOS spelling "Intel(R) Corporation" would fragment it. Whatever is not chosen
    here is still in ``fields``, which keeps every line lscpu printed.
    """
    vendor = (fields.get("Vendor ID") or "").strip()
    model = (fields.get("Model name") or "").strip()
    if vendor.lower() not in _ARCH_LICENSORS:
        return vendor, model
    # Each field falls back independently: a firmware vendor beside a MIDR core name is
    # still strictly more accurate than the licensor, even where one of the two is absent.
    return (
        (fields.get("BIOS Vendor ID") or "").strip() or vendor,
        (fields.get("BIOS Model name") or "").strip() or model,
    )


def collect(inv_dir: str) -> Dict[str, Any]:
    fields: Dict[str, str] = {}
    try:
        res = procutil.run_cmd(
            ["lscpu", "-J"], timeout=30, tee_path=os.path.join(inv_dir, "lscpu.json.txt")
        )
        if res.ok:
            fields = _flatten_lscpu_json(json.loads(res.stdout))
    except (procutil.CommandNotFound, json.JSONDecodeError):
        pass
    if not fields:
        try:
            res = procutil.run_cmd(
                ["lscpu"], timeout=30, tee_path=os.path.join(inv_dir, "lscpu.txt")
            )
            if res.ok:
                fields = _parse_lscpu_text(res.stdout)
        except procutil.CommandNotFound:
            pass

    def to_int(key: str):
        try:
            return int(fields.get(key, ""))
        except ValueError:
            return None

    flags = collect_flags(fields)
    virt = ""
    if "vmx" in flags:
        virt = "vmx"
    elif "svm" in flags:
        virt = "svm"

    vendor, model = _identity(fields)
    return {
        "fields": fields,
        "model": model,
        "vendor": vendor,
        "sockets": to_int("Socket(s)"),
        "cores_per_socket": to_int("Core(s) per socket"),
        "threads_total": to_int("CPU(s)"),
        "max_mhz": fields.get("CPU max MHz") or None,
        # The whole advertised feature set, reported for information. Whether a
        # machine has avx512f, sha_ni, or sev_snp decides whether a workload suits
        # it, and nothing passes or fails on the answer.
        "flags": flags,
        # Kept separate from the list above because it answers a different
        # question, and validate.virt.kvm reads it: not "what does this CPU
        # advertise" but "which vendor's virtualization extension is this".
        "flags_virt": virt,
    }


def collect_flags(fields):
    """Every CPU feature flag lscpu reported, sorted and deduplicated.

    Two spellings, because lscpu uses ``Flags`` on x86_64 and ``Features`` on
    aarch64. Reading only the first returned nothing at all on Arm.

    Sorted so two runs of the same CPU produce identical output regardless of the
    order lscpu happened to emit, which keeps a diff between them empty rather than
    a reshuffle. A list rather than the raw string so a consumer can test
    membership without splitting, and an empty list rather than None so it can be
    iterated unguarded.
    """
    raw = fields.get("Flags") or fields.get("Features") or ""
    return sorted(set(raw.split()))
