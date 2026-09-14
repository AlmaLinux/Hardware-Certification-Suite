"""Build clpeak on the machine under test, with every backend its hardware supports.

clpeak is in no EPEL or AlmaLinux repository, so the suite's maintainers provide it either way. A
prebuilt package is the wrong shape for it: its backends are compiled in, and which ones are useful
depends on the card in the machine, so one binary would either omit CUDA on an NVIDIA box or carry
SDK dependencies no AMD box can satisfy. Building on the target settles that by construction.

**Backend selection is almost all clpeak's own, with one hand from us.** Every
``CLPEAK_ENABLE_<BACKEND>`` option defaults to ON, and all of them except OpenCL are auto-detected
by clpeak's own CMake and silently disabled when their SDK is absent. So "build every backend
possible" is the default, and our job is mostly to install the SDKs that make a backend detectable,
then get out of the way.

Three backends need care, and all of them matter:

- **OpenCL has no ``find_package`` and its header is not on every release.** clpeak's OpenCL
  directory, when it cannot find ``CL/opencl.hpp``, git-clones OpenCL-SDK at an unpinned ``main``
  during configure (see ``opencl_hpp_missing``). AlmaLinux 10 ships the header in
  ``opencl-headers``; 8 and 9 do not. So OpenCL is left ON where the header is present and OFF where
  it is not:
  off, ``src/opencl/CMakeLists.txt`` returns before that fetch, and Vulkan, CUDA, and ROCm still
  build. This is what gives an el9 AMD card a Vulkan number instead of no benchmark at all. The
  headers and the ICD loader (``ocl-icd-devel``, in CRB) are installed for the case where OpenCL is
  on.
- **Vulkan is unconditional in practice.** ``find_package(Vulkan QUIET)`` succeeding is enough to
  enter clpeak's Vulkan directory, and its shader step then calls ``FATAL_ERROR`` when ``glslc`` is
  missing. The Vulkan headers and loader are in AppStream, enabled by default, so Vulkan is found on
  essentially every machine. ``glslc`` is in EPEL on 9 and 10 but not on 8, so the whole build
  still fails there: 8 gets no clpeak benchmark, which is the honest outcome, not a regression.
- **The CPU backend is turned off at configure time**, not filtered out afterwards. This is a GPU
  benchmark and a CPU figure in its output is noise a reader has to know to discard.

The source is bundled rather than downloaded. Nothing else in this suite fetches anything at run
time, deliberately, and a benchmark whose result depends on what a mirror served that day is not
comparable between machines. Drop a release tarball into ``alma_certify/data`` at packaging time;
when it is absent the benchmark skips and says so.
"""

from __future__ import annotations

import glob
import os
import re
import shutil
import tarfile
import tempfile
from typing import Any, Dict, List, NamedTuple, Optional, Sequence

_DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
# Any pinned release; the newest is used when several are present. Packaging drops one in.
_SOURCE_GLOB = "clpeak-*.tar.gz"

# What must be installed for the build to succeed at all.
#
# ``cmake`` and a C++ compiler for obvious reasons. The OpenCL pair because clpeak's OpenCL backend
# has no detection and cannot be skipped. The Vulkan trio because Vulkan is the one backend that is
# useful on every vendor's hardware, so it is worth having unconditionally rather than only where a
# vendor SDK happens to be present.
#
# ``glslc`` compiles the Vulkan compute shaders. It is the build dependency most likely to be
# somewhere awkward, which is why it is named rather than assumed.
BASE_BUILD_PACKAGES = (
    "cmake",
    "gcc-c++",
    "opencl-headers",
    "ocl-icd-devel",
    "vulkan-headers",
    "vulkan-loader-devel",
    "glslc",
)

# What a *built* clpeak needs to find any device at all. ``mesa-vulkan-drivers`` (AppStream) puts
# the Intel and Radeon Vulkan ICDs on the machine; without it a UHD 630 skipped the whole benchmark
# for want of an ICD. Harmless on NVIDIA, whose ICD comes with the driver.
RUNTIME_PACKAGES: Sequence[str] = ("mesa-vulkan-drivers", "vulkan-loader")

# The OpenCL runtime per vendor, which is a different question from the build headers below.
#
# Intel's is ``intel-opencl`` in EPEL, which drops ``/etc/OpenCL/vendors/intel.icd``. NVIDIA's comes
# with the driver stack that ``setup-gpu`` installs, and AMD's with ROCm, so neither needs naming
# here.
VENDOR_RUNTIME_PACKAGES: Dict[str, Sequence[str]] = {
    "8086": ("intel-opencl",),
}

# Per-vendor SDKs, keyed by PCI vendor id, that turn a backend from undetected into compiled in.
# Intel is absent on purpose: clpeak's oneAPI backend needs an IntelLLVM compiler, and Intel
# graphics are covered through OpenCL and Vulkan without it.
VENDOR_BUILD_PACKAGES: Dict[str, Sequence[str]] = {
    # What ``find_package(CUDAToolkit)`` looks for, and what ``setup-gpu`` already installs.
    "10de": ("cuda-toolkit",),
    # HIP and hipcc only: the ROCm backend's gate is ``hip_FOUND AND CLPEAK_HIPCC_FOUND``, and
    # rocBLAS and hipBLASLt are dlopen'd for extra GEMM figures, so asking for them would turn a few
    # hundred megabytes into about 2.5 GB for tests nobody asked for. EPEL carries ROCm on
    # AlmaLinux 10 x86_64 only; elsewhere ``missing`` reports these unavailable, which for a vendor
    # SDK is a note rather than a skip, and AMD's own repository is never added.
    "1002": ("rocm-hip-devel", "hipcc"),
}

# Which clpeak flag runs which backend on its own. Used one at a time, see ``run_backends``.
BACKEND_FLAGS = {
    "opencl": "--opencl",
    "cuda": "--cuda",
    "vulkan": "--vulkan",
    "rocm": "--rocm",
    "oneapi": "--oneapi",
}


class Built(NamedTuple):
    binary: str
    backends: List[str]
    version: str
    # What CMake declined to build and the reason it gave, keyed by backend. Kept because "which
    # APIs ran" is only half an answer on a machine with an AMD card and no ROCm: the other half is
    # which ones did not and why, and clpeak states the reason itself.
    disabled: Dict[str, str] = {}


def _version_key(archive: str):
    """Sort key for a release archive, numerically.

    Lexically, ``clpeak-2.0.9`` sorts above ``clpeak-2.0.18``, so a packaging update that left both
    in place would silently keep building the older one. The same trap as picking a CUDA prefix, and
    the same fix.
    """
    parts = []
    for chunk in source_version(archive).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(-1)
    return parts


def source_archive() -> Optional[str]:
    """The bundled clpeak source, newest by version, or None when packaging left none behind."""
    found = sorted(glob.glob(os.path.join(_DATA_DIR, _SOURCE_GLOB)), key=_version_key)
    return found[-1] if found else None


def source_version(archive: str) -> str:
    """The version out of the archive name, for the record. ``clpeak-2.0.18.tar.gz`` -> 2.0.18."""
    match = re.search(r"clpeak-(.+?)\.tar\.gz$", os.path.basename(archive))
    return match.group(1) if match else "unknown"


def required_packages(summary: Dict[str, Any]) -> List[str]:
    """Everything to install so clpeak's CMake finds every backend this hardware can use.

    The hardware decides only the *vendor* additions. The base list is unconditional, because
    OpenCL cannot be skipped and Vulkan is useful on every vendor.
    """
    packages = list(BASE_BUILD_PACKAGES) + list(RUNTIME_PACKAGES)
    for vendor_id in sorted(gpu_vendor_ids(summary)):
        for package in (
            tuple(VENDOR_BUILD_PACKAGES.get(vendor_id, ()))
            + tuple(VENDOR_RUNTIME_PACKAGES.get(vendor_id, ()))
        ):
            if package not in packages:
                packages.append(package)
    return packages


# How clpeak's own configure summary names each backend, from the pinned release's source:
#
#     --   OpenCL : ENABLED
#     --   ROCm   : disabled (ROCm/HIP package not found)
#
# Matched on these names, not the ``CLPEAK_ENABLE_*`` options: CMake echoes an option only inside a
# disabled line's parenthetical, so matching options found no enabled backend at all.
_SUMMARY_NAMES = {
    "opencl": "OpenCL",
    "vulkan": "Vulkan",
    "cuda": "CUDA",
    "rocm": "ROCm",
    "oneapi": "oneAPI",
}


def disabled_backends(configure_output: str) -> Dict[str, str]:
    """Which backends CMake declined, and the reason it printed, keyed by backend.

    Its own words in the parenthetical: ``ROCm : disabled (ROCm/HIP package not found)`` says
    something specific and actionable, and an operator who wanted a ROCm number is owed it rather
    than being left to read a build log for the absence of a line.

    Only the backends this suite would run. clpeak also reports CPU, which is off by our own
    ``-DCLPEAK_ENABLE_CPU=OFF``, and Metal, which needs an Apple host: naming either as a gap would
    be noise.
    """
    found = {}
    for backend, name in _SUMMARY_NAMES.items():
        match = re.search(
            r"\b%s\s*:\s*disabled\s*\(([^)]*)\)" % re.escape(name), configure_output,
        )
        if match:
            found[backend] = match.group(1).strip()
    return found


def enabled_backends(configure_output: str) -> List[str]:
    """The backends clpeak's configure summary says it enabled.

    From its own report rather than inferred from what we installed, because the authority on what
    got compiled in is the thing that compiled it. An SDK present but too old, or a header without
    its library, are both cases where our guess and the truth part company.
    """
    found = []
    for backend, name in _SUMMARY_NAMES.items():
        # ``ENABLED`` is upper case in the summary and ``disabled`` is not, so the case matters and
        # is deliberately not folded away.
        if re.search(r"\b%s\s*:\s*ENABLED\b" % re.escape(name), configure_output):
            found.append(backend)
    return found


# Where the OpenCL C++ binding header lives when a distribution ships it.
_OPENCL_HPP_PATHS = ("/usr/include/CL/opencl.hpp", "/usr/local/include/CL/opencl.hpp")


def vulkan_icds() -> List[str]:
    """The installed Vulkan driver manifests.

    The counterpart of ``opencl_icds``, and its absence is the reason a machine with a working Intel
    Vulkan driver was told it had no GPU compute runtime: the gate asked about OpenCL and CUDA and
    never about Vulkan, so clpeak's Vulkan backend was never given the chance to find the card.
    """
    return sorted(glob.glob("/usr/share/vulkan/icd.d/*.json"))


def opencl_hpp_missing() -> bool:
    """Whether ``CL/opencl.hpp`` is absent, which decides whether the OpenCL backend is built.

    This is the sharpest trap in building clpeak, and it is silent. Its OpenCL backend includes
    ``<CL/opencl.hpp>``, and when CMake cannot find that header it does not fail: it falls through
    to a module that **git-clones https://github.com/KhronosGroup/OpenCL-SDK at an unpinned
    ``main`` and builds it during the configure step**.

    Two things wrong with letting that happen. It downloads at run time, which nothing else in this
    suite does, and a benchmark built against whatever that branch said today is not comparable with
    one built last month. And on an offline machine it does not degrade, it hard-fails at a later
    ``find_package(OpenCL REQUIRED)`` with an error about OpenCL rather than about the network.

    AlmaLinux 10 ships the header in ``opencl-headers``. AlmaLinux 9 does not, and nothing in 9 or
    EPEL 9 provides it. So rather than refuse the whole build there, ``build`` turns the OpenCL
    backend off when this is true (``CLPEAK_ENABLE_OPENCL=OFF``), which makes clpeak's OpenCL
    ``CMakeLists`` return before the git-clone and lets Vulkan, CUDA, and ROCm build regardless.
    """
    return not any(os.path.exists(path) for path in _OPENCL_HPP_PATHS)


def build(ctx, *, jobs: Optional[int] = None) -> Optional[Built]:
    """Configure and build clpeak. None when it could not be built.

    ``CLPEAK_ENABLE_CPU=OFF`` always, and ``CLPEAK_ENABLE_OPENCL=OFF`` where ``CL/opencl.hpp`` is
    absent (see ``opencl_hpp_missing``); every other backend is left at its default of ON so
    clpeak's own detection decides, which is the whole point.
    """
    archive = source_archive()
    if archive is None:
        return None
    workdir = tempfile.mkdtemp(prefix="alma-certify-clpeak-")
    try:
        with tarfile.open(archive) as tar:
            _safe_extract(tar, workdir)
    except (tarfile.TarError, OSError):
        return None
    roots = [
        entry for entry in sorted(glob.glob(os.path.join(workdir, "*")))
        if os.path.isdir(entry)
    ]
    if not roots:
        return None
    source = roots[0]
    build_dir = os.path.join(workdir, "build")
    os.makedirs(build_dir, exist_ok=True)

    configure_args = ["cmake", "-S", source, "-B", build_dir,
                      "-DCMAKE_BUILD_TYPE=Release", "-DCLPEAK_ENABLE_CPU=OFF"]
    if opencl_hpp_missing():
        # No CL/opencl.hpp on this release (AlmaLinux 8 and 9): turn the OpenCL backend off rather
        # than let it git-clone OpenCL-SDK at an unpinned main during configure (see
        # ``opencl_hpp_missing``). clpeak's OpenCL CMakeLists returns before that fetch when the
        # option is off, so Vulkan, CUDA, and ROCm still build - an el9 AMD card gets Vulkan.
        configure_args.append("-DCLPEAK_ENABLE_OPENCL=OFF")

    configure = ctx.cmd(
        configure_args,
        timeout=900,
        artifact="clpeak-cmake.log",
    )
    if not configure.ok:
        return None
    compile_res = ctx.cmd(
        ["cmake", "--build", build_dir, "--parallel", str(jobs or (os.cpu_count() or 2))],
        timeout=3600,
        artifact="clpeak-build.log",
    )
    if not compile_res.ok:
        return None
    binary = _find_binary(build_dir)
    if binary is None:
        return None
    summary = configure.stdout + configure.stderr
    return Built(
        binary=binary,
        backends=enabled_backends(summary),
        version=source_version(archive),
        disabled=disabled_backends(summary),
    )


def _find_binary(build_dir: str) -> Optional[str]:
    for candidate in sorted(glob.glob(os.path.join(build_dir, "**", "clpeak"), recursive=True)):
        if os.access(candidate, os.X_OK) and not os.path.isdir(candidate):
            return candidate
    return None


def _safe_extract(tar: tarfile.TarFile, dest: str) -> None:
    """Extract without letting a member escape ``dest``.

    A tarball is data from outside this program, and ``extractall`` will happily write through
    ``../`` or an absolute path. The suite runs as root, so that is not a theoretical concern.

    Two layers, because neither covers every Python the suite runs on. The path check below holds
    everywhere. ``filter="data"`` additionally refuses device nodes, links pointing out of the tree,
    and setuid bits, and it is what silences the ``RuntimeWarning`` that 3.12 and newer print for a
    bare ``extractall``: the warning was reaching users on stderr mid-run, from an installed RPM.
    The argument only exists from 3.12 and the security backports (3.9.17, 3.10.12, 3.11.4), and the
    3.9 floor means an older one is possible, so it is passed only when this interpreter has it.
    """
    dest = os.path.realpath(dest)
    for member in tar.getmembers():
        target = os.path.realpath(os.path.join(dest, member.name))
        if not (target == dest or target.startswith(dest + os.sep)):
            raise tarfile.TarError("archive member escapes the destination: %s" % member.name)
    if hasattr(tarfile, "data_filter"):
        tar.extractall(dest, filter="data")  # noqa: S202 - members checked above
    else:
        tar.extractall(dest)  # noqa: S202 - members checked above


def cleanup(built: Built) -> None:
    """Remove the build tree. The binary lives under it, so this happens after the run."""
    root = built.binary
    for _ in range(6):
        root = os.path.dirname(root)
        if os.path.basename(root).startswith("alma-certify-clpeak-"):
            shutil.rmtree(root, ignore_errors=True)
            return


def gpu_vendor_ids(summary: Dict[str, Any]) -> set:
    """The PCI vendor ids of the GPUs present.

    Here rather than imported from the validation module, because this asks about every vendor and
    that module asks only about NVIDIA. Both read ``pci_ids`` through the same helper.
    """
    from . import hwquery

    return {
        hwquery.gpu_vendor_id(gpu)
        for gpu in hwquery.gpus(summary)
        if hwquery.gpu_vendor_id(gpu)
    }
