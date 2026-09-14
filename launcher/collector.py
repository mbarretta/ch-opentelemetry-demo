"""The laptop collector configuration: the capture file and the Langfuse trace pipeline."""

from . import core

# What the Langfuse-bound trace pipeline keeps: one storefront assistant turn from the browser's
# assistant.turn span down through the proxy, the storefront's API route, and the agent, and the
# legacy chat services. Everything else the storefront does is dropped (see langfuse_span_filter).
LANGFUSE_SERVICES = ("agent", "chatbot", "mcp")
ASSISTANT_PATH = "/api/assistant"
# The agent endpoints the storefront's API routes call for a turn. The conversation-status
# route is not a turn: its agent span is dropped below so it never arrives without its parent.
AGENT_TURN_ENDPOINTS = "/assistant/(message|actions)"
AGENT_STATUS_ROUTE = "/assistant/conversations/{conversation_id}"
# The name docker/envoy.tmpl.yaml gives the storefront cluster on the /api/assistant/ route; the
# egress span Envoy emits per request carries the cluster name and no URL.
ASSISTANT_CLUSTER = "frontend-assistant"
# The storefront's assistant API routes (pages/api/assistant/); Next.js sets http.route for them,
# and the collector fills it from the request target when a version leaves it empty.
ASSISTANT_ROUTES = (
    f"{ASSISTANT_PATH}/message",
    f"{ASSISTANT_PATH}/action",
    f"{ASSISTANT_PATH}/feedback",
    f"{ASSISTANT_PATH}/conversation/{{conversationId}}",
)
LANGFUSE_PREVIEW = "/var/lib/otel/langfuse-preview.jsonl"


def service_is(name):
    return f'resource.attributes["service.name"] == "{name}"'


def attribute_matches(attribute, pattern):
    """OTTL: the span attribute exists and matches the regular expression."""
    return f'IsMatch(attributes["{attribute}"], "{pattern}")'


def langfuse_span_filter():
    """OTTL span conditions for the Langfuse pipeline's filter processor; a match drops the span.

    The first condition is the keep set, negated. It spells out every ancestor of the agent's
    spans in a storefront turn, because a dropped ancestor leaves the spans below it orphaned:
    the browser's assistant.turn span and the fetch under it (frontend-web), Envoy's ingress
    span for the route and its egress span to the assistant cluster (frontend-proxy), and the
    storefront's inbound server spans, Next's api-route span (which carries the path only in
    next.span_name), and the outbound call to the agent (frontend). Agent, mcp, and the debug
    chatbot keep every span except health and conversation-status checks and the feedback-score
    plumbing, none of which is part of a turn.
    """
    keep = [
        " or ".join(service_is(name) for name in LANGFUSE_SERVICES),
        f'{service_is("frontend-web")} and ('
        f'(name == "assistant.turn" and attributes["assistant.request_id"] != nil) or '
        f'{attribute_matches("http.url", ASSISTANT_PATH)})',
        f'{service_is("frontend-proxy")} and ({attribute_matches("http.url", ASSISTANT_PATH)} or '
        f'attributes["upstream_cluster"] == "{ASSISTANT_CLUSTER}")',
        f'{service_is("frontend")} and ('
        + " or ".join(
            attribute_matches(attribute, ASSISTANT_PATH)
            for attribute in ("http.route", "http.target", "url.path", "next.span_name")
        )
        + f' or {attribute_matches("url.full", AGENT_TURN_ENDPOINTS)}'
        f' or {attribute_matches("http.url", AGENT_TURN_ENDPOINTS)})',
    ]
    return [
        "not (" + " or ".join(f"({condition})" for condition in keep) + ")",
        " or ".join(
            f'attributes["http.route"] == "{route}"'
            for route in ("/healthz", "/feedback", AGENT_STATUS_ROUTE)
        ),
        attribute_matches("http.url", "/api/public/scores"),
    ]


def assistant_route_statements():
    """OTTL statements that set http.route on the storefront's assistant API server spans.

    Same shape as the released transform/sanitize_spans workaround for Next.js API routes: only
    when the span has no http.route, matched on the request target, before span names are
    normalized from it.
    """
    statements = []
    for route in ASSISTANT_ROUTES:
        prefix = route.split("{", 1)[0]
        statements.append(
            f'set(span.attributes["http.route"], "{route}") where span.kind == SPAN_KIND_SERVER'
            f' and resource.attributes["service.name"] == "frontend"'
            f' and span.attributes["http.route"] == nil'
            f' and IsMatch(span.attributes["http.target"], "^{prefix}")'
        )
    return statements


def collector_config(env):
    import yaml

    config = yaml.safe_load((core.UPSTREAM / "src/otel-collector/otelcol-config.yml").read_text())
    exporters = config["exporters"]
    processors = config["processors"]
    exporters["file/capture"] = {
        "path": "/var/lib/otel/telemetry.jsonl",
        "rotation": {"max_megabytes": 20, "max_backups": 3},
    }
    processors["batch"] = {"timeout": "1s", "send_batch_size": 1024}
    pipelines = config["service"]["pipelines"]
    for signal in ("traces", "metrics", "logs"):
        pipelines[signal]["processors"].append("batch")
        pipelines[signal]["exporters"] = ["file/capture"]
    pipelines["traces"]["exporters"].append("span_metrics")
    # http.route for the assistant routes goes in ahead of the released span-name normalization.
    statements = processors["transform/sanitize_spans"]["trace_statements"][0]["statements"]
    normalize = next(
        (i for i, s in enumerate(statements) if s.startswith("set_semconv_span_name(")),
        len(statements),
    )
    statements[normalize:normalize] = assistant_route_statements()
    if env.get("CLICKSTACK_OTLP_ENDPOINT"):
        exporters["otlp_http/clickstack"] = {
            "endpoint": "${env:CLICKSTACK_OTLP_ENDPOINT}",
            "headers": {"authorization": "${env:CLICKSTACK_API_KEY}"},
        }
        for signal in ("traces", "metrics", "logs"):
            pipelines[signal]["exporters"].append("otlp_http/clickstack")
    # The Langfuse pipeline always runs: its filtered result goes to the preview file so the
    # retained set can be inspected (and smoke-tested) with or without Langfuse credentials.
    exporters["file/langfuse-preview"] = {
        "path": LANGFUSE_PREVIEW,
        "rotation": {"max_megabytes": 20, "max_backups": 3},
    }
    processors["filter/langfuse"] = {
        "error_mode": "ignore",
        "traces": {"span": langfuse_span_filter()},
    }
    pipelines["traces/langfuse"] = {
        "receivers": ["otlp"],
        "processors": [
            "memory_limiter",
            "transform/sanitize_spans",
            "filter/langfuse",
            "gen_ai_normalizer",
            "batch",
        ],
        "exporters": ["file/langfuse-preview"],
    }
    lf = [
        bool(env.get(key))
        for key in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    ]
    if any(lf) and not all(lf):
        raise SystemExit(
            "Set all three LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY, and LANGFUSE_SECRET_KEY."
        )
    if all(lf):
        exporters["otlp_http/langfuse"] = {
            "endpoint": "${env:LANGFUSE_BASE_URL}/api/public/otel",
            "headers": {
                "Authorization": "${env:LANGFUSE_AUTH_HEADER}",
                "x-langfuse-ingestion-version": "4",
            },
        }
        pipelines["traces/langfuse"]["exporters"].append("otlp_http/langfuse")
    return config
