"""launcher.eks.tunnel: the pidfile lifecycle, the detached loop, and the readiness probe."""

import argparse
import contextlib
import http.server
import os
import signal
import socket
import sys
import threading

import pytest

from launcher import core
from launcher.eks import aws, config, k8s, tunnel

# The one value these tests read out of `.env`, as the shared `redirected` fixture writes it.
ENV = {"EKS_TUNNEL_PORT": "9090"}


@pytest.fixture
def redirect_env():
    """The `.env` the shared `redirected` fixture writes for these tests."""
    return ENV


@pytest.fixture
def redirected(redirected):
    """The shared checkout with its run directory made, so no real pidfile or log is touched."""
    config.run_dir().mkdir(parents=True)
    return redirected


class Spawned:
    """`core.popen` recorded instead of run, reporting a pid the test chooses."""

    def __init__(self, pid):
        self.pid = pid
        self.calls = []

    def popen(self, *args, **kwargs):
        self.calls.append(([str(argument) for argument in args], kwargs))
        return self

    @property
    def argv(self):
        return self.calls[-1][0]

    @property
    def kwargs(self):
        return self.calls[-1][1]


@pytest.fixture
def spawned(monkeypatch):
    """The detached child, recorded. Its pid is this process's, which is reliably alive."""
    handle = Spawned(os.getpid())
    monkeypatch.setattr(core, "popen", handle.popen)
    return handle


@pytest.fixture
def tools(monkeypatch):
    """`core.need` recorded: these tests must not require aws and kubectl on the machine."""
    required = []
    monkeypatch.setattr(core, "need", lambda *commands: required.extend(commands))
    return required


@pytest.fixture
def slept(monkeypatch):
    """Every pause the module would have taken, recorded instead of taken."""
    taken = []
    monkeypatch.setattr(tunnel.time, "sleep", taken.append)
    return taken


def dead_pid():
    """A pid that names no process, for the stale-pidfile cases."""
    for candidate in range(99999, 300, -1):
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:
            continue
    raise AssertionError("every pid in the search range is taken")


@contextlib.contextmanager
def answering(status):
    """A local HTTP server that answers every request with `status`, on a port of its own."""

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(status)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_the_pidfile_and_the_log_live_under_runtime_eks(redirected):
    assert tunnel.pidfile() == redirected / ".runtime/eks/tunnel.pid"
    assert tunnel.logfile() == redirected / ".runtime/eks/tunnel.log"


def test_the_port_comes_from_the_flag_then_the_env_then_the_default(redirected):
    assert tunnel.resolved_port(9191) == 9191
    assert tunnel.resolved_port() == 9090, "EKS_TUNNEL_PORT in .env"
    (redirected / ".env").write_text("")
    assert tunnel.resolved_port() == int(config.DEFAULT_TUNNEL_PORT)


def test_start_refuses_when_the_pidfile_names_a_live_process(redirected, spawned, tools):
    tunnel.pidfile().write_text(f"{os.getpid()}\n")

    with pytest.raises(SystemExit) as failure:
        tunnel.start(port=9090)

    assert f"pid {os.getpid()}" in str(failure.value)
    assert "tunnel stop" in str(failure.value), "say how to take the running one down"
    assert spawned.calls == [], "nothing may be launched over a running tunnel"
    assert tunnel.pidfile().exists(), "the running tunnel's pidfile is not ours to remove"


def test_start_refuses_when_the_port_is_already_bound(redirected, spawned, tools):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]

        with pytest.raises(SystemExit) as failure:
            tunnel.start(port=port)

    assert f"port {port}" in str(failure.value)
    assert "EKS_TUNNEL_PORT" in str(failure.value), "name the way out of the collision"
    assert spawned.calls == [], "the other process owns the port; nothing is launched"


def test_a_port_nobody_listens_on_is_not_reported_busy():
    with socket.socket() as bound:
        bound.bind(("127.0.0.1", 0))
        port = bound.getsockname()[1]
    assert tunnel.port_is_busy(port) is False


def test_start_clears_a_stale_pidfile_and_launches_the_detached_loop(
    redirected, spawned, tools, slept, monkeypatch
):
    tunnel.pidfile().write_text(f"{dead_pid()}\n")
    monkeypatch.setattr(tunnel, "probe", lambda port, timeout=None: True)

    assert tunnel.start(port=9090, namespace="otel-demo") is True

    assert tools == ["aws", "kubectl"], "the loop runs both; say so before detaching"
    assert tunnel.pidfile().read_text().strip() == str(os.getpid())
    assert spawned.argv[0] == sys.executable, "the child is this CLI, in this interpreter"
    assert spawned.argv[2] == str(redirected / "scripts/demo.py")
    assert spawned.argv[3:] == [
        "eks",
        "tunnel",
        "--loop",
        "--port",
        "9090",
        "--namespace",
        "otel-demo",
    ]
    assert spawned.kwargs["start_new_session"] is True, "the tunnel outlives its terminal"
    assert spawned.kwargs["stdout"].name == str(tunnel.logfile())
    assert spawned.kwargs["stderr"] is spawned.kwargs["stdout"]
    assert spawned.kwargs["stdin"] is core.DEVNULL, "a detached child owns no terminal"
    assert slept == [], "a tunnel that answers at once is not waited on"


def test_the_readiness_probe_polls_twenty_times_at_one_second_intervals(
    redirected, spawned, tools, slept, monkeypatch, capsys
):
    """Twenty seconds for the port-forward to answer, then a warning rather than a failure."""
    attempts = []
    monkeypatch.setattr(tunnel, "probe", lambda port, timeout=None: attempts.append(port) or False)

    assert tunnel.start(port=9090) is False

    assert attempts == [9090] * 20
    assert slept == [1] * 20
    assert "did not answer" in capsys.readouterr().err
    assert tunnel.pidfile().read_text().strip() == str(os.getpid()), "the loop is still running"


def test_start_reports_a_loop_that_exits_immediately(redirected, tools, slept, monkeypatch):
    handle = Spawned(dead_pid())
    monkeypatch.setattr(core, "popen", handle.popen)
    monkeypatch.setattr(tunnel, "probe", lambda port, timeout=None: False)

    with pytest.raises(SystemExit) as failure:
        tunnel.start(port=9090)

    assert "exited immediately" in str(failure.value)
    assert str(tunnel.logfile()) in str(failure.value), "point at the log that holds the reason"
    assert slept == [], "a dead loop is not waited out"


@pytest.mark.parametrize("status", [200, 404, 503])
def test_any_http_answer_counts_as_up(status):
    """Envoy answers 503 while the storefront pods start: the port-forward still carried it."""
    with answering(status) as port:
        assert tunnel.probe(port) is True
    assert tunnel.probe(port) is False, "nothing listening is nothing to probe"


def test_stop_terms_the_process_group_and_kills_it_after_five_seconds(
    redirected, slept, monkeypatch
):
    pid = os.getpid()
    tunnel.pidfile().write_text(f"{pid}\n")
    monkeypatch.setattr(tunnel, "alive", lambda checked: True)
    monkeypatch.setattr(os, "getpgid", lambda checked: checked)
    signalled = []
    monkeypatch.setattr(os, "killpg", lambda group, number: signalled.append((group, number)))

    tunnel.stop()

    assert signalled == [(pid, signal.SIGTERM), (pid, signal.SIGKILL)]
    assert sum(slept) == 5, "five seconds of grace, then the group goes"
    assert not tunnel.pidfile().exists()


def test_stop_does_not_escalate_when_the_group_goes_on_term(redirected, slept, monkeypatch):
    pid = os.getpid()
    tunnel.pidfile().write_text(f"{pid}\n")
    answers = iter([True])
    monkeypatch.setattr(tunnel, "alive", lambda checked: next(answers, False))
    monkeypatch.setattr(os, "getpgid", lambda checked: checked)
    signalled = []
    monkeypatch.setattr(os, "killpg", lambda group, number: signalled.append((group, number)))

    tunnel.stop()

    assert signalled == [(pid, signal.SIGTERM)]
    assert slept == [], "a group that is already gone is not waited on"
    assert not tunnel.pidfile().exists()


def test_stop_cleans_a_stale_or_missing_pidfile_without_signalling_anything(
    redirected, monkeypatch
):
    signalled = []
    monkeypatch.setattr(os, "killpg", lambda group, number: signalled.append((group, number)))

    tunnel.stop()
    tunnel.pidfile().write_text(f"{dead_pid()}\n")
    tunnel.stop()
    tunnel.pidfile().write_text("not a pid\n")
    tunnel.stop()

    assert signalled == [], "a pidfile naming nobody is litter, not a process to signal"
    assert not tunnel.pidfile().exists()


def test_stop_refuses_to_signal_a_group_the_tunnel_does_not_lead(redirected, monkeypatch, capsys):
    """A pid the kernel recycled is a stranger; signalling its whole group could be the shell."""
    tunnel.pidfile().write_text(f"{os.getpid()}\n")
    monkeypatch.setattr(os, "getpgid", lambda checked: checked + 1)
    signalled = []
    monkeypatch.setattr(os, "killpg", lambda group, number: signalled.append((group, number)))

    tunnel.stop()

    assert signalled == []
    assert "not a tunnel" in capsys.readouterr().out
    assert not tunnel.pidfile().exists()


@pytest.mark.parametrize(("answers", "code"), [(True, 0), (False, 1)])
def test_status_exits_zero_when_the_tunnel_is_up_and_one_when_it_is_down(
    redirected, monkeypatch, answers, code
):
    """The exit status is what a script tests, so it is the contract, not the printed line."""
    tunnel.pidfile().write_text(f"{os.getpid()}\n")
    monkeypatch.setattr(tunnel, "probe", lambda port, timeout=None: answers)
    args = argparse.Namespace(loop=False, action="status", port=9090, namespace=None)

    with pytest.raises(SystemExit) as failure:
        tunnel.dispatch(args)

    assert failure.value.code == code


def test_status_is_down_when_no_loop_is_running(redirected, capsys):
    assert tunnel.status() is False
    assert "not running" in capsys.readouterr().out


def test_status_probes_the_port_it_was_given(redirected, monkeypatch):
    """`--port` is the escape hatch for a taken 8080; status must not probe 8080 anyway."""
    tunnel.pidfile().write_text(f"{os.getpid()}\n")
    probed = []
    monkeypatch.setattr(tunnel, "probe", lambda port, timeout=None: probed.append(port) or True)
    args = argparse.Namespace(loop=False, action="status", port=9191, namespace=None)

    with pytest.raises(SystemExit) as failure:
        tunnel.dispatch(args)

    assert failure.value.code == 0
    assert probed == [9191]


def test_restart_logs_in_and_points_kubectl_at_the_cluster_before_starting(
    redirected, monkeypatch
):
    """Starting needs an AWS session and a kubeconfig; stopping and status need neither."""
    order = []
    monkeypatch.setattr(aws, "aws_login", lambda: order.append("login"))
    monkeypatch.setattr(k8s, "kubeconfig", lambda: order.append("kubeconfig"))
    monkeypatch.setattr(tunnel, "stop", lambda: order.append("stop"))
    monkeypatch.setattr(
        tunnel,
        "start",
        lambda port=None, namespace=None: order.append(("start", port, namespace)) or True,
    )

    assert tunnel.restart(port=9191) is True

    assert order == ["login", "kubeconfig", "stop", ("start", 9191, None)]


def test_restart_exits_non_zero_when_the_storefront_never_answered(redirected, monkeypatch):
    """`eks tunnel && open http://localhost:8080` must not treat a silent tunnel as up."""
    monkeypatch.setattr(aws, "aws_login", lambda: None)
    monkeypatch.setattr(k8s, "kubeconfig", lambda: None)
    monkeypatch.setattr(tunnel, "stop", lambda: None)
    monkeypatch.setattr(tunnel, "start", lambda port=None, namespace=None: False)

    with pytest.raises(SystemExit) as failure:
        tunnel.restart()

    assert failure.value.code == 1


class Paused(Exception):
    """Raised out of the loop's retry pause, so a test can watch exactly one iteration."""


@pytest.fixture
def one_iteration(monkeypatch):
    """Turn the loop's retry pause into an exception, leaving one pass through the body."""

    def pause(seconds):
        raise Paused(seconds)

    monkeypatch.setattr(tunnel.time, "sleep", pause)


def test_the_loop_re_runs_the_port_forward_and_pauses_before_retrying(
    redirected, fake_sh, one_iteration
):
    with pytest.raises(Paused) as paused:
        tunnel.loop(port=9090, namespace="otel-demo")

    assert fake_sh.lines() == [
        "aws sts get-caller-identity",
        "kubectl -n otel-demo port-forward --address 127.0.0.1 svc/frontend-proxy 9090:9090",
    ]
    assert paused.value.args[0] == 2, "without a pause, a broken cluster is a busy loop"


def test_the_loop_logs_in_again_when_the_sso_session_has_expired(
    redirected, fake_sh, one_iteration, monkeypatch
):
    monkeypatch.setenv("AWS_PROFILE", "demo")
    fake_sh.reply("aws sts get-caller-identity", returncode=1)
    # Nobody is at the browser at 3 a.m.: the login itself fails, and the loop carries on.
    fake_sh.reply("aws sso login", returncode=1)

    with pytest.raises(Paused):
        tunnel.loop(port=9090, namespace="otel-demo")

    assert fake_sh.lines() == [
        "aws sts get-caller-identity",
        "aws sso login --profile demo",
        "kubectl -n otel-demo port-forward --address 127.0.0.1 svc/frontend-proxy 9090:9090",
    ], "a failed login must not take the loop down with it"


def test_the_loop_defaults_to_the_inherited_environment_never_to_dotenv(
    redirected, fake_sh, one_iteration, monkeypatch
):
    """Reading `.env` needs python-dotenv, and the detached child has no venv to import it from."""
    monkeypatch.setenv("EKS_TUNNEL_PORT", "9595")
    monkeypatch.setattr(core, "dotenv", lambda path: pytest.fail("the loop read .env"))

    with pytest.raises(Paused):
        tunnel.loop()

    assert fake_sh.lines()[-1].endswith(
        f"-n {config.NS_DEMO} port-forward --address 127.0.0.1 svc/frontend-proxy 9595:9595"
    )


LOOP_IN_A_BARE_INTERPRETER = """
import sys
sys.path.insert(0, {root!r})
import launcher.cli as cli
import launcher.core as core
core.run = lambda *argv, **kwargs: sys.exit("reached: " + " ".join(str(a) for a in argv))
args = cli.build_parser().parse_args(["eks", "tunnel", "--loop", "--port", "9090"])
third_party = sorted(
    name
    for name in sys.modules
    if not name.startswith(("_", "launcher"))
    and name.split(".")[0] not in sys.stdlib_module_names
)
print("third party:", third_party, file=sys.stderr)
args.handler(args)
"""


def test_the_loop_path_needs_nothing_but_the_standard_library():
    """The child is a bare interpreter: `-S` drops site-packages, so the venv is not importable."""
    finished = core.run(
        sys.executable,
        "-S",
        "-c",
        LOOP_IN_A_BARE_INTERPRETER.format(root=str(core.ROOT)),
        check=False,
        stdout=core.PIPE,
        stderr=core.PIPE,
        text=True,
    )

    assert "third party: []" in finished.stderr, finished.stderr
    assert "reached: aws sts get-caller-identity" in finished.stderr, finished.stderr
