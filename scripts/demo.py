#!/usr/bin/env python3
import argparse
import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
UPSTREAM = ROOT / ".upstream/opentelemetry-demo"
RUNTIME = ROOT / ".runtime"
TAG = "3.0.0"
COMMIT = "1755859a9de82c2e5e225be68abc401a5ebf2b4f"
REPO = "https://github.com/open-telemetry/opentelemetry-demo.git"


def run(*args, **kwargs):
    return subprocess.run(args, check=True, **kwargs)


def bootstrap():
    if not UPSTREAM.exists():
        run("git", "clone", "--depth", "1", "--branch", TAG, REPO, str(UPSTREAM))
    actual = subprocess.check_output(
        ["git", "-C", str(UPSTREAM), "rev-parse", "HEAD"], text=True
    ).strip()
    if actual != COMMIT:
        raise SystemExit(
            f"Expected upstream {COMMIT}, found {actual}; restore the pinned checkout."
        )
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
    tools = (UPSTREAM / "src/shared/tools.py").read_text()
    old = 'params={"user_id": user_id}'
    if tools.count(old) != 1:
        raise SystemExit("Upstream cart wrapper changed; review the overlay patch.")
    tools = tools.replace(old, 'params={"sessionId": user_id, "currencyCode": "USD"}')
    (RUNTIME / "tools.py").write_text(tools)
    (RUNTIME / "python/src/agents/tools.py").write_text(tools)
    if not (ROOT / ".env").exists():
        shutil.copyfile(ROOT / ".env.example", ROOT / ".env")
        (ROOT / ".env").chmod(0o600)
    print(f"Upstream {TAG} ({COMMIT[:12]}) verified; overlay prepared.")


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


def compose(env, args):
    generate_config(env)
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
        "-f",
        str(UPSTREAM / "compose.yaml"),
        "-f",
        str(UPSTREAM / "compose.agent.yaml"),
        "-f",
        str(ROOT / "compose.concierge.yaml"),
        "-f",
        str(RUNTIME / "isolation.yaml"),
        *args,
        env=env,
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
    args = parser.parse_args()
    if args.command == "bootstrap":
        bootstrap()
        return
    if not (RUNTIME / "tools.py").exists():
        raise SystemExit("Run python3 scripts/demo.py bootstrap first.")
    env = environment()
    if args.command == "scenario":
        name = args.arguments[0] if args.arguments else "shopping"
        if name not in {"shopping", "backend-failure", "budget-violation"}:
            raise SystemExit("Unknown scenario")
        scenario(name)
    elif args.command == "seed-prompts":
        os.environ.update(env)
        sys.path.insert(0, str(ROOT))
        asyncio.run(seed_prompts())
    elif args.command == "config":
        compose(env, ["config", "--quiet"])
        print("Compose configuration validated; collector config written without credentials.")
    elif args.command == "up":
        compose(env, ["up", "-d", "--no-build", *args.arguments])
        print(
            f"Shop: http://localhost:{env.get('SHOP_PORT', '8080')} · Chat: http://localhost:{env.get('CHAT_PORT', '7860')}"
        )
    elif args.command == "logs":
        compose(env, ["logs", "--tail", "80", *args.arguments])
    else:
        compose(env, [args.command, *args.arguments])


if __name__ == "__main__":
    main()
