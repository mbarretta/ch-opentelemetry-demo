import json
from datetime import UTC, datetime
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import HTTPException
from langchain.tools import tool

from concierge.agent import ChatRequest, ConciergeAgent, FeedbackRequest
from concierge.contract import (
    CONTRACT_VERSION,
    AssistantResponse,
    MessageRequest,
    product_refs,
)
from concierge.scripted_model import EXPENSIVE, EXPLORASCOPE, ScriptedModel, format_money
from scripts import demo


def product(product_id, currency="USD"):
    rate = {"USD": 1, "EUR": 0.9}[currency]
    base = 349.96 if product_id == EXPENSIVE else 101.96
    amount = round(base * rate, 2)
    return {
        "id": product_id,
        "name": "Expensive Telescope" if product_id == EXPENSIVE else "Explorascope",
        "description": "A beginner telescope. Easy to set up.",
        "picture": f"/images/products/{product_id}.jpg",
        "priceUsd": {
            "currencyCode": currency,
            "units": int(amount),
            "nanos": round((amount - int(amount)) * 1_000_000_000),
        },
    }


@pytest.fixture
def agent(monkeypatch):
    for key in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY"):
        monkeypatch.delenv(key, raising=False)
    instance = ConciergeAgent()
    calls = []
    carts = {}
    instance.test_calls = calls
    instance.test_carts = carts
    instance.fail_cart = False

    @tool
    async def list_products(currency_code: str = "USD"):
        """List products."""
        calls.append(("list_products", {"currency_code": currency_code}))
        return [product(EXPLORASCOPE, currency_code), product(EXPENSIVE, currency_code)]

    @tool
    async def get_product(product_id: str, currency_code: str = "USD"):
        """Get a product."""
        calls.append(("get_product", {"product_id": product_id, "currency_code": currency_code}))
        return product(product_id, currency_code)

    @tool
    async def get_cart(user_id: str, currency_code: str = "USD"):
        """Get cart."""
        calls.append(("get_cart", {"user_id": user_id, "currency_code": currency_code}))
        return {
            "userId": user_id,
            "items": [
                {**item, "product": product(item["productId"], currency_code)}
                for item in carts.get(user_id, [])
            ],
        }

    @tool
    async def add_to_cart(user_id: str, product_id: str, quantity: int):
        """Add to cart."""
        calls.append(("add_to_cart", {"user_id": user_id, "product_id": product_id}))
        if instance.fail_cart:
            return "Error while adding product to cart: HTTP 503"
        carts.setdefault(user_id, []).append({"productId": product_id, "quantity": quantity})
        return {"userId": user_id, "items": carts[user_id]}

    async def tools():
        return [list_products, get_product, get_cart, add_to_cart]

    instance.get_tool_list = tools
    return instance


@pytest.fixture
async def client(agent):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=agent.app), base_url="http://test"
    ) as client:
        yield client


def message(shop_session, conversation_id=None, text="Find a beginner telescope", **extra):
    return {
        "conversation_id": conversation_id,
        "shop_session_id": shop_session,
        "request_id": str(uuid4()),
        "message": text,
        "currency_code": "USD",
        **extra,
    }


async def test_message_binds_new_conversation_to_shop_session(agent, client):
    shop_session = str(uuid4())
    response = await client.post("/assistant/message", json=message(shop_session))
    assert response.status_code == 200, response.text
    body = response.json()
    UUID(body["conversation_id"])
    assert body["contract_version"] == CONTRACT_VERSION == "1"
    assert body["request_id"]
    assert body["cart_changed"] is False
    assert body["feedback_enabled"] is False
    assert len(body["trace_id"]) == 32
    assert body["demo"]["mode"] == "scripted"
    assert body["demo"]["prompt_source"] == "bundled"
    assert [t["name"] for t in body["demo"]["tools"]] == ["list_products", "get_product"]
    assert all("user_id" not in t["arguments"] for t in body["demo"]["tools"])
    assert set(body) == set(AssistantResponse.model_fields)
    state = agent.sessions[body["conversation_id"]]
    assert state.shop_session_id == shop_session
    assert state.turns == 1


async def test_rebinding_a_conversation_to_another_shop_session_is_rejected(agent, client):
    first = (await client.post("/assistant/message", json=message(str(uuid4())))).json()
    conversation_id = first["conversation_id"]
    other = await client.post(
        "/assistant/message", json=message(str(uuid4()), conversation_id, "Show my cart")
    )
    assert other.status_code == 409
    assert "storefront session" in other.json()["detail"]
    assert agent.sessions[conversation_id].turns == 1


async def test_cart_tools_receive_the_bound_shop_session(agent, client):
    shop_session = str(uuid4())
    first = (await client.post("/assistant/message", json=message(shop_session))).json()
    conversation_id = first["conversation_id"]
    added = (
        await client.post(
            "/assistant/message", json=message(shop_session, conversation_id, "Add it to my cart")
        )
    ).json()
    assert added["cart_changed"] is True
    shown = (
        await client.post(
            "/assistant/message", json=message(shop_session, conversation_id, "Show my cart")
        )
    ).json()
    assert shown["cart_changed"] is False
    assert agent.test_carts == {shop_session: [{"productId": EXPLORASCOPE, "quantity": 1}]}
    identities = {args["user_id"] for name, args in agent.test_calls if "user_id" in args}
    assert identities == {shop_session}
    assert conversation_id not in identities


async def test_product_refs_come_from_tool_results_not_prose(agent, client):
    body = (await client.post("/assistant/message", json=message(str(uuid4())))).json()
    assert body["product_refs"] == [
        {
            "id": EXPLORASCOPE,
            "name": "Explorascope",
            "picture": f"/images/products/{EXPLORASCOPE}.jpg",
            "price": {"currencyCode": "USD", "units": 101, "nanos": 960000000},
        }
    ]
    assert "Explorascope" in body["reply"]


def test_product_refs_ignore_failed_and_malformed_results():
    good = product(EXPLORASCOPE)
    calls = [
        {"name": "get_product", "arguments": {}, "result": {"error": "HTTP 500"}},
        {"name": "get_product", "arguments": {}, "result": {"id": "X", "name": "No price"}},
        {"name": "get_product", "arguments": {}, "result": good},
        {"name": "get_product", "arguments": {}, "result": good},
        {"name": "get_cart", "arguments": {}, "result": {"items": [{"product": product("Z")}]}},
    ]
    assert [ref.id for ref in product_refs(calls)] == [EXPLORASCOPE]
    catalog = [{"name": "list_products", "arguments": {}, "result": [good, product(EXPENSIVE)]}]
    assert [ref.id for ref in product_refs(catalog)] == [EXPLORASCOPE, EXPENSIVE]
    assert [ref.id for ref in product_refs(catalog + calls)] == [EXPLORASCOPE]
    assert product_refs([{"name": "list_products", "arguments": {}, "result": "Error"}]) == []


async def test_cart_identity_and_currency_are_injected_on_every_tool(agent):
    shop_session = str(uuid4())
    calls = []
    tools = {t.name: t for t in await agent.scoped_tools(shop_session, calls, "EUR")}
    for name in tools:
        assert "user_id" not in tools[name].args, name
        assert "currency_code" not in tools[name].args, name
    assert tools["list_products"].args == {}
    await tools["list_products"].ainvoke({})
    await tools["get_product"].ainvoke({"product_id": EXPLORASCOPE})
    await tools["get_cart"].ainvoke({})
    await tools["add_to_cart"].ainvoke({"product_id": EXPLORASCOPE, "quantity": 2})
    assert agent.test_calls == [
        ("list_products", {"currency_code": "EUR"}),
        ("get_product", {"product_id": EXPLORASCOPE, "currency_code": "EUR"}),
        ("get_cart", {"user_id": shop_session, "currency_code": "EUR"}),
        ("add_to_cart", {"user_id": shop_session, "product_id": EXPLORASCOPE}),
    ]
    assert [c["arguments"]["currency_code"] for c in calls[:3]] == ["EUR"] * 3


async def test_bootstrap_tools_patch_adds_currency_to_shop_calls(monkeypatch):
    patched = demo.patch_tools((demo.UPSTREAM / "src/shared/tools.py").read_text())
    namespace = {}
    exec(compile(patched, "tools.py", "exec"), namespace)
    requests = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, params=None, **kwargs):
            requests.append((url, params))
            return httpx.Response(200, json={}, request=httpx.Request("GET", url))

    monkeypatch.setattr(namespace["httpx"], "AsyncClient", FakeClient)
    await namespace["list_products"]("EUR")
    await namespace["get_product"](EXPLORASCOPE, "EUR")
    await namespace["get_cart"]("shopper", "EUR")
    await namespace["list_products"]()
    assert [params for _, params in requests] == [
        {"currencyCode": "EUR"},
        {"currencyCode": "EUR"},
        {"sessionId": "shopper", "currencyCode": "EUR"},
        {"currencyCode": "USD"},
    ]
    with pytest.raises(SystemExit):
        demo.patch_tools("def unrelated(): ...")


async def test_repeated_request_id_returns_stored_response_without_rerunning(agent, client):
    shop_session = str(uuid4())
    first = (await client.post("/assistant/message", json=message(shop_session))).json()
    conversation_id = first["conversation_id"]
    add = message(shop_session, conversation_id, "Add it to my cart")
    original = await client.post("/assistant/message", json=add)
    assert original.json()["cart_changed"] is True
    adds_before = [c for c in agent.test_calls if c[0] == "add_to_cart"]
    repeat = await client.post("/assistant/message", json=add)
    assert repeat.status_code == 200
    assert repeat.json() == original.json()
    assert [c for c in agent.test_calls if c[0] == "add_to_cart"] == adds_before
    assert len(adds_before) == 1
    assert agent.sessions[conversation_id].turns == 2
    other = (await client.post("/assistant/message", json=message(str(uuid4())))).json()
    reused = message(str(uuid4()), other["conversation_id"], "Show my cart")
    reused["request_id"] = add["request_id"]
    reused["shop_session_id"] = agent.sessions[other["conversation_id"]].shop_session_id
    assert (await client.post("/assistant/message", json=reused)).json()["cart_changed"] is False


async def test_concurrent_turn_on_same_conversation_is_rejected(agent, client):
    shop_session = str(uuid4())
    first = (await client.post("/assistant/message", json=message(shop_session))).json()
    conversation_id = first["conversation_id"]
    state = agent.sessions[conversation_id]
    async with state.lock:
        blocked = await client.post(
            "/assistant/message", json=message(shop_session, conversation_id, "Show my cart")
        )
    assert blocked.status_code == 409
    assert "in flight" in blocked.json()["detail"]
    assert state.turns == 1


async def test_add_to_cart_action_is_one_scoped_tool_call_in_the_trace(agent, client, spans):
    shop_session = str(uuid4())
    first = (await client.post("/assistant/message", json=message(shop_session))).json()
    conversation_id = first["conversation_id"]
    spans.clear()
    action = {
        "conversation_id": conversation_id,
        "request_id": str(uuid4()),
        "product_id": EXPLORASCOPE,
        "quantity": 2,
        "currency_code": "USD",
    }
    response = await client.post("/assistant/actions/add-to-cart", json=action)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["cart_changed"] is True
    assert body["conversation_id"] == conversation_id
    assert body["request_id"] == action["request_id"]
    assert body["contract_version"] == CONTRACT_VERSION
    assert set(body) == set(AssistantResponse.model_fields)
    assert [t["name"] for t in body["demo"]["tools"]] == ["add_to_cart"]
    assert agent.test_carts == {shop_session: [{"productId": EXPLORASCOPE, "quantity": 2}]}
    recorded = spans.get_finished_spans()
    assert [s.name for s in recorded if s.name == "add_to_cart"] == ["add_to_cart"]
    assert len({s.context.trace_id for s in recorded}) == 1
    assert all(s.attributes["gen_ai.conversation.id"] == conversation_id for s in recorded)
    assert all(s.attributes["session.id"] == shop_session for s in recorded)
    assert body["trace_id"] == format(recorded[0].context.trace_id, "032x")
    state = agent.sessions[conversation_id]
    assert state.turns == 2
    assert EXPLORASCOPE in state.messages[-2].content
    repeat = await client.post("/assistant/actions/add-to-cart", json=action)
    assert repeat.json() == body
    assert len([c for c in agent.test_calls if c[0] == "add_to_cart"]) == 1
    assert (
        await client.post(
            "/assistant/actions/add-to-cart", json={**action, "conversation_id": str(uuid4())}
        )
    ).status_code == 404
    assert (
        await client.post("/assistant/actions/add-to-cart", json={**action, "quantity": 0})
    ).status_code == 422


async def test_failed_add_to_cart_action_is_not_a_success_and_can_be_retried(agent, client):
    shop_session = str(uuid4())
    first = (await client.post("/assistant/message", json=message(shop_session))).json()
    action = {
        "conversation_id": first["conversation_id"],
        "request_id": str(uuid4()),
        "product_id": EXPLORASCOPE,
        "quantity": 1,
        "currency_code": "USD",
    }
    agent.fail_cart = True
    failed = await client.post("/assistant/actions/add-to-cart", json=action)
    assert failed.status_code == 502
    assert len(failed.json()["detail"]["trace_id"]) == 32
    agent.fail_cart = False
    retried = await client.post("/assistant/actions/add-to-cart", json=action)
    assert retried.status_code == 200
    assert retried.json()["cart_changed"] is True


async def test_conversation_status_and_expiry(agent, client):
    shop_session = str(uuid4())
    body = (
        await client.post(
            "/assistant/message", json={**message(shop_session), "currency_code": "EUR"}
        )
    ).json()
    conversation_id = body["conversation_id"]
    status = await client.get(f"/assistant/conversations/{conversation_id}")
    assert status.status_code == 200
    assert status.json() == {
        "conversation_id": conversation_id,
        "shop_session_id": shop_session,
        "turns": 1,
        "currency_code": "EUR",
        "expires_at": status.json()["expires_at"],
    }
    expires_at = datetime.fromisoformat(status.json()["expires_at"])
    assert expires_at.tzinfo is not None
    assert 3500 < (expires_at - datetime.now(UTC)).total_seconds() <= 3600
    assert (await client.get(f"/assistant/conversations/{uuid4()}")).status_code == 404
    assert (await client.get("/assistant/conversations/not-a-uuid")).status_code == 422
    with pytest.raises(HTTPException) as wrong:
        await agent.feedback(
            FeedbackRequest(session_id=conversation_id, trace_id="0" * 32, value=1)
        )
    assert wrong.value.status_code == 404
    unconfigured = await client.post(
        "/feedback", json={"session_id": conversation_id, "trace_id": body["trace_id"], "value": 1}
    )
    assert unconfigured.status_code == 503
    agent.sessions[conversation_id].touched -= 3601
    expired = await client.get(f"/assistant/conversations/{conversation_id}")
    assert expired.status_code == 404
    assert conversation_id not in agent.sessions
    stale = message(shop_session, conversation_id, "Show my cart")
    assert (await client.post("/assistant/message", json=stale)).status_code == 404


async def test_currency_flows_into_prompt_reply_and_budget_score(agent, client, spans):
    shop_session = str(uuid4())
    body = (
        await client.post(
            "/assistant/message",
            json={**message(shop_session), "currency_code": "EUR", "budget": 100},
        )
    ).json()
    assert body["product_refs"][0]["price"]["currencyCode"] == "EUR"
    assert "€91.76 EUR" in body["reply"]
    assert "$" not in body["reply"].replace("€", "")
    assert "USD" not in body["reply"]
    recorded = spans.get_finished_spans()
    generation = next(s for s in recorded if s.name == "model.generate")
    system = json.loads(generation.attributes["langfuse.observation.input"])["system"]
    assert "EUR 100.00" in system
    evaluation = next(s for s in recorded if s.name == "evaluate.budget")
    inputs = json.loads(evaluation.attributes["langfuse.observation.input"])
    assert inputs["budget"] == 100
    assert inputs["currency_code"] == "EUR"
    assert inputs["product"]["priceUsd"]["currencyCode"] == "EUR"
    assert json.loads(evaluation.attributes["langfuse.observation.output"]) == {
        "budget_adherence": 1
    }
    turn = next(s for s in recorded if s.name == "concierge.turn")
    assert turn.attributes["langfuse.trace.metadata.currency_code"] == "EUR"
    assert turn.attributes["assistant.request_id"] == body["request_id"]
    cart = (
        await client.post(
            "/assistant/message",
            json={
                **message(shop_session, body["conversation_id"], "Add it to my cart"),
                "currency_code": "EUR",
            },
        )
    ).json()
    shown = (
        await client.post(
            "/assistant/message",
            json={
                **message(shop_session, body["conversation_id"], "Show my cart"),
                "currency_code": "EUR",
            },
        )
    ).json()
    assert cart["cart_changed"] and "€91.76 EUR" in shown["reply"]


def test_money_formatting_uses_the_returned_currency_code():
    assert format_money({"currencyCode": "USD", "units": 101, "nanos": 960000000}) == "$101.96 USD"
    assert format_money({"currencyCode": "EUR", "units": 91, "nanos": 760000000}) == "€91.76 EUR"
    assert format_money({"currencyCode": "CHF", "units": 5, "nanos": 0}) == "5.00 CHF"
    assert format_money({"units": 5, "nanos": 0}) == "$5.00 USD"


async def test_product_context_explain_calls_get_product_for_that_id(agent, client):
    shop_session = str(uuid4())
    body = (
        await client.post(
            "/assistant/message",
            json={
                **message(shop_session, text="Explain this product"),
                "product_context": {"product_id": EXPENSIVE},
            },
        )
    ).json()
    assert [t["name"] for t in body["demo"]["tools"]] == ["get_product"]
    assert body["demo"]["tools"][0]["arguments"] == {
        "product_id": EXPENSIVE,
        "currency_code": "USD",
    }
    assert body["product_refs"][0]["id"] == EXPENSIVE
    assert "Expensive Telescope" in body["reply"]
    plain = (
        await client.post(
            "/assistant/message", json=message(shop_session, text="Explain this product")
        )
    ).json()
    assert [t["name"] for t in plain["demo"]["tools"]][0] == "list_products"


def test_scripted_model_without_context_ignores_explain_requests():
    from langchain_core.messages import HumanMessage

    model = ScriptedModel(scenario="shopping", product_id=None)
    result = model._generate([HumanMessage(content="Tell me about this product")])
    assert result.generations[0].message.tool_calls[0]["name"] == "list_products"


async def test_request_validation_is_strict(client):
    base = message(str(uuid4()))
    for bad in (
        {**base, "shop_session_id": "not-a-uuid"},
        {**base, "request_id": "abc"},
        {**base, "currency_code": "euros"},
        {**base, "message": ""},
        {**base, "budget": -1},
        {**base, "product_context": {"product_id": "../x"}},
        {**base, "unexpected": 1},
        {k: v for k, v in base.items() if k != "shop_session_id"},
    ):
        response = await client.post("/assistant/message", json=bad)
        assert response.status_code == 422, bad
    assert MessageRequest.model_validate(base).scenario is None


async def test_scenario_and_budget_changes_need_a_new_conversation(agent, client):
    shop_session = str(uuid4())
    body = (await client.post("/assistant/message", json=message(shop_session))).json()
    same = message(shop_session, body["conversation_id"], "Show my cart", budget=150)
    assert (await client.post("/assistant/message", json=same)).status_code == 200
    changed = message(shop_session, body["conversation_id"], "Show my cart", budget=99)
    assert (await client.post("/assistant/message", json=changed)).status_code == 409


async def test_turn_limit_needs_a_new_conversation(agent, client):
    shop_session = str(uuid4())
    body = (await client.post("/assistant/message", json=message(shop_session))).json()
    agent.sessions[body["conversation_id"]].turns = 20
    limited = await client.post(
        "/assistant/message", json=message(shop_session, body["conversation_id"], "Show my cart")
    )
    assert limited.status_code == 409
    assert "20 turns" in limited.json()["detail"]


async def test_legacy_prompt_keeps_conversation_id_as_cart_id(agent):
    session = uuid4()
    result = await agent.handle_prompt(
        ChatRequest(session_id=session, message="Find a beginner telescope")
    )
    await agent.handle_prompt(ChatRequest(session_id=session, message="Add it to my cart"))
    assert result["session_id"] == str(session)
    assert set(result) >= {"reply", "response", "tools", "scores", "trace_id", "links", "mode"}
    assert "contract_version" not in result
    assert agent.test_carts == {str(session): [{"productId": EXPLORASCOPE, "quantity": 1}]}
    assert agent.sessions[str(session)].shop_session_id == str(session)
    assert result["tools"][0]["arguments"] == {"currency_code": "USD"}
