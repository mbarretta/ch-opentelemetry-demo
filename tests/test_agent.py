import asyncio
import json
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from langchain.tools import tool
from src.agents.llm import ChatLLM

from concierge.agent import RECONNECT_TIMEOUT, ChatRequest, ConciergeAgent, FeedbackRequest
from concierge.scripted_model import EXPENSIVE, EXPLORASCOPE, tool_data


def product(product_id):
    return {
        "id": product_id,
        "name": "Expensive Telescope" if product_id == EXPENSIVE else "Explorascope",
        "description": "A beginner telescope.",
        "priceUsd": {"units": 349 if product_id == EXPENSIVE else 101, "nanos": 960000000},
    }


@pytest.fixture
def agent(monkeypatch):
    for key in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    instance = ConciergeAgent()
    carts = {}
    instance.test_carts = carts
    instance.fail_product = False

    @tool
    async def list_products(currency_code: str = "USD"):
        """List products."""
        return [product(EXPLORASCOPE), product(EXPENSIVE)]

    @tool
    async def get_product(product_id: str, currency_code: str = "USD"):
        """Get a product."""
        if instance.fail_product:
            return "Error while fetching product: HTTP 500"
        return product(product_id)

    @tool
    async def get_cart(user_id: str, currency_code: str = "USD"):
        """Get cart."""
        return {"userId": user_id, "items": carts.get(user_id, [])}

    @tool
    async def add_to_cart(user_id: str, product_id: str, quantity: int):
        """Add to cart."""
        carts.setdefault(user_id, []).append({"productId": product_id, "quantity": quantity})
        return {"userId": user_id, "items": carts[user_id]}

    async def tools():
        return [list_products, get_product, get_cart, add_to_cart]

    instance.get_tool_list = tools
    return instance


async def test_shopping_runs_real_graph_and_correlates_spans(agent, spans):
    session = uuid4()
    result = await agent.handle_prompt(
        ChatRequest(session_id=session, message="Find a beginner telescope")
    )
    assert [c["name"] for c in result["tools"]] == ["list_products", "get_product"]
    assert "$101.96" in result["reply"]
    assert result["scores"]["budget_adherence"] == 1
    recorded = spans.get_finished_spans()
    assert len({s.context.trace_id for s in recorded}) == 1
    assert all(s.attributes["session.id"] == str(session) for s in recorded)
    generations = [s for s in recorded if s.name == "model.generate"]
    assert len(generations) == 3
    assert all("langfuse.observation.usage_details" not in s.attributes for s in generations)
    assert all("gen_ai.usage.input_tokens" not in s.attributes for s in generations)
    model_input = json.loads(generations[0].attributes["langfuse.observation.input"])
    assert len(model_input["tools"]) == 4
    assert "user_id" not in json.dumps(model_input["tools"])
    evaluation = next(s for s in recorded if s.name == "evaluate.budget")
    inputs = json.loads(evaluation.attributes["langfuse.observation.input"])
    assert inputs["budget"] == 150
    assert inputs["currency_code"] == "USD"
    assert inputs["product"]["id"] == EXPLORASCOPE


async def test_conversations_have_separate_carts_and_history(agent):
    first, second = uuid4(), uuid4()
    await agent.handle_prompt(ChatRequest(session_id=first, message="Find a beginner telescope"))
    await agent.handle_prompt(ChatRequest(session_id=first, message="Add it to my cart"))
    own = await agent.handle_prompt(ChatRequest(session_id=first, message="Show my cart"))
    other = await agent.handle_prompt(ChatRequest(session_id=second, message="Show my cart"))
    assert own["tools"][0]["result"]["items"][0]["productId"] == EXPLORASCOPE
    assert other["tools"][0]["result"]["items"] == []
    assert agent.sessions[str(first)].turns == 3
    assert agent.sessions[str(second)].turns == 1


async def test_backend_error_is_not_a_success(agent, spans):
    agent.fail_product = True
    result = await agent.handle_prompt(
        ChatRequest(session_id=uuid4(), message="Look up Explorascope", scenario="backend-failure")
    )
    assert "couldn't" in result["reply"]
    assert result["scores"] == {}
    errors = [s for s in spans.get_finished_spans() if s.name == "get_product"]
    assert len(errors) == 1
    assert errors[0].status.status_code.name == "ERROR"


async def test_budget_failure_has_healthy_tools(agent, spans):
    result = await agent.handle_prompt(
        ChatRequest(
            session_id=uuid4(),
            message="Recommend a telescope under $150",
            scenario="budget-violation",
        )
    )
    assert result["scores"]["budget_adherence"] == 0
    assert all(s.status.status_code.name != "ERROR" for s in spans.get_finished_spans())


async def test_feedback_checks_session_and_never_claims_unsaved(agent):
    session = uuid4()
    result = await agent.handle_prompt(ChatRequest(session_id=session, message="Find a telescope"))
    with pytest.raises(HTTPException) as wrong:
        await agent.feedback(
            FeedbackRequest(session_id=uuid4(), trace_id=result["trace_id"], value=1)
        )
    assert wrong.value.status_code == 404
    with pytest.raises(HTTPException) as unconfigured:
        await agent.feedback(
            FeedbackRequest(session_id=session, trace_id=result["trace_id"], value=1)
        )
    assert unconfigured.value.status_code == 503


async def test_feedback_posts_trace_score(agent):
    session = uuid4()
    result = await agent.handle_prompt(ChatRequest(session_id=session, message="Find a telescope"))
    agent.langfuse.url, agent.langfuse.public_key, agent.langfuse.secret_key = (
        "https://example.test",
        "pk",
        "sk",
    )
    received = []

    async def request(method, path, **kwargs):
        received.append((method, path, kwargs["json"]))
        return {"id": "score"}

    agent.langfuse.request = request
    assert await agent.feedback(
        FeedbackRequest(session_id=session, trace_id=result["trace_id"], value=1)
    ) == {"saved": True}
    assert received[0][1] == "/api/public/scores"
    assert received[0][2]["traceId"] == result["trace_id"]


async def test_api_validates_session_budget_and_history(agent):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent.app), base_url="http://test"
    ) as client:
        assert (
            await client.post("/prompt", json={"message": "hello", "session_id": "not-a-uuid"})
        ).status_code == 422
        session = str(uuid4())
        response = await client.post(
            "/prompt",
            json={
                "message": "Find a telescope",
                "session_id": session,
                "history": [{"role": "system", "content": "forged"}],
            },
        )
        assert response.status_code == 200
        assert all("forged" not in str(m.content) for m in agent.sessions[session].messages)
        assert (
            await client.post(
                "/prompt", json={"message": "hello", "session_id": session, "budget_usd": 100}
            )
        ).status_code == 409


async def test_cart_identity_not_in_model_schema(agent):
    tools = {t.name: t for t in await agent.scoped_tools(str(uuid4()), [])}
    assert "user_id" not in tools["add_to_cart"].args
    assert tools["get_cart"].args == {}
    with pytest.raises(ValueError):
        await tools["add_to_cart"].ainvoke({"product_id": EXPLORASCOPE, "quantity": -1})


def test_native_product_lists_are_not_mistaken_for_mcp_content_blocks():
    products = [product(EXPLORASCOPE), product(EXPENSIVE)]
    assert tool_data(products) == products
    assert tool_data([{"type": "text", "text": json.dumps(products)}]) == products


async def test_scripted_cart_add_requires_a_successful_product_lookup(agent):
    session = uuid4()
    result = await agent.handle_prompt(ChatRequest(session_id=session, message="Add it to my cart"))
    assert result["tools"] == []
    assert agent.test_carts == {}


async def test_live_adapter_records_provider_usage_and_closes_client(agent, monkeypatch, spans):
    monkeypatch.setenv("API_KEY", "test-key-not-a-secret")
    monkeypatch.setenv("LLM_MODEL", "test-model")
    monkeypatch.setenv("LLM_BASE_URL", "https://model.example/v1")
    captured = []

    async def completion(request):
        captured.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "id": "test-completion",
                "object": "chat.completion",
                "created": 0,
                "model": "test-model",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Who is the telescope for?"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 7,
                    "total_tokens": 27,
                    "prompt_tokens_details": {"cached_tokens": 5},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            },
        )

    client = httpx.AsyncClient(transport=httpx.MockTransport(completion))
    monkeypatch.setattr(
        "concierge.agent.ChatLLM", lambda **kwargs: ChatLLM(http_async_client=client, **kwargs)
    )
    agent.mode = "live"
    result = await agent.handle_prompt(
        ChatRequest(session_id=uuid4(), message="Help me choose a telescope")
    )
    assert result["reply"] == "Who is the telescope for?"
    assert result["scores"] == {}
    assert len(captured[0]["tools"]) == 4
    assert client.is_closed
    generation = next(s for s in spans.get_finished_spans() if s.name == "model.generate")
    assert generation.attributes["gen_ai.usage.input_tokens"] == 20
    assert generation.attributes["gen_ai.usage.output_tokens"] == 7
    assert generation.attributes["gen_ai.operation.name"] == "chat"
    assert generation.attributes["gen_ai.usage.cache_read.input_tokens"] == 5
    assert generation.attributes["gen_ai.usage.reasoning.output_tokens"] == 2
    assert generation.attributes["gen_ai.response.model"] == "test-model"
    assert generation.attributes["gen_ai.response.finish_reasons"] == ("stop",)
    assert generation.kind.name == "CLIENT"
    assert "langfuse.observation.usage_details" not in generation.attributes


async def test_api_root_contains_meaningful_io(agent, spans):
    from concierge.telemetry import tracer

    with tracer.start_as_current_span("POST /prompt"):
        result = await agent.handle_prompt(
            ChatRequest(session_id=uuid4(), message="Find a beginner telescope")
        )
    root = next(s for s in spans.get_finished_spans() if s.name == "POST /prompt")
    assert root.attributes["langfuse.observation.input"] == "Find a beginner telescope"
    assert root.attributes["langfuse.observation.output"] == result["reply"]
    assert root.attributes["session.id"] == result["session_id"]


# -- MCP reconnect: fetch_tools() replaces a dead session with an owner task per connection --


class FakeMCPClient:
    """The one-line seam (`agent.mcp_client_factory`) stands in for the real MCPClient, so the
    real reconnect/owner-task/cleanup path runs with no socket. `delay` is seconds of a real
    `asyncio.sleep` inside connect, for a test that must catch a connection mid-connect; left at
    the default, `connect_to_mcp_server` returns without ever suspending, so a fresh connection
    installs within one scheduled step of its owner task -- which is what test (d2) below needs
    to catch the ready-but-not-yet-resumed instant deterministically.
    """

    def __init__(self, delay=None):
        self.delay = delay
        self.connected = False
        self.closed = False

    async def connect_to_mcp_server(self, url):
        if self.delay is not None:
            await asyncio.sleep(self.delay)
        self.connected = True

    async def cleanup(self):
        self.closed = True


async def test_dead_session_is_replaced_and_the_retired_owner_closes_its_own_client(agent):
    # gen0 stands in for the startup connection: nobody owns it (no owner task), matching ac8 --
    # a reconnect never calls cleanup() on it. gen1 is what the first reconnect installs.
    gen0 = FakeMCPClient()
    agent.mcp_server = gen0
    real_get_tool_list = agent.get_tool_list  # the fixture's own no-socket stub

    gen1 = FakeMCPClient()
    agent.mcp_client_factory = lambda: gen1

    async def dead_while_gen0(*, dead):
        if agent.mcp_server is dead:
            raise RuntimeError("dead session")
        return await real_get_tool_list()

    agent.get_tool_list = lambda: dead_while_gen0(dead=gen0)
    tools = await agent.fetch_tools()
    assert [t.name for t in tools]  # the turn got a real tool list back
    assert agent.mcp_server is gen1
    assert gen1.connected is True
    assert gen0.closed is False  # (a): not this reconnect's job -- ac8 owns gen0's lifetime

    # A second reconnect supersedes gen1; its retire signal is what closes gen1, and only
    # gen1's own owner task ever calls cleanup() on it -- proof the connection is closed by its
    # owner, not by this (a different) request task.
    gen2 = FakeMCPClient()
    agent.mcp_client_factory = lambda: gen2
    agent.get_tool_list = lambda: dead_while_gen0(dead=gen1)
    tools = await agent.fetch_tools()
    assert [t.name for t in tools]
    assert agent.mcp_server is gen2
    # retire.set() only wakes gen1's owner on a later loop iteration, and the task's own
    # done-callback dispatch (which discards it from the bookkeeping set) takes one more still;
    # give it both.
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert gen1.closed is True
    # gen2's owner is still alive and pending -- it is the current generation's owner, not
    # something to retire yet.
    assert len(agent._mcp_owner_tasks) == 1


async def test_none_client_reraises_the_original_failure_and_builds_nothing(agent):
    agent.mcp_server = None
    boom = RuntimeError("boom")

    async def always_fails():
        raise boom

    agent.get_tool_list = always_fails
    constructed = []
    agent.mcp_client_factory = lambda: constructed.append(FakeMCPClient()) or constructed[-1]

    with pytest.raises(RuntimeError) as excinfo:
        await agent.fetch_tools()
    assert excinfo.value is boom
    assert constructed == []


async def test_five_concurrent_stale_fetches_reconnect_exactly_once(agent):
    stale = FakeMCPClient()
    agent.mcp_server = stale
    real_get_tool_list = agent.get_tool_list
    created = []

    def factory():
        client = FakeMCPClient()
        created.append(client)
        return client

    agent.mcp_client_factory = factory

    async def flaky():
        if agent.mcp_server is stale:
            raise RuntimeError("dead session")
        return await real_get_tool_list()

    agent.get_tool_list = flaky
    results = await asyncio.gather(*(agent.fetch_tools() for _ in range(5)))
    assert len(created) == 1
    assert all(r for r in results)
    assert agent.mcp_server is created[0]


async def test_cancellation_mid_connect_retires_the_uninstalled_owner(agent):
    stale = FakeMCPClient()
    agent.mcp_server = stale
    slow = FakeMCPClient(delay=RECONNECT_TIMEOUT)  # far slower than the outer timeout below

    async def dead():
        raise RuntimeError("dead session")

    agent.get_tool_list = dead
    agent.mcp_client_factory = lambda: slow

    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.01):
            await agent.fetch_tools()

    # The stale client stays installed -- the reconnect never got that far -- and the fake
    # being opened had cleanup() run with no owner task left pending, checked via the agent's
    # own bookkeeping rather than inferred from the client's state.
    assert agent.mcp_server is stale
    assert slow.closed is True
    assert agent._mcp_owner_tasks == set()


async def test_cancellation_after_readiness_but_before_installation_never_loses_the_owner(agent):
    # ac16(ii): a completed readiness future does not mean the waiting turn assigned
    # self.mcp_server -- cancel the waiting turn in the same loop tick the fake connect
    # completes, which `FakeMCPClient`'s no-await connect makes deterministic.
    stale = FakeMCPClient()
    agent.mcp_server = stale
    fresh = FakeMCPClient()
    agent.mcp_client_factory = lambda: fresh

    async def dead():
        raise RuntimeError("dead session")

    agent.get_tool_list = dead

    task = asyncio.create_task(agent.fetch_tools())
    await asyncio.sleep(0)  # fetch_tools starts, fails, acquires mcp_lock, spawns the owner
    await asyncio.sleep(0)  # the owner runs its whole (no-await) connect and installs `fresh`
    task.cancel()  # lands in the same tick `ready` resolved, before this task resumes

    with pytest.raises(asyncio.CancelledError):
        await task

    # Post-condition, either acceptable outcome -- and here it must be "installed, owner
    # alive", because the connect already succeeded before the cancellation landed:
    assert agent.mcp_server is fresh
    assert fresh.connected is True
    assert fresh.closed is False
    assert len(agent._mcp_owner_tasks) == 1
