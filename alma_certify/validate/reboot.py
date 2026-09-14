"""Reboot survival: clean reboot, automatic resume, device re-enumeration.

Mechanics: on first entry the test records pre-reboot state, installs a
oneshot systemd unit that runs ``alma-certify resume <run_id>`` at next boot,
and reboots. The suite process dies before recording a result, so the resume
journal re-runs this test - which now finds the marker and verifies.
"""

from __future__ import annotations

import glob
import json
import os
import sys

from .. import procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status

UNIT_PATH = "/etc/systemd/system/alma-certify-resume.service"

UNIT_TEMPLATE = """[Unit]
Description=Resume an interrupted alma-certify run after reboot
After=network-online.target multi-user.target
Wants=network-online.target

[Service]
Type=oneshot
Environment=PYTHONPATH={pythonpath}
ExecStart={python} -m alma_certify resume {run_id} --run-dir {base_dir}
ExecStartPost=/usr/bin/systemctl disable alma-certify-resume.service

[Install]
WantedBy=multi-user.target
"""


def _boot_id() -> str:
    return procutil.read_file("/proc/sys/kernel/random/boot_id", "") or ""


def install_resume_unit(run_dir: str) -> None:
    base_dir = os.path.dirname(run_dir.rstrip("/"))
    run_id = os.path.basename(run_dir.rstrip("/"))
    package_parent = os.path.dirname(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    )
    unit = UNIT_TEMPLATE.format(
        python=sys.executable,
        pythonpath=package_parent,
        run_id=run_id,
        base_dir=base_dir,
    )
    with open(UNIT_PATH, "w", encoding="utf-8") as fh:
        fh.write(unit)
    procutil.run_cmd(["systemctl", "daemon-reload"], timeout=60)
    procutil.run_cmd(["systemctl", "enable", "alma-certify-resume.service"], timeout=60)


def remove_resume_unit() -> None:
    procutil.run_cmd(["systemctl", "disable", "alma-certify-resume.service"], timeout=60)
    try:
        os.unlink(UNIT_PATH)
    except OSError:
        pass
    procutil.run_cmd(["systemctl", "daemon-reload"], timeout=60)


class RebootSurvival(Test):
    id = "validate.platform.reboot"
    category = "platform"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    interactive = True  # reboots the machine; requires explicit opt-in
    default_timeout = 600

    def _marker_path(self, ctx: RunContext) -> str:
        return ctx.artifact_path("reboot-marker.json", test_id=self.id)

    def run(self, ctx: RunContext):
        marker_path = self._marker_path(ctx)
        if not os.path.exists(marker_path):
            marker = {
                "boot_id": _boot_id(),
                "pci_count": len(glob.glob("/sys/bus/pci/devices/*")),
                "disk_count": len(ctx.summary.get("disks", [])),
                "nic_count": len(ctx.summary.get("nics", [])),
            }
            with open(marker_path, "w", encoding="utf-8") as fh:
                json.dump(marker, fh)
            install_resume_unit(ctx.run_dir)
            ctx.log("rebooting for the reboot-survival test; "
                    "the run resumes automatically after boot")
            procutil.run_cmd(["systemctl", "reboot"], timeout=60)
            # if we are still alive after 10 minutes, the reboot didn't happen
            import time

            time.sleep(600)
            remove_resume_unit()
            return self.result(
                Status.ERROR, reason="systemctl reboot did not take effect"
            )

        # post-reboot verification path
        with open(marker_path, encoding="utf-8") as fh:
            marker = json.load(fh)
        remove_resume_unit()

        problems = []
        if _boot_id() == marker["boot_id"]:
            problems.append("boot id did not change - the machine did not reboot")
        pci_now = len(glob.glob("/sys/bus/pci/devices/*"))
        if pci_now != marker["pci_count"]:
            problems.append(
                "PCI device count changed across reboot (%d -> %d)"
                % (marker["pci_count"], pci_now)
            )
        failed_units = self._failed_units(ctx)
        if failed_units:
            problems.append("failed systemd units after boot: %s" % ", ".join(failed_units))

        details = {"pci_count": pci_now, "failed_units": failed_units}
        if problems:
            return self.result(Status.FAIL, reason="; ".join(problems), details=details)
        return self.result(Status.PASS, details=details)

    def _failed_units(self, ctx: RunContext):
        res = ctx.cmd(
            ["systemctl", "--failed", "--no-legend", "--plain"], timeout=30
        )
        if not res.ok:
            return []
        return [
            line.split()[0]
            for line in res.stdout.splitlines()
            if line.strip() and not line.split()[0].startswith("alma-certify")
        ]


REGISTRY.register(RebootSurvival)
