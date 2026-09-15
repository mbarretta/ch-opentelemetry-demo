"""The generated Helm values: everything about the release that `.env` and the build decide.

`deploy/eks/k8s/demo-values.yaml` holds what is true of every deployment. This module holds
what is not: the four image references from the build manifest, the agent and frontend
environment derived from `.env`, and the collector's Langfuse pipeline, which cannot live in a
static file because an `otlphttp` exporter with an unset `${env:LANGFUSE_BASE_URL}` fails
validation and CrashLoops the collector.

The OTTL statements are never written twice: they come from `launcher.collector`, the same
functions the laptop collector configuration uses.
"""

from .. import collector, core, stack
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
# The storefront's two per-deployment settings; the rest of its environment is static.
# PUBLIC_HYPERDX_ENABLED is here rather than in demo-values.yaml because it is a runtime switch
# the operator owns: hardcoded in the static file it recorded every session on every deployment
# whatever `.env` said, so SESSION_REPLAY was inert on this target. It is derived, not read --
# see frontend_env.
FRONTEND_ENV_KEYS = ("ASSISTANT_DEMO_DETAILS", "PUBLIC_HYPERDX_ENABLED")
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

# The exporter the static values give the demo's gateway collector: the hop by which everything
# this release records, browser session replay included, reaches ClickHouse. Its presence is
# this target's answer to "is ClickStack configured", which is what `SESSION_REPLAY=auto` asks.
CLICKSTACK_EXPORTER = "otlphttp/clickstack"


def eks_values(env, published, langfuse):
    """The generated values document: images, env-derived overrides, collector pipelines.

    `published` is `manifest.published` (service -> repository, tag, digest), `langfuse` the
    result of `config.load_langfuse_env()` -- empty when Langfuse is not configured, in which
    case the `traces/langfuse` pipeline exports to `debug` and no exporter references a secret.

    Only `${env:...}` references reach the document; no credential is ever rendered into it.
    `langfuse` is read for its emptiness alone: its four values are already in the
    `langfuse-credentials` Secret, and the collector expands them itself at start-up.

    The frontend's environment is `frontend_env`'s rather than `.env`'s, because one of its two
    keys -- the session-replay switch -- is derived from SESSION_REPLAY instead of read.
    """
    components = image_overrides(published)
    static = static_values()
    static_components = static.get("components") or {}
    for service, keys, source in (
        ("agent", AGENT_ENV_KEYS, env),
        ("frontend", FRONTEND_ENV_KEYS, frontend_env(env, static)),
    ):
        components[service]["envOverrides"] = env_overrides(
            static_components.get(service, {}), source, keys
        )
    return {"components": components, "opentelemetry-collector": collector_values(langfuse)}


def frontend_env(env, static):
    """`env` plus PUBLIC_HYPERDX_ENABLED, the one frontend value that is derived, not read.

    `stack.replay_flag` is the derivation the laptop uses, called here rather than repeated, so
    SESSION_REPLAY means one thing on both targets: `true` records the session, `false` does
    not, and `auto` records whenever ClickStack is configured. Only what "configured" means is
    per-target, and it is read from the release rather than from `.env`: the laptop's collector
    exports to ClickStack when CLICKSTACK_OTLP_ENDPOINT is set, and this one when the static
    values give it the `otlphttp/clickstack` exporter -- which every replay event travels
    through, so replay with no ClickStack behind it would record into nothing.

    A PUBLIC_HYPERDX_ENABLED in `.env` does not win over the derivation, exactly as it does not
    on Compose (`stack.environment` overwrites it too): SESSION_REPLAY is the documented switch.
    """
    decision = {**env, "CLICKSTACK_OTLP_ENDPOINT": clickstack_endpoint(static)}
    return {**env, "PUBLIC_HYPERDX_ENABLED": stack.replay_flag(decision)}


def clickstack_endpoint(static):
    """Where the gateway collector ships everything, from the static values, or `""` if nowhere.

    Read out of the document rather than restated as a constant here: the address itself lives
    in `demo-values.yaml` next to the Service it names, and a second copy would be a second
    thing to keep in step.
    """
    subchart = static.get("opentelemetry-collector") or {}
    exporters = (subchart.get("config") or {}).get("exporters") or {}
    return (exporters.get(CLICKSTACK_EXPORTER) or {}).get("endpoint") or ""


def image_overrides(published):
    """`imageOverride.{repository,tag}` for all four services, from an already-gated manifest.

    `images.require_published()` is the gate, and this function assumes a complete set: a
    repository and a tag for every service in `core.IMAGES`. Both callers supply one and
    neither needs re-checking here. `lifecycle.deploy` runs the gate before it reaches this
    module -- ahead of the first AWS and kubectl call, so the refusal costs nothing -- and it
    guarantees more than this document uses: every field in `images.PUBLISHED_FIELDS`, digest
    included, at the current build tag. `eks check` passes `check.example_images()`, which
    synthesises the two fields an image reference needs rather than a published record.

    Nothing here re-checks any of it. A second check would have to be kept in step with the
    gate's, and the copy that used to live on these lines had already fallen behind: it read
    `repository` and `tag` and never their values, so it could not see the case that matters --
    a complete set published at a tag the build inputs have moved past, which would roll out
    last week's images perfectly happily.

    `pullPolicy: IfNotPresent` is in the static values: the tags are content-derived, so it is
    true of every deployment. All four or none, because a release running two of our images and
    two released ones looks like a broken build rather than a missing `publish` -- which is
    what the gate refuses on.
    """
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
