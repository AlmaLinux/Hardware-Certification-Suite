"""KVM validation, staged like the old suite: CPU flags → modules → boot a VM."""

from __future__ import annotations

import os
import re
import time

from .. import hwquery, procutil
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status

DOMAIN_NAME = "alma-certify-kvm-test"

# The kernel tags its own message: an optional "[  0.000000]" timestamp, then
# kvm/kvm_intel/kvm_amd, then the complaint. Requiring that prefix is what stops
# "x86/cpu: SGX disabled by BIOS" from being read as a KVM verdict.
_KVM_DISABLED_RE = re.compile(
    r"(?:^|\])\s*kvm(?:_intel|_amd)?:.*disabled by bios", re.IGNORECASE
)


class KvmFunctional(Test):
    id = "validate.virt.kvm"
    category = "virtualization"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    default_timeout = 600
    packages = ("qemu-kvm", "libvirt", "libvirt-client")

    def applicable(self, ctx: RunContext):
        if not hwquery.cpu_virt_flag():
            return "CPU has no virtualization extensions (vmx/svm)"
        return None

    def run(self, ctx: RunContext):
        # stage 1: kvm modules
        flag = hwquery.cpu_virt_flag()
        vendor_mod = "kvm_intel" if flag == "vmx" else "kvm_amd"
        res = ctx.cmd(["modprobe", vendor_mod], timeout=30)
        if not res.ok:
            # The kernel refuses to load kvm_intel/kvm_amd when firmware has
            # locked virtualization off, so a failed modprobe is the only place
            # the BIOS explanation belongs. Reading it off dmesg regardless is
            # what broke this: the scan was a bare substring search over the
            # whole boot log, and AlmaLinux 8 logs "SGX disabled by BIOS" from
            # a subsystem that has nothing to do with KVM. Every el8 run failed
            # this test on that line while KVM worked perfectly.
            return self.result(
                Status.FAIL,
                reason=(
                    "virtualization is disabled in BIOS/firmware"
                    if self._kvm_disabled_by_firmware(ctx)
                    else "could not load %s" % vendor_mod
                ),
                details={"stage": "modules", "modprobe_error": res.stderr.strip()[:200]},
            )
        if not os.path.exists("/dev/kvm"):
            return self.result(
                Status.FAIL, reason="/dev/kvm does not exist",
                details={"stage": "modules"},
            )

        # stage 2: libvirt up
        ctx.cmd(["systemctl", "start", "libvirtd"], timeout=60)
        res = ctx.cmd(["virsh", "version"], timeout=30, artifact="virsh.log")
        if not res.ok:
            return self.result(
                Status.FAIL, reason="libvirtd is not responding",
                details={"stage": "libvirt"},
            )

        # stage 3: boot a minimal transient domain using the installed kernel
        kernel = "/boot/vmlinuz-%s" % os.uname().release
        initrd = "/boot/initramfs-%s.img" % os.uname().release
        if not (os.path.exists(kernel) and os.path.exists(initrd)):
            return self.result(
                Status.ERROR,
                reason="installed kernel/initramfs not found for VM boot",
                details={"stage": "boot"},
            )
        xml = self._domain_xml(kernel, initrd)
        xml_path = ctx.artifact_path("domain.xml")
        with open(xml_path, "w", encoding="utf-8") as fh:
            fh.write(xml)

        ctx.cmd(["virsh", "destroy", DOMAIN_NAME], timeout=30)  # stale cleanup
        res = ctx.cmd(["virsh", "create", xml_path], timeout=120, artifact="virsh.log")
        if not res.ok:
            return self.result(
                Status.FAIL, reason="virsh could not create the test VM",
                details={"stage": "boot"},
                artifacts=[ctx.rel_artifact("virsh.log"), ctx.rel_artifact("domain.xml")],
            )
        try:
            deadline = time.monotonic() + 60
            state = ""
            while time.monotonic() < deadline:
                res = ctx.cmd(["virsh", "domstate", DOMAIN_NAME], timeout=30)
                state = res.stdout.strip()
                if state == "running":
                    break
                time.sleep(2)
            if state != "running":
                return self.result(
                    Status.FAIL,
                    reason="test VM did not reach running state (last: %s)" % state,
                    details={"stage": "boot"},
                )
        finally:
            ctx.cmd(["virsh", "destroy", DOMAIN_NAME], timeout=60)
        return self.result(
            Status.PASS,
            details={"virt_flag": flag, "module": vendor_mod, "vm_booted": True},
        )

    def _kvm_disabled_by_firmware(self, ctx: RunContext) -> bool:
        """Whether *KVM* said it was switched off, not merely something that was.

        Anchored to the kvm subsystem prefix the kernel puts on its own line
        ("kvm: disabled by bios", "kvm_amd: ... disabled by bios"), so a
        neighboring subsystem reporting the same about itself no longer answers
        for KVM.
        """
        dmesg = ctx.cmd(["dmesg"], timeout=30)
        if not dmesg.ok:
            return False
        return any(_KVM_DISABLED_RE.search(line) for line in dmesg.stdout.splitlines())

    def _domain_xml(self, kernel: str, initrd: str) -> str:
        return """<domain type='kvm'>
  <name>%s</name>
  <memory unit='MiB'>256</memory>
  <vcpu>1</vcpu>
  <os>
    <type arch='%s'>hvm</type>
    <kernel>%s</kernel>
    <initrd>%s</initrd>
    <cmdline>console=ttyS0 rd.emergency=poweroff</cmdline>
  </os>
  <devices>
    <serial type='pty'><target port='0'/></serial>
    <console type='pty'><target type='serial' port='0'/></console>
  </devices>
</domain>
""" % (DOMAIN_NAME, os.uname().machine, kernel, initrd)

    def teardown(self, ctx: RunContext):
        procutil.run_cmd(["virsh", "destroy", DOMAIN_NAME], timeout=30)


REGISTRY.register(KvmFunctional)
