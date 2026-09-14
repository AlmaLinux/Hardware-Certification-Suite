"""Interactive USB hotplug test: detect insert and removal events."""

from __future__ import annotations

import time
from typing import Set

from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status


def _usb_ids(ctx: RunContext) -> Set[str]:
    res = ctx.cmd(["lsusb"], timeout=30)
    if not res.ok:
        return set()
    return {line.strip() for line in res.stdout.splitlines() if line.strip()}


class UsbHotplug(Test):
    id = "validate.usb.hotplug"
    category = "usb"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    interactive = True
    default_timeout = 300
    packages = ("usbutils",)

    def run(self, ctx: RunContext):
        before = _usb_ids(ctx)
        print("\n=== USB hotplug test ===")
        print("Plug a USB device into any port within 60 seconds...")
        inserted = self._wait_for_change(ctx, before, appear=True)
        if not inserted:
            return self.result(
                Status.FAIL, reason="no new USB device detected within 60s"
            )
        print("Detected: %s" % ", ".join(sorted(inserted)))
        print("Now remove the same device within 60 seconds...")
        after_insert = _usb_ids(ctx)
        removed = self._wait_for_change(ctx, after_insert, appear=False)
        if not removed:
            return self.result(
                Status.FAIL,
                reason="device insertion detected but removal was not",
                details={"inserted": sorted(inserted)},
            )
        return self.result(
            Status.PASS,
            details={"inserted": sorted(inserted), "removed": sorted(removed)},
        )

    def _wait_for_change(self, ctx: RunContext, baseline: Set[str], appear: bool):
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            time.sleep(2)
            now = _usb_ids(ctx)
            delta = (now - baseline) if appear else (baseline - now)
            if delta:
                return delta
        return set()


REGISTRY.register(UsbHotplug)
