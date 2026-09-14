"""launcher.cli: the command surface. What parses, what is refused, and where it dispatches."""

import argparse

import pytest

from launcher import cli, core, images, stack
from launcher.eks import tunnel

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


def test_tunnel_routes_each_action_to_its_own_function(monkeypatch):
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


def test_every_eks_subcommand_dispatches_to_a_stub():
    """The stubs are this task's deliverable: a reachable command, not an AttributeError."""
    parser = cli.build_parser()
    assert set(EKS_INVOCATIONS) == set(subcommands(subcommands(parser)["eks"]))

    for name, line in EKS_INVOCATIONS.items():
        args = parser.parse_args(line.split())
        with pytest.raises(SystemExit) as failure:
            args.handler(args)
        assert str(failure.value) == "not implemented yet", name


def test_publish_and_eks_do_not_need_the_upstream_checkout(tmp_path, monkeypatch):
    """Both run on a laptop that has never staged the frontend; `up` and `stage` do not."""
    monkeypatch.setattr(core, "RUNTIME", tmp_path)
    assert not (tmp_path / "tools.py").exists()
    # bootstrap is the third command past the gate; running it here would clone upstream, so
    # only the default it sets can be asserted.
    assert cli.build_parser().parse_args(["bootstrap"]).needs_bootstrap is False

    for line in ("publish", "eks status", "eks check"):
        with pytest.raises(SystemExit) as failure:
            cli.main(line.split())
        assert str(failure.value) == "not implemented yet", line

    for line in ("up", "stage"):
        with pytest.raises(SystemExit, match="bootstrap first"):
            cli.main(line.split())


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


def test_scenario_targets_the_laptop_by_default_and_the_cluster_on_request(monkeypatch):
    applied = []
    monkeypatch.setattr(stack, "scenario", applied.append)

    cli.run_scenario(cli.build_parser().parse_args(["scenario", "backend-failure"]))
    assert applied == ["backend-failure"]

    with pytest.raises(SystemExit, match="not implemented yet"):
        cli.run_scenario(cli.build_parser().parse_args(["scenario", "shopping", "--target", "eks"]))


def test_an_unknown_scenario_is_refused(capsys):
    with pytest.raises(SystemExit) as failure:
        cli.build_parser().parse_args(["scenario", "no-such-scenario"])
    assert failure.value.code == 2
    assert "no-such-scenario" in capsys.readouterr().err
