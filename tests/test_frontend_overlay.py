"""Static checks on the assistant overlay and its upstream patches.

The storefront has no unit-test runner of its own (adding one would add npm dependencies), so
the parts of the assistant shell that can be checked without a browser are asserted here:
the contract version the frontend mirrors, the theme-token rule for new component styles,
the empty-state copy, the Cypress hooks, and the one-patch-per-upstream-file layout.
"""

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / "frontend/overlay"
PATCHES = ROOT / "frontend/patches"
UPSTREAM_FRONTEND = ROOT / ".upstream/opentelemetry-demo/src/frontend"

ASSISTANT_SOURCES = sorted(
    path
    for pattern in ("components/Assistant/*", "providers/Assistant*", "gateways/Assistant*", "types/Assistant*")
    for path in OVERLAY.glob(pattern)
    if path.suffix in {".ts", ".tsx"}
)

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
