"""launcher.eks.flags: `demo.py eks flag`, the port of `flag.sh`.

Three things go wrong with feature flags mid-demo, and each has a test here. A flag or a
variant misspelled, which flagd accepts and then silently falls back on -- so both are
validated against the live document before anything is written. A write that reaches a pod
about to be replaced -- so the document is read back and checked rather than trusted. And
`productCatalogFailure`, whose `defaultVariant` this command will not touch because its
targeting rule always yields a variant: setting it here would appear to work and change
nothing, which is why `demo.py scenario` exists and why the help says so.

The write itself is `cat > F.tmp && mv F.tmp F` inside the flagd-ui container, asserted as argv:
the inode swap is what flagd's fsnotify watch reports as a single change.
"""

import argparse
import json

import pytest

from launcher import cli, core, stack
from launcher.eks import aws, config, flags, k8s

READ_FLAGD = (
    f"kubectl -n {config.NS_DEMO} exec {k8s.FLAGD_DEPLOYMENT} "
    f"-c {k8s.FLAGD_CONTAINER} -- cat {flags.FLAG_FILE}"
)
WRITE_SCRIPT = (
    f"cat > {flags.FLAG_FILE}.tmp && mv {flags.FLAG_FILE}.tmp {flags.FLAG_FILE}"
)

# A cut-down flagd document: one boolean flag, one multi-variant flag, and the catalog fault
# with the targeting rule that makes it a scenario rather than a flag.
DOCUMENT = {
    "$schema": "https://flagd.dev/schema/v0/flags.json",
    "flags": {
        "paymentUnreachable": {
            "description": "Payment service is unavailable",
            "state": "ENABLED",
            "defaultVariant": "off",
            "variants": {"off": False, "on": True},
        },
        "imageSlowLoad": {
            "state": "ENABLED",
            "defaultVariant": "off",
            "variants": {"10sec": 10000, "5sec": 5000, "off": 0},
        },
        stack.FAULT_FLAG: {
            "state": "ENABLED",
            "defaultVariant": "off",
            "targeting": {"if": [{"==": [{"var": "product_id"}, "OLJCESPC7Z"]}, "off", "off"]},
            "variants": {"off": False, "on": True},
        },
    },
}


@pytest.fixture
def cluster(fake_sh, monkeypatch):
    """A fake cluster serving `DOCUMENT` from the flagd-ui container, where writes land.

    `fake_sh` answers a command line from a fixed table, and both reads of the flag file are
    the same command line, so without this the read-back after a write would serve the
    pre-write copy and every confirmation would fail against a cluster that in fact took the
    change. The recorded write updates the answer, which is what the container's `mv` does.

    The session and the kubeconfig are stubbed rather than recorded: their argv is the subject
    of tests/test_eks_aws.py and tests/test_eks_k8s.py, and leaving them in would bury the
    exec calls these assertions are about.
    """
    fake_sh.reply(READ_FLAGD, stdout=json.dumps(DOCUMENT))
    recorded = fake_sh.run

    def run(*args, **kwargs):
        if kwargs.get("input"):
            fake_sh.reply(READ_FLAGD, stdout=kwargs["input"])
        return recorded(*args, **kwargs)

    monkeypatch.setattr(core, "run", run)
    monkeypatch.setattr(core, "need", lambda *commands: None)
    monkeypatch.setattr(aws, "aws_login", lambda: None)
    monkeypatch.setattr(k8s, "kubeconfig", lambda: None)
    monkeypatch.setattr(flags.time, "sleep", lambda seconds: None)
    return fake_sh


def writes(shell):
    """Every recorded write of the flag file: the execs that carried a document on stdin."""
    return [call for call in shell.calls if call.stdin is not None]


def reads(shell):
    """Every recorded read of the flag file."""
    return [call for call in shell.calls if call.line == READ_FLAGD]


def flag_parser():
    """The `eks flag` parser alone, declared the way `eks.register()` declares it."""
    subparsers = argparse.ArgumentParser().add_subparsers()
    flags.register(subparsers)
    return subparsers.choices["flag"]


# --- reading ---------------------------------------------------------------------------------


def test_listing_reads_the_watched_copy_and_reports_every_flag(cluster, capsys):
    """The file inside the pod, not `cm/flagd-config`: the ConfigMap is not what flagd reads."""
    flags.flag()

    assert cluster.lines() == [READ_FLAGD]
    out = capsys.readouterr().out
    # Sorted by name, each with its current variant and the variants it accepts.
    names = [line.split()[0] for line in out.splitlines() if line.startswith("  ")]
    assert names == sorted(DOCUMENT["flags"])
    assert "[10sec, 5sec, off]" in out
    assert not writes(cluster)


def test_showing_one_flag_names_its_variants_and_writes_nothing(cluster, capsys):
    flags.flag("imageSlowLoad")

    assert 'imageSlowLoad is "off"' in capsys.readouterr().out
    assert not writes(cluster)


# --- validation ------------------------------------------------------------------------------


def test_an_unknown_flag_and_an_unknown_variant_fail_differently(cluster):
    """Two different mistakes, two different messages: one is a typo, the other is a guess."""
    with pytest.raises(SystemExit) as unknown_flag:
        flags.flag("paymentUnreachible", "on")

    with pytest.raises(SystemExit) as unknown_variant:
        flags.flag("paymentUnreachable", "onn")

    assert "no such flag: paymentUnreachible" in str(unknown_flag.value)
    # And the flags that do exist, because nobody has the chart's fifteen memorised.
    assert "paymentUnreachable" in str(unknown_flag.value)
    assert str(unknown_variant.value).startswith("error: 'onn' is not a variant")
    assert "available: off, on" in str(unknown_variant.value)
    assert str(unknown_flag.value) != str(unknown_variant.value)
    assert not writes(cluster), "nothing is written until both the flag and the variant check out"


def test_the_catalog_fault_is_refused_and_names_the_command_that_works(cluster):
    """Its targeting rule always yields a variant, so this write would look fine and do nothing."""
    with pytest.raises(SystemExit) as refusal:
        flags.flag(stack.FAULT_FLAG, "on")

    assert "demo.py scenario" in str(refusal.value)
    assert not writes(cluster)


def test_reset_takes_no_flag_name(fake_sh, monkeypatch):
    """Otherwise `eks flag paymentUnreachable on --reset` would discard the set in silence."""
    monkeypatch.setattr(aws, "aws_login", lambda: pytest.fail("a refusal reaches no session"))

    with pytest.raises(SystemExit, match="takes no flag name"):
        flags.flag("paymentUnreachable", "on", reset=True)

    assert fake_sh.lines() == []


# --- writing ---------------------------------------------------------------------------------


def test_setting_a_variant_swaps_the_file_and_reads_it_back(cluster, capsys):
    flags.flag("imageSlowLoad", "5sec")

    written = writes(cluster)
    assert len(written) == 1
    assert written[0].argv == [
        "kubectl",
        "-n",
        config.NS_DEMO,
        "exec",
        "-i",
        k8s.FLAGD_DEPLOYMENT,
        "-c",
        k8s.FLAGD_CONTAINER,
        "--",
        "sh",
        "-c",
        WRITE_SCRIPT,
    ], "the temporary file and the move are what fsnotify sees as one change"

    document = json.loads(written[0].stdin)
    assert document["flags"]["imageSlowLoad"]["defaultVariant"] == "5sec"
    # Only that flag: a write that reshaped the rest of the document would strand the others.
    assert document["flags"]["paymentUnreachable"] == DOCUMENT["flags"]["paymentUnreachable"]

    assert len(reads(cluster)) == 2, "the file is read back rather than the write trusted"
    assert "imageSlowLoad: off -> 5sec" in capsys.readouterr().out


def test_a_variant_that_is_already_set_writes_nothing(cluster, capsys):
    """The no-op matters: the write costs a flagd re-read, and re-reads drop in-flight evaluations."""
    flags.flag("paymentUnreachable", "off")

    assert 'paymentUnreachable is already "off"' in capsys.readouterr().out
    assert not writes(cluster)
    assert len(reads(cluster)) == 1


def test_a_write_that_does_not_land_is_reported(cluster, monkeypatch):
    """A pod being replaced mid-exec takes the write with it, and says nothing about it."""
    monkeypatch.setattr(flags, "write_document", lambda document: None)

    with pytest.raises(SystemExit, match="did not take"):
        flags.flag("paymentUnreachable", "on")

    assert len(reads(cluster)) == 2


def test_an_unparseable_document_is_refused(cluster):
    cluster.reply(READ_FLAGD, stdout="{ not json")

    with pytest.raises(SystemExit, match="could not parse"):
        flags.flag("paymentUnreachable", "on")


# --- reset -----------------------------------------------------------------------------------


def test_reset_restarts_flagd_and_waits_for_the_roll_out(cluster):
    """The restart is the only way back: a toggle lives in the pod, not in the ConfigMap."""
    flags.flag(reset=True)

    assert cluster.lines() == [
        f"kubectl -n {config.NS_DEMO} rollout restart {k8s.FLAGD_DEPLOYMENT}",
        f"kubectl -n {config.NS_DEMO} rollout status {k8s.FLAGD_DEPLOYMENT} "
        f"--timeout={flags.ROLLOUT_TIMEOUT}",
    ]
    assert flags.ROLLOUT_TIMEOUT == "120s"


# --- the scenario writer ---------------------------------------------------------------------


def test_scenario_write_eks_applies_the_shared_transform_and_confirms_it(cluster, capsys):
    """`targeting.if[1]`, through the same temp-file-and-move exec, then read back and checked."""
    flags.scenario_write_eks("backend-failure")

    written = writes(cluster)
    assert len(written) == 1
    assert written[0].argv[-1] == WRITE_SCRIPT
    assert json.loads(written[0].stdin) == stack.scenario_flags(DOCUMENT, "backend-failure")
    assert stack.fault_variant(json.loads(written[0].stdin)) == "on"
    # Untouched, which is the trap the retired flag.sh fell into.
    assert json.loads(written[0].stdin)["flags"][stack.FAULT_FLAG]["defaultVariant"] == "off"

    assert len(reads(cluster)) == 2, "the document is read back and the change asserted"
    assert capsys.readouterr().out.strip() == stack.scenario_summary("backend-failure")


def test_a_scenario_that_does_not_land_is_reported(cluster, monkeypatch):
    monkeypatch.setattr(flags, "write_document", lambda document: None)

    with pytest.raises(SystemExit, match="did not take"):
        flags.scenario_write_eks("backend-failure")


def test_a_scenario_already_in_force_writes_nothing(cluster, capsys):
    """The catalog fault is off in the chart's document, so `shopping` has nothing to do."""
    flags.scenario_write_eks("shopping")

    assert not writes(cluster)
    assert "already set" in capsys.readouterr().out
    assert len(reads(cluster)) == 1


# --- the command surface ---------------------------------------------------------------------


def test_the_flag_command_needs_a_cluster_before_it_reads_anything(cluster, monkeypatch):
    """`tofu` too: the kubeconfig is built from the OpenTofu outputs."""
    required = []
    order = []
    monkeypatch.setattr(core, "need", lambda *commands: required.extend(commands))
    monkeypatch.setattr(aws, "aws_login", lambda: order.append("login"))
    monkeypatch.setattr(k8s, "kubeconfig", lambda: order.append("kubeconfig"))

    flags.flag()
    flags.scenario_write_eks("shopping")

    assert set(required) == {"aws", "tofu", "kubectl"}
    assert order == ["login", "kubeconfig", "login", "kubeconfig"]


def test_both_entry_points_route_here_without_reaching_a_process(monkeypatch, fake_sh):
    """`eks flag` and `scenario --target eks` reach these bodies, and a route test reaches no more.

    The route rather than the body: both bodies talk to AWS and the cluster for real, so what is
    asserted is the call each command line arrives at. Stubbing both of them is what keeps this
    test offline -- once `scenario_write_eks` stopped being a stub, calling through to it would
    run `aws configure list-profiles` and then `kubectl exec` against whatever cluster the
    caller's kubeconfig happened to name. The closing `fake_sh` assertion is defence in depth
    behind that, for the day one of these routes grows a step of its own before the call.
    """
    routed = []
    monkeypatch.setattr(flags, "flag", lambda *args, **kwargs: routed.append(("flag", args, kwargs)))
    monkeypatch.setattr(
        flags, "scenario_write_eks", lambda name: routed.append(("scenario_write_eks", name))
    )
    parser = cli.build_parser()

    for line in ("eks flag paymentUnreachable on", "scenario backend-failure --target eks"):
        args = parser.parse_args(line.split())
        args.handler(args)

    assert routed == [
        ("flag", ("paymentUnreachable", "on"), {"reset": False}),
        ("scenario_write_eks", "backend-failure"),
    ]
    assert fake_sh.lines() == [], "a route test reaches no aws or kubectl call"


def test_every_flag_argument_is_optional():
    """`eks flag` with nothing after it lists; the name and the variant are positional extras."""
    assert vars(flag_parser().parse_args([])) | {"handler": None} == {
        "name": None,
        "variant": None,
        "reset": False,
        "handler": None,
    }
    set_one = flag_parser().parse_args(["paymentUnreachable", "on"])
    assert (set_one.name, set_one.variant, set_one.reset) == ("paymentUnreachable", "on", False)
    assert flag_parser().parse_args(["--reset"]).reset is True


def test_the_help_says_the_catalog_fault_is_scenario_controlled():
    """The flag a presenter reaches for first is the one this command will not set."""
    # Normalised, because argparse wraps the description and the epilog to the terminal width.
    help_text = " ".join(flag_parser().format_help().split())
    note = " ".join(flags.SCENARIO_NOTE.split())

    assert stack.FAULT_FLAG in note and "demo.py scenario" in note
    assert help_text.count(note) == 2, "in the description and again in the epilog"
