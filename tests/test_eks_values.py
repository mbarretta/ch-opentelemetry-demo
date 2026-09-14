"""launcher.eks.values: the generated half of the Helm release.

The property these tests exist for is the conditional Langfuse exporter: an `otlphttp` exporter
whose `${env:LANGFUSE_BASE_URL}` is unset fails the collector's own configuration validation and
CrashLoops the pod, so the exporter -- and only the exporter -- has to appear and disappear with
the credentials, while the pipeline that carries the filtered spans stays valid either way.
"""

import pytest
import yaml

from launcher import collector, core
from launcher.eks import config, values

# A fully filled-in `.env`: the values the generated document is allowed to render plus the
# secrets it must not. Every secret is marked so a leak is a substring match, not a guess.
FULL_ENV = {
    "AGENT_MODE": "live",
    "MCP_ENABLED": "True",
    "LLM_BASE_URL": "https://api.openai.com/v1",
    "LLM_MODEL": "gpt-4o-mini",
    "LANGFUSE_PROMPT_LABEL": "workshop",
    "LANGFUSE_PROJECT_ID": "cm0project",
    "LANGFUSE_PUBLIC_URL": "https://cloud.langfuse.test",
    "CLICKSTACK_TRACE_URL_TEMPLATE": "https://clickstack.test/search?trace={trace_id}",
    "ASSISTANT_DEMO_DETAILS": "true",
    "LANGFUSE_BASE_URL": "https://cloud.langfuse.test",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-sensitive-public",
    "LANGFUSE_SECRET_KEY": "sk-lf-sensitive-secret",
    "API_KEY": "sensitive-llm-key",
    "CLICKHOUSE_PASSWORD": "sensitive-clickhouse",
    "OTLP_AUTH_TOKEN": "sensitive-otlp-token",
}
# What `demo.py publish` records, in the shape images.PUBLISHED_FIELDS pins.
PUBLISHED = {
    service: {
        "repository": f"111122223333.dkr.ecr.us-east-1.amazonaws.com/otel-demo-eks/{service}",
        "tag": "c0ffee1234",
        "digest": "sha256:" + "d" * 64,
    }
    for service in core.IMAGES
}
# The four keys of the `langfuse-credentials` Secret, values included: the generated document
# gets the real thing at deploy time and must still reference nothing but ${env:...}.
LANGFUSE = config.load_langfuse_env(FULL_ENV)
SECRET_VALUES = (
    FULL_ENV["LANGFUSE_PUBLIC_KEY"],
    FULL_ENV["LANGFUSE_SECRET_KEY"],
    FULL_ENV["API_KEY"],
    FULL_ENV["CLICKHOUSE_PASSWORD"],
    FULL_ENV["OTLP_AUTH_TOKEN"],
    LANGFUSE["LANGFUSE_AUTH_HEADER"],
    # The header without its scheme: the base64 payload is the credential itself.
    LANGFUSE["LANGFUSE_AUTH_HEADER"].split(" ", 1)[1],
)

# Collector components the chart's own configuration defines (chart 0.41.0, the version
# config.CHART_VERSION pins), so a pipeline may reference them without the generated values
# defining them. `demo.py eks check` re-checks this against a real `helm template`; here it
# keeps the unit test honest about what "a valid pipeline" means.
CHART_RECEIVERS = ("otlp",)
CHART_PROCESSORS = (
    "memory_limiter",
    "resourcedetection",
    "resource",
    "transform/sanitize_spans",
    "transform/sanitize_logs",
    "gen_ai_normalizer",
    "filter/sanitize_profiles",
    "batch",
)
CHART_EXPORTERS = ("debug",)


def collector_config(document):
    return document["opentelemetry-collector"]["config"]


def langfuse_pipeline(document):
    return collector_config(document)["service"]["pipelines"]["traces/langfuse"]


def overrides(document, service):
    """The component's envOverrides as a name -> entry mapping."""
    return {entry["name"]: entry for entry in document["components"][service]["envOverrides"]}


def static_values():
    """The committed half of the release, read straight from the file Helm gets first."""
    return yaml.safe_load((config.k8s_dir() / values.STATIC_VALUES).read_text())


@pytest.fixture
def configured():
    return values.eks_values(FULL_ENV, PUBLISHED, LANGFUSE)


@pytest.fixture
def unconfigured():
    """The same deployment with no Langfuse keys in `.env`: load_langfuse_env returns {}."""
    env = {key: value for key, value in FULL_ENV.items() if not key.startswith("LANGFUSE_")}
    return values.eks_values(env, PUBLISHED, {})


def test_all_four_image_references_come_from_the_manifest(configured):
    """`helm upgrade` gets both values files and zero `--set`, so all four land here."""
    for service in ("frontend", "frontend-proxy", "agent", "mcp"):
        override = configured["components"][service]["imageOverride"]
        assert override["repository"] == PUBLISHED[service]["repository"]
        assert override["tag"] == PUBLISHED[service]["tag"]
    assert set(core.IMAGES) == {"frontend", "frontend-proxy", "agent", "mcp"}
    assert "pullPolicy" not in configured["components"]["frontend"]["imageOverride"], (
        "IfNotPresent is static: it is true of every deployment"
    )


def test_a_missing_published_image_refuses_instead_of_emitting_a_partial_set():
    """A partial set would deploy the released image beside ours and look like a bad build."""
    partial = {service: entry for service, entry in PUBLISHED.items() if service != "mcp"}
    partial["frontend-proxy"] = {"repository": PUBLISHED["frontend-proxy"]["repository"]}

    with pytest.raises(SystemExit) as failure:
        values.eks_values(FULL_ENV, partial, LANGFUSE)

    message = str(failure.value)
    assert "mcp" in message, "the service with no entry at all is named"
    assert "frontend-proxy" in message, "an entry without a tag is not a published image"
    assert "frontend" in message and "publish" in message


def test_a_never_published_build_refuses():
    with pytest.raises(SystemExit):
        values.eks_values(FULL_ENV, {}, LANGFUSE)
    with pytest.raises(SystemExit):
        values.eks_values(FULL_ENV, None, LANGFUSE)


def test_the_langfuse_exporter_exists_exactly_when_langfuse_is_configured(
    configured, unconfigured
):
    exporter = collector_config(configured)["exporters"]["otlp_http/langfuse"]
    assert exporter["endpoint"] == "${env:LANGFUSE_BASE_URL}/api/public/otel"
    assert exporter["headers"] == {
        "Authorization": "${env:LANGFUSE_AUTH_HEADER}",
        "x-langfuse-ingestion-version": "4",
    }
    assert langfuse_pipeline(configured)["exporters"] == ["otlp_http/langfuse"]

    assert "otlp_http/langfuse" not in collector_config(unconfigured).get("exporters", {}), (
        "an otlphttp exporter with an unset ${env:...} endpoint CrashLoops the collector"
    )
    assert langfuse_pipeline(unconfigured)["exporters"] == ["debug"], (
        "the pipeline stays, filtering to the log, the way the laptop filters to the preview file"
    )


def test_both_documents_reference_only_collector_components_that_exist(configured, unconfigured):
    """A pipeline naming an undefined processor or exporter fails validation at start-up."""
    static_config = static_values()["opentelemetry-collector"]["config"]
    for document in (configured, unconfigured):
        defined = collector_config(document)
        pipeline = langfuse_pipeline(document)
        for kind, from_chart in (
            ("receivers", CHART_RECEIVERS),
            ("processors", CHART_PROCESSORS),
            ("exporters", CHART_EXPORTERS),
        ):
            available = {
                *from_chart,
                *defined.get(kind, {}),
                *static_config.get(kind, {}),
            }
            missing = [name for name in pipeline[kind] if name not in available]
            assert not missing, f"traces/langfuse {kind} not defined anywhere: {missing}"


def test_no_credential_reaches_the_generated_document(configured):
    """Rendered secrets would sit in `.runtime/` and in every `helm get values` output."""
    dumped = yaml.safe_dump(configured, sort_keys=False)

    for secret in SECRET_VALUES:
        assert secret not in dumped, f"{secret[:12]}... was rendered into the document"
    assert "${env:LANGFUSE_AUTH_HEADER}" in dumped, "the credential travels as a reference"
    assert "sensitive" not in dumped


def test_the_langfuse_pipeline_runs_the_processors_in_the_order_that_works(configured):
    """Routes are set before span names are sanitized, and the filter runs before batching."""
    assert langfuse_pipeline(configured)["processors"] == [
        "memory_limiter",
        "transform/assistant_routes",
        "transform/sanitize_spans",
        "filter/langfuse",
        "gen_ai_normalizer",
        "batch",
    ]
    assert langfuse_pipeline(configured)["receivers"] == ["otlp"]


def test_the_ottl_is_the_laptops_ottl(configured):
    """One definition of the retained set, or the two targets send Langfuse different traces."""
    processors = collector_config(configured)["processors"]
    assert processors["filter/langfuse"] == {
        "error_mode": "ignore",
        "traces": {"span": collector.langfuse_span_filter()},
    }
    assert processors["transform/assistant_routes"] == {
        "error_mode": "ignore",
        "trace_statements": collector.assistant_route_statements(),
    }
    assert processors["transform/assistant_routes"]["trace_statements"], (
        "the static file declares this processor empty; the generated file fills it"
    )


def test_the_agent_and_frontend_env_come_from_the_parsed_dotenv(configured):
    agent = overrides(configured, "agent")
    for key in (
        "AGENT_MODE",
        "MCP_ENABLED",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "LANGFUSE_PROMPT_LABEL",
        "LANGFUSE_PROJECT_ID",
        "LANGFUSE_PUBLIC_URL",
        "CLICKSTACK_TRACE_URL_TEMPLATE",
    ):
        assert agent[key] == {"name": key, "value": FULL_ENV[key]}, key
    assert overrides(configured, "frontend")["ASSISTANT_DEMO_DETAILS"] == {
        "name": "ASSISTANT_DEMO_DETAILS",
        "value": "true",
    }
    for key in ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "API_KEY"):
        assert agent[key].get("value") is None, f"{key} is a secretKeyRef, never a value"


def test_a_silent_dotenv_falls_back_to_the_defaults_the_laptop_uses():
    """The same defaults compose.concierge.yaml applies, because an empty value is not one."""
    agent = overrides(values.eks_values({}, PUBLISHED, {}), "agent")
    assert agent["AGENT_MODE"]["value"] == "scripted"
    # concierge/langfuse_api.py reads this with a "production" default, which "" would win over.
    assert agent["LANGFUSE_PROMPT_LABEL"]["value"] == "production"
    assert agent["LANGFUSE_PROJECT_ID"]["value"] == ""
    assert all(isinstance(entry["value"], str) for entry in agent.values() if "value" in entry)


def test_the_static_component_env_survives_the_generated_values(configured):
    """Helm replaces a list rather than merging it: the second -f file must carry the first's."""
    components = static_values()["components"]
    for service in ("agent", "frontend"):
        static = {entry["name"] for entry in components[service]["envOverrides"]}
        assert static, service
        assert static <= set(overrides(configured, service)), (
            f"{service} lost entries demo-values.yaml sets: helm's later -f wins outright"
        )
    agent = overrides(configured, "agent")
    for key in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "API_KEY"):
        secret = agent[key]["valueFrom"]["secretKeyRef"]
        assert secret["optional"] is True, f"{key} must not block a pod on a Langfuse-less deploy"
        assert secret["key"] == key
    assert agent["OTEL_EXPORTER_OTLP_ENDPOINT"]["value"] == "http://$(OTEL_COLLECTOR_NAME):4318"


def test_the_components_the_static_file_owns_are_left_alone(configured):
    """Only the four image-overridden components appear, so no other static list is replaced."""
    assert set(configured["components"]) == set(core.IMAGES)


def test_the_document_is_written_where_helm_upgrade_reads_it(tmp_path, monkeypatch, configured):
    monkeypatch.setattr(core, "RUNTIME", tmp_path / ".runtime")

    assert values.values_path() == tmp_path / ".runtime/eks/values.generated.yaml"
    written = values.write_values(configured)

    assert written == values.values_path()
    assert yaml.safe_load(written.read_text()) == configured
