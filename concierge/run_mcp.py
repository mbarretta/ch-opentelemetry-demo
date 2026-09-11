"""Keep released MCP instrumentation and add conversation baggage attributes."""

import logging

from dotenv import load_dotenv
from opentelemetry import trace
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from traceloop.sdk import Traceloop

from concierge.telemetry import ConversationProcessor

load_dotenv()
logging.basicConfig(level=logging.INFO)
Traceloop.init(app_name="mcp")
provider = trace.get_tracer_provider()
provider.add_span_processor(ConversationProcessor())
HTTPXClientInstrumentor().instrument()

from src.mcp_server.astronomy_shop_mcp_server import AstronomyShopMcp  # noqa: E402

if __name__ == "__main__":
    try:
        AstronomyShopMcp().run()
    finally:
        provider.shutdown()
