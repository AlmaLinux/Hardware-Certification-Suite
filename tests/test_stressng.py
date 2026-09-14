"""stress-ng metric extraction, against output captured from a real run.

The regression: three benchmarks each counted the metrics-table columns with
their own positional regex, and all three landed one column short, on **sys
time**. Published values were a stressor's seconds of system time wearing the
unit of a throughput: 0.13 bogo-ops/s for matrix, and 33 MB/s for stream on a
machine whose bundled bandwidth micro measured 52,682 MB/s.

A second regression, reported later and from the same table: on AlmaLinux 8 every
stress-ng benchmark reported nothing, while ``validate`` passed on the same
machine. stress-ng renamed its metrics log kind from ``info:`` to ``metrc:``
between the versions the supported releases ship, and the parser matched only the
newer one. ``validate`` was unaffected because it reads the exit code, never this
table, which is exactly why the two commands disagreed.

Fixtures below are verbatim output from **both** builds that matter, because one
version's output cannot show that the other still parses. Captured by running the
stressors in ``almalinux:8`` and ``almalinux:9`` containers:

  AlmaLinux 8: stress-ng 0.15.00, log kind ``info:``
  AlmaLinux 9: stress-ng 0.19.03, log kind ``metrc:``
  Workstation: stress-ng 0.21.03, log kind ``metrc:``
"""

import pytest

from alma_certify import procutil
from alma_certify.benchmarks.cpu import StressNgMatrix
from alma_certify.benchmarks.memory import StressNgStream
from alma_certify.benchmarks.sched import StressNgSwitch
from alma_certify.benchmarks.stressng import metrc_row, misc_metric
from alma_certify.config import Config
from alma_certify.registry import RunContext

MATRIX = """\
stress-ng: info:  [2778170] setting to a 3 secs run per stressor
stress-ng: metrc: [2778170] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s CPU used per       RSS Max
stress-ng: metrc: [2778170]                           (secs)    (secs)    (secs)   (real time) (usr+sys time) instance (%)          (KB)
stress-ng: metrc: [2778170] matrix            44136      3.00      5.99      0.00     14710.34        7373.91        99.75          2784
stress-ng: metrc: [2778170] miscellaneous metrics:
stress-ng: metrc: [2778170] matrix            824280.79 add matrix ops per sec (harmonic mean of 2 instances)
stress-ng: metrc: [2778170] matrix           1198112.39 copy matrix ops per sec (harmonic mean of 2 instances)
stress-ng: info:  [2778170] successful run completed in 3.00 secs
"""

SWITCH = """\
stress-ng: metrc: [2778998] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s CPU used per       RSS Max
stress-ng: metrc: [2778998] switch          1887613      3.00      0.87      4.86    629162.38      329239.32        95.55          2012
stress-ng: metrc: [2778998] miscellaneous metrics:
stress-ng: metrc: [2778998] switch              3178.48 nanosecs per context switch (pipe method) (harmonic mean of 2 instances)
"""

# --- AlmaLinux 8, stress-ng 0.15.00 -------------------------------------------
#
# Verbatim. Two things differ from the newer builds and both broke a pattern: the
# log kind is ``info:``, and the misc-metric line puts ``: (pipe)`` between the
# stressor name and its number where 0.19 puts only spaces.
EL8_MATRIX = """\
stress-ng: info:  [48] setting to a 3 second run per stressor
stress-ng: info:  [48] dispatching hogs: 1 matrix
stress-ng: info:  [48] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s CPU used per       RSS Max
stress-ng: info:  [48]                           (secs)    (secs)    (secs)   (real time) (usr+sys time) instance (%)          (KB)
stress-ng: info:  [48] matrix            20842      3.00      2.97      0.01      6946.55        6995.17        99.30          1880
stress-ng: info:  [48] successful run completed in 3.00s
"""

EL8_SWITCH = """\
stress-ng: info:  [52] switch: (pipe) 6042.75 nanosecs per context switch (based on parent run time)
stress-ng: info:  [51] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s CPU used per       RSS Max
stress-ng: info:  [51] switch           496424      3.00      0.29      2.09    165455.44      208352.50        79.41          1296
"""

STREAM = """\
stress-ng: info:  [2779491] stream: memory rate: 15048.76 MB read/sec, 10032.51 MB write/sec, 1314.98 double precision Mflop/sec (instance 1)
stress-ng: metrc: [2779488] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s CPU used per       RSS Max
stress-ng: metrc: [2779488] stream             3733     12.02     23.90      0.06       310.64         155.84        99.67         51420
stress-ng: metrc: [2779488] miscellaneous metrics:
stress-ng: metrc: [2779488] stream             15060.85 MB per sec memory read rate (harmonic mean of 2 instances)
stress-ng: metrc: [2779488] stream             10040.57 MB per sec memory write rate (harmonic mean of 2 instances)
stress-ng: metrc: [2779488] stream              1316.04 Mflop per sec (double precision) compute rate (harmonic mean of 2 instances)
"""

STREAM_TOO_SHORT = """\
stress-ng: info:  [2778941] stream: run duration too short to reliably determine memory rate
stress-ng: metrc: [2778933] stream              928      3.02      5.96      0.05       307.64         154.34        99.66         51624
stress-ng: metrc: [2778933] miscellaneous metrics:
stress-ng: metrc: [2778933] stream             12288.00 pages mmapped (geometric mean of 2 instances)
"""

# An older build, as el8 ships: no CPU-used or RSS columns.
MATRIX_OLD = """\
stress-ng: metrc: [900] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s
stress-ng: metrc: [900] matrix            12000     60.01    479.20      0.35       199.97          25.02
"""

# AlmaLinux 8, stress-ng 0.15.00. Verbatim: ``info:`` log kind, and the rate lines put the number
# first with the description reordered - "memory read rate (MB per sec)" where 0.19 writes "MB per
# sec memory read rate". Matching only the 0.19 wording reported no memory rate on this build.
STREAM_EL8 = """\
stress-ng: info:  [46] stream: memory rate: 2317.88 MB read/sec, 1545.26 MB write/sec, 202.54 Mflop/sec (instance 0)
stress-ng: info:  [46] stressor       bogo ops real time  usr time  sys time   bogo ops/s     bogo ops/s CPU used per       RSS Max
stress-ng: info:  [46] stream               17      6.15      5.74      0.34         2.76           2.80        98.81        394840
stress-ng: info:  [46] stream              2317.88 memory read rate (MB per sec) (geometic mean)
stress-ng: info:  [46] stream              1545.26 memory write rate (MB per sec) (geometic mean)
stress-ng: info:  [46] stream               202.54 memory rate (Mflop per sec) (geometic mean)
"""


# --- the table parser ---------------------------------------------------------

def test_columns_are_read_by_name():
    row = metrc_row(MATRIX, "matrix")
    assert row["bogo_ops"] == 44136
    assert row["real_time"] == 3.00
    assert row["usr_time"] == 5.99
    assert row["sys_time"] == 0.00
    assert row["bogo_ops_per_sec"] == 14710.34
    assert row["bogo_ops_per_sec_cpu"] == 7373.91


def test_the_rate_is_not_the_sys_time():
    """The whole bug in one assertion."""
    row = metrc_row(MATRIX, "matrix")
    assert row["bogo_ops_per_sec"] != row["sys_time"]
    assert row["bogo_ops_per_sec"] > 1000


def test_an_older_build_without_the_trailing_columns_still_parses():
    row = metrc_row(MATRIX_OLD, "matrix")
    assert row["bogo_ops_per_sec"] == 199.97
    assert "cpu_percent" not in row


def test_miscellaneous_lines_are_not_mistaken_for_the_table_row():
    """They begin with the same stressor name and a number."""
    row = metrc_row(STREAM, "stream")
    assert row["bogo_ops"] == 3733


def test_a_stressor_with_no_row_returns_nothing():
    assert metrc_row(MATRIX, "vm") is None
    assert metrc_row("", "matrix") is None


def test_misc_metrics_are_found_by_their_wording():
    assert misc_metric(SWITCH, "switch", "nanosecs per context switch") == 3178.48
    assert misc_metric(STREAM, "stream", "MB per sec memory read rate") == 15060.85
    assert misc_metric(STREAM, "stream", "MB per sec memory write rate") == 10040.57


def test_a_missing_misc_metric_is_absent_not_zero():
    assert misc_metric(STREAM_TOO_SHORT, "stream",
                       "MB per sec memory read rate") is None


# --- the benchmarks ----------------------------------------------------------

def make_ctx(tmp_path, output):
    ctx = RunContext(Config.load("/nonexistent"), str(tmp_path), inventory={"summary": {}})
    ctx.log = lambda msg: None
    ctx.cmd = lambda argv, **kw: procutil.CmdResult(
        argv=list(argv), returncode=0, stdout=output, stderr="")
    return ctx


def metrics_of(result):
    return {m.name: m.value for m in result.metrics}


def test_matrix_publishes_the_real_rate(tmp_path):
    result = StressNgMatrix().run(make_ctx(tmp_path, MATRIX))
    assert result.status == "pass"
    assert metrics_of(result)["bogo_ops_per_sec"] == 14710.34


def test_switch_publishes_the_real_rate_and_the_latency(tmp_path):
    result = StressNgSwitch().run(make_ctx(tmp_path, SWITCH))
    assert result.status == "pass"
    metrics = metrics_of(result)
    assert metrics["ctx_switch_rate"] == 629162.38
    # stress-ng measured this directly; the rate is a proxy for it.
    assert metrics["ctx_switch_latency"] == 3178.48


def test_the_switch_primary_is_still_the_rate(tmp_path):
    """Changing which metric is primary would repoint the leaderboard."""
    result = StressNgSwitch().run(make_ctx(tmp_path, SWITCH))
    primary = [m for m in result.metrics if m.primary]
    assert [m.name for m in primary] == ["ctx_switch_rate"]


def test_stream_publishes_megabytes_per_second(tmp_path):
    result = StressNgStream().run(make_ctx(tmp_path, STREAM))
    assert result.status == "pass"
    metrics = metrics_of(result)
    assert metrics["memory_read_rate"] == 15060.85
    assert metrics["memory_write_rate"] == 10040.57
    assert metrics["compute_rate"] == 1316.04
    # It is a cross-check for the bundled micro, so it has to be the same order
    # of magnitude as one. 33 MB/s was not.
    assert metrics["memory_read_rate"] > 1000


def test_stream_reads_the_el8_wording_of_the_rate_lines(tmp_path):
    """The reported bug: on AlmaLinux 8 the stream benchmark errored with "stress-ng reported no
    memory rate" because 0.15 writes "memory read rate (MB per sec)" where 0.19 writes "MB per sec
    memory read rate". The number and all three rates are the same measurement either way."""
    result = StressNgStream().run(make_ctx(tmp_path, STREAM_EL8))

    assert result.status == "pass"
    metrics = metrics_of(result)
    assert metrics["memory_read_rate"] == 2317.88
    assert metrics["memory_write_rate"] == 1545.26
    assert metrics["compute_rate"] == 202.54


@pytest.mark.parametrize("fixture", [STREAM, STREAM_EL8], ids=["0.19", "0.15"])
def test_the_read_rate_is_found_on_both_builds(fixture):
    """At the parser level, so a regression is caught even if the benchmark wrapper changes."""
    assert misc_metric(fixture, "stream",
                       ("MB per sec memory read rate", "memory read rate (MB per sec)")) is not None


def test_stream_units_are_what_they_claim(tmp_path):
    result = StressNgStream().run(make_ctx(tmp_path, STREAM))
    assert all(m.unit == "MB/s" for m in result.metrics
               if m.name.endswith("_rate") and m.name != "compute_rate")


def test_stream_says_so_rather_than_publishing_a_wrong_number(tmp_path):
    """A short run has no memory rate anywhere in the output.

    The table's bogo-ops column is not MB/s at any column offset, so there is
    nothing to fall back to and inventing one is how 33 MB/s got published.
    """
    result = StressNgStream().run(make_ctx(tmp_path, STREAM_TOO_SHORT))
    assert result.status == "error"
    assert "too short" in result.reason
    assert result.metrics == []


@pytest.mark.parametrize("cls,output", [
    (StressNgMatrix, "stress-ng: info: nothing useful here\n"),
    (StressNgSwitch, "stress-ng: info: nothing useful here\n"),
    (StressNgStream, "stress-ng: info: nothing useful here\n"),
])
def test_no_metrics_at_all_is_an_error(tmp_path, cls, output):
    result = cls().run(make_ctx(tmp_path, output))
    assert result.status == "error"


# --- the fix is not comparable with what it replaced --------------------------

@pytest.mark.parametrize("cls,output,stressor", [
    (StressNgMatrix, MATRIX, "matrix"),
    (StressNgSwitch, SWITCH, "switch"),
    (StressNgStream, STREAM, "stream"),
])
def test_corrected_benchmarks_declare_a_new_version(tmp_path, cls, output, stressor):
    """A leaderboard groups on (benchmark_id, benchmark_version, metric).

    Left at version 1 these results would rank against the old ones, and the
    old ones were seconds of system time. That is not a slower reading of the
    same quantity, so it must not share a ranking.
    """
    result = cls().run(make_ctx(tmp_path, output))
    assert result.details["benchmark_version"] == "2"



# --- both builds, one table ----------------------------------------------------


@pytest.mark.parametrize("output,stressor,expected", [
    pytest.param(MATRIX, "matrix", 14710.34, id="0.21-matrix"),
    pytest.param(SWITCH, "switch", 629162.38, id="0.21-switch"),
    pytest.param(EL8_MATRIX, "matrix", 6946.55, id="0.15-matrix"),
    pytest.param(EL8_SWITCH, "switch", 165455.44, id="0.15-switch"),
])
def test_the_rate_is_read_from_either_log_kind(output, stressor, expected):
    """The reported bug, from both sides.

    Before this, the 0.15 cases returned None and every stress-ng benchmark on
    AlmaLinux 8 published no metrics at all.
    """
    row = metrc_row(output, stressor)

    assert row is not None, "the metrics row was not found at all"
    assert row["bogo_ops_per_sec"] == expected


def test_the_context_switch_latency_is_read_from_either_wording():
    """Not only the log kind moved. 0.15 writes ``switch: (pipe) 6042.75 nanosecs``
    and 0.19 writes ``switch    6132.72 nanosecs``, so fixing the kind alone would
    have left this metric missing on AlmaLinux 8 while the row beside it worked."""
    assert misc_metric(EL8_SWITCH, "switch", "nanosecs per context switch") == 6042.75
    assert misc_metric(SWITCH, "switch", "nanosecs per context switch") == 3178.48


def test_an_ordinary_info_line_is_still_not_a_metrics_row():
    """What makes accepting ``info:`` safe. Every non-table line either lacks the
    stressor name or is not numeric across the first six columns, and it is that
    numeric requirement doing the work rather than the log kind."""
    noise = (
        "stress-ng: info:  [48] dispatching hogs: 1 matrix\n"
        "stress-ng: info:  [48] setting to a 3 second run per stressor\n"
        "stress-ng: info:  [55] stream: run duration too short to determine memory rate\n"
        "stress-ng: info:  [48] successful run completed in 3.00s\n"
    )

    assert metrc_row(noise, "matrix") is None
    assert metrc_row(noise, "stream") is None


def test_a_name_is_never_paired_with_another_line_s_number():
    """The bounded gap in the misc pattern must not reach across a newline.

    The prefixes here are deliberately digit-free. A first attempt used stress-ng's real
    ``[51]`` pid prefix, and the pid's own digits blocked the match on their own, so the
    test passed with the newline exclusion removed and proved nothing.
    """
    across = (
        "stress-ng: info: switch\n"
        "stress-ng: info: 6042.75 nanosecs per context switch\n"
    )

    assert misc_metric(across, "switch", "nanosecs per context switch") is None


def test_a_misc_metrics_line_is_never_mistaken_for_the_table():
    """The numeric-across requirement, on its own.

    A misc line is ``name <number> <words>``, so it satisfies the column count and its first
    column parses. Only the requirement that all six parse rejects it. Without that, this
    returns ``{"bogo_ops": 824280.79}``: a dict, so callers pass their ``is None`` check and
    then raise KeyError reaching for the rate. Tested against output with no table row in it,
    because with one present the row is found first and the misc line is never reached.
    """
    misc_only = (
        "stress-ng: metrc: [1] miscellaneous metrics:\n"
        "stress-ng: metrc: [1] matrix            824280.79 add matrix ops per sec"
        " (harmonic mean of 2 instances)\n"
    )

    assert metrc_row(misc_only, "matrix") is None
