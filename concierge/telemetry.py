import json
import logging
import os
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

from opentelemetry import baggage, context, trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import SpanProcessor, TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

attributes: ContextVar[dict] = ContextVar("conversation_attributes", default={})
tracer = trace.get_tracer("astronomy.concierge", "0.1.0")
logger = logging.getLogger(__name__)
BAGGAGE_KEYS = {
    "session.id",
    "gen_ai.conversation.id",
    "langfuse.session.id",
    "langfuse.trace.name",
    "langfuse.trace.tags",
    "langfuse.trace.metadata.scenario",
    "langfuse.trace.metadata.mode",
}


def encoded(value):
    return json.dumps(value, default=str, ensure_ascii=False)


def _as_span_value(key, value):
    # Baggage only carries strings (it crosses the wire as a W3C header), but Langfuse
    # expects langfuse.trace.tags as an actual OTel string array attribute.
    if key == "langfuse.trace.tags" and isinstance(value, str):
        return [value]
    return value


class ConversationProcessor(SpanProcessor):
    def on_start(self, span, parent_context=None):
        # Only copy our anonymous demo context, never arbitrary incoming baggage.
        for key in BAGGAGE_KEYS:
            value = baggage.get_baggage(key, context=parent_context)
            if isinstance(value, str) and len(value) <= 128:
                span.set_attribute(key, _as_span_value(key, value))
        for key, value in attributes.get().items():
            span.set_attribute(key, _as_span_value(key, value))


class JsonExporter(SpanExporter):
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, spans):
        try:
            with self.path.open("a") as stream:
                for span in spans:
                    stream.write(span.to_json(indent=None) + "\n")
            return SpanExportResult.SUCCESS
        except OSError:
            logger.exception("Could not write local trace file")
            return SpanExportResult.FAILURE


def configure(service):
    provider = TracerProvider(
        resource=Resource.create(
            {
                "service.name": service,
                "service.namespace": "opentelemetry-demo",
                "service.version": "3.0.0-concierge.1",
            }
        )
    )
    provider.add_span_processor(ConversationProcessor())
    if os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        exporter = OTLPSpanExporter()
    else:
        exporter = JsonExporter(os.getenv("TRACE_FILE", f".runtime/{service}-traces.jsonl"))
    provider.add_span_processor(BatchSpanProcessor(exporter, schedule_delay_millis=500))
    trace.set_tracer_provider(provider)
    HTTPXClientInstrumentor().instrument()
    RequestsInstrumentor().instrument()
    if service == "agent" and os.getenv("MCP_ENABLED", "False").lower() == "true":
        from opentelemetry.instrumentation.mcp import McpInstrumentor

        McpInstrumentor().instrument()
    return provider


@contextmanager
def conversation(session_id, scenario, mode, shop_session_id=None):
    """Stamp conversation identity on every span and propagate it as baggage.

    ``session.id`` is the storefront session (the cart) so agent spans line up with the
    frontend's own session attribute; legacy /prompt conversations use one id for both.
    """
    token = attributes.set(
        {
            "session.id": shop_session_id or session_id,
            "gen_ai.conversation.id": session_id,
            "langfuse.session.id": session_id,
            "langfuse.trace.name": "astronomy-concierge",
            "langfuse.trace.tags": "astronomy-concierge",
            "langfuse.trace.metadata.scenario": scenario,
            "langfuse.trace.metadata.mode": mode,
        }
    )
    ctx = context.get_current()
    for key, value in attributes.get().items():
        ctx = baggage.set_baggage(key, value, context=ctx)
    baggage_token = context.attach(ctx)
    trace.get_current_span().set_attributes(
        {key: _as_span_value(key, value) for key, value in attributes.get().items()}
    )
    try:
        yield
    finally:
        context.detach(baggage_token)
        attributes.reset(token)


def trace_id():
    return format(trace.get_current_span().get_span_context().trace_id, "032x")
