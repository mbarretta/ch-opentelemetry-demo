"""Paths, pins, and the process boundary the rest of the launcher goes through.

This is the only module that imports `subprocess`. Every command the CLI shells out to goes
through `run()`, `capture()` or `popen()`, so the whole process surface can be found in one
place -- and faked in one place by the tests.
"""

import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / ".upstream/opentelemetry-demo"
RUNTIME = ROOT / ".runtime"
TAG = "3.0.0"
COMMIT = "1755859a9de82c2e5e225be68abc401a5ebf2b4f"
REPO = "https://github.com/open-telemetry/opentelemetry-demo.git"
# New frontend files, relative to src/frontend/. Edits to upstream files are patches.
OVERLAY = ROOT / "frontend/overlay"
PATCHES = ROOT / "frontend/patches"
CONCIERGE = ROOT / "concierge"
PROMPTS = ROOT / "prompts"
DOCKER = ROOT / "docker"
IMAGE_PREFIX = "astronomy-concierge"
# Images built by `build`, service -> Dockerfile relative to its context. The frontend builds
# from the staged tree (.runtime/build) with the released Dockerfile; the others build from this
# repository with docker/*.Dockerfile on top of the released 3.0.0 images pinned by digest in
# docker/base-images.json. All four share one content-derived tag.
IMAGES = {
    "frontend": "src/frontend/Dockerfile",
    "agent": "docker/agent.Dockerfile",
    "mcp": "docker/mcp.Dockerfile",
    "frontend-proxy": "docker/frontend-proxy.Dockerfile",
}

PIPE = subprocess.PIPE
DEVNULL = subprocess.DEVNULL


def log(message):
    """Progress on stdout, in the `==> ...` shape the EKS scripts established."""
    print(f"==> {message}")


def die(message):
    """Fail with a message on stderr and a non-zero exit status.

    SystemExit carries the text, so callers and tests read it off the exception the way the
    inline `raise SystemExit(...)` sites in this package do; the interpreter prints it to
    stderr and exits 1.
    """
    raise SystemExit(f"error: {message}")


def need(*commands):
    """Require external commands, naming every missing one rather than the first."""
    missing = [command for command in commands if shutil.which(command) is None]
    if missing:
        die(f"missing required command(s): {', '.join(missing)}")


def dotenv(path):
    """The KEY=VALUE pairs of an env file as a mapping; a missing file reads as empty."""
    from dotenv import dotenv_values

    return dotenv_values(path)


def run(*args, check=True, **kwargs):
    """Run a command to completion, raising on failure unless `check=False`.

    Arguments are stringified, so callers can pass Paths. `check=False` is for the probes that
    read a return code instead of failing (see images.image_exists).
    """
    return subprocess.run([str(argument) for argument in args], check=check, **kwargs)


def capture(*args, check=True, text=True, **kwargs):
    """Run a command and return its CompletedProcess with stdout captured.

    stdout only, like the `check_output` calls this replaces: stderr still reaches the
    terminal so a failure stays diagnosable. Pass `text=False` for bytes, `stderr=PIPE` to
    read the error stream, and `check=False` to inspect the return code instead of raising.
    """
    kwargs.setdefault("stdout", PIPE)
    return subprocess.run([str(argument) for argument in args], check=check, text=text, **kwargs)


def popen(*args, **kwargs):
    """Start a command and return the Popen handle, for streaming its stdout.

    stdout is a pipe by default, which is the only reason to reach for this over `capture()`:
    images.stage() feeds `git archive` straight into tarfile instead of buffering the export.
    """
    kwargs.setdefault("stdout", PIPE)
    return subprocess.Popen([str(argument) for argument in args], **kwargs)
