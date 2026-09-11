import asyncio
import logging
import os
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Annotated, Literal
from urllib.parse import quote
from uuid import UUID, uuid4

import httpx
from fastapi import FastAPI, HTTPException
from langchain.agents import create_agent
from langchain.agents.middleware import wrap_model_call
from langchain.tools import tool
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.utils.function_calling import convert_to_openai_tool
from opentelemetry import trace
from opentelemetry.trace import SpanKind, Status, StatusCode
from pydantic import BaseModel, Field
from src.agents.agents import Agent
from src.agents.llm import ChatLLM

from concierge.contract import (
    CONTRACT_VERSION,
    IDLE_SECONDS,
    MAX_TURNS,
    AddToCartAction,
    AssistantResponse,
    ConversationStatus,
    DemoDetails,
    MessageRequest,
    Scenario,
    cart_changed,
    demo_tools,
    product_refs,
)
from concierge.langfuse_api import PROMPT_NAME, LangfuseAPI
from concierge.scripted_model import ScriptedModel, price, tool_data
from concierge.telemetry import conversation, encoded, trace_id, tracer

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    session_id: UUID
    message: str = Field(min_length=1, max_length=4000)
    scenario: Scenario = "shopping"
    budget_usd: float = Field(default=150, gt=0, le=100000, allow_inf_nan=False)


class FeedbackRequest(BaseModel):
    session_id: UUID
    trace_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    value: Literal[0, 1]
    comment: str = Field(default="", max_length=1000)


@dataclass
class Session:
    """One conversation. ``shop_session_id`` is the cart it is bound to for its whole life."""

    scenario: str
    budget: float
    shop_session_id: str
    currency_code: str = "USD"
    messages: list = field(default_factory=list)
    traces: set = field(default_factory=set)
    replies: dict = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    touched: float = field(default_factory=time.monotonic)
    turns: int = 0

    def expires_at(self):
        remaining = IDLE_SECONDS - (time.monotonic() - self.touched)
        return datetime.now(UTC) + timedelta(seconds=max(remaining, 0))


class ConciergeAgent(Agent):
    def __init__(self):
        super().__init__()
        self.sessions = {}
        self.langfuse = LangfuseAPI()
        self.mode = os.getenv("AGENT_MODE", "scripted")
        if self.mode not in {"scripted", "live"}:
            raise ValueError("AGENT_MODE must be scripted or live")
        if self.mode == "live" and not os.getenv("API_KEY"):
            raise ValueError("Set API_KEY before starting AGENT_MODE=live")
        self.app = FastAPI(title="Astronomy Concierge", lifespan=self.lifespan)
        self.app.post("/prompt")(self.handle_prompt)
        self.app.post("/feedback")(self.feedback)
        self.app.get("/healthz")(self.health)
        self.app.post("/assistant/message")(self.assistant_message)
        self.app.post("/assistant/actions/add-to-cart")(self.assistant_add_to_cart)
        self.app.get("/assistant/conversations/{conversation_id}")(self.assistant_conversation)

    @asynccontextmanager
    async def lifespan(self, app):
        async with super().lifespan(app):
            yield
        trace.get_tracer_provider().force_flush()

    async def health(self):
        return {
            "status": "ok",
            "mode": self.mode,
            "contract_version": CONTRACT_VERSION,
            "langfuse_configured": self.langfuse.enabled,
            "tools_transport": "mcp" if self.mcp_server else "http",
        }

    # -- conversation store -----------------------------------------------------------------

    def expire(self):
        now = time.monotonic()
        expired = [
            key
            for key, state in self.sessions.items()
            if now - state.touched > IDLE_SECONDS and not state.lock.locked()
        ]
        for key in expired:
            del self.sessions[key]

    def create(self, key, scenario, budget, shop_session_id, currency_code="USD"):
        if len(self.sessions) >= 256:
            raise HTTPException(503, "Conversation capacity reached. Try again later.")
        self.sessions[key] = Session(scenario, budget, shop_session_id, currency_code)
        return self.sessions[key]

    def session(self, request):
        """Legacy /prompt lookup: the conversation UUID doubles as the cart id."""
        self.expire()
        key = str(request.session_id)
        state = self.sessions.get(key) or self.create(
            key, request.scenario, request.budget_usd, shop_session_id=key
        )
        if state.scenario != request.scenario or state.budget != request.budget_usd:
            raise HTTPException(409, "Start a new conversation to change scenario or budget.")
        state.touched = time.monotonic()
        return state

    def conversation(self, conversation_id):
        self.expire()
        state = self.sessions.get(str(conversation_id))
        if state is None:
            raise HTTPException(404, "Conversation not found or expired. Start a new one.")
        return state

    def stored_or_ready(self, state, request_id):
        """Return the stored reply for a repeated request id, or None once a turn may start."""
        stored = state.replies.get(str(request_id))
        if stored is not None:
            return stored
        if state.lock.locked():
            raise HTTPException(409, "A turn is already in flight for this conversation.")
        if state.turns >= MAX_TURNS:
            raise HTTPException(
                409, f"This conversation reached {MAX_TURNS} turns. Start a new one."
            )
        return None

    # -- tools -----------------------------------------------------------------------------

    async def scoped_tools(self, user_id, calls, currency_code="USD"):
        """Model-visible tools.

        The cart identity and currency are injected here, never by the model, and the same
        wrappers front both the HTTP and the MCP tool transports.
        """
        upstream = {t.name: t for t in await self.get_tool_list()}

        async def invoke(name, args):
            with tracer.start_as_current_span(
                name,
                attributes={
                    "langfuse.observation.type": "tool",
                    "langfuse.observation.input": encoded(args),
                    "gen_ai.tool.name": name,
                    "gen_ai.operation.name": "execute_tool",
                    "gen_ai.tool.call.arguments": encoded(args),
                },
            ) as span:
                try:
                    result = tool_data(await upstream[name].ainvoke(args))
                    if isinstance(result, str) and result.lower().startswith(
                        ("error", "checkout failed")
                    ):
                        result = {"error": result}
                except Exception as exc:
                    span.record_exception(exc)
                    result = {"error": f"{name} could not reach the shop"}
                if isinstance(result, dict) and result.get("error"):
                    span.set_attribute("error.type", "shop_tool_error")
                    span.set_status(Status(StatusCode.ERROR, str(result["error"])[:300]))
                    span.set_attribute("langfuse.observation.level", "ERROR")
                span.set_attribute("langfuse.observation.output", encoded(result))
                span.set_attribute("gen_ai.tool.call.result", encoded(result))
                calls.append({"name": name, "arguments": args, "result": result})
                return result

        @tool
        async def list_products():
            """List the Astronomy Shop catalog with prices in the shopper's currency."""
            return await invoke("list_products", {"currency_code": currency_code})

        @tool
        async def get_product(product_id: str):
            """Look up one catalog product by ID before recommending it."""
            return await invoke(
                "get_product", {"product_id": product_id, "currency_code": currency_code}
            )

        @tool
        async def get_cart():
            """Read the shopper's cart."""
            return await invoke("get_cart", {"user_id": user_id, "currency_code": currency_code})

        @tool
        async def add_to_cart(product_id: str, quantity: Annotated[int, Field(ge=1, le=10)] = 1):
            """Add an item to the shopper's cart, only when the shopper requests it."""
            return await invoke(
                "add_to_cart",
                {"user_id": user_id, "product_id": product_id, "quantity": quantity},
            )

        return [list_products, get_product, get_cart, add_to_cart]

    # -- legacy endpoint -------------------------------------------------------------------

    async def handle_prompt(self, request: ChatRequest):
        if not request.message.strip():
            raise HTTPException(422, "Enter a message.")
        state = self.session(request)
        async with state.lock:
            if state.turns >= MAX_TURNS:
                raise HTTPException(
                    409, f"This conversation reached {MAX_TURNS} turns. Start a new one."
                )
            key = str(request.session_id)
            result = await self.execute_turn(key, state, request.message)
            return {
                "reply": result["reply"],
                "response": {"messages": [{"content": result["reply"]}]},
                "tools": result["calls"],
                "scores": result["scores"],
                "prompt_version": result["prompt_version"],
                "prompt_source": result["prompt_source"],
                "trace_id": result["trace_id"],
                "session_id": key,
                "mode": self.mode,
                "links": self.links(result["trace_id"]),
            }

    # -- storefront contract ---------------------------------------------------------------

    async def assistant_message(self, request: MessageRequest):
        if not request.message.strip():
            raise HTTPException(422, "Enter a message.")
        shop_session_id = str(request.shop_session_id)
        if request.conversation_id is None:
            self.expire()
            key = str(uuid4())
            state = self.create(
                key,
                request.scenario or "shopping",
                request.budget if request.budget is not None else 150,
                shop_session_id,
                request.currency_code,
            )
        else:
            key = str(request.conversation_id)
            state = self.conversation(key)
            if state.shop_session_id != shop_session_id:
                raise HTTPException(
                    409,
                    "This conversation belongs to a different storefront session. Start a new one.",
                )
            if (request.scenario is not None and request.scenario != state.scenario) or (
                request.budget is not None and request.budget != state.budget
            ):
                raise HTTPException(409, "Start a new conversation to change scenario or budget.")
        stored = self.stored_or_ready(state, request.request_id)
        if stored is not None:
            return stored
        async with state.lock:
            state.currency_code = request.currency_code
            product_id = request.product_context.product_id if request.product_context else None
            result = await self.execute_turn(
                key,
                state,
                request.message,
                request_id=str(request.request_id),
                product_id=product_id,
            )
            return self.remember(state, request.request_id, result, key)

    async def assistant_add_to_cart(self, request: AddToCartAction):
        key = str(request.conversation_id)
        state = self.conversation(key)
        stored = self.stored_or_ready(state, request.request_id)
        if stored is not None:
            return stored
        async with state.lock:
            state.currency_code = request.currency_code
            calls = []
            with conversation(key, state.scenario, self.mode, state.shop_session_id):
                request_span = trace.get_current_span()
                with tracer.start_as_current_span(
                    "concierge.action",
                    attributes={
                        "langfuse.observation.type": "chain",
                        "assistant.request_id": str(request.request_id),
                        "langfuse.trace.metadata.currency_code": request.currency_code,
                        "langfuse.observation.input": encoded(
                            {"action": "add_to_cart", **request.model_dump(mode="json")}
                        ),
                    },
                ) as span:
                    current_trace = trace_id()
                    add_to_cart = next(
                        t
                        for t in await self.scoped_tools(
                            state.shop_session_id, calls, request.currency_code
                        )
                        if t.name == "add_to_cart"
                    )
                    try:
                        async with asyncio.timeout(30):
                            await add_to_cart.ainvoke(
                                {"product_id": request.product_id, "quantity": request.quantity}
                            )
                    except Exception:
                        logger.exception("Cart action failed; trace_id=%s", current_trace)
                        calls.append(
                            {
                                "name": "add_to_cart",
                                "arguments": {"product_id": request.product_id},
                                "result": {"error": "add_to_cart did not complete"},
                            }
                        )
                    if not cart_changed(calls):
                        span.set_status(Status(StatusCode.ERROR, "Cart action failed"))
                        raise HTTPException(
                            502,
                            {
                                "message": "The shop could not add that item. Check your cart before retrying.",
                                "trace_id": current_trace,
                            },
                        )
                    plural = "item" if request.quantity == 1 else "items"
                    reply = f"Added {request.quantity} {plural} to your cart."
                    span.set_attribute("langfuse.observation.output", reply)
                    request_span.set_attribute("langfuse.observation.output", reply)
                    state.messages.extend(
                        [
                            HumanMessage(
                                content=f"Add {request.quantity} of product {request.product_id} to my cart."
                            ),
                            AIMessage(content=reply),
                        ]
                    )
                    prompt = await self.langfuse.prompt()
                    result = {
                        "reply": reply,
                        "calls": calls,
                        "scores": {},
                        "trace_id": current_trace,
                        "prompt_version": prompt.version,
                        "prompt_source": "langfuse" if prompt.managed else "bundled",
                    }
                    state.traces.add(current_trace)
                    state.turns += 1
                    state.touched = time.monotonic()
            return self.remember(state, request.request_id, result, key)

    async def assistant_conversation(self, conversation_id: UUID):
        state = self.conversation(conversation_id)
        return ConversationStatus(
            conversation_id=str(conversation_id),
            shop_session_id=state.shop_session_id,
            turns=state.turns,
            currency_code=state.currency_code,
            expires_at=state.expires_at(),
        )

    def remember(self, state, request_id, result, conversation_id):
        response = AssistantResponse(
            conversation_id=conversation_id,
            request_id=str(request_id),
            reply=result["reply"],
            product_refs=product_refs(result["calls"]),
            cart_changed=cart_changed(result["calls"]),
            trace_id=result["trace_id"],
            feedback_enabled=self.langfuse.enabled,
            demo=DemoDetails(
                mode=self.mode,
                scenario=state.scenario,
                prompt_version=result["prompt_version"],
                prompt_source=result["prompt_source"],
                tools=demo_tools(result["calls"]),
                links=self.links(result["trace_id"]),
            ),
        )
        state.replies[str(request_id)] = response
        return response

    # -- one traced turn -------------------------------------------------------------------

    async def execute_turn(self, key, state, message, request_id=None, product_id=None):
        """Run one agent turn for a locked conversation inside its trace context."""
        with conversation(key, state.scenario, self.mode, state.shop_session_id):
            request_span = trace.get_current_span()
            request_span.set_attribute("langfuse.observation.input", message)
            attributes = {
                "langfuse.observation.type": "agent",
                "gen_ai.operation.name": "invoke_agent",
                "gen_ai.agent.name": "astronomy-concierge",
                "langfuse.observation.input": encoded({"message": message}),
                "langfuse.trace.metadata.budget": state.budget,
                "langfuse.trace.metadata.currency_code": state.currency_code,
            }
            if request_id:
                attributes["assistant.request_id"] = request_id
            with tracer.start_as_current_span("concierge.turn", attributes=attributes) as span:
                current_trace = trace_id()
                try:
                    async with asyncio.timeout(90):
                        result = await self.run_turn(state, message, product_id)
                except Exception:
                    logger.exception("Conversation failed; trace_id=%s", current_trace)
                    span.set_status(Status(StatusCode.ERROR, "Agent turn failed"))
                    raise HTTPException(
                        502,
                        {
                            "message": "The agent could not finish. Check the trace or start a new conversation.",
                            "trace_id": current_trace,
                        },
                    ) from None
                span.set_attribute("langfuse.observation.output", result["reply"])
                request_span.set_attribute("langfuse.observation.output", result["reply"])
                result["trace_id"] = current_trace
                state.traces.add(current_trace)
                state.turns += 1
                state.touched = time.monotonic()
                return result

    async def run_turn(self, state, message, product_id=None):
        prompt = await self.langfuse.prompt()
        calls = []
        tools = await self.scoped_tools(state.shop_session_id, calls, state.currency_code)
        if self.mode == "scripted":
            model = ScriptedModel(scenario=state.scenario, product_id=product_id)
        else:
            model = ChatLLM(timeout=30, max_retries=1)

        @wrap_model_call
        async def observe_model(model_request, handler):
            model_name = "scripted-demo" if self.mode == "scripted" else os.getenv("LLM_MODEL", "")
            span_attrs = {
                "langfuse.observation.type": "generation",
                "langfuse.observation.model.name": model_name,
                "gen_ai.request.model": model_name,
                "gen_ai.operation.name": "chat",
                "gen_ai.prompt.name": PROMPT_NAME,
                "gen_ai.prompt.version": str(prompt.version),
                "langfuse.observation.input": encoded(
                    {
                        "system": system_prompt,
                        "messages": [m.model_dump() for m in model_request.messages],
                        "tools": [convert_to_openai_tool(t) for t in model_request.tools],
                    }
                ),
                "langfuse.observation.metadata.prompt_source": "langfuse"
                if prompt.managed
                else "bundled",
                "langfuse.observation.metadata.prompt_version": prompt.version,
            }
            if prompt.managed:
                span_attrs.update(
                    {
                        "langfuse.observation.prompt.name": PROMPT_NAME,
                        "langfuse.observation.prompt.version": prompt.version,
                    }
                )
            with tracer.start_as_current_span(
                "model.generate",
                attributes=span_attrs,
                kind=SpanKind.CLIENT if self.mode == "live" else SpanKind.INTERNAL,
            ) as span:
                response = await handler(model_request)
                span.set_attribute(
                    "langfuse.observation.output",
                    encoded([m.model_dump() for m in response.result]),
                )
                message = response.result[-1]
                metadata = message.response_metadata
                for source, target in (("model_name", "model"), ("id", "id")):
                    if metadata.get(source):
                        span.set_attribute(f"gen_ai.response.{target}", metadata[source])
                if metadata.get("finish_reason"):
                    span.set_attribute(
                        "gen_ai.response.finish_reasons", [metadata["finish_reason"]]
                    )
                usage = getattr(message, "usage_metadata", None)
                if usage and self.mode == "live":
                    # Langfuse maps standard usage attributes; missing usage stays unknown.
                    for key in ("input_tokens", "output_tokens"):
                        if key in usage:
                            span.set_attribute(f"gen_ai.usage.{key}", usage[key])
                    for group, source, target in (
                        ("input_token_details", "cache_read", "cache_read.input_tokens"),
                        ("input_token_details", "cache_creation", "cache_write.input_tokens"),
                        ("output_token_details", "reasoning", "reasoning.output_tokens"),
                    ):
                        value = usage.get(group, {}).get(source)
                        if value is not None:
                            span.set_attribute(f"gen_ai.usage.{target}", value)
                return response

        system_prompt = (
            prompt.text + f"\nThe shopper's budget is {state.currency_code} {state.budget:.2f}."
        )
        if product_id:
            system_prompt += f"\nThe shopper is currently viewing product {product_id}."
        graph = create_agent(
            model, tools=tools, system_prompt=system_prompt, middleware=[observe_model]
        )
        try:
            result = await graph.ainvoke(
                {"messages": [*state.messages, HumanMessage(content=message)]},
                config={"recursion_limit": self.agentRecursionLimit},
            )
        finally:
            if self.mode == "live":
                await model.http_async_client.aclose()
        state.messages = result["messages"]
        content = state.messages[-1].content
        reply = content if isinstance(content, str) else encoded(content)
        scores = {}
        # This evaluator has known fixture semantics; live replies use human feedback.
        if self.mode == "scripted":
            products = [
                call["result"]
                for call in calls
                if call["name"] == "get_product"
                and isinstance(call["result"], dict)
                and "id" in call["result"]
            ]
            if products:
                scores["budget_adherence"] = int(price(products[-1]) <= state.budget)
        for name, value in scores.items():
            with tracer.start_as_current_span(
                "evaluate.budget",
                attributes={
                    "langfuse.observation.type": "evaluator",
                    "langfuse.observation.input": encoded(
                        {
                            "budget": state.budget,
                            "currency_code": state.currency_code,
                            "product": products[-1],
                        }
                    ),
                    "langfuse.observation.output": encoded({name: value}),
                },
            ):
                try:
                    await self.langfuse.score(
                        trace_id(), name, value, "Deterministic scripted recommendation check"
                    )
                except httpx.HTTPError:
                    logger.warning("Could not submit evaluation score; trace_id=%s", trace_id())
        return {
            "reply": reply,
            "calls": calls,
            "scores": scores,
            "prompt_version": prompt.version,
            "prompt_source": "langfuse" if prompt.managed else "bundled",
        }

    async def feedback(self, request: FeedbackRequest):
        state = self.sessions.get(str(request.session_id))
        if not state or request.trace_id not in state.traces:
            raise HTTPException(404, "That response does not belong to this conversation.")
        if not self.langfuse.enabled:
            raise HTTPException(503, "Configure Langfuse to save feedback.")
        try:
            await self.langfuse.score(
                request.trace_id, "user_helpfulness", request.value, request.comment
            )
        except httpx.HTTPError:
            raise HTTPException(
                502, "Langfuse could not save feedback. Please try again."
            ) from None
        return {"saved": True}

    def links(self, current_trace):
        links = {}
        public_url = os.getenv("LANGFUSE_PUBLIC_URL") or os.getenv("LANGFUSE_BASE_URL", "")
        project_id = os.getenv("LANGFUSE_PROJECT_ID", "")
        if public_url and project_id:
            links["Langfuse"] = (
                f"{public_url.rstrip('/')}/project/{quote(project_id, safe='')}/traces/{current_trace}"
            )
        template = os.getenv("CLICKSTACK_TRACE_URL_TEMPLATE", "")
        if template:
            links["ClickStack"] = template.replace("{trace_id}", current_trace)
        return links
