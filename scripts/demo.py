#!/usr/bin/env python3
import argparse
import asyncio
import base64
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
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
STAGED_IMAGES = {"frontend"}
# Upstream files whose content, together with the overlay and patches, determines the tag.
FRONTEND_INPUTS = ("src/frontend/Dockerfile", "src/frontend/package-lock.json")
STAGED_DOCKERIGNORE = """
###################################
# astronomy-concierge staging: keep secrets, tooling state, and captures out of the context
.git
**/.env
.venv
.runtime
.upstream
.cache
**/*.jsonl
src/frontend/node_modules
src/frontend/.next
src/frontend/tsconfig.tsbuildinfo
###################################
"""


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def verify_upstream():
    if not UPSTREAM.exists():
        raise SystemExit(f"Missing {UPSTREAM}; run scripts/demo.py bootstrap first.")
    actual = subprocess.check_output(
        ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != COMMIT:
        raise SystemExit(
            f"Expected upstream {COMMIT}, found {actual}; restore the pinned checkout."
        )


def upstream_file(path):
    """Content of an upstream file at the pinned commit, independent of the working tree."""
    return subprocess.check_output(["git", "-C", str(UPSTREAM), "show", f"{COMMIT}:{path}"])


# The released shop tools hard-code USD and pass the cart id under the wrong query key.
# These edits let the agent forward the conversation's currency and reach the right cart.
TOOLS_PATCH = (
    (
        "async def list_products():",
        'async def list_products(currency_code: str = "USD"):',
    ),
    (
        'url = f"http://{BASE_URL}/api/products"\n    try:\n        async with httpx.AsyncClient(timeout=TIMEOUT) as client:\n            res = await client.get(url)',
        'url = f"http://{BASE_URL}/api/products"\n    try:\n        async with httpx.AsyncClient(timeout=TIMEOUT) as client:\n            res = await client.get(url, params={"currencyCode": currency_code})',
    ),
    (
        "async def get_product(product_id: str):",
        'async def get_product(product_id: str, currency_code: str = "USD"):',
    ),
    (
        'url = f"http://{BASE_URL}/api/products/{product_id}"\n    try:\n        async with httpx.AsyncClient(timeout=TIMEOUT) as client:\n            res = await client.get(url)',
        'url = f"http://{BASE_URL}/api/products/{product_id}"\n    try:\n        async with httpx.AsyncClient(timeout=TIMEOUT) as client:\n            res = await client.get(url, params={"currencyCode": currency_code})',
    ),
    (
        "async def get_cart(user_id: str):",
        'async def get_cart(user_id: str, currency_code: str = "USD"):',
    ),
    (
        'params={"user_id": user_id}',
        'params={"sessionId": user_id, "currencyCode": currency_code}',
    ),
)


def patch_tools(source):
    """Apply TOOLS_PATCH to the released src/shared/tools.py text; fail on any drift."""
    for old, new in TOOLS_PATCH:
        if source.count(old) != 1:
            raise SystemExit(
                f"Upstream tools.py changed around {old.splitlines()[0]!r}; review TOOLS_PATCH."
            )
        source = source.replace(old, new)
    return source


def corrected_tools():
    """The released src/shared/tools.py at the pinned commit with TOOLS_PATCH applied."""
    return patch_tools(upstream_file("src/shared/tools.py").decode())


def write_tools():
    """Refresh the corrected tools.py that the images copy and the host-run agent imports."""
    tools = corrected_tools()
    (RUNTIME / "tools.py").write_text(tools)
    host_copy = RUNTIME / "python/src/agents/tools.py"
    if host_copy.parent.is_dir():
        host_copy.write_text(tools)


def bootstrap():
    if not UPSTREAM.exists():
        run("git", "clone", "--depth", "1", "--branch", TAG, REPO, str(UPSTREAM))
    verify_upstream()
    changed = subprocess.check_output(
        ["git", "-C", str(UPSTREAM), "status", "--porcelain"], text=True
    )
    if changed:
        raise SystemExit("Upstream checkout has changes. Keep overlay changes in the project root.")
    RUNTIME.mkdir(exist_ok=True)
    (RUNTIME / "telemetry").mkdir(exist_ok=True)
    if not (RUNTIME / "flagd").exists():
        shutil.copytree(UPSTREAM / "src/flagd", RUNTIME / "flagd")
    for service in ["agent", "chatbot"]:
        shutil.copytree(UPSTREAM / f"src/{service}/src", RUNTIME / "python/src", dirs_exist_ok=True)
    write_tools()
    if not (ROOT / ".env").exists():
        shutil.copyfile(ROOT / ".env.example", ROOT / ".env")
        (ROOT / ".env").chmod(0o600)
    print(f"Upstream {TAG} ({COMMIT[:12]}) verified; overlay prepared.")


def overlay_files():
    """(path relative to src/frontend, source) pairs; the overlay's own README is documentation."""
    if not OVERLAY.is_dir():
        return []
    files = [path for path in OVERLAY.rglob("*") if path.is_file()]
    return sorted(
        (str(path.relative_to(OVERLAY)), path) for path in files if path != OVERLAY / "README.md"
    )


def patch_files():
    return sorted(PATCHES.glob("*.patch")) if PATCHES.is_dir() else []


def source_files(root):
    """(path relative to root, path) pairs for a directory the images copy.

    Bytecode caches and Markdown are not build inputs: .dockerignore keeps them out of the
    context, and like the overlay README they must not change the tag.
    """
    return sorted(
        (str(path.relative_to(root)), path)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".md"}
    )


def base_images():
    """service -> released image reference pinned by digest, from docker/base-images.json."""
    recorded = json.loads((DOCKER / "base-images.json").read_text())["images"]
    return {service: f"{entry['image']}@{entry['digest']}" for service, entry in recorded.items()}


def build_tag():
    """Short digest over everything that shapes the four images.

    Frontend: upstream commit, released Dockerfile and lockfile, overlay, patches. Agent, mcp,
    proxy: docker/ (Dockerfiles with their base digests, base-images.json, the Envoy template),
    concierge/, prompts/, and the corrected tools.py derived from the pin and TOOLS_PATCH.
    """
    digest = hashlib.sha256()

    def add(label, data):
        digest.update(f"{label}\0{len(data)}\0".encode())
        digest.update(data)

    add("upstream", COMMIT.encode())
    for path in FRONTEND_INPUTS:
        add(path, upstream_file(path))
    for relative, path in overlay_files():
        add(f"overlay/{relative}", path.read_bytes())
    for path in patch_files():
        add(f"patch/{path.name}", path.read_bytes())
    add("tools.py", corrected_tools().encode())
    for label, root in (("docker", DOCKER), ("concierge", CONCIERGE), ("prompts", PROMPTS)):
        for relative, path in source_files(root):
            add(f"{label}/{relative}", path.read_bytes())
    return digest.hexdigest()[:12]


def stage():
    """Export the pinned upstream commit to .runtime/build, then add the overlay and patches.

    The export is its own index-only git repository so `git apply` resolves paths against the
    staged tree (inside the project repository they would silently be skipped) and so
    `git -C .runtime/build diff -- src/frontend` shows exactly what the patches change.
    """
    verify_upstream()
    build = RUNTIME / "build"
    if build.is_symlink() or build.is_file():
        build.unlink()
    elif build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    export = ["git", "-C", str(UPSTREAM), "archive", "--format=tar", COMMIT]
    with (
        subprocess.Popen(export, stdout=subprocess.PIPE) as archive,
        tarfile.open(fileobj=archive.stdout, mode="r|") as tar,
    ):
        tar.extractall(build, filter="data")
    if archive.returncode != 0:
        raise SystemExit(f"git archive of upstream {COMMIT} failed.")
    ignore = build / ".dockerignore"
    ignore.write_text(ignore.read_text() + STAGED_DOCKERIGNORE)
    run("git", "-c", "init.defaultBranch=main", "init", "-q", str(build))
    run("git", "-C", str(build), "-c", "core.safecrlf=false", "add", "-A")
    frontend = build / "src/frontend"
    overlay = overlay_files()
    for relative, source in overlay:
        target = frontend / relative
        if target.exists():
            print(f"Overlay file {relative} replaces an upstream file; prefer a patch for edits.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    patches = patch_files()
    for patch in patches:
        check = subprocess.run(
            ["git", "-C", str(build), "apply", "--check", str(patch)],
            capture_output=True,
            text=True,
        )
        if check.returncode != 0:
            raise SystemExit(
                f"Patch {patch.name} does not apply to upstream {TAG}:\n{check.stderr.strip()}"
            )
        run("git", "-C", str(build), "apply", str(patch))
    print(
        f"Staged upstream {TAG} ({COMMIT[:12]}) with {len(overlay)} overlay files and "
        f"{len(patches)} patches in {build}."
    )


def host_platform():
    return subprocess.check_output(
        ["docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"], text=True
    ).strip()


def image_name(service, tag):
    return f"{IMAGE_PREFIX}-{service}:{tag}"


def image_variable(service):
    return f"CONCIERGE_{service.upper().replace('-', '_')}_IMAGE"


def image_exists(image):
    probe = subprocess.run(
        ["docker", "image", "inspect", image], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
    return probe.returncode == 0


def manifest_path():
    return RUNTIME / "images/manifest.json"


def read_manifest():
    path = manifest_path()
    return json.loads(path.read_text()) if path.exists() else None


def import_root():
    """Make the concierge package importable when this file runs as a script."""
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))


def contract_version():
    import_root()
    from concierge.contract import CONTRACT_VERSION

    return CONTRACT_VERSION


def write_manifest(tag, platform, image_ids):
    """Record the build; services built separately keep their entries."""
    manifest = read_manifest() or {}
    images = manifest.get("images", {})
    for service, image_id in image_ids.items():
        images[service] = {"image": image_name(service, tag), "id": image_id}
    manifest.update(
        {
            "tag": tag,
            "platform": platform,
            "upstream_commit": COMMIT,
            "contract_version": contract_version(),
            "base_images": base_images(),
            "images": images,
        }
    )
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def build_context(service):
    return RUNTIME / "build" if service in STAGED_IMAGES else ROOT


def build(services, platform=None):
    unknown = sorted(set(services) - set(IMAGES))
    if unknown:
        raise SystemExit(
            f"Unknown service(s) {', '.join(unknown)}; choose from {', '.join(IMAGES)}."
        )
    if STAGED_IMAGES & set(services):
        stage()
    write_tools()
    tag = build_tag()
    platform = platform or host_platform()
    image_ids = {}
    for service in services:
        image = image_name(service, tag)
        context = build_context(service)
        print(f"Building {image} for {platform} from {context}/{IMAGES[service]}")
        run(
            "docker",
            "build",
            "--platform",
            platform,
            "--file",
            str(context / IMAGES[service]),
            "--tag",
            image,
            str(context),
        )
        image_ids[service] = subprocess.check_output(
            ["docker", "image", "inspect", "--format", "{{.Id}}", image], text=True
        ).strip()
    manifest = write_manifest(tag, platform, image_ids)
    for service in services:
        print(f"{manifest['images'][service]['image']} {manifest['images'][service]['id']}")
    print(f"Manifest: {manifest_path()}")


def require_images():
    """The native stack runs only from images recorded by `build`."""
    manifest = read_manifest()
    hint = "run `scripts/demo.py build` first."
    if manifest is None:
        raise SystemExit(f"No build manifest at {manifest_path()}; {hint}")
    missing = []
    for service in IMAGES:
        recorded = manifest["images"].get(service)
        if recorded is None:
            missing.append(f"{service} (never built)")
        elif not image_exists(recorded["image"]):
            missing.append(recorded["image"])
    if missing:
        raise SystemExit(f"Missing local image(s): {', '.join(missing)}; {hint}")
    current = build_tag()
    if manifest["tag"] != current:
        print(
            f"Warning: build inputs changed since the last build (tag {current}, manifest "
            f"{manifest['tag']}); run `scripts/demo.py build` to refresh the images."
        )
    return manifest


def environment():
    from dotenv import dotenv_values

    values = {**dotenv_values(UPSTREAM / ".env"), **dotenv_values(ROOT / ".env"), **os.environ}
    values.update(
        {
            "CONCIERGE_ROOT": str(ROOT),
            "DEMO_VERSION": TAG,
            "IMAGE_VERSION": TAG,
            "IMAGE_NAME": "ghcr.io/open-telemetry/demo",
            "ENVOY_PORT": "8080",
            "AGENT_PORT": "8010",
            "CHATBOT_PORT": "7860",
            "MCP_PORT": "8011",
        }
    )
    built = (read_manifest() or {}).get("images", {})
    for service in IMAGES:
        values[image_variable(service)] = (
            built[service]["image"] if service in built else image_name(service, "unbuilt")
        )
    if values.get("LANGFUSE_PUBLIC_KEY") and values.get("LANGFUSE_SECRET_KEY"):
        encoded = base64.b64encode(
            f"{values['LANGFUSE_PUBLIC_KEY']}:{values['LANGFUSE_SECRET_KEY']}".encode()
        ).decode()
        values["LANGFUSE_AUTH_HEADER"] = "Basic " + encoded
    return {key: str(value) for key, value in values.items() if value is not None}


def collector_config(env):
    import yaml

    config = yaml.safe_load((UPSTREAM / "src/otel-collector/otelcol-config.yml").read_text())
    exporters = config["exporters"]
    exporters["file/capture"] = {
        "path": "/var/lib/otel/telemetry.jsonl",
        "rotation": {"max_megabytes": 20, "max_backups": 3},
    }
    config["processors"]["batch"] = {"timeout": "1s", "send_batch_size": 1024}
    pipelines = config["service"]["pipelines"]
    for signal in ("traces", "metrics", "logs"):
        pipelines[signal]["processors"].append("batch")
        pipelines[signal]["exporters"] = ["file/capture"]
    pipelines["traces"]["exporters"].append("span_metrics")
    if env.get("CLICKSTACK_OTLP_ENDPOINT"):
        exporters["otlp_http/clickstack"] = {
            "endpoint": "${env:CLICKSTACK_OTLP_ENDPOINT}",
            "headers": {"authorization": "${env:CLICKSTACK_API_KEY}"},
        }
        for signal in ("traces", "metrics", "logs"):
            pipelines[signal]["exporters"].append("otlp_http/clickstack")
    lf = [
        bool(env.get(key))
        for key in ("LANGFUSE_BASE_URL", "LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
    ]
    if any(lf) and not all(lf):
        raise SystemExit(
            "Set all three LANGFUSE_BASE_URL, LANGFUSE_PUBLIC_KEY, and LANGFUSE_SECRET_KEY."
        )
    if all(lf):
        exporters["otlp_http/langfuse"] = {
            "endpoint": "${env:LANGFUSE_BASE_URL}/api/public/otel",
            "headers": {
                "Authorization": "${env:LANGFUSE_AUTH_HEADER}",
                "x-langfuse-ingestion-version": "4",
            },
        }
        config["processors"]["filter/agent_services"] = {
            "error_mode": "propagate",
            "traces": {
                "span": [
                    'resource.attributes["service.name"] != "agent" and resource.attributes["service.name"] != "chatbot" and resource.attributes["service.name"] != "mcp"',
                    'attributes["http.route"] == "/healthz" or attributes["http.route"] == "/feedback"',
                ]
            },
        }
        pipelines["traces/langfuse"] = {
            "receivers": ["otlp"],
            "processors": ["memory_limiter", "filter/agent_services", "gen_ai_normalizer", "batch"],
            "exporters": ["otlp_http/langfuse"],
        }
    return config


def generate_config(env):
    import yaml

    (RUNTIME / "collector.yaml").write_text(yaml.safe_dump(collector_config(env), sort_keys=False))
    names = yaml.safe_load((UPSTREAM / "compose.yaml").read_text())["services"].keys()
    names = [*names, "agent", "chatbot", "mcp"]
    isolation = "networks:\n  default:\n    name: astronomy-concierge\nservices:\n"
    for name in names:
        isolation += f"  {name}:\n    container_name: !reset null\n"
    (RUNTIME / "isolation.yaml").write_text(isolation)


def compose(env, args, dev=False, debug_chatbot=False, **kwargs):
    """Run a compose command over the pinned stack.

    dev adds compose.dev.yaml (source bind mounts over the built images); debug_chatbot enables
    the `debug` profile that holds the Gradio client, which `up` leaves out by default.
    """
    generate_config(env)
    files = [
        UPSTREAM / "compose.yaml",
        UPSTREAM / "compose.agent.yaml",
        ROOT / "compose.concierge.yaml",
        ROOT / "compose.native.yaml",
        *([ROOT / "compose.dev.yaml"] if dev else []),
        RUNTIME / "isolation.yaml",
    ]
    return run(
        "docker",
        "compose",
        "--project-name",
        "astronomy-concierge",
        "--project-directory",
        str(UPSTREAM),
        "--env-file",
        str(UPSTREAM / ".env"),
        "--env-file",
        str(ROOT / ".env"),
        *(argument for path in files for argument in ("-f", str(path))),
        *(["--profile", "debug"] if debug_chatbot else []),
        *args,
        env=env,
        **kwargs,
    )


def scenario(name):
    path = RUNTIME / "flagd/demo.flagd.json"
    config = json.loads(path.read_text())
    flag = config["flags"]["productCatalogFailure"]
    flag["state"] = "ENABLED"
    flag["defaultVariant"] = "off"
    flag["targeting"]["if"][1] = "on" if name == "backend-failure" else "off"
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(config, indent=2) + "\n")
    temp.replace(path)
    print(
        f"Catalog fault {'enabled' if name == 'backend-failure' else 'disabled'}. Choose '{name}' in the chat UI."
    )


async def seed_prompts():
    from concierge.langfuse_api import PROMPT_NAME, PROMPTS, LangfuseAPI

    api = LangfuseAPI()
    if not api.enabled:
        raise SystemExit("Configure Langfuse in .env before seeding prompts.")
    for version in (1, 2):
        text = (PROMPTS / f"concierge-v{version}.txt").read_text()
        label = "production" if version == 1 else "budget-check"
        try:
            existing = await api.request(
                "GET", f"/api/public/v2/prompts/{PROMPT_NAME}", params={"label": label}
            )
        except Exception as exc:
            import httpx

            if not isinstance(exc, httpx.HTTPStatusError) or exc.response.status_code != 404:
                raise
        else:
            if existing.get("prompt") == text:
                print(f"Prompt label '{label}' already matches; skipped.")
                continue
            raise SystemExit(
                f"Prompt label '{label}' exists with different content; review it in Langfuse."
            )
        result = await api.request(
            "POST",
            "/api/public/v2/prompts",
            json={
                "name": PROMPT_NAME,
                "type": "text",
                "prompt": text,
                "labels": [label],
                "tags": ["astronomy-shop", "demo"],
            },
        )
        print(f"Created prompt version {result['version']} with label '{label}'.")


def main():
    parser = argparse.ArgumentParser(description="Pinned Astronomy Shop + Langfuse overlay")
    parser.add_argument(
        "command",
        choices=[
            "bootstrap",
            "stage",
            "build",
            "config",
            "up",
            "down",
            "ps",
            "logs",
            "restart",
            "scenario",
            "seed-prompts",
        ],
    )
    parser.add_argument("arguments", nargs="*")
    parser.add_argument(
        "--service",
        action="append",
        dest="services",
        metavar="NAME",
        help=f"build: image to build (repeatable); default all of {', '.join(IMAGES)}",
    )
    parser.add_argument(
        "--platform",
        metavar="OS/ARCH",
        help="build: target platform, for example linux/arm64; default is the Docker host's",
    )
    parser.add_argument(
        "--dev",
        action="store_true",
        help="up/config: bind-mount concierge/, prompts/, and tools.py over the agent and mcp images",
    )
    parser.add_argument(
        "--debug-chatbot",
        action="store_true",
        help="up: also start the Gradio chat client on CHAT_PORT",
    )
    args = parser.parse_args()
    if args.command == "bootstrap":
        bootstrap()
        return
    if not (RUNTIME / "tools.py").exists():
        raise SystemExit("Run python3 scripts/demo.py bootstrap first.")
    if args.command == "stage":
        stage()
        return
    if args.command == "build":
        build(args.services or list(IMAGES), args.platform)
        return
    env = environment()
    if args.command == "scenario":
        name = args.arguments[0] if args.arguments else "shopping"
        if name not in {"shopping", "backend-failure", "budget-violation"}:
            raise SystemExit("Unknown scenario")
        scenario(name)
    elif args.command == "seed-prompts":
        os.environ.update(env)
        import_root()
        asyncio.run(seed_prompts())
    elif args.command == "config":
        rendered = compose(
            env,
            ["config", "--format", "json"],
            dev=args.dev,
            debug_chatbot=args.debug_chatbot,
            stdout=subprocess.PIPE,
            text=True,
        )
        services = json.loads(rendered.stdout)["services"]
        for name, service in sorted(services.items()):
            print(f"{name}: {service.get('image', '(no image)')}")
        print("Compose configuration validated; collector config written without credentials.")
    elif args.command == "up":
        require_images()
        compose(
            env,
            ["up", "-d", "--no-build", *args.arguments],
            dev=args.dev,
            debug_chatbot=args.debug_chatbot,
        )
        print(f"Shop: http://localhost:{env.get('SHOP_PORT', '8080')}")
        if args.debug_chatbot:
            print(f"Chat (debug): http://localhost:{env.get('CHAT_PORT', '7860')}")
    # Lifecycle commands cover the debug profile too, so a chatbot started with --debug-chatbot
    # is listed, followed, restarted, and removed like the rest of the stack.
    elif args.command == "logs":
        compose(env, ["logs", "--tail", "80", *args.arguments], debug_chatbot=True)
    else:
        compose(env, [args.command, *args.arguments], debug_chatbot=True)


if __name__ == "__main__":
    main()
