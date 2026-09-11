#!/usr/bin/env bash
# Stage the frontend build tree, install its locked dependencies once, then type-check and lint.
#
# node_modules lives in .runtime/frontend-deps, keyed by package-lock.json, so restaging from
# scratch does not cost an `npm ci`; the staged tree gets a symlink that .dockerignore excludes.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
frontend="$root/.runtime/build/src/frontend"
deps="$root/.runtime/frontend-deps"

"$root/.venv/bin/python" "$root/scripts/demo.py" stage

# installed.lock is written only after a successful npm ci, so an interrupted install reinstalls.
mkdir -p "$deps"
if [ -d "$deps/node_modules" ] && cmp -s "$frontend/package-lock.json" "$deps/installed.lock"; then
    echo "check-frontend: node_modules matches package-lock.json; skipping npm ci"
else
    rm -f "$deps/installed.lock"
    cp "$frontend/package.json" "$frontend/package-lock.json" "$deps/"
    (cd "$deps" && npm ci --no-audit --no-fund)
    cp "$frontend/package-lock.json" "$deps/installed.lock"
fi
ln -sfn "$deps/node_modules" "$frontend/node_modules"

cd "$frontend"
echo "check-frontend: tsc --noEmit"
npx tsc --noEmit
echo "check-frontend: npm run lint"
npm run lint
echo "check-frontend: ok"
