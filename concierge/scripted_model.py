import json
from typing import Any
from uuid import uuid4

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult

EXPLORASCOPE = "OLJCESPC7Z"
EXPENSIVE = "66VCHSJNUP"


def tool_data(content):
    if (
        isinstance(content, list)
        and content
        and all(
            isinstance(block, dict) and block.get("type") == "text" and "text" in block
            for block in content
        )
    ):
        content = "".join(block["text"] for block in content)
    if isinstance(content, str):
        try:
            return json.loads(content)
        except ValueError:
            return content
    return content


SYMBOLS = {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥", "CAD": "$", "INR": "₹"}
EXPLAIN_WORDS = ("explain", "tell me about", "this product")


def money(product):
    return product.get("priceUsd") or product.get("price") or {}


def amount(money_value):
    return float(money_value.get("units", 0)) + float(money_value.get("nanos", 0)) / 1_000_000_000


def price(product):
    return amount(money(product))


def format_amount(value, code):
    return f"{SYMBOLS.get(code, '')}{value:.2f} {code}"


def format_money(money_value):
    """Format a shop Money value with the currency code the shop returned."""
    return format_amount(amount(money_value), money_value.get("currencyCode") or "USD")


class ScriptedModel(BaseChatModel):
    """A deterministic tool-calling fixture. It never calls a model provider."""

    scenario: str = "shopping"
    product_id: str | None = None

    @property
    def _llm_type(self):
        return "scripted-demo"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs: Any):
        latest = max(i for i, m in enumerate(messages) if isinstance(m, HumanMessage))
        current = messages[latest:]
        text = str(current[0].content).lower()
        tool_messages = [m for m in current if isinstance(m, ToolMessage)]

        def call(name, why, **args):
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": args,
                        "id": uuid4().hex,
                        "type": "tool_call",
                    }
                ],
                additional_kwargs={"reasoning_content": why},
            )

        def say(content, why):
            return AIMessage(content=content, additional_kwargs={"reasoning_content": why})

        if not tool_messages:
            if text.startswith("add ") or text == "add it to my cart":
                previous = [
                    tool_data(m.content)
                    for m in messages[:latest]
                    if isinstance(m, ToolMessage) and m.name == "get_product"
                ]
                selected = previous[-1] if previous else None
                if isinstance(selected, dict) and selected.get("id") and not selected.get("error"):
                    reply = call(
                        "add_to_cart",
                        "The shopper asked to add the product they just looked at, so I will add it to the cart.",
                        product_id=selected["id"],
                        quantity=1,
                    )
                elif self.product_id:
                    # Nothing looked up yet: "add this" means the product the shopper is viewing.
                    reply = call(
                        "add_to_cart",
                        "Nothing was looked up yet, so I will add the product the shopper is viewing.",
                        product_id=self.product_id,
                        quantity=1,
                    )
                else:
                    reply = say(
                        "Let's look up a product first. Use the sample shopping request, then ask me to add the recommendation.",
                        "There is no product to add yet, so I will ask the shopper to pick one first.",
                    )
            elif "cart" in text:
                reply = call("get_cart", "The shopper asked about their cart, so I will read it.")
            elif self.product_id and any(word in text for word in EXPLAIN_WORDS):
                reply = call(
                    "get_product",
                    "The shopper wants to know about the product they are viewing, so I will fetch its details.",
                    product_id=self.product_id,
                )
            elif self.scenario == "backend-failure":
                reply = call(
                    "get_product",
                    "This scenario exercises a backend failure, so I will look up a product and expect an error.",
                    product_id=EXPLORASCOPE,
                )
            elif "gift" in text and len(messages) <= 2 and "beginner" not in text:
                reply = say(
                    "Is this for someone starting out, or an experienced observer?",
                    "A gift request does not say how experienced the recipient is, so I will ask before recommending.",
                )
            else:
                reply = call(
                    "list_products",
                    "I need to see what the shop sells before I can recommend anything.",
                )
        else:
            last = tool_messages[-1]
            data = tool_data(last.content)
            if isinstance(data, dict) and data.get("error"):
                reply = say(
                    "I couldn't complete that shop request. The tool reported a backend error. Nothing has been confirmed; would you like to try again after the service recovers?",
                    "The tool returned an error, so I must not claim success and will say so.",
                )
            elif last.name == "list_products":
                selected = EXPENSIVE if self.scenario == "budget-violation" else EXPLORASCOPE
                reply = call(
                    "get_product",
                    "The catalog is listed; I will pick one product and fetch its price and details.",
                    product_id=selected,
                )
            elif last.name == "get_product" and isinstance(data, dict):
                name = data.get("name", "the selected product")
                cost = format_money(money(data))
                description = data.get("description", "").split(". ")[0].rstrip(".")
                if self.product_id and data.get("id") == self.product_id:
                    reply = say(
                        f"The **{name}** costs **{cost}**. {data.get('description', description)} Shipping and taxes are additional. Would you like me to add one to your cart?",
                        "I have the product the shopper is viewing, so I can explain it and offer to add it.",
                    )
                else:
                    reply = say(
                        f"I recommend the **{name}** for **{cost}**. {description}. Shipping and taxes are additional. Would you like me to add one to your cart?",
                        "I have the product details and price, so I can make the recommendation.",
                    )
            elif last.name == "add_to_cart":
                reply = say(
                    "Added one to this conversation's cart. Ask me to show your cart to review it.",
                    "The cart update succeeded, so I will confirm it.",
                )
            elif last.name == "get_cart":
                items = data.get("items", []) if isinstance(data, dict) else []
                if not items:
                    reply = say(
                        "Your cart is empty. Let's find something that fits your budget.",
                        "The cart has no items, so I will say so and steer back to shopping.",
                    )
                else:
                    lines = []
                    total = 0.0
                    code = money(items[0].get("product", {})).get("currencyCode") or "USD"
                    for item in items:
                        product = item.get("product", {})
                        quantity = item.get("quantity", 1)
                        line = price(product) * quantity
                        total += line
                        lines.append(
                            f"- {quantity} × {product.get('name', item.get('productId', 'Item'))} — {format_amount(line, code)}"
                        )
                    reply = say(
                        "Your cart:\n\n"
                        + "\n".join(lines)
                        + f"\n\n**Subtotal: {format_amount(total, code)}.** Shipping and taxes are additional.",
                        "The cart has items, so I will list each line and the subtotal.",
                    )
            else:
                reply = say(
                    "I couldn't interpret that result. Please try one of the sample shopping requests.",
                    "The tool result was not in a shape I recognize, so I will ask for a supported request.",
                )
        return ChatResult(generations=[ChatGeneration(message=reply)])
