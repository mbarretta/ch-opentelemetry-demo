"""AWS: the SSO session, the OpenTofu state and outputs, ECR, and the managed node group.

Ported from `deploy/eks/scripts/lib/aws.sh`. Every call shells out through `core.run` /
`core.capture`, so a test sees the argv rather than the cloud.

These are the shapes the tunnel, infra, k8s, lifecycle, ops and flags modules -- and the
top-level `images` -- are written against, which is why the argv is asserted here rather than
at each of their call sites. What is *not* here: emptying the state bucket and flipping the
nightly schedule's state live in `infra.py`, next to the destroy and nightly commands that own
them -- call those rather than adding a second copy back here.
"""

import json
import os

from .. import core
from . import config

# The parsed `tofu output -json`, read once per process. The lifecycle asks for the cluster, the
# node group, the ECR repositories and the scheduler name in the space of a few lines, and each
# read is a second or two of backend round-trip. `tf_outputs(refresh=True)` re-reads it after an
# apply; a test empties it with `monkeypatch.setattr(aws, "_OUTPUTS", None)`, which it must,
# because the cache outlives any redirection of `core.ROOT`.
_OUTPUTS = None


def aws_login():
    """Establish a session for the `AWS_PROFILE` in `.env`, running `aws sso login` if expired.

    Exports AWS_PROFILE and AWS_REGION into the process environment so every later aws, tofu
    and kubectl call -- including the exec-auth plugin kubectl spawns -- picks them up.
    """
    env = config.load_env()
    profile = env.get("AWS_PROFILE", "").strip()
    if not profile:
        core.die("AWS_PROFILE is unset: set it in the EKS section of .env")
    os.environ["AWS_PROFILE"] = profile
    os.environ["AWS_REGION"] = env.get("AWS_REGION", "").strip() or config.DEFAULT_REGION

    # Captured and then searched rather than piped into grep: an early grep exit would make a
    # SIGPIPE'd `aws` look like a missing profile.
    profiles = core.capture("aws", "configure", "list-profiles").stdout.split()
    if profile not in profiles:
        core.die(
            f"AWS profile '{profile}' is not configured; create it with: "
            f'aws configure sso --profile "{profile}"'
        )

    # SSO sessions expire (typically 8-12 h), so log in again only when the cached credentials
    # no longer work: `aws sso login` opens a browser.
    if _authenticated():
        return
    core.log(f"no valid session for profile {profile}; running aws sso login")
    core.run("aws", "sso", "login", "--profile", profile)
    if not _authenticated():
        core.die(f"still not authenticated as profile {profile} after aws sso login")


def _authenticated():
    """Whether the cached credentials currently work, as a bool rather than an exception."""
    probe = core.run(
        "aws",
        "sts",
        "get-caller-identity",
        check=False,
        stdout=core.DEVNULL,
        stderr=core.DEVNULL,
    )
    return probe.returncode == 0


def state_bucket():
    """The OpenTofu state bucket for the caller's account: STATE_BUCKET_PREFIX + account id."""
    account = core.capture(
        "aws", "sts", "get-caller-identity", "--query", "Account", "--output", "text"
    ).stdout.strip()
    if not account:
        core.die("could not read the AWS account id; is the session still valid?")
    return config.STATE_BUCKET_PREFIX + account


def tf_outputs(refresh=False):
    """Every OpenTofu output as a mapping, read once per process with `output -json`.

    One `tofu output` call answers every question the lifecycle asks (cluster, node group, ECR
    repositories, scheduler), so the result is cached; `refresh=True` re-reads it after an apply.
    """
    global _OUTPUTS
    if _OUTPUTS is not None and not refresh:
        return _OUTPUTS
    result = core.capture(
        "tofu",
        f"-chdir={config.tofu_dir()}",
        "output",
        "-json",
        check=False,
        stderr=core.PIPE,
    )
    if result.returncode:
        core.die(
            f"cannot read the OpenTofu outputs{_reason(result.stderr)}: "
            "run `demo.py eks apply` first"
        )
    try:
        parsed = json.loads(result.stdout or "{}")
    except ValueError:
        core.die("could not parse `tofu output -json`: run `demo.py eks apply` first")
    if not isinstance(parsed, dict):
        core.die("`tofu output -json` did not return an object: run `demo.py eks apply` first")
    # Each output is wrapped in {"sensitive", "type", "value"}; only the value is anyone's
    # business here, and it may be a map or a list (ecr_repository_urls, subnet_ids).
    _OUTPUTS = {name: entry["value"] for name, entry in parsed.items()}
    return _OUTPUTS


def _reason(stderr):
    """tofu's own last words, parenthesised for a message, or nothing when it was quiet.

    Keeping them means "no state" is not the only diagnosis on offer: an expired session and an
    unreachable backend fail here too, and they say so.
    """
    lines = [line.strip() for line in (stderr or "").splitlines() if line.strip()]
    return f" ({lines[-1]})" if lines else ""


def tf_out(name):
    """One OpenTofu output, or die pointing at `demo.py eks apply` and keeping tofu's reason."""
    outputs = tf_outputs()
    if name not in outputs:
        core.die(f"no OpenTofu output '{name}': run `demo.py eks apply` first")
    return outputs[name]


def repository_name(repository):
    """An ECR repository's name, given either the name or the full repository URL.

    The OpenTofu output and `manifest.published` both carry URLs
    (`<registry>/ch-opentelemetry-demo/frontend`), while the ECR API wants the name alone, so
    every caller would otherwise have to remember to split it.

    Only a registry host is stripped, not any first segment with a dot in it: a dot is legal in
    an ECR repository name, so `my.app/frontend` is a repository called `my.app/frontend`.
    """
    head, _, rest = str(repository).partition("/")
    return rest if rest and head.endswith("amazonaws.com") else str(repository)


def ecr_login():
    """Log docker in to the account's ECR registry.

    The password comes from `aws ecr get-login-password` and goes to `docker login
    --password-stdin`: it never appears in argv.
    """
    registry = tf_out("ecr_registry")
    # The repositories live in the region the cluster was applied in, which wins over whatever
    # .env says.
    region = tf_out("region")
    password = core.capture("aws", "ecr", "get-login-password", "--region", region).stdout.strip()
    core.log(f"logging docker in to {registry}")
    core.run(
        "docker",
        "login",
        "--username",
        "AWS",
        "--password-stdin",
        registry,
        input=password,
        text=True,
        stdout=core.DEVNULL,
    )
    return registry


def ecr_has_image(repository, tag):
    """Whether that repository holds an image with that tag; a missing tag is a plain no."""
    probe = core.run(
        "aws",
        "ecr",
        "describe-images",
        "--repository-name",
        repository_name(repository),
        "--image-ids",
        f"imageTag={tag}",
        check=False,
        stdout=core.DEVNULL,
        stderr=core.DEVNULL,
    )
    return probe.returncode == 0


def ecr_image_digest(repository, tag):
    """The registry manifest digest of a pushed image, for `manifest.published[service]`.

    Read back from the registry rather than scraped out of `docker push` output, which is also
    why it answers for an image that was already there and so was never pushed.

    Two failures, one refusal. An absent tag is an error exit -- the same return code
    `ecr_has_image` reads -- and a query that matches nothing prints the string `None`, which
    `--output text` would otherwise hand back as a literal digest. Neither is worth a traceback,
    so the return code is read rather than raised on, and stderr is left on the terminal (see
    `core.capture`) so the CLI's own diagnosis still reaches the operator.
    """
    name = repository_name(repository)
    result = core.capture(
        "aws",
        "ecr",
        "describe-images",
        "--repository-name",
        name,
        "--image-ids",
        f"imageTag={tag}",
        "--query",
        "imageDetails[0].imageDigest",
        "--output",
        "text",
        check=False,
    )
    digest = result.stdout.strip()
    if result.returncode or not digest or digest == "None":
        core.die(f"ECR has no image digest for {name}:{tag}; push it with `demo.py publish`")
    return digest


def _ng_describe(query):
    """One field of the node group, as text.

    The cluster and node group names are resolved into locals first: a failing lookup inside an
    argument list would otherwise reach `aws` as an empty name.
    """
    cluster = tf_out("cluster_name")
    nodegroup = tf_out("nodegroup_name")
    return core.capture(
        "aws",
        "eks",
        "describe-nodegroup",
        "--cluster-name",
        cluster,
        "--nodegroup-name",
        nodegroup,
        "--query",
        query,
        "--output",
        "text",
    ).stdout.strip()


def ng_desired():
    """The node group's current `desiredSize`."""
    value = _ng_describe("nodegroup.scalingConfig.desiredSize")
    try:
        return int(value)
    except ValueError:
        core.die(f"unexpected desiredSize from eks describe-nodegroup: {value!r}")


def ng_status():
    """The node group's status: ACTIVE, UPDATING, ..."""
    return _ng_describe("nodegroup.status")


def ng_scale(size):
    """Set the node group's desired size and wait until it is ACTIVE again.

    Two rejections to avoid: a scaling request while the group is still UPDATING (the nightly
    schedule just fired) and an update that changes nothing, so both are no-ops here.
    """
    size = int(size)
    cluster = tf_out("cluster_name")
    nodegroup = tf_out("nodegroup_name")
    maximum = int(tf_out("node_count"))

    # An update against a node group that is still UPDATING comes back as a
    # ResourceInUseException, so let the previous one settle first -- and before reading the
    # desired size, which mid-update is already the value the previous request asked for.
    if ng_status() == "UPDATING":
        core.log(f"node group {nodegroup} is UPDATING; waiting for it to become ACTIVE")
        _ng_wait_active(cluster, nodegroup)

    if ng_desired() == size:
        core.log(f"node group {nodegroup} already has desiredSize={size}")
        return

    core.log(f"scaling node group {nodegroup} to desiredSize={size} (minSize=0, maxSize={maximum})")
    core.run(
        "aws",
        "eks",
        "update-nodegroup-config",
        "--cluster-name",
        cluster,
        "--nodegroup-name",
        nodegroup,
        "--scaling-config",
        f"minSize=0,maxSize={maximum},desiredSize={size}",
        stdout=core.DEVNULL,
    )
    _ng_wait_active(cluster, nodegroup)


def _ng_wait_active(cluster, nodegroup):
    core.run(
        "aws",
        "eks",
        "wait",
        "nodegroup-active",
        "--cluster-name",
        cluster,
        "--nodegroup-name",
        nodegroup,
    )


def scheduler_get():
    """The nightly scale-down schedule as EventBridge Scheduler returns it."""
    name = tf_out("scheduler_name")
    result = core.capture("aws", "scheduler", "get-schedule", "--name", name, "--output", "json")
    try:
        return json.loads(result.stdout)
    except ValueError:
        core.die(f"could not parse the schedule {name} returned by EventBridge Scheduler")
