"""Which CPU vendor and model the collector reports, and where it reads them from.

On x86_64 ``lscpu`` names the silicon vendor directly: "Vendor ID: GenuineIntel". On
aarch64 it does not. "Vendor ID" there is the architecture licensor decoded from MIDR,
so an Ampere Altra - and a Graviton, and every other Neoverse part - reports "ARM",
while "Model name" gives the core design ("Neoverse-N1") rather than the processor.
Published as-is, a whole class of Arm servers arrives credited to the wrong company
under a name nobody sells. The firmware knows better, and the collector now asks it.
"""
from alma_certify.inventory.cpu import _identity

# Verbatim from an Ampere Altra Q80-30, 2 sockets, AlmaLinux 9.7 aarch64.
_ALTRA = {
    "Architecture": "aarch64",
    "Vendor ID": "ARM",
    "BIOS Vendor ID": "Ampere(R)",
    "Model name": "Neoverse-N1",
    "BIOS Model name": "Ampere(R) Altra(R) Processor",
}


def test_an_arm_licensor_defers_to_the_firmware_vendor():
    vendor, model = _identity(_ALTRA)

    assert vendor == "Ampere(R)"                    # not "ARM", who licensed the core
    assert model == "Ampere(R) Altra(R) Processor"  # not "Neoverse-N1", the core design


def test_x86_keeps_the_vendor_id_consumers_group_by():
    # The BIOS spelling here is "Intel(R) Corporation". Substituting it would split
    # every Intel machine in the catalog across two vendor strings for no gain.
    vendor, model = _identity({
        "Vendor ID": "GenuineIntel",
        "BIOS Vendor ID": "Intel(R) Corporation",
        "Model name": "Intel(R) Xeon(R) Gold 6430",
        "BIOS Model name": "Intel(R) Xeon(R) Gold 6430 CPU @ 2.10GHz",
    })

    assert vendor == "GenuineIntel"
    assert model == "Intel(R) Xeon(R) Gold 6430"


def test_each_field_falls_back_on_its_own():
    # Firmware that fills in one but not the other: the vendor is still an improvement
    # on the licensor, so it is taken even though the model has to stay as MIDR gave it.
    vendor, model = _identity(
        {"Vendor ID": "ARM", "BIOS Vendor ID": "Ampere(R)", "Model name": "Neoverse-N1"}
    )

    assert (vendor, model) == ("Ampere(R)", "Neoverse-N1")


def test_arm_with_no_firmware_help_reports_what_it_has():
    vendor, model = _identity({"Vendor ID": "ARM", "Model name": "Cortex-A72"})

    assert (vendor, model) == ("ARM", "Cortex-A72")


def test_missing_fields_are_empty_strings_not_none():
    assert _identity({}) == ("", "")
