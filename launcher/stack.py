"""The Compose stack: its environment, the generated configuration, and the demo scenarios."""

import base64
import copy
import json
import os
import re

from . import collector, core, images

# SESSION_REPLAY gates the ClickStack browser SDK that is compiled into the frontend image:
# `true` forces it on, `false` off, and `auto` (the default) turns it on whenever ClickStack is
# configured, since the replay events leave through the collector's ClickStack exporter. The
# accepted spellings are the ones the storefront's isDemoDetailsEnabled already takes.
SESSION_REPLAY_DEFAULT = "auto"
SESSION_REPLAY_ON = re.compile(r"^(1|true|on|yes)$", re.IGNORECASE)
SESSION_REPLAY_OFF = re.compile(r"^(0|false|off|no)$", re.IGNORECASE)


def session_replay(values):
    """Whether the frontend should start the replay SDK, from SESSION_REPLAY in `values`."""
    mode = (values.get("SESSION_REPLAY") or SESSION_REPLAY_DEFAULT).strip()
    if SESSION_REPLAY_ON.match(mode):
        return True
    if SESSION_REPLAY_OFF.match(mode):
        return False
    if mode.lower() != SESSION_REPLAY_DEFAULT:
        core.die(f"SESSION_REPLAY must be auto, true, or false; got {mode!r}.")
    return bool(values.get("CLICKSTACK_OTLP_ENDPOINT"))


def replay_flag(values):
    """PUBLIC_HYPERDX_ENABLED as the frontend server reads it, for either target.

    Both targets hand the frontend the same variable -- Compose from `environment()` below, EKS
    from the generated Helm values (`launcher.eks.values.frontend_env`) -- so the rendering
    lives here beside the decision rather than once per target. Off is empty rather than
    `"false"` because the browser's test is `NEXT_PUBLIC_HYPERDX_ENABLED === 'true'`, and an
    empty value is also what the Compose default renders for an unset key.
    """
    return "true" if session_replay(values) else ""


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

    EKS-only keys are dropped on the way out (see eks_only) and PUBLIC_HYPERDX_ENABLED is
    derived from SESSION_REPLAY (see session_replay); every remaining value is a string, so it
    can be handed to Compose as-is.
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
            # Read by the frontend server and handed to the browser as window.ENV (see
            # replay_flag for why off is empty rather than "false").
            "PUBLIC_HYPERDX_ENABLED": replay_flag(values),
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


# The fault scenario is one flag and one value. `targeting.if` is a JsonLogic if/then/else, so
# index 1 is the variant served for the product the fault is aimed at and index 2 the variant
# served for every other product. `defaultVariant` decides nothing here, because the targeting
# rule always yields a variant -- which is why the retired `flag.sh`, whose only edit was to
# `defaultVariant`, could never switch this flag, and why the EKS scenario writer goes through
# the transform below instead of the generic `eks flag` setter.
FAULT_FLAG = "productCatalogFailure"
FAULT_SCENARIO = "backend-failure"
# flagd evaluates a flag's targeting rule only while the flag is enabled, so a scenario written
# against a disabled flag lands in the file and serves nothing.
FLAG_ENABLED = "ENABLED"


def fault_enabled(name):
    """Whether a scenario name means the product-catalog fault is serving errors."""
    return name == FAULT_SCENARIO


def _fault_branch(document):
    """`productCatalogFailure`'s `targeting.if` list inside a flagd document, validated.

    Validated rather than indexed blind: this runs against a file a chart bump or a hand edit
    could have reshaped, and a scenario that silently did nothing is the worst thing that can
    happen halfway through a walkthrough. Returned as the live list, so the caller writing to
    it writes into `document`.

    `state` is checked here and not repaired. The retired writer set it to `ENABLED` on every
    scenario, which hid the one case worth hearing about: a flag somebody disabled by hand stays
    disabled, so the rule below is never evaluated and the fault never fires however the branch
    reads. Refusing says that; writing it back says nothing and quietly re-enables a flag the
    caller may have turned off deliberately.
    """
    flags = document.get("flags") if isinstance(document, dict) else None
    entry = (flags or {}).get(FAULT_FLAG) if isinstance(flags, dict) else None
    try:
        branch = entry["targeting"]["if"]
    except (KeyError, TypeError):
        core.die(f"the flagd configuration has no {FAULT_FLAG} targeting rule")
    if not isinstance(branch, list) or len(branch) != 3:
        core.die(f"{FAULT_FLAG}'s targeting.if is not an if/then/else: {branch!r}")
    if entry.get("state") != FLAG_ENABLED:
        core.die(
            f"{FAULT_FLAG} is {entry.get('state')!r}, not {FLAG_ENABLED!r}: flagd would ignore "
            "its targeting rule, so the scenario would appear to apply and change nothing"
        )
    return branch


def fault_variant(document):
    """The variant `productCatalogFailure` serves for the product the fault is aimed at.

    What a scenario actually sets, so the EKS writer can read the file back and check the same
    value this module wrote rather than a value of its own devising.
    """
    return _fault_branch(document)[1]


def scenario_flags(document, name):
    """A flagd document with the catalog fault set for the scenario `name`.

    The whole of what a scenario means, as a pure function: the local writer below and
    `eks.flags.scenario_write_eks()` both call it, so `backend-failure` cannot come to mean one
    thing on the laptop and another on the cluster. `document` is left exactly as it was given,
    and nothing but `targeting.if[1]` differs in the result.
    """
    updated = copy.deepcopy(document)
    _fault_branch(updated)[1] = "on" if fault_enabled(name) else "off"
    return updated


def scenario_summary(name):
    """The line both writers print once the scenario has landed."""
    state = "enabled" if fault_enabled(name) else "disabled"
    return f"Catalog fault {state}. Choose '{name}' in the chat UI."


def flagd_file():
    """The laptop's flagd configuration: the copy the Compose stack mounts into flagd."""
    return core.RUNTIME / "flagd/demo.flagd.json"


def scenario(name):
    """Apply a scenario to the laptop's flagd file: read, transform, write through a rename."""
    path = flagd_file()
    updated = scenario_flags(json.loads(path.read_text()), name)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(updated, indent=2) + "\n")
    # The rename is the point: flagd fsnotify-watches this file, and an inode swap arrives as
    # one event instead of a truncate followed by a series of appends. The EKS writer performs
    # the same swap inside the container with `mv` (see k8s.exec_write).
    temp.replace(path)
    print(scenario_summary(name))


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
