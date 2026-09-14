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
