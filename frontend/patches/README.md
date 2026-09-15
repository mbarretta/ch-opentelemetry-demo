# Frontend patches

Edits to files that already exist upstream are unified diffs applied by `scripts/demo.py
stage` after the overlay is copied. Patches apply in file-name order, one patch per upstream
file, named `NNNN-<topic>.patch` (for example `0001-app-assistant-provider.patch`).

`stage` runs `git apply --check` for every patch and stops with a message naming the patch
when one does not apply; nothing is staged in that case. It also refuses to run when
`.upstream/opentelemetry-demo` is not at the pinned 3.0.0 commit.

## Author or update a patch

`.runtime/build` is an index-only git repository whose index holds the pristine upstream
export, so `git diff` there shows exactly what differs from 3.0.0.

```sh
.venv/bin/python scripts/demo.py stage
# edit .runtime/build/src/frontend/pages/_app.tsx
git -C .runtime/build diff -- src/frontend/pages/_app.tsx > frontend/patches/0001-app-assistant-provider.patch
.venv/bin/python scripts/demo.py stage   # applies the patch from scratch
```

Because the diff is taken against the pristine export, regenerating a file's patch after
further edits produces the complete patch for that file; do not hand-merge hunks.

Patches are build inputs: their names and content are part of the image tag computed by
`scripts/demo.py build`.

## Session replay

Five patches carry the ClickStack browser SDK, because session replay ships in the one frontend
image and is switched on at container start rather than built in:

| Patch | Upstream file | Change |
| --- | --- | --- |
| `0000-package-lint-and-hyperdx.patch` | `package.json` | `"lint": "eslint ."` (upstream's `next lint` is gone in Next 16) and the `@hyperdx/browser` dependency |
| `0001-app-assistant-provider.patch` | `pages/_app.tsx` | `NEXT_PUBLIC_HYPERDX_ENABLED` and `NEXT_PUBLIC_HYPERDX_URL` in the `window.ENV` typing |
| `0006-document-assistant-env.patch` | `pages/_document.tsx` | reads `PUBLIC_HYPERDX_*` from the server environment and hands the browser `NEXT_PUBLIC_HYPERDX_*` |
| `0009-frontend-tracer-session-replay.patch` | `utils/telemetry/FrontendTracer.ts` | starts the SDK when `NEXT_PUBLIC_HYPERDX_ENABLED` is `true`, instead of the released tracer |
| `0010-package-lock-hyperdx.patch` | `package-lock.json` | the resolved lockfile entries for the SDK |

`0009` states `maskAllInputs: true` rather than inheriting it. `@hyperdx/browser` 0.25.1's
`init()` defaults it to `true`, while the package's own README documents `false` -- so the input
masking the storefront relies on rests on an undocumented default that an SDK bump could flip
silently. Keep the line through any regeneration. `maskAllText` genuinely defaults to `false`, so
rendered page text is recorded either way; that is part of the capture posture, not of this line.

The dependency pair is npm's output, not a hand-written diff: the released `Dockerfile` runs
`npm ci`, which fails on a lockfile that does not resolve. Regenerate both together.

```sh
.venv/bin/python scripts/demo.py stage
(cd .runtime/build/src/frontend && npm install @hyperdx/browser@0.25.1 --package-lock-only)
git -C .runtime/build diff -- src/frontend/package.json \
    > frontend/patches/0000-package-lint-and-hyperdx.patch
git -C .runtime/build diff -- src/frontend/package-lock.json \
    > frontend/patches/0010-package-lock-hyperdx.patch
```

`stage` has already applied `0000`, so the staged `package.json` still carries the lint script
and the regenerated patch holds both changes.
