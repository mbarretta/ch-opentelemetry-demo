# Merge plan: ch-opentelemetry-demo

## Context

Two sibling repos overlay the same OpenTelemetry Demo 3.0.0 release and cannot be deployed together without cross-repo coupling:

- `langfuse-sample-agent-app` (this repo, the base of the merge): Compose stack for the laptop. Adds a native storefront AI assistant (Python agent in `concierge/` + Next.js overlay and patches under `frontend/`), four custom images (`frontend`, `frontend-proxy`, `agent`, `mcp`) tagged by a content hash, and a Python-generated collector config with a Langfuse traces pipeline. Pinned to demo commit `1755859a` (= tag 3.0.0).
- `ch-otel-demo-eks`: OpenTofu + Helm deployment of the demo to an EKS cluster (Graviton nodes, scale-to-zero, private access via `kubectl port-forward`), an in-cluster ClickStack collector writing to ClickHouse Cloud, and a session-replay frontend patch (`@hyperdx/browser`) built on the Mac and pushed to ECR. ~1365 lines of bash. Frontend pinned to demo commit `d6fd782e` (99 commits after 3.0.0); chart 0.41.0 has `appVersion: 3.0.0`.

Both patch `package.json`, `pages/_app.tsx`, `pages/_document.tsx`; there can only be one frontend image. The demo pitch is one trace ID landing in both ClickStack and Langfuse, which is one collector config with two exporters. Outcome: one repo `ch-opentelemetry-demo`, one upstream pin, one patch set, one image set, one Python CLI, two targets (laptop Compose, EKS Helm), one README that states the purpose: the standard OpenTelemetry demo plus ClickHouse observability via ClickStack (all signals + session replay) plus a Langfuse-instrumented AI shopping assistant inside the Astronomy Shop.

### Decisions taken with the user

| Decision | Choice |
| --- | --- |
| Repository | New public GitHub repo `mbarretta/ch-opentelemetry-demo` (name verified free). This repo's history is the trunk; the EKS repo is imported with `git subtree add --prefix=deploy/eks` (no squash) so its 32 commits survive. Old repos archived only after the user confirms, at the very end. |
| CLI | One unified Python CLI. The EKS bash is ported to `demo.py eks …` subcommands; tofu/helm/kubectl/aws/docker stay shell-outs. The bash, `demo.sh`, `envvars.*` are deleted after the port. |
| Session replay | Compiled into the single frontend image; enabled on both targets whenever ClickStack is configured (`SESSION_REPLAY=auto`). |
| Langfuse | External (Langfuse Cloud) on both targets; keys in a Kubernetes Secret on EKS. |
| Upstream pin | 3.0.0 (`1755859a`) everywhere. The EKS `DEMO_REF=d6fd782e` disappears; replay hunks are rebased onto 3.0.0. |
| Internal identifiers | `astronomy-concierge` image prefix, compose project, Langfuse prompt/trace names, `concierge/` package stay. Only repo name, README, pyproject name/description, and ECR repo names change. |

## Key facts from exploration

**Frontend patch merge (verified with `git apply --check` against 3.0.0 blobs):**
- Replay hunks for `pages/_app.tsx`, `pages/_document.tsx`, `utils/telemetry/FrontendTracer.ts` apply cleanly to 3.0.0 (files identical between the two pins). `package.json` hunk fails (dependency versions differ); `package-lock.json` hunk fails (3,703-line drift) and must be regenerated with `npm install @hyperdx/browser@0.25.1 --package-lock-only` in the staged 3.0.0 tree (`src/frontend/Dockerfile` runs `npm ci`).
- Concierge patches `0001` (`_app.tsx`) and `0006` (`_document.tsx`) edit the same lines as the replay patch in both orders. Repo rule: one patch per upstream file (`frontend/patches/README.md`; `tests/test_frontend_overlay.py::test_patches_touch_distinct_upstream_files`). So the replay changes are folded into `0000`, `0001`, `0006`; `FrontendTracer.ts` and `package-lock.json` become new patches. Patches are regenerated from the pristine export (`demo.py stage`, edit `.runtime/build/src/frontend/…`, `git -C .runtime/build diff -- <path>`), never hand-merged.
- The replay code *replaces* the demo's `WebTracerProvider` path when `NEXT_PUBLIC_HYPERDX_ENABLED === 'true'` (early return before `SessionIdProcessor`, exporter, and auto-instrumentations). The overlay's `AssistantTracing.ts` uses only the global `@opentelemetry/api`, so it compiles unchanged; the `assistant.turn → fetch → proxy → frontend → agent` chain must be smoke-tested with replay on. `session.id` on non-assistant storefront spans is lost in replay mode; compensate with `HyperDX.setGlobalAttributes({ userId, 'session.id': userId })`.
- Server reads `PUBLIC_HYPERDX_ENABLED` / `PUBLIC_HYPERDX_URL` from container env at runtime (same mechanism as `ASSISTANT_TRANSPORT`), browser reads `window.ENV.NEXT_PUBLIC_HYPERDX_*`. Leave URL empty: SDK falls back to `origin + /otlp-http`, which `docker/envoy.tmpl.yaml:50` routes to the collector. The Compose collector already appends `otlp_http/clickstack` to traces, metrics and logs when `CLICKSTACK_OTLP_ENDPOINT` is set, and the logs pipeline has no filter that drops rrweb records. So replay data flows on the laptop once the SDK is in the bundle and the flag is set.

**Build machinery (this repo):** `scripts/demo.py` 781 lines. Tag = sha256 over pin, `src/frontend/{Dockerfile,package-lock.json}` at the pin, overlay files, patch names+contents, corrected `tools.py`, `docker/`, `concierge/`, `prompts/`. Neither `scripts/` nor new `launcher/`/`deploy/` trees feed the tag, so the CLI split cannot change the current tag `58acc12afb99` as long as `TOOLS_PATCH` moves byte-for-byte. No registry push exists. `compose()` layers upstream `compose.yaml` + `compose.agent.yaml` + `compose.concierge.yaml` + `compose.native.yaml` [+ `compose.dev.yaml`] + generated `.runtime/isolation.yaml`. Tests monkeypatch ~30 `scripts.demo` module globals (`demo.RUNTIME`, `demo.run`, `demo.image_exists`, `demo.generate_config`, …). Eight literal `astronomy-concierge-*` image-name assertions in `tests/test_build.py` (unchanged, prefix stays). `NATIVE_AGENT_UI_PLAN.md` is finished planning material that defers exactly "EKS deployment" and "session replay".

**Chart 0.41.0 (unpacked in scratchpad):** per-component keys allowed by `values.schema.json` (`additionalProperties: false`): `enabled`, `imageOverride{repository,tag,pullPolicy}`, `env`/`envOverrides` (items may use `valueFrom.secretKeyRef{name,key,optional}`), `additionalVolumes`, `command`, `resources`. No component-level `envFrom`. `envOverrides` upserts by name onto the chart's `env`. The collector subchart supports `extraEnvsFrom` with `secretRef.optional`. The chart's collector defines `memory_limiter`, `resourcedetection`, `resource`, `transform/sanitize_spans`, `transform/sanitize_logs`, `gen_ai_normalizer`, `filter/sanitize_profiles`, connector `span_metrics`; `batch` exists via the subchart default. The chart's `traces` pipeline is `[memory_limiter, resourcedetection, resource, transform/sanitize_logs, gen_ai_normalizer]` (an upstream slip: `sanitize_spans` is never run). The chart's `transform/sanitize_spans` has a different shape from compose's, so the concierge's `http.route` statements go in their own processor `transform/assistant_routes` instead of being spliced. The chart's agent env has no `OTEL_EXPORTER_OTLP_ENDPOINT` (it sets `TRACELOOP_BASE_URL`); `concierge/telemetry.py:71` needs it. The Envoy template is baked into the proxy image (no ConfigMap), so the proxy is an `imageOverride`. An `otlphttp` exporter with an unset `${env:LANGFUSE_BASE_URL}` fails validation and CrashLoops the collector, so the Langfuse exporter must be conditional, which a static values file cannot express.

**EKS repo:** every script starts with `cd "$(dirname "$0")/.."`; `tf_out` = `tofu -chdir=$REPO_ROOT/tofu output -raw`; single ECR repo `otel-demo-frontend`; S3 backend bucket `otel-demo-eks-tfstate-<account>` and key `otel-demo-eks/terraform.tfstate` are supplied by `init.sh` and are path-independent, so the existing cluster and state carry over. Tofu identifiers that must not change or the cluster is replaced: `var.name = "otel-demo-eks"`, the bucket, the key. `.gitignore` `tofu/...` patterns are root-anchored and break under `deploy/eks/`. `flag.sh`'s `defaultVariant` edit does *not* control `productCatalogFailure` (its `targeting.if` always yields a variant); the concierge `scenario()` correctly flips `targeting.if[1]`. `verify.sh` queries ClickHouse over HTTP with the five `CLICKHOUSE_*` keys.

## Design

### Target layout

```
README.md                      merged: purpose, added functionality, laptop + EKS runbooks, env table
scripts/demo.py                ~8-line shim → launcher.cli.main()
scripts/smoke.py               + --target eks (ClickHouse/Langfuse assertions through the tunnel)
scripts/dev-setup.sh, check-frontend.sh   unchanged roles
launcher/                      Python package (importable with existing pythonpath=["."]; excluded from build contexts)
  core.py                      ROOT, Paths, pins (TAG/COMMIT/REPO), IMAGE_PREFIX, IMAGES, log/die/need, run()/capture() = the only subprocess sites, dotenv()
  upstream.py                  verify_upstream, TOOLS_PATCH (byte-identical move), patch_tools, write_tools, bootstrap
  images.py                    file lists, build_tag, stage, build, manifest, require_images, + publish/record_published/require_published
  collector.py                 langfuse_span_filter, assistant_route_statements, collector_config (laptop)
  stack.py                     environment (+ PUBLIC_HYPERDX_ENABLED from SESSION_REPLAY, drops EKS-only keys), generate_config, compose, scenario_flags (pure) + local writer, seed_prompts
  cli.py                       full argparse tree declared once; imports launcher.eks.*.register()
  eks/config.py                CHART_VERSION="0.41.0", RELEASE="otel-demo", NS_DEMO, NS_CS="clickstack", STATE_KEY, key lists, load_clickstack_env, load_langfuse_env, langfuse_auth_header
  eks/aws.py                   aws_login (SSO), state_bucket, tf_outputs (-json, cached)/tf_out, ecr_login/ecr_has_image/ecr_image_digest, ng_desired/ng_status/ng_scale, scheduler_get/set_state, purge_state_bucket
  eks/k8s.py                   kubeconfig, wait_nodes_ready, ensure_namespace, secret_manifest→apply_manifest (stdin), delete_secret, helm_repo_ensure, helm_upgrade_args, exec_read/exec_write (flagd-ui file)
  eks/values.py                eks_values(env, published, langfuse) → generated Helm values (image overrides, env-derived agent/frontend env, collector Langfuse pieces)
  eks/tunnel.py                start/stop/status/restart + hidden --loop self-invocation (start_new_session, killpg); pidfile .runtime/eks/tunnel.pid
  eks/infra.py                 init, apply, destroy, nightly
  eks/lifecycle.py             deploy, up, down
  eks/flags.py                 flag (generic flagd flags), scenario_write_eks (targeting.if[1])
  eks/ops.py                   verify, status
  eks/check.py                 offline validation (port of check.sh)
concierge/ prompts/ docker/ frontend/ tests/ compose.*.yaml   as today; frontend/patches gains replay hunks
deploy/eks/
  tofu/                        from the EKS repo; ecr.tf → for_each over 4 repos "ch-opentelemetry-demo/<svc>"; outputs ecr_repository_urls (map) + ecr_registry
  k8s/demo-values.yaml         static: backends off, image pullPolicy, chatbot off, agent/mcp/frontend env, traces pipeline fix, extraEnvsFrom
  k8s/clickstack-collector.yaml, sql/create-user.sql
  README.md                    cluster design decisions, costs, recovery (trimmed from the EKS README; runbooks live in the root README)
docs/history/                  NATIVE_AGENT_UI_PLAN.md, eks-live-validation-2026-09-09.md (was LIVE-VALIDATION.md)
.claude/plans/                 both repos' completed plan JSONs; index rebuilt
```

Why `launcher/`: a package next to `scripts/demo.py` with the same name would shadow the module during the split; a root `demo/` collides with `scripts.demo` in tests. `launcher` is the vocabulary `.env.example` already uses. `.dockerignore` (root build context for agent/mcp/proxy) must exclude `deploy` and `launcher`.

### argparse tree (declared in full by the skeleton task; later tasks only fill bodies)

```
demo.py bootstrap | stage | build [--service S]... [--platform P] | publish [--service S]... [--force]
        config|up [--dev] [--debug-chatbot] | down|ps|logs|restart [args...]
        scenario NAME [--target local|eks] | seed-prompts
        eks init | apply [--yes] | deploy | up | down [--keep] | tunnel [stop|status] (hidden --loop --port --namespace)
            | flag [NAME [VARIANT]] | flag --reset | verify | status | nightly on|off | destroy [--purge-state] | check
```

`--dev/--debug-chatbot/--service/--platform` become per-subcommand flags (README already writes `demo.py up --dev`). `publish` and `eks *` do not require the upstream checkout; `publish` requires the manifest. Stubs raise `SystemExit("not implemented yet")` until their task lands.

### Configuration: one root `.env`

`.env.example` gains headed sections:
- `SESSION_REPLAY=auto` (auto = on when ClickStack is configured, `true` forces, `false` disables). `stack.environment()` derives `PUBLIC_HYPERDX_ENABLED`; `compose.native.yaml` frontend gets `- PUBLIC_HYPERDX_ENABLED=${PUBLIC_HYPERDX_ENABLED:-}`.
- EKS: `AWS_PROFILE=`, `AWS_REGION=us-east-1`, `CLICKHOUSE_ENDPOINT`, `CLICKHOUSE_USER=clickstack`, `CLICKHOUSE_PASSWORD`, `HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE=otel`, `OTLP_AUTH_TOKEN`, `EKS_TUNNEL_PORT=8080` (pointer to `deploy/eks/sql/create-user.sql`).

Secrets are assembled in Python from parsed keys and piped to `kubectl apply -f -` as manifests, so the old "verbatim env-file" constraint disappears. `stack.environment()` drops the EKS-only keys before invoking `docker compose`. README env table:

| Key | Laptop | EKS |
| --- | --- | --- |
| `AGENT_MODE`, `MCP_ENABLED`, `LLM_*`, `API_KEY`, `ASSISTANT_DEMO_DETAILS`, `LANGFUSE_*`, `CLICKSTACK_TRACE_URL_TEMPLATE`, `SESSION_REPLAY` | yes | yes (generated values / Secrets) |
| `CLICKSTACK_OTLP_ENDPOINT`, `CLICKSTACK_API_KEY` | collector exports straight to ClickStack | ignored: gateway exports to the in-cluster `clickstack-otel-collector` with `OTLP_AUTH_TOKEN` |
| `CLICKHOUSE_*`, `HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE`, `OTLP_AUTH_TOKEN`, `AWS_*`, `EKS_TUNNEL_PORT` | ignored (`smoke --target eks` reuses `CLICKHOUSE_*`) | in-cluster collector target, AWS identity, tunnel |
| `SHOP_PORT`, `AGENT_HOST_PORT`, `CHAT_PORT`, `AGENT_BASE_URL`, `ASSISTANT_TRANSPORT` | yes | fixed by chart/static values |

### Kubernetes Secrets (all piped via stdin, never on argv or disk)

| Secret (ns) | Keys | Consumers |
| --- | --- | --- |
| `clickstack/clickstack-credentials` | the five ClickStack keys | clickstack collector `envFrom` |
| `otel-demo/clickstack-otlp-token` | `OTLP_AUTH_TOKEN` | gateway collector `extraEnvsFrom` |
| `otel-demo/langfuse-credentials` (optional; deleted when `.env` has no Langfuse keys) | `LANGFUSE_BASE_URL`, `LANGFUSE_PUBLIC_KEY`, `LANGFUSE_SECRET_KEY`, `LANGFUSE_AUTH_HEADER` (`"Basic " + b64(pk:sk)`, shared helper with `stack.environment()`) | collector `extraEnvsFrom` (optional); agent `secretKeyRef` (optional) |
| `otel-demo/llm-credentials` (optional) | `API_KEY` | agent `secretKeyRef` (optional), live mode only |

Langfuse keys are all-or-nothing, same rule as `collector_config()`.

### Static Helm values (`deploy/eks/k8s/demo-values.yaml`)

Keep the existing content (backends off, product-catalog/checkout limits, `otlphttp/clickstack` exporter, `extraEnvsFrom clickstack-otlp-token`) and add:

```yaml
components:
  chatbot: { enabled: false }                       # concierge stack has no Gradio client
  frontend:
    imageOverride: { pullPolicy: IfNotPresent }     # content tags never change meaning
    envOverrides:
      - { name: PUBLIC_HYPERDX_ENABLED, value: "true" }
      - { name: AGENT_BASE_URL, value: http://agent:8010 }
      - { name: ASSISTANT_TRANSPORT, value: live }
  frontend-proxy:
    imageOverride: { pullPolicy: IfNotPresent }
  agent:
    imageOverride: { pullPolicy: IfNotPresent }
    resources: { limits: { memory: 750Mi } }
    envOverrides:
      - { name: USE_VCR, value: "False" }
      - { name: AGENT_BIND, value: 0.0.0.0 }
      - { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: http://$(OTEL_COLLECTOR_NAME):4318 }
      - { name: API_KEY, valueFrom: { secretKeyRef: { name: llm-credentials, key: API_KEY, optional: true } } }
      - { name: LANGFUSE_BASE_URL,  valueFrom: { secretKeyRef: { name: langfuse-credentials, key: LANGFUSE_BASE_URL,  optional: true } } }
      - { name: LANGFUSE_PUBLIC_KEY, valueFrom: { secretKeyRef: { name: langfuse-credentials, key: LANGFUSE_PUBLIC_KEY, optional: true } } }
      - { name: LANGFUSE_SECRET_KEY, valueFrom: { secretKeyRef: { name: langfuse-credentials, key: LANGFUSE_SECRET_KEY, optional: true } } }
  mcp:
    imageOverride: { pullPolicy: IfNotPresent }
    envOverrides:
      - { name: OTEL_EXPORTER_OTLP_ENDPOINT, value: http://$(OTEL_COLLECTOR_NAME):4318 }
opentelemetry-collector:
  extraEnvsFrom:
    - secretRef: { name: clickstack-otlp-token }
    - secretRef: { name: langfuse-credentials, optional: true }
  config:
    service:
      pipelines:
        traces:   # fix the upstream slip and run our route transform ahead of sanitize_spans
          processors: [memory_limiter, resourcedetection, resource, transform/assistant_routes, transform/sanitize_spans, gen_ai_normalizer]
          exporters: [otlphttp/clickstack, debug, span_metrics]
```

### Generated Helm values (`launcher/eks/values.py`, written to `.runtime/eks/values.generated.yaml` at deploy time)

Single source of truth stays in Python (`collector.langfuse_span_filter()`, `collector.assistant_route_statements()`, `images` manifest). Contents:
- `components.<svc>.imageOverride.{repository,tag}` for the four services from `manifest.published` (so `helm upgrade` needs zero `--set`).
- Env-derived plain values: agent `AGENT_MODE`, `MCP_ENABLED`, `LLM_BASE_URL`, `LLM_MODEL`, `LANGFUSE_PROMPT_LABEL`, `LANGFUSE_PROJECT_ID`, `LANGFUSE_PUBLIC_URL`, `CLICKSTACK_TRACE_URL_TEMPLATE`; frontend `ASSISTANT_DEMO_DETAILS`.
- Collector: `processors.transform/assistant_routes` (`trace_statements` from `assistant_route_statements()`), `processors.filter/langfuse` (from `langfuse_span_filter()`), pipeline `traces/langfuse` with receivers `[otlp]`, processors `[memory_limiter, transform/assistant_routes, transform/sanitize_spans, filter/langfuse, gen_ai_normalizer, batch]`, exporters `[otlp_http/langfuse]` when Langfuse is configured, else `[debug]` (valid pipeline, mirrors the laptop's preview-only mode). `exporters.otlp_http/langfuse` = `endpoint: ${env:LANGFUSE_BASE_URL}/api/public/otel`, headers `Authorization: ${env:LANGFUSE_AUTH_HEADER}`, `x-langfuse-ingestion-version: "4"`, only when configured.
- Contains only `${env:…}` references, never a secret value (test asserts, like `tests/test_integration_config.py:27`). The subchart's `kubernetesAttributes` preset only touches pipelines named `traces|metrics|logs|profiles`, so `traces/langfuse` is left alone (desired).

### Bash → Python mapping (behaviors to preserve)

| Bash | Python | Preserve |
| --- | --- | --- |
| `init.sh` | `infra.init` | `need(aws,tofu,docker,git,kubectl,helm)`; bucket create (no LocationConstraint in us-east-1)/versioning/public-block idempotent; `tofu init -backend-config` bucket/key/region; helm repo; warn on blank EKS keys |
| `apply.sh` | `infra.apply(yes)` | refuse before `init`; interactive `tofu apply` (stdin inherited); kubeconfig; `kubectl get nodes`; print next steps (`build`, `publish`, `eks deploy`) |
| `build-frontend.sh` | retired → `build` + `publish` | ECR login via `get-login-password` piped to `docker login --password-stdin`; skip when tag present unless `--force`; refuse when `manifest.platform != tf_out(node_platform)`; record `manifest.published[svc] = {repository, tag, digest}` |
| `deploy.sh` | `lifecycle.deploy` | order: env + `require_published` gate → login/kubeconfig → zero-node refusal → namespaces (JSON manifest on stdin, idempotent) → `clickstack-credentials` → apply collector manifest, `rollout status --timeout=300s`, tail 40 logs → `clickstack-otlp-token` → `langfuse-credentials`/`llm-credentials` apply-or-delete → write generated values → `helm upgrade --install otel-demo open-telemetry/opentelemetry-demo --version 0.41.0 -n otel-demo -f demo-values.yaml -f values.generated.yaml --wait --timeout 20m` → tunnel start (failure tolerated) → print URLs (`/`, `/feature/`, `/loadgen/`) |
| `up.sh` / `down.sh` | `lifecycle.up/down(keep)` | scale → wait Ready + coredns → deploy; down = tunnel stop, `helm uninstall --ignore-not-found --wait --timeout 5m`, delete both namespaces `--timeout=300s`, scale 0; `--keep` skips workload removal |
| `tunnel.sh`, `k8s.sh` loop | `tunnel.py` | detached loop (`sys.executable scripts/demo.py eks tunnel --loop`, `start_new_session=True`, log to `.runtime/eks/tunnel.log`) re-running `aws sso login` on expiry and `kubectl -n otel-demo port-forward --address 127.0.0.1 svc/frontend-proxy P:P`; refuse when pidfile alive or port busy; probe 20×1 s (503 from Envoy counts as up); `stop` = `killpg` TERM then KILL after 5 s; stale pidfile cleaned; `status` exit 0/1 |
| `flag.sh` | `flags.flag(name, variant, reset)` | read the fsnotify-watched copy via `kubectl -n otel-demo exec deploy/flagd -c flagd-ui -- cat /app/data/demo.flagd.json`; write with `exec -i … sh -c 'cat > F.tmp && mv F.tmp F'` (inode swap like `sed -i`); validate flag and variant; no-op when set; read back after 2 s; `--reset` = `rollout restart deploy/flagd` + `rollout status --timeout=120s`; header note that `productCatalogFailure` is scenario-controlled |
| (new) | `flags.scenario_write_eks(name)` and `demo.py scenario NAME --target eks` | same `stack.scenario_flags()` pure transform as the laptop (`targeting.if[1]`), written through the same exec path; re-read and assert |
| `verify.sh` | `ops.verify` | same four ClickHouse SQL queries via `httpx` with in-process auth (never argv), plus agent-span count, `/api/assistant/%` route span count, and one `POST /api/assistant/message` through the tunnel reporting its trace id; collector pods and logs; replay rows reported, not failed, on zero |
| `status.sh` | `ops.status` | identity, node group, nodes (when desired > 0), pod phases (`Counter`), helm release, tunnel, ECR presence for all four images at the manifest tag ("no manifest / not published" hints), schedule; exit 0 idle or up |
| `nightly.sh` | `infra.nightly(state)` | `get-schedule` JSON → `update-schedule` with every field fed back (it replaces the whole schedule) → `get-schedule --output table` |
| `destroy.sh` | `infra.destroy(purge)` | tunnel stop, best-effort `down`, interactive `tofu destroy`; `--purge-state` deletes all object versions and delete markers in ≤1000-key batches, then the bucket, then `rm -rf deploy/eks/tofu/.terraform` |
| `check.sh` | `check.check` | `tofu fmt -check`, `init -backend=false -input=false`, `validate`; kustomize or `--dry-run=client` parse of the collector manifest; `helm template` twice (Langfuse on/off generated values, `example/<svc>:check` overrides) asserting all four image overrides landed, every referenced collector processor/exporter is defined in the rendered ConfigMap, agent Deployment carries the optional `langfuse-credentials` refs, no chatbot Deployment |

ECR change is the only planned tofu diff: destroy `otel-demo-frontend`, create four `ch-opentelemetry-demo/<svc>` repos (MUTABLE so identical content tags can be re-pushed; lifecycle keeps 5), outputs `ecr_repository_urls` (map; `tf_outputs()` reads `-json`) and `ecr_registry`.

### Frontend patch set after the merge

| Patch | Upstream file | Change |
| --- | --- | --- |
| `0000-package-lint-and-hyperdx.patch` (renamed) | `package.json` | `"lint": "eslint ."` + `"@hyperdx/browser": "^0.25.1"` |
| `0001-app-assistant-provider.patch` | `pages/_app.tsx` | existing + `NEXT_PUBLIC_HYPERDX_ENABLED?`, `NEXT_PUBLIC_HYPERDX_URL?` in `window.ENV` typing |
| `0006-document-assistant-env.patch` | `pages/_document.tsx` | existing + destructure `PUBLIC_HYPERDX_ENABLED`, `PUBLIC_HYPERDX_URL`; emit `NEXT_PUBLIC_HYPERDX_*` in `envString` |
| `0009-frontend-tracer-session-replay.patch` (new) | `utils/telemetry/FrontendTracer.ts` | the EKS `HyperDX.init` block (`url: NEXT_PUBLIC_HYPERDX_URL || origin + '/otlp-http'`, `apiKey: 'managed-clickstack'`, `tracePropagationTargets: [/.*/]`, `consoleCapture`, `advancedNetworkCapture`, `demo.synthetic_request` resource attr) + `setGlobalAttributes({ userId, 'session.id': userId })` |
| `0010-package-lock-hyperdx.patch` (new) | `package-lock.json` | output of `npm install @hyperdx/browser@0.25.1 --package-lock-only` in `.runtime/build/src/frontend` |

Register `0009`/`0010` in `EXPECTED_PATCH_TARGETS` (`tests/test_frontend_overlay.py:78-90`); keep `test_patches_touch_distinct_upstream_files` green; the existing "exactly one `new WebTracerProvider(`" assertion on the released tracer still holds (HyperDX constructs its provider internally).

### Testing pattern

Generalize the existing `monkeypatch.setattr(demo, "run", …)` stub (`tests/test_build.py:251-268`) into a `fake_sh` fixture in `tests/conftest.py` recording every `core.run`/`core.capture` argv and stdin and answering from a prefix table. Existing tests keep their assertions and only move patch targets (`core.paths`, `upstream.COMMIT`, `upstream.TOOLS_PATCH`, `images.image_exists`, `core.run`, `stack.generate_config`); cross-module reads inside `launcher` must be attribute access at call time (`core.paths`, not `from .core import paths`) so patches take effect. New unit tests per module (argparse, secret manifest and "no secret string in any recorded argv", helm argv, publish gating, deploy ordering, generated values with/without Langfuse, tunnel pidfile, flag/scenario/nightly/purge). Network-touching validation lives in `demo.py eks check`, not the default pytest run. `ruff` targets gain `launcher`.

## Execution

### Pre-flight (done directly in the main checkout by the orchestrator, one commit each; not harness tasks)

1. `git subtree add --prefix=deploy/eks https://github.com/mbarretta/ch-otel-demo-eks.git main` (no `--squash`; the merge commit needs `-c commit.gpgsign=false` like the harness merges).
2. Housekeeping commit: `git mv deploy/eks/.claude/plans/feat-otel-demo-eks.json .claude/plans/` + `plan-edit.sh rebuild-index`; `git mv deploy/eks/LIVE-VALIDATION.md docs/history/eks-live-validation-2026-09-09.md`; `git mv NATIVE_AGENT_UI_PLAN.md docs/history/`; `.gitignore` += `deploy/eks/tofu/.terraform/`, `deploy/eks/tofu/*.tfstate*`, `deploy/eks/tofu/terraform.tfvars`, `deploy/eks/.run/`, `deploy/eks/opentelemetry-demo/`, `deploy/eks/envvars.*`; remove `deploy/eks/.gitignore`; `.dockerignore` += `deploy`, `launcher`; `pyproject.toml` name `ch-opentelemetry-demo`, description updated.
3. `gh repo create mbarretta/ch-opentelemetry-demo --public --description "OpenTelemetry Demo with ClickStack observability, session replay, and a Langfuse-traced AI shopping assistant"`; `git remote set-url origin https://github.com/mbarretta/ch-opentelemetry-demo.git`; `git push -u origin main`. (Directory rename on disk is optional and the user's call; nothing in the repo depends on the checkout path except the harness plan index, which `rebuild-index` fixes.)
4. Create the harness plan `feat-merge-ch-otel-demo` from the tasks below and run `/harness manage`.

### Harness tasks (waves of file-disjoint tasks)

Verification command for every task: `scripts/dev-setup.sh && .venv/bin/ruff check concierge scripts tests launcher && .venv/bin/pytest -q && ([ ! -x scripts/check-frontend.sh ] || scripts/check-frontend.sh)`.

| Wave | Task | Files | Acceptance criteria |
| --- | --- | --- | --- |
| 1 | **T1 Split `scripts/demo.py` into `launcher/`** (`core`, `upstream`, `images`, `collector`, `stack`, `cli`); shim; rewire test patch targets; fix `scripts/smoke.py` import; add `launcher` to ruff targets | `scripts/demo.py`, `launcher/{__init__,core,upstream,images,collector,stack,cli}.py`, `tests/test_build.py`, `tests/test_integration_config.py`, `tests/compose_yaml.py`, `tests/conftest.py`, `scripts/smoke.py`, `pyproject.toml` (ruff) | pytest + ruff green; `demo.py config` output identical before/after; `images.build_tag()` still `58acc12afb99` on the same tree; `grep -rn subprocess launcher` hits only `core.py`; every laptop subcommand still works (`ps`, `logs agent`) |
| 2 | **T2 CLI skeleton + config** | `launcher/cli.py`, `launcher/eks/{__init__,config,aws,k8s,values,tunnel,infra,lifecycle,flags,ops,check}.py` (signatures + `register()`; bodies raise `SystemExit("not implemented yet")`), `.env.example` (SESSION_REPLAY + EKS section), `tests/conftest.py` (`fake_sh`), `tests/test_cli.py`, `tests/test_eks_config.py` | full tree parses (`eks tunnel stop`, `build --service agent`, `logs agent frontend`, `scenario x --target eks`, hidden `--loop`); `demo.py --dev up` rejected; `demo.py up --dev` accepted; `load_clickstack_env` names every blank key; `load_langfuse_env` all-or-nothing + auth header; `stack.environment()` drops EKS-only keys (test) |
| 2 | **T3 `deploy/eks` data** | `deploy/eks/**` only: `tofu/ecr.tf` (for_each 4 repos), `tofu/outputs.tf` (map + registry), `k8s/demo-values.yaml` (static additions above), `README.md` (trimmed to cluster design/costs/recovery, commands as `demo.py eks …`); delete `scripts/`, `demo.sh`, `envvars.*`, `patches/`, `.run/` | `tofu -chdir=deploy/eks/tofu fmt -check && init -backend=false && validate` pass; `helm template … -f demo-values.yaml --set-string components.{frontend,frontend-proxy,agent,mcp}.imageOverride.{repository,tag}=example/…` renders, no chatbot Deployment, agent env has the optional secretKeyRefs and `OTEL_EXPORTER_OTLP_ENDPOINT`; no bash left under `deploy/eks`; values file has no account-specific value |
| 2 | **T4 Session replay in the frontend image + Compose gating** | `frontend/patches/0000,0001,0006,0009,0010`, `frontend/patches/README.md`, `tests/test_frontend_overlay.py` (registry), `compose.native.yaml`, `launcher/stack.py` (`environment()` derives `PUBLIC_HYPERDX_ENABLED`), `tests/test_integration_config.py` (additive), `docker/README.md` (platform table note) | `demo.py stage` applies all patches; `scripts/check-frontend.sh` passes; `demo.py build --service frontend` succeeds; `up` then browser visit with `SESSION_REPLAY=true` yields `resourceLogs` records with `rum.sessionId` in `.runtime/telemetry/telemetry.jsonl`; `scripts/smoke.py` passes with replay on (native chain intact); `SESSION_REPLAY=false` renders empty `PUBLIC_HYPERDX_ENABLED` |
| 3 | **T5 aws + k8s helpers** | `launcher/eks/{aws,k8s}.py`, `tests/test_eks_aws.py`, `tests/test_eks_k8s.py` | `tf_outputs` parsed once per process; `ng_scale` no-op path and UPDATING wait; `secret_manifest` base64/JSON shape; no secret value in any recorded argv; `helm_upgrade_args` exact argv (`-f` order, `--wait --timeout 20m`) |
| 3 | **T6 tunnel** | `launcher/eks/tunnel.py`, `tests/test_eks_tunnel.py` | pidfile lifecycle (stale cleanup, refuse when alive/port busy); Popen with `start_new_session=True`; `killpg` TERM→KILL; `--loop` stdlib-only; status exit codes |
| 3 | **T7 generated Helm values** | `launcher/eks/values.py`, `tests/test_eks_values.py` | `otlp_http/langfuse` present iff Langfuse configured, `traces/langfuse` exporters `[debug]` otherwise; processors list exactly as specified; image overrides for all four from `published`; env-derived agent/frontend overrides; no secret string in `yaml.safe_dump` output; filter/route statements equal `collector.langfuse_span_filter()`/`assistant_route_statements()` |
| 4 | **T8 infra** | `launcher/eks/infra.py`, `tests/test_eks_infra.py` | init bucket branches + backend-config argv; apply refuses before init; nightly feeds back every field; destroy purge batches ≤1000 and deletes bucket last |
| 4 | **T9 publish** | `launcher/images.py` (additive), `launcher/eks/aws.py` (additive `ecr_login`, `ecr_image_digest` only), `tests/test_publish.py` | platform gate with `build --platform` hint; skip-unless-force; `docker tag`/`push` argv; password on stdin; `manifest.published` merge; `require_published` rejects missing/stale |
| 4 | **T10 flags + scenario target** | `launcher/eks/flags.py`, `launcher/stack.py` (`scenario_flags` pure + local writer), `tests/test_scenario.py`, `tests/test_eks_flags.py` | `scenario_flags` flips only `targeting.if[1]` (moved `test_fault_targets_only_selected_product`); eks writer uses temp+mv exec argv and re-reads; `flag` validates name/variant, no-op when set, `--reset` restarts |
| 4 | **T11 ops** | `launcher/eks/ops.py`, `tests/test_eks_ops.py` | verify: SQL over `httpx` with auth kwarg (assert not in argv), added agent/assistant-route queries, one assistant POST; status: pod-phase counting, ECR presence per service, exit 0 idle and up |
| 5 | **T12 lifecycle** | `launcher/eks/lifecycle.py`, `tests/test_eks_deploy.py` | ordering per mapping table (gate → login → zero-node refusal precedes any kubectl); Langfuse-absent path deletes the secret and generated values omit the exporter; `llm-credentials` only when `API_KEY` set; helm argv has two `-f` and zero `--set`; `up`/`down` sequences; `--keep` |
| 5 | **T13 smoke `--target eks`** | `scripts/smoke.py`, `tests/test_smoke_queries.py` | native turn through the tunnel; ClickHouse poll (≤60 s) on `otel.otel_traces` by trace id asserting services ⊇ {frontend-web, frontend-proxy, frontend, agent, product-catalog}, one `concierge.turn`, no orphans, `http.route=/api/assistant/message`; Langfuse `GET /api/public/traces/<id>` when configured; legacy `/prompt` block skipped on eks; local mode unchanged |
| 5 | **T14 `eks check`** | `launcher/eks/check.py`, `tests/test_eks_check.py` | tofu fmt/init/validate; collector manifest parse; two `helm template` renders with assertions listed in the mapping table; names missing tools instead of skipping |
| 6 | **T15 README + docs** | `README.md`, `deploy/eks/README.md` (cross-links only), `docker/README.md`, `frontend/overlay/README.md` (if mentions), `.env.example` comments | see README outline below; every command in the README exists in `demo.py --help`; env table matches `.env.example` sections; no reference to `demo.sh`, `envvars.*`, `build-frontend`, `langfuse-sample-agent-app`, `ch-otel-demo-eks` except in the history note |

Managed-mode expectations: all waves to runners; security audit and quality review at cycle end; the plan JSON records signing disabled as before.

### README outline (T15)

1. Title `# ch-opentelemetry-demo` + one paragraph: the OpenTelemetry Demo 3.0.0 Astronomy Shop, unchanged upstream, with three additions: (a) ClickHouse observability via ClickStack for every signal, (b) browser session replay via the ClickStack browser SDK, (c) a native AI shopping assistant in the storefront whose agent turns are traced end to end into Langfuse and correlated by trace ID with ClickStack. Two ways to run it: Docker Compose on a laptop, or an EKS cluster you own.
2. **What is added** table: component → where it lives → what you see (storefront Assistant panel; ClickStack traces/logs/metrics/sessions; Langfuse sessions/traces/scores/prompts; feature-flag fault scenarios).
3. **Architecture** diagram (browser → Envoy → frontend `/api/assistant` → agent → shop services; collector fan-out to ClickStack and Langfuse; same trace id in both).
4. **Run on a laptop** (current Start section, plus `SESSION_REPLAY`).
5. **Run on EKS**: prerequisites (aws, tofu, kubectl, helm, docker buildx), `.env` EKS section, runbook `demo.py eks init → eks apply → build → publish → eks deploy` (or `eks up`), everyday cycle (`eks up/down/tunnel/status/verify`, `scenario --target eks`, `eks flag`), costs and nightly scale-down, link to `deploy/eks/README.md` for design decisions and recovery.
6. **Configuration** env table (laptop vs EKS).
7. **Use the assistant**, **Connect ClickStack and Langfuse**, **Demo walkthrough** (both targets), **Live model**, **MCP transport**, **Test and develop** (+ `eks check`, `smoke --target eks`), **Source and integration notes**, **Langfuse skill integration review** — carried from the current README, edited for the two targets.
8. **History** note: merged from `langfuse-sample-agent-app` and `ch-otel-demo-eks` on the merge date; planning records under `docs/history/`.

### Post-merge live validation (manual, needs AWS + ClickHouse Cloud + Langfuse credentials)

1. `demo.py eks init` then `demo.py eks apply` — plan shows only the ECR repository changes.
2. `demo.py build` (arm64 host), `demo.py publish` — four images in ECR, `manifest.published` recorded, `eks status` shows all present.
3. `demo.py eks up` — collector and demo release healthy; `kubectl -n otel-demo get deploy -o jsonpath` shows the ECR tags on the four custom services; tunnel up.
4. Browser: storefront at `http://localhost:8080`, Assistant panel turn, Add to cart; `demo.py eks verify` shows agent spans, assistant route spans, and a `hyperdx_sessions` row; Langfuse shows the session with one trace per turn; ClickStack shows the same trace id.
5. `scripts/smoke.py --target eks` passes; `demo.py scenario backend-failure --target eks` makes "Explain this product" fail for the Explorascope, `shopping` restores it.
6. Laptop regression: `demo.py build && demo.py up` with `SESSION_REPLAY=auto` and ClickStack configured; `scripts/smoke.py`; Cypress `Assistant.cy.ts`.
7. With the user's confirmation only: archive `mbarretta/langfuse-sample-agent-app` and `mbarretta/ch-otel-demo-eks` on GitHub with a pointer in their descriptions.

### Risks

- HyperDX tracer swap: the `assistant.turn` chain relies on HyperDX's fetch instrumentation and context manager; T4's smoke gate is the check. Fallback if it breaks: keep the demo tracer and add only the session recorder — a follow-up task, not part of this plan.
- Chart processor names are verified against 0.41.0 only; `eks check` locks them in for future chart bumps.
- `linux/amd64` remains untested; `publish` refuses a platform mismatch so a wrong build cannot reach the cluster silently.
- Two laptops binding 8080: `EKS_TUNNEL_PORT` and `SHOP_PORT` both default to 8080; running the laptop stack and the tunnel together requires changing one (documented).
