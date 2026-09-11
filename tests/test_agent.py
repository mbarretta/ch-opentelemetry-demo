import json
from uuid import uuid4

import httpx
import pytest
from fastapi import HTTPException
from langchain.tools import tool
from src.agents.llm import ChatLLM

from concierge.agent import ChatRequest, ConciergeAgent, FeedbackRequest
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
    async def list_products():
        """List products."""
        return [product(EXPLORASCOPE), product(EXPENSIVE)]

    @tool
    async def get_product(product_id: str):
        """Get a product."""
        if instance.fail_product:
            return "Error while fetching product: HTTP 500"
        return product(product_id)

    @tool
    async def get_cart(user_id: str):
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
    assert inputs["budget_usd"] == 150
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
