"""The command-line interface: the whole command surface, declared in one place.

The laptop commands are declared here; each `demo.py eks ...` subcommand is declared by the
module that implements it (`launcher.eks.register`), so filling in an EKS module never means
editing this file.

Every command runs through `bootstrap` first, except the ones that need neither the upstream
checkout nor the staged tree: `bootstrap` itself, `publish` (which needs the build manifest)
and everything under `eks` (which needs AWS and a cluster). Those set `needs_bootstrap=False`.
"""

import argparse
import asyncio
import json
import os

from . import core, eks, images, stack, upstream
from .eks import flags as eks_flags

SCENARIOS = ("shopping", "backend-failure", "budget-violation")
TARGETS = ("local", "eks")
# Compose subcommands passed straight through, with whatever arguments follow them.
PASSTHROUGH = {
    "down": "stop and remove the stack",
    "ps": "list the stack's containers",
    "restart": "restart services, for example `restart agent`",
}


def build_parser():
    """The full parser. Separate from main() so a test can parse without dispatching."""
    parser = argparse.ArgumentParser(
        prog="demo.py",
        description="Pinned Astronomy Shop with the Langfuse-traced assistant, on Compose or EKS",
    )
    parser.set_defaults(needs_bootstrap=True)
    commands = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    bootstrap = commands.add_parser(
        "bootstrap", help="clone or update the pinned upstream checkout and correct its tools"
    )
    bootstrap.set_defaults(handler=lambda args: upstream.bootstrap(), needs_bootstrap=False)

    stage = commands.add_parser(
        "stage", help="export the pinned frontend tree and apply the overlay and patches"
    )
    stage.set_defaults(handler=lambda args: images.stage())

    build = commands.add_parser("build", help="build the four images at the content tag")
    add_service_argument(build, "build")
    build.add_argument(
        "--platform",
        metavar="OS/ARCH",
        help="target platform, for example linux/arm64; default is the Docker host's",
    )
    build.set_defaults(handler=run_build)

    publish = commands.add_parser("publish", help="push the built images to ECR for the cluster")
    add_service_argument(publish, "publish")
    publish.add_argument(
        "--force",
        action="store_true",
        help="push again even when the registry already holds the tag",
    )
    publish.set_defaults(handler=run_publish, needs_bootstrap=False)

    config = commands.add_parser(
        "config", help="render and validate the Compose configuration without starting anything"
    )
    add_stack_arguments(config)
    config.set_defaults(handler=run_config)

    up = commands.add_parser("up", help="start the stack from the built images")
    add_stack_arguments(up)
    add_arguments_argument(up)
    up.set_defaults(handler=run_up)

    logs = commands.add_parser("logs", help="tail service logs, for example `logs agent frontend`")
    add_arguments_argument(logs)
    logs.set_defaults(handler=run_logs)

    for name, help_text in PASSTHROUGH.items():
        passthrough = commands.add_parser(name, help=help_text)
        add_arguments_argument(passthrough)
        passthrough.set_defaults(handler=run_compose)

    scenario = commands.add_parser("scenario", help="set the demo's fault-injection scenario")
    scenario.add_argument(
        "name", nargs="?", default="shopping", choices=SCENARIOS, help="default: shopping"
    )
    scenario.add_argument(
        "--target",
        choices=TARGETS,
        default="local",
        help="where to apply it: the laptop's flagd file, or the cluster's (default: local)",
    )
    scenario.set_defaults(handler=run_scenario)

    seed = commands.add_parser("seed-prompts", help="create the agent's prompts in Langfuse")
    seed.set_defaults(handler=run_seed_prompts)

    eks_parser = commands.add_parser("eks", help="deploy and run the demo on an EKS cluster")
    eks_parser.set_defaults(needs_bootstrap=False)
    eks.register(eks_parser.add_subparsers(dest="eks_command", required=True, metavar="COMMAND"))

    return parser


def add_service_argument(parser, verb):
    parser.add_argument(
        "--service",
        action="append",
        dest="services",
        metavar="NAME",
        help=f"image to {verb} (repeatable); default all of {', '.join(core.IMAGES)}",
    )


def add_stack_arguments(parser):
    parser.add_argument(
        "--dev",
        action="store_true",
        help="bind-mount concierge/, prompts/, and tools.py over the agent and mcp images",
    )
    parser.add_argument(
        "--debug-chatbot",
        action="store_true",
        help="also start the Gradio chat client on CHAT_PORT",
    )


def add_arguments_argument(parser):
    parser.add_argument(
        "arguments", nargs="*", metavar="ARG", help="passed through to `docker compose`"
    )


def run_build(args):
    images.build(args.services or list(core.IMAGES), args.platform)


def run_publish(args):
    images.publish(args.services or list(core.IMAGES), force=args.force)


def run_config(args):
    env = stack.environment()
    stack.require_observability(env)
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
    print("Compose configuration validated.")


def run_up(args):
    env = stack.environment()
    stack.require_observability(env)
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


# The lifecycle commands cover the debug profile too, so a chatbot started with --debug-chatbot
# is listed, followed, restarted, and removed along with the rest of the stack.
def run_logs(args):
    stack.compose(
        stack.environment(), ["logs", "--tail", "80", *args.arguments], debug_chatbot=True
    )


def run_compose(args):
    stack.compose(stack.environment(), [args.command, *args.arguments], debug_chatbot=True)


def run_scenario(args):
    if args.target == "eks":
        return eks_flags.scenario_write_eks(args.name)
    return stack.scenario(args.name)


def run_seed_prompts(args):
    os.environ.update(stack.environment())
    images.import_root()
    asyncio.run(stack.seed_prompts())


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.needs_bootstrap and not (core.RUNTIME / "tools.py").exists():
        core.die("run demo.py bootstrap first")
    return args.handler(args)
