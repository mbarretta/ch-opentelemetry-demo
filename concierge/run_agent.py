import logging
import os

import uvicorn
from dotenv import load_dotenv
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from concierge.telemetry import configure

load_dotenv()
os.environ["USE_VCR"] = "False"
os.environ["MCP_ENABLED"] = str(os.getenv("MCP_ENABLED", "False").lower() == "true")
logging.basicConfig(level=logging.INFO)
provider = configure("agent")

from concierge.agent import ConciergeAgent  # noqa: E402

agent = ConciergeAgent()
app = agent.app
FastAPIInstrumentor.instrument_app(
    app, excluded_urls="healthz,feedback", exclude_spans=["receive", "send"]
)

if __name__ == "__main__":
    try:
        uvicorn.run(
            app,
            host=os.getenv("AGENT_BIND", "127.0.0.1"),
            port=int(os.getenv("AGENT_PORT", "8010")),
        )
    finally:
        provider.shutdown()
