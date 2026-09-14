"""The cluster: kubeconfig, namespaces, Secrets, Helm, and the flagd-ui file exec.

Ported from `deploy/eks/scripts/lib/k8s.sh` and the `kubectl` calls spread through the bash
scripts. Manifests are built in Python and applied on stdin, which is what keeps Secret values
out of argv and off disk.

The bodies land with the aws/k8s helper task; the signatures here are the contract the
lifecycle, flags, ops and check modules are written against.
"""


def kubeconfig():
    """Point kubectl at the demo cluster with `aws eks update-kubeconfig`."""
    raise SystemExit("not implemented yet")


def wait_nodes_ready(timeout=300):
    """Wait until the scaled-up node group's nodes are Ready, and coredns with them."""
    raise SystemExit("not implemented yet")


def ensure_namespace(namespace):
    """Create a namespace if it is not there, idempotently.

    `create ... --dry-run=client -o yaml | apply -f -` is the spelling that succeeds whether or
    not the namespace already exists.
    """
    raise SystemExit("not implemented yet")


def secret_manifest(name, namespace, data):
    """An Opaque Secret manifest with base64-encoded values, as a plain dict.

    Built here and applied by `apply_manifest()` on stdin: no `--from-literal` (argv is
    world-readable through ps) and no rendered file on disk.
    """
    raise SystemExit("not implemented yet")


def apply_manifest(manifest, namespace=None):
    """Apply one manifest by piping it to `kubectl apply -f -` on stdin."""
    raise SystemExit("not implemented yet")


def delete_secret(name, namespace):
    """Delete a Secret, tolerating its absence.

    The deploy path deletes rather than skips: a Langfuse Secret left over from an earlier run
    would keep feeding credentials to the collector after they were removed from `.env`.
    """
    raise SystemExit("not implemented yet")


def helm_repo_ensure():
    """Register the demo chart repository and refresh its index, idempotently.

    `--force-update` makes a re-run a no-op instead of "repository name already exists", and
    repairs the URL when `open-telemetry` is already registered pointing somewhere else.
    """
    raise SystemExit("not implemented yet")


def helm_upgrade_args(values_files):
    """The full argv for the release's `helm upgrade --install`, values files in order.

    Returned rather than run so the deploy path and `eks check` render from one definition, and
    so a test can assert the argv instead of the cluster state.
    """
    raise SystemExit("not implemented yet")


def exec_read(path):
    """Read a file out of the flagd-ui container.

    flagd never reads `cm/flagd-config` directly: an init container copies it once into an
    emptyDir shared with flagd-ui, and flagd fsnotify-watches that copy. The read and write
    therefore go through the pod, and through flagd-ui because the flagd container is
    distroless and has no shell.
    """
    raise SystemExit("not implemented yet")


def exec_write(path, text):
    """Replace that file through a temporary file and a move, so flagd sees one atomic change.

    `cat > F.tmp && mv F.tmp F` in the container: the inode swap `sed -i` also performs, which
    is what fsnotify reports as a single write rather than a truncate plus a series of appends.
    """
    raise SystemExit("not implemented yet")
