import os
from uuid import uuid4

import gradio as gr
import requests
from src.chat_interface.chat_interface import ChatAgentUI, get_chat_ui_config

from concierge.telemetry import conversation, tracer

SCENARIOS = ["shopping", "backend-failure", "budget-violation"]
EXAMPLES = {
    "shopping": "Find a beginner telescope for viewing the moon under $150.",
    "backend-failure": "Look up the National Park Foundation Explorascope.",
    "budget-violation": "Recommend a beginner telescope under $150.",
}


class ConciergeUI(ChatAgentUI):
    def respond(self, message, history, session_id, scenario, budget):
        if not (message or "").strip():
            return "", history, session_id, "", "", {}
        session_id = session_id or str(uuid4())
        history = list(history or [])
        mode = os.getenv("AGENT_MODE", "scripted")
        payload = {
            "session_id": session_id,
            "message": message,
            "scenario": scenario,
            "budget_usd": budget,
        }
        try:
            with conversation(session_id, scenario, mode):
                with tracer.start_as_current_span(
                    "chat.turn",
                    attributes={
                        "langfuse.observation.type": "chain",
                        "langfuse.observation.input": message,
                    },
                ) as span:
                    response = requests.post(self.config.agentBaseUrl, json=payload, timeout=100)
                    response.raise_for_status()
                    data = response.json()
                    span.set_attribute("langfuse.observation.output", data["reply"])
            history.extend(
                [
                    {"role": "user", "content": message},
                    {"role": "assistant", "content": data["reply"]},
                ]
            )
            links = " · ".join(f"[{name}]({url})" for name, url in data["links"].items())
            details = f"Trace: `{data['trace_id']}`  \nSession: `{session_id}`"
            if links:
                details += f"  \n{links}"
            return (
                "",
                history,
                session_id,
                data["trace_id"],
                details,
                {
                    "tools": data["tools"],
                    "scores": data["scores"],
                    "mode": data["mode"],
                    "prompt": {"source": data["prompt_source"], "version": data["prompt_version"]},
                },
            )
        except requests.RequestException as exc:
            detail = "The agent is unavailable. Check its logs, then try again."
            if exc.response is not None:
                try:
                    detail = str(exc.response.json().get("detail", detail))
                except ValueError:
                    pass
            raise gr.Error(detail) from None

    def feedback(self, session_id, current_trace, value):
        if not current_trace:
            return "Send a message before rating a response."
        try:
            response = requests.post(
                self.config.agentBaseUrl.rsplit("/", 1)[0] + "/feedback",
                json={"session_id": session_id, "trace_id": current_trace, "value": value},
                timeout=10,
            )
            if response.ok:
                return "Feedback saved to Langfuse."
            return str(response.json().get("detail", "Feedback could not be saved."))
        except requests.RequestException, ValueError:
            return "Feedback could not be saved. Please try again."

    def build(self):
        mode = os.getenv("AGENT_MODE", "scripted")
        with gr.Blocks(title="Astronomy Shop Concierge", analytics_enabled=False) as ui:
            session_id = gr.State("")
            current_trace = gr.State("")
            gr.Markdown(
                "# Astronomy Shop Concierge\nChoose a telescope, compare products, and prepare a cart."
            )
            if mode == "scripted":
                gr.Markdown(
                    "**Scripted demo** · Real shop tools, deterministic model responses. Use the sample request, then ask to add it or show your cart. Token usage and model cost are not simulated."
                )
            else:
                gr.Markdown(
                    "**Live assistant** · Ask a shopping question and refine the recommendation."
                )
            with gr.Row():
                scenario = gr.Dropdown(
                    SCENARIOS,
                    value="shopping",
                    label="Demo scenario",
                    interactive=mode == "scripted",
                )
                budget = gr.Number(value=150, minimum=1, maximum=100000, label="Budget (USD)")
                reset = gr.Button("New conversation")
            chatbot = gr.Chatbot(height=400)
            message = gr.Textbox(label="Message", placeholder="Help me find a beginner telescope…")
            with gr.Row():
                send = gr.Button("Send", variant="primary")
                sample = gr.Button("Use sample request")
                add = gr.Button("Add it to my cart")
                cart = gr.Button("Show my cart")
            details = gr.Markdown()
            with gr.Row():
                good = gr.Button("Helpful")
                bad = gr.Button("Not helpful")
                feedback_status = gr.Markdown()
            with gr.Accordion("Demo inspector", open=False):
                inspector = gr.JSON(label="Tools, evaluation, and prompt")
            inputs = [message, chatbot, session_id, scenario, budget]
            outputs = [message, chatbot, session_id, current_trace, details, inspector]
            send.click(self.respond, inputs, outputs, concurrency_limit=8, concurrency_id="chat")
            message.submit(
                self.respond, inputs, outputs, concurrency_limit=8, concurrency_id="chat"
            )
            sample.click(lambda s: EXAMPLES[s], [scenario], [message])

            def add_item(history, session, scenario, budget):
                return self.respond("Add it to my cart", history, session, scenario, budget)

            def show_cart(history, session, scenario, budget):
                return self.respond("Show my cart", history, session, scenario, budget)

            add.click(add_item, inputs[1:], outputs, concurrency_limit=8, concurrency_id="chat")
            cart.click(show_cart, inputs[1:], outputs, concurrency_limit=8, concurrency_id="chat")
            for event in [reset.click, scenario.change, budget.change, chatbot.clear]:
                event(
                    lambda: ([], "", "", "", {}, ""),
                    outputs=[
                        chatbot,
                        session_id,
                        current_trace,
                        details,
                        inspector,
                        feedback_status,
                    ],
                )
            good.click(
                lambda s, t: self.feedback(s, t, 1), [session_id, current_trace], feedback_status
            )
            bad.click(
                lambda s, t: self.feedback(s, t, 0), [session_id, current_trace], feedback_status
            )
        return ui

    def launch(self, agent_config=None):
        self.build().queue().launch(
            server_name=self.config.uiBaseUrl,
            server_port=self.config.uiPort,
            root_path=self.config.rootPath,
        )


def launch():
    ConciergeUI(get_chat_ui_config()).launch()
