"""DIMM topology and socket count, from real dmidecode output.

Both matter for reading a benchmark: an all-core score roughly doubles with a
second socket, and memory bandwidth tracks how the channels are populated as
much as how much capacity is installed.

The regression here is a unit spelling. dmidecode 3.5 changed "Size: 16 GB" to
"Size: 16 GiB", and the size table only knew the old spellings, so every DIMM on
any machine with a current dmidecode came back with no capacity at all. The
fixture is a verbatim block from a ThinkPad P16s Gen 2 that exhibited it.
"""

from alma_certify.inventory.dmi import parse_dmidecode
from alma_certify.inventory.normalize import _memory

# ThinkPad P16s Gen 2, soldered LPDDR5, dmidecode 3.6 (IEC units).
LPDDR5_IEC = """\
Handle 0x0009, DMI type 17, 92 bytes
Memory Device
\tArray Handle: 0x0006
\tTotal Width: 32 bits
\tData Width: 32 bits
\tSize: 16 GiB
\tForm Factor: Other
\tLocator: DIMM 0
\tBank Locator: P0 CHANNEL A
\tType: LPDDR5
\tSpeed: 6400 MT/s
\tManufacturer: Micron Technology
\tPart Number: MT62F4G32D8DV-026 WT
\tRank: 2
\tConfigured Memory Speed: 6400 MT/s
"""

# HP ProLiant DL360 Gen9, registered DDR4, older dmidecode (decimal spelling).
DDR4_OLD_UNITS = """\
Handle 0x1001, DMI type 17, 40 bytes
Memory Device
\tSize: 32 GB
\tLocator: PROC 1 DIMM 1
\tBank Locator: Not Specified
\tType: DDR4
\tSpeed: 2400 MT/s
\tManufacturer: UNKNOWN
\tPart Number: NOT AVAILABLE
\tRank: 2
\tConfigured Memory Speed: 1600 MT/s
"""

EMPTY_SLOT = """\
Handle 0x1002, DMI type 17, 40 bytes
Memory Device
\tSize: No Module Installed
\tLocator: PROC 1 DIMM 2
\tType: Unknown
"""

GIB = 1024 ** 3


def dimms_from(*blocks):
    records = parse_dmidecode("\n".join(blocks))
    by_type = {}
    for record in records:
        by_type.setdefault(record["dmi_type"], []).append(record)
    return _memory({"total_bytes": 0}, by_type)


def test_iec_sizes_are_understood():
    """"16 GiB" is what dmidecode 3.5 and later print."""
    memory = dimms_from(LPDDR5_IEC)
    assert memory["dimms"][0]["size_bytes"] == 16 * GIB


def test_the_older_decimal_spelling_still_works():
    """el8 and el9 ship builds that print "32 GB" for the same quantity."""
    memory = dimms_from(DDR4_OLD_UNITS)
    assert memory["dimms"][0]["size_bytes"] == 32 * GIB


def test_both_spellings_mean_the_same_thing():
    assert (dimms_from(LPDDR5_IEC)["dimms"][0]["size_bytes"]
            == dimms_from("Handle 0x0, DMI type 17, 40 bytes\nMemory Device\n"
                          "\tSize: 16 GB\n\tLocator: X\n")["dimms"][0]["size_bytes"])


def test_configured_speed_wins_and_the_rated_one_is_kept():
    """A 2400 module the platform clocks at 1600 is a fact about the platform."""
    dimm = dimms_from(DDR4_OLD_UNITS)["dimms"][0]
    assert dimm["speed_mts"] == 1600
    assert dimm["rated_speed_mts"] == 2400


def test_channel_and_rank_are_recorded():
    """Channel population and rank move bandwidth more than capacity does."""
    dimm = dimms_from(LPDDR5_IEC)["dimms"][0]
    assert dimm["bank_locator"] == "P0 CHANNEL A"
    assert dimm["rank"] == 2


def test_empty_slots_are_counted_but_not_listed():
    """Room to expand is part of the picture; a blank slot is not a module."""
    memory = dimms_from(LPDDR5_IEC, DDR4_OLD_UNITS, EMPTY_SLOT)
    assert len(memory["dimms"]) == 2
    assert memory["slots_populated"] == 2
    assert memory["slots_total"] == 3


def test_an_unparseable_size_is_absent_rather_than_zero():
    """Zero capacity would be a claim; None says the firmware did not say."""
    memory = dimms_from("Handle 0x0, DMI type 17, 40 bytes\nMemory Device\n"
                        "\tSize: enormous\n\tLocator: X\n\tType: DDR4\n")
    assert memory["dimms"][0]["size_bytes"] is None


def test_a_machine_with_no_memory_devices_reports_none():
    memory = dimms_from("")
    assert memory["dimms"] == []
    assert memory["slots_total"] is None
