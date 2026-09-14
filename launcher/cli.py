"""The command-line interface: one argparse parser over the laptop Compose stack."""

import argparse
import asyncio
import json
import os

from . import core, images, stack, upstream


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
        help=f"build: image to build (repeatable); default all of {', '.join(core.IMAGES)}",
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
        upstream.bootstrap()
        return
    if not (core.RUNTIME / "tools.py").exists():
        raise SystemExit("Run python3 scripts/demo.py bootstrap first.")
    if args.command == "stage":
        images.stage()
        return
    if args.command == "build":
        images.build(args.services or list(core.IMAGES), args.platform)
        return
    env = stack.environment()
    if args.command == "scenario":
        name = args.arguments[0] if args.arguments else "shopping"
        if name not in {"shopping", "backend-failure", "budget-violation"}:
            raise SystemExit("Unknown scenario")
        stack.scenario(name)
    elif args.command == "seed-prompts":
        os.environ.update(env)
        images.import_root()
        asyncio.run(stack.seed_prompts())
    elif args.command == "config":
        rendered = stack.compose(
            env,
            ["config", "--format", "json"],
            dev=args.dev,
            debug_chatbot=args.debug_chatbot,
            stdout=core.PIPE,
            text=True,
        )
        services = json.loads(rendered.stdout)["services"]
        for name, service in sorted(services.items()):
            print(f"{name}: {service.get('image', '(no image)')}")
        print("Compose configuration validated; collector config written without credentials.")
    elif args.command == "up":
        images.require_images()
        stack.compose(
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
        stack.compose(env, ["logs", "--tail", "80", *args.arguments], debug_chatbot=True)
    else:
        stack.compose(env, [args.command, *args.arguments], debug_chatbot=True)
