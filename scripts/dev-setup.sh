#!/usr/bin/env bash
# Make this checkout ready for scripts/demo.py and the test suite. Safe to run repeatedly.
#
# In a linked git worktree the upstream clone and the virtualenv are shared with the main
# checkout through symlinks and .env is copied from it, so every worktree can run the same
# verification command without re-cloning or re-installing anything.
set -euo pipefail

root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root"

git_dir="$(cd "$(git rev-parse --git-dir)" && pwd)"
common_dir="$(cd "$(git rev-parse --git-common-dir)" && pwd)"
main="$(cd "$common_dir/.." && pwd)"

log() { echo "dev-setup: $*"; }

# venv_ok DIR: the interpreter and the entry points the verification command uses all run.
# A venv created at another path (the repository was moved) has dead shebangs and fails this.
venv_ok() {
    [ -x "$1/bin/python" ] &&
        "$1/bin/python" -c 'import sys' >/dev/null 2>&1 &&
        "$1/bin/pytest" --version >/dev/null 2>&1 &&
        "$1/bin/ruff" --version >/dev/null 2>&1
}

create_venv() {
    local target=$1
    command -v uv >/dev/null 2>&1 || {
        echo "dev-setup: uv is required to create $target (https://docs.astral.sh/uv/)." >&2
        exit 1
    }
    if [ -e "$target" ]; then
        log "recreating $target: its interpreter or entry points no longer run"
    else
        log "creating $target with Python 3.14"
    fi
    uv venv --clear --python 3.14 "$target"
    uv pip install --python "$target/bin/python" -r "$root/requirements-dev.lock"
}

# link NAME: expose the main checkout's NAME here; an existing entry of any kind is kept.
link() {
    local name=$1
    local target="$main/$name"
    if [ -e "$root/$name" ] || [ -L "$root/$name" ]; then
        return
    fi
    ln -s "$target" "$root/$name"
    log "linked $name -> $target"
}

if [ "$git_dir" != "$common_dir" ]; then
    log "linked worktree of $main"
    venv_ok "$main/.venv" || create_venv "$main/.venv"
    mkdir -p "$main/.upstream"
    link .venv
    link .upstream
else
    venv_ok "$root/.venv" || create_venv "$root/.venv"
fi

if [ ! -e "$root/.env" ]; then
    if [ "$git_dir" != "$common_dir" ] && [ -f "$main/.env" ]; then
        cp "$main/.env" "$root/.env"
        log "copied .env from $main"
    else
        cp "$root/.env.example" "$root/.env"
        log "created .env from .env.example"
    fi
    chmod 600 "$root/.env"
fi

"$root/.venv/bin/python" "$root/scripts/demo.py" bootstrap
