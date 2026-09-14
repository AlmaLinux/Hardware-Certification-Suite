"""Killing a command, and killing every command when a run is abandoned.

Two things a stuck run needs from the process layer. A timeout that kills the command must not then
wait forever for it to die - SIGKILL does not reach a process blocked in the kernel, and a
``smartctl`` or ``nvme`` call against a drive that has stopped answering sits in uninterruptible
sleep until its I/O returns, so the unbounded wait after the kill made the timeout it was enforcing
meaningless. And the interface can now be quit part way through a run, which leaves the run's thread
detached; the commands it started are separate processes and would outlive the interface that
started them unless something reaps them.
"""

import time

from alma_certify import procutil


def test_a_timeout_kills_the_command_and_does_not_wait_forever(monkeypatch):
    """The kill is bounded. Stubbed rather than staged with a real unkillable process, because
    there is no portable way to make one - and a test that needed one would hang when it failed."""
    monkeypatch.setattr(procutil, "_KILL_GRACE", 0.1)
    monkeypatch.setattr(procutil, "_kill_group", lambda proc: None)  # a kill that does not land

    started = time.monotonic()
    result = procutil.run_cmd(["sleep", "30"], timeout=0.2)

    assert result.timed_out is True
    assert time.monotonic() - started < 10, "the wait after the kill has to be bounded"
    # Really gone, so the test does not leak a sleeper.
    procutil.kill_live()


def test_the_streaming_path_bounds_its_kill_too(monkeypatch):
    """``on_line`` takes a different route through run_cmd, and it had the same unbounded wait."""
    monkeypatch.setattr(procutil, "_KILL_GRACE", 0.1)
    monkeypatch.setattr(procutil, "_kill_group", lambda proc: None)

    started = time.monotonic()
    result = procutil.run_cmd(["sleep", "30"], timeout=0.2, on_line=lambda line: None)

    assert result.timed_out is True
    assert time.monotonic() - started < 10
    procutil.kill_live()


def test_abandoning_a_run_kills_the_commands_it_started():
    """What Quit does to a run's children. Without it they carry on writing into a run directory
    nobody is watching, after the process that started them is gone."""
    import threading

    running = threading.Event()
    done = threading.Event()

    def slow():
        running.set()
        procutil.run_cmd(["sleep", "30"], timeout=60)
        done.set()

    worker = threading.Thread(target=slow, daemon=True)
    worker.start()
    running.wait(5)
    # The command is started inside run_cmd, a moment after the event.
    for _ in range(100):
        if procutil.kill_live():
            break
        time.sleep(0.05)
    else:
        raise AssertionError("the live command was never registered")

    assert done.wait(10), "killing the group has to let the command's caller return"


def test_a_finished_command_leaves_nothing_behind_to_kill():
    """The registry is not a leak: an ordinary command is off it by the time it returns."""
    assert procutil.run_cmd(["true"], timeout=10).ok
    assert procutil.kill_live() == 0
