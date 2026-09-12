#!/usr/bin/env python3
"""Exercise the running demo and restore its catalog fault setting afterwards.

Two paths are checked against the running scripted stack: the legacy ``POST /prompt`` API on
the agent's host port, and one native storefront turn through the proxy under a synthetic
browser span, whose whole ancestor chain must reach the Langfuse preview file.
"""

import json
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.demo import RUNTIME, environment, scenario  # noqa: E402

# What the Langfuse pipeline may retain from a storefront turn (see demo.langfuse_span_filter).
RETAINED_SERVICES = {"frontend-web", "frontend-proxy", "frontend", "agent", "mcp"}


def spans_in(pattern, trace_id):
    """Spans of one trace across a capture file and its rotated backups."""
    spans = []
    for path in (RUNTIME / "telemetry").glob(pattern):
        with path.open() as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                for resource in record.get("resourceSpans", []):
                    attributes = {
                        a["key"]: a["value"] for a in resource["resource"].get("attributes", [])
                    }
                    service = attributes.get("service.name", {}).get("stringValue", "unknown")
                    for scope in resource.get("scopeSpans", []):
                        for span in scope.get("spans", []):
                            if span.get("traceId") == trace_id:
                                spans.append({**span, "service": service})
    return spans


def captured_spans(trace_id):
    return spans_in("telemetry*.jsonl", trace_id)


def preview_spans(trace_id):
    return spans_in("langfuse-preview*.jsonl", trace_id)


def attribute(span, key):
    for entry in span.get("attributes", []):
        if entry["key"] == key:
            return entry["value"].get("stringValue")
    return None


def orphans(spans):
    ids = {s["spanId"] for s in spans}
    return [
        (s["service"], s["name"], s["parentSpanId"])
        for s in spans
        if s.get("parentSpanId") and s["parentSpanId"] not in ids
    ]


def native_turn(shop_url):
    """One storefront turn under a synthetic browser span, exported the way the browser does.

    The span is created by a tracer provider of this process (resource frontend-web) and sent
    through the proxy's OTLP route, and its context travels to the storefront as traceparent
    and baggage, exactly like the panel's assistant.turn span and the fetch under it.
    """
    from opentelemetry import baggage
    from opentelemetry.baggage.propagation import W3CBaggagePropagator
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.propagators.composite import CompositePropagator
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    provider = TracerProvider(resource=Resource.create({"service.name": "frontend-web"}))
    provider.add_span_processor(
        SimpleSpanProcessor(OTLPSpanExporter(endpoint=f"{shop_url}/otlp-http/v1/traces"))
    )
    propagator = CompositePropagator([TraceContextTextMapPropagator(), W3CBaggagePropagator()])
    shop_session = str(uuid4())
    request = {
        "conversation_id": None,
        "shop_session_id": shop_session,
        "request_id": str(uuid4()),
        "message": "Find a beginner telescope for the moon under $150",
        "currency_code": "USD",
        "budget": 150,
    }
    tracer = provider.get_tracer("smoke.browser")
    try:
        with tracer.start_as_current_span(
            "assistant.turn",
            attributes={"assistant.request_id": request["request_id"], "session.id": shop_session},
        ) as span:
            headers = {}
            propagator.inject(headers, context=baggage.set_baggage("session.id", shop_session))
            response = httpx.post(
                f"{shop_url}/api/assistant/message", json=request, headers=headers, timeout=110
            )
            response.raise_for_status()
            result = response.json()
            span.set_attribute("gen_ai.conversation.id", result["conversation_id"])
            span.set_attribute("langfuse.session.id", result["conversation_id"])
            parent = format(span.get_span_context().span_id, "016x")
            assert format(span.get_span_context().trace_id, "032x") == result["trace_id"], (
                "the agent answered in another trace"
            )
    finally:
        provider.shutdown()
    return result, shop_session, parent


def check_native_trace(result, shop_session, parent):
    """The preview holds the synthetic parent and every ancestor between it and the agent."""
    trace_id = result["trace_id"]
    # The preview and the full capture are separate exporters, flushed by separate batches, so
    # both are polled: the shop-service spans can land after the preview is already complete.
    for _ in range(30):
        preview = preview_spans(trace_id)
        full = captured_spans(trace_id)
        ids = {s["spanId"] for s in preview}
        if (
            parent in ids
            and not orphans(preview)
            and any(s["name"] == "concierge.turn" for s in preview)
            and "product-catalog" in {s["service"] for s in full}
        ):
            break
        time.sleep(1)
    ids = {s["spanId"] for s in preview}
    assert parent in ids, "the synthetic browser parent did not reach the Langfuse preview"
    assert not orphans(preview), orphans(preview)
    assert sum(s["name"] == "concierge.turn" for s in preview) == 1, Counter(
        s["name"] for s in preview
    )
    services = {s["service"] for s in preview}
    assert {"frontend-web", "frontend-proxy", "frontend", "agent"} <= services, services
    assert services <= RETAINED_SERVICES, services - RETAINED_SERVICES
    inbound = [
        s
        for s in preview
        if s["service"] == "frontend"
        and s["kind"] == 2
        and attribute(s, "http.route") == "/api/assistant/message"
    ]
    assert inbound, "no storefront server span for /api/assistant/message with http.route"
    route_span = [s for s in preview if attribute(s, "assistant.contract_version")]
    assert route_span, "no storefront span carries assistant.contract_version"
    for span in route_span:
        assert attribute(span, "gen_ai.conversation.id") == result["conversation_id"]
        assert attribute(span, "langfuse.session.id") == result["conversation_id"]
        assert attribute(span, "session.id") == shop_session
        assert attribute(span, "assistant.request_id") == result["request_id"]
    turn = next(s for s in preview if s["name"] == "concierge.turn")
    assert attribute(turn, "session.id") == shop_session
    assert attribute(turn, "gen_ai.conversation.id") == result["conversation_id"]
    # The unfiltered capture has the same trace plus the shop services the filter drops.
    assert {s["spanId"] for s in preview} <= {s["spanId"] for s in full}
    assert "product-catalog" in {s["service"] for s in full}
    print("Native turn retained for Langfuse:", dict(Counter(s["service"] for s in preview)))


def main():
    env = environment()
    url = f"http://localhost:{env.get('AGENT_HOST_PORT', '8010')}"
    shop_url = f"http://localhost:{env.get('SHOP_PORT', '8080')}"
    reports = []
    with httpx.Client(base_url=url, timeout=100) as client:
        health = client.get("/healthz").json()
        assert health["mode"] == "scripted", (
            "Smoke scenarios require AGENT_MODE=scripted"
        )

        def prompt(message, session, name="shopping"):
            response = client.post(
                "/prompt", json={"session_id": session, "message": message, "scenario": name}
            )
            response.raise_for_status()
            result = response.json()
            reports.append(result)
            print(
                f"{name}: {result['trace_id']} · tools={[t['name'] for t in result['tools']]} · scores={result['scores']}"
            )
            return result

        flags = RUNTIME / "flagd/demo.flagd.json"
        original = flags.read_text()
        try:
            scenario("shopping")
            session = str(uuid4())
            success = prompt("Find a beginner telescope for the moon under $150", session)
            assert success["scores"] == {"budget_adherence": 1}, success
            prompt("Add it to my cart", session)
            own = prompt("Show my cart", session)
            other = prompt("Show my cart", str(uuid4()))
            assert own["tools"][0]["result"]["items"][0]["productId"] == "OLJCESPC7Z"
            assert other["tools"][0]["result"]["items"] == [], other
            bad = prompt("Recommend a telescope under $150", str(uuid4()), "budget-violation")
            assert bad["scores"] == {"budget_adherence": 0}, bad
            scenario("backend-failure")
            for _ in range(10):
                failed = prompt("Look up Explorascope", str(uuid4()), "backend-failure")
                if failed["tools"][0]["result"].get("error"):
                    break
                time.sleep(1)
            else:
                raise AssertionError("Catalog fault did not activate")
            assert "couldn't" in failed["reply"], failed
        finally:
            temp = flags.with_suffix(".restore")
            temp.write_text(original)
            temp.replace(flags)
        expected_services = {"agent", "frontend", "product-catalog"}
        if health["tools_transport"] == "mcp":
            expected_services.add("mcp")
        for _ in range(15):
            spans = captured_spans(success["trace_id"])
            services = {s["service"] for s in spans}
            if expected_services <= services:
                break
            time.sleep(1)
        assert expected_services <= services, services
        assert any(s["name"] == "model.generate" for s in spans)
        assert any(s["name"] == "get_product" for s in spans)
        assert sum(not s.get("parentSpanId") for s in spans) == 1
        assert not orphans(spans), orphans(spans)
        for span in spans:
            attrs = {a["key"]: a["value"] for a in span.get("attributes", [])}
            if span["service"] in {"agent", "mcp"}:
                assert attrs.get("session.id", {}).get("stringValue") == success["session_id"], (
                    span["service"], span["name"], "missing session context"
                )
            if span["name"] == "POST /prompt":
                assert attrs["langfuse.observation.input"]["stringValue"]
                assert attrs["langfuse.observation.output"]["stringValue"] == success["reply"]
        print("Correlated trace services:", dict(Counter(s["service"] for s in spans)))

        native, shop_session, parent = native_turn(shop_url)
        reports.append(native)
        print(f"native: {native['trace_id']} · conversation={native['conversation_id']}")
        check_native_trace(native, shop_session, parent)
        (RUNTIME / "smoke-results.json").write_text(json.dumps(reports, indent=2))
        print("All smoke scenarios passed. Original fault configuration restored.")


if __name__ == "__main__":
    main()
