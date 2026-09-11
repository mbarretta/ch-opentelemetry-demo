# Astronomy Shop concierge

A shopping assistant for OpenTelemetry Demo **3.0.0**, with correlated ClickStack and Langfuse telemetry. The overlay extends the released Python agent and Gradio chat UI and reuses the shop's tools. Upstream source remains unchanged.

The assistant can list products, inspect an item, add it to a conversation's cart, and read that cart. It supports a live model and a deterministic demo mode. The latter exercises the real LangGraph tool loop and shop services without a model API key.

## Start

Requirements: Docker with Compose 2.24.4 or later, Git, Python 3.14, and [uv](https://docs.astral.sh/uv/). Allow roughly 4 GB of available Docker memory and several GB for the released images. The stack runs locally; ClickStack and Langfuse are external destinations.

```sh
python3 scripts/demo.py bootstrap
uv venv --python 3.14
uv pip install -r requirements.txt
.venv/bin/python scripts/demo.py up
```

Open the [chat UI](http://localhost:7860) or [Astronomy Shop](http://localhost:8080). Choose **Use sample request**, then **Send**. After the recommendation, use **Add it to my cart** and **Show my cart**. The assistant's cart belongs to its conversation; it is separate from the storefront browser's cart.

Bootstrap creates `.env` from `.env.example`, verifies the exact upstream commit, and stages the overlay's cart-wrapper fix. The launcher pins every shop image to `3.0.0`; it does not inherit upstream's `DEMO_VERSION=latest` default. Containers and their network use the `astronomy-concierge` project name.

```sh
.venv/bin/python scripts/demo.py ps
.venv/bin/python scripts/demo.py logs agent chatbot
.venv/bin/python scripts/demo.py restart agent chatbot
.venv/bin/python scripts/demo.py down
```

`down` stops this demo. Conversation history lives in the agent process and resets when it restarts. Each session expires after an hour of inactivity and allows 20 turns. Use **New conversation** to reset it; changing scenario or budget also starts a fresh session.

## Connect existing ClickStack and Langfuse instances

Fill in `.env` locally. It is ignored by Git. Use container-reachable addresses; for a service on this computer, use `host.docker.internal` instead of `localhost`.

| Variable | Value |
| --- | --- |
| `CLICKSTACK_OTLP_ENDPOINT` | ClickStack collector's OTLP **HTTP** base URL, without `/v1/traces` |
| `CLICKSTACK_API_KEY` | ClickStack ingestion key, sent as the raw `authorization` header |
| `LANGFUSE_BASE_URL` | Langfuse base URL, without `/api/public/otel` and without a trailing slash |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Project API keys |
| `LANGFUSE_PROJECT_ID` | Project ID used for browser links |
| `LANGFUSE_PUBLIC_URL` | Optional browser-facing URL if different from `LANGFUSE_BASE_URL` |
| `CLICKSTACK_TRACE_URL_TEMPLATE` | Optional trace URL copied from your deployment, replacing its trace ID with `{trace_id}` |

Apply the configuration and create the two sample prompt versions:

```sh
.venv/bin/python scripts/demo.py config
.venv/bin/python scripts/demo.py up
.venv/bin/python scripts/demo.py seed-prompts
```

Prompt seeding uses the Langfuse public API. It creates `astronomy-concierge` with labels `production` and `budget-check`, skips matching existing prompts, and stops if a label already has different content. Runtime prompt reads cache for 60 seconds and fall back to the explicitly labeled bundled prompt during an outage. The inspector shows which prompt was used.

The collector sends all shop traces, metrics, and logs to ClickStack. It sends the `chatbot`, `agent`, and `mcp` spans to Langfuse, preserving the chat root and agent/tool parents. All trace collection uses OpenTelemetry. Operation names, conversation IDs, model names, tool arguments/results, and provider-reported token counts use `gen_ai.*` attributes. Langfuse attributes add observation types, readable input/output, prompt links, and filterable metadata. The exporter uses the v4 ingestion header. Feedback and scripted evaluation scores use `POST /api/public/scores`.

Both destinations receive the same trace IDs. The chat displays the ID and configured links after each turn. A conversation's turns share a session ID but have separate trace IDs. The API's `/prompt` endpoint also accepts incoming W3C trace context.

Without backend configuration, the collector records telemetry in `.runtime/telemetry/telemetry.jsonl`. Files rotate at 20 MB with three backups. This captures structure for local testing; it is **not** a local Langfuse or ClickStack instance. Feedback reports that Langfuse must be configured instead of claiming it was saved.

## Demo walkthrough

| Scenario | Steps | What to inspect |
| --- | --- | --- |
| Shopping | Select `shopping`, keep the $150 budget, send the sample, add the recommendation, and view the cart. | `list_products` → `get_product`, the $101.96 Explorascope, successful cart calls, session grouping. |
| Backend failure | Run the command below, select `backend-failure`, then send its sample request. | An actual `GetProduct` failure for `OLJCESPC7Z`, the failed tool span, and the assistant's recovery message. |
| Budget violation | Restore the catalog, select `budget-violation`, and send its sample with the $150 budget. | A deliberately scripted $349.95 recommendation, healthy backend spans, and `budget_adherence=0`. |

```sh
.venv/bin/python scripts/demo.py scenario backend-failure
# After demonstrating the error:
.venv/bin/python scripts/demo.py scenario shopping
```

The scenario command changes only the overlay's catalog flag file. It preserves other flags and leaves the upstream checkout untouched. The UI scenario selects the agent fixture; the CLI controls the real service fault separately. Allow a moment for flagd to pick up a change.

Scripted mode is a test fixture, not free-form AI reasoning. Its recommendation and tool sequence are deliberately predictable. It records no invented token counts or model costs. Its budget score checks the known recommended product's actual returned price; it does not evaluate arbitrary live-model prose. In live mode, use **Helpful / Not helpful** for human evaluation or add an evaluator in your Langfuse project.

## Use a live model

Set these values in `.env`, then run `scripts/demo.py up` again:

```dotenv
AGENT_MODE=live
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
API_KEY=your-provider-key
```

The released `ChatLLM` adapter accepts OpenAI-compatible endpoints. The overlay disables VCR replay so changed prompts cannot accidentally hit a fuzzy cassette match. Live calls record provider-reported token usage; Langfuse can calculate cost for recognized models. Custom model pricing must be configured in Langfuse. Live mode uses actual provider quota and billing.

For a prompt comparison, switch `LANGFUSE_PROMPT_LABEL` from `production` to `budget-check` and recreate the agent with `up`. Compare the same shopping request across versions in Langfuse. Changing prompts affects the live model, not the deterministic fixture.

## Optional MCP transport

Set `MCP_ENABLED=True` in `.env` and run `up`. The agent exposes the same four scoped tools while delegating their execution to the released MCP service. MCP instrumentation carries trace context and W3C baggage in protocol messages across the persistent connection. An allowlist copies anonymous conversation context onto MCP spans. `/healthz` reports `tools_transport` as `http` or `mcp`.

## Test and develop

```sh
uv pip install -r requirements-dev.lock
.venv/bin/pytest -q
.venv/bin/ruff check concierge scripts tests
.venv/bin/python scripts/smoke.py
```

The smoke script needs the running scripted demo. It checks recommendation, cart isolation, budget failure, the actual catalog fault, and cross-service trace correlation. It restores the prior catalog flag even if a check fails. Detailed results go to `.runtime/smoke-results.json`.

The package and prompts are mounted read-only into the released agent/chatbot images. After editing Python code, restart those containers; changing mounted files alone does not reload their processes. The `up` command recreates containers when their configuration changes.

The default Gradio interface is served directly on port 7860. Use this URL for the overlay rather than the upstream proxy's `/chatbot` route.

## Source and integration notes

- Upstream: [3.0.0](https://github.com/open-telemetry/opentelemetry-demo/releases/tag/3.0.0), commit `1755859a9de82c2e5e225be68abc401a5ebf2b4f`.
- The only staged upstream tool change corrects `get_cart` from `user_id` to the frontend's `sessionId` query parameter and supplies `currencyCode=USD`. It is applied to both built-in and MCP wrappers.
- The overlay subclasses the released `Agent` and `ChatAgentUI`. It uses one OpenTelemetry provider in each overlay process, with explicit model/tool spans, to avoid duplicate LLM instrumentation.
- ClickStack dashboards written for older `app.*` attributes may need the release's `demo.*` names.
- This is a local demo with in-memory sessions, not a production authentication or persistence design. Keep its unauthenticated chat/API listeners local.
- References: [Langfuse OpenTelemetry](https://langfuse.com/integrations/native/opentelemetry), [prompt API](https://langfuse.com/docs/prompt-management/get-started), [scores API](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk), [ClickStack collector](https://clickhouse.com/docs/clickstack/ingesting-data/collector).

## Langfuse skill integration review

Reviewed on September 11, 2026 using the agent skill from [langfuse/skills](https://github.com/langfuse/skills), the current [trace audit guidance](https://langfuse.com/docs/observability/best-practices), and [OTel attribute mapping](https://langfuse.com/integrations/native/opentelemetry).

| Finding | Change |
| --- | --- |
| Usage was recorded only in Langfuse-specific JSON. | Record standard input/output token attributes, plus reported cache and reasoning counts. Omit missing counts. Langfuse maps these attributes on ingestion. |
| MCP retained trace IDs but lost session attributes. | Propagate anonymous session/scenario/mode through OTel baggage and copy allowed keys onto MCP spans. Concurrent conversations have isolated context. |
| Direct API requests had empty root input/output. | Populate the existing HTTP root observation. Keep the chat root's input/output for UI requests. |
| Generations lacked the tools available to the model. | Capture the scoped tool definitions alongside the system prompt and message history. |
| An evaluator output alone did not explain its result. | Capture the checked product and budget on the evaluator observation. |
| Anonymous sessions were represented as users. | Keep session/conversation IDs; omit user IDs until the app has a distinct user identity. |
| ASGI send/receive spans added noise. | Exclude these internal leaves while retaining the HTTP request and application hierarchy. |

The application keeps one OTel tracing provider per process and one collector export path. Langfuse's SDK is also OTel-based and is its recommended Python integration; adopting it is compatible with this design. For this released-image overlay, explicit OTel spans preserve the existing dependencies and avoid a second LLM instrumentor emitting duplicate generations. Langfuse-specific attributes are carried in ordinary OTLP spans and remain available in ClickStack.

Prompt retrieval/versioning and saved scores remain Langfuse API operations. The documented score creation endpoint is separate from OTLP trace ingestion; an evaluator span alone does not create a saved Langfuse score. No Langfuse trace-ingestion REST API is used.

Use synthetic demo inputs: trace payloads contain chat messages, prompts, tool definitions, and tool results. Baggage contains only anonymous conversation IDs and demo labels; it may travel to downstream services and model endpoints. Shop services retain trace correlation but are not modified to copy baggage into their own span attributes.

Validation covers unit tests and captured OTLP traces from real shop calls over HTTP and MCP. Project-side rendering, prompt linkage, pricing, and saved scores still require the existing Langfuse instance configuration. The skill's final audit step, “Fetch the trace(s) you just created from Langfuse,” remains pending until that access is available; local capture does not verify Langfuse ingestion.
