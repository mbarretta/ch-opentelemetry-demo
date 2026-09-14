"""Is telemetry landing, and where does the deployment stand.

Ports `verify.sh` and `status.sh`. The bodies land with the ops task.
"""


def register(subparsers):
    """Declare `eks verify` and `eks status`."""
    verify_parser = subparsers.add_parser(
        "verify", help="confirm telemetry is reaching ClickHouse Cloud, and report what is not"
    )
    verify_parser.set_defaults(handler=lambda args: verify())

    status_parser = subparsers.add_parser(
        "status", help="who you are, what is running, and what the nightly schedule will do"
    )
    status_parser.set_defaults(handler=lambda args: status())


def verify():
    """Query ClickHouse Cloud for recent rows, then show the collector pods and their logs.

    The credentials travel as request headers through `httpx`, never in argv. Traces, logs,
    metrics and session-replay counts are reported; a zero count for replay is reported rather
    than failed, because replay needs a browser to have visited the storefront.
    """
    raise SystemExit("not implemented yet")


def status():
    """Report identity, node group, pods, release, tunnel, published images and schedule.

    Exits 0 in both the idle and the up state: a non-zero exit means something is actually
    broken (authentication, missing state), not that the demo is switched off.
    """
    raise SystemExit("not implemented yet")
