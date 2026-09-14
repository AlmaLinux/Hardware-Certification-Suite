"""Network validation: link state, DHCP on unused NICs, short data-path check."""

from __future__ import annotations

import os
import re

from .. import hwquery, procutil
from ..inventory import pci as pci_mod
from ..registry import REGISTRY, RunContext, Test
from ..result import Direction, Metric, Severity, Status

# PCI class codes. 02xx is the network-controller group: 0200 is Ethernet and
# 0280 is "network controller", which in practice means wireless.
NETWORK_CLASS_PREFIX = "02"
CLASS_LABELS = {
    "0200": "ethernet",
    "0201": "token ring",
    "0202": "fddi",
    "0203": "atm",
    "0207": "infiniband",
    "0280": "wireless",
}


class PciNetworkDrivers(Test):
    """Every network controller on the PCI bus has a driver bound to it.

    ``ip link`` cannot answer this: a controller with no driver never gets a
    netdev, so it is simply absent there - unsupported hardware would pass
    unnoticed. ``lspci -k`` enumerates the devices themselves, so an
    unsupported wired or wireless controller shows up as the failure it is.

    Also confirms each bound driver actually produced an interface: a driver
    that attaches but creates no netdev is its own failure mode.
    """

    id = "validate.network.pci-drivers"
    category = "network"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 60
    packages = ("pciutils",)

    def _controllers(self, ctx: RunContext) -> list:
        res = ctx.cmd(
            ["lspci", "-vmmnnk"], timeout=30, artifact="lspci-network.txt"
        )
        if not res.ok:
            return []
        return [
            dev for dev in pci_mod.parse_lspci_vmm(res.stdout)
            if pci_mod.class_id(dev).startswith(NETWORK_CLASS_PREFIX)
        ]

    def applicable(self, ctx: RunContext):
        try:
            if not self._controllers(ctx):
                return "no PCI network controllers found"
        except procutil.CommandNotFound:
            return "lspci not available"
        return None

    def run(self, ctx: RunContext):
        controllers = self._controllers(ctx)
        artifacts = [ctx.rel_artifact("lspci-network.txt")]

        described, unbound, no_interface = [], [], []
        for dev in controllers:
            slot = dev.get("slot", "?")
            code = pci_mod.class_id(dev)
            entry = {
                "pci": slot,
                "kind": CLASS_LABELS.get(code, "other"),
                "device": _strip_ids(dev.get("device", "")),
                "vendor": _strip_ids(dev.get("vendor", "")),
                "driver": dev.get("driver") or None,
                "modules_available": dev.get("module") or None,
                "interfaces": _interfaces_for(slot),
            }
            described.append(entry)
            if not entry["driver"]:
                unbound.append(entry)
            elif not entry["interfaces"]:
                no_interface.append(entry)

        details = {"controllers": described}
        if unbound:
            return self.result(
                Status.FAIL,
                reason="network controller(s) with no driver bound: %s"
                % "; ".join(_describe(e) for e in unbound),
                details=details,
                artifacts=artifacts,
            )
        if no_interface:
            return self.result(
                Status.FAIL,
                reason="driver bound but no interface created for: %s"
                % "; ".join(_describe(e) for e in no_interface),
                details=details,
                artifacts=artifacts,
            )
        return self.result(Status.PASS, details=details, artifacts=artifacts)


def _describe(entry: dict) -> str:
    text = "%s %s (%s)" % (entry["pci"], entry["device"], entry["kind"])
    if entry.get("modules_available") and not entry.get("driver"):
        # A module exists for it but nothing is bound: worth naming, because
        # the fix is usually different from "no support at all".
        text += " [module available: %s]" % entry["modules_available"]
    return text


def _interfaces_for(slot: str) -> list:
    """Netdev names the kernel created for a PCI slot."""
    if not slot:
        return []
    # lspci prints "3b:00.0"; sysfs uses the full "0000:3b:00.0"
    candidates = [slot] if slot.count(":") == 2 else ["0000:%s" % slot]
    for addr in candidates:
        net_dir = "/sys/bus/pci/devices/%s/net" % addr
        if os.path.isdir(net_dir):
            return sorted(os.listdir(net_dir))
    return []


def _strip_ids(value: str) -> str:
    return re.sub(r"\s*\[[0-9a-f]{4}\]$", "", value).strip()


class LinkState(Test):
    id = "validate.network.link"
    category = "network"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 120
    packages = ("ethtool",)

    def applicable(self, ctx: RunContext):
        if not hwquery.physical_nics(ctx.summary):
            return "no physical NICs detected"
        return None

    def run(self, ctx: RunContext):
        nics = hwquery.physical_nics(ctx.summary)
        no_driver = [n["name"] for n in nics if not n.get("driver")]
        up = [n for n in nics if n.get("link")]
        details = {
            "nics": [
                {
                    "name": n["name"],
                    "driver": n.get("driver"),
                    "driver_version": n.get("driver_version"),
                    "link": n.get("link"),
                    "speed_mbps": n.get("speed_mbps"),
                }
                for n in nics
            ]
        }
        if no_driver:
            return self.result(
                Status.FAIL,
                reason="NIC(s) without a bound driver: %s" % ", ".join(no_driver),
                details=details,
            )
        if not up:
            return self.result(
                Status.FAIL,
                reason="no NIC has link - at least one connected interface is required",
                details=details,
            )
        return self.result(Status.PASS, details=details)


class DataPath(Test):
    """Short iperf3 sanity vs --peer: traffic flows at a sane fraction of
    line rate. A functionality gate, not a throughput soak."""

    id = "validate.network.datapath"
    category = "network"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    default_timeout = 300
    packages = ("iperf3",)

    def applicable(self, ctx: RunContext):
        if not ctx.peer:
            return "no --peer given (needs an iperf3 server on a second host)"
        return None

    def run(self, ctx: RunContext):
        duration = ctx.config.getint("validate", "network_smoke_seconds", 30)
        min_fraction = ctx.config.getfloat("validate", "network_min_fraction", 0.5)
        port = ctx.config.getint("network", "iperf3_port", 5201)

        res = ctx.cmd(
            [
                "iperf3", "-c", ctx.peer, "-p", str(port),
                "-t", str(duration), "-P", "4", "--json",
            ],
            timeout=duration + 60,
            artifact="iperf3.json",
        )
        artifacts = [ctx.rel_artifact("iperf3.json")]
        if not res.ok:
            return self.result(
                Status.FAIL,
                reason="iperf3 could not exchange traffic with %s" % ctx.peer,
                artifacts=artifacts,
            )
        import json as _json

        try:
            data = _json.loads(res.stdout)
            bps = data["end"]["sum_received"]["bits_per_second"]
        except (ValueError, KeyError):
            return self.result(
                Status.ERROR, reason="could not parse iperf3 output", artifacts=artifacts
            )
        gbps = bps / 1e9

        # compare against the fastest NIC with link
        speeds = [
            n["speed_mbps"] for n in hwquery.physical_nics(ctx.summary)
            if n.get("link") and n.get("speed_mbps")
        ]
        details = {"peer": ctx.peer, "throughput_gbps": round(gbps, 3)}
        metrics = [Metric("throughput", round(gbps, 3), "Gbit/s", Direction.INFO)]
        if speeds:
            line_gbps = max(speeds) / 1000.0
            details["line_rate_gbps"] = line_gbps
            if gbps < line_gbps * min_fraction:
                return self.result(
                    Status.FAIL,
                    reason="throughput %.2f Gbit/s is below %d%% of line rate (%.2f Gbit/s)"
                    % (gbps, int(min_fraction * 100), line_gbps),
                    details=details,
                    metrics=metrics,
                    artifacts=artifacts,
                )
        return self.result(Status.PASS, details=details, metrics=metrics,
                           artifacts=artifacts)


REGISTRY.register(PciNetworkDrivers)
REGISTRY.register(LinkState)
REGISTRY.register(DataPath)
