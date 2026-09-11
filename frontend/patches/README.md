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
