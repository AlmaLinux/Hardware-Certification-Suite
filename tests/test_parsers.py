"""Parser unit tests using captured tool output fixtures."""

import pytest

from alma_certify.inventory.cpu import _flatten_lscpu_json, _parse_lscpu_text
from alma_certify.inventory.dmi import parse_dmidecode
from alma_certify.inventory.normalize import (
    _dimm_bytes,
    build_summary,
)
from alma_certify.inventory.pci import class_id, parse_lspci_vmm
from alma_certify.validate.kernel import TAINT_BITS, load_patterns

DMIDECODE_SAMPLE = """\
# dmidecode 3.5
Getting SMBIOS data from sysfs.
SMBIOS 3.4.0 present.

Handle 0x0001, DMI type 1, 27 bytes
System Information
\tManufacturer: Dell Inc.
\tProduct Name: PowerEdge R760
\tSerial Number: ABC1234
\tUUID: 4c4c4544-0042-3510-8043-c2c04f313233

Handle 0x0400, DMI type 4, 48 bytes
Processor Information
\tSocket Designation: CPU1
\tVersion: Intel(R) Xeon(R) Gold 6430
\tCore Count: 32

Handle 0x1100, DMI type 17, 40 bytes
Memory Device
\tSize: 32 GB
\tLocator: A1
\tType: DDR5
\tSpeed: 4800 MT/s
\tManufacturer: SK Hynix
\tPart Number: HMCG88AGBRA
\tConfigured Memory Speed: 4400 MT/s

Handle 0x1101, DMI type 17, 40 bytes
Memory Device
\tSize: No Module Installed
\tLocator: A2
"""


def test_parse_dmidecode():
    records = parse_dmidecode(DMIDECODE_SAMPLE)
    assert len(records) == 4
    system = [r for r in records if r["dmi_type"] == 1][0]
    assert system["props"]["Manufacturer"] == "Dell Inc."
    assert system["props"]["Product Name"] == "PowerEdge R760"
    dimms = [r for r in records if r["dmi_type"] == 17]
    assert dimms[0]["props"]["Size"] == "32 GB"


def test_normalize_summary_from_dmi():
    parsed = {
        "dmi": {"records": parse_dmidecode(DMIDECODE_SAMPLE)},
        "cpu": {"model": "", "vendor": "GenuineIntel", "sockets": 2,
                "cores_per_socket": 32, "threads_total": 128,
                "max_mhz": "3400.0", "flags_virt": "vmx"},
        "memory": {"total_bytes": 549755813888},
        "storage": {"disks": [{"name": "nvme0n1", "serial": "S1", "transport": "nvme"}]},
        "network": {"nics": []},
        "gpu": {"gpus": []},
    }
    summary = build_summary(parsed)
    assert summary["system"]["vendor"] == "Dell Inc."
    assert summary["system"]["serial"] == "ABC1234"
    # DMI type 4 Version fills in when lscpu model is empty
    assert summary["cpus"][0]["model"] == "Intel(R) Xeon(R) Gold 6430"
    assert summary["cpus"][0]["cores"] == 64
    dimms = summary["memory"]["dimms"]
    assert len(dimms) == 1  # "No Module Installed" slot excluded
    assert dimms[0]["size_bytes"] == 32 * 1024 ** 3
    assert dimms[0]["speed_mts"] == 4400  # configured speed preferred

    redacted = build_summary(parsed, redact=True)
    assert redacted["system"]["serial"] is None
    assert redacted["disks"][0]["serial"] is None


def test_dimm_bytes():
    assert _dimm_bytes("32 GB") == 32 * 1024 ** 3
    assert _dimm_bytes("512 MB") == 512 * 1024 ** 2
    assert _dimm_bytes("No Module Installed") is None
    assert _dimm_bytes(None) is None


LSPCI_SAMPLE = """\
Slot:\t01:00.0
Class:\tVGA compatible controller [0300]
Vendor:\tNVIDIA Corporation [10de]
Device:\tAD102 [GeForce RTX 4090] [2684]
Driver:\tnvidia

Slot:\t02:00.0
Class:\tEthernet controller [0200]
Vendor:\tIntel Corporation [8086]
Device:\tEthernet Controller X710 [1572]
Driver:\ti40e
Module:\ti40e
"""


def test_parse_lspci():
    devices = parse_lspci_vmm(LSPCI_SAMPLE)
    assert len(devices) == 2
    gpu = devices[0]
    assert class_id(gpu) == "0300"
    assert gpu["driver"] == "nvidia"
    assert class_id(devices[1]) == "0200"


LSCPU_JSON = {
    "lscpu": [
        {"field": "CPU(s):", "data": "32"},
        {
            "field": "Vendor ID:", "data": "AuthenticAMD",
            "children": [{"field": "Model name:", "data": "AMD EPYC 9354"}],
        },
    ]
}


def test_flatten_lscpu_json():
    fields = _flatten_lscpu_json(LSCPU_JSON)
    assert fields["CPU(s)"] == "32"
    assert fields["Model name"] == "AMD EPYC 9354"


def test_parse_lscpu_text():
    fields = _parse_lscpu_text("CPU(s):     8\nModel name: Test CPU\n")
    assert fields["CPU(s)"] == "8"
    assert fields["Model name"] == "Test CPU"


def test_dmesg_patterns_load(tmp_path):
    conf = tmp_path / "patterns.conf"
    conf.write_text("[deny]\nHardware Error\n[allow]\nACPI Error:\n")
    deny, allow = load_patterns(str(conf))
    assert any(p.search("CPU 0: Hardware Error detected") for p in deny)
    assert any(p.search("ACPI Error: something benign") for p in allow)


def test_taint_bits_table():
    assert TAINT_BITS[7] == ("D", "kernel died (OOPS/BUG)", "fault")


DMI_CUSTOM_BUILD = """\
Handle 0x0001, DMI type 1, 27 bytes
System Information
\tManufacturer: ASRock
\tProduct Name: B650M PG Riptide
\tSerial Number: Default string
\tUUID: 02006b9c-0206-0000-0000-000000000000

Handle 0x0002, DMI type 2, 15 bytes
Base Board Information
\tManufacturer: ASRock
\tProduct Name: B650M PG Riptide
\tVersion: 1.0
\tSerial Number: M80-E4001800042
"""

DMI_PREBUILT = """\
Handle 0x0001, DMI type 1, 27 bytes
System Information
\tManufacturer: Dell Inc.
\tProduct Name: PowerEdge R760
\tSerial Number: ABC1234

Handle 0x0002, DMI type 2, 15 bytes
Base Board Information
\tManufacturer: Dell Inc.
\tProduct Name: 0M83RH
"""

DMI_OEM_PLACEHOLDER = """\
Handle 0x0001, DMI type 1, 27 bytes
System Information
\tManufacturer: To Be Filled By O.E.M.
\tProduct Name: To Be Filled By O.E.M.

Handle 0x0002, DMI type 2, 15 bytes
Base Board Information
\tManufacturer: Micro-Star International Co., Ltd.
\tProduct Name: MPG X670E CARBON WIFI (MS-7D70)
"""


def _summary_for(dmi_text):
    parsed = {
        "dmi": {"records": parse_dmidecode(dmi_text)},
        "cpu": {"model": "AMD Ryzen 9 7950X", "sockets": 1},
        "memory": {"total_bytes": 0},
        "storage": {"disks": []},
        "network": {"nics": []},
        "gpu": {"gpus": []},
    }
    return build_summary(parsed)


# The prebuilt/custom/unknown classification is Lumina's - it is a guess about what firmware
# authors meant, and a guess belongs where it can be revised for bundles already submitted. These
# tests keep the parsing they always covered; the kind assertions moved with the rule, to
# ``lumina/results/tests/test_system_kind.py``, along with two tests that existed only for it.


def test_custom_build_detected_when_system_mirrors_board():
    """Board vendors copy the motherboard identity into the DMI system
    table on self-built machines; that mirror must not be mistaken for a
    vendor system model."""
    summary = _summary_for(DMI_CUSTOM_BUILD)
    assert summary["baseboard"]["vendor"] == "ASRock"
    assert summary["baseboard"]["product"] == "B650M PG Riptide"


def test_prebuilt_detected_from_distinct_system_model():
    summary = _summary_for(DMI_PREBUILT)
    assert summary["system"]["product"] == "PowerEdge R760"
    assert summary["baseboard"]["product"] == "0M83RH"


def test_custom_build_detected_from_oem_placeholders():
    summary = _summary_for(DMI_OEM_PLACEHOLDER)
    assert summary["system"]["vendor"] is None, "the placeholder is not kept"
    assert "MPG X670E" in summary["baseboard"]["product"]


def test_unknown_when_dmi_is_empty():
    summary = _summary_for("")
    assert summary["system"]["product"] is None
    assert summary["baseboard"]["product"] is None


# Lenovo stamps the machine-type code into both the system and baseboard
# tables and puts the readable model in Version, which is the field lshw
# reports as the system's "version". The MTM here is the real one from run
# 4f47867b; that run came back as "Custom build: LENOVO 21K9001NUS".
DMI_LENOVO_LAPTOP = """\
Handle 0x0001, DMI type 1, 27 bytes
System Information
\tManufacturer: LENOVO
\tProduct Name: 21K9001NUS
\tVersion: ThinkBook 14 G6+ ABP
\tSerial Number: PF4YA2GY
\tFamily: ThinkBook 14 G6+ ABP

Handle 0x0002, DMI type 2, 15 bytes
Base Board Information
\tManufacturer: LENOVO
\tProduct Name: 21K9001NUS
\tVersion: SDK0K17763 WIN

Handle 0x0003, DMI type 3, 22 bytes
Chassis Information
\tManufacturer: LENOVO
\tType: Notebook
"""

# A barebones rack server: the board maker's identity in both tables, no
# readable model anywhere, and a chassis type that used to be taken as proof
# of an OEM machine.
DMI_CUSTOM_SERVER = """\
Handle 0x0001, DMI type 1, 27 bytes
System Information
\tManufacturer: Supermicro
\tProduct Name: H13SSW
\tVersion: 0123456789

Handle 0x0002, DMI type 2, 15 bytes
Base Board Information
\tManufacturer: Supermicro
\tProduct Name: H13SSW

Handle 0x0003, DMI type 3, 22 bytes
Chassis Information
\tManufacturer: Supermicro
\tType: Rack Mount Chassis

Handle 0x0028, DMI type 38, 18 bytes
IPMI Device Information
\tInterface Type: KCS (Keyboard Control Style)
\tSpecification Version: 2.0
\tI2C Slave Address: 0x10
\tNV Storage Device: Not Present
"""


def test_machine_type_code_resolves_to_the_readable_model():
    """Regression, run 4f47867b: Product Name was an opaque MTM, so the run
    was listed as "LENOVO 21K9001NUS" and classified as a custom build."""
    summary = _summary_for(DMI_LENOVO_LAPTOP)
    system = summary["system"]

    assert system["product"] == "ThinkBook 14 G6+ ABP"
    assert system["model_number"] == "21K9001NUS"   # still identifiable


def test_board_revision_string_is_not_mistaken_for_a_model():
    """The baseboard's "SDK0K17763 WIN" has a space but is not a product name.
    Only the system table's own Version/Family may stand in for the model."""
    summary = _summary_for(DMI_LENOVO_LAPTOP)
    assert "SDK0K17763" not in (summary["system"]["product"] or "")


def test_chassis_form_factor_is_recorded():
    assert _summary_for(DMI_LENOVO_LAPTOP)["chassis"]["type"] == "Notebook"
    assert _summary_for(DMI_CUSTOM_SERVER)["chassis"]["type"] == "Rack Mount Chassis"


def test_chassis_type_does_not_decide_how_a_machine_was_built():
    """People build servers, and Framework ships laptops as kits, so a form
    factor cannot imply a vendor assembled the machine. This barebones server
    has no readable system model and stays custom despite its rack chassis."""
    summary = _summary_for(DMI_CUSTOM_SERVER)
    assert summary["baseboard"]["product"] == "H13SSW"


def test_bmc_detected_from_smbios_without_touching_any_driver():
    bmc = _summary_for(DMI_CUSTOM_SERVER)["bmc"]
    assert bmc["present"] is True
    assert bmc["interface"].startswith("KCS")
    assert bmc["ipmi_version"] == "2.0"
    # Never collected: a management address is a live attack surface and these
    # reports get published.
    assert not any("ip" == key or "lan" in key for key in bmc)


def test_bmc_absent_on_a_machine_without_one():
    assert _summary_for(DMI_LENOVO_LAPTOP)["bmc"] == {"present": False}


def test_baseboard_serial_redacted():
    parsed = {"dmi": {"records": parse_dmidecode(DMI_CUSTOM_BUILD)},
              "cpu": {}, "memory": {}, "storage": {}, "network": {}, "gpu": {}}
    summary = build_summary(parsed, redact=True)
    assert summary["baseboard"]["serial"] is None
    assert summary["baseboard"]["product"] == "B650M PG Riptide"


# --- benchmark output parsers -------------------------------------------------
# Fixtures are verbatim output from the tool versions that broke the original
# parsers, plus the older layouts, so both keep working.

OPENSSL_35_RSA = """\
                   sign    verify    encrypt   decrypt   sign/s verify/s  encr./s  decr./s
rsa  4096 bits 0.000987s 0.000041s 0.000042s 0.000987s   1013.3  24544.8  23868.2   1013.6
                               keygen    encaps    decaps keygens/s  encaps/s  decaps/s
                    rsa4096 0.358214s 0.000042s 0.000986s       2.8   23723.4    1014.5
"""

OPENSSL_30_RSA = """\
                  sign    verify    sign/s verify/s
rsa 4096 bits 0.004185s 0.000065s    239.0  15384.6
"""


def test_openssl_rsa_parse_handles_openssl_35_extra_columns():
    """OpenSSL 3.4 added encrypt/decrypt columns; sign/s is still the first
    trailing rate, so parse positionally from the end."""
    from alma_certify.benchmarks.crypto import parse_rsa_line

    assert parse_rsa_line(OPENSSL_35_RSA) == (1013.3, 24544.8)


def test_openssl_rsa_parse_handles_older_two_column_layout():
    from alma_certify.benchmarks.crypto import parse_rsa_line

    assert parse_rsa_line(OPENSSL_30_RSA) == (239.0, 15384.6)


def test_openssl_rsa_parse_returns_none_on_junk():
    from alma_certify.benchmarks.crypto import parse_rsa_line

    assert parse_rsa_line("openssl: command failed") is None


# zstd -b redraws one progress line per iteration; only the final figure is
# the measurement, and 1.5 added the "x" prefix on the ratio.
ZSTD_157 = (
    "|-corpus.bin : 20000000 -> 20000469 (x1.000), 5047.2 MB/s, 29052.8 MB/s "
    "\\-corpus.bin : 20000000 -> 20000469 (x1.000), 5201.6 MB/s, 29800.1 MB/s"
)
ZSTD_OLD = "corpus.bin : 536870912 -> 175298560 (3.063), 445.7 MB/s, 1214.4 MB/s"


def test_the_zstd_pattern_matches_every_progress_line_and_the_x_prefix():
    """A pattern property: all iterations match, and 1.5's ``x`` prefix is tolerated.

    Renamed from ``..._takes_final_measurement...``, which it never tested. Taking
    ``[-1]`` here is the *test* selecting the last match; production does its own
    selection at ``compress.py:65``, and changing that to ``matches[0]`` left all 393
    tests green - so a mid-run progress figure could have been published as the
    machine's measurement. ``test_zstd_publishes_the_final_iteration`` covers that.
    """
    from alma_certify.benchmarks.compress import _ZSTD_RESULT_RE

    matches = _ZSTD_RESULT_RE.findall(ZSTD_157)
    assert len(matches) == 2, "both progress lines must match"
    assert matches[-1] == ("1.000", "5201.6", "29800.1")


def test_zstd_parse_handles_pre_1_5_ratio_format():
    from alma_certify.benchmarks.compress import _ZSTD_RESULT_RE

    ratio, comp, _ = _ZSTD_RESULT_RE.findall(ZSTD_OLD)[-1]
    assert (ratio, comp) == ("3.063", "445.7")


# Three tests stood here covering the GL and Vulkan benchmarks' shared gate: skip with no display,
# skip with no render node, run when a session exists. The gate and both benchmarks are gone, and
# the reason is recorded in ``benchmarks/gpu.py``: neither had ever produced a number, because that
# gate excluded every headless server and vkmark was invoked with an option it does not have.


# --- the summary reports GPUs as observed --------------------------------------
#
# Four tests stood here covering the collector's renaming of an AMD APU's integrated GPU from the
# CPU's brand string. That is a judgement about what a part is called, and it moved to Lumina with
# its cases - see ``lumina/results/tests/test_gpu_identity.py``. What is left to check here is
# that the summary passes the collected GPU records through untouched.


def _summary_with(cpu_model, gpus, dmi_text=DMI_LENOVO_LAPTOP):
    parsed = {
        "dmi": {"records": parse_dmidecode(dmi_text)},
        "cpu": {"model": cpu_model, "sockets": 1},
        "memory": {"total_bytes": 0},
        "storage": {"disks": []},
        "network": {"nics": []},
        "gpu": {"gpus": gpus},
    }
    return build_summary(parsed)


def test_gpu_records_reach_the_summary_unchanged():
    """Including an unnamed AMD die on a machine whose CPU string carries a product name: the
    rename that used to happen here now happens in the reader, so the bundle keeps the codename
    lspci actually reported."""
    collected = {
        "pci": "c1:00.0",
        "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI]",
                    "device": "Phoenix1 [15bf]"},
        "driver": "amdgpu", "driver_version": None, "runtime": {}, "vbios": None,
    }

    summary = _summary_with("AMD Ryzen 7 PRO 7840U w/ Radeon 780M Graphics", [collected])

    assert summary["gpus"] == [collected]


# --- raw PCI enumeration for the server to categorize -------------------------

# A slice of a real lspci -vmmnnk: the Mellanox NIC that a netdev-only view missed, the ASPEED BMC
# display, and an AMD iGPU. The server decides which is a NIC/GPU from ``class_id``; the collector
# passes them through without judging.
LSPCI_MIXED = """\
Slot:\t01:00.0
Class:\tEthernet controller [0200]
Vendor:\tMellanox Technologies [15b3]
Device:\tMT27710 Family [ConnectX-4 Lx] [1015]
SVendor:\tMellanox Technologies [15b3]
SDevice:\tDevice [0065]
Driver:\tmlx5_core

Slot:\t0c:00.0
Class:\tVGA compatible controller [0300]
Vendor:\tASPEED Technology, Inc. [1a03]
Device:\tASPEED Graphics Family [2000]
Driver:\tast

Slot:\t11:00.0
Class:\tVGA compatible controller [0300]
Vendor:\tAdvanced Micro Devices, Inc. [AMD/ATI] [1002]
Device:\tRaphael [164e]
Driver:\tamdgpu
"""


def test_the_raw_pci_enumeration_reaches_the_summary_for_the_server_to_categorize():
    """The collector passes every lspci device through with its class, so Lumina - not the collector
    - decides what is a NIC or a GPU. The Mellanox is here whether or not a netdev exists, which is
    the bug that motivated this: netdev-only NIC detection missed a driverless/odd-mode card."""
    parsed = {
        "pci": {"devices": parse_lspci_vmm(LSPCI_MIXED), "available": True},
        "dmi": {"records": []}, "cpu": {}, "memory": {}, "storage": {},
        "network": {"nics": []}, "gpu": {"gpus": []},
    }
    devices = build_summary(parsed)["pci_devices"]

    by_slot = {d["pci"]: d for d in devices}
    assert set(by_slot) == {"01:00.0", "0c:00.0", "11:00.0"}
    # The Mellanox: class 0200 (network), named, driver captured - present regardless of any netdev.
    mlx = by_slot["01:00.0"]
    assert mlx["class_id"] == "0200"
    assert mlx["pci_ids"]["vendor"] == "Mellanox Technologies [15b3]"
    assert mlx["pci_ids"]["device"] == "MT27710 Family [ConnectX-4 Lx] [1015]"
    assert mlx["driver"] == "mlx5_core"
    # The two display devices carry class 0300 so the server can tell an accelerator from a BMC.
    assert by_slot["0c:00.0"]["class_id"] == "0300"   # ASPEED BMC
    assert by_slot["11:00.0"]["class_id"] == "0300"   # AMD iGPU


def test_pci_devices_is_empty_when_lspci_is_unavailable():
    parsed = {
        "pci": {"devices": [], "available": False},
        "dmi": {"records": []}, "cpu": {}, "memory": {}, "storage": {},
        "network": {"nics": []}, "gpu": {"gpus": []},
    }
    assert build_summary(parsed)["pci_devices"] == []


# --- unbranded firmware -------------------------------------------------------

# Run 71314765: a Lenovo-MTM server (7D2X...) whose firmware left "OEM" in the
# manufacturer fields. Lumina reported it as "Custom build: OEM 7D2XCTO1WW" and
# would have created a manufacturer called OEM.
DMI_UNBRANDED = """\
Handle 0x0001, DMI type 1, 27 bytes
System Information
\tManufacturer: OEM
\tProduct Name: Not Specified
\tVersion: OEM
\tSerial Number: Not Specified

Handle 0x0002, DMI type 2, 15 bytes
Base Board Information
\tManufacturer: OEM
\tProduct Name: 7D2XCTO1WW
"""


def test_placeholder_manufacturer_is_not_reported_as_a_vendor():
    """"OEM" is what a vendor leaves when nobody filled the field in. Passing
    it on creates a manufacturer called OEM in the catalog."""
    summary = _summary_for(DMI_UNBRANDED)
    assert summary["system"]["vendor"] is None
    assert summary["system"]["product"] is None
    assert summary["baseboard"]["vendor"] is None
    # the one real string survives
    assert summary["baseboard"]["product"] == "7D2XCTO1WW"


@pytest.mark.parametrize("placeholder", [
    "OEM", "O.E.M.", "Default string", "To Be Filled By O.E.M.",
    "System manufacturer", "Not Applicable", "unknown", "-",
])
def test_known_placeholder_strings_are_all_dropped(placeholder):
    dmi = (
        "Handle 0x0001, DMI type 1, 27 bytes\nSystem Information\n"
        "\tManufacturer: %s\n\tProduct Name: %s\n" % (placeholder, placeholder)
    )
    system = _summary_for(dmi)["system"]
    assert system["vendor"] is None
    assert system["product"] is None


# --- SELinux is provenance, not a hardware verdict ----------------------------


class _FakeCtx:
    """Minimal RunContext stand-in: only ctx.cmd is exercised."""

    def __init__(self, stdout="", ok=True, missing=False):
        self._stdout, self._ok, self._missing = stdout, ok, missing
        self.summary = {}

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        if self._missing:
            from alma_certify import procutil
            raise procutil.CommandNotFound(argv[0])
        from alma_certify.procutil import CmdResult
        return CmdResult(argv=list(argv), returncode=0 if self._ok else 1,
                         stdout=self._stdout, stderr="")


def test_selinux_enforcing_passes_quietly():
    from alma_certify.validate.platform import SelinuxState

    result = SelinuxState().run(_FakeCtx("Enforcing\n"))
    assert result.status == "pass"
    assert result.details["state"] == "enforcing"


@pytest.mark.parametrize("state", ["Permissive", "Disabled"])
def test_selinux_not_enforcing_is_reported_not_failed(state):
    """A boot parameter is not a hardware defect. A real submission failed
    certification here with SELinux disabled while every hardware test on it
    passed."""
    from alma_certify.validate.platform import SelinuxState

    result = SelinuxState().run(_FakeCtx(state + "\n"))

    assert result.status == "pass"
    assert "worth a look" in result.reason
    assert state.lower() in result.reason
    assert result.details["state"] == state.lower()


def test_selinux_never_gates_certification():
    from alma_certify.validate.platform import SelinuxState

    assert SelinuxState.severity == "informational"


def test_missing_selinux_tooling_is_a_skip():
    """Nothing was measured, which is not the same as a finding."""
    from alma_certify.validate.platform import SelinuxState

    result = SelinuxState().run(_FakeCtx(missing=True))
    assert result.status == "skip"
    assert "getenforce" in result.reason


# --- an unbound GPU is never a hardware failure --------------------------------


class _GpuCtx:
    def __init__(self, gpus):
        self.summary = {"gpus": gpus}

    def cmd(self, *args, **kwargs):     # pragma: no cover - must not be reached
        raise AssertionError("the GPU test no longer shells out to dmesg")


def _gpu(model, vendor, driver=None):
    return {"pci": "07:00.0", "vendor": vendor, "model": model, "driver": driver}


def test_a_management_display_adapter_needs_no_driver():
    """Every server has one. It drives a VGA console nobody looks at, and
    failing certification for it condemned working servers."""
    from alma_certify.validate.gpu import GpuDriverState

    result = GpuDriverState().run(_GpuCtx([_gpu("MGA G200EH", "matrox")]))

    assert result.status == "pass"
    assert "need" in result.reason and "none" in result.reason
    assert "MGA G200EH" in result.reason


def test_an_unbound_discrete_gpu_is_reported_not_failed():
    """NVIDIA's driver is out of tree and nouveau is often blacklisted, which
    is a software choice rather than a hardware defect."""
    from alma_certify.validate.gpu import GpuDriverState

    result = GpuDriverState().run(
        _GpuCtx([_gpu("AD102 [GeForce RTX 4090]", "nvidia")])
    )

    assert result.status == "pass"
    assert "worth a look" in result.reason
    assert "no driver bound" in result.reason


def test_the_gpu_test_never_gates_certification():
    from alma_certify.validate.gpu import GpuDriverState

    assert GpuDriverState.severity == "informational"


def test_bound_drivers_are_named():
    from alma_certify.validate.gpu import GpuDriverState

    result = GpuDriverState().run(
        _GpuCtx([_gpu("Phoenix1", "amd", driver="amdgpu")])
    )
    assert result.status == "pass"
    assert "amdgpu" in result.reason


def test_a_mixed_machine_describes_each_group():
    """A server with a BMC adapter and a real accelerator: the adapter must not
    be lumped in with the card that genuinely has no driver."""
    from alma_certify.validate.gpu import GpuDriverState

    result = GpuDriverState().run(_GpuCtx([
        _gpu("MGA G200EH", "matrox"),
        _gpu("GA100", "nvidia"),
        _gpu("Phoenix1", "amd", driver="amdgpu"),
    ]))

    assert result.status == "pass"
    assert "amdgpu" in result.reason
    assert "which need" in result.reason          # the Matrox
    assert "worth a look" in result.reason        # the NVIDIA
    assert result.reason.index("MGA G200EH") != result.reason.index("GA100")


# --- compression: speed is a metric, ratio is not -----------------------------

ZSTD_OUTPUT = """\
 3#alma-corpus.bin : 536870912 -> 79953271 (x6.715), 6322.4 MB/s, 4350.1 MB/s
"""


def test_zstd_publishes_speeds_and_not_the_ratio(tmp_path):
    """A fixed level over a deterministic corpus gives the same ratio on every
    machine, so publishing it as a comparable metric ranks nothing. Three very
    different systems all reported 6.76 at level 19."""
    from alma_certify import procutil
    from alma_certify.benchmarks.compress import ZstdBench
    from alma_certify.config import Config
    from alma_certify.registry import RunContext

    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {}})
    ctx.log = lambda msg: None
    ctx.cmd = lambda argv, **kw: procutil.CmdResult(
        argv=list(argv), returncode=0, stdout=ZSTD_OUTPUT, stderr="")
    bench = ZstdBench()
    bench._corpus = str(tmp_path / "corpus.bin")
    bench._size_mb = 512

    result = bench.run(ctx)

    names = {m.name for m in result.metrics}
    assert "comp_speed_l3" in names
    assert "decomp_speed_l3" in names
    assert not any("ratio" in name for name in names)
    # Still recorded, as provenance for the speed figures beside it.
    assert result.details["ratio_l3"] == 6.715


def test_zstd_publishes_the_final_iteration_not_an_intermediate_one(tmp_path):
    """``zstd -b`` redraws a progress line per iteration; only the last is the result.

    The production selection is ``matches[-1]`` at ``compress.py:65``. Nothing
    exercised it: the regex test above does its own ``[-1]``, so flipping production
    to ``matches[0]`` published a mid-run figure with the suite green. Uses the
    two-iteration fixture, where the intermediate and final speeds differ.
    """
    from alma_certify import procutil
    from alma_certify.benchmarks.compress import ZstdBench
    from alma_certify.config import Config
    from alma_certify.registry import RunContext

    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {}})
    ctx.log = lambda msg: None
    ctx.cmd = lambda argv, **kw: procutil.CmdResult(
        argv=list(argv), returncode=0, stdout=ZSTD_157 + "\n", stderr="")
    bench = ZstdBench()
    bench._corpus = str(tmp_path / "corpus.bin")
    bench._size_mb = 512

    result = bench.run(ctx)

    speeds = {m.name: m.value for m in result.metrics}
    assert speeds["comp_speed_l3"] == 5201.6, "published an intermediate iteration"
    assert speeds["decomp_speed_l3"] == 29800.1


# --- the GPU benchmark catalog is deliberately two entries ----------------------


def test_only_the_gpu_benchmarks_that_earn_their_packaging_are_registered():
    """hashcat, glmark2, and vkmark were removed rather than packaged.

    Asserted rather than left to a reading of the module, because the cost being avoided is a
    recurring one: the suite's maintainers own the package for anything it needs, and a benchmark
    reintroduced without that conversation puts one back on them. ``benchmarks/gpu.py`` carries the
    reasoning for each.
    """
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    gpu_benchmarks = sorted(
        cls.id for cls in REGISTRY.all()
        if cls.category == "gpu" and cls.run_type == "benchmark"
    )

    assert gpu_benchmarks == ["bench.gpu.clpeak", "bench.gpu.cuda-bandwidth"]


def test_nothing_in_the_suite_still_asks_for_the_removed_packages():
    """A leftover ``packages`` entry would have the package manager install a tool nobody runs, and
    would put it back on the maintainers by the back door."""
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    wanted = {pkg for cls in REGISTRY.all() for pkg in getattr(cls, "packages", ())}

    assert not wanted & {"hashcat", "glmark2", "vkmark"}


# --- the report and the schema document have to agree ----------------------------
#
# ``docs/schema.md`` is the contract between this suite and lumina: lumina's ingest reads the keys
# it documents and nothing else, and the two repositories cannot import each other. Nothing
# compared them, and that let a whole feature quietly not work.
#
# ``claim_scope`` was recorded in the run's journal, narrowed which tests ran, was printed in
# ``alma-certify runs``, was documented here, and was read by lumina's ingest, and
# ``report.assemble`` never wrote it. So every ``--scope gpu`` run reached the server as a
# whole-machine claim, and its review page asked the reviewer for the machine's vendor and model.
# Reported from run 3c2a5873.


def _documented_run_keys():
    """The keys the ``run`` table in docs/schema.md promises."""
    import os
    import re

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "docs", "schema.md")
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    section = text.split("### `run`", 1)[1].split("###", 1)[0]
    keys = set()
    for line in section.splitlines():
        if not line.startswith("|") or line.startswith("|---"):
            continue
        cell = line.split("|")[1].strip()
        if cell in ("key", ""):
            continue
        # "`started_at` / `finished_at`" documents two keys on one row.
        keys.update(re.findall(r"`([a-z_]+)`", cell))
    return keys


def _assembled_run_keys():
    import tempfile

    from alma_certify import report as report_mod
    from alma_certify.state import RunState

    state = RunState.create(tempfile.mkdtemp(), "9f3c1a2e-1111-4111-8111-111111111111", {
        "run_types": ["validate"], "claim_scope": ["gpu"],
    })
    return set(report_mod.assemble(state, {}, [], {})["run"])


def test_every_documented_run_key_is_actually_written():
    """The direction that broke. A key in the contract that the report never produces is a feature
    that works everywhere except where it counts."""
    missing = _documented_run_keys() - _assembled_run_keys()

    assert not missing, "documented in docs/schema.md and never written: %s" % sorted(missing)


def test_every_written_run_key_is_documented():
    """The other direction, so a field added here reaches lumina's authors rather than surprising
    them."""
    undocumented = _assembled_run_keys() - _documented_run_keys()

    assert not undocumented, "written into report.json and undocumented: %s" % sorted(undocumented)


def test_a_scoped_run_says_what_it_claims():
    import tempfile

    from alma_certify import report as report_mod
    from alma_certify.state import RunState

    base = tempfile.mkdtemp()
    scoped = RunState.create(base, "3c2a5873-f5db-422d-8dcc-49ab5cc2ac9e", {
        "run_types": ["validate"], "claim_scope": ["gpu"],
    })
    whole = RunState.create(base, "0b71d4c9-2222-4222-8222-222222222222", {"run_types": ["run"]})

    assert report_mod.assemble(scoped, {}, [], {})["run"]["claim_scope"] == ["gpu"]
    # Absent or empty means the whole machine, which is what every report before the field said.
    assert report_mod.assemble(whole, {}, [], {})["run"]["claim_scope"] == []
