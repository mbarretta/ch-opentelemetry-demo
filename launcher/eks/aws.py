"""AWS: the SSO session, the OpenTofu state and outputs, ECR, and the managed node group.

Ported from `deploy/eks/scripts/lib/aws.sh`. Every call shells out through `core.run` /
`core.capture`, so a test sees the argv rather than the cloud.

The bodies land with the aws/k8s helper task; the signatures here are what the tunnel, infra,
lifecycle, publish, ops and check modules are written against.
"""


def aws_login():
    """Establish a session for the `AWS_PROFILE` in `.env`, running `aws sso login` if expired.

    Exports AWS_PROFILE and AWS_REGION into the process environment so every later aws, tofu
    and kubectl call -- including the exec-auth plugin kubectl spawns -- picks them up.
    """
    raise SystemExit("not implemented yet")


def state_bucket():
    """The OpenTofu state bucket for the caller's account: STATE_BUCKET_PREFIX + account id."""
    raise SystemExit("not implemented yet")


def tf_outputs(refresh=False):
    """Every OpenTofu output as a mapping, read once per process with `output -json`.

    One `tofu output` call answers every question the lifecycle asks (cluster, node group, ECR
    repositories, scheduler), so the result is cached; `refresh=True` re-reads it after an apply.
    """
    raise SystemExit("not implemented yet")


def tf_out(name):
    """One OpenTofu output, or die pointing at `demo.py eks apply` and keeping tofu's reason."""
    raise SystemExit("not implemented yet")


def ecr_login():
    """Log docker in to the account's ECR registry.

    The password comes from `aws ecr get-login-password` and goes to `docker login
    --password-stdin`: it never appears in argv.
    """
    raise SystemExit("not implemented yet")


def ecr_has_image(repository, tag):
    """Whether that repository holds an image with that tag; a missing tag is a plain no."""
    raise SystemExit("not implemented yet")


def ecr_image_digest(repository, tag):
    """The registry manifest digest of a pushed image, for `manifest.published[service]`."""
    raise SystemExit("not implemented yet")


def ng_desired():
    """The node group's current `desiredSize`."""
    raise SystemExit("not implemented yet")


def ng_status():
    """The node group's status: ACTIVE, UPDATING, ..."""
    raise SystemExit("not implemented yet")


def ng_scale(size):
    """Set the node group's desired size and wait until it is ACTIVE again.

    Two rejections to avoid: a scaling request while the group is still UPDATING (the nightly
    schedule just fired) and an update that changes nothing, so both are no-ops here.
    """
    raise SystemExit("not implemented yet")


def scheduler_get():
    """The nightly scale-down schedule as EventBridge Scheduler returns it."""
    raise SystemExit("not implemented yet")


def scheduler_set_state(state):
    """Enable or disable the nightly schedule, preserving everything else about it.

    `update-schedule` replaces the whole schedule, so every field read by `scheduler_get()` is
    fed back and only `State` changes.
    """
    raise SystemExit("not implemented yet")


def purge_state_bucket(bucket):
    """Delete every object version and delete marker, then the bucket itself.

    Versioning is on, so a plain recursive delete leaves the old versions behind and
    `delete-bucket` fails with BucketNotEmpty.
    """
    raise SystemExit("not implemented yet")
