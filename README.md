# ch-opentelemetry-demo

The OpenTelemetry Demo **3.0.0** Astronomy Shop, upstream source unchanged, with three additions: ClickHouse observability through **ClickStack** for every signal the shop emits, **browser session replay** recorded by the ClickStack browser SDK, and a native **AI shopping assistant** in the storefront whose every turn is traced end to end into **Langfuse**. One collector exports to both backends, so an assistant turn carries the same trace ID in ClickStack and in Langfuse.

There are two ways to run it, from the same tree, the same upstream pin and the same four images: **Docker Compose on a laptop**, or an **EKS cluster you own**. Langfuse is external (Langfuse Cloud) either way, and both targets can point at the same Langfuse project.

## What is added

| Addition | Where it lives | What you see |
| --- | --- | --- |
| **Shopping assistant** panel in the storefront, built from the shop's own React components and theme | `frontend/overlay/`, `frontend/patches/` (an overlay of new files plus checked patches on released ones; the upstream checkout is never modified) | An **Assistant** button in the header and **Ask about this product** on product pages; product cards you can add to the shopper's real cart; **Helpful / Not helpful** on every answer |
| **The agent**: the released Python agent subclassed into a LangGraph tool loop over the shop's own catalog and cart services | `concierge/`, served behind the storefront's same-origin `/api/assistant/*` routes | Recommendations with real prices, cart mutations the header count reflects, a live model or a deterministic scripted mode that needs no API key |
| **ClickStack for every signal** | `launcher/collector.py` on the laptop, `deploy/eks/k8s/clickstack-collector.yaml` in the cluster | Traces, logs and metrics from all of the demo's services in ClickHouse, replacing the demo's bundled Jaeger, Prometheus, Grafana and OpenSearch |
| **Browser session replay** | `frontend/patches/0009-frontend-tracer-session-replay.patch`, compiled into the one frontend image | Replayable sessions in ClickStack, correlated to the traces of the same visit |
| **Langfuse tracing of the assistant, and only the assistant** | `concierge/telemetry.py`, the Langfuse pipeline in `launcher/collector.py` and in the generated Helm values | One Langfuse session per conversation and one trace per turn, with prompt versions, tool calls, token counts and saved feedback scores |
| **Fault scenarios** driven through the demo's own feature flags | `demo.py scenario`, `demo.py eks flag` | A real `GetProduct` failure, the failed tool span, and the assistant's recovery — on either target |

## Architecture

```
 browser  ── storefront pages, the Shopping assistant panel, the session-replay recorder
   │
   │  one origin for everything: shop pages, assistant calls, and the browser's own spans
   ▼
 frontend-proxy (Envoy)
   ├── /                 ──▶  frontend (Next.js storefront) ──▶ cart, currency, ads, …
   ├── /api/assistant/*  ──▶  frontend route ──▶ agent (LangGraph) ──▶ product-catalog,
   │      120 s route, server-side only          tools over HTTP or mcp   cart, currency
   └── /otlp-http        ──▶  OpenTelemetry collector ◀── every shop service
                                         │
                 ┌───────────────────────┴───────────────────────┐
                 │ all traces, metrics, logs and replay          │ one assistant turn, whole chain
                 ▼                                               ▼
            ClickStack (ClickHouse)                      Langfuse (Cloud project)
```

That last fan-out is the point of the demo: **one collector, two exporters, one trace ID.** The browser's `assistant.turn` span, the proxy's route span, the storefront's `/api/assistant/*` route span, its call to the agent, and every `agent` and `mcp` span below it all belong to a single trace. ClickStack receives that trace alongside everything else the shop did; Langfuse receives exactly that one chain and nothing else, so the same ID you search in ClickStack finds the turn in Langfuse. Nothing arrives in Langfuse as an orphan, because every ancestor of an assistant span is kept on purpose (`langfuse_span_filter` in `launcher/collector.py`).

On the laptop the collector is the demo's own, configured by the launcher. In the cluster it is the demo's gateway collector, exporting to an in-cluster ClickStack collector that writes to ClickHouse Cloud, and to Langfuse directly.

## Run on a laptop

Requirements: Docker with Compose 2.24.4 or later, Git, Python 3.14, and [uv](https://docs.astral.sh/uv/). Allow roughly 4 GB of available Docker memory and several GB for the released images. Everything runs locally; ClickStack and Langfuse are external destinations.

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
| `SESSION_REPLAY` | `auto` (default) records browser sessions whenever ClickStack is configured; `true` forces the recorder on, `false` off. The SDK is compiled into the frontend image either way, so this is a container-start setting, not a rebuild. Read [What is captured](#what-is-captured) before you leave it on. |
| `ASSISTANT_TRANSPORT` | `live` (default) sends the panel's requests to the storefront routes. `fixtures` renders canned answers without an agent call, for reviewing the UI states. |
| `ASSISTANT_DEMO_DETAILS` | Off by default. Set it to `true` to show a collapsed **Demo details** area under each answer with scenario, prompt version and source, tool names, and trace links. The links name the Langfuse and ClickStack backends, so opt in for demo runs rather than for anonymous shoppers. |

Every key is documented in `.env.example` and in [Configuration](#configuration); changing the `ASSISTANT_*`, `AGENT_BASE_URL` or `SESSION_REPLAY` values needs `up` to recreate the frontend container, not a rebuild.

## Run on EKS

The cluster runs the same demo for a room instead of one laptop: a managed node group that scales to zero between demos, an in-cluster ClickStack collector writing to ClickHouse Cloud, and the four custom images pulled from ECR. Nothing in it is reachable from the internet — `kubectl port-forward` is the only access path, so the storefront still opens at `http://localhost:8080`.

`deploy/eks/README.md` is the reference half: why the cluster looks the way it does, what it costs, and how to recover from a lost tunnel, a stuck node group or a lost state file. The runbook is here.

### Prerequisites

- An AWS account and an **IAM Identity Center (SSO) profile** with access to it: `aws configure sso --profile <name>`, then `AWS_PROFILE` in `.env`. Everything deploys into one region (`AWS_REGION`, default `us-east-1`).
- `aws`, `tofu`, `kubectl`, `helm`, `docker` (with `buildx`) and `git` on the path. `demo.py eks init` names whichever is missing.
- A **ClickHouse Cloud** service. Run `deploy/eks/sql/create-user.sql` once in its SQL console with a real password in place of `SECURE_PASSWORD`, then fill in the five ClickStack keys in the EKS section of `.env` (`CLICKHOUSE_ENDPOINT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE`, `OTLP_AUTH_TOKEN`). `OTLP_AUTH_TOKEN` is any long random string; it never leaves the cluster.
- Langfuse keys are optional here, as on the laptop. With them, the collector gets a Langfuse pipeline and the agent gets the Secret; without them the demo runs ClickStack-only.
- An **arm64 build host**. The node group is Graviton, and `publish` refuses to push a manifest whose platform does not match the cluster's, so a wrong build cannot reach it silently. Apple silicon builds `linux/arm64` natively.

`demo.py eks init` and `eks apply` need only the AWS session; they warn about blank ClickStack keys rather than failing, so a presenter can fill `.env` in during the ten minutes the control plane takes.

### First run

```sh
.venv/bin/python scripts/demo.py eks init
.venv/bin/python scripts/demo.py eks apply
.venv/bin/python scripts/demo.py build
.venv/bin/python scripts/demo.py publish
.venv/bin/python scripts/demo.py eks deploy
```

| Step | What it does |
| --- | --- |
| `eks init` | Checks the tools, logs in, creates the versioned OpenTofu state bucket if it is absent, initialises the S3 backend, and adds the chart repository. Idempotent; run it on any new laptop or clone. |
| `eks apply` | `tofu apply` for the VPC lookup, the EKS control plane, the node group, the four ECR repositories and the nightly schedule, then writes the kubeconfig. Interactive: tofu's own prompt reads your terminal, or pass `--yes`. About 15 minutes the first time, and the only step that takes that long. |
| `build` | The same build as the laptop target — one content-derived tag for all four images. |
| `publish` | Logs in to ECR and pushes the four images, refusing a platform that does not match the node group's and skipping a tag the registry already holds unless you pass `--force`. |
| `eks deploy` | Creates the namespaces, applies the NetworkPolicy that keeps everything but the storefront off the agent, creates the Secrets (assembled in Python and piped to `kubectl apply -f -`, never written to disk or to a command line), installs the ClickStack collector, writes the generated Helm values from `.env` and the build manifest, installs the demo release, and opens the tunnel. Safe to re-run. |

> **The first `eks apply` after this merge destroys the old `otel-demo-frontend` ECR repository and every image in it.** This repository uses four repositories, `ch-opentelemetry-demo/{frontend,frontend-proxy,agent,mcp}`, where the pre-merge deployment used the single `otel-demo-frontend`; an ECR repository name forces replacement and there is no `moved` block, so tofu deletes the old repository and every image in it. That is intended — the old images are built from a different upstream pin and cannot serve this release — but it means **`eks apply` must be followed by `build` and `publish` before anything can be deployed.** `eks apply` prints the same warning, and `eks deploy` refuses rather than rolling out a partial set.

`eks deploy` will not proceed until all four images are published at the current build tag. `publish --service frontend` on its own therefore never satisfies it: the gate wants the whole set, because a release that can only name three of four images deploys last week's demo perfectly happily. Re-run `publish` without `--service` after a partial push.

### The everyday cycle

```sh
.venv/bin/python scripts/demo.py eks up        # scale up, wait for nodes, deploy, tunnel
.venv/bin/python scripts/demo.py eks status    # who you are, what is running, what tonight does
.venv/bin/python scripts/demo.py eks verify    # drive one assistant turn, then count what landed
.venv/bin/python scripts/demo.py eks tunnel    # reopen the port-forward
.venv/bin/python scripts/demo.py eks down      # tunnel closed, workloads removed, nodes at zero
```

| Command | What it is for |
| --- | --- |
| `eks up` | Idle to a browsable storefront in about two and a half minutes. It writes the kubeconfig *before* it scales the node group, so an expired SSO session or a cluster that is no longer there fails in seconds rather than after five minutes of nodes you are already paying for. |
| `eks down` | The nightly state: tunnel closed, release uninstalled, both namespaces deleted, node group at zero. `--keep` scales to zero but leaves the workloads in the API, so they reschedule on the way up. |
| `eks tunnel` | Restarts the port-forward; `eks tunnel stop` closes it and `eks tunnel status` exits 0 only when the storefront answers. A 503 from Envoy means the tunnel is fine and the frontend is still starting. |
| `eks status` | Identity, node group size and state, pod phases, the Helm release, the tunnel, whether each of the four images is in ECR at the current tag, and the nightly schedule. Exits 0 whether the demo is idle or up — it is a report, not an assertion. |
| `eks verify` | The demo's claim as a report: it sends one assistant turn through the tunnel, prints that turn's trace ID, then counts the spans, logs, metrics and replay sessions that reached ClickHouse Cloud. The Langfuse half of the same ID is checked in the Langfuse UI. |
| `scenario backend-failure --target eks` | Breaks `GetProduct` for the Explorascope in the cluster's flagd file, exactly as the laptop command does to the laptop's. `scenario shopping --target eks` restores it. |
| `eks flag` | Lists, shows or sets any other flagd flag without a restart; `eks flag --reset` restarts flagd back to the chart defaults. The product-catalog fault is not settable here (its targeting rule always yields a variant, so `defaultVariant` decides nothing for it) — use `scenario` for that, which is why the two commands exist. |
| `eks check` | Offline validation of the whole EKS surface: OpenTofu fmt, init and validate, the collector manifest and the agent's NetworkPolicy parsed, and the chart rendered twice (Langfuse configured and not) with every image override and every referenced collector processor asserted. No AWS credentials, no cluster. |

> **Anything that can reach the tunnel can spend the model credential.** The storefront's
> assistant route has no authentication and the agent behind it runs with the live `API_KEY`
> from the `llm-credentials` Secret, so while the port-forward is open every process on your
> laptop can drive paid model turns through it. Close it with `demo.py eks tunnel stop` when the
> demo is over, and keep `API_KEY` out of `.env` for a scripted-only session. Inside the cluster
> the exposure stops at the storefront: `deploy/eks/k8s/network-policy.yaml` restricts ingress to
> the agent's port 8010 to the frontend pod, so no other pod in `otel-demo` can call it. That
> policy is enforced only because the `vpc-cni` add-on carries `enableNetworkPolicy`
> (`deploy/eks/tofu/eks.tf`) — on a cluster applied before that, re-run `demo.py eks apply`, or
> the API server accepts the policy and filters nothing.

### Costs and the nightly schedule

Idle, only the EKS control plane is billed, about **$73/month**. Up, two `m7g.xlarge` Graviton nodes add about **$0.33/hour**, so roughly $10/day if you forget. An EventBridge Scheduler rule scales the node group to zero every night (default 23:00 `America/New_York`), which bounds a forgotten demo at one day of nodes.

```sh
.venv/bin/python scripts/demo.py eks nightly off   # for a demo that runs past the hour
.venv/bin/python scripts/demo.py eks nightly on
```

`down` is the nightly state and costs nothing but the control plane; `destroy` is the end of the demo.

```sh
.venv/bin/python scripts/demo.py eks destroy
.venv/bin/python scripts/demo.py eks destroy --purge-state   # also deletes the state bucket
```

Cheaper nodes, the cost trims in the cluster module, and the recovery runbooks are all in [deploy/eks/README.md](deploy/eks/README.md).

### The two 8080s

`SHOP_PORT` (the laptop stack) and `EKS_TUNNEL_PORT` (the tunnel) both default to 8080. Running the laptop stack and the tunnel at the same time needs one of them changed in `.env`.

## Configuration

One root `.env`, created from `.env.example` by `bootstrap` and ignored by Git. Keys that only one target reads are dropped before the other sees them: the EKS keys never reach `docker compose`, and only the keys this table marks for EKS are carried into the cluster. On the laptop, use container-reachable addresses — for a service on this computer, `host.docker.internal`, not `localhost`.

| Key | Laptop | EKS |
| --- | --- | --- |
| `SHOP_PORT` | The storefront's port on the Docker host | Ignored: the chart fixes the in-cluster ports, and `EKS_TUNNEL_PORT` chooses the local one |
| `AGENT_HOST_PORT` | The agent's own port on the host, for the legacy `POST /prompt` API and the smoke script | Ignored: nothing outside the cluster reaches the agent |
| `CHAT_PORT` | The Gradio debug client's port (`up --debug-chatbot`) | Ignored: the chart's chatbot component is disabled |
| `AGENT_MODE`, `MCP_ENABLED`, `LLM_BASE_URL`, `LLM_MODEL` | Yes | Yes, through the generated Helm values |
| `API_KEY` | Yes | Yes, as the `llm-credentials` Secret, which is created only when the key is set and deleted when it is not |
| `GRAPH_RECURSION_LIMIT` | Yes | Ignored: the chart's own agent default (25) applies |
| `AGENT_BASE_URL`, `ASSISTANT_TRANSPORT` | Yes. `AGENT_BASE_URL` is server-side only — the storefront's routes call the agent with it, and no agent, model or Langfuse address ever reaches the browser bundle | Fixed by `deploy/eks/k8s/demo-values.yaml` (`http://agent:8010` and `live`) |
| `ASSISTANT_DEMO_DETAILS` | Yes | Yes, through the generated Helm values |
| `CLICKSTACK_OTLP_ENDPOINT`, `CLICKSTACK_API_KEY` | The collector exports straight to your ClickStack deployment with these | Ignored: the cluster's gateway collector exports to the in-cluster ClickStack collector instead, authenticating with `OTLP_AUTH_TOKEN` |
| `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY` | All three or none | All three or none, as the `langfuse-credentials` Secret; without them the collector's Langfuse pipeline is not rendered at all |
| `LANGFUSE_PROJECT_ID`, `LANGFUSE_PUBLIC_URL`, `CLICKSTACK_TRACE_URL_TEMPLATE` | The browser-facing links under **Demo details** | The same, through the generated Helm values |
| `LANGFUSE_PROMPT_LABEL` | Which prompt label the agent reads (`production`, or `budget-check` for a comparison) | The same, through the generated Helm values |
| `SESSION_REPLAY` | `auto` records whenever ClickStack is configured; `true` forces, `false` disables | The same three modes, through the generated Helm values, which derive the frontend's `PUBLIC_HYPERDX_ENABLED` from it. `auto` records on every deployment, because the cluster's gateway collector always exports to ClickStack; `false` is applied by the next `eks deploy` |
| `AWS_PROFILE`, `AWS_REGION` | Ignored | The SSO profile and the region every `eks` subcommand and `publish` works in |
| `CLICKHOUSE_ENDPOINT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE` | Not used by the stack. `eks verify` and `smoke.py --target eks` read them to query what landed | The in-cluster ClickStack collector's ClickHouse Cloud target, as the `clickstack-credentials` Secret |
| `OTLP_AUTH_TOKEN` | Ignored | The shared secret the demo's gateway collector presents to the ClickStack collector, as the `clickstack-otlp-token` Secret |
| `EKS_TUNNEL_PORT` | Ignored | The local port `eks tunnel` forwards the storefront to |

## Use the assistant

The panel opens as a right-side panel at desktop widths (768 px and up) and as a full-screen dialog below that. It starts with **Find the right telescope**, three suggestions (**A telescope for a beginner**, **Help me choose under $150**, **Explain this product**), and the active budget and currency. Product pages add **Ask about this product**, which opens the same panel with that product as context.

Every recommendation is a compact product card whose name, picture, and price come from the agent's successful catalog tool results, never from the reply text. **View product** opens the product page; **Add to cart** performs one agent cart action against the shopper's storefront cart, and the header count, cart dropdown, and cart page update without a reload. Each answer offers **Helpful** / **Not helpful**, which scores that answer's trace in Langfuse and reports whether the score was saved.

### Conversation and cart

- The conversation is bound to the browser's storefront session, so the assistant reads and changes the same cart the storefront shows. A different browser session has its own cart and cannot continue another session's conversation.
- **New conversation** starts a fresh conversation ID and clears the transcript; the cart is unchanged. Closing and reopening the panel, and moving between the home, product, and cart pages, keep the transcript.
- Conversation history lives in the agent process: a conversation expires after an hour of inactivity or 20 turns and is lost when the agent restarts. On page reload the panel resumes only after the agent confirms the conversation is alive; otherwise it starts fresh and says why (previous conversation expired, or the agent could not be reached). `demo.py restart agent` demonstrates this on the laptop.
- One turn is in flight per conversation: the composer and card actions are disabled until it settles. Retries reuse the request ID and the agent deduplicates them, so a repeated click never adds an item twice. A cart action that times out is never retried automatically: the panel refetches the cart and shows an explicit uncertain state until the cart confirms or the shopper retries.
- Prices follow the header currency switcher. Cards, the reply amount, the budget comparison, and the cart use the shop's currency service for the selected currency; original amounts are never relabeled as another currency. Switching currency refreshes card prices and applies to the next turn.
- If the agent is down, the panel shows an inline error with **Retry** and the rest of the shop keeps working.

## Connect ClickStack and Langfuse

Both destinations are external and both are optional, on both targets. Fill in `.env`; [Configuration](#configuration) says which target reads what, and this table is about the values themselves, which are easy to get subtly wrong.

| Key | Value |
| --- | --- |
| `CLICKSTACK_OTLP_ENDPOINT` | Your ClickStack collector's OTLP **HTTP** base URL, without `/v1/traces` |
| `CLICKSTACK_API_KEY` | The ClickStack ingestion key, sent as the raw `authorization` header |
| `LANGFUSE_BASE_URL` | The Langfuse base URL, without `/api/public/otel` and without a trailing slash |
| `LANGFUSE_PUBLIC_KEY` / `LANGFUSE_SECRET_KEY` | Project API keys. All Langfuse keys are all-or-nothing: a half-configured set is refused rather than half-exported |
| `LANGFUSE_PROJECT_ID` | Project ID, used to build browser links |
| `LANGFUSE_PUBLIC_URL` | Optional browser-facing URL when it differs from `LANGFUSE_BASE_URL` |
| `CLICKSTACK_TRACE_URL_TEMPLATE` | Optional trace URL copied from your deployment, with its trace ID replaced by `{trace_id}` |
| `CLICKHOUSE_ENDPOINT` | EKS only: the ClickHouse Cloud **HTTPS** interface, port included |

Apply the configuration and create the two sample prompt versions:

```sh
.venv/bin/python scripts/demo.py config
.venv/bin/python scripts/demo.py up
.venv/bin/python scripts/demo.py seed-prompts
```

Prompt seeding uses the Langfuse public API and is target-independent — one Langfuse project serves the laptop and the cluster. It creates `astronomy-concierge` with labels `production` and `budget-check`, skips matching existing prompts, and stops if a label already has different content. Runtime prompt reads cache for 60 seconds and fall back to the explicitly labeled bundled prompt during an outage. The inspector shows which prompt was used.

The collector sends all shop traces, metrics, and logs to ClickStack. To Langfuse it sends one storefront assistant turn end to end and nothing else the storefront does: the browser's `assistant.turn` span and the fetch under it, the proxy's spans for `/api/assistant/`, the storefront's `/api/assistant/*` route spans and its call to the agent, and every `agent`, `mcp`, and (debug) `chatbot` span except health checks and feedback-score calls. Every ancestor in that chain is kept on purpose, so nothing arrives in Langfuse as an orphan; unrelated storefront traffic and shop-service spans are dropped (`langfuse_span_filter` in `launcher/collector.py`, which is also the single source of that filter for the cluster's generated Helm values). All trace collection uses OpenTelemetry. Operation names, conversation IDs, model names, tool arguments/results, and provider-reported token counts use `gen_ai.*` attributes. Langfuse attributes add observation types, readable input/output, prompt links, and filterable metadata. The exporter uses the v4 ingestion header. Feedback and scripted evaluation scores use `POST /api/public/scores`.

Both destinations receive the same trace IDs. With `ASSISTANT_DEMO_DETAILS=true`, each answer's demo details show the ID and configured links; **Helpful / Not helpful** scores that answer's trace. Two identities travel on every span of a turn: `gen_ai.conversation.id` (also `langfuse.session.id`) is the conversation, so a conversation's turns form one Langfuse session with separate trace IDs, and `session.id` is the storefront (cart) session, the same value the shop's own browser and server spans carry. The API's `/prompt` endpoint also accepts incoming W3C trace context.

Without backend configuration on the laptop, the collector records telemetry in `.runtime/telemetry/telemetry.jsonl`. Files rotate at 20 MB with three backups. This captures structure for local testing; it is **not** a local Langfuse or ClickStack instance. Feedback reports that Langfuse must be configured instead of claiming it was saved. The Langfuse-bound subset is always also written to `.runtime/telemetry/langfuse-preview.jsonl`, with or without credentials, so what Langfuse would receive can be inspected (the smoke script asserts on it). The cluster writes no capture file; its equivalent without Langfuse keys is a Langfuse pipeline that ends in the collector's `debug` exporter, readable with `kubectl logs`.

### What is captured

Capture is verbatim: this is a demo on synthetic data, so the text typed into the composer and the assistant's replies are recorded in full on the spans (`langfuse.observation.input` / `output`) and sent to both destinations, and nothing is masked or redacted.

**Session replay is recorded the same way, and records more.** Whenever the recorder is on — which on both targets means `SESSION_REPLAY`, and by default means whenever ClickStack is configured — the ClickStack browser SDK runs with `advancedNetworkCapture` and `consoleCapture` enabled, so replay sessions carry request and response **bodies** and the browser **console** into ClickHouse. That includes the shopper's messages and the assistant's replies, alongside everything else the page fetched. It is what makes a replay worth watching next to the trace, and it is the same posture as the verbatim spans above: fine for a demo on synthetic data, and not something to point at real shoppers. Set `SESSION_REPLAY=false` if you want the storefront without it: on the laptop the next `up` recreates the frontend container with the recorder off, in the cluster the next `eks deploy` (or `eks up`) recreates its pod.

Do not point either target at real customer data without adding masking.

## Demo walkthrough

The walkthrough is the same on both targets; only the command that injects the fault differs.

| Scenario | Steps | What to inspect |
| --- | --- | --- |
| Shopping | In the storefront panel, keep the $150 budget and send **A telescope for a beginner**. Add the recommendation from its card and open the cart. | `list_products` → `get_product`, the $101.96 Explorascope, one successful `add_to_cart`, the header count, and the conversation's Langfuse session. |
| Backend failure | Run the matching command below, then ask the panel to **Explain this product** from the Explorascope's product page (or send its sample request in the debug client with `backend-failure` selected). | An actual `GetProduct` failure for `OLJCESPC7Z`, the failed tool span, and the assistant's recovery message. |
| Budget violation | Restore the catalog. In the debug client (`up --debug-chatbot`, laptop only), select `budget-violation` and send its sample with the $150 budget. | A deliberately scripted $349.95 recommendation, healthy backend spans, and `budget_adherence=0`. |

```sh
.venv/bin/python scripts/demo.py scenario backend-failure                  # laptop
.venv/bin/python scripts/demo.py scenario backend-failure --target eks     # cluster
# After demonstrating the error:
.venv/bin/python scripts/demo.py scenario shopping
```

The scenario command changes one field in the demo's flagd document — the product-catalog fault's targeting rule — and nothing else: other flags are preserved and the upstream checkout is untouched. `--target local` (the default) writes the overlay's flag file; `--target eks` writes the cluster's copy through `kubectl exec`, then reads it back to confirm. Allow a moment for flagd to pick up a change. An unknown scenario name is an argparse usage error and exits 2 before anything is written, and a flagd document whose product-catalog flag is not `ENABLED` is refused rather than force-enabled: flagd evaluates a targeting rule only while the flag is on, so writing to a disabled flag would land in the file and serve nothing. Upstream ships it `ENABLED`, so the ordinary walkthrough never meets that refusal.

The storefront panel always uses the `shopping` fixture; the other fixtures are selected in the debug client, and the CLI controls the real service fault separately.

Scripted mode is a test fixture, not free-form AI reasoning. Its recommendation and tool sequence are deliberately predictable. It records no invented token counts or model costs. Its budget score checks the known recommended product's actual returned price; it does not evaluate arbitrary live-model prose. In live mode, use **Helpful / Not helpful** for human evaluation or add an evaluator in your Langfuse project.

## Use a live model

Set these values in `.env`, then run `up` again on the laptop, or `eks deploy` for the cluster:

```dotenv
AGENT_MODE=live
LLM_BASE_URL=https://api.openai.com/v1
LLM_MODEL=gpt-4o-mini
API_KEY=your-provider-key
```

The released `ChatLLM` adapter accepts OpenAI-compatible endpoints. The overlay disables VCR replay so changed prompts cannot accidentally hit a fuzzy cassette match. Live calls record provider-reported token usage; Langfuse can calculate cost for recognized models. Custom model pricing must be configured in Langfuse. Live mode uses actual provider quota and billing.

In the cluster the model key is the only one of these that becomes a Secret (`llm-credentials`); the other three ride in the generated Helm values, and `eks deploy` deletes the Secret again if `API_KEY` goes back to blank.

For a prompt comparison, switch `LANGFUSE_PROMPT_LABEL` from `production` to `budget-check` and recreate the agent — `up` on the laptop, `eks deploy` in the cluster. Compare the same shopping request across versions in Langfuse. Changing prompts affects the live model, not the deterministic fixture.

## Optional MCP transport

Set `MCP_ENABLED=True` in `.env` and run `up` (laptop) or `eks deploy` (cluster). The agent exposes the same four scoped tools while delegating their execution to the released MCP service. MCP instrumentation carries trace context and W3C baggage in protocol messages across the persistent connection. An allowlist copies anonymous conversation context onto MCP spans. `/healthz` reports `tools_transport` as `http` or `mcp`.

## Test and develop

```sh
scripts/dev-setup.sh
.venv/bin/pytest -q
.venv/bin/ruff check concierge scripts tests launcher
scripts/check-frontend.sh
.venv/bin/python scripts/demo.py eks check
.venv/bin/python scripts/smoke.py
.venv/bin/python scripts/smoke.py --target eks
```

`dev-setup.sh` creates `.venv` from `requirements-dev.lock` when it is missing and runs `bootstrap`. In a linked git worktree it symlinks `.upstream` and `.venv` from the main checkout and copies its `.env`, so a fresh worktree passes the same checks. `check-frontend.sh` stages the frontend build tree (see `frontend/overlay/README.md`) and type-checks and lints it. `pytest` and `ruff` cover `concierge/`, `launcher/` (the CLI, one module per concern), `scripts/` and the tests themselves, and they touch no network, no AWS and no cluster: everything AWS-shaped is asserted against recorded command lines.

`demo.py eks check` is the other offline gate, and the one that catches what unit tests cannot — values the chart rejects, a collector pipeline naming a processor nobody defines, a manifest Kubernetes will not parse. It may download the pinned OpenTofu providers and the demo chart, which is why it is a subcommand rather than part of `pytest`. It needs no credentials and no cluster, and it names a missing tool rather than skipping the check that needed it.

### The smoke script

`scripts/smoke.py` needs a *running* demo, and which one it exercises is `--target`.

`--target local` (the default) drives the scripted laptop stack over two paths. On the legacy `POST /prompt` API it checks recommendation, cart isolation, budget failure, the actual catalog fault, and cross-service trace correlation. Then it drives one native storefront turn: it exports a synthetic browser span through the proxy's OTLP route, sends a turn through `http://localhost:8080/api/assistant/message` under it, and asserts that `.runtime/telemetry/langfuse-preview.jsonl` holds that parent, every ancestor down to exactly one `concierge.turn`, and the conversation and storefront-session attributes on the storefront's route span. To cover the MCP transport, set `MCP_ENABLED=True` in `.env`, run `up` (it recreates the agent), and run the script again.

`--target eks` drives that same native turn through the `eks tunnel` port-forward and then asserts the demo's central claim where the cluster puts it, as a gate rather than as a report: **one** trace ID, found in ClickHouse Cloud with the whole storefront-to-agent span set intact — `frontend-web`, `frontend-proxy`, `frontend`, `agent` and `product-catalog`, exactly one `concierge.turn`, no orphans, `http.route=/api/assistant/message` — and the same ID answered by `GET /api/public/traces/<id>` when Langfuse is configured. It polls ClickHouse for up to a minute, because the collector batches before it exports. The legacy `/prompt` block is skipped there and says so: it needs the capture file, the agent's host port and the laptop's flag file, none of which a cluster has. Run `eks up` (or at least `eks tunnel`) first; the script names what to run if the tunnel is not answering.

Either target restores the prior catalog flag setting even if a check fails, and detailed results go to `.runtime/smoke-results.json`.

### Browser tests

The released storefront's Cypress suite runs from the staged tree against the running laptop stack. `frontend/overlay/cypress/e2e/Assistant.cy.ts` drives the panel with the scripted agent: open from the header, send **A telescope for a beginner**, check the product card, add to cart, watch the header count, find the item on the cart page, close and reopen with the transcript intact, start a new conversation with the cart untouched, and, at 1440 px and 390 px, open, submit, and close with the keyboard, check the mobile dialog's focus trap and focus return, and read the live region's pending and completed announcements.

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

The EKS target's own timings, measured on a real account and a real ClickHouse Cloud service, are in [deploy/eks/README.md](deploy/eks/README.md#measured-timings-why-scale-to-zero); the full record is under `docs/history/`. Those runs predate this merge — they were made from the EKS repository's shell scripts, at its own upstream pin — so they are evidence for the cluster's shape and timings, not for the current CLI.

The agent and mcp images bake in the package, prompts, and corrected tools, so a Python change needs `demo.py build` and `up`. For faster iteration, `up --dev` bind-mounts `concierge/`, `prompts/`, and `.runtime/tools.py` read-only over those images; after editing, restart the containers, because changing mounted files alone does not reload their processes. The `up` command recreates containers when their configuration changes. Frontend changes always need `build` and `up`. For the cluster, a change needs `build`, `publish` and `eks deploy` — the tag is content-derived, so `publish` and the release both follow the build automatically.

The Gradio interface (`up --debug-chatbot`) is served directly on port 7860; the proxy has no `/chatbot` route, and the cluster disables the component entirely.

## Source and integration notes

- Upstream: [3.0.0](https://github.com/open-telemetry/opentelemetry-demo/releases/tag/3.0.0), commit `1755859a9de82c2e5e225be68abc401a5ebf2b4f`, for both targets. The demo Helm chart is `open-telemetry/opentelemetry-demo` 0.41.0, whose `appVersion` is the same 3.0.0.
- The only staged upstream tool change (`TOOLS_PATCH` in `launcher/upstream.py`) corrects `get_cart` from `user_id` to the frontend's `sessionId` query parameter and gives `list_products`, `get_product`, and `get_cart` a `currency_code` parameter forwarded as `currencyCode`. The corrected file is baked into both the agent and MCP images.
- The overlay subclasses the released `Agent` and `ChatAgentUI`. It uses one OpenTelemetry provider in each overlay process, with explicit model/tool spans, to avoid duplicate LLM instrumentation. The session-replay SDK replaces the storefront's own browser tracer when it is enabled, rather than running beside it, for the same reason; the assistant's spans use only the global `@opentelemetry/api`, so the turn's chain is stitched identically in both modes.
- ClickStack dashboards written for older `app.*` attributes may need the release's `demo.*` names.
- This is a demo with in-memory conversations and unauthenticated chat and API listeners, not a production authentication or persistence design. On the laptop, keep those listeners local. In the cluster they are reachable only through your own `kubectl port-forward`; do not put a load balancer in front of them.
- References: [Langfuse OpenTelemetry](https://langfuse.com/integrations/native/opentelemetry), [prompt API](https://langfuse.com/docs/prompt-management/get-started), [scores API](https://langfuse.com/docs/evaluation/evaluation-methods/scores-via-sdk), [ClickStack collector](https://clickhouse.com/docs/clickstack/ingesting-data/collector), [ClickStack browser SDK](https://clickhouse.com/docs/clickstack/sdks/browser).

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

## History

This repository is the merge of two siblings that overlaid the same OpenTelemetry Demo release and could not be deployed together: `langfuse-sample-agent-app`, which owned the Compose stack, the four custom images and the Langfuse-traced assistant, and `ch-otel-demo-eks`, which owned the OpenTofu and Helm EKS deployment, the in-cluster ClickStack collector and the session-replay frontend patch. Both patched the same three storefront files, and there can only be one frontend image.

The merge, in September 2026, kept this repository's history as the trunk and imported the EKS repository with `git subtree add --prefix=deploy/eks`, so its commits survive here. It settled on one upstream pin (3.0.0), one patch set, one image set, one Python CLI with an `eks` half, and two targets. The EKS repository's shell CLI and its own env files are gone, ported into `launcher/`; its frontend pin, 99 commits past 3.0.0, was retired and its session-replay hunks rebased onto 3.0.0. Internal identifiers were deliberately left alone: the `astronomy-concierge` image prefix, the `concierge/` package, the Compose project name, the Langfuse prompt and trace names, and the OpenTofu cluster name and state key, which could not move without replacing the live cluster.

Planning records for both halves are under `docs/history/` — the native assistant plan and the EKS live-validation transcript — and the completed harness plans, including this merge's, are in `.claude/plans/`.
