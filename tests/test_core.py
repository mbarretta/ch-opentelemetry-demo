"""launcher.core: the reporting helpers and the three subprocess chokepoints."""

import sys

import pytest

from launcher import core


def test_log_reports_progress_on_stdout(capsys):
    core.log("staging the frontend")
    assert capsys.readouterr().out == "==> staging the frontend\n"


def test_die_reports_on_stderr_and_exits_non_zero():
    # Through a real interpreter, because what is being checked is what the process does with
    # the SystemExit die() raises, not the exception itself.
    program = (
        f"import sys; sys.path.insert(0, {str(core.ROOT)!r}); "
        "from launcher import core; core.die('no cluster')"
    )
    failed = core.run(
        sys.executable,
        "-c",
        program,
        check=False,
        stdout=core.PIPE,
        stderr=core.PIPE,
        text=True,
    )
    assert failed.returncode != 0
    assert "no cluster" in failed.stderr
    assert "no cluster" not in failed.stdout


def test_every_launcher_refusal_goes_through_die_and_int_exits_are_left_alone():
    """The refusal convention core.die's docstring states, enforced over launcher/.

    A second spelling of failure is the thing being prevented, and it comes back one
    `raise SystemExit("...")` at a time, so the rule is a test rather than a review habit.
    `raise SystemExit(<int>)` is deliberately still allowed: that form sets an exit CODE and
    prints nothing, which is how `eks tunnel status` answers the shell.
    """
    import ast

    # Keyed by file rather than by line, so that editing any of these modules for an
    # unrelated reason cannot fail this test with an accusation about refusal style.
    messages, codes = [], []
    for source in sorted((core.ROOT / "launcher").rglob("*.py")):
        tree = ast.parse(source.read_text())
        for node in ast.walk(tree):
            raised = node.exc if isinstance(node, ast.Raise) else None
            if not isinstance(raised, ast.Call) or getattr(raised.func, "id", None) != "SystemExit":
                continue
            where = str(source.relative_to(core.ROOT))
            texts = [
                child
                for arg in raised.args
                for child in ast.walk(arg)
                if isinstance(child, ast.JoinedStr)
                or (isinstance(child, ast.Constant) and isinstance(child.value, str))
            ]
            (messages if texts else codes).append(where)

    assert messages == ["launcher/core.py"], (
        "an operator-facing refusal outside core.die: use core.die(...) instead"
    )
    assert sorted(codes) == ["launcher/eks/tunnel.py"] * 2, (
        "the tunnel's integer exits are exit codes, not messages, and must stay as they are"
    )


def test_need_names_every_missing_command_in_one_message():
    core.need("git", "python3")
    with pytest.raises(SystemExit) as failure:
        core.need("tofu-not-installed", "git", "helm-not-installed")
    message = str(failure.value)
    assert "tofu-not-installed" in message and "helm-not-installed" in message
    assert "git" not in message, "a command that is present must not be reported missing"


def test_dotenv_reads_pairs_and_an_absent_file_is_empty(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SHOP_PORT=8080\n# comment\nCLICKSTACK_API_KEY=abc\n")
    assert dict(core.dotenv(env)) == {"SHOP_PORT": "8080", "CLICKSTACK_API_KEY": "abc"}
    assert dict(core.dotenv(tmp_path / "absent")) == {}


def test_run_stringifies_arguments_and_raises_only_when_checked(tmp_path):
    core.run("touch", tmp_path / "made-by-run")
    assert (tmp_path / "made-by-run").exists(), "Path arguments are stringified"
    assert core.run("false", check=False).returncode != 0
    with pytest.raises(core.subprocess.CalledProcessError):
        core.run("false")


def test_capture_returns_stdout_as_text_or_bytes():
    assert core.capture("echo", "hello").stdout == "hello\n"
    assert core.capture("echo", "hello", text=False).stdout == b"hello\n"
    failed = core.capture("ls", "no-such-path", check=False, stderr=core.PIPE)
    assert failed.returncode != 0 and failed.stderr


def test_popen_hands_back_a_pipe_to_stream_from():
    with core.popen(sys.executable, "-c", "print('chunk')") as process:
        assert process.stdout.readline() == b"chunk\n"
    assert process.returncode == 0


def test_fake_sh_records_every_call_and_answers_from_the_prefix_table(fake_sh):
    """The fixture the offline tests are written against: all three chokepoints, recorded."""
    fake_sh.reply("kubectl", stdout="applied\n")
    fake_sh.reply("kubectl -n otel-demo get nodes", stdout="node-1 Ready\n")
    fake_sh.reply("helm upgrade", stderr="no release\n", returncode=1)

    core.run("kubectl", "apply", "-f", "-", input='{"kind": "Secret"}')
    assert core.capture("kubectl", "-n", "otel-demo", "get", "nodes").stdout == "node-1 Ready\n"
    assert core.capture("kubectl", "get", "ns").stdout == "applied\n", "longest prefix wins"
    with core.popen("git", "archive", "HEAD") as archive:
        assert archive.stdout.read() == b"", "an unanswered call succeeds with no output"

    assert fake_sh.lines() == [
        "kubectl apply -f -",
        "kubectl -n otel-demo get nodes",
        "kubectl get ns",
        "git archive HEAD",
    ]
    assert fake_sh.calls[0].stdin == '{"kind": "Secret"}', "what goes in on stdin is recorded too"
    assert fake_sh.calls[1].stdin is None

    with pytest.raises(core.subprocess.CalledProcessError):
        core.run("helm", "upgrade", "otel-demo")
    failed = core.run("helm", "upgrade", "otel-demo", check=False)
    assert (failed.returncode, failed.stderr) == (1, "no release\n")
