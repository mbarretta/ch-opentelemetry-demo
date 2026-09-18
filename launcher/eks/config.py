"""What the EKS target is: its pinned chart, its names, and the keys it reads from `.env`.

Every other module under `launcher.eks` reads its constants from here, so the names that must
not drift -- the OpenTofu state location, the release and namespace names, the Kubernetes Secret
names the static Helm values reference -- are declared once and in one file.

Paths are functions rather than constants because `core.ROOT` and `core.RUNTIME` are what the
tests redirect; a module-level `core.RUNTIME / "eks"` would bind the real directory at import.
"""

import base64
import os

from .. import core

# The demo chart and its repository. The chart version is pinned: its collector processor names
# are what the generated values and `eks check` assert against, and a bump can also move the
# frontend image the replay patch expects.
CHART_VERSION = "0.41.2"
HELM_REPO_NAME = "open-telemetry"
HELM_REPO_URL = "https://open-telemetry.github.io/opentelemetry-helm-charts"
RELEASE = "otel-demo"
# The demo release and the ClickStack collector live in one namespace each.
NS_DEMO = "otel-demo"
NS_CS = "clickstack"

# OpenTofu state. One bucket per AWS account, deterministic so `eks destroy --purge-state` and a
# second laptop can find it with no local state. Renaming either the bucket prefix or the key
# orphans the state of the live cluster, so both stay exactly as the bash `init.sh` wrote them.
STATE_BUCKET_PREFIX = "otel-demo-eks-tfstate-"
STATE_KEY = "otel-demo-eks/terraform.tfstate"
DEFAULT_REGION = "us-east-1"

# The storefront is private: access is a local port-forward, not an ingress.
DEFAULT_TUNNEL_PORT = "8080"

# Kubernetes Secrets. `deploy/eks/k8s/demo-values.yaml` references these names in
# `extraEnvsFrom` and `secretKeyRef`, so they are part of the deployment's contract.
SECRET_CLICKSTACK = "clickstack-credentials"
SECRET_OTLP_TOKEN = "clickstack-otlp-token"
SECRET_LANGFUSE = "langfuse-credentials"
SECRET_LLM = "llm-credentials"

# The ClickStack collector's ClickHouse Cloud credentials plus the shared token the demo's
# gateway collector presents to it. All five are required: the collector reads them from
# `clickstack-credentials` and CrashLoops without any one of them.
CLICKSTACK_KEYS = (
    "CLICKHOUSE_ENDPOINT",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE",
    "OTLP_AUTH_TOKEN",
)
# The agent's LLM credential, and the whole content of the `llm-credentials` Secret: one
# optional key, meaningful only in live mode (a scripted run calls no model).
LLM_KEYS = ("API_KEY",)
# Langfuse is required on this target, the same way the five ClickStack keys above are: the
# demo's whole point is correlated ClickStack + Langfuse telemetry, so load_langfuse_env dies
# rather than silently degrading to none.
LANGFUSE_KEYS = (
    "LANGFUSE_BASE_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
)
# What the `langfuse-credentials` Secret holds: the three keys plus the derived header, so the
# collector's exporter can reference `${env:LANGFUSE_AUTH_HEADER}` without assembling it itself.
LANGFUSE_SECRET_KEYS = (*LANGFUSE_KEYS, "LANGFUSE_AUTH_HEADER")


# The ClickStack collector: the static manifest `eks deploy` applies as-is, and the name it is
# addressed by afterwards. A Deployment rather than a pod, because the rollout renames the pod
# every time; `eks deploy` waits on it and tails it, `eks status` reports it, and `eks check`
# parses the manifest, so all three read the names from here.
COLLECTOR_MANIFEST = "clickstack-collector.yaml"
COLLECTOR_DEPLOYMENT = "deploy/clickstack-otel-collector"
# How many lines of the collector's log are worth showing: enough to see whether the ClickHouse
# credentials it has just read are being accepted.
COLLECTOR_LOG_TAIL = 40

# The agent's ingress policy: the other static manifest `eks deploy` applies as-is, restricting
# who may call the pod that holds the model credential. Named here rather than at the call site
# for the same reason the collector manifest is -- `eks deploy` applies it and `eks check`
# parses it, and a filename spelled in two modules is a filename that can drift in one.
NETWORK_POLICY_MANIFEST = "network-policy.yaml"
# Every manifest under `k8s_dir()` that is applied unchanged, which is exactly what `eks check`
# has to be able to parse offline. `eks deploy` applies each of them at its own point in the
# sequence rather than by iterating this tuple -- the policy before the release exists, the
# collector with its own roll-out wait and log tail -- so an entry added here has to be given an
# apply of its own in `launcher/eks/lifecycle.py`, and `tests/test_eks_deploy.py` fails until it
# has one. The generated and static *values* are not here: those are Helm input, and the check
# renders them instead.
STATIC_MANIFESTS = (COLLECTOR_MANIFEST, NETWORK_POLICY_MANIFEST)


def eks_dir():
    """The deployment data: OpenTofu, the static Helm values, the static manifests, the SQL."""
    return core.ROOT / "deploy/eks"


def tofu_dir():
    return eks_dir() / "tofu"


def k8s_dir():
    return eks_dir() / "k8s"


def run_dir():
    """Scratch state that survives between invocations: tunnel pidfile and log, generated values."""
    return core.RUNTIME / "eks"


def load_env():
    """Every configuration value the EKS target reads: the root `.env` under the environment.

    Deliberately not `stack.environment()`: that one adds the Compose plumbing and drops the
    EKS-only keys, which are exactly the keys this target is here for.
    """
    values = {**core.dotenv(core.ROOT / ".env"), **os.environ}
    return {key: str(value) for key, value in values.items() if value is not None}


def load_clickstack_env(env=None):
    """The five ClickStack keys, or die naming every blank and missing one at once.

    One message rather than one per run: filling these in is the first thing a new presenter
    does, and finding out about the fourth blank key on the fourth attempt is not a workflow.
    """
    values = load_env() if env is None else env
    core.require_keys(
        values,
        CLICKSTACK_KEYS,
        "The EKS section of .env.example describes all five; "
        "deploy/eks/sql/create-user.sql creates the ClickHouse user.",
    )
    return {key: values[key] for key in CLICKSTACK_KEYS}


def load_langfuse_env(env=None):
    """The `langfuse-credentials` Secret's four keys, or die naming every blank and missing one.

    Mandatory now, the same way `load_clickstack_env` already is: a blank or missing key --
    including all three at once, which used to return `{}` and let the deploy quietly leave
    Langfuse out -- dies instead. Correlated ClickStack + Langfuse telemetry is the whole point
    of this demo, so there is no valid "none of them" configuration left on this target.
    """
    values = load_env() if env is None else env
    core.require_keys(values, LANGFUSE_KEYS, "The EKS section of .env.example describes all three.")
    return langfuse_secret(values)


def langfuse_secret(values):
    """The `langfuse-credentials` Secret's shape, given a mapping that already carries every
    `LANGFUSE_KEYS` entry -- the four keys plus the derived header.

    Split out of `load_langfuse_env` so `launcher.eks.check`'s offline render, which validates
    its own synthetic environments rather than a real `.env`, can build the same shape without
    going through the die this function's caller performs.
    """
    secret = {key: values[key] for key in LANGFUSE_KEYS}
    secret["LANGFUSE_AUTH_HEADER"] = langfuse_auth_header(
        values["LANGFUSE_PUBLIC_KEY"], values["LANGFUSE_SECRET_KEY"]
    )
    return secret


def langfuse_auth_header(public_key, secret_key):
    """The Basic credentials Langfuse's OTLP endpoint expects, from the key pair.

    The collector gets this as `${env:LANGFUSE_AUTH_HEADER}`, never as a rendered value: on the
    laptop from the process environment, on EKS from the `langfuse-credentials` Secret.
    """
    encoded = base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    return "Basic " + encoded
