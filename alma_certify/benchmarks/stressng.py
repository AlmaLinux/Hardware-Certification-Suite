"""Reading stress-ng's metrics output by column name, in one place.

Three benchmarks each carried their own positional regex over the metrics table
and all three counted the columns wrong, so all three published a stressor's
**system time in seconds** as its throughput. The table is:

    stressor   bogo ops  real time  usr time  sys time  bogo ops/s  bogo ops/s ...
                            (secs)     (secs)    (secs) (real time) (usr+sys time)
    matrix        44136       3.00       5.99      0.00    14710.34        7373.91

A pattern of ``name (\\d+) num num (num)`` looks like it lands on a rate and
lands on sys time, which for a userspace-bound stressor is near zero: a real
run reported 0.13 bogo-ops/s for matrix and 33 MB/s for stream against a
measured 52,682 MB/s from the bundled bandwidth micro.

Naming the columns once is the fix. Positional parsing repeated per caller is
how the same mistake got made three times.
"""

from __future__ import annotations

import re
from typing import Dict, Optional

# stress-ng renamed its metrics log kind between the versions the supported releases ship:
#
#   AlmaLinux 8, stress-ng 0.15:  stress-ng: info:  [48] matrix  20842  3.00 ...
#   AlmaLinux 9, stress-ng 0.19:  stress-ng: metrc: [48] matrix  20396  3.00 ...
#
# Matching only ``metrc:`` reported nothing on AlmaLinux 8 while ``validate`` passed there, since it
# reads only the exit code. Accepting ``info:`` is safe: the row must still be numeric all the way
# across, which no other info line is.
LOG_KIND = r"(?:metrc|info):"

# Column order of the main metrics table. Anything past the sixth is optional: the
# CPU-used and RSS columns are absent from old enough builds, though both 0.15 and
# 0.19 do emit them, so a row is accepted on the first six.
ROW_FIELDS = (
    "bogo_ops",
    "real_time",
    "usr_time",
    "sys_time",
    "bogo_ops_per_sec",       # per real time - the rate to publish
    "bogo_ops_per_sec_cpu",   # per usr+sys time
    "cpu_percent",
    "rss_kb",
)

REQUIRED_COLUMNS = 6

_NUMBER_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def _numeric(token: str) -> bool:
    return bool(_NUMBER_RE.match(token))


def metrc_row(output: str, stressor: str) -> Optional[Dict[str, float]]:
    """The stressor's row from the main metrics table, as named fields.

    Returns None when the table has no row for it, which is the honest answer:
    the stressor did not report, and no number should be invented for it.
    """
    pattern = re.compile(r"%s.*?\b%s\b\s+(.*)$" % (LOG_KIND, re.escape(stressor)))
    for line in output.splitlines():
        match = pattern.search(line)
        if not match:
            continue
        tokens = match.group(1).split()
        # The miscellaneous-metrics lines start with the same stressor name and
        # one number, then words ("15060.85 MB per sec memory read rate"). Only
        # the table row is numeric all the way across.
        if len(tokens) < REQUIRED_COLUMNS:
            continue
        if not all(_numeric(token) for token in tokens[:REQUIRED_COLUMNS]):
            continue
        row = {}
        for name, token in zip(ROW_FIELDS, tokens):
            if not _numeric(token):
                break
            row[name] = float(token)
        return row
    return None


def misc_metric(output: str, stressor: str, description) -> Optional[float]:
    """A value from the miscellaneous metrics, matched on stress-ng's wording.

    These are the measurements the stressor actually took, aggregated across
    instances: nanoseconds per context switch, MB per second read rate. Worth
    more than a bogo-ops count, and they only appear when the run was long
    enough to produce them.

    ``description`` may be one wording or several to try in order, because stress-ng reworded
    these lines between the versions the supported releases ship. The stream rates in particular
    reordered the words around the number:

        0.19:  stream  3288.47 MB per sec memory read rate (harmonic mean of 1 instance)
        0.15:  stream  2317.88 memory read rate (MB per sec) (geometic mean)

    Matching only the 0.19 wording reported no memory rate at all on AlmaLinux 8, exactly as the
    log-kind change did for the metrics table. Both versions put the number first and the
    description after, so one pattern with alternative descriptions covers them.
    """
    wordings = (description,) if isinstance(description, str) else tuple(description)
    for wording in wordings:
        # ``[^0-9\n]{0,24}`` between the name and the number, because the two versions put
        # different things there:
        #
        #   0.15:  switch: (pipe) 6042.75 nanosecs per context switch (based on ...)
        #   0.19:  switch              6132.72 nanosecs per context switch (pipe method)
        #
        # So it is not only the log kind that moved. Bounded, and excluding newlines, so this
        # cannot reach across lines to pair a name with some other row's number. The description
        # is matched as a prefix: both versions append their own parenthetical after it.
        pattern = re.compile(
            r"%s.*?\b%s\b[^0-9\n]{0,24}([0-9]+(?:\.[0-9]+)?)\s+%s"
            % (LOG_KIND, re.escape(stressor), re.escape(wording))
        )
        match = pattern.search(output)
        if match:
            return float(match.group(1))
    return None
