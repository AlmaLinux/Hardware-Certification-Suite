"""Building clpeak on the machine under test, with every backend its hardware supports."""

import io
import os
import tarfile

import pytest

from alma_certify import gpubuild

NV = {"pci_ids": {"vendor": "NVIDIA Corporation [10de]"}, "driver": "nvidia"}
AMD = {"pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]"}, "driver": "amdgpu"}
MATROX = {"pci_ids": {"vendor": "Matrox Electronics Systems Ltd. [102b]"}, "driver": "mgag200"}

# clpeak's real configure summary, copied from the pinned release's own CMakeLists rather than
# invented. The first version of this fixture used ``CLPEAK_ENABLE_CUDA: ON``, which CMake never
# prints, so the parser agreed with the fixture and both disagreed with clpeak.
CMAKE_OUT = """\
-- The CXX compiler identification is GNU 11.5.0
-- ===============================================================
-- clpeak backend summary
--
--   OpenCL : ENABLED
--   Vulkan : ENABLED  (1.4.328)
--   CUDA   : ENABLED  (Toolkit 13.3)
--   ROCm   : disabled (ROCm/HIP package not found)
--   Metal  : disabled (non-Apple host)
--   oneAPI : disabled (IntelSYCL not found - source setvars.sh then reconfigure)
--   CPU    : disabled (CLPEAK_ENABLE_CPU=OFF)
-- ===============================================================
-- Configuring done
"""


# --- what to install ------------------------------------------------------------


def test_the_base_dependencies_are_unconditional():
    """OpenCL has no find_package in clpeak, so a missing header is a build failure rather than a
    skipped backend. It cannot be treated as one more optional SDK."""
    packages = gpubuild.required_packages({"gpus": [MATROX]})

    for needed in ("cmake", "gcc-c++", "opencl-headers", "ocl-icd-devel", "glslc"):
        assert needed in packages


def test_an_nvidia_card_adds_the_cuda_toolkit():
    """Which is exactly what find_package(CUDAToolkit) looks for."""
    assert "cuda-toolkit" in gpubuild.required_packages({"gpus": [NV]})


def test_a_machine_with_no_nvidia_card_is_not_given_the_cuda_toolkit():
    """It would be a large download to enable a backend with no hardware to run it."""
    assert "cuda-toolkit" not in gpubuild.required_packages({"gpus": [AMD, MATROX]})


def test_two_vendors_get_both_sets():
    """A workstation with an NVIDIA card and an AMD one is a real machine, and the combos the
    maintainer described are the normal case rather than the exception."""
    packages = gpubuild.required_packages({"gpus": [NV, AMD]})

    assert "cuda-toolkit" in packages
    assert "opencl-headers" in packages


def test_the_list_has_no_duplicates():
    packages = gpubuild.required_packages({"gpus": [NV, NV]})

    assert len(packages) == len(set(packages))


# --- what got built -------------------------------------------------------------


def test_the_backends_come_from_cmakes_own_report():
    """Not from what we installed. An SDK present but too old, or a header without its library, are
    both cases where our guess and the truth part company."""
    assert sorted(gpubuild.enabled_backends(CMAKE_OUT)) == ["cuda", "opencl", "vulkan"]


def test_a_backend_reported_off_is_not_claimed():
    assert "rocm" not in gpubuild.enabled_backends(CMAKE_OUT)
    assert "oneapi" not in gpubuild.enabled_backends(CMAKE_OUT)


def test_nothing_is_claimed_from_output_that_says_nothing():
    """A reformatting upstream must empty this list rather than fill it with wishes."""
    assert gpubuild.enabled_backends("-- Configuring done\n") == []


def test_every_backend_flag_has_a_summary_name():
    """The two have to agree: a backend detected and then run with the wrong flag would measure
    something other than what was built, and one with no summary name can never be detected."""
    for backend in gpubuild.BACKEND_FLAGS:
        name = gpubuild._SUMMARY_NAMES.get(backend)
        assert name, backend
        assert gpubuild.enabled_backends("--   %s : ENABLED" % name) == [backend]


def test_the_option_names_are_not_the_summary_names():
    """The trap this parser fell into. ``CLPEAK_ENABLE_ROCM`` is the option; the summary says
    ``ROCm``. CMake never echoes the options, and only the *disabled* lines mention one at all,
    inside their parenthetical, so matching options found every disabled backend and no enabled one:
    a good build reported having none and the benchmark errored.
    """
    assert gpubuild.enabled_backends("-- CLPEAK_ENABLE_CUDA: ON") == []
    assert gpubuild._SUMMARY_NAMES["rocm"] == "ROCm"
    assert gpubuild._SUMMARY_NAMES["oneapi"] == "oneAPI"


def test_a_disabled_line_mentioning_its_option_is_not_read_as_enabled():
    """``CPU : disabled (CLPEAK_ENABLE_CPU=OFF)`` contains the option name and the word OFF, and the
    ROCm and Vulkan disabled lines are the same shape."""
    assert gpubuild.enabled_backends(
        "--   ROCm   : disabled (ROCm/HIP package not found)\n"
        "--   CPU    : disabled (CLPEAK_ENABLE_CPU=OFF)\n"
    ) == []


# --- the source ------------------------------------------------------------------


def test_no_bundled_source_is_a_packaging_gap_not_a_machine_problem(monkeypatch, tmp_path):
    monkeypatch.setattr(gpubuild, "_DATA_DIR", str(tmp_path))

    assert gpubuild.source_archive() is None


def test_the_newest_bundled_release_is_used(monkeypatch, tmp_path):
    for version in ("2.0.9", "2.0.18"):
        (tmp_path / ("clpeak-%s.tar.gz" % version)).write_bytes(b"")
    monkeypatch.setattr(gpubuild, "_DATA_DIR", str(tmp_path))

    assert gpubuild.source_archive().endswith("clpeak-2.0.18.tar.gz")


def test_the_version_is_recorded_from_the_archive_name():
    assert gpubuild.source_version("/x/clpeak-2.0.18.tar.gz") == "2.0.18"
    assert gpubuild.source_version("/x/clpeak.tar.gz") == "unknown"


def test_a_tarball_cannot_write_outside_the_build_directory(tmp_path):
    """A tarball is data from outside this program, and the suite runs as root. ``extractall`` would
    happily follow ``../`` out of the build tree."""
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("../escaped")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(archive) as tar, pytest.raises(tarfile.TarError):
        gpubuild._safe_extract(tar, str(dest))

    assert not (tmp_path / "escaped").exists()


def test_an_ordinary_tarball_extracts(tmp_path):
    archive = tmp_path / "clpeak-1.0.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("clpeak-1.0/CMakeLists.txt")
        payload = b"project(clpeak)\n"
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(archive) as tar:
        gpubuild._safe_extract(tar, str(dest))

    assert (dest / "clpeak-1.0" / "CMakeLists.txt").exists()


class _Recorder:
    """Just enough TarFile for ``_safe_extract``, so the call can be inspected."""

    def __init__(self):
        self.kwargs = None

    def getmembers(self):
        info = tarfile.TarInfo("clpeak-1.0/CMakeLists.txt")
        info.size = 0
        return [info]

    def extractall(self, dest, **kwargs):
        self.kwargs = kwargs


def test_the_extraction_filter_is_asked_for_by_name():
    """Reported from an installed RPM: 3.12 and 3.13 print a ``RuntimeWarning`` about the extraction
    filter for a bare ``extractall``, and it landed on the user's terminal in the middle of a run.

    Asserted on the call rather than on the absence of a warning, which was the first attempt and
    had no teeth: 3.14 made ``data`` the default and dropped the warning, so on a modern
    interpreter, removing the argument entirely still emitted nothing and the test still passed. The
    argument is what has to survive, because the interpreters that warn are the ones users have.
    """
    tar = _Recorder()

    gpubuild._safe_extract(tar, "/tmp/nowhere")

    if hasattr(tarfile, "data_filter"):
        assert tar.kwargs == {"filter": "data"}
    else:
        # 3.9.16 and older have no such argument, and passing it is a TypeError.
        assert tar.kwargs == {}


def test_an_executable_stays_executable(tmp_path):
    """The ``data`` filter clears some mode bits, and a source tree whose configure script arrived
    without its executable bit would fail the build for a reason nobody would look for here."""
    archive = tmp_path / "clpeak-1.0.tar"
    with tarfile.open(archive, "w") as tar:
        info = tarfile.TarInfo("clpeak-1.0/tools/gen.sh")
        info.mode = 0o755
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    dest = tmp_path / "dest"
    dest.mkdir()

    with tarfile.open(archive) as tar:
        gpubuild._safe_extract(tar, str(dest))

    assert os.access(dest / "clpeak-1.0" / "tools" / "gen.sh", os.X_OK)


def test_a_disabled_backend_is_reported_with_the_reason_cmake_gave():
    """"Which APIs ran" is only half an answer on a machine with an AMD card and no ROCm, and clpeak
    states the reason itself. ``ROCm/HIP package not found`` is actionable; the absence of a line is
    not."""
    disabled = gpubuild.disabled_backends(CMAKE_OUT)

    assert disabled["rocm"] == "ROCm/HIP package not found"
    assert disabled["oneapi"].startswith("IntelSYCL not found")
    for enabled in ("opencl", "cuda", "vulkan"):
        assert enabled not in disabled


def test_the_backends_this_suite_would_not_run_are_not_called_gaps():
    """clpeak also reports CPU, which is off by our own ``-DCLPEAK_ENABLE_CPU=OFF``, and Metal,
    which needs an Apple host. Naming either as something missing would be noise."""
    disabled = gpubuild.disabled_backends(CMAKE_OUT)

    assert "cpu" not in disabled
    assert "metal" not in disabled


def test_a_build_carries_both_lists(monkeypatch, tmp_path):
    """Through the real ``build``, because the two lists are what the run log and the report are
    made of, and a build that collected only one of them would leave a machine with no ROCm
    indistinguishable from a machine whose ROCm failed."""
    archive = tmp_path / "clpeak-2.0.18.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("clpeak-2.0.18/CMakeLists.txt")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    monkeypatch.setattr(gpubuild, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gpubuild, "opencl_hpp_missing", lambda: False)
    monkeypatch.setattr(gpubuild, "_find_binary", lambda build_dir: "/tmp/clpeak")

    class Ctx:
        def cmd(self, argv, timeout=None, artifact=None):
            class R:
                ok = True
                stdout = CMAKE_OUT
                stderr = ""

            return R()

        def rel_artifact(self, name):
            return name

    built = gpubuild.build(Ctx())

    assert sorted(built.backends) == ["cuda", "opencl", "vulkan"]
    assert built.disabled["rocm"] == "ROCm/HIP package not found"
    assert built.version == "2.0.18"


# --- the configure command -------------------------------------------------------


def test_the_cpu_backend_is_off_at_configure_time(monkeypatch, tmp_path):
    """Off at configure time rather than filtered out afterwards: this is a GPU benchmark, and a CPU
    figure in its output is noise a reader has to know to discard."""
    archive = tmp_path / "clpeak-2.0.18.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("clpeak-2.0.18/CMakeLists.txt")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    monkeypatch.setattr(gpubuild, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gpubuild, "opencl_hpp_missing", lambda: False)

    calls = []

    class Ctx:
        def cmd(self, argv, timeout=None, artifact=None):
            calls.append(argv)

            class R:
                ok = False
                stdout = ""
                stderr = ""

            return R()

        def rel_artifact(self, name):
            return name

    gpubuild.build(Ctx())

    assert calls, "cmake should have been invoked"
    assert "-DCLPEAK_ENABLE_CPU=OFF" in calls[0]
    assert not any(
        arg.startswith("-DCLPEAK_ENABLE_") and arg != "-DCLPEAK_ENABLE_CPU=OFF"
        for arg in calls[0]
    ), "every other backend must be left at its default so clpeak's own detection decides"


# --- the trap that would have made this suite download things -------------------


def test_a_missing_opencl_header_disables_opencl_rather_than_refusing(monkeypatch, tmp_path):
    """The sharpest trap in building clpeak, and a silent one.

    Its OpenCL backend includes ``<CL/opencl.hpp>``. When CMake cannot find that header it does not
    fail: it falls through to a module that git-clones the Khronos OpenCL SDK at an unpinned
    ``main`` and builds it during configure. That would give this suite a network dependency it has
    nowhere else, on a moving branch, and on an offline machine it hard-fails later with an error
    about OpenCL rather than about the network.

    AlmaLinux 10 ships the header. AlmaLinux 8 and 9 do not. So the build is not refused there: it
    configures with ``CLPEAK_ENABLE_OPENCL=OFF``, which makes clpeak's OpenCL CMakeLists return
    before that git-clone while Vulkan, CUDA, and ROCm still build. That is how an el9 AMD card gets
    a Vulkan number instead of no benchmark at all.
    """
    archive = tmp_path / "clpeak-2.0.18.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        info = tarfile.TarInfo("clpeak-2.0.18/CMakeLists.txt")
        info.size = 0
        tar.addfile(info, io.BytesIO(b""))
    monkeypatch.setattr(gpubuild, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gpubuild, "_OPENCL_HPP_PATHS", (str(tmp_path / "absent.hpp"),))

    calls = []

    class Ctx:
        def cmd(self, argv, timeout=None, artifact=None):
            calls.append(argv)

            class R:
                ok = False
                stdout = ""
                stderr = ""

            return R()

        def rel_artifact(self, name):
            return name

    # Configure runs (the build is not refused), and it turns OpenCL off so the git-clone that a
    # missing header would otherwise trigger is never reached.
    gpubuild.build(Ctx())
    assert calls, "configure should run rather than the whole build being refused"
    assert "-DCLPEAK_ENABLE_OPENCL=OFF" in calls[0]


def test_the_header_being_present_is_what_allows_it(monkeypatch, tmp_path):
    header = tmp_path / "opencl.hpp"
    header.write_text("// present\n")
    monkeypatch.setattr(gpubuild, "_OPENCL_HPP_PATHS", (str(header),))

    assert gpubuild.opencl_hpp_missing() is False


# --- ROCm costs a fraction of what it looks like --------------------------------


def test_rocm_asks_only_for_hip_and_hipcc():
    """The backend's gate is hip plus hipcc. rocBLAS and hipBLASLt are dlopen'd at run time rather
    than linked, and only switch on extra GEMM figures, so asking for them would turn a few hundred
    megabytes into about 2.5 GB on somebody else's machine for tests nobody requested."""
    packages = gpubuild.required_packages({"gpus": [AMD]})

    assert "rocm-hip-devel" in packages
    assert "hipcc" in packages
    for expensive in ("rocblas", "rocblas-devel", "hipblaslt", "hipblaslt-devel", "miopen"):
        assert expensive not in packages


def test_an_nvidia_only_machine_is_not_given_rocm():
    assert "rocm-hip-devel" not in gpubuild.required_packages({"gpus": [NV]})


# --- the packaging step is a single pinned command ------------------------------


def _spec_text():
    """The spec, or a skip. The source tarball leaves packaging/ out and this suite runs from that
    tarball in the package's own %check, so a test that reads the spec would otherwise fail the
    build over a file the build deliberately did not ship."""
    import pathlib

    path = (pathlib.Path(__file__).resolve().parent.parent
            / "packaging" / "alma-certify.spec")
    if not path.is_file():
        pytest.skip("no spec here; this runs from a source tarball that omits packaging/")
    return path.read_text(encoding="utf-8")


def test_one_clpeak_version_stated_in_one_place():
    """One version, in one place, fetched at packaging time. A build must never reach for the
    network on its own, and a benchmark built against whatever a branch said that day is not
    comparable with one built last month.

    That place is the spec, because the spec is what rpmbuild obeys. The Makefile used to state it
    too, and the two drifted: the spec moved to 2.1.4 while the Makefile still said 2.0.18, so
    `make` fetched one tarball and rpmbuild then failed asking for another. The Makefile derives
    both the version and the URL from the spec now, so there is nothing left to keep in step.
    """
    import pathlib
    import re

    spec = _spec_text()
    root = pathlib.Path(__file__).resolve().parent.parent
    makefile = (root / "Makefile").read_text(encoding="utf-8")

    version = re.search(r"^%global clpeak_version (\S+)$", spec, re.M)
    assert version, "the spec has to pin the version"
    assert re.match(r"^\d+\.\d+", version.group(1)), version.group(1)
    assert "clpeak-%{clpeak_version}.tar.gz" in spec, "Source1 must use that macro, not a literal"

    assert not re.search(r"^CLPEAK_VERSION :?= ", makefile, re.M), (
        "the Makefile must not state a version of its own; that is what drifted"
    )
    assert "rpmspec -P $(SPEC)" in makefile and "Source1" in makefile, (
        "the Makefile has to read Source1 out of the spec"
    )


def test_the_installed_name_is_the_one_the_suite_looks_for():
    """The two halves meet on a filename, so a mismatch would ship a tarball the run cannot see.

    The spec is what puts it on the machine, into alma_certify/data/ beside the code that globs for
    it. Where the *build* stages it no longer matters to the run: it is Source1 now, and rpmbuild
    takes it from the source directory rather than from the source tree.
    """
    import re

    spec = _spec_text()

    installed = re.search(
        r"%\{buildroot\}%\{alma_certify_home\}/alma_certify/data/(clpeak-\S+\.tar\.gz)", spec
    )
    assert installed, "the spec must install clpeak where the suite looks for it"
    name = installed.group(1).replace("%{clpeak_version}", "2.1.4")

    # ``source_archive`` globs for exactly this shape, and ``source_version`` parses it.
    assert gpubuild._SOURCE_GLOB.replace("*", "2.1.4") == name
    assert gpubuild.source_version(name) == "2.1.4"


def test_the_skip_says_what_to_do_rather_than_only_what_is_wrong(monkeypatch, tmp_path):
    """It is a packaging gap, and the person reading the log is the person who can close it."""
    from alma_certify.benchmarks import gpu as gpu_bench
    from alma_certify.config import Config
    from alma_certify.registry import RunContext

    monkeypatch.setattr(gpubuild, "_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(gpu_bench, "opencl_icds", lambda: ["/etc/OpenCL/vendors/x.icd"])
    ctx = RunContext(
        Config.load("/nonexistent"), str(tmp_path),
        inventory={"summary": {"gpus": [NV]}},
    )

    reason = gpu_bench.Clpeak().applicable(ctx)

    # The target has to be the one that puts it where source_archive() looks. clpeak-source
    # stages into the RPM's source directory instead, so naming that one sent people to a command
    # that fetched the tarball and left the skip in place.
    assert "make clpeak-in-tree" in reason
    assert "Nothing about this machine" in reason


def test_a_package_build_fetches_the_source_rather_than_warning_about_it():
    """A packaging step that has to be remembered will be forgotten.

    It was a separate ``make clpeak-source`` with a warning if skipped, and the first person to skip
    it shipped a package whose GPU compute benchmark silently skipped: the warning scrolled past
    inside an rpmbuild and the run said only that the source was missing.

    ``tarball`` therefore depends on the tarball as a real file target, so ``srpm`` and ``rpm``
    inherit it and a build that already has it does not refetch.
    """
    import pathlib
    import re

    makefile = pathlib.Path(__file__).resolve().parent.parent / "Makefile"
    text = makefile.read_text()

    # A file target, not a phony one: that is what makes it both automatic and idempotent.
    assert re.search(r"^\$\(CLPEAK_TARBALL\):$", text, re.M), (
        "the tarball must be a file target so make can decide whether it is needed"
    )
    assert re.search(r"^tarball: \$\(CLPEAK_TARBALL\)$", text, re.M), (
        "tarball must depend on it rather than warn about it"
    )
    # Both sources have to land where rpmbuild looks, which is what makes `make srpm` work from a
    # clean tree. Staging only one of them was the other half of the breakage.
    assert re.search(r"^TARBALL := \$\(SOURCEDIR\)/", text, re.M)
    assert re.search(r"^CLPEAK_TARBALL := \$\(SOURCEDIR\)/", text, re.M)
    # No build-without-it switch: clpeak is Source1, and rpmbuild refuses to start with a source
    # missing, so the old CLPEAK_SOURCE=none would fail the build rather than degrade it. An
    # offline host pre-places the tarball instead, which the file target above already allows.
    assert "CLPEAK_SOURCE" not in text


def test_the_checksum_file_covers_the_pinned_version():
    """A pinned version is only a pin if the bytes behind it are pinned too.

    The two are edited separately - the version in the spec, the hash in packaging/sources.sha512 -
    so a bump that updates one and not the other would either fetch a new tarball and check it
    against the old hash, or check nothing at all. `sha512sum -c --ignore-missing` is quiet about a
    file it has no line for, which is the failure mode that would pass unnoticed.
    """
    import pathlib
    import re

    spec = _spec_text()
    root = pathlib.Path(__file__).resolve().parent.parent
    sums_path = root / "packaging" / "sources.sha512"
    if not sums_path.is_file():
        pytest.skip("no checksum file here; this runs from a tarball that omits packaging/")

    version = re.search(r"^%global clpeak_version (\S+)$", spec, re.M).group(1)
    sums = sums_path.read_text(encoding="utf-8")

    expected = "clpeak-%s.tar.gz" % version
    assert expected in sums, (
        "packaging/sources.sha512 has no line for %s; a version bump left the checksum behind"
        % expected
    )
    line = next(ln for ln in sums.splitlines() if expected in ln and not ln.startswith("#"))
    digest = line.split()[0]
    assert re.fullmatch(r"[0-9a-f]{128}", digest), "not a sha512: %r" % digest


def test_the_fetch_refuses_a_tarball_that_does_not_match():
    """The whole point of recording the hash. Verified against the Makefile rather than by running
    a download, so it holds without a network."""
    import pathlib
    import re

    makefile = (pathlib.Path(__file__).resolve().parent.parent / "Makefile").read_text()

    assert "sha512sum -c" in makefile, "the fetch must check what it downloaded"
    fetch = makefile.split("$(CLPEAK_TARBALL):")[1].split("\n\n")[0]
    assert "rm -f $(CLPEAK_TARBALL)" in fetch, (
        "a tarball that fails its checksum has to be removed, or the next run reuses it"
    )
    assert re.search(r"exit 1", fetch), "and the build has to stop"


def test_the_spec_says_which_architectures_are_supported():
    """x86_64 and aarch64 only, stated where the build system reads it.

    Not a preference: dmidecode has no ppc64le or s390x build in AlmaLinux, and it is a hard
    Requires because the suite reads SMBIOS to identify the machine it is certifying. Without
    ExclusiveArch the build system would happily build this for all four and ship a package that
    cannot resolve its own dependencies on two of them.
    """
    import re

    spec = _spec_text()

    match = re.search(r"^ExclusiveArch:\s+(.+)$", spec, re.M)
    assert match, "the spec must say which architectures it is for"
    arches = match.group(1).split()
    assert arches == ["x86_64", "aarch64"], arches
    assert "dmidecode" in spec, "the dependency that makes it true has to still be there"
