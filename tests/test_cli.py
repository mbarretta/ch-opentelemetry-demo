"""launcher.cli: the command surface. What parses, what is refused, and where it dispatches."""

import argparse

import pytest

from launcher import cli, core, images, stack
from launcher.eks import aws, check, ops, tunnel
from launcher.eks import flags as eks_flags

# Every command line the CLI accepts. The EKS half is declared by the modules that implement it
# (`launcher.eks.register`), so this list is also the check that each register() ran.
COMMAND_LINES = [
    "bootstrap",
    "stage",
    "build",
    "build --service agent --service mcp",
    "build --platform linux/arm64",
    "publish",
    "publish --force",
    "publish --service frontend",
    "config",
    "config --dev",
    "up",
    "up --dev",
    "up --debug-chatbot",
    "down",
    "ps",
    "logs agent frontend",
    "restart agent",
    "scenario",
    "scenario backend-failure",
    "scenario backend-failure --target eks",
    "seed-prompts",
    "eks init",
    "eks apply",
    "eks apply --yes",
    "eks deploy",
    "eks up",
    "eks down",
    "eks down --keep",
    "eks tunnel",
    "eks tunnel stop",
    "eks tunnel status",
    "eks tunnel --loop --port 9090 --namespace otel-demo",
    "eks flag",
    "eks flag paymentUnreachable",
    "eks flag paymentUnreachable on",
    "eks flag --reset",
    "eks verify",
    "eks status",
    "eks nightly on",
    "eks nightly off",
    "eks destroy",
    "eks destroy --purge-state",
    "eks check",
]

# One invocation per `eks` subcommand, with the arguments it requires. Compared against the
# parser below, so a new subcommand cannot be added without giving it a line here.
EKS_INVOCATIONS = {
    "init": "eks init",
    "apply": "eks apply",
    "destroy": "eks destroy",
    "nightly": "eks nightly on",
    "deploy": "eks deploy",
    "up": "eks up",
    "down": "eks down",
    "tunnel": "eks tunnel",
    "flag": "eks flag",
    "verify": "eks verify",
    "status": "eks status",
    "check": "eks check",
}


def subcommands(parser):
    """The subparsers a parser declares, keyed by name."""
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices
    raise AssertionError("parser declares no subcommands")


@pytest.fixture
def bootstrapped(tmp_path, monkeypatch):
    """A .runtime that looks bootstrapped, so the gate in main() lets a command through."""
    monkeypatch.setattr(core, "RUNTIME", tmp_path)
    (tmp_path / "tools.py").write_text("# corrected tools\n")
    monkeypatch.setattr(stack, "generate_config", lambda env: None)
    monkeypatch.setattr(stack, "environment", lambda: {"SHOP_PORT": "8080"})
    return tmp_path


@pytest.mark.parametrize("line", COMMAND_LINES)
def test_every_command_line_parses_to_a_callable(line):
    args = cli.build_parser().parse_args(line.split())
    assert callable(args.handler), line


def test_flags_belong_to_the_subcommand_that_uses_them():
    parse = cli.build_parser().parse_args
    assert parse("build --service agent --service mcp".split()).services == ["agent", "mcp"]
    assert parse(["build"]).services is None, "no --service means all four"
    assert parse("build --platform linux/amd64".split()).platform == "linux/amd64"
    assert parse(["publish", "--force"]).force is True
    assert parse("logs agent frontend".split()).arguments == ["agent", "frontend"]
    assert parse(["up", "--dev", "--debug-chatbot"]).dev is True
    assert parse(["eks", "down", "--keep"]).keep is True
    assert parse(["eks", "destroy", "--purge-state"]).purge_state is True
    assert parse(["eks", "nightly", "off"]).state == "off"
    assert parse(["eks", "flag", "paymentUnreachable", "on"]).variant == "on"
    assert parse(["eks", "flag", "--reset"]).reset is True
    assert parse(["eks", "tunnel", "stop"]).action == "stop"


def test_the_stack_flags_are_per_subcommand_not_global(capsys):
    """The README writes `demo.py up --dev`; the old flat parser also took `--dev up`."""
    assert cli.build_parser().parse_args(["up", "--dev"]).dev is True

    with pytest.raises(SystemExit) as failure:
        cli.build_parser().parse_args(["--dev", "up"])

    assert failure.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_a_command_is_required(capsys):
    with pytest.raises(SystemExit) as failure:
        cli.build_parser().parse_args([])
    assert failure.value.code == 2
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["eks"])
    assert "usage:" in capsys.readouterr().err


def test_the_tunnel_loop_is_hidden_but_reachable():
    """The loop is the detached child's own entry point, not something to run by hand."""
    tunnel_parser = subcommands(subcommands(cli.build_parser())["eks"])["tunnel"]
    help_text = tunnel_parser.format_help()
    for flag in ("--loop", "--port", "--namespace"):
        assert flag not in help_text, f"{flag} stays out of the help"

    parsed = tunnel_parser.parse_args(["--loop", "--port", "9090", "--namespace", "otel-demo"])

    assert (parsed.loop, parsed.port, parsed.namespace) == (True, 9090, "otel-demo")


def test_tunnel_routes_each_action_to_its_own_function(monkeypatch, fake_sh):
    """Each action word reaches its own tunnel function, and the routing reaches nothing else.

    The four functions are stubbed because `tunnel` is implemented: `loop` alone is a
    `while True` around `kubectl port-forward`, so a routing test that called it would not
    return. `fake_sh` is the net behind the stubs, for the reason the dispatch test below
    gives, and it holds the day one of these routes grows a step of its own before the call.
    """
    routed = []
    for name in ("stop", "status", "restart", "loop"):
        monkeypatch.setattr(tunnel, name, lambda *args, name=name, **kwargs: routed.append(name))
    parser = cli.build_parser()

    for line in ("eks tunnel", "eks tunnel stop", "eks tunnel --loop"):
        args = parser.parse_args(line.split())
        args.handler(args)

    status = parser.parse_args(["eks", "tunnel", "status"])
    with pytest.raises(SystemExit) as failure:
        status.handler(status)

    assert routed == ["restart", "stop", "loop", "status"]
    assert failure.value.code == 1, "a tunnel that is down is what a script tests for"
    assert fake_sh.lines() == [], "a routing test reaches no kubectl call"


# Subcommands whose bodies have landed, each with the test file that now owns its behaviour.
# Their handlers are deliberately not called below: an implemented handler talks to AWS and the
# cluster for real, which is not something a parser test may do.
IMPLEMENTED = {
    "init": "tests/test_eks_infra.py",
    "apply": "tests/test_eks_infra.py",
    "destroy": "tests/test_eks_infra.py",
    "nightly": "tests/test_eks_infra.py",
    "deploy": "tests/test_eks_deploy.py",
    "up": "tests/test_eks_deploy.py",
    "down": "tests/test_eks_deploy.py",
    "tunnel": "tests/test_eks_tunnel.py",
    "flag": "tests/test_eks_flags.py",
    "verify": "tests/test_eks_ops.py",
    "status": "tests/test_eks_ops.py",
    "check": "tests/test_eks_check.py",
}


def dispatch_every_eks_subcommand(parser, fake_sh, implemented=IMPLEMENTED):
    """Call the handler of every `eks` subcommand outside `implemented`, checking each call.

    Three things are asserted about a handler, and all three are asserted immediately after
    the call that they are about rather than once at the end of the caller's body, so each
    failure names the subcommand that caused it and no failure can mask another:

    1. It reached no process. Checked first, and against only the calls this one invocation
       added -- parsing the line included -- because an unimplemented handler that talks to
       AWS is the thing this guard exists to catch: checked after (3), a landed body's own
       refusal message would fail first and the process would go unreported. The closing
       assertion keeps the whole-body guarantee this replaced, for the calls no single
       invocation owns.
    2. It refused rather than returned.
    3. It refused with the stub's own message.

    Limbs (1) and (3) are what make the guard hold whatever the environment has in it, which
    is the other half of the point. A body that lands without its `IMPLEMENTED` entry trips
    (1) when `AWS_PROFILE` is set -- `aws.aws_login()` reaches `aws configure list-profiles`
    before anything else -- and trips (3) when the profile is unset, because then it dies at
    that precondition having started no process at all. Bare CI is the unset case, so a guard
    resting on (1) alone is a guard that passes there for a reason that has nothing to do with
    the handlers.

    Provoked into failing by the two tests below, which is the only way to know a guard that
    is green on every run is green because it held.
    """
    for name, line in EKS_INVOCATIONS.items():
        started = len(fake_sh.calls)
        args = parser.parse_args(line.split())
        assert callable(args.handler), name
        if name in implemented:
            continue

        raised = None
        try:
            args.handler(args)
        except (Exception, SystemExit) as error:
            raised = error
        spawned = fake_sh.lines()[started:]

        assert spawned == [], (
            f"`demo.py {line}` has no entry in IMPLEMENTED but reached {spawned}: a body has "
            f"landed without the test file that owns it"
        )
        assert isinstance(raised, SystemExit), (
            f"`demo.py {line}` did not refuse as unimplemented; it raised {raised!r}"
        )
        assert str(raised) == "not implemented yet", f"`demo.py {line}` refused with: {raised}"

    assert fake_sh.lines() == [], (
        "a dispatch test reaches no aws, kubectl, helm or tofu call. The per-invocation checks "
        f"above did not account for these: {fake_sh.lines()}"
    )


def test_every_eks_subcommand_dispatches_to_its_module(fake_sh):
    """A reachable command in every case: a stub that says so, or a body with its own tests.

    `fake_sh` is the safety net rather than a convenience. It stands in front of the three
    `core` chokepoints every launcher module reaches the outside world through, so a body that
    lands without its `IMPLEMENTED` entry is recorded here instead of running live `aws`,
    `kubectl`, `helm` or `tofu` from a parser test. `dispatch_every_eks_subcommand` is where
    that net is checked, once per invocation.
    """
    parser = cli.build_parser()
    assert set(EKS_INVOCATIONS) == set(subcommands(subcommands(parser)["eks"]))
    assert fake_sh.lines() == [], "building the parser reaches no process"

    for name, owner in IMPLEMENTED.items():
        assert (core.ROOT / owner).exists(), f"{name} names a test file that does not exist"

    dispatch_every_eks_subcommand(parser, fake_sh)


def spawn_a_process(*_arguments, **_keywords):
    """A handler body that reaches the outside world, which is what the guard is there to catch."""
    core.run("aws", "sts", "get-caller-identity")


def use_profile(monkeypatch, value):
    """Decide what `AWS_PROFILE` and `AWS_REGION` hold, whatever the caller's shell says.

    Both keys are handed to monkeypatch even when they are being cleared, so the original
    values are restored afterwards: `aws.aws_login()` writes both into `os.environ` directly,
    and a leaked AWS_REGION would decide what a later test sees.
    """
    for key in ("AWS_PROFILE", "AWS_REGION"):
        monkeypatch.delenv(key, raising=False)
    if value:
        monkeypatch.setenv("AWS_PROFILE", value)


@pytest.mark.parametrize("configured", (False, True), ids=("profile-unset", "profile-set"))
def test_the_dispatch_guard_names_the_subcommand_that_reaches_a_process(
    monkeypatch, fake_sh, configured
):
    """The guard above, provoked: substitute one body and it has to fail, and say which one.

    `eks status` is given a body that calls `core.run()`, and taken out of `IMPLEMENTED` so
    that the guard reaches it -- which is exactly the shape of the mistake being guarded
    against, a subcommand implemented without its test file. Run with the profile unset and
    set, because bare CI has no AWS_PROFILE and a laptop does, and a guard that only bites on
    one of the two is not a guard the CI run is entitled to.
    """
    use_profile(monkeypatch, "demo" if configured else "")
    monkeypatch.setattr(ops, "status", spawn_a_process)

    with pytest.raises(AssertionError) as caught:
        dispatch_every_eks_subcommand(
            cli.build_parser(), fake_sh, implemented=set(IMPLEMENTED) - {"status"}
        )

    assert "demo.py eks status" in str(caught.value), "the guard names the offending subcommand"
    assert "aws sts get-caller-identity" in str(caught.value), "and the call it let through"


@pytest.mark.parametrize("configured", (False, True), ids=("profile-unset", "profile-set"))
def test_the_dispatch_guard_catches_a_landed_body_whatever_the_profile_holds(
    tmp_path, monkeypatch, fake_sh, configured
):
    """The same provocation with a realistic body, which is where the old guard leaked.

    `aws.aws_login()` is the first line of every implemented `eks` body, so it stands in for
    one here. With the profile set it reaches `aws configure list-profiles` and the no-process
    limb catches it; with the profile unset it dies at its own precondition before starting
    anything, and only the refusal-message limb can. The guard this replaced was a single
    end-of-body `fake_sh.lines() == []`, so on bare CI -- profile unset -- a landed body
    started no process and the assertion held: green, and blind.

    `core.ROOT` is redirected at an empty directory so the repository's own `.env`, if the
    developer has one, cannot supply the profile that the unset case is about.
    """
    monkeypatch.setattr(core, "ROOT", tmp_path)
    use_profile(monkeypatch, "demo" if configured else "")
    monkeypatch.setattr(ops, "status", aws.aws_login)

    with pytest.raises(AssertionError) as caught:
        dispatch_every_eks_subcommand(
            cli.build_parser(), fake_sh, implemented=set(IMPLEMENTED) - {"status"}
        )

    reported = str(caught.value)
    assert "demo.py eks status" in reported, "the guard names the offending subcommand"
    if configured:
        assert "aws configure list-profiles" in reported, "caught by the no-process limb"
    else:
        assert "AWS_PROFILE is unset" in reported, "caught by the refusal-message limb"


def test_publish_and_eks_do_not_need_the_upstream_checkout(tmp_path, monkeypatch, fake_sh):
    """Both run on a laptop that has never staged the frontend; `up` and `stage` do not."""
    monkeypatch.setattr(core, "RUNTIME", tmp_path)
    assert not (tmp_path / "tools.py").exists()
    # bootstrap is the third command past the gate; running it here would clone upstream, so
    # only the default it sets can be asserted.
    assert cli.build_parser().parse_args(["bootstrap"]).needs_bootstrap is False

    # The gate is the subject, so what is asserted is that each line arrives at its body --
    # all three have landed, and `publish` is a top-level command rather than an `eks`
    # subcommand, so `IMPLEMENTED` does not speak for it. The bodies are stubbed because they
    # push to ECR and read the cluster for real; `fake_sh` is the net behind the stubs, for the
    # same reason the dispatch test above gives.
    reached = []
    monkeypatch.setattr(
        images, "publish", lambda services, force=False: reached.append("publish")
    )
    monkeypatch.setattr(ops, "status", lambda: reached.append("eks status"))
    monkeypatch.setattr(check, "check", lambda: reached.append("eks check"))

    for line in ("publish", "eks status", "eks check"):
        cli.main(line.split())

    assert reached == ["publish", "eks status", "eks check"], "the gate let all three through"

    for line in ("up", "stage"):
        with pytest.raises(SystemExit, match="bootstrap first"):
            cli.main(line.split())

    assert fake_sh.lines() == [], "a routed body and a refusal both reach no process"


def test_logs_reaches_compose_with_the_debug_profile(bootstrapped, fake_sh):
    cli.main(["logs", "agent", "frontend"])

    argv = fake_sh.last.argv
    assert argv[:2] == ["docker", "compose"]
    assert argv[argv.index("--profile") + 1] == "debug"
    assert argv[-5:] == ["logs", "--tail", "80", "agent", "frontend"]


def test_up_passes_its_flags_through_to_compose(bootstrapped, fake_sh, monkeypatch, capsys):
    monkeypatch.setattr(images, "require_images", lambda: None)

    cli.main(["up", "--dev", "--debug-chatbot"])

    line = fake_sh.last.line
    assert "compose.dev.yaml" in line and "--profile debug" in line
    assert line.endswith("up -d --no-build")
    out = capsys.readouterr().out
    assert "http://localhost:8080" in out and "Chat (debug)" in out


def test_scenario_targets_the_laptop_by_default_and_the_cluster_on_request(monkeypatch, fake_sh):
    """`--target` decides which flagd file a scenario is written to, and nothing else does.

    Both transforms are stubbed, the cluster one because it is implemented now: called here it
    runs `aws configure list-profiles`, checks the SSO session, reads the OpenTofu outputs,
    rewrites the caller's kubeconfig and then `kubectl exec`s into whichever cluster that
    named -- from a parser test. The bodies have their own tests (tests/test_eks_flags.py);
    what belongs here is the route, with `fake_sh` as the net behind both stubs.
    """
    applied = []
    monkeypatch.setattr(stack, "scenario", lambda name: applied.append(("local", name)))
    monkeypatch.setattr(
        eks_flags, "scenario_write_eks", lambda name: applied.append(("eks", name))
    )
    parse = cli.build_parser().parse_args

    cli.run_scenario(parse(["scenario", "backend-failure"]))
    cli.run_scenario(parse(["scenario", "shopping", "--target", "local"]))
    cli.run_scenario(parse(["scenario", "shopping", "--target", "eks"]))

    assert applied == [("local", "backend-failure"), ("local", "shopping"), ("eks", "shopping")]
    assert fake_sh.lines() == [], "a target test reaches no aws, kubectl or tofu call"


def test_an_unknown_scenario_is_refused(capsys):
    with pytest.raises(SystemExit) as failure:
        cli.build_parser().parse_args(["scenario", "no-such-scenario"])
    assert failure.value.code == 2
    assert "no-such-scenario" in capsys.readouterr().err
