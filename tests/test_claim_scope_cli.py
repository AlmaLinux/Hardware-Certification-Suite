"""``--scope``: a run that is a claim about one component rather than about the machine.

The flag exists because narrowing the *tests* and narrowing the *claim* are different things.
``--category gpu`` already runs only the GPU tests, and such a run is still submitted as a claim
about the whole machine: the server sees a machine validation that skipped nearly everything.
``--scope gpu`` says the claim itself is narrower, and the server then certifies the card and
nothing else, which is what makes a GPU passed through to a cloud instance certifiable at all.

The scope is refused here rather than by the server, so an operator finds out before spending an
hour on a run the catalog will not accept.
"""

import argparse

import pytest

from alma_certify.cli import SCOPE_CATEGORIES, SCOPE_KINDS, _csv, _scope, _scoped_categories


def ns(**kw):
    return argparse.Namespace(**{"scope": None, "category": None, **kw})


# --- the flag ------------------------------------------------------------------


def test_no_scope_is_a_whole_machine_run():
    """Which is every run before this flag existed, and most runs after it."""
    assert _scope(ns()) is None


def test_a_scope_is_parsed():
    assert _scope(ns(scope="gpu")) == ["gpu"]


def test_a_gpu_is_the_only_component_a_run_may_claim():
    """At the maintainer's direction, and the reason is virtualization. A component claim exists for
    hardware a guest sees as the real device, and a passed-through GPU is the only one that
    qualifies: a CPU is whatever the hypervisor exposed, a NIC may be paravirtual, and a disk is a
    virtio queue onto storage the guest cannot see."""
    from alma_certify.cli import SCOPE_KINDS

    assert SCOPE_KINDS == ("gpu",)
    assert _scope(ns(scope="gpu")) == ["gpu"]


@pytest.mark.parametrize("kind", ["cpu", "nic", "storage"])
def test_the_kinds_that_used_to_work_are_refused_with_the_reason(kind):
    """These were accepted once, so somebody may have one in a script. "Unknown kind" on its own
    would read as a typo rather than as a decision."""
    with pytest.raises(SystemExit) as caught:
        _scope(ns(scope=kind))

    message = str(caught.value)
    assert kind in message
    assert "whole-machine run on bare metal" in message
    assert "hypervisor" in message


def test_a_scope_is_deduplicated_and_ordered():
    """So two operators claiming the same thing produce the same report, and the server stores
    the same value for both."""
    assert _scope(ns(scope="gpu, gpu ,gpu")) == ["gpu"]


def test_an_unknown_kind_is_refused_with_the_known_ones():
    """A skip or an error nobody can act on is a dead end, so the message lists the alternatives."""
    with pytest.raises(SystemExit) as caught:
        _scope(ns(scope="teleporter"))

    assert "teleporter" in str(caught.value)
    for kind in SCOPE_KINDS:
        assert kind in str(caught.value)


def test_one_bad_kind_among_good_ones_is_still_refused():
    with pytest.raises(SystemExit):
        _scope(ns(scope="gpu,teleporter"))


# --- what it runs --------------------------------------------------------------


def test_a_scope_implies_its_categories():
    """Running the storage and firmware checks to prove something about a GPU wastes an hour and
    produces failures that are about the host."""
    assert _scoped_categories(["gpu"], None) == ["gpu"]


def test_a_kind_whose_category_is_named_differently_still_maps():
    """The kinds are component kinds and the categories are test categories, and for network they
    are not the same word."""
    assert _scoped_categories(["gpu"], None) == ["gpu"]


def test_several_kinds_imply_several_categories():
    assert sorted(_scoped_categories(["gpu"], None)) == ["gpu"]


def test_an_explicit_category_wins_and_is_not_narrowed():
    """Somebody who passed both means it. Silently intersecting them would skip tests they asked
    for without saying so."""
    assert _scoped_categories(["gpu"], _csv("cpu")) == ["cpu"]


def test_no_scope_leaves_the_categories_alone():
    assert _scoped_categories(None, None) is None
    assert _scoped_categories(None, _csv("cpu,gpu")) == ["cpu", "gpu"]


# --- the two halves have to agree ----------------------------------------------


def test_every_scope_kind_has_categories_behind_it():
    """The server refuses to certify a claim with no gating result in its categories, so a kind
    offered here with nothing mapped to it would produce a run that is accepted and certifies
    nothing. That is a worse outcome than not offering the kind."""
    for kind in SCOPE_KINDS:
        assert SCOPE_CATEGORIES.get(kind), kind


def test_the_offered_kinds_are_ones_the_suite_can_test():
    """Each mapped category must actually have tests in the registry, or the implied filter selects
    an empty plan and the run proves nothing."""
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    have = {cls.category for cls in REGISTRY.all() if cls.run_type == "validate"}
    for kind, categories in SCOPE_CATEGORIES.items():
        assert set(categories) & have, kind
