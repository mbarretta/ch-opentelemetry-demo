"""Storefront assistant contract (version "1").

Request and response models for the native storefront assistant endpoints on the agent:

- ``POST /assistant/message`` runs one conversation turn (``MessageRequest``).
- ``POST /assistant/actions/add-to-cart`` performs one scoped cart mutation (``AddToCartAction``).
- ``GET /assistant/conversations/{id}`` reports a live conversation (``ConversationStatus``).

Both POST routes answer with ``AssistantResponse``. ``product_refs`` and ``cart_changed`` are
derived only from successful tool results of that turn; nothing is parsed from the reply
prose and no model-generated price or URL is echoed. The frontend copies ``CONTRACT_VERSION``
as ``ASSISTANT_CONTRACT_VERSION`` and treats a mismatch as a recoverable error.

Conversation lifetime
---------------------

Conversations live in the agent process memory only:

- A conversation is bound to the storefront ``shop_session_id`` (the cart) when it is created
  and can never be rebound (409 ``foreign``). Every turn (message or cart action) names that
  session and is refused when it names another one, so a leaked ``conversation_id`` alone
  cannot change the cart it is bound to. Its scenario and budget are fixed at creation too; a
  turn that asks for different ones is refused (409 ``rebind``).
- It expires after one hour without a turn, or after 20 turns (an add-to-cart action counts as
  a turn; a further turn is refused with 409 ``turn_limit``). Requests for an unknown or
  expired conversation return 404; the client then starts a new conversation.
- ``request_id`` values are scoped per conversation and dropped with it. Repeating a request
  with the same ``request_id`` returns the stored response without running the agent or the
  tools again; only one turn may be in flight per conversation (409 ``in_flight`` otherwise).
- Every 409 on these routes carries ``{"detail": {"message", "reason"}}`` with the reason from
  ``ConflictReason``, so the storefront classifies it without reading the status route: only
  ``in_flight`` clears on its own and may be retried. The legacy ``POST /prompt`` route keeps
  its plain-string ``detail``.
- An agent restart loses every conversation and every stored request id. A mutation that was
  in flight during the restart has an uncertain outcome: the retry cannot be deduplicated, so
  the client must refresh the cart and show the outcome as uncertain instead of retrying
  automatically.
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

CONTRACT_VERSION = "1"
IDLE_SECONDS = 3600
MAX_TURNS = 20
Scenario = Literal["shopping", "backend-failure", "budget-violation"]
# Why a turn was refused with 409 (the frontend mirrors this as ASSISTANT_CONFLICT_REASONS).
ConflictReason = Literal["foreign", "in_flight", "turn_limit", "rebind"]
CurrencyCode = Field(default="USD", pattern=r"^[A-Z]{3}$")
ProductId = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProductContext(StrictModel):
    product_id: str = ProductId


class MessageRequest(StrictModel):
    conversation_id: UUID | None = None
    shop_session_id: UUID
    request_id: UUID
    message: str = Field(min_length=1, max_length=4000)
    currency_code: str = CurrencyCode
    product_context: ProductContext | None = None
    scenario: Scenario | None = None
    budget: float | None = Field(default=None, gt=0, le=100000, allow_inf_nan=False)


class AddToCartAction(StrictModel):
    conversation_id: UUID
    shop_session_id: UUID
    request_id: UUID
    product_id: str = ProductId
    quantity: int = Field(default=1, ge=1, le=10)
    currency_code: str = CurrencyCode


class Money(BaseModel):
    currencyCode: str = Field(pattern=r"^[A-Z]{3}$")
    units: int = 0
    nanos: int = 0


class ProductRef(BaseModel):
    id: str
    name: str
    picture: str = ""
    price: Money


class DemoToolCall(BaseModel):
    name: str
    arguments: dict
    ok: bool


class DemoDetails(BaseModel):
    mode: str
    scenario: str
    prompt_version: int | str
    prompt_source: str
    tools: list[DemoToolCall]
    links: dict[str, str]


class AssistantResponse(BaseModel):
    contract_version: Literal["1"] = CONTRACT_VERSION
    conversation_id: str
    request_id: str
    reply: str
    product_refs: list[ProductRef]
    cart_changed: bool
    trace_id: str
    feedback_enabled: bool
    demo: DemoDetails


class ConversationStatus(BaseModel):
    conversation_id: str
    shop_session_id: str
    turns: int
    currency_code: str
    expires_at: datetime


def product_ref(data):
    """Validate one shop product payload into a ProductRef, or None if it is not one."""
    if not isinstance(data, dict) or data.get("error"):
        return None
    try:
        return ProductRef(
            id=data["id"],
            name=data["name"],
            picture=data.get("picture") or "",
            price=Money.model_validate(data.get("priceUsd") or data.get("price") or {}),
        )
    except KeyError, TypeError, ValidationError:
        return None


def product_refs(calls):
    """Product references from this turn's successful catalog tool results, in call order.

    Looked-up products (``get_product``) win over the catalog listing (``list_products``): a
    turn that lists the catalog and then inspects one product recommends that product. A turn
    that only lists the catalog references every product it listed.
    """
    looked_up, listed = [], []
    for call in calls:
        result = call["result"]
        if call["name"] == "get_product":
            looked_up.append(product_ref(result))
        elif call["name"] == "list_products" and isinstance(result, list):
            listed.extend(product_ref(item) for item in result)
    refs = {}
    for ref in [r for r in looked_up if r] or [r for r in listed if r]:
        refs.setdefault(ref.id, ref)
    return list(refs.values())


def cart_changed(calls):
    """True only when an add_to_cart call in this turn returned a non-error result."""
    return any(
        call["name"] == "add_to_cart"
        and isinstance(call["result"], dict)
        and not call["result"].get("error")
        for call in calls
    )


def demo_tools(calls):
    """Model-visible tool calls for the demo panel: the injected cart identity is omitted."""
    return [
        DemoToolCall(
            name=call["name"],
            arguments={k: v for k, v in call["arguments"].items() if k != "user_id"},
            ok=not (isinstance(call["result"], dict) and call["result"].get("error")),
        )
        for call in calls
    ]
