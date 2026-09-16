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
from src.agents.mcp_client import MCPClient

from concierge.contract import (
    CONTRACT_VERSION,
    IDLE_SECONDS,
    MAX_TURNS,
    AddToCartAction,
    AssistantResponse,
    ConflictReason,
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


# Refusals shared by the legacy /prompt route (plain-string detail) and the storefront routes.
REBIND_MESSAGE = "Start a new conversation to change scenario or budget."
TURN_LIMIT_MESSAGE = f"This conversation reached {MAX_TURNS} turns. Start a new one."

# How long a turn waits, while holding mcp_lock, for a replacement MCP connection. Without a
# bound here, streamablehttp_client's 30s default POST timeout would run under the mutex and
# every queued turn would wait behind one wedged connect, eating a third of the 90s turn budget
# (execute_turn's asyncio.timeout(90)) before any of them got a chance to retry.
RECONNECT_TIMEOUT = 10


def conflict(reason: ConflictReason, message: str) -> HTTPException:
    """A 409 on the storefront routes: the reason lets the client classify it (contract.py)."""
    return HTTPException(409, {"message": message, "reason": reason})


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
        # Constructed outside a running loop, which is valid on 3.14 (the loop parameter and
        # get_event_loop() call were removed in 3.10; binding is lazy via _LoopBoundMixin's
        # _get_loop, on acquire's slow path). That laziness is also why this must stay a
        # per-instance lock rather than a module-level or session-scoped shared agent: the
        # latter would bind it to whichever loop acquired it first and break the ac12(c)
        # concurrency test, since run_agent.py builds the agent at module scope for one
        # long-lived uvicorn loop while the test fixture rebuilds it, function-scoped, per test.
        self.mcp_lock = asyncio.Lock()
        # A dedicated task per MCP connection (see _mcp_owner) opens, publishes, and later
        # closes it; these strong references keep such a task alive even if nothing else awaits
        # it -- the event loop only holds a weak reference to a task, so an unreferenced one can
        # be garbage collected mid-run.
        self._mcp_owner_tasks = set()
        # The retire signal for whichever connection is currently installed, if that connection
        # was opened by one of our owner tasks rather than by the lifespan task's own startup
        # connect. Set here by the reconnect that supersedes it.
        self._mcp_retire = None
        # Overridable one-line seam: tests install a fake here so the real reconnect/owner-task
        # path runs against a client with no socket.
        self.mcp_client_factory = MCPClient

    @asynccontextmanager
    async def lifespan(self, app):
        async with super().lifespan(app):
            # The vendored teardown below (`if self.mcp_server: await self.mcp_server.cleanup()`,
            # in Agent.lifespan) must close the connection THIS task opened, not whatever
            # generation a reconnect has since installed -- so capture it now and restore it
            # right before that teardown runs, no matter how many reconnects happened in
            # between. Every replacement connection is retired through its own owner task
            # instead (ac8).
            startup_client = self.mcp_server
            yield
            self.mcp_server = startup_client
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
            raise HTTPException(409, REBIND_MESSAGE)
        state.touched = time.monotonic()
        return state

    def conversation(self, conversation_id):
        self.expire()
        state = self.sessions.get(str(conversation_id))
        if state is None:
            raise HTTPException(404, "Conversation not found or expired. Start a new one.")
        return state

    def owned_conversation(self, conversation_id, shop_session_id):
        """The live conversation with this id, only for the storefront session it is bound to."""
        state = self.conversation(conversation_id)
        if state.shop_session_id != str(shop_session_id):
            raise conflict(
                "foreign",
                "This conversation belongs to a different storefront session. Start a new one.",
            )
        return state

    def stored_or_ready(self, state, request_id):
        """Return the stored reply for a repeated request id, or None once a turn may start."""
        stored = state.replies.get(str(request_id))
        if stored is not None:
            return stored
        if state.lock.locked():
            raise conflict("in_flight", "A turn is already in flight for this conversation.")
        if state.turns >= MAX_TURNS:
            raise conflict("turn_limit", TURN_LIMIT_MESSAGE)
        return None

    # -- MCP reconnect ----------------------------------------------------------------------
    #
    # ONE OWNER TASK PER CONNECTION. Whichever task calls connect_to_mcp_server() is the only
    # task that ever calls cleanup() on that connection, and it does so only after its own
    # retire signal fires -- never from a request task, and never by dropping the last
    # reference for the garbage collector to finalize (an async generator's finalization is
    # cancellation too, at an unpredictable later moment). This is not a style preference: the
    # exit stack's ClientSession.__aexit__ closes an anyio CancelScope, and CancelScope.__enter__
    # records whichever task entered it as that scope's _host_task; anyio's _deliver_cancellation
    # then cancels every task in the scope that "is not current" -- including the host task, if
    # some other task calls cleanup(). For the very first connection, the host task is uvicorn's
    # lifespan task, so a stray cleanup() from a request task kills lifespan and the shutdown
    # force_flush() (see lifespan() above) never runs again. Do not "simplify" this back into a
    # plain `await client.cleanup()` from a request task.

    async def fetch_tools(self):
        """The tool list, replacing a probably-dead MCP session with a fresh one on failure.

        Broad `except Exception` on purpose, matching the precedent at invoke()'s own
        `except Exception` above (an "external system misbehaved" boundary): the exception shape
        the released image actually raises on a dead session cannot be pinned down, so the
        trigger is a failed fetch, never a matched exception type.
        """
        stale = self.mcp_server  # captured BEFORE the fetch, never re-read after a failure --
        # the loser of a reconnect race must compare against what it saw, not against
        # self.mcp_server as it stands after the fact, or it would reconnect a healthy session.
        try:
            return await self.get_tool_list()
        except Exception:
            if stale is None:
                # HTTP-tools mode: get_tool_list() cannot fail this way, but if a test or a
                # future caller makes it, there is no MCP connection to replace.
                raise
        await self._replace_mcp_session(stale)
        return await self.get_tool_list()

    async def _replace_mcp_session(self, stale):
        """Install a fresh MCP connection, unless another turn already has.

        Serialized by mcp_lock together with the identity check, so N concurrent turns that all
        saw `stale` produce exactly one reconnect: the loser of the lock re-checks and finds
        self.mcp_server already replaced, and returns without opening a second connection.
        """
        async with self.mcp_lock:
            if self.mcp_server is not stale:
                return
            ready = asyncio.get_running_loop().create_future()
            retire = asyncio.Event()
            owner = asyncio.create_task(self._mcp_owner(ready, retire))
            self._mcp_owner_tasks.add(owner)
            owner.add_done_callback(self._mcp_owner_tasks.discard)
            cancelled = None
            try:
                async with asyncio.timeout(RECONNECT_TIMEOUT):
                    await ready
            except BaseException as exc:
                # asyncio.CancelledError derives from BaseException, not Exception (its MRO is
                # CancelledError -> BaseException -> object), and both an outer cancellation and
                # this timeout expiring deliver it here, so `except Exception` above would miss
                # it -- as does a genuine connect failure, reraised through `ready`. Remember it;
                # whether it is re-raised below depends on whether a connection got installed.
                cancelled = exc
                if self.mcp_server is stale:
                    # Nothing installed yet: the owner is still connecting, or it already
                    # failed and ran its own cleanup. Cancelling it is how a task that never
                    # reached `await retire.wait()` notices -- it remains the only task that
                    # ever calls cleanup() on what it opened. Awaiting it here is what makes
                    # that retirement deterministic rather than a fire-and-forget hope.
                    owner.cancel()
                    await owner
            if self.mcp_server is not stale:
                # Installed, cancelled or not: _mcp_owner assigns self.mcp_server BEFORE
                # signalling `ready`, so a waiter cancelled the instant `ready` resolves still
                # always finds an installed, owned connection -- never a published-but-
                # uninstalled one (ac16's same-tick race). Retire the generation this replaces,
                # through THAT owner's own task rather than here, and remember this one for
                # whichever reconnect supersedes it next.
                previous_retire, self._mcp_retire = self._mcp_retire, retire
                if previous_retire is not None:
                    previous_retire.set()
            if cancelled is not None:
                raise cancelled

    async def _mcp_owner(self, ready, retire):
        """Open one MCP connection, publish it, then close only what this task itself opened."""
        client = self.mcp_client_factory()
        try:
            await client.connect_to_mcp_server(self.mcp_server_url)
        except BaseException as exc:
            await client.cleanup()
            if not ready.done():
                ready.set_exception(exc)
            return
        self.mcp_server = client
        if not ready.done():
            ready.set_result(client)
        await retire.wait()
        await client.cleanup()

    # -- tools -----------------------------------------------------------------------------

    async def scoped_tools(self, user_id, calls, currency_code="USD"):
        """Model-visible tools.

        The cart identity and currency are injected here, never by the model, and the same
        wrappers front both the HTTP and the MCP tool transports.
        """
        upstream = {t.name: t for t in await self.fetch_tools()}

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
                raise HTTPException(409, TURN_LIMIT_MESSAGE)
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
                "links": await self.links(result["trace_id"]),
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
            state = self.owned_conversation(key, shop_session_id)
            if (request.scenario is not None and request.scenario != state.scenario) or (
                request.budget is not None and request.budget != state.budget
            ):
                raise conflict("rebind", REBIND_MESSAGE)
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
            return await self.remember(state, request.request_id, result, key)

    async def assistant_add_to_cart(self, request: AddToCartAction):
        key = str(request.conversation_id)
        # Ownership first: a stored reply is never handed to another storefront session.
        state = self.owned_conversation(key, request.shop_session_id)
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
                    # Everything that awaits runs before the cart call: once the shop has
                    # changed the cart, recording the turn below cannot be interrupted, so the
                    # conversation never misses an action the shop performed.
                    prompt = await self.langfuse.prompt()
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
            return await self.remember(state, request.request_id, result, key)

    async def assistant_conversation(self, conversation_id: UUID, shop_session_id: UUID):
        # The caller names its own session (a required query parameter); the bound one is never
        # returned, so a leaked conversation id alone does not yield the storefront cart key.
        state = self.owned_conversation(conversation_id, shop_session_id)
        return ConversationStatus(
            conversation_id=str(conversation_id),
            turns=state.turns,
            currency_code=state.currency_code,
            expires_at=state.expires_at(),
        )

    async def remember(self, state, request_id, result, conversation_id):
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
                links=await self.links(result["trace_id"]),
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
            llm_kwargs = {"timeout": 30, "max_retries": 1}
            if os.getenv("LLM_MODEL", "").lower().startswith("gpt-5"):
                # gpt-5 models default to reasoning enabled, which OpenAI's
                # /v1/chat/completions rejects alongside function tools; "none"
                # disables reasoning so the agent's tool calls go through.
                llm_kwargs["reasoning_effort"] = "none"
            model = ChatLLM(**llm_kwargs)

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

    async def links(self, current_trace):
        links = {}
        public_url = os.getenv("LANGFUSE_PUBLIC_URL") or os.getenv("LANGFUSE_BASE_URL", "")
        project_id = await self.langfuse.project_id(os.getenv("LANGFUSE_PROJECT_ID", ""))
        if public_url and project_id:
            links["Langfuse"] = (
                f"{public_url.rstrip('/')}/project/{quote(project_id, safe='')}/traces/{current_trace}"
            )
        template = os.getenv("CLICKSTACK_TRACE_URL_TEMPLATE", "")
        if template:
            links["ClickStack"] = template.replace("{trace_id}", current_trace)
        return links
