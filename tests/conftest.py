import os

os.environ["USE_VCR"] = "False"
os.environ["MCP_ENABLED"] = "False"
os.environ["AGENT_MODE"] = "scripted"
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import pytest  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,  # noqa: E402
)

from concierge.telemetry import ConversationProcessor  # noqa: E402

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(ConversationProcessor())
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)


@pytest.fixture
def spans():
    exporter.clear()
    yield exporter
