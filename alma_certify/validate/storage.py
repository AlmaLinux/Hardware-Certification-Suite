"""Storage validation: SMART health, verified I/O sanity, NVMe logs."""

from __future__ import annotations

import json
import mmap
import os
import re
import shutil
import tempfile
from typing import List

from .. import hwquery
from ..registry import REGISTRY, RunContext, Test
from ..result import Severity, Status

_DMESG_IO_ERR = re.compile(
    r"(I/O error|critical (medium|target) error|blk_update_request.*error)", re.I
)

# Filesystems that live in RAM. An I/O check against one of these measures
# memory, and passing it would claim the storage was validated.
MEMORY_FILESYSTEMS = frozenset({"tmpfs", "ramfs", "devtmpfs"})


def _fs_type(path: str) -> str:
    """The filesystem type backing a path, via the longest matching mount point."""
    best, best_type = "", ""
    try:
        with open("/proc/self/mounts", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                fields = line.split()
                if len(fields) < 3:
                    continue
                mount, fs_type = fields[1], fields[2]
                if (path == mount or path.startswith(mount.rstrip("/") + "/")) \
                        and len(mount) >= len(best):
                    best, best_type = mount, fs_type
    except OSError:
        return ""
    return best_type


def _supports_direct_io(directory: str) -> bool:
    """Whether this filesystem accepts O_DIRECT, asked by trying it.

    An aligned write, not just an open: alignment is the other half of the
    contract, and a filesystem that accepts the flag then rejects the write
    would still take fio down mid-job.
    """
    if not hasattr(os, "O_DIRECT"):
        return False
    probe = os.path.join(directory, ".alma-certify-odirect-probe")
    fd = None
    try:
        fd = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_DIRECT)
        # mmap with no file gives page-aligned memory, which is what O_DIRECT
        # wants and what a plain bytes object cannot promise.
        with mmap.mmap(-1, 4096) as buf:
            os.write(fd, buf)
    except (OSError, ValueError):
        return False
    else:
        return True
    finally:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(probe)
        except OSError:
            pass


class SmartHealth(Test):
    id = "validate.storage.smart"
    category = "storage"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 300
    packages = ("smartmontools",)

    def applicable(self, ctx: RunContext):
        if not ctx.summary.get("disks"):
            return "no fixed disks detected"
        return None

    def run(self, ctx: RunContext):
        failed: List[str] = []
        unsupported: List[str] = []
        checked: List[str] = []
        for disk in ctx.summary.get("disks", []):
            name = disk["name"]
            res = ctx.cmd(
                ["smartctl", "-j", "-H", "/dev/%s" % name],
                timeout=60,
                artifact="smartctl-%s.json" % name,
            )
            try:
                data = json.loads(res.stdout) if res.stdout.strip() else {}
            except json.JSONDecodeError:
                data = {}
            smart = data.get("smart_status")
            if not isinstance(smart, dict) or "passed" not in smart:
                unsupported.append(name)
                continue
            checked.append(name)
            if not smart["passed"]:
                failed.append(name)
            # NVMe: also gate on critical_warning
            nvme = data.get("nvme_smart_health_information_log", {})
            if nvme.get("critical_warning") not in (None, 0):
                failed.append(name)
            if nvme.get("media_errors") not in (None, 0):
                failed.append(name)
        failed = sorted(set(failed))
        details = {"checked": checked, "unsupported": unsupported, "failed": failed}
        if failed:
            return self.result(
                Status.FAIL, reason="SMART health failed: %s" % ", ".join(failed),
                details=details,
            )
        if not checked:
            return self.result(
                Status.SKIP, reason="no disk supports SMART health reporting",
                details=details,
            )
        return self.result(Status.PASS, details=details)


class FioSanity(Test):
    id = "validate.storage.io-sanity"
    category = "storage"
    run_type = "validate"
    severity = Severity.REQUIRED
    default_timeout = 600
    packages = ("fio",)

    def run(self, ctx: RunContext):
        duration = ctx.config.getint("validate", "io_smoke_seconds", 60)
        timeout = ctx.config.timeout_for(self.id, self.default_timeout)
        target_dir = ctx.config.get("benchmark", "storage_target_dir") or "/var/tmp"

        fs_type = _fs_type(target_dir)
        if fs_type in MEMORY_FILESYSTEMS:
            # Writing to RAM and reading it back says nothing about a disk, and
            # a pass here would be a lie about storage having been validated.
            return self.result(
                Status.SKIP,
                reason="%s is on %s, which is memory, not a disk - set "
                       "storage_target_dir to a location on the storage to test"
                       % (target_dir, fs_type),
            )

        free = shutil.disk_usage(target_dir).free
        size_mb = min(512, max(64, int(free / (1024 * 1024) / 10)))

        # A private working directory, not fixed names in a shared location. This test runs as root
        # by default, and storage_target_dir defaults to /var/tmp, which is world-writable. The old
        # code wrote alma-certify-fio-sanity.tmp and .alma-certify-odirect-probe at predictable
        # paths there, so any local user could pre-plant a symlink at one of those names and
        # have root truncate and overwrite whatever it pointed at. mkdtemp creates the directory
        # atomically, mode 0700 and owned by root, under a name nobody can guess, so there is
        # nothing to aim a symlink at and both the probe and the test file live inside it. The GPU
        # probe already isolates its scratch the same way.
        try:
            work = tempfile.mkdtemp(prefix="alma-certify-io-", dir=target_dir)
        except OSError as exc:
            return self.result(
                Status.ERROR,
                reason="could not create a working directory under %s: %s" % (target_dir, exc),
            )
        test_file = os.path.join(work, "fio-sanity.tmp")

        try:
            # O_DIRECT bypasses the page cache, which is what makes this a test of
            # the device rather than of RAM. Not every filesystem implements it -
            # overlayfs under a container and older kernels refuse it outright - and
            # fio simply dies with "destination does not support O_DIRECT". Ask the
            # filesystem first and adapt, rather than failing the run over it. Probed inside the
            # private directory so its scratch file is isolated too.
            direct = _supports_direct_io(work)
            io_mode = ["--direct=1"] if direct else ["--end_fsync=1"]

            dmesg_before = self._dmesg_errors(ctx)
            # Two passes, because one fio job cannot do both jobs honestly.
            #
            # Pass 1 lays the file down and reads every block back. verify only
            # means anything over data fio itself wrote: with rw=randrw the read
            # side picks random offsets, so it reads blocks this run has not
            # written yet and finds no checksum header there. time_based made it
            # worse by looping the file underneath the verify. That combination
            # reported "verify failed" on AlmaLinux 8 while el9/el10 happened to
            # get away with it - it is a bad job spec, not a bad disk, and no disk
            # was ever actually verified on any release.
            integrity = ctx.cmd(
                [
                    "fio", "--name=integrity", "--filename=%s" % test_file,
                    "--size=%dM" % size_mb, "--rw=write", "--bs=64k",
                    "--iodepth=8", "--numjobs=1",
                    "--verify=crc32c", "--do_verify=1", "--verify_fatal=1",
                    "--output-format=json",
                ] + io_mode,
                timeout=timeout,
                artifact="fio-integrity.json",
            )
            artifacts = [ctx.rel_artifact("fio-integrity.json")]
            outcome = self._classify(integrity, artifacts, "write and read back verified data")
            if outcome is not None:
                return outcome

            # Pass 2 is the mixed random workload the old job was reaching for,
            # with no verify attached - what it proves is that small random I/O in
            # both directions completes without errors, which the dmesg delta below
            # is what actually judges.
            mixed = ctx.cmd(
                [
                    "fio", "--name=mixed", "--filename=%s" % test_file,
                    "--size=%dM" % size_mb, "--rw=randrw", "--bs=4k",
                    "--iodepth=8", "--numjobs=1",
                    "--runtime=%d" % duration, "--time_based",
                    "--output-format=json",
                ] + io_mode,
                timeout=timeout,
                artifact="fio-mixed.json",
            )
            artifacts.append(ctx.rel_artifact("fio-mixed.json"))
            dmesg_after = self._dmesg_errors(ctx)

            outcome = self._classify(mixed, artifacts, "run mixed random I/O")
            if outcome is not None:
                return outcome

            new_errors = dmesg_after - dmesg_before
            if new_errors > 0:
                return self.result(
                    Status.FAIL,
                    reason="%d new I/O error(s) in dmesg during test" % new_errors,
                    artifacts=artifacts,
                )
            return self.result(
                Status.PASS,
                # Whether the page cache was in the way changes what a pass proves,
                # so it is recorded rather than left for someone to assume.
                reason=("" if direct else
                        "verified with buffered I/O: %s does not support O_DIRECT, "
                        "so some reads may have been served from the page cache"
                        % fs_type),
                details={"size_mb": size_mb, "duration_s": duration,
                         "verified": "crc32c", "direct_io": direct,
                         "filesystem": fs_type},
                artifacts=artifacts,
            )
        finally:
            # The whole directory, so the probe, the fio file, and anything fio left behind on a
            # crash all go, rather than only the one name the old _cleanup knew about.
            shutil.rmtree(work, ignore_errors=True)

    def _classify(self, res, artifacts: List[str], what: str):
        """Return a failing result for this fio pass, or None if it went fine.

        fio exits 1 both when verification finds corrupt data and when it
        rejects its own command line, so a bare returncode check reported
        "verify failed" - your disk is lying to you - for what could equally be
        an option this fio build does not have. Whether fio produced a JSON
        report separates the two: no report means the job never ran.
        """
        if res.timed_out:
            return self.result(Status.ERROR, reason="fio exceeded its timeout",
                               artifacts=artifacts)
        try:
            report = json.loads(res.stdout)
        except (TypeError, ValueError):
            report = None
        if report is None:
            if res.returncode == 0:
                return None
            detail = (res.stderr or res.stdout or "").strip().splitlines()
            return self.result(
                Status.ERROR,
                reason="fio could not %s: %s" % (
                    what, detail[-1][:200] if detail else "no output",
                ),
                artifacts=artifacts,
            )
        # error is an errno per job; fio reports the verify mismatch here too.
        errno = next((job.get("error") for job in report.get("jobs", [])
                      if job.get("error")), 0)
        if errno or res.returncode != 0:
            return self.result(
                Status.FAIL,
                reason="fio failed to %s (error %s)" % (what, errno or res.returncode),
                artifacts=artifacts,
            )
        return None

    def _dmesg_errors(self, ctx: RunContext) -> int:
        res = ctx.cmd(["dmesg", "--level=err,crit,alert,emerg"], timeout=30)
        if not res.ok:
            return 0
        return sum(1 for line in res.stdout.splitlines() if _DMESG_IO_ERR.search(line))


class NvmeErrorLog(Test):
    id = "validate.storage.nvme-errorlog"
    category = "storage"
    run_type = "validate"
    severity = Severity.CONDITIONAL
    default_timeout = 120
    packages = ("nvme-cli",)

    def applicable(self, ctx: RunContext):
        if not hwquery.nvme_devices(ctx.summary):
            return "no NVMe devices"
        return None

    def run(self, ctx: RunContext):
        problems = []
        for disk in hwquery.nvme_devices(ctx.summary):
            ctrl = disk["name"].rstrip("0123456789").rstrip("n")
            res = ctx.cmd(
                ["nvme", "smart-log", "/dev/%s" % ctrl, "-o", "json"],
                timeout=30,
                artifact="nvme-smart-%s.json" % ctrl,
            )
            try:
                data = json.loads(res.stdout)
            except (json.JSONDecodeError, ValueError):
                continue
            if data.get("critical_warning") not in (0, None):
                problems.append("%s: critical_warning=%s" % (ctrl, data["critical_warning"]))
            if data.get("media_errors") not in (0, None):
                problems.append("%s: media_errors=%s" % (ctrl, data["media_errors"]))
        if problems:
            return self.result(Status.FAIL, reason="; ".join(problems))
        return self.result(Status.PASS)


REGISTRY.register(SmartHealth)
REGISTRY.register(FioSanity)
REGISTRY.register(NvmeErrorLog)
