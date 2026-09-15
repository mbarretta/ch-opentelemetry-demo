#!/usr/bin/env python3
"""Exercise the running demo and restore its catalog fault setting afterwards.

`--target local`, the default, checks two paths against the running scripted Compose stack: the
legacy ``POST /prompt`` API on the agent's host port, and one native storefront turn through
the proxy under a synthetic browser span, whose whole ancestor chain must reach the Langfuse
preview file.

`--target eks` drives that same native turn through the `demo.py eks tunnel` port-forward and
then asserts the demo's central claim where the cluster puts it: ONE trace id, in ClickHouse
Cloud with the whole storefront-to-agent span set intact, and the same id Langfuse answers for.
The cluster writes no capture file, and the legacy `/prompt` block needs one -- plus the agent's
host port and the laptop's flagd file -- so it is skipped there.
"""

import argparse
import json
import re
import sys
import time
from collections import Counter
from pathlib import Path
from uuid import uuid4

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from launcher import RUNTIME, core, environment, scenario  # noqa: E402
from launcher.cli import TARGETS  # noqa: E402
from launcher.eks import config, ops, tunnel  # noqa: E402

# What the Langfuse pipeline may retain from a storefront turn (see collector.langfuse_span_filter).
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


def smoke_local():
    """The laptop stack: the legacy `/prompt` scenarios, then one native storefront turn."""
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


# --- the EKS target ------------------------------------------------------------------------

# Every service a storefront turn must have spans from on the cluster. The laptop checks its
# service set against the Langfuse preview, which is filtered down to one turn's ancestry; the
# ClickHouse table holds the whole trace, so product-catalog -- the tool call the agent makes --
# belongs in the set here rather than only in the unfiltered capture.
EKS_SERVICES = ("frontend-web", "frontend-proxy", "frontend", "agent", "product-catalog")
# The agent's own span for the turn, and the storefront service whose server span carries the
# route. The route is `ops.ASSISTANT_PATH` (`/api/assistant/message`) and deliberately not
# `collector.ASSISTANT_PATH`, which is the `/api/assistant` prefix the OTTL statements match on.
TURN_SPAN = "concierge.turn"
ROUTE_SERVICE = "frontend"
# The gateway collector batches spans, the ClickStack collector batches again, and ClickHouse
# inserts in parts, so one trace arrives in pieces over several seconds. A minute is the budget
# for the whole span set to be there; the poll returns as soon as it is, so a healthy deployment
# pays a couple of seconds rather than the minute. Langfuse ingests asynchronously too and gets
# the same budget of its own.
EKS_POLL_SECONDS = 60
EKS_POLL_INTERVAL = 2
# Langfuse's read API for one trace, and how long one of those reads may take.
LANGFUSE_TRACE_PATH = "/api/public/traces"
LANGFUSE_TIMEOUT = 30
# A trace id as both backends spell it: 32 hex digits, and nothing else may be interpolated into
# the statement or the URL below.
TRACE_ID = re.compile(r"[0-9a-f]{32}\Z")


def require_trace_id(trace_id):
    """Refuse anything that is not a trace id before it reaches a SQL statement or a URL.

    The id comes back from the storefront inside the cluster rather than from a person, so it
    crosses a trust boundary: it is interpolated into both a ClickHouse statement and a Langfuse
    path, and the query runs as the collector's write user. The one shape it may have is cheap
    to insist on.

    An unconditional refusal rather than an `assert`, because `python -O` and PYTHONOPTIMIZE=1
    compile asserts out, and the guard on the one value this script interpolates that it did not
    produce itself has to survive that. `ops.require_identifier` refuses the database name the
    same way, and `require_assertions` below keeps the checks that are still asserts honest.
    """
    if not TRACE_ID.match(trace_id or ""):
        core.die(f"not a trace id: {trace_id!r}")
    return trace_id


def require_assertions():
    """Refuse to run with asserts compiled out, because this script's gate *is* its asserts.

    Every claim the run makes -- the trace is whole, the scores are what the scenario promises,
    exactly one turn per request, no ancestor dropped -- is an `assert`, so under `python -O` or
    PYTHONOPTIMIZE this would print its progress and exit 0 whatever the demo actually did. A
    green gate with the checks removed is worse than no gate, and converting forty assertions
    into `if` statements would only hide the same problem behind more code, so the run refuses
    the interpreter instead. The guards that must hold even here -- `require_trace_id` and
    `ops.require_identifier`, the two values interpolated into SQL -- refuse unconditionally.
    """
    if not __debug__:
        core.die(
            "smoke.py asserts the demo's claims, so it will not run with asserts compiled out: "
            "re-run without python -O / PYTHONOPTIMIZE"
        )


def until(attempt):
    """Retry `attempt` until it reports nothing wrong, and return its last answer either way.

    `attempt` returns a (value, failures) pair; an empty `failures` ends the poll. The deadline
    is checked after the attempt, so the budget bounds the waiting rather than the number of
    tries, and a backend that answers slowly is still given one full answer.

    The budget is read from the module rather than taken as an argument, so the constant above
    is the only place it is written down -- the failure messages quote the same name, and a
    default argument would have frozen it at import instead.
    """
    deadline = time.monotonic() + EKS_POLL_SECONDS
    while True:
        value, failures = attempt()
        if not failures or time.monotonic() >= deadline:
            return value, failures
        time.sleep(EKS_POLL_INTERVAL)


def eks_span_sql(database, trace_id):
    """The one statement the EKS smoke run issues: every span of one trace, as ClickHouse has it.

    The columns are aliased to the names the capture-file helpers above already use, so
    `orphans()` reads a ClickHouse row and a `telemetry.jsonl` span the same way. The time bound
    is what makes this cheap: `otel_traces` is partitioned by day, the turn happened seconds
    ago, and the window is the one `eks verify` reports over.
    """
    return f"""
    SELECT ServiceName AS service, SpanName AS name, SpanId AS spanId,
           ParentSpanId AS parentSpanId, SpanAttributes['http.route'] AS route
    FROM {database}.otel_traces
    WHERE TraceId = '{require_trace_id(trace_id)}'
          AND Timestamp > now() - INTERVAL {ops.WINDOW}
    """


def eks_spans(client, database, trace_id):
    """The trace's spans out of ClickHouse, one dict per row.

    JSONEachRow rather than the pretty table `eks verify` prints: this is a gate, so the rows
    are read rather than shown, and a malformed answer must fail the run instead of being
    reported and carried past.
    """
    statement = f"{ops.trim(eks_span_sql(database, trace_id))}\nFORMAT JSONEachRow"
    response = client.post("/", content=statement.encode())
    response.raise_for_status()
    return [json.loads(line) for line in response.text.splitlines() if line.strip()]


def eks_trace_failures(spans, parent):
    """Everything wrong with one trace as ClickHouse holds it; empty means the demo's claim holds.

    Failures rather than assertions, because this is the body of a poll: a partial trace is the
    expected answer for the first few seconds and a final answer only at the deadline, and the
    caller needs every reason at once when it gets there. The four properties are the whole
    claim -- the request crossed the browser, the proxy, the storefront, the agent and the
    catalog under one id; the agent took exactly one turn on it; no ancestor was dropped on the
    way; and the storefront span names the route the assistant answers on.
    """
    failures = []
    services = {span["service"] for span in spans}
    missing = [name for name in EKS_SERVICES if name not in services]
    if missing:
        failures.append(f"no spans from {', '.join(missing)} (only {sorted(services)})")
    turns = sum(span["name"] == TURN_SPAN for span in spans)
    if turns != 1:
        failures.append(f"{turns} {TURN_SPAN} spans, expected exactly one")
    stray = orphans(spans)
    if stray:
        failures.append(f"spans whose parent is not in the trace: {stray}")
    if not any(
        span["service"] == ROUTE_SERVICE and span["route"] == ops.ASSISTANT_PATH
        for span in spans
    ):
        failures.append(
            f"no {ROUTE_SERVICE} span carries http.route = {ops.ASSISTANT_PATH}"
        )
    if parent not in {span["spanId"] for span in spans}:
        failures.append("the synthetic browser parent span is not in the trace")
    return failures


def check_eks_trace(client, database, trace_id, parent):
    """Poll ClickHouse until the turn's whole trace is there, or fail saying what is missing.

    A ClickHouse answer that is not a result is one more reason to wait rather than a reason to
    stop: a Cloud service that has idled answers the first query after it wakes with a 503, and
    failing the gate on that would report a broken demo because the database was asleep.
    """
    print(f"polling {database}.otel_traces for {trace_id} (up to {EKS_POLL_SECONDS}s)")

    def attempt():
        try:
            spans = eks_spans(client, database, trace_id)
        except httpx.HTTPError as failure:
            return [], [f"ClickHouse did not answer the query ({failure})"]
        return spans, eks_trace_failures(spans, parent)

    spans, failures = until(attempt)
    assert not failures, (
        f"trace {trace_id} in {database}.otel_traces after {EKS_POLL_SECONDS}s: "
        + "; ".join(failures)
    )
    print("Correlated ClickHouse services:", dict(Counter(s["service"] for s in spans)))
    return spans


def langfuse_trace(client, trace_id):
    """One read of Langfuse's trace API as a (trace, failures) pair, for the poll above.

    A 404 is the expected answer while Langfuse is still ingesting, and any other non-200 --
    or no answer at all -- is worth another attempt too: all of them are reasons to wait rather
    than to stop, and the last one becomes the message if the budget runs out.
    """
    try:
        response = client.get(f"{LANGFUSE_TRACE_PATH}/{trace_id}")
    except httpx.HTTPError as failure:
        return None, [f"Langfuse did not answer ({failure})"]
    if response.status_code == 404:
        return None, ["Langfuse has no trace with that id"]
    if response.status_code != 200:
        return None, [f"Langfuse answered {response.status_code}"]
    trace = response.json()
    if trace.get("id") != trace_id:
        return trace, [f"Langfuse answered with trace {trace.get('id')!r} instead"]
    return trace, []


def check_langfuse_trace(trace_id, langfuse):
    """Assert Langfuse holds the same trace id, when Langfuse is configured at all.

    Skipped and said to be skipped when it is not: Langfuse is optional on both targets (see
    `config.load_langfuse_env`), and a deployment that was never given keys has not failed this
    check, it has opted out of the half of the demo the keys buy.

    The key pair goes to `httpx` as `auth=`, which is the same in-process path
    `ops.clickhouse_client` uses for the ClickHouse password: the credential becomes a request
    header and never a command line.
    """
    if not langfuse:
        print("Langfuse is not configured in .env; skipped the Langfuse half of the trace.")
        return None
    with httpx.Client(
        base_url=langfuse["LANGFUSE_BASE_URL"],
        auth=(langfuse["LANGFUSE_PUBLIC_KEY"], langfuse["LANGFUSE_SECRET_KEY"]),
        timeout=LANGFUSE_TIMEOUT,
    ) as client:
        trace, failures = until(lambda: langfuse_trace(client, trace_id))
    assert not failures, (
        f"trace {trace_id} in Langfuse after {EKS_POLL_SECONDS}s: " + "; ".join(failures)
    )
    print(f"Langfuse holds the same trace: {trace.get('name') or trace_id}")
    return trace


def smoke_eks():
    """One native turn through the tunnel, then that one trace id in both backends.

    This is the demo's claim as a gate rather than as a report: `eks verify` counts what landed
    in the last fifteen minutes and prints it for an audience, while this run drives a turn it
    can name and refuses to pass unless that exact id is complete in ClickHouse Cloud and
    present in Langfuse.

    Nothing here starts a process, so no credential can reach an argv: the two backends are
    read over HTTPS with their secrets in `auth=`, and the cluster is reached through the
    tunnel that `demo.py eks tunnel` already put on loopback.
    """
    clickstack = config.load_clickstack_env()
    database = ops.require_identifier(clickstack["HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE"])
    langfuse = config.load_langfuse_env()
    port = tunnel.resolved_port()
    assert tunnel.probe(port), (
        f"nothing answers on {tunnel.url(port)}: start the port-forward with "
        "`demo.py eks tunnel`, and check the release with `demo.py eks status`"
    )

    native, shop_session, parent = native_turn(tunnel.url(port).rstrip("/"))
    trace_id = require_trace_id(native["trace_id"])
    print(
        f"native: {trace_id} · conversation={native['conversation_id']} · "
        f"shop session={shop_session}"
    )
    with ops.clickhouse_client(clickstack) as client:
        check_eks_trace(client, database, trace_id, parent)
    check_langfuse_trace(trace_id, langfuse)
    (RUNTIME / "smoke-results.json").write_text(json.dumps([native], indent=2))
    print(f"EKS smoke passed: {trace_id} is one trace in ClickHouse and in Langfuse.")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Exercise the running demo: the laptop stack, or the EKS deployment."
    )
    parser.add_argument(
        "--target",
        choices=TARGETS,
        default="local",
        help="which deployment to exercise (default: local)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    require_assertions()
    if args.target == "eks":
        smoke_eks()
    else:
        smoke_local()


if __name__ == "__main__":
    main()
