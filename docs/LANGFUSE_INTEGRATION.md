# Langfuse integration: how and why

This document explains the Langfuse-specific changes this repository layers onto the upstream [OpenTelemetry Demo](https://github.com/open-telemetry/opentelemetry-demo) (pinned at 3.0.0). It covers the new AI agent that Langfuse observes, where the Langfuse wiring lives in the code, why it's built the way it is, and which Langfuse product features that wiring turns on. It's a companion to `README.md`, which documents the whole repository (ClickStack, session replay, the EKS target); this file goes deeper on the Langfuse half alone.

## 1. The new AI agent

Upstream's demo has no AI agent. This repository adds one: a **shopping concierge** for the Astronomy Shop storefront, plus a native panel in the storefront UI to talk to it.

- **The agent** (`concierge/agent.py`) subclasses the OpenTelemetry Demo's own released `Agent` class into a [LangGraph](https://langchain-ai.github.io/langgraph/) tool-calling loop, served by FastAPI on port 8010. It runs in two modes, chosen by `AGENT_MODE`:
  - `scripted` (default): a deterministic fixture (`concierge/scripted_model.py`) that needs no model API key — useful for demos and CI.
  - `live`: a real OpenAI-compatible model through the released `ChatLLM` adapter.
- **Tools**: `list_products`, `get_product`, `get_cart`, `add_to_cart`, wrapping the shop's own product-catalog and cart gRPC services. The cart identity (`user_id`) and currency are always injected server-side (`ConciergeAgent.scoped_tools` in `concierge/agent.py`) — the model never supplies them, so it cannot address another shopper's cart. Tools run over plain HTTP by default or, with `MCP_ENABLED=True`, over the released MCP tool server.
- **Two API surfaces**: a legacy `POST /prompt` endpoint (used by an optional Gradio debug client, `concierge/chatbot.py`) and the storefront's own contract — `POST /assistant/message`, `POST /assistant/actions/add-to-cart`, `GET /assistant/conversations/{id}` — defined in `concierge/contract.py`. Conversations are bound for their whole life to the storefront's cart session, expire after hidden 20-turn/1-hour limits, and dedupe by request ID.
- **The panel** (`frontend/overlay/`) is a right-side panel built from the storefront's own React components, reachable from a header button and from "Ask about this product." It's what a shopper actually uses; every turn it sends is what Langfuse ends up tracing.

Everything below is about how that agent's behavior — every model call, every tool call, every prompt version, every piece of human feedback — becomes visible in Langfuse.

## 2. Where the Langfuse wiring lives

There is no Langfuse SDK anywhere in this codebase. The integration is built entirely on the OpenTelemetry SDK the agent already uses, plus two narrow direct calls to Langfuse's public REST API for the two things OTel spans can't do (fetch a managed prompt, save a score). That choice is deliberate — see [§5](#5-design-choices-and-why) — and it means the "Langfuse wiring" is really just a set of attribute names and a couple of pipeline stages layered on ordinary tracing code.

| File | Role |
| --- | --- |
| `concierge/telemetry.py` | Configures the one `TracerProvider` per process (agent, mcp, chatbot). Defines `conversation()`, a context manager every turn runs inside that stamps `session.id`, `gen_ai.conversation.id`, `langfuse.session.id`, `langfuse.trace.name`, `langfuse.trace.tags` (always `["astronomy-concierge"]`, so this app's traffic is distinguishable in a shared Langfuse project), and scenario/mode metadata onto the current span and propagates them as OTel baggage so every downstream span (tool calls, model calls, MCP hops) inherits the same identity. `ConversationProcessor` copies that baggage onto every span as it starts, converting `langfuse.trace.tags` back into a string array since baggage itself only carries strings. Exports to `OTEL_EXPORTER_OTLP_ENDPOINT` when set, otherwise to a local JSON-lines file — never a live Langfuse endpoint from the agent process itself (see §3). |
| `concierge/agent.py` | Puts `langfuse.observation.*` and `gen_ai.*` attributes on every span in a turn: the outer `concierge.turn` (type `agent`), the model call (`model.generate`, type `generation`), each tool call (type `tool`), the cart-mutation chain (`concierge.action`, type `chain`), and the scripted budget check (`evaluate.budget`, type `evaluator`). This is the file that actually shapes what a Langfuse trace looks like. |
| `concierge/langfuse_api.py` | The only code that talks to Langfuse directly, over its REST API rather than OTLP: `GET /api/public/v2/prompts/{name}` to fetch the managed system prompt (60-second cache, falls back to the bundled prompt file on any error), and `POST /api/public/scores` to save a score against a trace ID. `LangfuseAPI.enabled` is `True` only when a base URL and both keys are set; every route that needs Langfuse checks it and degrades explicitly rather than pretending to succeed. |
| `concierge/contract.py` | Not Langfuse-specific, but carries the Langfuse-observable facts out to the browser: `trace_id`, `feedback_enabled`, and (behind `ASSISTANT_DEMO_DETAILS`) `prompt_version`/`prompt_source` and trace links, so a presenter can show what Langfuse saw without opening Langfuse. |
| `frontend/overlay/utils/telemetry/AssistantTracing.ts` | The browser half. Wraps every panel request in an `assistant.turn` span using the storefront's own tracer (so the fetch instrumentation's child span, and the `traceparent` it injects, carry the whole trace from browser to agent), and sets the same `gen_ai.conversation.id` / `langfuse.session.id` attributes once the agent answers with a conversation ID. |
| `launcher/collector.py` (laptop), `launcher/eks/values.py` + `deploy/eks/k8s/demo-values.yaml` (EKS) | The collector-side Langfuse trace pipeline (`traces/langfuse`): a filter that keeps only one assistant turn's ancestor chain, the `gen_ai_normalizer` processor (already part of the upstream chart's collector config, reused here), and — only when credentials are present — an `otlphttp`-style exporter to Langfuse's native OTel endpoint. `launcher/collector.py` is the single source of the filter logic for both targets; `launcher/eks/values.py` calls the same functions rather than re-deriving them. |
| `compose.concierge.yaml`, `.env` / `launcher/eks/config.py` | Wires `LANGFUSE_*` values into the agent container (keys, project ID, prompt label, public URL) and into the collector (base URL plus a precomputed `LANGFUSE_AUTH_HEADER` — see below). On EKS these become a `langfuse-credentials` Secret, created only when all three core keys are set. |

### The auth header is computed once, not by the collector

The collector's OTLP HTTP exporter needs a `Basic base64(public_key:secret_key)` Authorization header, but the collector config can't run code. `launcher/eks/config.langfuse_auth_header()` (and its laptop equivalent) base64-encodes the key pair once, in Python, and hands the collector only `LANGFUSE_AUTH_HEADER` as `${env:LANGFUSE_AUTH_HEADER}` — the collector never sees the raw keys, and both targets share the one encoding function.

## 3. How one browser click becomes one Langfuse trace

The demo's headline claim is **one collector, two exporters, one trace ID**: the same trace that lands in ClickStack for a shopping-assistant turn also lands in Langfuse, under the same ID.

```
browser (assistant.turn span)
  → frontend-proxy (Envoy)
    → frontend (Next.js /api/assistant/* route)
      → agent (concierge.turn → model.generate / tool spans → concierge.action)
        → product-catalog / cart / currency (shop gRPC services)
```

1. **Identity travels as OTel baggage, not as a Langfuse SDK concept.** `concierge/telemetry.py` stamps a fixed allow-list of keys (`BAGGAGE_KEYS`) — never arbitrary incoming baggage — onto every span in a turn: `session.id` (the storefront cart session), `gen_ai.conversation.id` (also copied to `langfuse.session.id`), and scenario/mode metadata. The browser side (`AssistantTracing.ts`) sets the same keys before the fetch that reaches the agent, so a conversation's turns form one Langfuse *session* with separate *trace* IDs per turn, exactly the identity model Langfuse's OTel integration expects.
2. **Every span is plain OTLP, carrying `langfuse.*` attributes Langfuse recognizes on ingestion** — `langfuse.observation.type`, `.input`, `.output`, `.model.name`, `.metadata.*` — alongside the OTel GenAI semantic-convention attributes (`gen_ai.operation.name`, `gen_ai.usage.*`, etc.). Nothing is sent through a Langfuse-specific SDK call; the same spans that carry these attributes also flow to ClickStack.
3. **The collector filters before it exports.** `langfuse_span_filter()` (`launcher/collector.py`) keeps only the ancestor chain of an assistant turn — the browser span, the proxy's route spans, the storefront's `/api/assistant/*` route and its call to the agent, and every `agent`/`mcp` span except health checks and feedback/score calls — and drops everything else the storefront does. That's *why* Langfuse receives exactly one clean trace per turn and nothing else: an orphaned span (missing an ancestor upstream) would show up broken in the Langfuse UI, so every ancestor is kept on purpose rather than filtered independently at each hop.
4. **Export is conditional, never half-configured.** All three of `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` must be set or none may be — a partial set makes the collector refuse to start (`core.die(...)` in `launcher/collector.py`) rather than silently drop the pipeline. Without credentials, the filtered Langfuse-bound subset still gets written — to `.runtime/telemetry/langfuse-preview.jsonl` on the laptop, or to the collector's `debug` exporter (readable with `kubectl logs`) on EKS — so what *would* reach Langfuse is always inspectable, credentials or not. `scripts/smoke.py` asserts against that preview file.
5. **The exporter speaks Langfuse's native OTel endpoint**, `POST {LANGFUSE_BASE_URL}/api/public/otel`, with the `x-langfuse-ingestion-version: 4` header and the precomputed Basic auth header.

## 4. Langfuse features this wiring turns on

| Feature | How it's populated |
| --- | --- |
| **Sessions** | One Langfuse session per conversation (`langfuse.session.id` = `gen_ai.conversation.id`), so every turn of one shopper's chat groups together even though each turn is its own trace. |
| **Traces & typed observations** | Each turn is a trace containing typed observations via `langfuse.observation.type`: `agent` (the turn itself), `generation` (the model call, with model name, prompt/messages/tools as input, and the reply as output), `tool` (each `list_products`/`get_product`/`get_cart`/`add_to_cart` call, input and result captured), `chain` (a cart-mutation action), and `evaluator` (the scripted budget check). |
| **Prompt Management** | The agent's system prompt is a named Langfuse prompt (`astronomy-concierge`), fetched by `LangfuseAPI.prompt()` with a 60-second cache and a fall back to the bundled `prompts/concierge-v1.txt` on any failure. `scripts/demo.py seed-prompts` creates two labeled versions — `production` (`concierge-v1.txt`) and `budget-check` (`concierge-v2.txt`) — through the same public API, target-independent (one Langfuse project serves both the laptop and EKS). Switching `LANGFUSE_PROMPT_LABEL` and restarting the agent lets you A/B two prompt versions on the same live model. Generation spans link back to the prompt name/version so Langfuse's prompt-usage view works. |
| **Scores (human + programmatic evaluation)** | Two sources write scores through `POST /api/public/scores`: the storefront's **Helpful / Not helpful** buttons (`user_helpfulness`, human feedback on a specific trace) and, in scripted mode, a deterministic `budget_adherence` check against the actual recommended product's price. Both go through the same `LangfuseAPI.score()` call. |
| **Cost / token usage** | Live-mode generation spans record standard OTel GenAI usage attributes (`gen_ai.usage.input_tokens`, `output_tokens`, plus reported cache-read/cache-write and reasoning-token counts when the provider returns them) — Langfuse maps these on ingestion to compute cost for recognized models. Scripted mode records none, deliberately, since there's no real model call to meter. |
| **OTel-native ingestion** | The whole thing rides Langfuse's own recommended [OpenTelemetry integration](https://langfuse.com/integrations/native/opentelemetry) rather than a parallel SDK — one tracer provider, one export path, attributes Langfuse and ClickStack both understand. |
| **In-app trace links** | With `ASSISTANT_DEMO_DETAILS=true`, each answer's "Demo details" panel links straight to that turn's trace in the Langfuse UI (built from `LANGFUSE_PROJECT_ID` + `LANGFUSE_PUBLIC_URL`/`LANGFUSE_BASE_URL` in `ConciergeAgent.links()`), next to the equivalent ClickStack link for the same trace ID. |

## 5. Design choices and why

- **No Langfuse SDK; OTel attributes instead.** The agent already keeps one OTel tracer provider per process with explicit model/tool spans (to avoid a second LLM auto-instrumentor emitting duplicate generations alongside the hand-written ones). Langfuse's own SDK is itself OTel-based and is its recommended Python integration path, so this is compatible with — not a workaround of — how Langfuse expects to be integrated; it just means the attributes are set by hand rather than by an SDK wrapper. It also means every `langfuse.*` attribute is an ordinary OTLP span attribute, so it's equally visible in ClickStack.
- **Two things stay outside OTLP on purpose**: fetching a managed prompt and saving a score are Langfuse REST API operations with no OTLP equivalent — a generation span alone doesn't create a saved score, and there's no trace-ingestion REST API in use here at all. `concierge/langfuse_api.py` is intentionally the only file that makes those calls.
- **The filter is a single source of truth.** `launcher/collector.py`'s `langfuse_span_filter()` and `assistant_route_statements()` are called by both the laptop collector config and `launcher/eks/values.py`'s generated Helm values, so the laptop and the cluster can never disagree about which spans reach Langfuse.
- **Anonymous by design.** Baggage carries only conversation/session IDs and demo labels — no user identity — the product of an internal self-review against Langfuse's trace-audit guidance, including recording usage as standard attributes instead of Langfuse-specific JSON, and excluding ASGI-internal spans as noise.
- **Capture is verbatim, deliberately, for a demo.** Shopper text and assistant replies are recorded in full on `langfuse.observation.input`/`output` with no masking — appropriate for synthetic demo data, explicitly called out in `README.md` as not appropriate for real shopper data without adding redaction.

## 6. Minimal configuration

The full configuration reference (every key, both targets) is in `README.md`'s [Configuration](../README.md#configuration) and [Connect ClickStack and Langfuse](../README.md#connect-clickstack-and-langfuse) sections. The Langfuse-specific keys, all read from `.env`:

| Key | Purpose |
| --- | --- |
| `LANGFUSE_BASE_URL` | Langfuse base URL (no `/api/public/otel`, no trailing slash). |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Project API keys — all three of these plus the base URL are all-or-nothing; a partial set is refused, not partially exported. |
| `LANGFUSE_PROJECT_ID` | The project name shown in the Langfuse UI, used only to build the browser-facing trace link. `ConciergeAgent.links()` resolves it to Langfuse's opaque project id via `LangfuseAPI.project_id()` (`GET /api/public/projects`), since trace URLs are keyed by that id, not the name. |
| `LANGFUSE_PUBLIC_URL` | Optional override when the browser-facing URL differs from `LANGFUSE_BASE_URL`. |
| `LANGFUSE_PROMPT_LABEL` | Which prompt label the agent reads (`production` or `budget-check`). |

`demo.py config`/`up` on the laptop refuse to start unless all three are set, alongside `CLICKSTACK_OTLP_ENDPOINT` and `CLICKSTACK_API_KEY`; `eks deploy` on the cluster refuses unless all three are set, alongside the five ClickStack keys in the EKS section of `.env`. See `README.md`'s [Configuration](../README.md#configuration) and [Connect ClickStack and Langfuse](../README.md#connect-clickstack-and-langfuse) sections. The agent process itself has no such check: `AGENT_MODE=scripted` still needs no model key, and the collector's own defensive fallback (traces going to the local preview file / `debug` exporter instead of Langfuse) is unchanged below that CLI-level gate.

## References

- [Langfuse OpenTelemetry integration](https://langfuse.com/integrations/native/opentelemetry)
- [Langfuse prompt management API](https://langfuse.com/docs/prompt-management/get-started)
- [Langfuse scores via SDK/API](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk)
- `README.md` — the full repository reference, including the ClickStack/session-replay additions this document doesn't cover.
