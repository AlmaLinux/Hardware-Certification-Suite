"""The run planner: what a guided run builds, independent of any interface that renders it.

The invariant worth stating first: **the planner produces commands, it does not perform runs.** So
the most valuable test here is that every argv it can build parses with the real CLI parser. A
front-end that could produce a command the CLI rejects would be a second, wrong definition of what a
run is. These moved out of the old curses module when it was retired; the Textual interface reuses
this same planner, and its own navigation is covered in test_textual_tui.py.
"""

import pytest

from alma_certify import cli, planning


@pytest.mark.parametrize("what,expected", [
    ("validate", ["validate"]),
    ("benchmark", ["benchmark"]),
    ("run", ["run"]),
])
def test_the_plainest_plan_is_the_plainest_command(what, expected):
    assert planning.Plan(what).argv() == expected


def test_every_command_the_planner_can_build_parses():
    """The invariant this module exists to keep. ``benchmark`` and ``validate`` do not take the same
    flags, so a planner that can produce a command the CLI would reject is not hypothetical."""
    parser = cli.build_parser()
    built = 0
    for what in ("validate", "benchmark", "run"):
        for scope in ([], ["gpu"], ["cpu", "gpu"], list(cli.SCOPE_KINDS)):
            for categories in ([], ["cpu"], ["cpu", "memory"]):
                for gpu in (planning.GPU_ASK, planning.GPU_YES, planning.GPU_NO):
                    for pre_release in (False, True):
                        for submit in (True, False):
                            for publish_after in (None, "2027-01-01"):
                                plan = planning.Plan(what)
                                plan.scope = scope
                                plan.categories = [] if scope else categories
                                plan.gpu = gpu
                                plan.pre_release = pre_release
                                plan.publish_after = publish_after
                                plan.submit = submit
                                parser.parse_args(plan.argv())
                                built += 1
    assert built == 3 * 4 * 3 * 3 * 2 * 2 * 2





def test_a_scope_becomes_one_comma_separated_flag():
    plan = planning.Plan("run")
    plan.scope = ["gpu", "cpu"]

    assert plan.argv() == ["run", "--scope", "gpu,cpu"]


def test_asking_about_the_gpu_driver_adds_no_flag():
    """The default is not a flag: doing nothing means "ask me when we get there", a third answer and
    not the absence of one."""
    plan = planning.Plan("run")
    plan.gpu = planning.GPU_ASK

    assert "--gpu-setup" not in plan.argv()
    assert "--no-gpu-setup" not in plan.argv()


@pytest.mark.parametrize("gpu,flag", [
    (planning.GPU_YES, "--gpu-setup"),
    (planning.GPU_NO, "--no-gpu-setup"),
])
def test_the_other_two_gpu_answers_are_the_two_flags(gpu, flag):
    plan = planning.Plan("run")
    plan.gpu = gpu

    assert flag in plan.argv()


def test_the_flags_that_hold_results_back():
    plan = planning.Plan("validate")
    plan.pre_release = True
    plan.submit = False

    assert plan.argv() == ["validate", "--pre-release", "--no-submit"]


def test_an_embargo_date_is_a_publish_after_flag():
    plan = planning.Plan("validate")
    plan.pre_release = True
    plan.publish_after = "2027-03-01"

    assert plan.argv() == ["validate", "--pre-release", "--publish-after", "2027-03-01"]


def test_no_embargo_date_adds_no_publish_after():
    """Holding until a person lifts it is the default: --pre-release with no date."""
    plan = planning.Plan("validate")
    plan.pre_release = True

    assert "--publish-after" not in plan.argv()


def test_the_command_shown_is_the_command_run():
    plan = planning.Plan("run")
    plan.scope = ["gpu"]

    assert plan.command() == "alma-certify " + " ".join(plan.argv())


def test_a_full_machine_validation_certifies_the_whole_system():
    assert planning.Plan("validate").certifies_whole_system() is True
    assert planning.Plan("run").certifies_whole_system() is True


def test_a_narrowed_or_speed_only_run_is_supporting_evidence_not_a_certification():
    # A hand-picked set of categories does not cover the machine.
    picked = planning.Plan("validate")
    picked.categories = ["cpu"]
    assert picked.certifies_whole_system() is False

    # Neither does a single scope.
    scoped = planning.Plan("validate")
    scoped.scope = ["gpu"]
    assert scoped.certifies_whole_system() is False

    # A benchmark measures speed and never certifies, even run whole and complete.
    assert planning.Plan("benchmark").certifies_whole_system() is False


def test_the_hub_shows_every_setting_with_its_current_value():
    plan = planning.Plan("run")
    plan.scope = ["gpu"]
    plan.gpu = planning.GPU_NO
    plan.pre_release = True
    plan.submit = False

    rendered = {item.label: item.detail for item in plan.rows()}

    assert rendered["What it certifies"] == "gpu"
    assert rendered["Missing GPU driver"] == "never install anything"
    assert rendered["Unreleased hardware"].startswith("yes")
    assert rendered["Upload when finished"] == "no"
    assert rendered["Categories"] == "all of them"


def test_the_run_type_has_one_writer():
    """Both routes to a run type go through ``set_what``, so the categories reset cannot be
    half-applied: ``alma-certify benchmark --category ipmi`` selects nothing, runs nothing,
    exits 0."""
    plan = planning.Plan("validate")
    plan.categories = ["ipmi"]

    plan.set_what("validate")
    assert plan.categories == ["ipmi"], "the same type is not a change"

    plan.set_what("benchmark")
    assert plan.categories == []


def test_the_categories_offered_are_the_ones_a_run_would_execute():
    """From the registry, not a list kept here, so a category arriving with a new test appears
    without anybody remembering to update the interface, and not the raw registry either: over
    ``REGISTRY.all()`` it offered ``usb``, whose only tests need a person to plug something in, so a
    default run that selected it ran nothing and exited 0."""
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    for what, run_types in (("validate", ("validate",)), ("benchmark", ("benchmark",)),
                            ("run", ("validate", "benchmark"))):
        offered = planning.categories_for(what)
        assert offered, what
        expected = sorted({
            cls.category for run_type in run_types for cls in REGISTRY.select(run_type)
        })
        assert offered == expected


def test_an_interactive_only_category_is_not_offered():
    """``usb`` is the live example: every test in it needs somebody at the machine, so a default run
    that selected it would run nothing at all."""
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    interactive_only = {
        cls.category for cls in REGISTRY.all()
        if cls.run_type == "validate" and cls.interactive
    } - {cls.category for cls in REGISTRY.select("validate")}

    assert interactive_only, "the premise of this test has gone away; check whether usb changed"
    assert not interactive_only & set(planning.categories_for("validate"))


def test_a_run_that_could_select_nothing_is_not_reachable():
    """Whatever the planner hands off, the tests it selects are not empty."""
    from alma_certify.registry import REGISTRY, load_all_tests

    load_all_tests()
    for what in ("validate", "benchmark", "run"):
        for category in planning.categories_for(what):
            plan = planning.Plan(what)
            plan.categories = [category]
            run_types = ("validate", "benchmark") if what == "run" else (what,)
            selected = [
                cls for run_type in run_types
                for cls in REGISTRY.select(run_type, categories=[category])
            ]
            assert selected, "%s would run nothing" % plan.command()


def test_a_survey_is_just_collect_with_no_selection_flags():
    plan = planning.Plan("collect")
    assert plan.argv() == ["collect"]
    # A survey measures nothing and claims nothing.
    assert plan.certifies_whole_system() is False


def test_switching_to_a_survey_drops_choices_that_cannot_apply():
    """Backing into a survey must not leave a scope or an embargo silently set - and
    ``collect`` does not accept the selection flags at all, so none may reach argv."""
    plan = planning.Plan("validate")
    plan.scope = ["gpu"]
    plan.categories = ["gpu"]
    plan.pre_release = True
    plan.publish_after = "2027-01-01"

    plan.set_what("collect")

    assert plan.scope == []
    assert plan.categories == []
    assert plan.pre_release is False
    assert plan.publish_after is None
    assert plan.argv() == ["collect"]


def test_a_survey_still_honours_the_upload_choice():
    plan = planning.Plan("collect")
    plan.submit = False
    assert plan.argv() == ["collect", "--no-submit"]


def test_the_interface_cannot_turn_off_certificate_checking():
    """``--allow-self-signed`` is a developer's flag for a dev or staging catalog, and a
    checkbox for it in a guided interface is an invitation to turn off certificate
    verification without meaning to. It is reachable by typing it, not by clicking it:
    ``alma-certify --allow-self-signed tui`` carries it into the run."""
    assert not hasattr(planning.Plan("run"), "allow_self_signed")
    for what in ("run", "validate", "benchmark", "collect"):
        assert "--allow-self-signed" not in planning.Plan(what).argv()
