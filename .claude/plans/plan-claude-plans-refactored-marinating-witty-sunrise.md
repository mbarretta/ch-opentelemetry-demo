# Harness plan: merge `ch-otel-demo-eks` into this repo as `ch-opentelemetry-demo`

## Context

`.claude/plans/refactored-marinating-rain.md` is a finished merge design, written in a prior
session with the user's decisions already recorded. It is not a harness plan — it is the
design document a harness plan should be *authored from*. This invocation (`/harness plan`)
turns it into one: a plan JSON at `.claude/plans/feat-merge-ch-otel-demo.json`, 15 tasks
across 6 file-disjoint waves, ready for `/harness manage`.

The work itself: two sibling repos overlay the same OpenTelemetry Demo 3.0.0 release and
cannot be deployed together. This repo (`langfuse-sample-agent-app`) owns the Compose stack,
the four custom images, and the Langfuse-traced AI assistant; `ch-otel-demo-eks` owns the
OpenTofu/Helm EKS deployment, the in-cluster ClickStack collector, and a session-replay
frontend patch. Both patch the same three frontend files and there can only be one frontend
image. Outcome: one repo, one upstream pin, one patch set, one image set, one Python CLI, two
targets (laptop Compose, EKS Helm).

Read `refactored-marinating-rain.md` for the full design — target layout, argparse tree,
Kubernetes Secret table, the static/generated Helm values split, and the bash → Python
behavior-preservation mapping. This file records what the harness plan phase adds: verification
of the design's load-bearing claims, the corrections that verification turned up, the conflict
analysis, and the task/wave/AC set to be written into the plan JSON.

## What I verified, and what was wrong

The design was written from exploration in another session. I re-checked its load-bearing
claims against both working trees. **Confirmed correct:** `scripts/demo.py` is 781 lines and
`smoke.py` 259; `frontend/patches/` holds `0000`–`0008` and `patches/README.md:1-27` states the
one-patch-per-upstream-file rule and the regenerate-from-pristine-export workflow;
`tests/test_frontend_overlay.py:166-171` enforces that rule dynamically; exactly 8 literal
`astronomy-concierge-*` assertions in `tests/test_build.py`; the EKS replay patch targets
exactly the 5 claimed files; nothing under `scripts/` feeds `build_tag()` (verified input by
input at `demo.py:212-236`), so the split cannot move the tag; `tofu/variables.tf:10` has
`var.name` default `otel-demo-eks` and `tofu/backend.tf` deliberately omits bucket/key/region
so `init.sh`'s `-backend-config` flags are path-independent — the live cluster and state
survive the move; `tofu/outputs.tf` already exposes `node_platform` for the publish gate;
EKS repo has 32 commits, so `subtree add` without `--squash` preserves them.

### Corrections the design needs

1. **Pre-flight has not started.** `deploy/`, `launcher/` and `docs/` do not exist. Pre-flight
   steps 1–3 must land before **wave 1** — not merely before T3's wave, because the
   housekeeping commit also touches `pyproject.toml` and `.dockerignore`, and T1 touches
   `pyproject.toml`.

2. **`.claude/` is untracked in the EKS repo.** `git ls-files .claude` there returns nothing,
   so `git subtree add` will not import it and the design's
   `git mv deploy/eks/.claude/plans/feat-otel-demo-eks.json .claude/plans/` **will fail**.
   Copy from the sibling working tree instead. (Also good news: the untracked
   `envvars.aws` / `envvars.clickhouse` holding live secrets are not imported either.)

3. **`core.py`'s advertised helpers mostly do not exist yet.** The design lists
   `log/die/need`, `dotenv()`, and `capture()` as things to move into `core.py`. None of them
   exist in `demo.py`: errors are `raise SystemExit(...)` inline, dotenv is a function-local
   `from dotenv import dotenv_values` at `demo.py:421`, and there is no `capture()` anywhere.
   These are **new code**, and T1 must write them (the EKS port needs them — they are what
   `deploy/eks/scripts/lib/common.sh` provides today).

4. **`run()` is not a chokepoint today.** Nine `subprocess` sites bypass it:
   `verify_upstream` (`:88`), `upstream_file` (`:99`), `bootstrap` (`:161`), `stage`
   (`:255` Popen, `:275` run), `host_platform` (`:292`), `image_exists` (`:306`), `build`
   (`:387`), plus `main()` passing `stdout=subprocess.PIPE` into `compose()` at `:754`.
   **Decided:** `core.py` exposes three chokepoints — `run()`, `capture()`, `popen()` — and
   `images.stage()` uses `core.popen()` so the export keeps streaming into `tarfile`. The
   constraint stays literally greppable.

5. **Monkeypatch surface is 12 attributes, not ~30:** `OVERLAY, PATCHES, RUNTIME, COMMIT,
   CONCIERGE, PROMPTS, DOCKER, TOOLS_PATCH, image_exists, ROOT, run, generate_config` — 13
   call sites in `tests/test_build.py` plus `tests/test_integration_config.py:108`. T1's
   rewire is much smaller than assumed.

6. **Stale line numbers.** The `run` stub to generalize into `fake_sh` is at
   `tests/test_build.py:277-297`, not 251-268. `EXPECTED_PATCH_TARGETS` is at
   `tests/test_frontend_overlay.py:78-89` and has **no entry for `0000-package-lint-script.patch`**
   — renaming that patch costs nothing in the registry.

7. **The cited secret-leak precedent asserts something narrower than claimed.**
   `tests/test_integration_config.py:18-27` ends in `assert "sensitive" not in json.dumps(config)`
   — the fixture's marker text was not copied verbatim. Nothing in the suite asserts an
   `${env:…}`-only reference pattern. T7's AC is reworded below so the evaluator grades a real
   property instead of hunting a precedent that does not exist.

8. **A shadowing trap in `seed_prompts()`.** `demo.py:647` does
   `from concierge.langfuse_api import PROMPT_NAME, PROMPTS, LangfuseAPI` — a function-local
   import that shadows the module-level `PROMPTS` for the whole body. The two paths coincide
   today but are different objects from different modules. T1 must **not** "tidy" this into
   `core.PROMPTS`.

9. **The `.gitignore` anchoring rationale is wrong** (the remedy still works). A `.gitignore`
   inside `deploy/eks/` anchors its `tofu/...` patterns to its own directory, not the repo root,
   so it would keep working in place. Hoisting to the root `.gitignore` with `deploy/eks/`
   prefixes is still fine — just not for the stated reason.

10. **EKS bash is 1058 lines**, not ~1365 (14 scripts + `demo.sh` + 3 `lib/` files).

Two facts that make the parallel flow safe, which the design did not record:

- `scripts/dev-setup.sh` symlinks only `.venv` and `.upstream` into a linked worktree; each
  worktree gets its **own** `.runtime/`. So `demo.py stage`/`build` in T4 cannot race a
  wave-mate's `.runtime/build`. The shared `.venv` is used read-only by `ruff`/`pytest`.
- Clean tree, no existing worktrees, `main` level with `origin/main`, `commit.gpgsign=false`.

## Plan JSON to be written

`.claude/plans/feat-merge-ch-otel-demo.json`

| Field | Value |
|---|---|
| slug | `feat-merge-ch-otel-demo` |
| kind / mode | `feature` / `dev` |
| parallelism | `auto` |
| autonomy | `managed` (the design asks for all waves to runners) |
| signing_disabled | `true` |
| verification | `scripts/dev-setup.sh && .venv/bin/ruff check concierge scripts tests launcher && .venv/bin/pytest -q && ([ ! -x scripts/check-frontend.sh ] \|\| scripts/check-frontend.sh)` |

No `CHARTER.md` exists. `_index.json` records the creation offer was already made
(`offered_at: 2026-09-11`, not declined), so per the once-only rule it is not re-offered;
`plan.charter` records the absence and nothing blocks. No open deferrals or findings carry in
from `feat-native-assistant-ui` — clean intake.

### `plan.constraints[]`

The cycle's prohibitions, graded on every task, distinct from the per-task deliverables:

1. `subprocess` is imported only in `launcher/core.py`; every other module goes through
   `core.run()` / `core.capture()` / `core.popen()`.
2. No secret value reaches argv or disk: Kubernetes Secrets are assembled in Python and piped
   to `kubectl apply -f -` on stdin; ClickHouse and Langfuse auth travel in request headers or
   function kwargs, never a command line.
3. Tofu identifiers `var.name = "otel-demo-eks"`, state bucket `otel-demo-eks-tfstate-<account>`
   and key `otel-demo-eks/terraform.tfstate` are unchanged — moving any replaces the live cluster.
4. The upstream pin stays 3.0.0 / `1755859a` everywhere; no `DEMO_REF` or `d6fd782e` survives.
5. Internal identifiers unchanged: `astronomy-concierge` image prefix, `concierge/` package,
   Compose project name, Langfuse prompt/trace names. Only the repo name, README, `pyproject`
   name/description and ECR repo names change.

### Tasks and waves

The verification command above applies to every task. Full ACs, file lists and
behavior-preservation detail live in `refactored-marinating-rain.md`; this table records the
wave assignment and the scope changes exploration forced.

| Wave | Task | Scope |
|---|---|---|
| 1 | **T1** Split `demo.py` into `launcher/` + shim | `scripts/{demo,smoke}.py`, `launcher/*.py`, `tests/{test_build,test_integration_config,compose_yaml,conftest}.py`, `pyproject.toml` |
| 2 | **T2** CLI skeleton + EKS config | `launcher/cli.py`, `launcher/eks/*.py` (signatures + `register()`, bodies `SystemExit("not implemented yet")`), `.env.example`, `tests/conftest.py` (`fake_sh`), `tests/test_cli.py`, `tests/test_eks_config.py` |
| 2 | **T3** `deploy/eks` data | `deploy/eks/**` only — `tofu/ecr.tf` (for_each 4 repos), `tofu/outputs.tf` (map + registry), `k8s/demo-values.yaml`, `README.md`; delete `scripts/`, `demo.sh`, `envvars.*`, `patches/` |
| 2 | **T4** Session replay + Compose gating | `frontend/patches/{0000,0001,0006,0009,0010}` + `README.md`, `tests/test_frontend_overlay.py`, `compose.native.yaml`, `launcher/stack.py`, `tests/test_integration_config.py`, `docker/README.md` |
| 3 | **T5** aws + k8s helpers | `launcher/eks/{aws,k8s}.py`, `tests/test_eks_{aws,k8s}.py` |
| 3 | **T6** tunnel | `launcher/eks/tunnel.py`, `tests/test_eks_tunnel.py` |
| 3 | **T7** generated Helm values | `launcher/eks/values.py`, `tests/test_eks_values.py` |
| 4 | **T8** infra (init/apply/destroy/nightly) | `launcher/eks/infra.py`, `tests/test_eks_infra.py` |
| 4 | **T9** publish | `launcher/images.py` (additive), `launcher/eks/aws.py` (additive), `tests/test_publish.py` |
| 4 | **T10** flags + scenario target | `launcher/eks/flags.py`, `launcher/stack.py`, `tests/test_{scenario,eks_flags}.py` |
| 4 | **T11** ops (verify/status) | `launcher/eks/ops.py`, `tests/test_eks_ops.py` |
| 5 | **T12** lifecycle (deploy/up/down) | `launcher/eks/lifecycle.py`, `tests/test_eks_deploy.py` |
| 5 | **T13** smoke `--target eks` | `scripts/smoke.py`, `tests/test_smoke_queries.py` |
| 5 | **T14** `eks check` | `launcher/eks/check.py`, `tests/test_eks_check.py` |
| 6 | **T15** README + docs | `README.md`, `deploy/eks/README.md`, `docker/README.md`, `.env.example` comments |

### T1's scope, restated from exploration

T1 is the pivot task and the design understated it. Beyond the mechanical split it must:

- **Write** `core.log/die/need/dotenv` (new) and `core.run/capture/popen` (only `run` exists).
- **Rewrite the 7 bypassing call sites** onto the chokepoints, with `stage()` on
  `core.popen()` so the `git archive` → `tarfile` stream is preserved, and
  `image_exists`/`stage`'s `git apply --check` keeping their returncode inspection (they
  deliberately opt out of `check=True`).
- Own the **EKS-only-key drop** in `stack.environment()` (moved here — see conflict analysis).
- Pin the **`manifest.published[svc] = {repository, tag, digest}` schema** in `images.manifest`
  (moved here — see conflict analysis).
- Keep cross-module reads as **attribute access** (`core.RUNTIME`, `images.read_manifest()`),
  never `from .core import RUNTIME`, or the 12 monkeypatch targets stop taking effect.
  Note `stack.environment()` calls three `images` functions (`read_manifest`, `image_variable`,
  `image_name`) and `images.build_tag()` calls two `upstream` ones (`upstream_file`,
  `corrected_tools`) — the import graph is `cli → stack → images → upstream → core`, acyclic.
- **Leave `seed_prompts()`'s local `PROMPTS` import alone** (correction 8).
- Keep the flat argparse parser as-is; T2 replaces it with the subparser tree.

ACs: pytest + ruff green; `demo.py config` byte-identical before and after;
`images.build_tag()` still returns `58acc12afb99` on the same tree; `grep -rn subprocess launcher`
hits only `core.py`; `ps` and `logs agent` still work.

### Conflict analysis result

Greedy-colored over the four conflict classes. The design's wave assignment survives with
three changes:

1. **Wave 2 had a real lexical conflict.** T2's AC "`stack.environment()` drops the EKS-only
   keys" requires editing `launcher/stack.py`, which T4 also owns (it adds the
   `PUBLIC_HYPERDX_ENABLED` derivation) — two wave-mates writing one file. **Fix:** the key
   drop moves into **T1**, which creates `stack.py` and can carry the key tuple from the
   design's `.env.example` EKS section. T2 keeps only the `eks/config.py` key lists and the
   `.env.example` copy; T4 keeps only the replay derivation.

2. **T7 (wave 3) reads a `manifest.published` shape that T9 (wave 4) writes.** T9 cannot move
   into wave 3 — it edits `launcher/eks/aws.py`, which is T5's. **Fix:** pin the
   `published[svc] = {repository, tag, digest}` schema as an AC on **T1**'s `images.manifest`,
   so T7 and T9 code to a written contract rather than to each other.

3. **T4's live-browser AC is not gateable by a context-free implementer** (decided: automated
   proxy + manual rrweb). T4's automated ACs become: `demo.py stage` applies all patches;
   `check-frontend.sh` passes; `build --service frontend` succeeds; `@hyperdx/browser` resolves
   in the staged tree and appears in the built bundle; **`scripts/smoke.py` passes with replay
   on** — the real guard on the design's top risk, that the HyperDX tracer swap breaks the
   `assistant.turn → fetch → proxy → agent` chain; and `SESSION_REPLAY=false` renders an empty
   `PUBLIC_HYPERDX_ENABLED`. The rrweb-records assertion moves to manual validation step 6.

Everything else is write-disjoint. Waves stay 1 / 3 / 3 / 4 / 3 / 1; wave 4 sits at the
concurrency cap of 4.

## Pre-flight (orchestrator, direct commits — not harness tasks)

All of this lands before wave 1.

1. `git subtree add --prefix=deploy/eks https://github.com/mbarretta/ch-otel-demo-eks.git main`
   (no `--squash`; merge commit with `-c commit.gpgsign=false`).
2. Housekeeping commit: **copy** (not `git mv`)
   `/Users/barretta/workspace/ch/ch-otel-demo-eks/.claude/plans/feat-otel-demo-eks.json` into
   `.claude/plans/` and `plan-edit.sh rebuild-index`;
   `git mv deploy/eks/LIVE-VALIDATION.md docs/history/eks-live-validation-2026-09-09.md`;
   `git mv NATIVE_AGENT_UI_PLAN.md docs/history/`; root `.gitignore` gains the `deploy/eks/…`
   patterns and `deploy/eks/.gitignore` is removed; `.dockerignore` gains `deploy` and
   `launcher`; `pyproject.toml` name → `ch-opentelemetry-demo`.
3. `gh repo create mbarretta/ch-opentelemetry-demo --public --description "OpenTelemetry Demo with ClickStack observability, session replay, and a Langfuse-traced AI shopping assistant"`;
   `git remote set-url origin …`; `git push -u origin main`. (Decided: switch now, as the
   design reads — the new public repo carries the full merge history including in-progress waves.)
4. Write the plan JSON; run the Codex adversarial plan review (`codex` 0.154.0 is installed and
   this plan clears the kind/size gate); then the managed-run go gate.

## Verification

Per task, the harness gate: implementer → `mab-harness:code-review` → `harness-evaluator`
against the ACs, with the verification command run inside each worktree; then the cycle-end
security audit and cross-task quality review.

End-to-end after the cycle merges — manual, needs AWS + ClickHouse Cloud + Langfuse credentials:

1. `demo.py eks init && demo.py eks apply` — plan shows only the ECR repository changes.
2. `demo.py build` on the arm64 host, `demo.py publish` — four images in ECR,
   `manifest.published` recorded, `eks status` shows all present.
3. `demo.py eks up` — collector and release healthy, ECR tags on the four custom services,
   tunnel up.
4. Browser at `http://localhost:8080`: an Assistant turn and an Add to cart;
   `demo.py eks verify` shows agent spans, assistant route spans and a `hyperdx_sessions` row;
   Langfuse shows one trace per turn; ClickStack shows the same trace id.
5. `scripts/smoke.py --target eks`; `demo.py scenario backend-failure --target eks` breaks
   "Explain this product" for the Explorascope and `shopping` restores it.
6. Laptop regression: `demo.py build && demo.py up` with `SESSION_REPLAY=auto` and ClickStack
   configured; after a browser visit confirm `.runtime/telemetry/telemetry.jsonl` carries
   `resourceLogs` with `rum.sessionId` (the AC moved off T4); `scripts/smoke.py`; Cypress
   `Assistant.cy.ts`.
7. With the user's confirmation only: archive both old GitHub repos with a pointer in their
   descriptions.
