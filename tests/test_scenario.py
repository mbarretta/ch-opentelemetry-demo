"""The demo's fault scenarios: one pure transform, two writers.

The scenarios are the walkthrough -- "watch the catalog start failing, then watch the trace"
is the demo -- so the thing worth holding in place is that the laptop and the cluster mean the
same thing by a scenario name. That is only true while `stack.scenario_flags()` is the single
definition of what a scenario does and both writers go through it, which is what the last test
here asserts by comparing the documents the two paths produce.

The trap these tests also pin down: `productCatalogFailure`'s `defaultVariant` decides nothing,
because its `targeting.if` rule always yields a variant. Editing it -- which is all the retired
`flag.sh` ever did -- looks like it worked and changes nothing.
"""

import copy
import json

import pytest

from launcher import core, stack
from launcher.eks import aws, config, flags, k8s

FLAGD_SOURCE = "src/flagd/demo.flagd.json"
READ_FLAGD = (
    f"kubectl -n {config.NS_DEMO} exec {k8s.FLAGD_DEPLOYMENT} "
    f"-c {k8s.FLAGD_CONTAINER} -- cat {flags.FLAG_FILE}"
)


@pytest.fixture
def document():
    """The chart's own flagd configuration, from the pinned upstream checkout.

    The real document rather than a fixture of our own: the transform's whole job is to survive
    contact with what upstream actually ships, three levels of nesting included.
    """
    return json.loads((core.UPSTREAM / FLAGD_SOURCE).read_text())


@pytest.fixture
def flagd_file(tmp_path, monkeypatch, document):
    """A redirected `.runtime/flagd/demo.flagd.json` holding that document."""
    monkeypatch.setattr(core, "RUNTIME", tmp_path)
    (tmp_path / "flagd").mkdir()
    path = stack.flagd_file()
    path.write_text(json.dumps(document))
    return path


def expected(document, variant):
    """`document` with the catalog fault's targeted branch set to `variant`, and nothing else."""
    wanted = copy.deepcopy(document)
    wanted["flags"][stack.FAULT_FLAG]["targeting"]["if"][1] = variant
    return wanted


# --- the pure transform ---------------------------------------------------------------------


def test_fault_targets_only_selected_product(document):
    """`targeting.if[1]` and nothing else: the then-branch of the rule that matches one product.

    Moved here from the Compose configuration tests when the transform became pure. The
    `if[2]` assertion is the point of the flag's name: the else-branch stays `off`, so the rest
    of the catalogue keeps working while one product fails.
    """
    faulty = stack.scenario_flags(document, "backend-failure")
    fault = faulty["flags"][stack.FAULT_FLAG]

    assert fault["targeting"]["if"][1] == "on"
    assert fault["targeting"]["if"][2] == "off"
    # The whole document, so anything else the transform touched would show up here.
    assert faulty == expected(document, "on")
    # And `defaultVariant` above all: a transform that wrote it would appear to work.
    assert fault["defaultVariant"] == document["flags"][stack.FAULT_FLAG]["defaultVariant"]

    assert stack.scenario_flags(faulty, "shopping") == document
    assert stack.scenario_flags(document, "budget-violation") == expected(document, "off")


def test_the_transform_is_pure(document):
    """No I/O, no shared state, and the document it was handed comes back untouched."""
    before = copy.deepcopy(document)

    first = stack.scenario_flags(document, "backend-failure")
    second = stack.scenario_flags(document, "backend-failure")

    assert document == before, "the caller's document was mutated in place"
    assert first == second
    # Two results of one input are separate documents, not two names for one.
    first["flags"][stack.FAULT_FLAG]["targeting"]["if"][1] = "off"
    assert second["flags"][stack.FAULT_FLAG]["targeting"]["if"][1] == "on"


def test_fault_variant_reads_back_what_the_transform_wrote(document):
    """The read the EKS writer confirms with, against the value the transform sets."""
    assert stack.fault_variant(document) == "off"
    assert stack.fault_variant(stack.scenario_flags(document, "backend-failure")) == "on"


@pytest.mark.parametrize(
    "broken",
    [
        {"flags": {}},
        {"flags": {stack.FAULT_FLAG: {"defaultVariant": "off"}}},
        {"flags": {stack.FAULT_FLAG: {"targeting": {"if": ["rule", "on"]}}}},
    ],
    ids=["no-such-flag", "no-targeting-rule", "not-an-if-then-else"],
)
def test_a_flagd_document_without_the_rule_is_refused(broken):
    """Refused rather than silently ignored: a scenario that did nothing is the worst outcome."""
    with pytest.raises(SystemExit, match=stack.FAULT_FLAG):
        stack.scenario_flags(broken, "backend-failure")


def test_a_disabled_flag_is_refused_rather_than_re_enabled(document):
    """flagd evaluates no targeting rule on a disabled flag, so the write would serve nothing.

    The retired writer set `state` to `ENABLED` on every scenario, which meant a flag somebody
    had turned off came back on unasked and the one case worth hearing about was never said out
    loud. Refusing is the whole reason the transform validates instead of repairing.
    """
    document["flags"][stack.FAULT_FLAG]["state"] = "DISABLED"

    with pytest.raises(SystemExit, match="would appear to apply and change nothing") as refusal:
        stack.scenario_flags(document, "backend-failure")

    assert "DISABLED" in str(refusal.value)
    # The read-back path too, which is what the EKS writer confirms a write with.
    with pytest.raises(SystemExit, match=stack.FAULT_FLAG):
        stack.fault_variant(document)


# --- the laptop writer ----------------------------------------------------------------------


def test_the_laptop_writer_applies_the_transform_and_swaps_the_file(
    flagd_file, document, capsys
):
    """flagd fsnotify-watches this file, so the writer renames onto it rather than rewriting it."""
    stack.scenario("backend-failure")

    assert json.loads(flagd_file.read_text()) == expected(document, "on")
    assert not list(flagd_file.parent.glob("*.tmp")), "the temporary file is renamed, not left"
    assert capsys.readouterr().out.strip() == stack.scenario_summary("backend-failure")

    stack.scenario("shopping")
    assert json.loads(flagd_file.read_text()) == document


def test_the_summary_names_the_scenario_and_what_it_did():
    assert "enabled" in stack.scenario_summary("backend-failure")
    for name in ("shopping", "budget-violation"):
        assert "disabled" in stack.scenario_summary(name), name
        assert name in stack.scenario_summary(name)


# --- one transform, two targets -------------------------------------------------------------


def test_both_targets_write_the_same_document(flagd_file, document, monkeypatch, fake_sh):
    """The assertion the whole split exists for: the two writers produce the same document.

    The laptop writes a file and the cluster writes through `kubectl exec`, so the only way to
    compare them is to read back what each one produced -- the file on one side, the stdin of
    the recorded exec on the other.
    """
    fake_sh.reply(READ_FLAGD, stdout=json.dumps(document))
    monkeypatch.setattr(core, "need", lambda *commands: None)
    monkeypatch.setattr(aws, "aws_login", lambda: None)
    monkeypatch.setattr(k8s, "kubeconfig", lambda: None)
    monkeypatch.setattr(flags.time, "sleep", lambda seconds: None)
    # What the container's `mv` does: the next `cat` serves what was just written, so the
    # writer's own read-back confirmation gets a straight answer instead of the pre-write copy.
    recorded = fake_sh.run

    def landing_run(*args, **kwargs):
        if kwargs.get("input"):
            fake_sh.reply(READ_FLAGD, stdout=kwargs["input"])
        return recorded(*args, **kwargs)

    monkeypatch.setattr(core, "run", landing_run)

    stack.scenario("backend-failure")
    flags.scenario_write_eks("backend-failure")

    written = [call.stdin for call in fake_sh.calls if call.stdin is not None]
    assert len(written) == 1, fake_sh.lines()
    assert json.loads(written[0]) == json.loads(flagd_file.read_text())
