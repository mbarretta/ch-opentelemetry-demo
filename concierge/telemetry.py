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


class Steps:
    """Numbered ``agent.step`` spans that group one model decision with the tools it triggered.

    A step starts when the model is called and stays open until the next model call (or the end of
    the turn), because the tool node runs *after* the model hook returns. That lifetime cannot be
    a ``with`` block, so this helper owns it. Every parent is passed explicitly: the turn context
    is captured at construction, and ``ctx`` is the open step's context for whoever starts a child
    span, so nothing depends on contextvar inheritance across the graph's task boundaries.
    """

    NAME = "agent.step"

    def __init__(self):
        self.turn = context.get_current()
        self.ctx = self.turn
        self.number = 0
        self.span = None

    def begin(self):
        """End the previous step, start the next one under the turn, and return its context."""
        self.end()
        self.number += 1
        self.span = tracer.start_span(
            self.NAME,
            context=self.turn,
            attributes={
                "langfuse.observation.type": "chain",
                "langfuse.observation.metadata.step": self.number,
            },
        )
        self.ctx = trace.set_span_in_context(self.span, self.turn)
        return self.ctx

    def record(self, finish_reason, tool_names):
        """Note what the model decided at this step: why it stopped and which tools it asked for."""
        if self.span is None:
            return
        final = not tool_names
        self.span.set_attributes(
            {
                "langfuse.observation.metadata.finish_reason": finish_reason,
                "langfuse.observation.metadata.tools_requested": list(tool_names),
                "langfuse.observation.metadata.final": final,
                "langfuse.observation.output": encoded(
                    {
                        "step": self.number,
                        "finish_reason": finish_reason,
                        "tools_requested": list(tool_names),
                        "final": final,
                    }
                ),
            }
        )

    def end(self):
        """Close the open step, if any; safe to call again."""
        span, self.span = self.span, None
        self.ctx = self.turn
        if span is not None:
            span.end()
