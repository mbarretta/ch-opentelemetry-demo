# Astronomy Shop concierge

A shopping assistant built into the OpenTelemetry Demo **3.0.0** Astronomy Shop storefront, with correlated ClickStack and Langfuse telemetry. The storefront gains a native **Shopping assistant** panel made from the shop's own React components and theme; the released Python agent runs behind same-origin storefront API routes and reuses the shop's tools. Upstream source remains unchanged: new storefront files are an overlay and edits to released files are checked patches, both applied when the build tree is staged.

The assistant recommends catalog products, explains an item, adds it to the shopper's own cart, and reads that cart. It supports a live model and a deterministic scripted mode. The latter exercises the real LangGraph tool loop and shop services without a model API key.

## Start

Requirements: Docker with Compose 2.24.4 or later, Git, Python 3.14, and [uv](https://docs.astral.sh/uv/). Allow roughly 4 GB of available Docker memory and several GB for the released images. The stack runs locally; ClickStack and Langfuse are external destinations.

```sh
python3 scripts/demo.py bootstrap
uv venv --python 3.14
uv pip install -r requirements.txt
.venv/bin/python scripts/demo.py build
.venv/bin/python scripts/demo.py up
```

Open the [Astronomy Shop](http://localhost:8080/) and choose **Assistant** in the header.

| Step | What it does |
| --- | --- |
| `bootstrap` | Creates `.env` from `.env.example`, verifies the exact upstream commit, and writes the corrected shop tools. |
| `build` | Stages the 3.0.0 source with the overlay and patches under `.runtime/build/`, then builds the frontend, agent, mcp, and frontend-proxy images for the Docker host's platform and records them in `.runtime/images/manifest.json`. The first frontend build takes several minutes. See `docker/README.md`. |
| `up` | Starts the stack from the recorded images, with no application-code bind mounts and without the Gradio client. It stops before starting anything when the manifest or an image is missing and names `build`; it warns when the build inputs changed since the last build. |

The launcher pins every other shop image to `3.0.0`; it does not inherit upstream's `DEMO_VERSION=latest` default. Containers and their network use the `astronomy-concierge` project name.

```sh
.venv/bin/python scripts/demo.py ps
.venv/bin/python scripts/demo.py logs agent frontend
.venv/bin/python scripts/demo.py restart agent
.venv/bin/python scripts/demo.py down
```

### Options

| Option | Effect |
| --- | --- |
| `up --dev` | Adds `compose.dev.yaml`: `concierge/`, `prompts/`, and `.runtime/tools.py` are bind-mounted read-only over the agent and mcp images. Restart those containers after editing. |
| `up --debug-chatbot` | Also starts the Gradio [chat client](http://localhost:7860) on `CHAT_PORT`. It is an optional debug client for the legacy `POST /prompt` API and is not part of the storefront experience. Its cart is its own conversation ID, separate from any browser's cart. |
| `AGENT_BASE_URL` | Server-side only: the storefront's `/api/assistant/*` routes call the agent here (default `http://agent:8010`). The browser only ever calls the shop origin; no agent, model, or Langfuse address reaches the browser bundle. |
| `ASSISTANT_TRANSPORT` | `live` (default) sends the panel's requests to the storefront routes. `fixtures` renders canned answers without an agent call, for reviewing the UI states. |
| `ASSISTANT_DEMO_DETAILS` | `true` (default) shows a collapsed **Demo details** area under each answer with scenario, prompt version and source, tool names, and trace links. `false` hides it. |

`ASSISTANT_*` and `AGENT_BASE_URL` are documented in `.env.example`; changing them needs `up` to recreate the frontend container, not a rebuild.

## Use the assistant

The panel opens as a right-side panel at desktop widths (768 px and up) and as a full-screen dialog below that. It starts with **Find the right telescope**, three suggestions (**A telescope for a beginner**, **Help me choose under $150**, **Explain this product**), and the active budget and currency. Product pages add **Ask about this product**, which opens the same panel with that product as context.

Every recommendation is a compact product card whose name, picture, and price come from the agent's successful catalog tool results, never from the reply text. **View product** opens the product page; **Add to cart** performs one agent cart action against the shopper's storefront cart, and the header count, cart dropdown, and cart page update without a reload. Each answer offers **Helpful** / **Not helpful**, which scores that answer's trace in Langfuse and reports whether the score was saved.

### Conversation and cart

- The conversation is bound to the browser's storefront session, so the assistant reads and changes the same cart the storefront shows. A different browser session has its own cart and cannot continue another session's conversation.
- **New conversation** starts a fresh conversation ID and clears the transcript; the cart is unchanged. Closing and reopening the panel, and moving between the home, product, and cart pages, keep the transcript.
- Conversation history lives in the agent process: a conversation expires after an hour of inactivity or 20 turns and is lost when the agent restarts. On page reload the panel resumes only after the agent confirms the conversation is alive; otherwise it starts fresh and says why (previous conversation expired, or the agent could not be reached). `demo.py restart agent` demonstrates this.
- One turn is in flight per conversation: the composer and card actions are disabled until it settles. Retries reuse the request ID and the agent deduplicates them, so a repeated click never adds an item twice. A cart action that times out is never retried automatically: the panel refetches the cart and shows an explicit uncertain state until the cart confirms or the shopper retries.
- Prices follow the header currency switcher. Cards, the reply amount, the budget comparison, and the cart use the shop's currency service for the selected currency; original amounts are never relabeled as another currency. Switching currency refreshes card prices and applies to the next turn.
- If the agent is down, the panel shows an inline error with **Retry** and the rest of the shop keeps working.

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

The collector sends all shop traces, metrics, and logs to ClickStack. To Langfuse it sends one storefront assistant turn end to end and nothing else the storefront does: the browser's `assistant.turn` span and the fetch under it, the proxy's spans for `/api/assistant/`, the storefront's `/api/assistant/*` route spans and its call to the agent, and every `agent`, `mcp`, and (debug) `chatbot` span except health checks and feedback-score calls. Every ancestor in that chain is kept on purpose, so nothing arrives in Langfuse as an orphan; unrelated storefront traffic and shop-service spans are dropped (`langfuse_span_filter` in `scripts/demo.py`). All trace collection uses OpenTelemetry. Operation names, conversation IDs, model names, tool arguments/results, and provider-reported token counts use `gen_ai.*` attributes. Langfuse attributes add observation types, readable input/output, prompt links, and filterable metadata. The exporter uses the v4 ingestion header. Feedback and scripted evaluation scores use `POST /api/public/scores`.

Both destinations receive the same trace IDs. Each answer's demo details show the ID and configured links; **Helpful / Not helpful** scores that answer's trace. Two identities travel on every span of a turn: `gen_ai.conversation.id` (also `langfuse.session.id`) is the conversation, so a conversation's turns form one Langfuse session with separate trace IDs, and `session.id` is the storefront (cart) session, the same value the shop's own browser and server spans carry. The API's `/prompt` endpoint also accepts incoming W3C trace context.

Without backend configuration, the collector records telemetry in `.runtime/telemetry/telemetry.jsonl`. Files rotate at 20 MB with three backups. This captures structure for local testing; it is **not** a local Langfuse or ClickStack instance. Feedback reports that Langfuse must be configured instead of claiming it was saved. The Langfuse-bound subset is always also written to `.runtime/telemetry/langfuse-preview.jsonl`, with or without credentials, so what Langfuse would receive can be inspected (the smoke script asserts on it).

Capture is verbatim: this is a demo on synthetic data, so the text typed into the composer and the assistant's replies are recorded in full on the spans (`langfuse.observation.input` / `output`) and sent to both destinations, and nothing is masked or redacted. Do not point it at real customer data without adding masking.

## Demo walkthrough

| Scenario | Steps | What to inspect |
| --- | --- | --- |
| Shopping | In the storefront panel, keep the $150 budget and send **A telescope for a beginner**. Add the recommendation from its card and open the cart. | `list_products` → `get_product`, the $101.96 Explorascope, one successful `add_to_cart`, the header count, and the conversation's Langfuse session. |
| Backend failure | Run the command below, then ask the panel to **Explain this product** from the Explorascope's product page (or send its sample request in the debug client with `backend-failure` selected). | An actual `GetProduct` failure for `OLJCESPC7Z`, the failed tool span, and the assistant's recovery message. |
| Budget violation | Restore the catalog. In the debug client (`up --debug-chatbot`), select `budget-violation` and send its sample with the $150 budget. | A deliberately scripted $349.95 recommendation, healthy backend spans, and `budget_adherence=0`. |

```sh
.venv/bin/python scripts/demo.py scenario backend-failure
# After demonstrating the error:
.venv/bin/python scripts/demo.py scenario shopping
```

The scenario command changes only the overlay's catalog flag file. It preserves other flags and leaves the upstream checkout untouched. The storefront panel always uses the `shopping` fixture; the other fixtures are selected in the debug client, and the CLI controls the real service fault separately. Allow a moment for flagd to pick up a change.

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
scripts/dev-setup.sh
.venv/bin/pytest -q
.venv/bin/ruff check concierge scripts tests
scripts/check-frontend.sh
.venv/bin/python scripts/smoke.py
```

`dev-setup.sh` creates `.venv` from `requirements-dev.lock` when it is missing and runs `bootstrap`. In a linked git worktree it symlinks `.upstream` and `.venv` from the main checkout and copies its `.env`, so a fresh worktree passes the same checks. `check-frontend.sh` stages the frontend build tree (see `frontend/overlay/README.md`) and type-checks and lints it.

The smoke script needs the running scripted demo. It checks recommendation, cart isolation, budget failure, the actual catalog fault, and cross-service trace correlation over the legacy `/prompt` API, then one native storefront turn: it exports a synthetic browser span through the proxy's OTLP route, sends a turn through `http://localhost:8080/api/assistant/message` under it, and asserts that `.runtime/telemetry/langfuse-preview.jsonl` holds that parent, every ancestor down to exactly one `concierge.turn`, and the conversation and storefront-session attributes on the storefront's route span. It restores the prior catalog flag even if a check fails. Detailed results go to `.runtime/smoke-results.json`. To cover the MCP transport, set `MCP_ENABLED=True` in `.env`, run `up` (it recreates the agent), and run the script again.

### Browser tests

The released storefront's Cypress suite runs from the staged tree against the running native stack. `frontend/overlay/cypress/e2e/Assistant.cy.ts` drives the panel with the scripted agent: open from the header, send **A telescope for a beginner**, check the product card, add to cart, watch the header count, find the item on the cart page, close and reopen with the transcript intact, start a new conversation with the cart untouched, and, at 1440 px and 390 px, open, submit, and close with the keyboard, check the mobile dialog's focus trap and focus return, and read the live region's pending and completed announcements.

```sh
scripts/check-frontend.sh                       # stages the tree and links node_modules
cd .runtime/build/src/frontend
npx cypress run --spec cypress/e2e/Assistant.cy.ts
npx cypress run --spec 'cypress/e2e/Home.cy.ts,cypress/e2e/ProductDetail.cy.ts,cypress/e2e/Checkout.cy.ts'
```

The base URL comes from upstream's `.env` (`FRONTEND_PORT=8080`); set `CYPRESS_BASE_URL` when `SHOP_PORT` differs. `check-frontend.sh` must run after `build`, because staging removes the `node_modules` link.

### Validated

Results on September 11, 2026, on image set `28c8a6773491` (`linux/arm64`, Docker Desktop on Apple silicon), produced from a clean state (`down`, then `.runtime/build` and `.runtime/images` removed) by `bootstrap`, `build`, and `up`, with `AGENT_MODE=scripted`:

| Check | Result |
| --- | --- |
| `Assistant.cy.ts` | 3 of 3 passing: the shopping flow, the desktop panel with the keyboard at 1440 px, and the mobile dialog with the keyboard at 390 px. |
| `Home.cy.ts`, `ProductDetail.cy.ts`, `Checkout.cy.ts` | 4 of 7 passing. The same three tests fail the same way against the released `ghcr.io/open-telemetry/demo:3.0.0-frontend` image in this stack: Home `should recover corrupted stored session` (a localStorage race), ProductDetail `should not render product picture or request undefined image when picture is missing`, and Checkout `should create an order with two items` (the order completes after its 4 s assertion window). They are upstream 3.0.0 behavior, not regressions from the overlay. |
| `scripts/smoke.py` | Passes over HTTP tools and, with `MCP_ENABLED=True`, over MCP. |
| `ruff`, `pytest`, `check-frontend.sh` | Pass. `npm run lint` reports one pre-existing warning in upstream `utils/telemetry/SessionIdProcessor.ts`. |
| Build hygiene | Both build contexts, listed with the probe in `docker/README.md`, contain no `.env`, `.venv`, credentials, telemetry captures, or `node_modules`. `docker image inspect` and `docker history` of the four images show no `LANGFUSE_*`, `API_KEY`, or secret values. The manifest records the tag, platform, image IDs, base digests, upstream commit, and `contract_version`; a test shows that editing `prompts/concierge-v1.txt` changes the tag. |
| Platform | `linux/arm64` built and tested. `linux/amd64` untested; see `docker/README.md`. |

The agent and mcp images bake in the package, prompts, and corrected tools, so a Python change needs `demo.py build` and `up`. For faster iteration, `up --dev` bind-mounts `concierge/`, `prompts/`, and `.runtime/tools.py` read-only over those images; after editing, restart the containers, because changing mounted files alone does not reload their processes. The `up` command recreates containers when their configuration changes. Frontend changes always need `build` and `up`.

The Gradio interface (`up --debug-chatbot`) is served directly on port 7860; the native proxy has no `/chatbot` route.

## Source and integration notes

- Upstream: [3.0.0](https://github.com/open-telemetry/opentelemetry-demo/releases/tag/3.0.0), commit `1755859a9de82c2e5e225be68abc401a5ebf2b4f`.
- The only staged upstream tool change (`TOOLS_PATCH` in `scripts/demo.py`) corrects `get_cart` from `user_id` to the frontend's `sessionId` query parameter and gives `list_products`, `get_product`, and `get_cart` a `currency_code` parameter forwarded as `currencyCode`. The corrected file is baked into both the agent and MCP images.
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
