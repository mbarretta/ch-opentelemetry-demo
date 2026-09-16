"""The AWS infrastructure: state bucket, OpenTofu, and the nightly scale-down schedule.

Ports `init.sh`, `apply.sh`, `destroy.sh` and `nightly.sh`.

These four commands own the two things that cost money and cannot be recreated from the tree:
the S3 bucket holding the OpenTofu state and the EKS control plane the state describes. The
bucket name and state key come from `config` rather than from anything derived here, because
moving either one orphans the live cluster instead of managing it.
"""

import json
import shutil
import sys

from .. import core
from . import aws, config, k8s, lifecycle, tunnel

# The one region whose `create-bucket` must not carry a LocationConstraint: us-east-1 is the S3
# API's global default and rejects being named explicitly (InvalidLocationConstraint).
LEGACY_GLOBAL_REGION = "us-east-1"

# `delete-objects` takes at most this many keys per call.
DELETE_BATCH_LIMIT = 1000

# `eks nightly on|off` -> the EventBridge Scheduler state it means.
SCHEDULE_STATES = {"on": "ENABLED", "off": "DISABLED"}

# `update-schedule` replaces the whole schedule: every field not passed is reset. These are the
# writable fields `get-schedule` can return, mapped to the flag that feeds each one back, so a
# schedule created with a description, a KMS key or a start date keeps them across a state flip.
SCHEDULE_FLAGS = {
    "Name": "--name",
    "GroupName": "--group-name",
    "ScheduleExpression": "--schedule-expression",
    "ScheduleExpressionTimezone": "--schedule-expression-timezone",
    "StartDate": "--start-date",
    "EndDate": "--end-date",
    "Description": "--description",
    "FlexibleTimeWindow": "--flexible-time-window",
    "Target": "--target",
    "KmsKeyArn": "--kms-key-arn",
    "ActionAfterCompletion": "--action-after-completion",
    "State": "--state",
}
# Returned by `get-schedule` and not settable, so dropping them is correct rather than lossy.
SCHEDULE_READ_ONLY = ("Arn", "CreationDate", "LastModificationDate")


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
    core.need("aws", "tofu", "docker", "git", "kubectl", "helm")

    aws.aws_login()
    core.log("authenticated as")
    core.run("aws", "sts", "get-caller-identity", "--output", "table")

    env = config.load_env()
    bucket_region = region(env)
    bucket = aws.state_bucket()
    ensure_state_bucket(bucket, bucket_region)

    # backend.tf declares `backend "s3"` with no bucket, key or region, so the account-specific
    # values never land in the repository; they are supplied here, from config, unchanged.
    core.log(f"tofu init (backend s3://{bucket}/{config.STATE_KEY})")
    tofu(
        "init",
        "-input=false",
        f"-backend-config=bucket={bucket}",
        f"-backend-config=key={config.STATE_KEY}",
        f"-backend-config=region={bucket_region}",
    )

    core.log(f"helm repo {config.HELM_REPO_NAME}")
    k8s.helm_repo_ensure()

    blank = warn_blank_keys(env)
    print()
    core.log("init complete")
    if blank:
        print("Fill in the key(s) above, then run: demo.py eks apply")
    else:
        print("Next: demo.py eks apply")


def region(env=None):
    """The region every `aws` and `tofu` call in this module works in.

    `aws.aws_login()` has already exported AWS_REGION by the time init asks, so reading it back
    out of the environment keeps one answer rather than two.
    """
    values = config.load_env() if env is None else env
    return values.get("AWS_REGION") or config.DEFAULT_REGION


def bucket_exists(bucket):
    """Whether the state bucket is already there. A missing bucket is a plain no, not an error."""
    probe = core.run(
        "aws",
        "s3api",
        "head-bucket",
        "--bucket",
        bucket,
        check=False,
        stdout=core.DEVNULL,
        stderr=core.DEVNULL,
    )
    return probe.returncode == 0


def ensure_state_bucket(bucket, bucket_region):
    """Create the state bucket when it is absent, then harden it -- on every run.

    Versioning and the public-access block are re-applied even when the bucket already exists:
    both calls are idempotent, and a bucket left behind by an interrupted first run would
    otherwise stay unversioned, which is exactly the state in which a clobbered `terraform.tfstate`
    is unrecoverable.
    """
    if bucket_exists(bucket):
        core.log(f"state bucket s3://{bucket} already exists")
    else:
        core.log(f"creating state bucket s3://{bucket} in {bucket_region}")
        create = ["aws", "s3api", "create-bucket", "--bucket", bucket, "--region", bucket_region]
        if bucket_region != LEGACY_GLOBAL_REGION:
            create += ["--create-bucket-configuration", f"LocationConstraint={bucket_region}"]
        core.run(*create, stdout=core.DEVNULL)

    core.log(f"enabling versioning and blocking public access on s3://{bucket}")
    core.run(
        "aws",
        "s3api",
        "put-bucket-versioning",
        "--bucket",
        bucket,
        "--versioning-configuration",
        "Status=Enabled",
        stdout=core.DEVNULL,
    )
    core.run(
        "aws",
        "s3api",
        "put-public-access-block",
        "--bucket",
        bucket,
        "--public-access-block-configuration",
        "BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,"
        "RestrictPublicBuckets=true",
        stdout=core.DEVNULL,
    )


def warn_blank_keys(env):
    """Report, without failing, the ClickStack and Langfuse keys `eks deploy` will need.

    `init` and `apply` are infrastructure: they need an AWS session and nothing else. Naming the
    blanks now means a presenter fills `.env` in during the ten minutes the control plane takes,
    rather than discovering the first blank key at deploy time -- where `config.load_clickstack_env`
    and `config.load_langfuse_env` both refuse outright rather than warning. This stays a warning
    here on purpose: `init` and `apply` are infrastructure-only and genuinely need neither
    ClickHouse nor Langfuse credentials to succeed.
    """
    blank = [key for key in config.CLICKSTACK_KEYS if not env.get(key)]
    blank += [key for key in config.LANGFUSE_KEYS if not env.get(key)]
    if blank:
        print(
            f"warning: blank or missing in .env: {', '.join(blank)}. "
            "`demo.py eks init` and `apply` do not need them; `eks deploy` does. "
            "The EKS section of .env.example describes all five ClickStack keys and all three "
            "Langfuse keys; deploy/eks/sql/create-user.sql creates the ClickHouse user.",
            file=sys.stderr,
        )
    return blank


def tofu(*args, **kwargs):
    """Run OpenTofu against `deploy/eks/tofu`.

    stdin is inherited (no `stdin=`, no `-input=false` unless a caller asks for it), which is
    what keeps `apply` and `destroy` interactive.
    """
    return core.run("tofu", f"-chdir={config.tofu_dir()}", *args, **kwargs)


def initialised():
    """Whether `tofu init` has configured the S3 backend in this checkout.

    The backend-state file, not the directory: `.terraform/` alone also appears after `eks
    check` runs `init -backend=false`.
    """
    return (config.tofu_dir() / ".terraform/terraform.tfstate").exists()


def _idle_nodegroup_exists():
    """Whether a *previously applied* node group is sitting `ACTIVE` at `desiredSize=0`.

    Read straight from `tofu output -json` rather than through `aws.tf_out` (which `die`s):
    on the very first apply there is no state yet, and an empty/absent `nodegroup_name` output
    just means "nothing to protect", not a refusal.
    """
    result = core.capture(
        "tofu", f"-chdir={config.tofu_dir()}", "output", "-json", check=False, stderr=core.DEVNULL
    )
    if result.returncode:
        return False
    try:
        parsed = json.loads(result.stdout or "{}")
    except ValueError:
        return False
    if "nodegroup_name" not in parsed:
        return False
    return aws.ng_status() == "ACTIVE" and aws.ng_desired() == 0


def apply(yes=False):
    """Run `tofu apply`, then write the kubeconfig and report the next steps.

    Refuses before `init`, because `tofu apply` on an uninitialised backend fails with a
    generic message whose fix is always the same. Interactive by default: tofu's own
    confirmation prompt reads the inherited stdin.

    A node group that already exists and is idle (`desiredSize=0`) is scaled up to
    `node_count` before the tofu apply and back down to 0 after it: EKS add-on version
    updates (coredns, kube-proxy, vpc-cni all pick up whatever AWS currently publishes as
    `most_recent`) have to reschedule pods to reach ACTIVE, and at zero nodes there is nowhere
    to put them, so `tofu apply` blocks for the add-on's full 20-minute timeout and then fails.
    `eks.tf`'s node group comment already names this failure for an add-on *create* on the very
    first apply, where the node group is created at `node_count` alongside it; it recurs on any
    later re-apply while idle, whenever AWS has shipped a newer add-on patch since the last one.
    Restoring 0 afterwards keeps `apply` infra-only -- `eks deploy` still refuses at zero nodes
    and points at `eks up`, exactly as before. `warn_blank_keys` runs here too, before the tofu
    apply, for the same reason `init` runs it: a presenter reads the warning while the control
    plane is still coming up rather than at `eks deploy`, which is where a blank key actually
    refuses.
    """
    core.need("aws", "tofu", "kubectl")
    if not initialised():
        core.die("OpenTofu backend not initialised: run `demo.py eks init` first")

    aws.aws_login()
    warn_blank_keys(config.load_env())

    scaled_up_for_addons = _idle_nodegroup_exists()
    if scaled_up_for_addons:
        node_count = int(aws.tf_out("node_count"))
        core.log(
            f"node group is idle; scaling to {node_count} first, so add-on version updates "
            "have somewhere to reschedule pods"
        )
        aws.ng_scale(node_count)

    core.log("tofu apply")
    tofu("apply", *(["-auto-approve"] if yes else []))

    core.log("kubeconfig")
    k8s.kubeconfig()
    # The node group starts at desiredSize = node_count, but kubelets can take a minute or two
    # to register after apply returns; an empty list here is normal.
    core.run("kubectl", "get", "nodes", "-o", "wide")

    if scaled_up_for_addons:
        core.log("scaling the node group back to 0 (only brought up for the add-on updates)")
        aws.ng_scale(0)

    print()
    core.log("tofu output")
    tofu("output")

    print(
        "\nnote: this repository's ECR layout is four repositories (ch-opentelemetry-demo/"
        "<service>), not the single otel-demo-frontend the EKS repo created. The repository name"
        " forces replacement and there is no `moved` block, so the first apply after the merge"
        " destroys otel-demo-frontend and every image in it: re-publish before deploying.\n"
        "\nNext:\n"
        "  demo.py build              build the four images at the content tag\n"
        "  demo.py publish            push them to the ECR repositories this apply created\n"
        "  demo.py eks deploy         install the ClickStack collector and the demo, then tunnel"
    )


def tolerate(description, action):
    """Run a courtesy step whose failure must not stop a teardown.

    The bash spelling was `|| true` plus a warning. SystemExit is caught with everything else:
    `core.die` and an unimplemented helper both raise it, and neither is a reason to leave a
    cluster running.
    """
    try:
        return action()
    except (Exception, SystemExit) as failure:
        print(
            f"warning: {description} did not complete cleanly ({failure}); continuing",
            file=sys.stderr,
        )
        return None


def destroy(purge_state=False):
    """Destroy the cluster, node group, ECR repositories, schedule and (if dedicated) the VPC.

    Closes the tunnel and takes the workloads down first as a courtesy, tolerating failure.
    Without `--purge-state` the emptied state bucket stays so a later init/apply reuses it.
    """
    # kubectl and helm are not required: only the courtesy `down` below uses them, it guards
    # itself, and its failure is tolerated.
    core.need("aws", "tofu")

    tolerate("closing the tunnel", tunnel.stop)

    aws.aws_login()

    # A missing or half-destroyed cluster must not stop the destroy: nothing the demo creates
    # in the cluster is an out-of-band resource that would block `tofu destroy`.
    cluster = tolerate(
        "reading cluster_name from the OpenTofu state", lambda: aws.tf_out("cluster_name")
    )
    if cluster and cluster_reachable(cluster):
        core.log(f"cluster {cluster} exists; running down first")
        tolerate("down", lifecycle.down)
    else:
        core.log("no reachable cluster in state; skipping down")

    core.log("tofu destroy")
    tofu("destroy")

    if purge_state:
        purge_state_bucket()

    print()
    core.log("destroy complete")


def cluster_reachable(cluster):
    """Whether that EKS cluster still answers `describe-cluster`."""
    probe = core.run(
        "aws",
        "eks",
        "describe-cluster",
        "--name",
        cluster,
        check=False,
        stdout=core.DEVNULL,
        stderr=core.DEVNULL,
    )
    return probe.returncode == 0


def purge_state_bucket():
    """Empty and delete the state bucket, and drop the backend config that pointed at it."""
    bucket = aws.state_bucket()
    if not bucket_exists(bucket):
        core.log(f"state bucket s3://{bucket} does not exist; nothing to purge")
        return

    core.log(f"purging state bucket s3://{bucket}")
    purge_bucket(bucket)
    # The local backend config still names the bucket that is now gone.
    shutil.rmtree(config.tofu_dir() / ".terraform", ignore_errors=True)
    print("State bucket deleted. To start over: demo.py eks init && demo.py eks apply")


def delete_batches(listing, limit=DELETE_BATCH_LIMIT):
    """The `list-object-versions` answer as `delete-objects` payloads of at most `limit` keys.

    Both groups, not just `Versions`: versioning is on, so a key deleted earlier survives as a
    delete marker, and a bucket holding only markers is still non-empty as far as
    `delete-bucket` is concerned.
    """
    keys = [
        {"Key": entry["Key"], "VersionId": entry["VersionId"]}
        for group in ("Versions", "DeleteMarkers")
        for entry in listing.get(group) or ()
    ]
    return [keys[start : start + limit] for start in range(0, len(keys), limit)]


def purge_bucket(bucket):
    """Delete every object version and delete marker, then the bucket itself.

    Versioning is on (`init`), so a plain `aws s3 rm --recursive` would leave the old versions
    behind and `delete-bucket` would fail with BucketNotEmpty. The bucket goes last and only if
    every batch succeeded: `core.run` raises on a failed batch, which leaves the bucket in place
    holding whatever is left rather than reporting a deletion that did not happen.
    """
    while True:
        listing = core.capture(
            "aws", "s3api", "list-object-versions", "--bucket", bucket, "--output", "json"
        )
        batches = delete_batches(json.loads(listing.stdout or "{}"))
        if not batches:
            break
        for batch in batches:
            delete_objects(bucket, batch)

    core.run("aws", "s3api", "delete-bucket", "--bucket", bucket)


def delete_objects(bucket, batch):
    """Delete one batch of versions, and refuse to pretend a key that survived was removed.

    `delete-objects` reports a key it could not delete (object lock, a bucket policy) in the
    response's `Errors` rather than in its exit status, so a batch that "succeeded" can leave the
    bucket exactly as it was -- and the caller's loop would then re-list, re-delete and never
    finish. Reading the response is what turns that into one message.
    """
    response = core.capture(
        "aws",
        "s3api",
        "delete-objects",
        "--bucket",
        bucket,
        "--delete",
        json.dumps({"Objects": batch, "Quiet": True}),
        "--output",
        "json",
    )
    errors = json.loads(response.stdout or "{}").get("Errors") or []
    if errors:
        first = errors[0]
        core.die(
            f"could not delete {len(errors)} of {len(batch)} object version(s) from "
            f"s3://{bucket} ({first.get('Key')}: {first.get('Code')} {first.get('Message', '')}). "
            "The bucket is left in place; the OpenTofu state it holds is still there."
        )


def update_schedule_args(name, current, state):
    """The full `update-schedule` argv that changes only `State`.

    `update-schedule` is a replace, not a patch: a field the call omits is reset to its default,
    so the schedule OpenTofu created (its expression, timezone, flexible window and target) has
    to be read and fed straight back. A field this mapping does not know about is reported
    rather than dropped in silence -- the command still works, but the operator learns that
    something in the schedule will not survive the flip.
    """
    fields = {**current, "Name": name, "State": state}
    unknown = [
        field for field in fields if field not in SCHEDULE_FLAGS and field not in SCHEDULE_READ_ONLY
    ]
    if unknown:
        print(
            f"warning: schedule field(s) {', '.join(sorted(unknown))} are not fed back to "
            "update-schedule and will be reset; add them to SCHEDULE_FLAGS.",
            file=sys.stderr,
        )

    args = ["aws", "scheduler", "update-schedule"]
    for field, flag in SCHEDULE_FLAGS.items():
        value = fields.get(field)
        # An absent or empty field contributes no argument, the way `${description:+...}` did:
        # `--description ""` is a value, not an omission, and some flags reject the empty string.
        if value is None or value == "" or value == {} or value == []:
            continue
        args += [flag, json.dumps(value) if isinstance(value, (dict, list)) else str(value)]
    return args


def nightly(state):
    """Turn the nightly scale-down schedule on or off, and print what it will do.

    The hour and timezone are OpenTofu variables (`scale_down_hour`, `scale_down_timezone`),
    changed with `eks apply`, not here.
    """
    if state not in SCHEDULE_STATES:
        core.die(f"nightly takes on or off, not {state!r}")
    wanted = SCHEDULE_STATES[state]

    core.need("aws", "tofu")
    aws.aws_login()

    name = aws.tf_out("scheduler_name")
    current = aws.scheduler_get()

    core.log(f"setting schedule {name} to {wanted}")
    core.run(*update_schedule_args(name, current, wanted), stdout=core.DEVNULL)

    core.run(
        "aws",
        "scheduler",
        "get-schedule",
        "--name",
        name,
        "--query",
        "{State: State, ScheduleExpression: ScheduleExpression, "
        "Timezone: ScheduleExpressionTimezone}",
        "--output",
        "table",
    )
