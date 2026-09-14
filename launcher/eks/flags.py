"""The demo's flagd feature flags, read and written in the running cluster.

Ports `flag.sh`, and adds the EKS half of `demo.py scenario`. Both go through the same file
inside the flagd-ui container (see `k8s.exec_read`), and the scenario transform is the laptop's
`stack.scenario_flags()` so one definition decides what a scenario means on both targets.

The bodies land with the flags task.
"""


def register(subparsers):
    """Declare `eks flag`."""
    parser = subparsers.add_parser(
        "flag", help="list, show or set a flagd feature flag without restarting anything"
    )
    parser.add_argument(
        "name", nargs="?", help="the flag to show or set; omitted lists every flag"
    )
    parser.add_argument(
        "variant", nargs="?", help="the variant to set; omitted shows the flag's variants"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="restart flagd, returning every flag to the chart defaults",
    )
    parser.set_defaults(handler=lambda args: flag(args.name, args.variant, reset=args.reset))


def flag(name=None, variant=None, reset=False):
    """List every flag, show one, or set one to a variant; `--reset` restarts flagd.

    Validates the flag and the variant against the file before writing, is a no-op when the
    variant is already set, and re-reads afterwards so the report is what flagd now holds.
    `productCatalogFailure` is not set from here: its targeting rule always yields a variant,
    which is why `demo.py scenario` exists.
    """
    raise SystemExit("not implemented yet")


def scenario_write_eks(name):
    """Apply a demo scenario to the cluster's flagd file and confirm it by re-reading.

    The transform is `stack.scenario_flags()` -- the same `targeting.if[1]` edit the laptop
    makes -- so a scenario cannot come to mean two different things on the two targets.
    """
    raise SystemExit("not implemented yet")
