# Frontend overlay

New storefront files live here, at the path they have under the upstream `src/frontend/`
directory. `scripts/demo.py stage` copies every file in this tree onto
`.runtime/build/src/frontend/` after exporting the pinned OpenTelemetry Demo 3.0.0 commit.
The upstream checkout in `.upstream/` is never modified.

| Overlay path | Staged path |
| --- | --- |
| `frontend/overlay/components/Assistant/AssistantPanel.tsx` | `.runtime/build/src/frontend/components/Assistant/AssistantPanel.tsx` |
| `frontend/overlay/pages/api/assistant/message.ts` | `.runtime/build/src/frontend/pages/api/assistant/message.ts` |

This `README.md` is documentation. It is the only file in the tree that `stage` skips, and it
does not take part in the image tag.

## Rules

- Add files here. Change existing upstream files with a patch in `frontend/patches/`; an
  overlay file that shadows an upstream path is reported by `stage` and should become a patch.
- The released `src/frontend/Dockerfile` copies fixed directories (`components/`, `gateways/`,
  `pages/`, `providers/`, `styles/`, `types/`, `utils/telemetry/`, ...). Put new code inside one
  of them, or the image build cannot see it.
- Every overlay file is a build input: its path and content are part of the image tag that
  `scripts/demo.py build` computes, so editing a file here produces a new
  `astronomy-concierge-frontend:<tag>`.

## Check

```sh
scripts/check-frontend.sh
```

This stages the tree, runs `npm ci` once per `package-lock.json`, then `npx tsc --noEmit` and
`npm run lint` in `.runtime/build/src/frontend`. A type error in an overlay file fails the check.
