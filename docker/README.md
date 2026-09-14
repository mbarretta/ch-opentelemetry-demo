# Native images

`scripts/demo.py build` produces the four images both targets run from and records them in
`.runtime/images/manifest.json`, which `demo.py up` requires.
The build itself publishes nothing: the images exist only in the local Docker engine until
`demo.py publish` pushes them to the EKS target's ECR repositories, which records what it pushed
in the same manifest.

| Service | Dockerfile | Context | Base image | Entry point |
| --- | --- | --- | --- | --- |
| frontend | released `src/frontend/Dockerfile`, unchanged | `.runtime/build` (staged 3.0.0 tree, see `frontend/overlay/README.md`) | the released Dockerfile's own Node images | released |
| agent | `docker/agent.Dockerfile` | repository root | `ghcr.io/open-telemetry/demo:3.0.0-agent@sha256:...` | `python -m concierge.run_agent` |
| mcp | `docker/mcp.Dockerfile` | repository root | `ghcr.io/open-telemetry/demo:3.0.0-mcp@sha256:...` | `python -m concierge.run_mcp` |
| frontend-proxy | `docker/frontend-proxy.Dockerfile` | repository root | `ghcr.io/open-telemetry/demo:3.0.0-frontend-proxy@sha256:...` | released (`envsubst` then `envoy`) |

The agent and mcp images add `concierge/`, `prompts/`, and the corrected shop tools that
`bootstrap` writes to `.runtime/tools.py`, at `src/agents/tools.py` (agent) and
`src/mcp_server/tools.py` (mcp), the paths the released code imports. The proxy image replaces
only `envoy.tmpl.yaml`. Every other shop service keeps its released
`ghcr.io/open-telemetry/demo:3.0.0-*` image; `demo.py config` lists them.

The frontend image also carries the ClickStack browser SDK (`@hyperdx/browser`): patches `0000`
and `0010` add it to the staged `package.json` and lockfile, so the released Dockerfile's
`npm ci` installs it and `next build` bundles it (see `frontend/patches/README.md`, Session
replay). Session replay is therefore a property of the single image on both targets, and
`PUBLIC_HYPERDX_ENABLED` decides at container start whether the bundled SDK initializes.

## Base image digests

`base-images.json` records the digest each `FROM` line pins and how it was resolved:

```sh
docker buildx imagetools inspect ghcr.io/open-telemetry/demo:3.0.0-agent
```

The `FROM` lines use the multi-platform index digest, so `--platform` selects the matching
manifest; `platforms` in the JSON lists the per-platform digests that index points to. A test
checks that every Dockerfile matches the JSON. To move to a new release, re-run the inspect
command, update the JSON and the `FROM` lines together, and let the tag change.

## Tag and manifest

All four images share one tag: the first 12 hex digits of a SHA-256 over the build inputs.

| Input | Covers |
| --- | --- |
| upstream commit | the pinned 3.0.0 source |
| `src/frontend/Dockerfile`, `src/frontend/package-lock.json` (at the pin) | the frontend build and its lockfile |
| `frontend/overlay/**` (except its README), `frontend/patches/*.patch` | storefront changes, the session-replay SDK among them |
| `docker/**` (except this README) | Dockerfiles with their base digests, `base-images.json`, the Envoy template |
| `concierge/**`, `prompts/**` (no bytecode) | the Python package and prompts |
| corrected `tools.py` | derived from the pin and `TOOLS_PATCH` in `launcher/upstream.py` |

Editing any of them, for example `prompts/concierge-v1.txt`, changes the tag, and `demo.py up`
warns when the manifest's tag no longer matches the working tree.

`.runtime/images/manifest.json` records `tag`, `platform`, `upstream_commit`,
`contract_version` (from `concierge/contract.py`), `base_images`, and per-service `image`
and `id`. Services built separately with `--service` keep their entries.

## Platforms

`build` targets the Docker host's platform unless `--platform` says otherwise. The tag does not
encode the platform: a second platform's images replace the local images under the same tag,
and the manifest's `platform` records whichever was built last. Build one platform at a time,
and build the host platform again before `up`.

The platform matters to the EKS target too: its node group is Graviton, so `demo.py publish`
refuses to push a manifest whose `platform` does not match the cluster's and names the
`build --platform` that would fix it. A wrong build cannot reach the cluster silently.

| Platform | Status on September 11, 2026 |
| --- | --- |
| `linux/arm64` | Built and tested (Docker Desktop on Apple silicon): Cypress, and `scripts/smoke.py` over HTTP and MCP transports. |
| `linux/amd64` | Untested. `build --platform linux/amd64` was not run; the released base images publish `amd64` manifests, so the build path exists but nothing has been verified. |

## Commands

```sh
.venv/bin/python scripts/demo.py build                       # all four, host platform
.venv/bin/python scripts/demo.py build --service agent --service mcp
.venv/bin/python scripts/demo.py build --platform linux/amd64
.venv/bin/python scripts/demo.py publish                     # the same four into ECR, for EKS
.venv/bin/python scripts/demo.py publish --service agent --force
.venv/bin/python scripts/demo.py up                          # native stack from the manifest
.venv/bin/python scripts/demo.py up --dev                    # plus compose.dev.yaml source mounts
.venv/bin/python scripts/demo.py up --debug-chatbot          # plus the Gradio client on CHAT_PORT
```

`up` refuses to start before every image in the manifest exists locally and names the missing
ones; `publish` skips a tag the registry already holds unless `--force` says otherwise, and
`demo.py eks deploy` refuses until all four are published at the current tag, so a partial
`--service` push never becomes a partial release. The native stack has no application-code bind mounts on `agent`, `mcp`, or `frontend`;
`--dev` mounts `concierge/`, `prompts/`, and `.runtime/tools.py` over the agent and mcp images
so a container restart picks up Python edits. The chatbot has no image of its own: it is the
released chatbot image with the package mounted, kept behind the Compose `debug` profile.

The frontend image bakes in no assistant settings. `compose.native.yaml` passes `AGENT_BASE_URL`,
`ASSISTANT_TRANSPORT`, `ASSISTANT_DEMO_DETAILS`, and `PUBLIC_HYPERDX_ENABLED` from `.env` to the
frontend container when it starts, so changing them needs `up`, not a rebuild.
`PUBLIC_HYPERDX_ENABLED` is derived, not read: `SESSION_REPLAY` is `auto` (replay on whenever
ClickStack is configured), `true`, or `false`, and off renders empty. Demo details are off unless
`ASSISTANT_DEMO_DETAILS=true`; that opt-in shows the scenario, prompt, tool calls, and trace
links, which name the Langfuse and ClickStack backends, under each answer, so use it for demo
runs only. `tests/test_integration_config.py` checks the rendered default.

## Envoy template

`envoy.tmpl.yaml` is the released `src/frontend-proxy/envoy.tmpl.yaml` with two changes:

- a `/api/assistant/` route with a 120 s timeout, ahead of the catch-all, so an agent turn is
  not cut off by Envoy's 15 s default. It points at `frontend-assistant`, a copy of the
  `frontend` cluster under a second name: with `spawn_upstream_span`, Envoy emits a
  `router <cluster> egress` span per request that carries the cluster name and no URL, and the
  name is what lets the collector's Langfuse filter keep the assistant route's egress span
  (`langfuse_span_filter` in `launcher/collector.py`) while dropping the rest of the storefront's;
- the `/chatbot` routes and the `chatbot` cluster removed, so the proxy starts without the
  Gradio service.

Every other route, cluster, and timeout is unchanged, and the `${VAR}` placeholders are still
rendered by `envsubst` at container start. `tests/test_build.py` compares the template against
the released one.

## Build context

The root `.dockerignore` limits the agent, mcp, and proxy contexts to `concierge/`, `prompts/`,
`docker/`, and `.runtime/tools.py`. Secrets (`.env`), the example env, the virtualenv, the
upstream checkout, caches, telemetry captures, compose files, docs, tests, and scripts stay out.
The staged tree's `.dockerignore` (upstream's plus the rules `stage` appends) keeps `.env`
files, `node_modules`, `.next`, and Cypress run artifacts out of the frontend context. To list a
context:

```sh
printf 'FROM busybox\nCOPY . /ctx\nRUN find /ctx -type f | sort\n' | docker build --no-cache --progress=plain -f - .
```
