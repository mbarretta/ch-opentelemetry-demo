"""Static checks on the assistant overlay and its upstream patches.

The storefront has no unit-test runner of its own (adding one would add npm dependencies), so
the parts of the assistant shell that can be checked without a browser are asserted here:
the contract version the frontend mirrors, the theme-token rule for new component styles,
the empty-state copy, the Cypress hooks, and the one-patch-per-upstream-file layout.
"""

import re
from pathlib import Path
from typing import get_args

import pytest

from concierge import contract

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "frontend/overlay"
PATCHES = ROOT / "frontend/patches"
UPSTREAM_FRONTEND = ROOT / ".upstream/opentelemetry-demo/src/frontend"
ENVOY_TEMPLATE = ROOT / "docker/envoy.tmpl.yaml"

# Browser code: everything here is compiled into the client bundle.
ASSISTANT_SOURCES = sorted(
    path
    for pattern in ("components/Assistant/*", "providers/Assistant*", "gateways/Assistant*", "types/Assistant*")
    for path in OVERLAY.glob(pattern)
    if path.suffix in {".ts", ".tsx"}
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
SERVER_SOURCES = [*API_ROUTES.values(), AGENT_GATEWAY, ASSISTANT_SERVICE]

REQUIRED_CYPRESS_HOOKS = {
    "AssistantTrigger": "assistant-trigger",
    "AssistantPanel": "assistant-panel",
    "AssistantComposer": "assistant-composer",
    "AssistantSend": "assistant-send",
    "AssistantMessage": "assistant-message",
    "AssistantProductCard": "assistant-product-card",
    "AssistantSuggestion": "assistant-suggestion",
    "AssistantClose": "assistant-close",
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
}


def patch_targets(patch: Path) -> list[str]:
    return re.findall(r"^\+\+\+ b/(.+)$", patch.read_text(), flags=re.MULTILINE)


def test_contract_version_mirrors_the_agent_constant():
    types = (OVERLAY / "types/Assistant.ts").read_text()
    assert re.search(r"export const ASSISTANT_CONTRACT_VERSION = ['\"]1['\"]", types)


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
    joined = "\n".join(path.read_text() for path in ASSISTANT_SOURCES)
    assert "NEXT_PUBLIC_" not in joined
    assert not re.search(r"agent:8010|localhost:8010|langfuse", joined, flags=re.IGNORECASE)


def test_server_sources_hard_code_no_agent_or_observability_address():
    joined = "\n".join(path.read_text() for path in SERVER_SOURCES if path.is_file())
    assert "NEXT_PUBLIC_" not in joined
    assert not re.search(r"agent:8010|localhost:8010|langfuse|https?://[a-z]", joined, flags=re.IGNORECASE)


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
    route = re.search(r'prefix: "/api/assistant/" \}\s*\n\s*route: \{ cluster: frontend, timeout: (\d+)s', ENVOY_TEMPLATE.read_text())
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


def test_demo_details_render_scenario_prompt_tools_and_links():
    source = (OVERLAY / "components/Assistant/DemoDetails.tsx").read_text()
    for field in ("demo.scenario", "demo.prompt_version", "demo.prompt_source", "demo.tools", "demo.links"):
        assert field in source, f"DemoDetails does not render {field}"
    assert "tool.name" in source


def test_every_assistant_answer_offers_feedback():
    transcript = (OVERLAY / "components/Assistant/Transcript.tsx").read_text()
    assert re.search(r"entry\.kind === 'assistant' \? <Feedback", transcript)
    assert "feedback_enabled ? <Feedback" not in transcript
