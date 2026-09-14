"""The survey-facing collection additions: x86-64 level and /etc/machine-id."""
from __future__ import annotations

from alma_certify import report
from alma_certify.inventory import _machine_id


class _Uname:
    def __init__(self, machine):
        self.machine = machine


def _with_flags(monkeypatch, machine, flags):
    monkeypatch.setattr(report.os, "uname", lambda: _Uname(machine))
    monkeypatch.setattr(
        report.procutil, "read_file",
        lambda path, default="": "flags\t: fpu vme " + " ".join(sorted(flags)),
    )


def test_x86_64_level_detects_v3(monkeypatch):
    _with_flags(monkeypatch, "x86_64", report._X86_64_V3)
    assert report._x86_64_level() == "v3"


def test_x86_64_level_detects_v2_when_avx2_is_absent(monkeypatch):
    _with_flags(monkeypatch, "x86_64", report._X86_64_V2)
    assert report._x86_64_level() == "v2"


def test_x86_64_level_detects_v4(monkeypatch):
    _with_flags(monkeypatch, "x86_64", report._X86_64_V4)
    assert report._x86_64_level() == "v4"


def test_x86_64_level_is_blank_off_x86(monkeypatch):
    monkeypatch.setattr(report.os, "uname", lambda: _Uname("aarch64"))
    assert report._x86_64_level() == ""


def test_machine_id_returns_a_string_or_none():
    # Reads /etc/machine-id directly; on any real host it is a hex string, and a
    # stripped image yields None. Either is acceptable - it must not raise.
    result = _machine_id()
    assert result is None or isinstance(result, str)
