"""CPU feature flags, collected and reported informationally.

``lscpu`` has always given us the full flag list and the collector has always read
it - but only to answer one question, "is there vmx or svm", and then threw the rest
away. Everything else the CPU advertises was parsed and discarded on the same line.

Those flags are worth publishing. Whether a machine has ``avx512f``, ``sha_ni``, or
``sev_snp`` decides whether a workload will run well on it, and the catalog is the
natural place to look that up. They are **informational**: nothing passes or fails
on them, so no threshold has to be agreed and no result can regress because Intel
renamed a bit.

Kept as a sorted list rather than the raw string so a consumer can test membership
without splitting, and so two runs of the same CPU produce byte-identical output
regardless of how ``lscpu`` happened to order them.
"""
from alma_certify.inventory.cpu import collect_flags
from alma_certify.inventory.normalize import build_summary


def test_the_full_flag_list_is_kept():
    flags = collect_flags({"Flags": "fpu vme de pse tsc avx512f sha_ni"})

    assert "avx512f" in flags
    assert "sha_ni" in flags
    assert len(flags) == 7


def test_flags_are_sorted_so_two_runs_of_one_cpu_match():
    """lscpu's order is not guaranteed, and a diff between two runs of the same
    machine should be empty rather than a reshuffle."""
    one = collect_flags({"Flags": "sse2 avx fpu"})
    two = collect_flags({"Flags": "fpu sse2 avx"})

    assert one == two == ["avx", "fpu", "sse2"]


def test_duplicates_collapse():
    assert collect_flags({"Flags": "fpu fpu sse2"}) == ["fpu", "sse2"]


def test_a_cpu_reporting_no_flags_yields_an_empty_list():
    """Not None: a consumer iterating the field should not have to guard it. Some
    aarch64 lscpu builds report "Features" or nothing at all."""
    assert collect_flags({}) == []
    assert collect_flags({"Flags": "   "}) == []


def test_aarch64_reports_its_flags_under_features():
    """lscpu says "Flags" on x86_64 and "Features" on aarch64, and a collector that
    only knew the first returned nothing at all on Arm."""
    flags = collect_flags({"Features": "fp asimd evtstrm aes sha1 sha2 crc32"})

    assert "asimd" in flags
    assert "crc32" in flags


def test_the_flags_reach_the_normalized_summary():
    """The summary is what the bundle carries and what lumina ingests, so a field
    the collector fills but ``build_summary`` drops is not reported at all."""
    parsed = {
        "dmi": {"records": []},
        "cpu": {
            "model": "AMD EPYC 9354", "vendor": "AuthenticAMD",
            "sockets": 1, "cores_per_socket": 32, "threads_total": 64,
            "max_mhz": "3800.0", "flags_virt": "svm",
            "flags": ["avx2", "sha_ni", "svm"],
        },
    }

    summary = build_summary(parsed)

    assert summary["cpus"][0]["flags"] == ["avx2", "sha_ni", "svm"]
    # The virtualization answer stays: validate.virt.kvm reads it, and it is a
    # different question from "what does this CPU advertise".
    assert summary["cpus"][0]["flags_virt"] == "svm"


def test_a_cpu_with_no_flags_still_normalizes():
    parsed = {
        "dmi": {"records": []},
        "cpu": {"model": "Unknown", "vendor": "", "sockets": 1,
                "cores_per_socket": 1, "threads_total": 1},
    }

    summary = build_summary(parsed)

    assert summary["cpus"][0]["flags"] == []


# --- the informational result --------------------------------------------------
#
# Collecting the flags was only half of "report them informationally". They landed
# in ``inventory.cpus[].flags`` and stopped there - and the inventory is an opaque
# blob on a run page, while *results* are what people read, filter, and compare. So
# they were collected and invisible, which is how this came back as "I don't see
# anything being reported about CPU feature flags".
#
# ``validate.cpu.flags`` closes that. It measures nothing and asserts nothing.


class _Ctx:
    """Just enough RunContext: the test reads the inventory and shells out to
    nothing."""

    def __init__(self, inventory):
        self.inventory = inventory


def _run(flags):
    from alma_certify.validate.cpu import CpuFlagsInfo

    inventory = {"cpus": [{"model": "AMD EPYC 9004", "flags": list(flags)}]}
    return CpuFlagsInfo().run(_Ctx(inventory)).to_dict()


def test_the_result_is_informational():
    """It must never gate a verdict: a CPU is not defective for lacking AMX."""
    from alma_certify.validate.cpu import CpuFlagsInfo

    assert CpuFlagsInfo.severity == "informational"


def test_an_informational_failure_cannot_change_the_verdict():
    """Belt and braces on the above, through the real verdict function."""
    from alma_certify.report import verdict

    rep = {"results": [
        {"id": "validate.cpu.functional", "run_type": "validate",
         "status": "pass", "severity": "required"},
        {"id": "validate.cpu.flags", "run_type": "validate",
         "status": "fail", "severity": "informational"},
    ]}

    assert verdict(rep) is True


def test_the_full_list_is_in_the_result():
    result = _run(["aes", "avx2", "svm", "zzz_made_up"])

    assert result["details"]["flags"] == ["aes", "avx2", "svm", "zzz_made_up"]
    assert result["details"]["count"] == 4


def test_notable_flags_are_grouped():
    """A 159-entry alphabetical list buries the handful of facts that decide what
    a machine can be used for. Nobody scans for "sev_snp" by eye."""
    result = _run(["aes", "avx512f", "svm", "sev_snp", "ibpb", "constant_tsc", "fpu"])

    notable = result["details"]["notable"]
    assert notable["virtualization"] == ["svm"]
    assert notable["confidential_computing"] == ["sev_snp"]
    assert notable["crypto_acceleration"] == ["aes"]
    assert notable["vector_extensions"] == ["avx512f"]
    assert notable["speculation_controls"] == ["ibpb"]
    assert notable["timing_and_scheduling"] == ["constant_tsc"]


def test_absent_groups_are_omitted_not_listed_empty():
    """These are capabilities, not requirements. An empty "confidential_computing"
    row would read as a finding."""
    result = _run(["fpu", "aes"])

    assert "confidential_computing" not in result["details"]["notable"]
    assert result["details"]["notable"] == {"crypto_acceleration": ["aes"]}


def test_aarch64_flags_are_recognised_too():
    """The notable table covers both architectures; aarch64 reports its features
    under a different lscpu heading but the same normalized field."""
    result = _run(["asimd", "aes", "sha2", "sve2", "crc32"])

    notable = result["details"]["notable"]
    assert "sve2" in notable["vector_extensions"]
    assert "sha2" in notable["crypto_acceleration"]


def test_a_cpu_reporting_nothing_skips_rather_than_passing():
    """Nothing measured is not the same as nothing found."""
    result = _run([])

    assert result["status"] == "skip"
    assert "no CPU feature flags" in result["reason"]


def test_an_empty_inventory_skips_rather_than_crashing():
    from alma_certify.validate.cpu import CpuFlagsInfo

    for inventory in ({}, {"cpus": []}, {"cpus": [{}]}, {"cpus": [None]}):
        result = CpuFlagsInfo().run(_Ctx(inventory)).to_dict()
        assert result["status"] == "skip", inventory


def test_the_reason_says_how_many():
    """So the count is visible without expanding the details."""
    result = _run(["aes", "avx2"])

    assert result["reason"] == "2 CPU feature flags reported"


def test_it_is_registered_so_it_actually_runs():
    """A test class nobody registered is dead code that looks implemented."""
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    ids = [cls.id for cls in REGISTRY.select("validate", interactive=True)]

    assert "validate.cpu.flags" in ids


def test_it_reads_the_inventory_rather_than_running_lscpu_again():
    """A second lscpu could disagree with the report over a mid-run change, and
    then one run would carry two answers to the same question.

    Asserted by giving it an inventory the machine does not have: if it shelled
    out, it would report this host's real flags instead.
    """
    result = _run(["made_up_flag_not_on_any_cpu"])

    assert result["details"]["flags"] == ["made_up_flag_not_on_any_cpu"]
