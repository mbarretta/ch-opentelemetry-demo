import logging

from dotenv import load_dotenv

from concierge.telemetry import configure

load_dotenv()
logging.basicConfig(level=logging.INFO)
provider = configure("chatbot")

from concierge.chatbot import launch  # noqa: E402

if __name__ == "__main__":
    try:
        launch()
    finally:
        provider.shutdown()
