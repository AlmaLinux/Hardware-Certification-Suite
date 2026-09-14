"""GPU validation: record what is present and which driver is bound."""

from __future__ import annotations

from .. import hwquery
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status


class GpuDriverState(Test):
    """Record which GPUs are present and whether a driver is bound.

    Never fails on an unbound GPU, and the reported case is why: nearly every
    server has a BMC display adapter (Matrox G200, ASPEED AST) whose entire job
    is a VGA console nobody looks at. It is not an accelerator, nothing on the
    machine depends on it, and whether the kernel binds ``mgag200`` or leaves
    the EFI framebuffer in place is a kernel packaging detail rather than a
    property of the hardware. Failing certification for it condemned working
    servers.

    An unbound *discrete* GPU is not a hardware defect either. It usually means
    the driver lives outside the distribution (NVIDIA's does) or was blacklisted
    on purpose, and the suite has no way to know whether this machine was ever
    intended to do GPU work. So it is reported for a reviewer, not gated.

    Where GPU capability does have to be proven, it is enforced at the point of
    the claim instead: the benchmark catalog refuses to record a GPU metric
    without driver and runtime versions, so a leaderboard entry can never come
    from a card whose driver nobody identified.

    The old version also failed on GPU driver errors in dmesg. That is dropped:
    ``validate.kernel.dmesg`` already scans the log against a curated pattern
    file with an allowlist for known-benign firmware noise, where this did a
    bare substring match on the driver name - and with a driver called ``ast``
    that also matches "last", "broadcast", and "fastpath".
    """

    id = "validate.gpu.driver"
    category = "gpu"
    run_type = "validate"
    severity = Severity.INFORMATIONAL
    default_timeout = 60

    def applicable(self, ctx: RunContext):
        if not hwquery.gpus(ctx.summary):
            return "no GPU detected"
        return None

    def run(self, ctx: RunContext):
        gpus = hwquery.gpus(ctx.summary)
        details = {"gpus": gpus}

        # Management adapters first, and regardless of whether a driver is bound: a bound ``ast`` on
        # an ASPEED is still a VGA console, not a card under test, and calling it "1 with a driver"
        # would imply an accelerator that is not there. Matched by vendor id (see
        # ``hwquery.is_management_adapter``), so the split holds on a current collector, which no
        # longer writes the flattened ``vendor`` token this used to read - and did not, silently.
        adapters = [gpu for gpu in gpus if hwquery.is_management_adapter(gpu)]
        accelerators = [gpu for gpu in gpus if not hwquery.is_management_adapter(gpu)]
        bound = [gpu for gpu in accelerators if gpu.get("driver")]
        others = [gpu for gpu in accelerators if not gpu.get("driver")]

        parts = []
        if bound:
            parts.append(
                "%d with a driver (%s)"
                % (len(bound), ", ".join(sorted({g["driver"] for g in bound})))
            )
        if adapters:
            parts.append(
                "%d management display adapter(s), which need none (%s)"
                % (len(adapters), _names(adapters))
            )
        if others:
            parts.append(
                "worth a look: %d with no driver bound (%s) - if this machine "
                "is meant to do GPU work, the driver is missing or blacklisted"
                % (len(others), _names(others))
            )
        return self.result(
            Status.PASS,
            reason="; ".join(parts) or "no GPU detected",
            details=details,
        )


def _names(gpus: list) -> str:
    # ``model`` is the legacy flattened name; a current collector carries it in ``pci_ids.device``
    # ("ASPEED Graphics Family [...]"), so read that before falling back to the bare PCI slot.
    return ", ".join(
        gpu.get("model") or (gpu.get("pci_ids") or {}).get("device") or gpu.get("pci") or "?"
        for gpu in gpus
    )


REGISTRY.register(GpuDriverState)
