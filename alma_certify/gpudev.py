"""Choose which GPUs clpeak benchmarks, so a machine with several cards of the same model is not
benchmarked once per identical card by default.

clpeak runs every device it finds on a backend and, because it keys its own results by device
*name*, two physically identical cards collapse into one entry inside a single invocation. That
gives correct per-model results for free but wastes time re-benchmarking the duplicates, and it
makes the two identical cards impossible to report separately. So:

- **Default:** enumerate the devices (``clpeak --list-devices``), pick one index per distinct name,
  and pin clpeak to those with the per-backend selector flags. Identical cards are benchmarked once.
- **--all-gpus:** benchmark every device, one clpeak invocation per device, stamping an ordinal
  within each name so identical cards submit as individual results.

**Best-effort, with a safe fallback.** ``--list-devices`` output is text, not a stable interface, so
an unreadable or surprising layout yields no plan and the caller lets clpeak run every device - the
same correct per-name results as today, only without the time saved. Nothing here can produce a
wrong number; the worst case is a duplicate benchmarked that need not have been.

**OpenCL and Metal are left to run every device.** clpeak's OpenCL device index is relative to a
platform, not a flat ordinal, so pinning it reliably would mean tracking platform+device; the flat
per-backend selector this module uses fits CUDA, ROCm, Vulkan, and oneAPI. OpenCL therefore does not
dedupe or split identical cards - its results still collapse per name inside clpeak, which is
correct, just not time-saving. Named here so the limitation is not a surprise.
"""

from __future__ import annotations

import re
from typing import Callable, Dict, List, NamedTuple, Optional

# clpeak's per-backend device selector, for the backends whose device index is a flat ordinal.
# OpenCL (--cl-device) and Metal are deliberately absent: their index is platform-relative.
BACKEND_DEVICE_FLAG = {
    "vulkan": "--vk-device",
    "cuda": "--cuda-device",
    "rocm": "--rocm-device",
    "oneapi": "--oneapi-device",
}

# ``=== CUDA backend ===`` headers and ``  CUDA Device 0: NVIDIA L40S [Discrete GPU]`` device lines.
# The name is taken whole (any trailing ``[type]`` included): it is used only to group identical
# cards, never stamped on a result - the result's device string comes from clpeak's JSON - so as
# long as identical cards parse to the same string, grouping is correct.
_HEADER = re.compile(r"===\s*(\w+)\s*backend\s*===", re.IGNORECASE)
_DEVICE = re.compile(r"\bDevice\s+(\d+):\s*(.+?)\s*$")

# Devices clpeak enumerates and benchmarks that are not GPUs - the CPU, in one guise or another.
# Recording one publishes a CPU's compute as a graphics card's, which is how "llvmpipe" turned up
# as a GPU in the catalog. Caught two ways, because clpeak's two surfaces expose different things:
#
# - ``--list-devices`` tags each device with its type (``[CPU]``, ``[GPU]``, ``[Discrete GPU]``),
#   so any CPU runtime is caught by that whatever its device name - lavapipe on Vulkan, and on
#   OpenCL rusticl-on-llvmpipe, pocl, and Intel's CPU runtime, the last two of which report the
#   bare CPU brand string with no marker in it. This is the signal ``validate/gpuapi.py`` uses too.
# - the results JSON carries no type, only the name. There the software *rasterizers* are matched
#   by these markers (they are named for what they are); a CPU runtime that reports a plain brand
#   name is caught instead by ``software_device_names`` cross-referencing the enumeration's type.
SOFTWARE_DEVICE_MARKERS = ("llvmpipe", "swrast", "softpipe", "lavapipe", "swiftshader")

# ``llvmpipe (LLVM 17.0.6, 256 bits) [CPU]`` -> the name and the bracketed type clpeak appends.
_TYPE_SUFFIX = re.compile(r"^(?P<name>.*?)\s*\[(?P<type>[^\]]*)\]\s*$")


def _split_type(raw: str) -> tuple:
    """``('llvmpipe (LLVM ...)', 'CPU')`` from a ``--list-devices`` name; type ``''`` if none."""
    match = _TYPE_SUFFIX.match(raw or "")
    if match:
        return match.group("name").strip(), match.group("type").strip()
    return (raw or "").strip(), ""


def is_software_device(name: str) -> bool:
    """Whether a clpeak device is the CPU rather than a GPU.

    Reads the ``[type]`` clpeak's ``--list-devices`` appends where it is present (any ``CPU`` type),
    and otherwise - on the results JSON, which carries no type - falls back to the software-
    rasterizer marker names. The fallback cannot recognize a CPU runtime that reports a plain brand
    name; that case is handled by ``software_device_names`` cross-referencing the enumeration.
    """
    bare, type_str = _split_type(name)
    if "cpu" in type_str.lower():
        return True
    lowered = bare.lower()
    return any(marker in lowered for marker in SOFTWARE_DEVICE_MARKERS)


class Invocation(NamedTuple):
    """One clpeak run: the device-selector args to pass, and the ordinal to stamp on its results.

    ``ordinal`` is None in the default (deduped) plan - the device name alone identifies the model.
    Under ``--all-gpus`` it is the 0-based position of this card within its name group, so two
    identical cards become ordinal 0 and 1 and can be told apart downstream.
    """

    device_args: List[str]
    ordinal: Optional[int]


def list_devices(binary: str, run: Callable) -> Dict[str, List[tuple]]:
    """``{backend: [(index, name), ...]}`` from ``clpeak --list-devices``, or ``{}`` if unreadable.

    ``run`` is ``ctx.cmd``-shaped: it takes an argv and returns an object with ``ok`` and
    ``stdout``. Any failure or empty output returns ``{}``; the caller lets clpeak run every device.
    """
    try:
        res = run([binary, "--list-devices"])
    except Exception:  # noqa: BLE001 - enumeration must never be why a benchmark does not start
        return {}
    if not getattr(res, "ok", False):
        return {}
    devices: Dict[str, List[tuple]] = {}
    backend = None
    for raw in (res.stdout or "").splitlines():
        line = raw.strip()
        header = _HEADER.search(line)
        if header:
            backend = header.group(1).lower()
            continue
        if backend is None:
            continue
        match = _DEVICE.search(line)
        if match and not is_software_device(match.group(2)):
            # A CPU/software device is left out of the plan so it is never pinned and benchmarked
            # on a backend that can select devices. On a backend that cannot (OpenCL) it still runs,
            # and the curated figures drop it there; see ``benchmarks.gpu._figures``.
            devices.setdefault(backend, []).append((int(match.group(1)), match.group(2)))
    return devices


def software_device_names(binary: str, run: Callable) -> set:
    """The bare device names ``--list-devices`` reports as a CPU/software device.

    The results JSON carries no device type, so a figure produced on a CPU OpenCL runtime whose name
    is a plain brand string (pocl, Intel's) cannot be recognized from the result alone. This reads
    the enumeration, which does carry the type, and returns the names to drop - stripped of the
    ``[type]`` suffix so they match the JSON's bare device string. Empty when enumeration is
    unreadable, in which case the caller still drops the software rasterizers by their marker names.
    """
    try:
        res = run([binary, "--list-devices"])
    except Exception:  # noqa: BLE001 - enumeration must never be why a benchmark does not start
        return set()
    if not getattr(res, "ok", False):
        return set()
    names = set()
    for raw in (res.stdout or "").splitlines():
        line = raw.strip()
        if _HEADER.search(line):
            continue
        match = _DEVICE.search(line)
        if match and is_software_device(match.group(2)):
            names.add(_split_type(match.group(2))[0])
    return names


def plan(
    binary: str, run: Callable, backends: List[str], *, all_gpus: bool,
) -> Dict[str, List[Invocation]]:
    """Per backend, the clpeak invocations to make.

    Only the backends in ``backends`` are planned, and only those with a flat device selector are
    pinned; every other backend (and any that could not be enumerated) gets a single unpinned
    invocation, which runs every device - correct, just not deduped.
    """
    devices = list_devices(binary, run)
    out: Dict[str, List[Invocation]] = {}
    for backend in backends:
        devs = devices.get(backend)
        if not devs or backend not in BACKEND_DEVICE_FLAG:
            # Not enumerable, or a backend we do not pin (OpenCL/Metal): run every device once.
            out[backend] = [Invocation(device_args=[], ordinal=None)]
            continue
        out[backend] = _invocations(BACKEND_DEVICE_FLAG[backend], devs, all_gpus=all_gpus)
    return out


def _invocations(flag: str, devs: List[tuple], *, all_gpus: bool) -> List[Invocation]:
    seen: Dict[str, int] = {}
    invocations: List[Invocation] = []
    if not all_gpus:
        # One index per distinct name, all in a single invocation: identical cards benchmarked once.
        picked = []
        for index, name in devs:
            if name not in seen:
                seen[name] = 1
                picked.append(index)
        return [Invocation(device_args=[flag, ",".join(str(i) for i in picked)], ordinal=None)]
    # Every device on its own, with an ordinal within its name group, so identical cards are
    # benchmarked and reported separately.
    for index, name in devs:
        ordinal = seen.get(name, 0)
        seen[name] = ordinal + 1
        invocations.append(Invocation(device_args=[flag, str(index)], ordinal=ordinal))
    return invocations
