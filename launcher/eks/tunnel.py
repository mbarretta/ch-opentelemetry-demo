"""The local port-forward to the storefront, and the detached loop that keeps it alive.

The cluster has no ingress: the storefront is reached through `kubectl port-forward` on
127.0.0.1. Ports `tunnel.sh` and the loop in `lib/k8s.sh`.

`--loop` is the hidden self-invocation: `start()` spawns this same CLI in a new session
(`start_new_session=True`, so a laptop closing the terminal does not take the tunnel with it)
and the child runs the re-connect loop, re-running `aws sso login` when the session expires.

Two consequences of that child shape run through the whole module. It is a *process group*,
which is why `stop()` signals the group rather than the pid -- the loop spawns `kubectl` under
itself, and a `kubectl port-forward` that outlives the loop keeps the port. And it is a *bare
interpreter*: nothing guarantees the venv's third-party packages are importable, so the `--loop`
path reads its port from the command line and the inherited environment instead of `.env`
(`core.dotenv()` imports python-dotenv) and probes with `urllib` instead of `httpx`.
"""

import argparse
import os
import signal
import socket
import sys
import time
import urllib.error
import urllib.request

from .. import core
from . import aws, config, k8s

# The service the storefront is behind, and the readiness budget: twenty attempts a second
# apart, the same twenty seconds the bash allowed.
SERVICE = "svc/frontend-proxy"
PROBE_ATTEMPTS = 20
PROBE_INTERVAL = 1
PROBE_TIMEOUT = 2
# The collision check only has to reach a listener on loopback, which either answers at once or
# not at all; it is not the readiness budget and does not move with it.
CONNECT_TIMEOUT = 1
# `status` waits a second longer than `start`: it runs once, against a tunnel that should
# already be up, so a slow answer is worth waiting for rather than reporting as down.
STATUS_PROBE_TIMEOUT = 3
# How long a TERMed group has before it is KILLed, and how often it is looked at.
STOP_GRACE = 5
STOP_POLL = 0.5
# The pause between port-forwards in the loop: without it, a cluster that refuses every
# connection turns the re-connect loop into a busy loop. Nothing about `stop()` depends on it --
# TERM goes to the whole group, so a sleeping loop and its kubectl go down together.
RETRY_DELAY = 2


def register(subparsers):
    """Declare `eks tunnel [stop|status]` and its hidden loop flags."""
    parser = subparsers.add_parser(
        "tunnel", help="manage the local port-forward to the storefront"
    )
    parser.add_argument(
        "action",
        nargs="?",
        choices=("stop", "status"),
        help="stop the tunnel, or report whether it is up (exit 0 when up, 1 when not); "
        "omitted restarts it",
    )
    # The loop is the detached child `start()` spawns, not something to run by hand; --port and
    # --namespace exist for it and as an escape hatch when EKS_TUNNEL_PORT is already taken.
    # They reach the loop, the start and the status probe; `stop` finds the tunnel through the
    # pidfile, so there is nothing for a port to say to it.
    parser.add_argument("--loop", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--port", type=int, metavar="PORT", help=argparse.SUPPRESS)
    parser.add_argument("--namespace", metavar="NS", help=argparse.SUPPRESS)
    parser.set_defaults(handler=dispatch)


def dispatch(args):
    """Route `eks tunnel` to the action its arguments name."""
    if args.loop:
        return loop(port=args.port, namespace=args.namespace)
    if args.action == "stop":
        return stop()
    if args.action == "status":
        # The exit status is the point of `tunnel status`: it is what a script tests.
        raise SystemExit(0 if status(port=args.port) else 1)
    return restart(port=args.port, namespace=args.namespace)


def pidfile():
    """Where the detached loop's pid is recorded, between invocations."""
    return config.run_dir() / "tunnel.pid"


def logfile():
    """Where the detached loop's output goes; the only account of why a tunnel died."""
    return config.run_dir() / "tunnel.log"


def url(port):
    return f"http://localhost:{port}/"


def resolved_port(port=None):
    """The port to forward: the flag, then `EKS_TUNNEL_PORT`, then the default.

    Reads `.env`, so this is the parent's resolution only; see `loop()` for the child's.
    """
    if port:
        return int(port)
    return int(config.load_env().get("EKS_TUNNEL_PORT") or config.DEFAULT_TUNNEL_PORT)


def read_pid():
    """The pid in the pidfile, or None when there is no usable one.

    A file naming something that is not a number is as stale as one naming a dead process: both
    are litter from an interrupted run, and both are cleaned up rather than reported.
    """
    try:
        return int(pidfile().read_text().strip())
    except (FileNotFoundError, ValueError):
        return None


def alive(pid):
    """Whether that pid names a running process. Signal 0 checks without delivering."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Somebody else's process: running, but not a tunnel this CLI started.
        return True
    return True


def leads_its_group(pid):
    """Whether that pid is the leader of its own process group.

    `start()` spawns the loop with `start_new_session=True`, so a live tunnel's pid *is* its
    group id. Anything else in the pidfile is a pid the kernel has recycled, and signalling a
    stranger's whole group -- which could be the caller's own shell -- is worse than leaving a
    process behind.
    """
    try:
        return os.getpgid(pid) == pid
    except (ProcessLookupError, PermissionError):
        return False


def port_is_busy(port):
    """Whether something already listens on the port, which `kubectl port-forward` could not.

    Replaces the bash `lsof -iTCP -sTCP:LISTEN`: a connect attempt answers the same question
    without a third tool, and without needing to own the process holding the port.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe_socket:
        probe_socket.settimeout(CONNECT_TIMEOUT)
        return probe_socket.connect_ex(("127.0.0.1", port)) == 0


def probe(port, timeout=PROBE_TIMEOUT):
    """Whether the storefront answers on the port at all.

    Any HTTP status counts, which is the whole point: Envoy answers 503 while the pods behind
    it are still starting, and that is a demo that is not ready yet, not a tunnel that is down.
    """
    try:
        urllib.request.urlopen(url(port), timeout=timeout).close()
    except urllib.error.HTTPError:
        return True
    except OSError:
        # Connection refused, reset, or timed out: nothing is carrying the request.
        return False
    return True


def start(port=None, namespace=None):
    """Start the detached tunnel and wait until the storefront answers.

    Refuses when the pidfile names a live process or the port is already busy; a stale pidfile
    is cleaned up rather than reported. The probe allows the storefront 20 seconds to answer,
    and counts Envoy's 503 as up: the port-forward is established, the pods behind it are not
    all ready yet.
    """
    port = resolved_port(port)
    namespace = namespace or config.NS_DEMO
    core.need("aws", "kubectl")

    running = read_pid()
    if running is not None and alive(running):
        core.die(
            f"tunnel is already running (pid {running}): {url(port)} "
            "-- run `demo.py eks tunnel stop` first"
        )
    if port_is_busy(port):
        core.die(
            f"port {port} is already in use by another process; "
            "free it, or set EKS_TUNNEL_PORT in .env to a port that is not taken"
        )
    pidfile().unlink(missing_ok=True)

    config.run_dir().mkdir(parents=True, exist_ok=True)
    # Appended to rather than truncated, unlike the bash: the first question after a tunnel
    # dies overnight is why, and the answer is in the log the next `start` would have replaced.
    # `-u` so the loop's own lines reach it as they happen rather than a buffer at a time,
    # which is what makes tailing it useful while a tunnel is flapping.
    with logfile().open("ab") as log:
        child = core.popen(
            sys.executable,
            "-u",
            core.ROOT / "scripts/demo.py",
            "eks",
            "tunnel",
            "--loop",
            "--port",
            port,
            "--namespace",
            namespace,
            stdin=core.DEVNULL,
            stdout=log,
            stderr=log,
            start_new_session=True,
        )
    pidfile().write_text(f"{child.pid}\n")

    for _ in range(PROBE_ATTEMPTS):
        if probe(port):
            core.log(f"tunnel up (pid {child.pid}): {url(port)}")
            return True
        if not alive(child.pid):
            core.die(f"tunnel loop exited immediately; see {logfile()}")
        time.sleep(PROBE_INTERVAL)
    # The loop is alive and will keep trying, so this is a warning and not a failure: a
    # `deploy` that got this far is better off reporting it than unwinding.
    print(
        f"warning: tunnel loop is running (pid {child.pid}) but {url(port)} did not answer "
        f"within {PROBE_ATTEMPTS * PROBE_INTERVAL} s; see {logfile()}",
        file=sys.stderr,
    )
    return False


def stop():
    """Stop the tunnel: TERM the process group, KILL it after five seconds, clear the pidfile.

    The group, not the pid: the loop child spawns `kubectl port-forward` under itself.
    """
    pid = read_pid()
    if pid is None or not alive(pid):
        pidfile().unlink(missing_ok=True)
        core.log("tunnel is not running")
        return
    if not leads_its_group(pid):
        pidfile().unlink(missing_ok=True)
        core.log(f"pidfile named pid {pid}, which is not a tunnel; left it alone")
        return

    signal_group(pid, signal.SIGTERM)
    for _ in range(round(STOP_GRACE / STOP_POLL)):
        if not alive(pid):
            break
        time.sleep(STOP_POLL)
    if alive(pid):
        signal_group(pid, signal.SIGKILL)
    pidfile().unlink(missing_ok=True)
    core.log("tunnel stopped")


def signal_group(pid, number):
    """Signal a tunnel's whole process group, tolerating a group that has already gone."""
    try:
        os.killpg(pid, number)
    except (ProcessLookupError, PermissionError):
        pass


def status(port=None):
    """Whether the tunnel is up, as a bool, after reporting what it found."""
    pid = read_pid()
    if pid is None or not alive(pid):
        print("tunnel: not running")
        return False
    port = resolved_port(port)
    if probe(port, timeout=STATUS_PROBE_TIMEOUT):
        print(f"tunnel: up (pid {pid}) {url(port)}")
        return True
    print(f"tunnel: loop running (pid {pid}) but {url(port)} is not answering; see {logfile()}")
    return False


def restart(port=None, namespace=None):
    """Stop any running tunnel, then start one. The default `eks tunnel` action.

    Exits non-zero when the loop is up but the storefront never answered, which is what the
    bash entry point did under `set -e`: a caller chaining something onto `eks tunnel` must not
    read that as a working tunnel. `lifecycle.deploy` keeps the tolerant path by calling
    `start()` directly, exactly as `deploy.sh` spelled it `tunnel_start || true`.
    """
    # Starting needs an AWS session -- the loop re-runs `aws sso login` for the AWS_PROFILE
    # exported here -- and a kubeconfig pointing at the cluster. Stopping needs neither, which
    # is why both live here rather than in start().
    aws.aws_login()
    k8s.kubeconfig()
    stop()
    if not start(port=port, namespace=namespace):
        raise SystemExit(1)
    return True


def loop(port=None, namespace=None):
    """The detached child: re-run `kubectl port-forward` for as long as it keeps dying.

    Standard library only, and logging to `.runtime/eks/tunnel.log`: it outlives the parent
    process and its terminal. The port arrives on the command line and AWS_PROFILE in the
    inherited environment, because reading `.env` would mean importing python-dotenv and this
    interpreter has no venv to import it from.
    """
    port = int(port or os.environ.get("EKS_TUNNEL_PORT") or config.DEFAULT_TUNNEL_PORT)
    namespace = namespace or config.NS_DEMO
    profile = os.environ.get("AWS_PROFILE")
    while True:
        expired = core.run(
            "aws",
            "sts",
            "get-caller-identity",
            check=False,
            stdout=core.DEVNULL,
            stderr=core.DEVNULL,
        ).returncode
        # check=False throughout: every command here is expected to fail eventually (a pod
        # restart, a closed laptop lid, a login nobody was at the browser for), and the answer
        # is always another pass through the loop rather than an exit. Surviving an expired SSO
        # session unattended is the reason this loop exists.
        if expired:
            core.log("AWS session expired; running `aws sso login`")
            core.run(
                "aws", "sso", "login", *(("--profile", profile) if profile else ()), check=False
            )
        core.log(f"forwarding 127.0.0.1:{port} to {SERVICE} in {namespace}")
        core.run(
            "kubectl",
            "-n",
            namespace,
            "port-forward",
            "--address",
            "127.0.0.1",
            SERVICE,
            f"{port}:{port}",
            check=False,
        )
        time.sleep(RETRY_DELAY)
