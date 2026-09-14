"""The verified-I/O sanity check: how it drives fio, and how it reads the result.

Two regressions live here. First, the job spec asked fio to verify checksums
while doing time-based random *read/write*, so the read side hit offsets this
run had never written and found no checksum header - AlmaLinux 8's fio reported
that as a verify failure, el9 and el10 happened not to, and on no release was
anything actually verified. Second, any nonzero exit was reported as "fio verify
failed", which tells someone their disk is corrupting data when fio may simply
have rejected the command line.
"""

import json

import pytest

from alma_certify import procutil
from alma_certify.config import Config
from alma_certify.registry import RunContext
from alma_certify.validate import storage as storage_mod
from alma_certify.validate.storage import FioSanity


def fio_json(*, error=0, rc=0):
    """A minimal fio --output-format=json report, as fio actually shapes it."""
    return procutil.CmdResult(
        argv=["fio"], returncode=rc, stderr="",
        stdout=json.dumps({
            "fio version": "fio-3.19",
            "jobs": [{"jobname": "sanity", "error": error,
                      "write": {"io_bytes": 1048576}}],
        }),
    )


# What el8's fio prints when it dislikes an option: no JSON at all.
FIO_REFUSED = procutil.CmdResult(
    argv=["fio"], returncode=1, stdout="",
    stderr="fio: job 'sanity' dropped\nfio: unrecognized option 'nonsense'",
)


class FakeFio:
    """Answers each fio pass in turn, plus the dmesg scans around them."""

    def __init__(self, *, results=None, dmesg=""):
        self.results = list(results or [])
        self.dmesg = dmesg
        self.fio_calls = []

    def cmd(self, argv, timeout=None, artifact=None, check=False):
        if argv[0] == "dmesg":
            return procutil.CmdResult(argv=list(argv), returncode=0,
                                      stdout=self.dmesg, stderr="")
        if argv[0] == "fio":
            self.fio_calls.append(list(argv))
            return self.results.pop(0) if self.results else fio_json()
        raise AssertionError("unexpected call: %r" % (argv,))

    def flags(self, index):
        """The pass's argv as a set, so order does not matter to assertions."""
        return set(self.fio_calls[index])


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_mod.shutil, "disk_usage",
                        lambda path: type("DU", (), {"free": 40 * 1024 ** 3})())
    # Pinned so the tests describe behavior rather than whatever filesystem the
    # machine running them happens to provide.
    monkeypatch.setattr(storage_mod, "_fs_type", lambda path: "ext4")
    monkeypatch.setattr(storage_mod, "_supports_direct_io", lambda path: True)
    config = Config.load("/nonexistent")
    real_get = config.get
    monkeypatch.setattr(
        config, "get",
        lambda section, key, fallback="": (
            str(tmp_path) if key == "storage_target_dir"
            else real_get(section, key, fallback)
        ),
    )
    context = RunContext(config, str(tmp_path), inventory={"summary": {}})
    context.log = lambda msg: None
    return context


def run_with(ctx, fake):
    ctx.cmd = fake.cmd
    return FioSanity().run(ctx)


# --- how fio is driven --------------------------------------------------------

def test_verification_writes_the_data_it_verifies(ctx):
    """Pass 1 is a plain write with do_verify, not a random mix."""
    fake = FakeFio()
    assert run_with(ctx, fake).status == "pass"
    integrity = fake.flags(0)
    assert "--rw=write" in integrity
    assert "--verify=crc32c" in integrity
    assert "--do_verify=1" in integrity
    # time_based loops the file underneath the verify pass. It has no business
    # in a job whose whole point is reading back exactly what it wrote.
    assert not any(flag.startswith("--time_based") for flag in integrity)
    assert not any(flag.startswith("--runtime") for flag in integrity)


def test_mixed_pass_makes_no_verify_claim(ctx):
    """Pass 2 exercises random I/O both ways; dmesg is what judges it."""
    fake = FakeFio()
    run_with(ctx, fake)
    mixed = fake.flags(1)
    assert "--rw=randrw" in mixed
    assert "--time_based" in mixed
    assert not any(flag.startswith("--verify") for flag in mixed)
    assert not any(flag.startswith("--do_verify") for flag in mixed)


def _fio_target(fake, index=0):
    """The path the pass's fio was pointed at, from its --filename flag."""
    for flag in fake.fio_calls[index]:
        if flag.startswith("--filename="):
            return flag.split("=", 1)[1]
    raise AssertionError("no --filename in %r" % (fake.fio_calls[index],))


def test_both_passes_run_and_the_scratch_directory_is_removed(ctx, tmp_path):
    fake = FakeFio()
    assert run_with(ctx, fake).status == "pass"
    assert len(fake.fio_calls) == 2
    # Nothing left behind: not the file, and not the private directory it lived in.
    assert list(tmp_path.iterdir()) == []


def test_the_scratch_file_is_in_a_private_subdirectory_not_a_predictable_name(ctx, tmp_path):
    """The fix for a root-owned write to a predictable path in a world-writable directory.

    The test runs as root and storage_target_dir defaults to /var/tmp, so a fixed name there let a
    local user pre-plant a symlink and redirect root's truncating write. Both fio passes must write
    inside a freshly created private subdirectory instead, so there is no predictable path to aim a
    symlink at.
    """
    import os

    fake = FakeFio()
    run_with(ctx, fake)

    for index in (0, 1):
        target = _fio_target(fake, index)
        parent = os.path.dirname(target)
        # Not target_dir itself, but a subdirectory of it...
        assert parent != str(tmp_path)
        assert os.path.dirname(parent) == str(tmp_path)
        # ...created by mkdtemp, so a predictable name is not part of the path.
        assert os.path.basename(parent).startswith("alma-certify-io-")
    # Both passes share the one private directory rather than each inventing a path.
    assert os.path.dirname(_fio_target(fake, 0)) == os.path.dirname(_fio_target(fake, 1))


def test_the_scratch_directory_is_owner_only(ctx, tmp_path, monkeypatch):
    """0700, so even before cleanup no other local user can enter it or plant a file inside.

    Captured during the run, because the directory is removed when the run finishes; mkdtemp
    creates it 0700 atomically, and this pins that it is mkdtemp doing the work.
    """
    import os
    import tempfile as tempfile_mod

    seen = {}
    real_mkdtemp = tempfile_mod.mkdtemp

    def recording_mkdtemp(*args, **kwargs):
        path = real_mkdtemp(*args, **kwargs)
        seen["path"] = path
        seen["mode"] = os.stat(path).st_mode & 0o777
        return path

    monkeypatch.setattr(storage_mod.tempfile, "mkdtemp", recording_mkdtemp)
    run_with(ctx, FakeFio())

    assert seen["mode"] == 0o700
    assert not os.path.exists(seen["path"]), "the private directory must be cleaned up"


# --- reading the outcome ------------------------------------------------------

def test_fio_refusing_the_job_is_an_error_not_a_disk_failure(ctx):
    """The el8 symptom: exit 1 with no report means nothing was measured."""
    fake = FakeFio(results=[FIO_REFUSED])
    result = run_with(ctx, fake)
    assert result.status == "error"
    assert "verify failed" not in result.reason
    assert "unrecognized option" in result.reason


def test_a_real_verify_mismatch_still_fails(ctx):
    """errno 84 is EILSEQ - fio's report for a checksum that did not match."""
    fake = FakeFio(results=[fio_json(error=84, rc=1)])
    result = run_with(ctx, fake)
    assert result.status == "fail"
    assert "84" in result.reason
    assert "write and read back verified data" in result.reason


def test_a_failing_mixed_pass_is_attributed_to_the_mixed_pass(ctx):
    fake = FakeFio(results=[fio_json(), fio_json(error=5, rc=1)])
    result = run_with(ctx, fake)
    assert result.status == "fail"
    assert "mixed random I/O" in result.reason


def test_a_verify_failure_skips_the_mixed_pass(ctx):
    """No point hammering a device that just failed to return its own data."""
    fake = FakeFio(results=[fio_json(error=84, rc=1)])
    run_with(ctx, fake)
    assert len(fake.fio_calls) == 1


def test_timeout_is_an_error(ctx):
    timed_out = procutil.CmdResult(argv=["fio"], returncode=-9, stdout="",
                                   stderr="", timed_out=True)
    result = run_with(ctx, FakeFio(results=[timed_out]))
    assert result.status == "error"
    assert "timeout" in result.reason


def test_new_io_errors_in_dmesg_fail_the_check(ctx):
    fake = FakeFio(dmesg="[ 9.1] blk_update_request: I/O error, dev sda, sector 42\n")
    # Same log before and after, so the delta is zero and this passes; the
    # count only means something as a change across the workload.
    assert run_with(ctx, fake).status == "pass"


def test_io_errors_appearing_during_the_run_fail_the_check(ctx):
    class Appearing(FakeFio):
        def cmd(self, argv, timeout=None, artifact=None, check=False):
            if argv[0] == "fio":
                self.dmesg += "[ 9.1] blk_update_request: I/O error, dev sda\n"
            return super().cmd(argv, timeout=timeout, artifact=artifact, check=check)

    result = run_with(ctx, Appearing())
    assert result.status == "fail"
    assert "new I/O error" in result.reason


# --- adapting to the filesystem underneath ------------------------------------

def test_direct_io_is_used_where_the_filesystem_supports_it(ctx):
    fake = FakeFio()
    result = run_with(ctx, fake)
    assert "--direct=1" in fake.flags(0)
    assert "--direct=1" in fake.flags(1)
    assert result.details["direct_io"] is True
    assert result.reason == ""


def test_buffered_fallback_when_o_direct_is_refused(ctx, monkeypatch):
    """The symptom was `fio: destination does not support O_DIRECT`.

    Overlayfs under a container and older kernels refuse the flag outright.
    Losing the whole storage check over it is worse than testing through the
    page cache and saying so.
    """
    monkeypatch.setattr(storage_mod, "_supports_direct_io", lambda path: False)
    monkeypatch.setattr(storage_mod, "_fs_type", lambda path: "overlay")
    fake = FakeFio()
    result = run_with(ctx, fake)

    assert result.status == "pass"
    for index in (0, 1):
        assert "--direct=1" not in fake.flags(index)
        assert "--end_fsync=1" in fake.flags(index)
    assert result.details["direct_io"] is False
    assert result.details["filesystem"] == "overlay"
    # A weaker check than the direct one, so the report says which it was.
    assert "buffered" in result.reason
    assert "page cache" in result.reason


@pytest.mark.parametrize("fs_type", ["tmpfs", "ramfs", "devtmpfs"])
def test_a_ram_backed_target_is_skipped_not_passed(ctx, monkeypatch, fs_type):
    """Passing an I/O check run against RAM would claim a disk was validated."""
    monkeypatch.setattr(storage_mod, "_fs_type", lambda path: fs_type)
    fake = FakeFio()
    result = run_with(ctx, fake)

    assert result.status == "skip"
    assert fs_type in result.reason
    assert "storage_target_dir" in result.reason
    assert fake.fio_calls == []


# --- the filesystem probes ----------------------------------------------------

def test_fs_type_prefers_the_longest_matching_mount(tmp_path, monkeypatch):
    mounts = tmp_path / "mounts"
    mounts.write_text(
        "/dev/sda1 / ext4 rw 0 0\n"
        "tmpfs /var tmpfs rw 0 0\n"
        "/dev/sdb1 /var/lib xfs rw 0 0\n"
    )
    real_open = storage_mod.open if hasattr(storage_mod, "open") else open
    monkeypatch.setattr(
        "builtins.open",
        lambda path, *a, **k: (real_open(str(mounts), *a, **k)
                               if path == "/proc/self/mounts"
                               else real_open(path, *a, **k)),
    )
    assert storage_mod._fs_type("/var/lib/alma-certify") == "xfs"
    assert storage_mod._fs_type("/var/tmp") == "tmpfs"
    assert storage_mod._fs_type("/home/someone") == "ext4"
    # "/variant" starts with "/var" as a string but is not under that mount.
    assert storage_mod._fs_type("/variant") == "ext4"


def test_direct_io_probe_leaves_nothing_behind(tmp_path):
    storage_mod._supports_direct_io(str(tmp_path))
    assert list(tmp_path.iterdir()) == []


def test_direct_io_probe_says_no_when_the_directory_is_unwritable(tmp_path):
    assert storage_mod._supports_direct_io(str(tmp_path / "does-not-exist")) is False
