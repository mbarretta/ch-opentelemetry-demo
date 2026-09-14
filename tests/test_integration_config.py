import json

import pytest
from dotenv import dotenv_values

from launcher import collector, core, stack
from tests.compose_yaml import rendered_environment

BOTH_BACKENDS = {
    "CLICKSTACK_OTLP_ENDPOINT": "https://click.test",
    "CLICKSTACK_API_KEY": "sensitive-click",
    "LANGFUSE_BASE_URL": "https://lf.test",
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "sensitive-lf",
}


def test_both_backends_receive_intended_signals_without_secrets_in_config():
    config = collector.collector_config(BOTH_BACKENDS)
    pipelines = config["service"]["pipelines"]
    for signal in ("traces", "metrics", "logs"):
        assert "otlp_http/clickstack" in pipelines[signal]["exporters"]
    assert set(pipelines["traces/langfuse"]["exporters"]) == {
        "otlp_http/langfuse",
        "file/langfuse-preview",
    }
    assert "sensitive" not in json.dumps(config)


def test_incomplete_langfuse_config_rejected():
    with pytest.raises(SystemExit):
        collector.collector_config({"LANGFUSE_BASE_URL": "https://lf.test"})


def test_capture_mode_needs_no_backends_and_still_previews_the_langfuse_set():
    config = collector.collector_config({})
    pipelines = config["service"]["pipelines"]
    assert "file/capture" in pipelines["traces"]["exporters"]
    assert "otlp_http/langfuse" not in config["exporters"]
    # The filtered set is always written, so the retained spans can be inspected without credentials.
    assert pipelines["traces/langfuse"]["exporters"] == ["file/langfuse-preview"]
    assert config["exporters"]["file/langfuse-preview"]["path"] == collector.LANGFUSE_PREVIEW
    assert collector.LANGFUSE_PREVIEW == "/var/lib/otel/langfuse-preview.jsonl"


def test_langfuse_pipeline_filters_after_route_normalization():
    config = collector.collector_config(BOTH_BACKENDS)
    processors = config["service"]["pipelines"]["traces/langfuse"]["processors"]
    assert processors.index("transform/sanitize_spans") < processors.index("filter/langfuse")
    assert processors.index("filter/langfuse") < processors.index("gen_ai_normalizer")
    assert processors[-1] == "batch"


def test_langfuse_filter_keeps_the_whole_assistant_ancestor_chain():
    config = collector.collector_config({})
    span_filter = config["processors"]["filter/langfuse"]
    assert span_filter["error_mode"] == "ignore"
    conditions = span_filter["traces"]["span"]
    keep = conditions[0]
    assert keep.startswith("not (")
    # Agent-side services keep every span.
    for service in ("agent", "mcp", "chatbot"):
        assert f'resource.attributes["service.name"] == "{service}"' in keep
    # Browser: the turn span by name plus its request id, and the fetch under it by URL.
    assert 'name == "assistant.turn" and attributes["assistant.request_id"] != nil' in keep
    assert 'resource.attributes["service.name"] == "frontend-web"' in keep
    # Proxy: the ingress span by URL and the egress span by the assistant cluster's name.
    assert 'resource.attributes["service.name"] == "frontend-proxy"' in keep
    assert f'attributes["upstream_cluster"] == "{collector.ASSISTANT_CLUSTER}"' in keep
    # Storefront: inbound server spans by route or target, Next's api-route span, the agent call.
    assert 'resource.attributes["service.name"] == "frontend"' in keep
    for attribute in ("http.route", "http.target", "url.path", "next.span_name", "http.url"):
        assert f'IsMatch(attributes["{attribute}"], "/api/assistant")' in keep, attribute
    for attribute in ("url.full", "http.url"):
        assert f'IsMatch(attributes["{attribute}"], "/assistant/(message|actions)")' in keep
    # Unrelated storefront traffic has no keep rule; these are dropped explicitly.
    for route in ("/healthz", "/feedback", collector.AGENT_STATUS_ROUTE):
        assert f'attributes["http.route"] == "{route}"' in conditions[1], route
    assert 'IsMatch(attributes["http.url"], "/api/public/scores")' in conditions[2]


def test_assistant_cluster_name_matches_the_envoy_template():
    template = (core.DOCKER / "envoy.tmpl.yaml").read_text()
    assert f"route: {{ cluster: {collector.ASSISTANT_CLUSTER}, timeout:" in template
    assert f"- name: {collector.ASSISTANT_CLUSTER}\n" in template


def test_transform_sets_http_route_for_assistant_api_routes_before_span_names_are_normalized():
    config = collector.collector_config({})
    statements = config["processors"]["transform/sanitize_spans"]["trace_statements"][0]["statements"]
    ours = [s for s in statements if "/api/assistant" in s]
    assert [s.split('"')[3] for s in ours] == list(collector.ASSISTANT_ROUTES)
    for statement in ours:
        assert statement.startswith('set(span.attributes["http.route"], "/api/assistant/')
        assert 'span.kind == SPAN_KIND_SERVER' in statement
        assert 'resource.attributes["service.name"] == "frontend"' in statement
        assert 'span.attributes["http.route"] == nil' in statement
        assert 'IsMatch(span.attributes["http.target"], "^/api/assistant/' in statement
    normalize = next(i for i, s in enumerate(statements) if s.startswith("set_semconv_span_name("))
    assert all(statements.index(s) < normalize for s in ours)
    # Every released statement is still there, in order.
    released = [s for s in statements if "/api/assistant" not in s]
    assert released[-1].startswith("set_semconv_span_name(")
    assert any("/api/cart" in s for s in released)


def test_fault_targets_only_selected_product(tmp_path, monkeypatch):
    monkeypatch.setattr(core, "RUNTIME", tmp_path)
    (tmp_path / "flagd").mkdir()
    original = json.loads((core.UPSTREAM / "src/flagd/demo.flagd.json").read_text())
    path = tmp_path / "flagd/demo.flagd.json"
    path.write_text(json.dumps(original))
    stack.scenario("backend-failure")
    changed = json.loads(path.read_text())
    fault = changed["flags"]["productCatalogFailure"]
    assert fault["targeting"]["if"][1] == "on"
    assert fault["targeting"]["if"][2] == "off"
    stack.scenario("shopping")
    assert json.loads(path.read_text()) == original


def frontend_environment(variables):
    return rendered_environment("compose.native.yaml", "frontend", variables)


def test_demo_details_are_off_unless_the_operator_opts_in():
    # pages/_document.tsx hands the value to the browser as window.ENV; the overlay's
    # isDemoDetailsEnabled treats anything but 1/true/on/yes as off, so the default renders empty.
    assert frontend_environment({})["ASSISTANT_DEMO_DETAILS"] == ""
    assert frontend_environment({"ASSISTANT_DEMO_DETAILS": ""})["ASSISTANT_DEMO_DETAILS"] == ""
    on = frontend_environment({"ASSISTANT_DEMO_DETAILS": "true"})
    assert on["ASSISTANT_DEMO_DETAILS"] == "true"
    off = frontend_environment({"ASSISTANT_DEMO_DETAILS": "false"})
    assert off["ASSISTANT_DEMO_DETAILS"] == "false"
    # The other assistant settings keep their defaults regardless.
    assert frontend_environment({})["ASSISTANT_TRANSPORT"] == "live"
    assert frontend_environment({})["AGENT_BASE_URL"] == "http://agent:8010"


def test_env_example_documents_demo_details_as_opt_in():
    example = dotenv_values(core.ROOT / ".env.example")
    assert example["ASSISTANT_DEMO_DETAILS"] == "false"
    assert example["ASSISTANT_TRANSPORT"] == "live"
