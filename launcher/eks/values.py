"""The generated Helm values: everything about the release that `.env` and the build decide.

`deploy/eks/k8s/demo-values.yaml` holds what is true of every deployment. This module holds
what is not: the four image references from the build manifest, the agent and frontend
environment derived from `.env`, and the collector's Langfuse pipeline, which cannot live in a
static file because an `otlphttp` exporter with an unset `${env:LANGFUSE_BASE_URL}` fails
validation and CrashLoops the collector.

The OTTL statements are never written twice: they come from `launcher.collector`, the same
functions the laptop collector configuration uses.
"""

from .. import collector, core
from . import config

STATIC_VALUES = "demo-values.yaml"

# Agent settings that vary by deployment and hold no credential: the model to call, whether the
# MCP tool server is in the loop, and the three Langfuse and ClickStack values the Demo details
# panel turns into trace links. The credentials next to them in `.env` reach the pod as
# `secretKeyRef`s from demo-values.yaml instead, which is why none of them are listed here.
AGENT_ENV_KEYS = (
    "AGENT_MODE",
    "MCP_ENABLED",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "LANGFUSE_PROMPT_LABEL",
    "LANGFUSE_PROJECT_ID",
    "LANGFUSE_PUBLIC_URL",
    "CLICKSTACK_TRACE_URL_TEMPLATE",
)
# The storefront's one per-deployment setting; the rest of its environment is static.
FRONTEND_ENV_KEYS = ("ASSISTANT_DEMO_DETAILS",)
# Every key above is emitted on every deploy, blank included, because the chart's own defaults
# for three of them are placeholders (`LLM_BASE_URL: https://local-llm.com`, `LLM_MODEL:
# azure/gpt-5.5`): leaving a key out would let the release advertise a model that does not
# exist instead of the nothing that `.env` actually says. The two exceptions are keys whose
# reader has a non-empty default of its own, which a present-but-empty value would win over --
# `concierge/langfuse_api.py` reads LANGFUSE_PROMPT_LABEL as `os.getenv(key, "production")`.
# Both defaults are the ones `compose.concierge.yaml` applies on the laptop.
ENV_DEFAULTS = {"AGENT_MODE": "scripted", "LANGFUSE_PROMPT_LABEL": "production"}

# The Langfuse-bound trace pipeline, its processors in the only order that works: routes are set
# before `transform/sanitize_spans` normalizes span names from them, the filter drops everything
# outside one assistant turn before the batcher, and `batch` is last.
LANGFUSE_PIPELINE = "traces/langfuse"
LANGFUSE_PROCESSORS = (
    "memory_limiter",
    "transform/assistant_routes",
    "transform/sanitize_spans",
    "filter/langfuse",
    "gen_ai_normalizer",
    "batch",
)
LANGFUSE_EXPORTER = "otlp_http/langfuse"
# Where the collector sends what survives the filter when there are no Langfuse credentials. A
# pipeline needs at least one exporter, and `debug` is the one the chart always defines: the
# retained set is still assembled and still inspectable (`kubectl logs`), which is what the
# laptop's `file/langfuse-preview` gives a presenter without credentials.
PREVIEW_EXPORTER = "debug"


def eks_values(env, published, langfuse):
    """The generated values document: images, env-derived overrides, collector pipelines.

    `published` is `manifest.published` (service -> repository, tag, digest), `langfuse` the
    result of `config.load_langfuse_env()` -- empty when Langfuse is not configured, in which
    case the `traces/langfuse` pipeline exports to `debug` and no exporter references a secret.

    Only `${env:...}` references reach the document; no credential is ever rendered into it.
    `langfuse` is read for its emptiness alone: its four values are already in the
    `langfuse-credentials` Secret, and the collector expands them itself at start-up.
    """
    components = image_overrides(published)
    static = static_values().get("components", {})
    for service, keys in (("agent", AGENT_ENV_KEYS), ("frontend", FRONTEND_ENV_KEYS)):
        components[service]["envOverrides"] = env_overrides(static.get(service, {}), env, keys)
    return {"components": components, "opentelemetry-collector": collector_values(langfuse)}


def image_overrides(published):
    """`imageOverride.{repository,tag}` for all four services, or refuse to deploy a partial set.

    `pullPolicy: IfNotPresent` is in the static values: the tags are content-derived, so it is
    true of every deployment. All four or none, because a release running two of our images and
    two released ones looks like a broken build rather than a missing `publish`.
    """
    published = published or {}
    unpublished = [
        service
        for service in core.IMAGES
        if not all((published.get(service) or {}).get(field) for field in ("repository", "tag"))
    ]
    if unpublished:
        core.die(
            f"no published image for {', '.join(unpublished)}: "
            "run `demo.py build` then `demo.py publish` before deploying. "
            f"All of {', '.join(core.IMAGES)} must be in the manifest with a repository and tag."
        )
    return {
        service: {
            "imageOverride": {
                "repository": published[service]["repository"],
                "tag": published[service]["tag"],
            }
        }
        for service in core.IMAGES
    }


def env_overrides(static_component, env, keys):
    """The component's `envOverrides`: what the static file sets, plus the keys from `.env`.

    The static entries are carried through rather than left to Helm. `helm upgrade` gets both
    files, but Helm merges maps and *replaces* lists, so a second `-f` that sets
    `components.agent.envOverrides` discards the first one's list outright -- which would drop
    the agent's collector endpoint and its three optional `langfuse-credentials` refs. Upserting
    by name here is the same rule the chart's own `otel-demo.envOverriden` helper applies.
    """
    derived = [
        {"name": key, "value": str(env.get(key) or ENV_DEFAULTS.get(key, ""))} for key in keys
    ]
    named = {entry["name"] for entry in derived}
    static = static_component.get("envOverrides", [])
    return [entry for entry in static if entry.get("name") not in named] + derived


def static_values():
    """The committed half of the release, read for the lists this document has to carry over."""
    import yaml

    return yaml.safe_load((config.k8s_dir() / STATIC_VALUES).read_text()) or {}


def collector_values(langfuse):
    """The demo's gateway collector: the assistant OTTL, and the Langfuse pipeline if configured.

    The subchart's `kubernetesAttributes` preset only rewrites pipelines named `traces`,
    `metrics`, `logs` or `profiles`, so `traces/langfuse` is left alone -- deliberately: the
    spans it forwards to Langfuse are an LLM trace, not a Kubernetes one.
    """
    exporters = {}
    if langfuse:
        exporters[LANGFUSE_EXPORTER] = {
            "endpoint": "${env:LANGFUSE_BASE_URL}/api/public/otel",
            "headers": {
                "Authorization": "${env:LANGFUSE_AUTH_HEADER}",
                "x-langfuse-ingestion-version": "4",
            },
        }
    pipeline = {
        "receivers": ["otlp"],
        "processors": list(LANGFUSE_PROCESSORS),
        "exporters": [LANGFUSE_EXPORTER] if langfuse else [PREVIEW_EXPORTER],
    }
    document = {
        "processors": {
            # The static file declares this processor with an empty statement list so it renders
            # on its own; the statements are here because they are shared with the laptop.
            "transform/assistant_routes": {
                "error_mode": "ignore",
                "trace_statements": collector.assistant_route_statements(),
            },
            "filter/langfuse": {
                "error_mode": "ignore",
                "traces": {"span": collector.langfuse_span_filter()},
            },
        },
        "service": {"pipelines": {LANGFUSE_PIPELINE: pipeline}},
    }
    if exporters:
        document = {"exporters": exporters, **document}
    return {"config": document}


def values_path():
    """Where the generated document is written: `.runtime/eks/values.generated.yaml`."""
    return config.run_dir() / "values.generated.yaml"


def write_values(values):
    """Write the generated document and return its path, for `helm upgrade -f`."""
    import yaml

    path = values_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(values, sort_keys=False))
    return path
