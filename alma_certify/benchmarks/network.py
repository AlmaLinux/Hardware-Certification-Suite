"""Network benchmarks: iperf3 TCP throughput against a peer host."""

from __future__ import annotations

import json

from ..registry import REGISTRY, RunContext
from ..result import Direction, Metric
from .base import BenchmarkTest


class _Iperf3(BenchmarkTest):
    category = "network"
    default_timeout = 180
    packages = ("iperf3",)
    reverse = False

    def applicable(self, ctx: RunContext):
        if not ctx.peer:
            return "no --peer given (needs an iperf3 server on a second host)"
        return None

    def run(self, ctx: RunContext):
        port = ctx.config.getint("network", "iperf3_port", 5201)
        argv = ["iperf3", "-c", ctx.peer, "-p", str(port), "-t", "60", "-P", "4", "--json"]
        if self.reverse:
            argv.append("-R")
        res = ctx.cmd(argv, timeout=self.default_timeout, artifact="iperf3.json")
        artifacts = [ctx.rel_artifact("iperf3.json")]
        try:
            data = json.loads(res.stdout)
            end = data["end"]
            bps = end["sum_received"]["bits_per_second"]
            retrans = end.get("sum_sent", {}).get("retransmits")
        except (ValueError, KeyError):
            return self.bench_error("iperf3 produced no parseable output", artifacts)
        metrics = [
            Metric("throughput", round(bps / 1e9, 3), "Gbit/s", Direction.HIGHER,
                   primary=True)
        ]
        if retrans is not None:
            metrics.append(Metric("retransmits", retrans, "count", Direction.LOWER))
        return self.bench_result(
            metrics, details={"peer": ctx.peer, "streams": 4, "reverse": self.reverse},
            artifacts=artifacts,
        )


class Iperf3Tcp(_Iperf3):
    id = "bench.net.iperf3-tcp"
    reverse = False


class Iperf3Reverse(_Iperf3):
    id = "bench.net.iperf3-reverse"
    reverse = True


REGISTRY.register(Iperf3Tcp)
REGISTRY.register(Iperf3Reverse)
