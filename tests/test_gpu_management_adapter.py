"""A machine whose only 'GPU' is its BMC console must not attempt GPU benchmarks.

Nearly every server has a baseboard-management display adapter - an ASPEED AST or Matrox G200 - that
drives a VGA console for the out-of-band interface. It is a PCI display-class device with an
in-kernel driver (``ast``, ``mgag200``), so anything that asks only "class 03xx with a driver"
mistakes it for a GPU. It is not an accelerator: nothing benchmarks on it. Counting it as one made
clpeak fire on a headless X11SCL-F and then fail against the CPU software rasterizer - the only
device its OpenCL and Vulkan runtimes could find - which in turn blocked the machine's cert.

These pin that a management adapter is dropped from the accelerator set, so clpeak is skipped with a
clear reason, while a real card beside the BMC is unaffected.
"""

from alma_certify import hwquery
from alma_certify.benchmarks import gpu as gpu_bench
from alma_certify.config import Config
from alma_certify.registry import RunContext
from alma_certify.validate.gpu import GpuDriverState

# The X11SCL-F's onboard BMC graphics as a current collector reports it: a display-class device
# with the ``ast`` driver bound and the vendor id inside pci_ids.vendor.
ASPEED = {
    "pci": "07:00.0",
    "pci_ids": {"vendor": "ASPEED Technology, Inc. [1a03]",
                "device": "ASPEED Graphics Family [2000]"},
    "driver": "ast",
}
MATROX = {
    "pci_ids": {"vendor": "Matrox Electronics Systems Ltd. [102b]", "device": "MGA G200eW [0532]"},
    "driver": "mgag200",
}
# The same ASPEED as an older collector wrote it, with a flattened ``vendor`` token and no pci_ids.
ASPEED_LEGACY = {"vendor": "aspeed", "model": "ASPEED Graphics Family", "driver": "ast"}
# Real accelerators, for the "not everything is an adapter" and "a real card is unaffected" cases.
NVIDIA = {"pci_ids": {"vendor": "NVIDIA Corporation [10de]", "device": "AD102 [26b9]"},
          "driver": "nvidia"}
INTEL = {"pci_ids": {"vendor": "Intel Corporation [8086]", "device": "UHD Graphics 630 [3e92]"},
         "driver": "i915"}
AMD = {
    "pci_ids": {"vendor": "Advanced Micro Devices, Inc. [AMD/ATI] [1002]", "device": "Navi [744c]"},
    "driver": "amdgpu",
}


class _Pkg:
    def missing(self, packages, repos=(), timeout=900):
        return list(packages)

    def ensure(self, packages, repos=(), timeout=900):
        return True


class _Ctx:
    def __init__(self, summary):
        self.summary = summary

    def cmd(self, *a, **k):  # pragma: no cover - the validator must not shell out
        raise AssertionError("the GPU validation does not run commands")


# --- the predicate ---------------------------------------------------------------


def test_a_bmc_adapter_is_recognised_by_pci_vendor_id():
    assert hwquery.is_management_adapter(ASPEED) is True
    assert hwquery.is_management_adapter(MATROX) is True


def test_a_bmc_adapter_is_recognised_from_a_legacy_vendor_token():
    """A stored bundle from before pci_ids has a flattened ``vendor``; both shapes must match."""
    assert hwquery.is_management_adapter(ASPEED_LEGACY) is True


def test_real_accelerators_are_not_management_adapters():
    assert hwquery.is_management_adapter(NVIDIA) is False
    assert hwquery.is_management_adapter(INTEL) is False
    assert hwquery.is_management_adapter(AMD) is False


def test_accelerator_gpus_drops_the_bmc_but_keeps_the_card():
    assert hwquery.accelerator_gpus({"gpus": [ASPEED, NVIDIA]}) == [NVIDIA]


def test_the_validation_and_benchmark_policies_are_kept_separate():
    """Deliberate split, and the one that must not drift: the GPU-*API* validation shares
    ``bound_gpus``/``no_driver_reason`` and treats a BMC as a display device its ICD gate may still
    probe, so those stay BMC-inclusive; only the benchmark view (``accelerator_gpus`` /
    ``accelerator_skip_reason``) drops it. Collapsing the two is what broke, then unbroke, this."""
    # Validation-shared predicates still see the bound BMC.
    assert hwquery.bound_gpus({"gpus": [ASPEED]}) == [ASPEED]
    assert hwquery.no_driver_reason({"gpus": [ASPEED]}) is None
    # The benchmark view does not.
    assert hwquery.accelerator_gpus({"gpus": [ASPEED]}) == []
    assert hwquery.accelerator_skip_reason({"gpus": [ASPEED]}) is not None


# --- the benchmark skip reason ---------------------------------------------------


def test_a_lone_bmc_adapter_gets_a_named_benchmark_skip_reason():
    reason = hwquery.accelerator_skip_reason({"gpus": [ASPEED]})
    assert reason is not None
    assert "management" in reason.lower() and "not an accelerator" in reason
    # It IS in the inventory, so "no GPU detected" would read as false.
    assert "no GPU detected" not in reason
    assert "ASPEED" in reason


def test_a_real_card_beside_the_bmc_is_not_held_back():
    assert hwquery.accelerator_skip_reason({"gpus": [ASPEED, NVIDIA]}) is None


# --- the benchmark uses it -------------------------------------------------------


def test_clpeak_is_skipped_when_the_only_gpu_is_the_bmc(tmp_path):
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path),
                     inventory={"summary": {"gpus": [ASPEED]}}, pkg=_Pkg())

    reason = gpu_bench.Clpeak().applicable(ctx)

    # Skipped, not run, and for the right reason - a management adapter is not an accelerator.
    assert reason == hwquery.accelerator_skip_reason({"gpus": [ASPEED]})
    assert "management" in reason.lower() and "not an accelerator" in reason


def test_clpeak_still_considers_a_real_card_beside_the_bmc():
    """The BMC being present must not mask the NVIDIA card from the benchmark's driver gate."""
    assert hwquery.accelerator_skip_reason({"gpus": [ASPEED, NVIDIA]}) is None


# --- the validation uses it too --------------------------------------------------


def test_the_validator_reports_a_bound_bmc_as_an_adapter_not_a_gpu():
    result = GpuDriverState().run(_Ctx({"gpus": [ASPEED]}))

    assert result.status == "pass"
    assert "management display adapter" in result.reason
    # It must not be counted among "N with a driver", which would present the VGA chip as a card.
    assert "with a driver" not in result.reason
    assert "ASPEED Graphics Family" in result.reason
