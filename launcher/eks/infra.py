"""The AWS infrastructure: state bucket, OpenTofu, and the nightly scale-down schedule.

Ports `init.sh`, `apply.sh`, `destroy.sh` and `nightly.sh`. The bodies land with the infra task.
"""


def register(subparsers):
    """Declare `eks init`, `eks apply`, `eks destroy` and `eks nightly`."""
    init_parser = subparsers.add_parser(
        "init", help="create the OpenTofu state bucket and initialise the backend"
    )
    init_parser.set_defaults(handler=lambda args: init())

    apply_parser = subparsers.add_parser(
        "apply", help="create or update the cluster, node group, ECR repositories and schedule"
    )
    apply_parser.add_argument(
        "--yes",
        "-y",
        action="store_true",
        help="apply without the confirmation prompt",
    )
    apply_parser.set_defaults(handler=lambda args: apply(yes=args.yes))

    destroy_parser = subparsers.add_parser(
        "destroy", help="tear down everything OpenTofu created; this is the end of the demo"
    )
    destroy_parser.add_argument(
        "--purge-state",
        action="store_true",
        help="also delete the OpenTofu state bucket and the local .terraform directory",
    )
    destroy_parser.set_defaults(handler=lambda args: destroy(purge_state=args.purge_state))

    nightly_parser = subparsers.add_parser(
        "nightly", help="enable or disable the nightly scale-to-zero schedule"
    )
    nightly_parser.add_argument("state", choices=("on", "off"))
    nightly_parser.set_defaults(handler=lambda args: nightly(args.state))


def init():
    """Prepare the account for a deployment: required commands, state bucket, `tofu init`.

    Idempotent end to end: the bucket is created only when absent (with no LocationConstraint
    in us-east-1), versioning and the public-access block are applied every time, and the
    backend is re-initialised against the same bucket and key. Blank EKS keys in `.env` are a
    warning here, not a failure -- they are only needed at deploy time.
    """
    raise SystemExit("not implemented yet")


def apply(yes=False):
    """Run `tofu apply`, then write the kubeconfig and report the next steps.

    Refuses before `init`, because `tofu apply` on an uninitialised backend fails with a
    generic message whose fix is always the same. Interactive by default: tofu's own
    confirmation prompt reads the inherited stdin.
    """
    raise SystemExit("not implemented yet")


def destroy(purge_state=False):
    """Destroy the cluster, node group, ECR repositories, schedule and (if dedicated) the VPC.

    Closes the tunnel and takes the workloads down first as a courtesy, tolerating failure.
    Without `--purge-state` the emptied state bucket stays so a later init/apply reuses it.
    """
    raise SystemExit("not implemented yet")


def nightly(state):
    """Turn the nightly scale-down schedule on or off, and print what it will do.

    The hour and timezone are OpenTofu variables (`scale_down_hour`, `scale_down_timezone`),
    changed with `eks apply`, not here.
    """
    raise SystemExit("not implemented yet")
