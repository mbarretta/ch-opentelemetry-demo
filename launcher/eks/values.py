"""The generated Helm values: everything about the release that `.env` and the build decide.

`deploy/eks/k8s/demo-values.yaml` holds what is true of every deployment. This module holds
what is not: the four image references from the build manifest, the agent and frontend
environment derived from `.env`, and the collector's Langfuse pipeline, which cannot live in a
static file because an `otlphttp` exporter with an unset `${env:LANGFUSE_BASE_URL}` fails
validation and CrashLoops the collector.

The OTTL statements are never written twice: they come from `launcher.collector`, the same
functions the laptop collector configuration uses.

The bodies land with the generated-values task.
"""


def eks_values(env, published, langfuse):
    """The generated values document: images, env-derived overrides, collector pipelines.

    `published` is `manifest.published` (service -> repository, tag, digest), `langfuse` the
    result of `config.load_langfuse_env()` -- empty when Langfuse is not configured, in which
    case the `traces/langfuse` pipeline exports to `debug` and no exporter references a secret.

    Only `${env:...}` references reach the document; no credential is ever rendered into it.
    """
    raise SystemExit("not implemented yet")


def values_path():
    """Where the generated document is written: `.runtime/eks/values.generated.yaml`."""
    raise SystemExit("not implemented yet")


def write_values(values):
    """Write the generated document and return its path, for `helm upgrade -f`."""
    raise SystemExit("not implemented yet")
