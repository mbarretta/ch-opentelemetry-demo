"""The cluster: kubeconfig, namespaces, Secrets, Helm, and the flagd-ui file exec.

Ported from `deploy/eks/scripts/lib/k8s.sh` and the `kubectl` calls spread through the bash
scripts. Manifests are built in Python and applied on stdin, which is what keeps Secret values
out of argv and off disk.

These are the shapes the lifecycle, flags, ops and check modules are written against, so the
`helm upgrade` argv and the Secret path are asserted here rather than at each call site.
"""

import base64
import json
import os
import shlex
import time

from .. import core
from . import aws, config

# The chart inside `config.HELM_REPO_URL`, and the ceiling on the release's first roll-out: on
# fresh nodes every image is a cold pull, and the demo is some thirty of them.
CHART_NAME = "opentelemetry-demo"
HELM_TIMEOUT = "20m"
# `aws eks wait nodegroup-active` can return before the kubelets have registered, so the nodes
# get their own ten-minute ceiling, and CoreDNS the five minutes the bash allowed it.
NODES_TIMEOUT = 600
COREDNS_TIMEOUT = "300s"
NODE_POLL_SECONDS = 10
# flagd is distroless and has no shell, so both the read and the write of the file it watches go
# through the flagd-ui container beside it, which mounts the same emptyDir.
FLAGD_DEPLOYMENT = "deploy/flagd"
FLAGD_CONTAINER = "flagd-ui"


def kubeconfig():
    """Point kubectl at the demo cluster with `aws eks update-kubeconfig`."""
    cluster = aws.tf_out("cluster_name")
    # The region the cluster was applied in, not whatever .env says: the two differ exactly when
    # someone is about to spend an hour on an empty `kubectl get nodes`.
    args = [
        "aws",
        "eks",
        "update-kubeconfig",
        "--region",
        aws.tf_out("region"),
        "--name",
        cluster,
        "--alias",
        cluster,
    ]
    # The profile is embedded in the kubeconfig's exec-auth entry, so plain `kubectl` works from
    # any shell afterwards. It is absent only when the caller authenticated some other way.
    profile = os.environ.get("AWS_PROFILE", "")
    if profile:
        args += ["--profile", profile]
    core.run(*args, stdout=core.DEVNULL)
    core.run("kubectl", "config", "use-context", cluster, stdout=core.DEVNULL)


def wait_nodes_ready(count=None, timeout=NODES_TIMEOUT):
    """Wait until the scaled-up node group's nodes are Ready, and coredns with them.

    `count` defaults to the node group's full size, which is what `eks up` scaled it to.
    kubectl's stderr is left visible on purpose: an authentication or kubeconfig failure must
    not masquerade as slow node startup for ten minutes.
    """
    if count is None:
        count = int(aws.tf_out("node_count"))
    core.log(f"waiting for {count} Ready node(s)")
    deadline = time.monotonic() + timeout
    while True:
        ready = _ready_nodes()
        if ready >= count:
            break
        if time.monotonic() >= deadline:
            core.die(f"only {ready} of {count} node(s) Ready after {timeout} seconds")
        time.sleep(NODE_POLL_SECONDS)
    core.log(f"{ready} node(s) Ready; waiting for CoreDNS")
    core.run(
        "kubectl",
        "-n",
        "kube-system",
        "rollout",
        "status",
        "deploy/coredns",
        f"--timeout={COREDNS_TIMEOUT}",
    )


def _ready_nodes():
    """How many nodes report exactly `Ready`.

    Exactly: `Ready,SchedulingDisabled` is a cordoned node, which will not take the demo's pods.
    A failed read counts as zero rather than raising, because the node group is still coming up
    for the first minute or so and the API server answers before the nodes do.
    """
    listing = core.capture("kubectl", "get", "nodes", "--no-headers", check=False)
    if listing.returncode:
        return 0
    fields = [line.split() for line in (listing.stdout or "").splitlines()]
    return sum(1 for columns in fields if len(columns) > 1 and columns[1] == "Ready")


def ensure_namespace(namespace):
    """Create a namespace if it is not there, idempotently.

    An applied manifest rather than `create namespace`: apply succeeds whether or not the
    object already exists, which is what makes `eks deploy` re-runnable.
    """
    core.log(f"ensuring namespace {namespace}")
    apply_manifest({"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": namespace}})


def secret_manifest(name, namespace, data):
    """An Opaque Secret manifest with base64-encoded values, as a plain dict.

    Built here and applied by `apply_manifest()` on stdin: no `--from-literal` (argv is
    world-readable through ps) and no rendered file on disk.
    """
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "type": "Opaque",
        "metadata": {"name": name, "namespace": namespace},
        # `data`, not `stringData`: the value is carried base64-encoded and exactly as given,
        # with no trailing newline of the kind a shell pipeline would have added.
        "data": {
            key: base64.b64encode(("" if value is None else str(value)).encode()).decode()
            for key, value in data.items()
        },
    }


def apply_manifest(manifest, namespace=None):
    """Apply one manifest by piping it to `kubectl apply -f -` on stdin."""
    args = ["kubectl"]
    if namespace:
        args += ["-n", namespace]
    args += ["apply", "-f", "-"]
    # JSON, which kubectl reads wherever it reads YAML, and which needs no YAML dependency here.
    core.run(*args, input=json.dumps(manifest), text=True)


def delete_secret(name, namespace):
    """Delete a Secret, tolerating its absence.

    The deploy path deletes rather than skips: a Langfuse Secret left over from an earlier run
    would keep feeding credentials to the collector after they were removed from `.env`.
    """
    core.run("kubectl", "-n", namespace, "delete", "secret", name, "--ignore-not-found")


def helm_repo_ensure():
    """Register the demo chart repository and refresh its index, idempotently.

    `--force-update` makes a re-run a no-op instead of "repository name already exists", and
    repairs the URL when `open-telemetry` is already registered pointing somewhere else.
    """
    core.run(
        "helm",
        "repo",
        "add",
        config.HELM_REPO_NAME,
        config.HELM_REPO_URL,
        "--force-update",
        stdout=core.DEVNULL,
    )
    core.run("helm", "repo", "update", config.HELM_REPO_NAME, stdout=core.DEVNULL)


def helm_upgrade_args(values_files):
    """The full argv for the release's `helm upgrade --install`, values files in order.

    Returned rather than run so the deploy path and `eks check` render from one definition, and
    so a test can assert the argv instead of the cluster state.

    Order is the contract: a later `-f` wins in helm, so the caller passes the static
    `demo-values.yaml` first and the generated values second. There is no `--set` -- every
    value, image overrides included, comes from a file.
    """
    args = [
        "helm",
        "upgrade",
        "--install",
        config.RELEASE,
        f"{config.HELM_REPO_NAME}/{CHART_NAME}",
        "--version",
        config.CHART_VERSION,
        "-n",
        config.NS_DEMO,
    ]
    for path in values_files:
        args += ["-f", str(path)]
    # Helm 4's --wait uses the kstatus watcher, stricter than Helm 3's readiness poll; if it
    # ever stalls on a resource that is in fact fine, `--wait=legacy` restores the old behaviour.
    return args + ["--wait", "--timeout", HELM_TIMEOUT]


def _flagd_exec(*flags):
    """The `kubectl exec` prefix for the flagd-ui container, with any exec flags in place."""
    return [
        "kubectl",
        "-n",
        config.NS_DEMO,
        "exec",
        *flags,
        FLAGD_DEPLOYMENT,
        "-c",
        FLAGD_CONTAINER,
        "--",
    ]


def exec_read(path):
    """Read a file out of the flagd-ui container.

    flagd never reads `cm/flagd-config` directly: an init container copies it once into an
    emptyDir shared with flagd-ui, and flagd fsnotify-watches that copy. The read and write
    therefore go through the pod, and through flagd-ui because the flagd container is
    distroless and has no shell.
    """
    return core.capture(*_flagd_exec(), "cat", path).stdout


def exec_write(path, text):
    """Replace that file through a temporary file and a move, so flagd sees one atomic change.

    `cat > F.tmp && mv F.tmp F` in the container: the inode swap `sed -i` also performs, which
    is what fsnotify reports as a single write rather than a truncate plus a series of appends.
    """
    target = shlex.quote(str(path))
    temporary = shlex.quote(f"{path}.tmp")
    core.run(
        *_flagd_exec("-i"),
        "sh",
        "-c",
        f"cat > {temporary} && mv {temporary} {target}",
        input=text,
        text=True,
    )
