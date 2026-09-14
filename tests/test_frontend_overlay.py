"""Static checks on the assistant overlay and its upstream patches.

The storefront has no unit-test runner of its own (adding one would add npm dependencies), so
the parts of the assistant shell that can be checked without a browser are asserted here:
the contract version and wire limits the frontend mirrors, the theme-token rule for new
component styles, the empty-state copy, the Cypress hooks, and the one-patch-per-upstream-file
layout.
"""

import json
import re
from pathlib import Path
from typing import get_args

import pytest

from concierge import contract
from concierge.agent import FeedbackRequest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "frontend/overlay"
PATCHES = ROOT / "frontend/patches"
UPSTREAM_FRONTEND = ROOT / ".upstream/opentelemetry-demo/src/frontend"
ENVOY_TEMPLATE = ROOT / "docker/envoy.tmpl.yaml"

# Browser code: everything here is compiled into the client bundle.
ASSISTANT_SOURCES = sorted(
    path
    for pattern in (
        "components/Assistant/*",
        "providers/Assistant*",
        "gateways/Assistant*",
        "types/Assistant*",
        "utils/telemetry/Assistant*",
    )
    for path in OVERLAY.glob(pattern)
    if path.suffix in {".ts", ".tsx"}
)

# An agent, model, or Langfuse address or credential, in the forms one would take in source. The
# Langfuse span attribute names (langfuse.session.id, langfuse.observation.*) are not addresses;
# ac1 of the observability task puts them on browser spans.
ADDRESS_OR_CREDENTIAL = re.compile(
    r"agent:8010|localhost:8010|https?://[a-z]|NEXT_PUBLIC_"
    r"|LANGFUSE_(BASE_URL|PUBLIC_URL|PUBLIC_KEY|SECRET_KEY|AUTH_HEADER)|pk-lf-|sk-lf-|langfuse\.(com|io)"
    r"|LLM_BASE_URL|LLM_MODEL|api\.openai\.com|gpt-4",
    flags=re.IGNORECASE,
)

# Server code: the same-origin API routes and what they call. The released Dockerfile copies
# only fixed directories (pages/, gateways/, services/, ...), so these must live inside them.
API_ROUTES = {
    "message": OVERLAY / "pages/api/assistant/message.ts",
    "action": OVERLAY / "pages/api/assistant/action.ts",
    "feedback": OVERLAY / "pages/api/assistant/feedback.ts",
    "conversation": OVERLAY / "pages/api/assistant/conversation/[conversationId].ts",
}
AGENT_GATEWAY = OVERLAY / "gateways/http/Agent.gateway.ts"
ASSISTANT_SERVICE = OVERLAY / "services/Assistant.service.ts"
# Shared by the browser gateway and the API routes (utils/telemetry/ is copied by the Dockerfile).
TRACING = OVERLAY / "utils/telemetry/AssistantTracing.ts"
SERVER_SOURCES = [*API_ROUTES.values(), AGENT_GATEWAY, ASSISTANT_SERVICE, TRACING]

REQUIRED_CYPRESS_HOOKS = {
    "AssistantTrigger": "assistant-trigger",
    "AssistantPanel": "assistant-panel",
    "AssistantComposer": "assistant-composer",
    "AssistantSend": "assistant-send",
    "AssistantMessage": "assistant-message",
    "AssistantProductCard": "assistant-product-card",
    "AssistantSuggestion": "assistant-suggestion",
    "AssistantClose": "assistant-close",
    "AssistantCardAddToCart": "assistant-card-add-to-cart",
    "AssistantNewConversation": "assistant-new-conversation",
    "AssistantNotice": "assistant-notice",
    "AssistantUncertain": "assistant-uncertain",
}

EXPECTED_PATCH_TARGETS = {
    "0001-app-assistant-provider.patch": "src/frontend/pages/_app.tsx",
    "0002-layout-assistant-host.patch": "src/frontend/components/Layout/Layout.tsx",
    "0003-header-assistant-trigger.patch": "src/frontend/components/Header/Header.tsx",
    "0004-product-page-ask-assistant.patch": "src/frontend/pages/product/[productId]/index.tsx",
    "0005-cypress-fields-assistant.patch": "src/frontend/utils/enums/CypressFields.ts",
    "0006-document-assistant-env.patch": "src/frontend/pages/_document.tsx",
    # The released header overflows a 390 px viewport before any control is added (280 px logo,
    # 130 px currency select, 40 px margin); these let the brand shrink and tighten the switcher.
    "0007-header-styled-brand-fit.patch": "src/frontend/components/Header/Header.styled.ts",
    "0008-currency-switcher-mobile-width.patch": "src/frontend/components/CurrencySwitcher/CurrencySwitcher.styled.ts",
    # Session replay: the tracer branch that starts the ClickStack browser SDK, and the lockfile
    # entry for it. Its package.json half rides in 0000, which predates this registry.
    "0009-frontend-tracer-session-replay.patch": "src/frontend/utils/telemetry/FrontendTracer.ts",
    "0010-package-lock-hyperdx.patch": "src/frontend/package-lock.json",
}


def patch_targets(patch: Path) -> list[str]:
    return re.findall(r"^\+\+\+ b/(.+)$", patch.read_text(), flags=re.MULTILINE)


def patched_upstream(patch: Path) -> str:
    """The pinned upstream file with `patch` applied, hunk by hunk.

    Every patch is a complete diff against the pristine 3.0.0 export, so this is the file
    `demo.py stage` produces -- without staging a tree or shelling out to `git apply`. The
    context and removed lines are checked against upstream as they are consumed, so a patch that
    no longer fits the pin fails here rather than asserting against a half-applied file.
    """
    (target,) = patch_targets(patch)
    original = (UPSTREAM_FRONTEND / target.removeprefix("src/frontend/")).read_text().splitlines()
    applied: list[str] = []
    position = 0
    in_hunk = False
    for line in patch.read_text().splitlines():
        header = re.match(r"^@@ -(\d+)", line)
        if header:
            start = int(header.group(1)) - 1
            assert start >= position, f"{patch.name}: hunks are out of order"
            applied.extend(original[position:start])
            position = start
            in_hunk = True
        elif not in_hunk or line.startswith("\\"):
            continue  # the diff preamble, or "\ No newline at end of file"
        elif line.startswith("+"):
            applied.append(line[1:])
        else:
            assert line == "" or line.startswith((" ", "-")), f"{patch.name}: stray {line!r}"
            assert original[position] == line[1:], (
                f"{patch.name} does not fit upstream at line {position + 1}"
            )
            if not line.startswith("-"):
                applied.append(original[position])
            position += 1
    assert in_hunk, f"{patch.name} has no hunks"
    applied.extend(original[position:])
    return "\n".join(applied) + "\n"


def test_contract_version_mirrors_the_agent_constant():
    types = (OVERLAY / "types/Assistant.ts").read_text()
    assert re.search(r"export const ASSISTANT_CONTRACT_VERSION = ['\"]1['\"]", types)
    # The 409 reason enum is the contract's; the turn limit itself is the agent's to enforce.
    reasons = re.search(r"ASSISTANT_CONFLICT_REASONS = \[([^\]]+)\]", types)
    assert reasons and set(re.findall(r"'([^']+)'", reasons.group(1))) == set(
        get_args(contract.ConflictReason)
    )
    assert "ASSISTANT_MAX_TURNS" not in types


def test_assistant_sources_exist_and_use_theme_tokens_instead_of_theme_hex_values():
    assert ASSISTANT_SOURCES, "no assistant overlay sources found"
    theme = (UPSTREAM_FRONTEND / "styles/Theme.ts").read_text()
    theme_hex = {value.lower() for value in re.findall(r"#[0-9a-fA-F]{6}\b", theme)}
    assert theme_hex, "expected hex colors in upstream Theme.ts"
    offenders = {}
    for path in ASSISTANT_SOURCES:
        found = {value.lower() for value in re.findall(r"#[0-9a-fA-F]{3,8}\b", path.read_text())}
        duplicates = found & theme_hex
        if duplicates:
            offenders[str(path.relative_to(OVERLAY))] = sorted(duplicates)
    assert not offenders, f"hard-coded theme colors: {offenders}"
    styled = (OVERLAY / "components/Assistant/AssistantPanel.styled.ts").read_text()
    for token in ("colors.otelBlue", "colors.otelYellow", "colors.textGray", "colors.lightBorderGray", "breakpoints.desktop", "fonts."):
        assert token in styled, f"AssistantPanel.styled.ts does not reference theme.{token}"


def test_empty_state_copy_and_suggestion_chips():
    joined = "\n".join(path.read_text() for path in ASSISTANT_SOURCES)
    assert "Find the right telescope" in joined
    for chip in ("A telescope for a beginner", "Help me choose under $150", "Explain this product"):
        assert chip in joined, f"missing suggestion chip {chip!r}"


def test_no_agent_or_langfuse_address_reaches_the_browser_bundle():
    assert TRACING in ASSISTANT_SOURCES
    for path in ASSISTANT_SOURCES:
        found = ADDRESS_OR_CREDENTIAL.search(path.read_text())
        assert not found, f"{path.relative_to(OVERLAY)} contains {found.group(0)!r}"


def test_server_sources_hard_code_no_agent_or_observability_address():
    for path in SERVER_SOURCES:
        assert path.is_file(), f"missing {path.relative_to(OVERLAY)}"
        found = ADDRESS_OR_CREDENTIAL.search(path.read_text())
        assert not found, f"{path.relative_to(OVERLAY)} contains {found.group(0)!r}"


def test_address_guard_catches_the_forms_it_exists_for():
    for sample in (
        "fetch('http://agent:8010/assistant/message')",
        "const url = process.env.LANGFUSE_BASE_URL",
        "const key = 'pk-lf-0123'",
        "https://cloud.langfuse.com",
        "window.ENV.NEXT_PUBLIC_AGENT_URL",
        "model: 'gpt-4o-mini'",
    ):
        assert ADDRESS_OR_CREDENTIAL.search(sample), sample
    for sample in ("span.setAttribute('langfuse.session.id', id)", "attributes['langfuse.observation.output']"):
        assert not ADDRESS_OR_CREDENTIAL.search(sample), sample


@pytest.mark.parametrize("name,target", sorted(EXPECTED_PATCH_TARGETS.items()))
def test_each_assistant_patch_edits_exactly_its_upstream_file(name, target):
    patch = PATCHES / name
    assert patch.is_file(), f"missing {name}"
    assert patch_targets(patch) == [target]


def test_patches_touch_distinct_upstream_files():
    seen = {}
    for patch in sorted(PATCHES.glob("*.patch")):
        for target in patch_targets(patch):
            assert target not in seen, f"{patch.name} and {seen[target]} both edit {target}"
            seen[target] = patch.name


def test_assistant_cypress_spec_covers_the_native_flow_with_the_shared_hooks():
    """The browser test lands beside the released specs and drives the panel through the same
    data-cy hooks; it is run against the native stack, not here (see README, Browser tests)."""
    source = (OVERLAY / "cypress/e2e/Assistant.cy.ts").read_text()
    assert "from '../../utils/Cypress'" in source
    assert "from '../../utils/enums/CypressFields'" in source
    for member in (
        "AssistantTrigger",
        "AssistantSend",
        "AssistantMessage",
        "AssistantProductCard",
        "AssistantCardAddToCart",
        "CartItemCount",
        "CartGoToShopping",
        "AssistantClose",
        "AssistantNewConversation",
    ):
        assert f"CypressFields.{member}" in source, member
    assert "A telescope for a beginner" in source
    # Keyboard and live-region coverage at the two widths the plan names.
    assert 'aria-live="polite"' in source and "aria-modal" in source
    assert "{enter}" in source and "{esc}" in source and "key: 'Tab'" in source
    assert "width: 1440" in source and "width: 390" in source


def test_cypress_fields_patch_adds_the_assistant_hooks():
    added = "\n".join(
        line[1:]
        for line in (PATCHES / "0005-cypress-fields-assistant.patch").read_text().splitlines()
        if line.startswith("+") and not line.startswith("+++")
    )
    for member, value in REQUIRED_CYPRESS_HOOKS.items():
        assert re.search(rf"{member}\s*=\s*'{value}'", added), f"CypressFields.{member} = '{value}' not added"


def test_document_patch_injects_assistant_env_without_next_public_prefix():
    patch = (PATCHES / "0006-document-assistant-env.patch").read_text()
    assert "ASSISTANT_TRANSPORT" in patch and "ASSISTANT_DEMO_DETAILS" in patch
    assert "NEXT_PUBLIC_ASSISTANT" not in patch


# -- same-origin API routes (task 5) ---------------------------------------------------------


def ts_interface_fields(source: str, name: str) -> set[str]:
    """Top-level property names of `export interface <name> { ... }` in a TypeScript source."""
    match = re.search(rf"export interface {name}\s*\{{(.*?)^\}}", source, flags=re.DOTALL | re.MULTILINE)
    assert match, f"interface {name} not found"
    return set(re.findall(r"^\s{2}(\w+)\??:", match.group(1), flags=re.MULTILINE))


def ts_number_constant(source: str, name: str) -> int:
    """The value of `export const <name> = <integer>;` in a TypeScript source."""
    match = re.search(rf"^export const {name} = (\d+);$", source, flags=re.MULTILINE)
    assert match, f"constant {name} not found"
    return int(match.group(1))


def ts_regex_constant(source: str, name: str) -> str:
    """The pattern of `export const <name> = /<pattern>/;` (no flags) in a TypeScript source."""
    match = re.search(rf"^export const {name} = /(.+)/;$", source, flags=re.MULTILINE)
    assert match, f"regex constant {name} not found"
    return match.group(1)


def field_constraint(model, name: str, attribute: str):
    """One constraint (max_length, le, pattern, ...) of a Pydantic field, from its metadata."""
    for item in model.model_fields[name].metadata:
        if hasattr(item, attribute):
            return getattr(item, attribute)
    raise AssertionError(f"{model.__name__}.{name} has no {attribute} constraint")


@pytest.mark.parametrize("name", sorted(API_ROUTES))
def test_each_api_route_exists_and_is_wrapped_in_the_instrumentation_middleware(name):
    route = API_ROUTES[name]
    assert route.is_file(), f"missing {route.relative_to(OVERLAY)}"
    source = route.read_text()
    assert "InstrumentationMiddleware" in source
    assert re.search(r"^export default ", source, flags=re.MULTILINE)


def test_agent_base_url_is_read_server_side_at_request_time_only():
    for path in SERVER_SOURCES:
        assert path.is_file(), f"missing {path.relative_to(OVERLAY)}"
    gateway = AGENT_GATEWAY.read_text()
    assert "process.env.AGENT_BASE_URL" in gateway
    # Inside a function, not destructured once when the module loads.
    assert not re.search(r"^(const|let|var) .*process\.env", gateway, flags=re.MULTILINE)
    for path in [*ASSISTANT_SOURCES, *API_ROUTES.values(), ASSISTANT_SERVICE]:
        assert "AGENT_BASE_URL" not in path.read_text(), f"{path.relative_to(OVERLAY)} names AGENT_BASE_URL"
    for patch in PATCHES.glob("*.patch"):
        assert "src/frontend/next.config.js" not in patch_targets(patch), f"{patch.name} edits next.config.js"
        assert "AGENT_BASE_URL" not in patch.read_text(), f"{patch.name} mentions AGENT_BASE_URL"


def test_agent_deadline_sits_between_95s_and_the_proxy_route_timeout():
    gateway = AGENT_GATEWAY.read_text()
    deadline = re.search(r"AGENT_TIMEOUT_MS = ([\d_]+)", gateway)
    assert deadline, "AGENT_TIMEOUT_MS constant not found"
    deadline_ms = int(deadline.group(1).replace("_", ""))
    route = re.search(
        r'prefix: "/api/assistant/" \}\s*\n\s*route: \{ cluster: frontend-assistant, timeout: (\d+)s', ENVOY_TEMPLATE.read_text()
    )
    assert route, "Envoy /api/assistant/ route timeout not found"
    proxy_ms = int(route.group(1)) * 1000
    assert 95_000 <= deadline_ms < proxy_ms, f"deadline {deadline_ms} ms not in [95000, {proxy_ms})"
    assert "AbortSignal.timeout(AGENT_TIMEOUT_MS)" in gateway


def test_routes_forward_no_arbitrary_headers_or_body_fields():
    gateway = AGENT_GATEWAY.read_text()
    # Only a JSON content type goes upstream; trace context comes from the Node auto-instrumentation.
    assert "traceparent" not in gateway.lower()
    assert "req.headers" not in gateway and "request.headers" not in gateway
    for name, route in API_ROUTES.items():
        source = route.read_text()
        assert "req.headers" not in source and "request.headers" not in source, f"{name} forwards headers"
        # Bodies are rebuilt field by field by the service, never spread through.
        assert "...body" not in source and "...req.body" not in source, f"{name} spreads the browser body"


def test_live_transport_is_the_default_and_fixtures_stay_selectable():
    gateway = (OVERLAY / "gateways/Assistant.gateway.ts").read_text()
    types = (OVERLAY / "types/Assistant.ts").read_text()
    assert "'fixtures' ? FixtureTransport : LiveTransport" in gateway
    assert "basePath = '/api/assistant'" in gateway
    assert "import request from '../utils/Request'" in gateway
    assert re.search(r"ASSISTANT_TRANSPORTS = \['live', 'fixtures'\]", types)


def test_frontend_response_types_mirror_the_agent_contract_models():
    types = (OVERLAY / "types/Assistant.ts").read_text()
    assert ts_interface_fields(types, "AssistantResponse") == set(contract.AssistantResponse.model_fields)
    assert ts_interface_fields(types, "AssistantDemo") == set(contract.DemoDetails.model_fields)
    assert ts_interface_fields(types, "AssistantDemoToolCall") == set(contract.DemoToolCall.model_fields)
    assert ts_interface_fields(types, "AssistantProductRef") == set(contract.ProductRef.model_fields)
    assert ts_interface_fields(types, "AssistantConversationStatus") == set(contract.ConversationStatus.model_fields)
    assert ts_interface_fields(types, "AssistantMessageRequest") == set(contract.MessageRequest.model_fields)
    assert ts_interface_fields(types, "AssistantActionRequest") == set(contract.AddToCartAction.model_fields)
    scenarios = re.search(r"ASSISTANT_SCENARIOS = \[([^\]]+)\]", types)
    assert scenarios and set(re.findall(r"'([^']+)'", scenarios.group(1))) == set(get_args(contract.Scenario))


def test_frontend_wire_limits_mirror_the_agent_contract_fields():
    """The storefront pre-validates with the agent's own limits, so a body it forwards is never
    422'd for size or shape and a body it rejects would have been rejected upstream too. Each
    constant is pinned to the Pydantic field it copies; the service imports them, so the values
    live in one place on the frontend side."""
    types = (OVERLAY / "types/Assistant.ts").read_text()
    assert ts_number_constant(types, "ASSISTANT_MESSAGE_MAX_LENGTH") == field_constraint(
        contract.MessageRequest, "message", "max_length"
    )
    assert ts_number_constant(types, "ASSISTANT_BUDGET_MAX") == field_constraint(contract.MessageRequest, "budget", "le")
    assert ts_number_constant(types, "ASSISTANT_QUANTITY_MAX") == field_constraint(contract.AddToCartAction, "quantity", "le")
    assert ts_regex_constant(types, "ASSISTANT_CURRENCY_CODE_PATTERN") == field_constraint(
        contract.MessageRequest, "currency_code", "pattern"
    )
    assert field_constraint(contract.AddToCartAction, "currency_code", "pattern") == field_constraint(
        contract.MessageRequest, "currency_code", "pattern"
    )
    # ProductId keeps its length bounds as min_length/max_length beside an unbounded pattern; the
    # storefront folds the three into one quantifier.
    product_pattern = field_constraint(contract.AddToCartAction, "product_id", "pattern")
    assert product_pattern.endswith("+$"), product_pattern
    bounds = "{%d,%d}" % (
        field_constraint(contract.AddToCartAction, "product_id", "min_length"),
        field_constraint(contract.AddToCartAction, "product_id", "max_length"),
    )
    assert ts_regex_constant(types, "ASSISTANT_PRODUCT_ID_PATTERN") == product_pattern[:-2] + bounds + "$"
    assert ts_regex_constant(types, "ASSISTANT_TRACE_ID_PATTERN") == field_constraint(FeedbackRequest, "trace_id", "pattern")
    # The service enforces these and declares none of its own.
    service = ASSISTANT_SERVICE.read_text()
    for name in (
        "ASSISTANT_MESSAGE_MAX_LENGTH",
        "ASSISTANT_BUDGET_MAX",
        "ASSISTANT_QUANTITY_MAX",
        "ASSISTANT_CURRENCY_CODE_PATTERN",
        "ASSISTANT_PRODUCT_ID_PATTERN",
        "ASSISTANT_TRACE_ID_PATTERN",
    ):
        assert re.search(rf"^\s+{name},$", service, flags=re.MULTILINE), f"service does not import {name}"
        assert service.count(name) >= 2, f"service imports {name} but never uses it"
    assert not re.search(r"^const (MESSAGE_MAX_LENGTH|BUDGET_MAX|QUANTITY_MAX|CURRENCY_CODE|PRODUCT_ID|TRACE_ID) =", service, flags=re.MULTILINE)


def test_composer_ui_cap_stays_within_the_agent_message_limit():
    """The composer's maxLength is a UI cap on what a shopper types, distinct from the contract's
    ASSISTANT_MESSAGE_MAX_LENGTH the service enforces on the wire, and never above it. It is not
    exported, and no overlay file uses the bare MESSAGE_MAX_LENGTH name, so a grep for the message
    limit finds one governing constant."""
    composer = (OVERLAY / "components/Assistant/Composer.tsx").read_text()
    match = re.search(r"^const COMPOSER_MAX_LENGTH = (\d+);$", composer, flags=re.MULTILINE)
    assert match, "Composer.tsx does not declare a non-exported COMPOSER_MAX_LENGTH"
    assert int(match.group(1)) <= field_constraint(contract.MessageRequest, "message", "max_length")
    assert "maxLength={COMPOSER_MAX_LENGTH}" in composer
    for path in sorted(OVERLAY.rglob("*")):
        if path.suffix in {".ts", ".tsx"}:
            assert not re.search(r"\bMESSAGE_MAX_LENGTH\b", path.read_text()), f"{path.relative_to(OVERLAY)} names MESSAGE_MAX_LENGTH"


def test_demo_details_render_scenario_prompt_tools_and_links():
    source = (OVERLAY / "components/Assistant/DemoDetails.tsx").read_text()
    for field in ("demo.scenario", "demo.prompt_version", "demo.prompt_source", "demo.tools", "demo.links"):
        assert field in source, f"DemoDetails does not render {field}"
    assert "tool.name" in source


def test_every_assistant_answer_offers_feedback():
    """Every answer renders Feedback with the response's feedback_enabled. When the agent said
    scores are not stored, Feedback.tsx says so up front, with the same 'Feedback not saved' line
    a rejected click shows, and keeps both buttons, so a click still reports the agent's answer
    and the Cypress hook stays in place."""
    transcript = (OVERLAY / "components/Assistant/Transcript.tsx").read_text()
    assert re.search(r"entry\.kind === 'assistant' \? <Feedback", transcript)
    assert "enabled={response.feedback_enabled}" in transcript
    feedback = (OVERLAY / "components/Assistant/Feedback.tsx").read_text()
    assert "enabled: boolean;" in feedback
    assert re.search(r"enabled \? 'Was this helpful\?' : `\$\{NOT_SAVED\} ", feedback)
    assert feedback.count("data-state=\"not_saved\"") == 1, "the up-front hint must not claim the post-click not_saved state"
    assert not re.search(r"if \(!enabled\)", feedback), "a disabled score store must not hide the buttons"
    assert feedback.count("submitFeedback(entryId, true)") == 1
    assert feedback.count("submitFeedback(entryId, false)") == 1


def test_fixtures_include_an_answer_whose_score_is_not_stored():
    """The fixture transport shows the up-front hint on a real page: one keyword answers with
    feedback_enabled false, and a click on that answer comes back not saved, as the live agent would."""
    fixtures = (OVERLAY / "gateways/AssistantFixtures.ts").read_text()
    assert "feedback_enabled: feedbackEnabled," in fixtures
    assert "feedbackEnabled: false" in fixtures
    assert re.search(r"return \{ saved: false, message: ", fixtures)


# -- shared shopping state (task 6) ----------------------------------------------------------

PROVIDER = OVERLAY / "providers/Assistant.provider.tsx"
BROWSER_GATEWAY = OVERLAY / "gateways/Assistant.gateway.ts"
SESSION_GATEWAY = OVERLAY / "gateways/AssistantSession.gateway.ts"
CARD = OVERLAY / "components/Assistant/AssistantProductCard.tsx"
TRANSCRIPT = OVERLAY / "components/Assistant/Transcript.tsx"
PANEL = OVERLAY / "components/Assistant/AssistantPanel.tsx"


def test_cards_add_through_the_action_route_and_never_the_storefront_cart_mutation():
    card = CARD.read_text()
    assert "onClick={() => addToCart(product)}" in card
    assert "post<AssistantResponse>('action', action)" in BROWSER_GATEWAY.read_text()
    joined = "\n".join(path.read_text() for path in ASSISTANT_SOURCES)
    # The storefront's own cart mutation (CartProvider.addItem -> ApiGateway.addCartItem) is never called.
    assert "addCartItem" not in joined
    assert not re.search(r"\baddItem\(", joined)
    provider = PROVIDER.read_text()
    # A second click before React re-renders the disabled control must not start a second request.
    assert "inFlightRef" in provider


def test_provider_refetches_the_cart_when_it_changed_and_when_an_action_is_uncertain():
    provider = PROVIDER.read_text()
    assert "response.cart_changed" in provider
    assert provider.count("invalidateQueries({ queryKey: ['cart'] })") >= 2


def test_timed_out_cart_action_is_shown_as_uncertain_and_never_retried_automatically():
    provider = PROVIDER.read_text()
    assert "failure.code === 'timeout'" in provider
    assert "setUnresolved(turn)" in provider
    # Nothing schedules a retry; the only retry paths are the shopper's controls.
    assert "setTimeout" not in provider and "setInterval" not in provider
    transcript = TRANSCRIPT.read_text()
    assert "CypressFields.AssistantUncertain" in transcript
    assert "onClick={retryUncertain}" in transcript
    assert "onClick={dismissUncertain}" in transcript
    # The cart snapshot taken before the action is what confirms it afterwards.
    assert "quantityBefore" in provider


def test_conversation_is_resumed_only_after_the_status_route_confirms_it():
    types = (OVERLAY / "types/Assistant.ts").read_text()
    assert "getConversation(conversationId: string, shopSessionId: string): Promise<AssistantConversationStatus>" in types
    gateway = BROWSER_GATEWAY.read_text()
    assert "conversation/${encodeURIComponent(conversationId)}" in gateway
    provider = PROVIDER.read_text()
    assert ".getConversation(" in provider
    assert "previous conversation expired" in provider
    # Ownership is the agent's call: the caller's session travels with the request and a 409
    # reason 'foreign' starts fresh; the status body no longer names a shop session to compare.
    resolve_stored = re.search(r"^const resolveStored = .*?^\};", provider, flags=re.DOTALL | re.MULTILINE)
    assert resolve_stored, "resolveStored not found in the provider"
    assert "reason === 'foreign' ? FOREIGN_NOTE" in resolve_stored.group(0)
    assert "shop_session_id" not in resolve_stored.group(0)
    assert "shop_session_id" not in ts_interface_fields(types, "AssistantConversationStatus")
    # Persistence goes through one gateway, like the storefront's Session.gateway.
    assert SESSION_GATEWAY.is_file()
    assert "localStorage" in SESSION_GATEWAY.read_text()
    assert "localStorage" not in provider


def test_status_route_passes_the_callers_validated_session_to_the_agent():
    route = API_ROUTES["conversation"].read_text()
    service = ASSISTANT_SERVICE.read_text()
    gateway = AGENT_GATEWAY.read_text()
    # Both query parameters go through the service's uuid() check before anything is called.
    assert "parseConversationQuery(query)" in route
    assert "uuid(query, 'conversationId')" in service
    assert "uuid(query, 'shop_session_id')" in service
    assert "shop_session_id: shopSessionId" in gateway
    # The browser gateway sends the session the same way, as a query parameter of the same name.
    browser = BROWSER_GATEWAY.read_text()
    assert "method: 'GET', queryParams" in browser
    assert "{ shop_session_id: shopSessionId }" in browser


def test_conflicts_are_classified_from_the_agent_reason_without_a_status_round_trip():
    # The server-side gateway reads the agent's structured 409 and decides retryability from
    # the reason alone: only a turn in flight clears on its own.
    gateway = AGENT_GATEWAY.read_text()
    assert "reason === 'in_flight'" in gateway
    assert re.search(r"new AssistantRouteError\(409, 'conflict', [^;]*, reason\)", gateway)
    # The reason travels in the route's error envelope and into the browser's AssistantError.
    assert "if (this.reason) error.reason = this.reason;" in ASSISTANT_SERVICE.read_text()
    assert "reason?: AssistantConflictReason" in (OVERLAY / "types/Assistant.ts").read_text()
    assert "payload.error.reason" in BROWSER_GATEWAY.read_text()
    # The provider branches on it: a foreign conversation starts fresh with the message back in
    # the composer; every other 409 is shown with the retryability the gateway decided.
    provider = PROVIDER.read_text()
    assert "failure.reason === 'foreign'" in provider
    assert "FOREIGN_NOTE : EXPIRED_NOTE" in provider
    assert "conflictKind" not in provider
    assert "ASSISTANT_MAX_TURNS" not in provider
    # The status route is read once, to resume a stored conversation, never to explain a 409.
    assert provider.count(".getConversation(") == 1


def test_new_conversation_clears_the_transcript_and_leaves_the_cart_alone():
    panel = PANEL.read_text()
    assert "CypressFields.AssistantNewConversation" in panel
    assert "onClick={newConversation}" in panel
    provider = PROVIDER.read_text()
    start = re.search(r"const startFresh = useCallback\((.*?)\n  \);", provider, flags=re.DOTALL)
    assert start, "startFresh not found"
    assert "setConversationId(null)" in start.group(1)
    assert "setTranscript(" in start.group(1)
    assert "cart" not in start.group(1)


def test_card_prices_follow_the_selected_currency_from_the_catalog():
    card = CARD.read_text()
    # Same query key as the product page, so the two share one cache entry per currency.
    assert "['product', product.id, 'selectedCurrency', currencyCode]" in card
    assert "ApiGateway.getProduct(product.id, currencyCode)" in card
    # The agent's amount is shown with its own currency until the catalog answers; never relabeled.
    assert "product.price.currencyCode === currencyCode" in card


# -- observability (task 7) ------------------------------------------------------------------

UPSTREAM_TRACER = UPSTREAM_FRONTEND / "utils/telemetry/FrontendTracer.ts"
TURN_ATTRIBUTES = ("gen_ai.conversation.id", "langfuse.session.id", "assistant.request_id", "assistant.contract_version")


def test_turn_span_comes_from_the_global_tracer_and_wraps_the_request():
    tracing = TRACING.read_text()
    assert re.search(r"import \{[^}]*\btrace\b[^}]*\} from '@opentelemetry/api'", tracing)
    assert "TURN_SPAN_NAME = 'assistant.turn'" in tracing
    assert re.search(r"trace\s*\.getTracer\('[^']+'\)\s*\.startSpan\(TURN_SPAN_NAME", tracing)
    # The request runs inside the span's context, so the fetch span is its child.
    assert "trace.setSpan(context.active(), span)" in tracing
    assert "await context.with(turnContext, run)" in tracing
    # Ends only once the request settled; a rejection is an ERROR and is rethrown.
    assert "SpanStatusCode.ERROR" in tracing
    assert re.search(r"throw error;\s*\} finally \{\s*span\.end\(\);", tracing)
    for attribute in TURN_ATTRIBUTES:
        assert f"'{attribute}'" in tracing, attribute
    assert "AttributeNames.SESSION_ID" in tracing
    # Both browser turns go through it; nothing else in the browser sources starts a span.
    gateway = BROWSER_GATEWAY.read_text()
    assert "withAssistantTurn('message', identity, () => post<AssistantResponse>('message', message))" in gateway
    assert "withAssistantTurn('action', identity, () => post<AssistantResponse>('action', action))" in gateway
    assert "shopSessionId: message.shop_session_id" in gateway
    assert "shopSessionId: action.shop_session_id" in gateway
    for path in ASSISTANT_SOURCES:
        if path != TRACING:
            assert "startSpan(" not in path.read_text(), f"{path.relative_to(OVERLAY)} starts its own span"


def test_api_routes_record_the_turn_identity_and_contract_version():
    message = API_ROUTES["message"].read_text()
    assert "shopSessionId: message.shop_session_id, requestId: message.request_id" in message
    assert "recordTurnOnActiveSpan({ conversationId: response.conversation_id, contractVersion: response.contract_version })" in message
    action = API_ROUTES["action"].read_text()
    assert "shopSessionId: action.shop_session_id, requestId: action.request_id" in action
    assert "recordTurnOnActiveSpan({ contractVersion: response.contract_version })" in action
    tracing = TRACING.read_text()
    assert "trace.getSpan(context.active())?.setAttributes(turnAttributes(identity))" in tracing


def test_no_second_browser_tracing_provider_or_fetch_instrumentation():
    released = UPSTREAM_TRACER.read_text()
    assert released.count("new WebTracerProvider(") == 1
    assert released.count("'@opentelemetry/instrumentation-fetch'") == 1
    # The released tracer is changed, if at all, by a checked patch, never shadowed by an overlay file.
    assert not (OVERLAY / "utils/telemetry/FrontendTracer.ts").exists()
    # Code forms, not words: comments may name the released provider they rely on.
    setup_markers = (
        "new WebTracerProvider(",
        "registerInstrumentations(",
        "getWebAutoInstrumentations(",
        "new OTLPTraceExporter(",
        "new BatchSpanProcessor(",
        "new ZoneContextManager(",
        "'@opentelemetry/instrumentation-fetch'",
        "'@opentelemetry/sdk-trace-web'",
        "'@opentelemetry/sdk-trace-base'",
        "'@opentelemetry/exporter-trace-otlp-http'",
        ".register({",
    )
    for path in OVERLAY.rglob("*.ts*"):
        source = path.read_text()
        for marker in setup_markers:
            assert marker not in source, f"{path.relative_to(OVERLAY)} sets up browser tracing ({marker})"
    for patch in PATCHES.glob("*.patch"):
        added = "\n".join(line for line in patch.read_text().splitlines() if line.startswith("+") and not line.startswith("+++"))
        for marker in setup_markers:
            assert marker not in added, f"{patch.name} adds browser tracing setup ({marker})"


# -- session replay --------------------------------------------------------------------------

TRACER_PATCH = PATCHES / "0009-frontend-tracer-session-replay.patch"
LOCK_PATCH = PATCHES / "0010-package-lock-hyperdx.patch"
PACKAGE_PATCH = PATCHES / "0000-package-lint-and-hyperdx.patch"
APP_PATCH = PATCHES / "0001-app-assistant-provider.patch"
DOCUMENT_PATCH = PATCHES / "0006-document-assistant-env.patch"
REPLAY_SDK = "@hyperdx/browser"
REPLAY_SDK_VERSION = "0.25.1"
REPLAY_FLAG = "NEXT_PUBLIC_HYPERDX_ENABLED"
# What AssistantTracing.ts calls on the global @opentelemetry/api registrations: the tracer, the
# active context, and the baggage propagator. Whichever provider FrontendTracer installs has to
# leave all three working, or assistant.turn stops being the parent of the fetch it wraps.
API_ENTRY_POINTS = (
    r"trace\s*\.getTracer\(",
    r"trace\.getSpan\(context\.active\(\)",
    r"trace\.setSpan\(context\.active\(\)",
    r"context\.with\(",
    r"propagation\.getActiveBaggage\(",
    r"propagation\.setBaggage\(",
)


def replay_branch(tracer: str) -> str:
    """The body of the patched tracer's `if (NEXT_PUBLIC_HYPERDX_ENABLED === 'true')` block."""
    branch = re.search(
        rf"^  if \({REPLAY_FLAG} === 'true'\) \{{\n(.*?)\n  \}}\n",
        tracer,
        re.MULTILINE | re.DOTALL,
    )
    assert branch, "the patched tracer has no session-replay branch"
    return branch.group(1)


def test_replay_branch_exports_same_origin_and_leaves_the_released_tracer_to_the_other_mode():
    tracer = patched_upstream(TRACER_PATCH)
    body = replay_branch(tracer)
    assert f"await import('{REPLAY_SDK}')" in body
    assert "HyperDX.init({" in body
    # Same-origin export: the base URL is the page's own origin, which the proxy routes to the
    # collector, so there is no CORS preflight and no collector credential in the bundle.
    assert "url: NEXT_PUBLIC_HYPERDX_URL || `${window.location.origin}/otlp-http`" in body
    # Propagation is not restricted to a cross-origin allow-list, so traceparent rides the
    # storefront's own /api/assistant fetches -- the hop the assistant turn depends on.
    assert "tracePropagationTargets: [/.*/]" in body
    assert "service: NEXT_PUBLIC_OTEL_SERVICE_NAME" in body
    assert "'demo.synthetic_request': IS_SYNTHETIC_REQUEST" in body
    # The SessionIdProcessor does not run in this mode, so the storefront's id comes back as a
    # global attribute under both the name the storefront uses and the one the processor set.
    assert "HyperDX.setGlobalAttributes({ userId, 'session.id': userId })" in body
    # The branch returns: the SDK registers its own provider globally, and building the demo's
    # as well would double-instrument fetch and export every request twice.
    assert body.rstrip().endswith("return;")


def test_replay_patch_only_adds_to_the_released_tracer_and_keeps_the_api_entry_points():
    removed = [
        line
        for line in TRACER_PATCH.read_text().splitlines()
        if line.startswith("-") and not line.startswith("---")
    ]
    assert removed == [], "the replay patch must leave the released tracer path untouched"
    tracer = patched_upstream(TRACER_PATCH)
    # One provider each: the released one for the default mode, the SDK's inside the branch.
    assert tracer.count("new WebTracerProvider(") == 1
    assert tracer.count("registerInstrumentations(") == 1
    # Neither mode tears down or replaces the global registrations AssistantTracing.ts reaches
    # through @opentelemetry/api; both leave them to the SDK that owns the provider.
    for hostile in (
        "trace.disable(",
        "context.disable(",
        "propagation.disable(",
        "trace.setGlobalTracerProvider(",
        "context.setGlobalContextManager(",
        "propagation.setGlobalPropagator(",
    ):
        assert hostile not in tracer, hostile
    tracing = TRACING.read_text()
    assert re.search(r"import \{[^}]*\bcontext\b[^}]*\} from '@opentelemetry/api'", tracing)
    for entry_point in API_ENTRY_POINTS:
        assert re.search(entry_point, tracing), entry_point


def test_replay_flag_travels_from_the_service_environment_to_the_tracer():
    app = patched_upstream(APP_PATCH)
    for key in (REPLAY_FLAG, "NEXT_PUBLIC_HYPERDX_URL"):
        assert f"{key}?: string;" in app, key
    document = patched_upstream(DOCUMENT_PATCH)
    # Read from the frontend service environment when the server starts, like the assistant
    # switches beside them, and handed to the browser under the names the tracer reads.
    assert "PUBLIC_HYPERDX_ENABLED = ''," in document
    assert "PUBLIC_HYPERDX_URL = ''," in document
    assert f"{REPLAY_FLAG}: '${{PUBLIC_HYPERDX_ENABLED}}'," in document
    assert "NEXT_PUBLIC_HYPERDX_URL: '${PUBLIC_HYPERDX_URL}'," in document
    assert REPLAY_FLAG in patched_upstream(TRACER_PATCH)


def test_replay_sdk_is_a_resolved_dependency_of_the_staged_tree():
    package = json.loads(patched_upstream(PACKAGE_PATCH))
    assert package["dependencies"][REPLAY_SDK] == f"^{REPLAY_SDK_VERSION}"
    assert package["scripts"]["lint"] == "eslint .", "0000 still carries the lint script"
    lock = json.loads(patched_upstream(LOCK_PATCH))
    assert lock["packages"][""]["dependencies"][REPLAY_SDK] == f"^{REPLAY_SDK_VERSION}"
    sdk = lock["packages"][f"node_modules/{REPLAY_SDK}"]
    assert sdk["version"] == REPLAY_SDK_VERSION
    # npm resolved the whole subtree: every dependency the SDK names is locked at the version it
    # asks for, with an integrity hash. A hand-merged lockfile fails `npm ci` here instead.
    for dependency, version in sdk["dependencies"].items():
        entry = lock["packages"][f"node_modules/{dependency}"]
        assert entry["version"] == version, dependency
        assert entry["integrity"].startswith("sha512-"), dependency
