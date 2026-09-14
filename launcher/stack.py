"""The Compose stack: its environment, the generated configuration, and the demo scenarios."""

import base64
import json
import os

from . import collector, core, images

# Keys only the EKS target consumes. They are dropped before the environment reaches
# `docker compose`, which has no reader for them: CLICKHOUSE_PASSWORD and OTLP_AUTH_TOKEN
# would otherwise sit in the environment of every container in the laptop stack.
EKS_ONLY_PREFIXES = ("CLICKHOUSE_", "AWS_")
EKS_ONLY_KEYS = (
    "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE",
    "OTLP_AUTH_TOKEN",
    "EKS_TUNNEL_PORT",
)


def eks_only(key):
    """Whether a configuration key belongs to the EKS target alone.

    CLICKSTACK_* is deliberately not covered: on the laptop the collector exports straight to
    ClickStack with those keys.
    """
    return key in EKS_ONLY_KEYS or key.startswith(EKS_ONLY_PREFIXES)


def environment():
    """The environment `docker compose` runs under: env files, then the process environment.

    EKS-only keys are dropped on the way out (see eks_only); every remaining value is a
    string, so it can be handed to Compose as-is.
    """
    values = {
        **core.dotenv(core.UPSTREAM / ".env"),
        **core.dotenv(core.ROOT / ".env"),
        **os.environ,
    }
    values.update(
        {
            "CONCIERGE_ROOT": str(core.ROOT),
            "DEMO_VERSION": core.TAG,
            "IMAGE_VERSION": core.TAG,
            "IMAGE_NAME": "ghcr.io/open-telemetry/demo",
            "ENVOY_PORT": "8080",
            "AGENT_PORT": "8010",
            "CHATBOT_PORT": "7860",
            "MCP_PORT": "8011",
        }
    )
    built = (images.read_manifest() or {}).get("images", {})
    for service in core.IMAGES:
        values[images.image_variable(service)] = (
            built[service]["image"] if service in built else images.image_name(service, "unbuilt")
        )
    if values.get("LANGFUSE_PUBLIC_KEY") and values.get("LANGFUSE_SECRET_KEY"):
        encoded = base64.b64encode(
            f"{values['LANGFUSE_PUBLIC_KEY']}:{values['LANGFUSE_SECRET_KEY']}".encode()
        ).decode()
        values["LANGFUSE_AUTH_HEADER"] = "Basic " + encoded
    return {
        key: str(value)
        for key, value in values.items()
        if value is not None and not eks_only(key)
    }


def generate_config(env):
    import yaml

    (core.RUNTIME / "collector.yaml").write_text(
        yaml.safe_dump(collector.collector_config(env), sort_keys=False)
    )
    names = yaml.safe_load((core.UPSTREAM / "compose.yaml").read_text())["services"].keys()
    names = [*names, "agent", "chatbot", "mcp"]
    isolation = "networks:\n  default:\n    name: astronomy-concierge\nservices:\n"
    for name in names:
        isolation += f"  {name}:\n    container_name: !reset null\n"
    (core.RUNTIME / "isolation.yaml").write_text(isolation)


def compose(env, args, dev=False, debug_chatbot=False, **kwargs):
    """Run a compose command over the pinned stack.

    dev adds compose.dev.yaml (source bind mounts over the built images); debug_chatbot enables
    the `debug` profile that holds the Gradio client, which `up` leaves out by default.
    """
    generate_config(env)
    files = [
        core.UPSTREAM / "compose.yaml",
        core.UPSTREAM / "compose.agent.yaml",
        core.ROOT / "compose.concierge.yaml",
        core.ROOT / "compose.native.yaml",
        *([core.ROOT / "compose.dev.yaml"] if dev else []),
        core.RUNTIME / "isolation.yaml",
    ]
    return core.run(
        "docker",
        "compose",
        "--project-name",
        "astronomy-concierge",
        "--project-directory",
        str(core.UPSTREAM),
        "--env-file",
        str(core.UPSTREAM / ".env"),
        "--env-file",
        str(core.ROOT / ".env"),
        *(argument for path in files for argument in ("-f", str(path))),
        *(["--profile", "debug"] if debug_chatbot else []),
        *args,
        env=env,
        **kwargs,
    )


def scenario(name):
    path = core.RUNTIME / "flagd/demo.flagd.json"
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
