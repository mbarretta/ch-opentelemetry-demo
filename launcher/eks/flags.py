"""The demo's flagd feature flags, read and written in the running cluster.

Ports `flag.sh`, and adds the EKS half of `demo.py scenario`. Both go through the same file
inside the flagd-ui container (see `k8s.exec_read`), and the scenario transform is the laptop's
`stack.scenario_flags()` so one definition decides what a scenario means on both targets.

Why a file inside a pod rather than `cm/flagd-config`: flagd never reads the ConfigMap. An init
container copies it once into an emptyDir shared with flagd-ui, and flagd fsnotify-watches that
copy. Patching the ConfigMap changes nothing until the pod restarts -- and that restart discards
every toggle made here, which is exactly what `--reset` is for.
"""

import json
import time

from .. import core, stack
from . import aws, config, k8s

# The copy flagd watches, inside the flagd-ui container beside it.
FLAG_FILE = "/app/data/demo.flagd.json"
# flagd re-reads the file on the fsnotify event, so the read-back waits for it rather than
# racing it. The same two seconds the bash allowed.
CONFIRM_DELAY = 2
# `rollout status` after a restart: the ceiling the bash used, kept because a flagd pod that
# takes longer than this is not coming back on its own.
ROLLOUT_TIMEOUT = "120s"
# Column widths for the listing, wide enough for the longest flag name the chart ships
# (`recommendationCacheFailure`) and the longest variant name (`10sec`).
NAME_WIDTH = 28
VARIANT_WIDTH = 8

# Said in the help and again at the foot of the listing: `productCatalogFailure` is the one flag
# this command will not set, because setting it here would appear to work and change nothing.
SCENARIO_NOTE = (
    f"{stack.FAULT_FLAG} is not settable here: its targeting rule always yields a variant, so "
    f"`defaultVariant` decides nothing for it. Use `demo.py scenario {stack.FAULT_SCENARIO} "
    "--target eks` instead."
)


def register(subparsers):
    """Declare `eks flag`."""
    parser = subparsers.add_parser(
        "flag",
        help="list, show or set a flagd feature flag without restarting anything",
        description="List, show or set a flagd feature flag without restarting anything. "
        + SCENARIO_NOTE,
        epilog=SCENARIO_NOTE,
    )
    parser.add_argument(
        "name", nargs="?", help="the flag to show or set; omitted lists every flag"
    )
    parser.add_argument(
        "variant", nargs="?", help="the variant to set; omitted shows the flag's variants"
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="restart flagd, returning every flag to the chart defaults",
    )
    parser.set_defaults(handler=lambda args: flag(args.name, args.variant, reset=args.reset))


def connect():
    """What both entry points need before they can reach the cluster's flagd.

    `tofu` among the required commands because the kubeconfig is built from the OpenTofu
    outputs; `jq`, which the bash needed, is gone with the shell.
    """
    core.need("aws", "tofu", "kubectl")
    aws.aws_login()
    k8s.kubeconfig()


def read_document():
    """The flagd configuration the cluster's flagd is currently watching, parsed."""
    text = k8s.exec_read(FLAG_FILE)
    try:
        return json.loads(text or "")
    except ValueError:
        core.die(
            f"could not parse {FLAG_FILE} out of the {k8s.FLAGD_CONTAINER} container; "
            "run `demo.py eks flag --reset` to restore the chart's copy"
        )


def write_document(document):
    """Replace that file with `document`, as one atomic change flagd sees once."""
    k8s.exec_write(FLAG_FILE, json.dumps(document, indent=2) + "\n")


def flag_entry(document, name):
    """One flag's definition, or die naming the flag and listing the ones that exist.

    By name rather than as an empty answer: a typo is the likeliest reason to get here, and the
    fifteen flags the chart ships are not something anyone has memorised.
    """
    flags = document.get("flags") or {}
    if name not in flags:
        core.die(f"no such flag: {name}\nknown flags: {', '.join(sorted(flags))}")
    return flags[name]


def variants(entry):
    """A flag's variant names, in file order: the keys of its `variants` object."""
    return list(entry.get("variants") or {})


def flag(name=None, variant=None, reset=False):
    """List every flag, show one, or set one to a variant; `--reset` restarts flagd.

    Validates the flag and the variant against the file before writing, is a no-op when the
    variant is already set, and re-reads afterwards so the report is what flagd now holds.
    `productCatalogFailure` is not set from here: its targeting rule always yields a variant,
    which is why `demo.py scenario` exists.
    """
    if reset and (name or variant):
        core.die("--reset returns every flag to the chart defaults; it takes no flag name")
    connect()
    if reset:
        return reset_flags()

    document = read_document()
    if name is None:
        return list_flags(document)

    entry = flag_entry(document, name)
    available = variants(entry)
    current = entry.get("defaultVariant")
    if variant is None:
        print(f'{name} is "{current}" (available: {", ".join(available)})')
        return None

    # Showing the catalog fault is fine; setting it from here is not. The write would succeed
    # and change nothing, which is worse than a refusal that names the command that works.
    if name == stack.FAULT_FLAG:
        core.die(SCENARIO_NOTE)
    # Rejected here rather than in the file: flagd accepts a document naming a variant that
    # does not exist and silently falls back, which is a confusing thing to debug mid-demo.
    if variant not in available:
        core.die(f"'{variant}' is not a variant of {name} (available: {', '.join(available)})")
    if variant == current:
        print(f'{name} is already "{variant}"')
        return None

    entry["defaultVariant"] = variant
    write_document(document)

    # Read the file back rather than trusting the write: the exec could have succeeded against
    # a pod that is about to be replaced, and a flag nobody set is better reported than found.
    time.sleep(CONFIRM_DELAY)
    landed = flag_entry(read_document(), name).get("defaultVariant")
    if landed != variant:
        core.die(f'the write did not take: {name} is still "{landed}"')
    print(f"{name}: {current} -> {landed}")
    return landed


def list_flags(document):
    """Every flag with its current variant and the variants it accepts, sorted by name."""
    core.log("feature flags currently served by flagd")
    flags = document.get("flags") or {}
    for name in sorted(flags):
        entry = flags[name]
        current = str(entry.get("defaultVariant", ""))
        print(
            f"  {name:<{NAME_WIDTH}} {current:<{VARIANT_WIDTH}} [{', '.join(variants(entry))}]"
        )
    print(f"\n{SCENARIO_NOTE}")
    return None


def reset_flags():
    """Restart flagd, which discards every toggle and re-copies the ConfigMap's defaults."""
    core.log("restarting flagd -- every flag returns to the chart defaults")
    core.run("kubectl", "-n", config.NS_DEMO, "rollout", "restart", k8s.FLAGD_DEPLOYMENT)
    core.run(
        "kubectl",
        "-n",
        config.NS_DEMO,
        "rollout",
        "status",
        k8s.FLAGD_DEPLOYMENT,
        f"--timeout={ROLLOUT_TIMEOUT}",
    )
    return None


def scenario_write_eks(name):
    """Apply a demo scenario to the cluster's flagd file and confirm it by re-reading.

    The transform is `stack.scenario_flags()` -- the same `targeting.if[1]` edit the laptop
    makes -- so a scenario cannot come to mean two different things on the two targets.
    """
    connect()
    document = read_document()
    updated = stack.scenario_flags(document, name)
    wanted = stack.fault_variant(updated)
    if updated == document:
        print(f"{stack.scenario_summary(name)} (already set)")
        return wanted

    write_document(updated)
    time.sleep(CONFIRM_DELAY)
    landed = stack.fault_variant(read_document())
    if landed != wanted:
        core.die(
            f"the scenario did not take: {stack.FAULT_FLAG} still serves "
            f'"{landed}" for the product it fails on, not "{wanted}"'
        )
    print(stack.scenario_summary(name))
    return landed
