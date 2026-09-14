"""Deploying the demo and moving the cluster between idle and up.

Ports `deploy.sh`, `up.sh` and `down.sh`. The bodies land with the lifecycle task.
"""


def register(subparsers):
    """Declare `eks deploy`, `eks up` and `eks down`."""
    deploy_parser = subparsers.add_parser(
        "deploy", help="deploy the collector and the demo onto the running cluster, then tunnel"
    )
    deploy_parser.set_defaults(handler=lambda args: deploy())

    up_parser = subparsers.add_parser(
        "up", help="scale the node group up, wait for the nodes, then deploy"
    )
    up_parser.set_defaults(handler=lambda args: up())

    down_parser = subparsers.add_parser(
        "down", help="close the tunnel, remove the workloads and scale the node group to zero"
    )
    down_parser.add_argument(
        "--keep",
        action="store_true",
        help="scale to zero but leave the workloads in the API; they reschedule on the way up",
    )
    down_parser.set_defaults(handler=lambda args: down(keep=args.keep))


def deploy():
    """Deploy the ClickStack collector and the demo release, then open the tunnel.

    Ordered so that nothing expensive or half-done happens after a knowable refusal: the
    environment and the published-image gate first, then the AWS session and kubeconfig, then
    the zero-node refusal (deploying onto no nodes would leave every pod Pending until the
    20-minute `helm --wait` gave up), and only then the namespaces, Secrets, collector, and the
    release. Safe to re-run: every step is an apply or an upgrade.
    """
    raise SystemExit("not implemented yet")


def up():
    """Bring the demo up from idle: scale the node group, wait for the nodes, then deploy."""
    raise SystemExit("not implemented yet")


def down(keep=False):
    """Return to the idle state: tunnel down, workloads removed, node group at zero.

    The control plane, the ECR images and the OpenTofu state stay, so `eks up` brings it back
    in minutes. Telemetry already written to ClickHouse Cloud is never touched.
    """
    raise SystemExit("not implemented yet")
