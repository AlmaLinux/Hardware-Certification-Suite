"""Every field the source tools printed, kept as JSON.

The summary is a schema: named keys, one shape, a contract. It is also a decision about what is
worth keeping, made once, by whoever wrote it - and a machine whose model lives in a field nobody
promoted is unidentifiable until this suite ships again. An Ampere Altra carries its model in the
DMI processor structure's Part Number, which nothing here read.

So the whole of what dmidecode and lspci printed rides along beside the summary, parsed. The
reader composes an identity from it, and can be corrected for bundles already submitted.
"""

from alma_certify.inventory import dmi
from alma_certify.inventory.normalize import build_summary

DMIDECODE = """\
Handle 0x0001, DMI type 1, 27 bytes
System Information
\tManufacturer: LENOVO
\tProduct Name: 21ME001LUS
\tSerial Number: PF3ABCDE
\tUUID: 4c4c4544-0031-0000

Handle 0x0400, DMI type 4, 48 bytes
Processor Information
\tSocket Designation: CPU 1
\tManufacturer: Ampere(R)
\tVersion: Ampere(R) Altra(R) Processor
\tPart Number: Q80-30
\tCore Count: 80
\tAsset Tag: Not Specified

Handle 0x0700, DMI type 7, 27 bytes
Cache Information
\tSocket Designation: L3 Cache
\tInstalled Size: 32768 kB

Handle 0x2A00, DMI type 42, 12 bytes
Management Controller Host Interface
\tInterface Type: Network
"""


def _summary(redact=False):
    parsed = {"dmi": {"records": dmi.parse_dmidecode(DMIDECODE)},
              "pci": {"devices": [{
                  "slot": "0000:01:00.0", "class": "VGA compatible controller [0300]",
                  "vendor": "NVIDIA Corporation [10de]", "device": "AD102 [26b9]",
                  "driver": "nvidia", "rev": "a1", "module": "nvidia",
              }]}}
    return build_summary(parsed, redact=redact)["fields"]


def test_the_processor_structure_is_kept_whole():
    """The case that prompted this: the model is in Part Number, which nothing promoted."""
    processor = _summary()["dmi"]["processor"][0]

    assert processor["props"]["Part Number"] == "Q80-30"
    assert processor["props"]["Core Count"] == "80"


def test_structures_are_keyed_by_what_they_describe():
    """So a rule reads ``dmi.processor`` rather than knowing that a processor is type 4."""
    fields = _summary()["dmi"]

    assert set(fields) >= {"system", "processor", "cache"}


def test_a_structure_with_no_name_here_keeps_its_type_rather_than_being_dropped():
    """The table is a convenience, not a filter: an unknown structure is still a structure."""
    assert "type_42" in _summary()["dmi"]


def test_the_handle_is_kept_so_structures_can_be_joined():
    """A cache names the processor it belongs to by handle, and a memory device its array."""
    assert _summary()["dmi"]["processor"][0]["handle"] == "0x0400"


def test_every_lspci_key_is_kept_not_the_six_the_summary_uses():
    device = _summary()["pci"][0]

    assert device["rev"] == "a1"
    assert device["module"] == "nvidia"
    assert device["slot"] == "0000:01:00.0"


def test_redacting_drops_identity_from_every_structure_not_only_the_promoted_ones():
    """Serials and asset tags are on most DMI structures. Blanking the three the summary promotes
    and passing the rest through here would put them straight back in the bundle."""
    fields = _summary(redact=True)["dmi"]

    assert "Serial Number" not in fields["system"][0]["props"]
    assert "UUID" not in fields["system"][0]["props"]
    assert "Asset Tag" not in fields["processor"][0]["props"]


def test_redacting_keeps_everything_that_describes_the_part():
    """It is a redaction, not a deletion: what the machine *is* still has to come through."""
    processor = _summary(redact=True)["dmi"]["processor"][0]

    assert processor["props"]["Part Number"] == "Q80-30"
    assert processor["props"]["Version"] == "Ampere(R) Altra(R) Processor"


def test_without_redaction_the_identity_fields_are_there():
    assert _summary()["dmi"]["system"][0]["props"]["Serial Number"] == "PF3ABCDE"


def test_a_machine_with_no_dmi_still_builds_a_summary():
    """dmidecode is absent on some Arm platforms and refused in some containers."""
    fields = build_summary({"dmi": {"records": []}, "pci": {"devices": []}})["fields"]

    assert fields == {"dmi": {}, "pci": []}
