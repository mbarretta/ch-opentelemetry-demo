"""launcher.core: the reporting helpers and the three subprocess chokepoints."""

import ast
import sys

import pytest

from launcher import core

# The trees the two conventions below are enforced over. launcher/ is the CLI and scripts/ is
# the other operator-facing entry point, so a refusal can be written in either. The process
# surface is a claim about the whole repository, so its scan covers tests/ and concierge/ too.
REFUSAL_TREES = ("launcher", "scripts")
IMPORT_TREES = ("launcher", "scripts", "tests", "concierge")


def python_sources(base, *trees):
    """Every .py file under `trees`, sorted, paired with its path relative to `base`.

    A tree that is not there is refused rather than skipped: `rglob` over a missing directory
    yields nothing, which is how a scan whose tree was renamed stays green while covering
    none of it -- the exact way the guards below would go silent.

    Raised rather than asserted, because `python -O` strips `assert` and this is the one line
    standing between a renamed tree and a scan that silently covers nothing. The suite is not
    run under `-O` today, but `tests/test_smoke_queries.py` exists because this repo does run
    `-O` children, so the cost of not relying on that is one keyword.
    """
    for tree in trees:
        root = base / tree
        if not root.is_dir():
            raise AssertionError(
                f"{tree}/ is not a directory under {base}: the scan would be empty"
            )
        for source in sorted(root.rglob("*.py")):
            yield source, str(source.relative_to(base))


def refusal_sites(base, *trees):
    """The files under `trees` that raise SystemExit, split into messages and bare exit codes.

    Paths come back relative to `base`, sorted and keyed by file. `base` is a parameter rather
    than core.ROOT so that the same walk the real trees are scanned with can be pointed at a
    synthetic tree: a guard nothing has ever run against a failing input is not a guard.
    """
    messages, codes = [], []
    for source, where in python_sources(base, *trees):
        for node in ast.walk(ast.parse(source.read_text())):
            raised = node.exc if isinstance(node, ast.Raise) else None
            if not isinstance(raised, ast.Call):
                continue
            if getattr(raised.func, "id", None) != "SystemExit":
                continue
            texts = [
                child
                for arg in raised.args
                for child in ast.walk(arg)
                if isinstance(child, ast.JoinedStr)
                or (isinstance(child, ast.Constant) and isinstance(child.value, str))
            ]
            (messages if texts else codes).append(where)
    return sorted(messages), sorted(codes)


def subprocess_importers(base, *trees):
    """The files under `trees` that import `subprocess`, relative to `base` and sorted.

    Both spellings count -- `import subprocess`, aliased or not, and `from subprocess import
    ...` -- because either one puts a second process surface in the repository.
    """
    importers = []
    for source, where in python_sources(base, *trees):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                found = any(alias.name.split(".")[0] == "subprocess" for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                found = (node.module or "").split(".")[0] == "subprocess"
            else:
                continue
            if found:
                importers.append(where)
    return sorted(set(importers))


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


def test_every_refusal_goes_through_die_and_int_exits_are_left_alone():
    """The refusal convention core.die's docstring states, enforced over launcher/ and scripts/.

    A second spelling of failure is the thing being prevented, and it comes back one
    `raise SystemExit("...")` at a time, so the rule is a test rather than a review habit.
    scripts/ is scanned alongside launcher/ because it holds the other operator-facing entry
    points -- smoke.py already refuses through core.die -- so it is where the next stray
    refusal is most likely to be written.
    `raise SystemExit(<int>)` is deliberately still allowed: that form sets an exit CODE and
    prints nothing, which is how `eks tunnel status` answers the shell.
    """
    # Keyed by file rather than by line, so that editing any of these modules for an
    # unrelated reason cannot fail this test with an accusation about refusal style.
    messages, codes = refusal_sites(core.ROOT, *REFUSAL_TREES)

    assert messages == ["launcher/core.py"], (
        "an operator-facing refusal outside core.die: use core.die(...) instead"
    )
    assert sorted(codes) == ["launcher/eks/tunnel.py"] * 2, (
        "the tunnel's integer exits are exit codes, not messages, and must stay as they are"
    )


def test_the_refusal_scan_reaches_a_stray_systemexit_under_scripts(tmp_path):
    """The negative half of the guard above: prove the widened scan bites on scripts/.

    The real tree holds no stray refusal, so a scan that silently skipped scripts/ would pass
    the test above exactly as a working one does. This one feeds the same walk a synthetic
    scripts/-shaped tree that does hold one.
    """
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "stray.py").write_text('raise SystemExit("no cluster: run `eks deploy` first")\n')
    (scripts / "answers_the_shell.py").write_text("raise SystemExit(2)\n")
    (scripts / "well_behaved.py").write_text("from launcher import core\n\ncore.die('gone')\n")

    messages, codes = refusal_sites(tmp_path, "scripts")

    assert messages == ["scripts/stray.py"], "a message refusal under scripts/ must be reported"
    assert codes == ["scripts/answers_the_shell.py"], "an integer exit is a code, not a refusal"


def test_both_scans_refuse_a_tree_that_is_not_there(tmp_path):
    """A renamed or misspelled tree must fail loudly rather than quietly scan nothing."""
    (tmp_path / "launcher").mkdir()

    with pytest.raises(AssertionError, match="scripts/"):
        refusal_sites(tmp_path, "launcher", "scripts")
    with pytest.raises(AssertionError, match="scripts/"):
        subprocess_importers(tmp_path, "launcher", "scripts")


def test_subprocess_is_imported_in_core_alone_so_the_process_surface_stays_one_place():
    """The claim in core.py's own module docstring, enforced rather than asserted in prose.

    Every command the CLI runs goes through core.run(), core.capture() or core.popen(), which
    is what lets the tests fake the whole process surface in one fixture. A second module
    importing subprocess spawns a child nothing can record, and the last one arrived from a
    test rather than from the launcher, so tests/ and concierge/ are scanned too.
    """
    importers = subprocess_importers(core.ROOT, *IMPORT_TREES)
    strays = ", ".join(where for where in importers if where != "launcher/core.py")

    assert importers == ["launcher/core.py"], (
        "subprocess is imported outside launcher/core.py -- go through core.run(), "
        f"core.capture() or core.popen() instead: {strays or 'core.py stopped importing it'}"
    )


def test_the_subprocess_scan_reports_both_import_spellings(tmp_path):
    """The negative half of the guard above, over a synthetic tree holding both spellings."""
    tree = tmp_path / "launcher"
    (tree / "eks").mkdir(parents=True)
    (tree / "plain.py").write_text("import subprocess\n")
    (tree / "eks" / "aliased.py").write_text("import subprocess as sp\n")
    (tree / "eks" / "from_import.py").write_text("from subprocess import run\n")
    (tree / "innocent.py").write_text("from launcher import core\n\ncore.log('no child here')\n")

    assert subprocess_importers(tmp_path, "launcher") == [
        "launcher/eks/aliased.py",
        "launcher/eks/from_import.py",
        "launcher/plain.py",
    ], "both import spellings are reported, and an innocent module is not"


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
