"""launcher.eks.lifecycle: deploy, up and down, and the order the cluster has to see them in.

`deploy` is the command where ordering *is* the behaviour, so these tests assert the recorded
sequence of processes rather than any return value. Three orderings are load-bearing and each
has a cost attached: the published-image gate before the first AWS call (otherwise the release
rolls out last week's images), the zero-node refusal before the first kubectl call (otherwise
the Secrets and the collector land in a cluster with nothing to run them on, and `helm --wait`
takes twenty minutes to say so), and `demo-values.yaml` before `values.generated.yaml` in the
`helm upgrade` argv (Helm replaces lists, so the reverse order silently drops the agent's
collector endpoint and its optional secret references).

Every process goes through `fake_sh`, so the credentials in the fixtures below are also what
proves the constraint the whole Secret path exists for: no secret value in any recorded argv.
"""

import base64
import json
import shutil
from pathlib import Path

import pytest
import yaml
from conftest import Recorder, write_env

from launcher import core, images
from launcher.eks import aws, config, k8s, lifecycle, tunnel, values

REGION = "us-east-1"
REGISTRY = "111122223333.dkr.ecr.us-east-1.amazonaws.com"
CLUSTER = "otel-demo-eks"
NODEGROUP = f"{CLUSTER}-demo"
NODE_COUNT = 2
PROFILE = "demo"
PORT = 8080
TAG = "c0ffee1234"

# Every credential the deploy handles, marked so a leak is a substring match rather than a
# guess: the five the ClickStack collector reads, the Langfuse pair, the agent's API key, and
# the ECR password `docker login` is fed on stdin.
CLICKSTACK = {
    "CLICKHOUSE_ENDPOINT": "https://ch.test:8443",
    "CLICKHOUSE_USER": "clickstack",
    "CLICKHOUSE_PASSWORD": "sensitive-clickhouse-password",
    "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE": "otel",
    "OTLP_AUTH_TOKEN": "sensitive-otlp-token",
}
LANGFUSE = {
    "LANGFUSE_BASE_URL": "https://cloud.langfuse.test",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-sensitive-public",
    "LANGFUSE_SECRET_KEY": "sk-lf-sensitive-secret",
}
API_KEY = "sensitive-llm-api-key"
ECR_PASSWORD = "sensitive-ecr-password"
AUTH_HEADER = config.langfuse_auth_header(
    LANGFUSE["LANGFUSE_PUBLIC_KEY"], LANGFUSE["LANGFUSE_SECRET_KEY"]
)
SECRET_VALUES = (
    CLICKSTACK["CLICKHOUSE_PASSWORD"],
    CLICKSTACK["OTLP_AUTH_TOKEN"],
    LANGFUSE["LANGFUSE_PUBLIC_KEY"],
    LANGFUSE["LANGFUSE_SECRET_KEY"],
    API_KEY,
    ECR_PASSWORD,
    AUTH_HEADER,
    # The header without its scheme: the base64 payload is the credential itself.
    AUTH_HEADER.split(" ", 1)[1],
)

# A fully configured `.env`: the agent settings the generated values render, plus every
# credential above. The four optional-Secret tests below take keys back out of it.
ENV = {
    "AWS_PROFILE": PROFILE,
    "AWS_REGION": REGION,
    "EKS_TUNNEL_PORT": str(PORT),
    "AGENT_MODE": "live",
    "MCP_ENABLED": "True",
    "LLM_BASE_URL": "https://llm.test/v1",
    "LLM_MODEL": "test-model",
    "LANGFUSE_PROMPT_LABEL": "workshop",
    "LANGFUSE_PROJECT_ID": "cm0project",
    "LANGFUSE_PUBLIC_URL": "https://cloud.langfuse.test",
    "CLICKSTACK_TRACE_URL_TEMPLATE": "https://clickstack.test/search?trace={trace_id}",
    "ASSISTANT_DEMO_DETAILS": "true",
    "API_KEY": API_KEY,
    **CLICKSTACK,
    **LANGFUSE,
}

# `tofu output -json`, in the wrapped shape OpenTofu answers with.
OUTPUTS = {
    "region": {"value": REGION},
    "cluster_name": {"value": CLUSTER},
    "nodegroup_name": {"value": NODEGROUP},
    "node_count": {"value": NODE_COUNT},
    "ecr_registry": {"value": REGISTRY},
}
# What `demo.py publish` recorded, in the shape `images.PUBLISHED_FIELDS` pins.
PUBLISHED = {
    service: {
        "repository": f"{REGISTRY}/ch-opentelemetry-demo/{service}",
        "tag": TAG,
        "digest": "sha256:" + "d" * 64,
    }
    for service in core.IMAGES
}

# The two `describe-nodegroup` reads share a prefix; `fake_sh` answers the longest match, which
# is how one fake shell answers both the status and the desired-size question.
NG_DESIRED = (
    f"aws eks describe-nodegroup --cluster-name {CLUSTER} --nodegroup-name {NODEGROUP} "
    "--query nodegroup.scalingConfig.desiredSize"
)
NG_STATUS = f"aws eks describe-nodegroup --cluster-name {CLUSTER}"
# Two nodes that have registered and are schedulable, as `kubectl get nodes --no-headers`
# prints them.
READY_NODES = (
    "ip-10-0-1-11.ec2.internal   Ready   <none>   2m    v1.33.0\n"
    "ip-10-0-2-22.ec2.internal   Ready   <none>   2m    v1.33.0\n"
)

LOGIN = [["aws", "configure", "list-profiles"], ["aws", "sts", "get-caller-identity"]]
APPLY = ["kubectl", "apply", "-f", "-"]


def tofu_output():
    return ["tofu", f"-chdir={config.tofu_dir()}", "output", "-json"]


def describe(query):
    return [
        "aws",
        "eks",
        "describe-nodegroup",
        "--cluster-name",
        CLUSTER,
        "--nodegroup-name",
        NODEGROUP,
        "--query",
        query,
        "--output",
        "text",
    ]


def scale_to(size):
    """The two calls `aws.ng_scale` makes once it has decided the size really has to change."""
    return [
        [
            "aws",
            "eks",
            "update-nodegroup-config",
            "--cluster-name",
            CLUSTER,
            "--nodegroup-name",
            NODEGROUP,
            "--scaling-config",
            f"minSize=0,maxSize={NODE_COUNT},desiredSize={size}",
        ],
        [
            "aws",
            "eks",
            "wait",
            "nodegroup-active",
            "--cluster-name",
            CLUSTER,
            "--nodegroup-name",
            NODEGROUP,
        ],
    ]


def kubeconfig():
    return [
        [
            "aws",
            "eks",
            "update-kubeconfig",
            "--region",
            REGION,
            "--name",
            CLUSTER,
            "--alias",
            CLUSTER,
            "--profile",
            PROFILE,
        ],
        ["kubectl", "config", "use-context", CLUSTER],
    ]


def argvs(shell):
    return [call.argv for call in shell.calls]


def one(shell, *prefix):
    matched = [call for call in shell.calls if call.argv[: len(prefix)] == list(prefix)]
    assert len(matched) == 1, f"expected exactly one {' '.join(prefix)} call, got {matched}"
    return matched[0]


def applied(shell):
    """Every manifest piped to `kubectl apply -f -`, in order, as (kind, name) pairs."""
    return [
        (document["kind"], document["metadata"]["name"]) for document in manifests(shell)
    ]


def manifests(shell):
    return [json.loads(call.stdin) for call in shell.calls if call.argv == APPLY]


def secret(shell, name):
    """One applied Secret, with its values decoded, or None when it was never applied."""
    found = [
        document
        for document in manifests(shell)
        if document["kind"] == "Secret" and document["metadata"]["name"] == name
    ]
    if not found:
        return None
    assert len(found) == 1, f"{name} was applied {len(found)} times"
    return {
        key: base64.b64decode(value).decode() for key, value in found[0]["data"].items()
    }


def network_policy():
    """The `kubectl apply` of the agent's ingress policy, from the committed manifest."""
    return ["kubectl", "apply", "-f", str(config.k8s_dir() / config.NETWORK_POLICY_MANIFEST)]


def unapplied_static_manifests(shell):
    """Every `config.STATIC_MANIFESTS` entry no recorded `kubectl apply -f <path>` named.

    The tuple is what `eks check` parses offline, but the deploy applies its members one at a
    time from two functions rather than by iterating it -- the policy before the release, the
    collector with its own roll-out wait -- so the two sets are related by convention and
    nothing else. Reading the applied set out of the recording rather than from a list kept
    here is what makes a third entry added to the tuple show up as unapplied.

    The piped `apply -f -` calls are not this: those carry the namespaces and Secrets that are
    assembled in Python, and `applied()` above is the accessor for them. An apply is matched by
    its verb rather than by a fixed argv prefix, because `k8s.apply_manifest` writes a namespace
    flag ahead of `apply` and a manifest applied by path could be spelled the same way.
    """
    from_disk = {
        path
        for call in shell.calls
        if call.argv[0] == "kubectl" and "apply" in call.argv
        for flag, path in zip(call.argv, call.argv[1:])
        if flag == "-f" and path != "-"
    }
    return [
        name for name in config.STATIC_MANIFESTS if str(config.k8s_dir() / name) not in from_disk
    ]


def deletion(name):
    return ["kubectl", "-n", config.NS_DEMO, "delete", "secret", name, "--ignore-not-found"]


def generated():
    """The values document the deploy wrote, read back off disk as Helm would read it."""
    return yaml.safe_load(values.values_path().read_text())


# The collaborators outside this module, recorded with the process count at the time. Where a
# call sits in the sequence is the assertion here rather than what it was passed -- that is
# `at()` -- because `require_published` has to have run before the first process and the tunnel
# after the last one, and neither fact is visible in `fake_sh`'s own record: neither of them
# starts a process of its own. The same body records arguments for tests/test_eks_infra.py,
# where it is `Stubs`.
Steps = Recorder


@pytest.fixture
def redirect_env():
    """The `.env` the shared `redirected` fixture writes for these tests."""
    return ENV


@pytest.fixture
def redirected(redirected, monkeypatch):
    """The shared checkout, plus the two patches and the one real directory a deploy needs.

    `deploy/eks/k8s` is copied in rather than faked: the deploy reads `demo-values.yaml` for
    the static `envOverrides` it has to carry into the generated document, and a stand-in file
    would make that carry-over untestable.
    """
    # Every external command is faked; `need` would still look for aws, docker, tofu, kubectl
    # and helm on the developer's PATH, where a missing one is not this module's problem.
    monkeypatch.setattr(core, "need", lambda *commands: None)
    # The process-wide `tofu output` cache outlives a redirected core.ROOT, so it starts empty.
    monkeypatch.setattr(aws, "_OUTPUTS", None)
    shutil.copytree(Path(__file__).parent.parent / "deploy/eks/k8s", redirected / "deploy/eks/k8s")
    return redirected


@pytest.fixture
def cluster(redirected, fake_sh):
    """A live session, readable OpenTofu outputs, and a node group that is scaled up."""
    fake_sh.reply("tofu", stdout=json.dumps(OUTPUTS))
    fake_sh.reply("aws configure list-profiles", stdout=f"default\n{PROFILE}\n")
    fake_sh.reply("aws ecr get-login-password", stdout=f"{ECR_PASSWORD}\n")
    fake_sh.reply(NG_STATUS, stdout="ACTIVE")
    fake_sh.reply(NG_DESIRED, stdout=str(NODE_COUNT))
    fake_sh.reply("kubectl get nodes", stdout=READY_NODES)
    return fake_sh


@pytest.fixture
def steps(cluster, monkeypatch):
    """The three collaborators a deploy must not really run: the gate and the tunnel."""
    recorder = Steps(cluster)
    recorder.patch(monkeypatch, images, "require_published", result=PUBLISHED)
    # The real `start()` spawns a detached CLI and then polls http://localhost:8080 for twenty
    # seconds; `stop()` signals a process group. Both have their own tests.
    recorder.patch(monkeypatch, tunnel, "start", result=True)
    recorder.patch(monkeypatch, tunnel, "stop")
    return recorder


# --- deploy --------------------------------------------------------------------------------


def test_deploy_runs_every_step_in_the_order_the_cluster_needs_them(cluster, steps):
    """AC2: the thirteen steps, as the argv of every process the deploy starts, in order."""
    lifecycle.deploy()

    assert argvs(cluster) == [
        # 2. the session, then the ECR login, which is also the first read of the state
        *LOGIN,
        tofu_output(),
        ["aws", "ecr", "get-login-password", "--region", REGION],
        ["docker", "login", "--username", "AWS", "--password-stdin", REGISTRY],
        # 3. the zero-node refusal, before any kubectl call
        describe("nodegroup.scalingConfig.desiredSize"),
        # 4. kubeconfig and both namespaces
        *kubeconfig(),
        APPLY,
        APPLY,
        # 5. the agent's ingress policy, before the release creates the pod it selects
        network_policy(),
        # 6. clickstack-credentials
        APPLY,
        # 7. the collector, its roll-out, and its first log lines
        ["kubectl", "apply", "-f", str(config.k8s_dir() / config.COLLECTOR_MANIFEST)],
        [
            "kubectl",
            "-n",
            config.NS_CS,
            "rollout",
            "status",
            config.COLLECTOR_DEPLOYMENT,
            "--timeout=300s",
        ],
        ["kubectl", "-n", config.NS_CS, "logs", config.COLLECTOR_DEPLOYMENT, "--tail=40"],
        # 8. and 9. the OTLP token, then the two optional Secrets
        APPLY,
        APPLY,
        APPLY,
        # 11. the release, from the two values files
        ["helm", "repo", "add", config.HELM_REPO_NAME, config.HELM_REPO_URL, "--force-update"],
        ["helm", "repo", "update", config.HELM_REPO_NAME],
        [
            "helm",
            "upgrade",
            "--install",
            config.RELEASE,
            f"{config.HELM_REPO_NAME}/{k8s.CHART_NAME}",
            "--version",
            config.CHART_VERSION,
            "-n",
            config.NS_DEMO,
            "-f",
            str(config.k8s_dir() / values.STATIC_VALUES),
            "-f",
            str(values.values_path()),
            "--wait",
            "--timeout",
            k8s.HELM_TIMEOUT,
        ],
    ]
    # Which manifest went to each `apply -f -`, in the order they were piped: the namespaces
    # before the Secrets they hold, and clickstack-credentials before the collector that reads
    # it (the collector manifest is the named `apply -f` between them above).
    assert applied(cluster) == [
        ("Namespace", config.NS_CS),
        ("Namespace", config.NS_DEMO),
        ("Secret", config.SECRET_CLICKSTACK),
        ("Secret", config.SECRET_OTLP_TOKEN),
        ("Secret", config.SECRET_LANGFUSE),
        ("Secret", config.SECRET_LLM),
    ]
    # 1. and 12.: the gate ran before the first process, the tunnel after the last one.
    assert steps.at("require_published") == [0]
    assert steps.at("start") == [len(cluster.calls)]


def test_deploy_restricts_ingress_to_the_agent_before_the_release_creates_it(cluster, steps):
    """AC4: the committed NetworkPolicy is applied, once, and ahead of the `helm upgrade`.

    The order is the assertion rather than the presence: the agent answers unauthenticated
    requests with the live model credential in its environment, so a policy applied after the
    release would leave it open for the length of a twenty-minute `helm --wait`.
    """
    lifecycle.deploy()

    sequence = argvs(cluster)
    assert sequence.count(network_policy()) == 1, sequence
    assert sequence.index(network_policy()) < [argv[:2] for argv in sequence].index(
        ["helm", "upgrade"]
    )
    # Applied by path rather than piped, so the committed manifest is the whole contract: it
    # names the namespace itself, because nothing on that argv does.
    policy = yaml.safe_load(Path(network_policy()[-1]).read_text())
    assert policy["kind"] == "NetworkPolicy"
    assert policy["metadata"]["namespace"] == config.NS_DEMO


def test_every_manifest_eks_check_parses_is_one_the_deploy_actually_applies(cluster, steps):
    """The set `eks check` validates and the files a deploy applies are the same set.

    `eks check` iterates `config.STATIC_MANIFESTS` (launcher/eks/check.py), so an entry added
    to the tuple is parsed, reported as checked and -- because the deploy applies its manifests
    one at a time rather than by iterating the tuple -- never sent to the API server. Nothing
    but this assertion connects the two, which is why it reads the applied set back out of the
    recorded processes instead of naming the two manifests it expects.
    """
    lifecycle.deploy()

    unapplied = unapplied_static_manifests(cluster)
    assert unapplied == [], (
        f"config.STATIC_MANIFESTS names {', '.join(unapplied)}, which `eks check` parses and "
        "this deploy never applied: give it an apply in launcher/eks/lifecycle.py, or take it "
        "out of the tuple so the check stops claiming it"
    )


def test_the_static_manifest_guard_names_an_entry_the_deploy_never_applies(
    cluster, steps, monkeypatch
):
    """The guard above, fed the failure it exists to catch: a third entry nothing applies.

    The tuple is patched rather than edited, so the real one keeps its two members; what is
    asserted is the reported name, because a guard that went silent -- collecting the applied
    paths in a shape that never matches, say -- would otherwise still look green.
    """
    never_applied = "ingress-allowlist.yaml"
    lifecycle.deploy()
    monkeypatch.setattr(config, "STATIC_MANIFESTS", (*config.STATIC_MANIFESTS, never_applied))

    assert unapplied_static_manifests(cluster) == [never_applied]


def test_deploy_refuses_a_node_group_at_zero_before_it_touches_the_cluster(cluster, steps):
    """AC2: deploying onto no nodes is 20 minutes of `helm --wait` and then a failure anyway."""
    cluster.reply(NG_DESIRED, stdout="0")

    with pytest.raises(SystemExit) as failure:
        lifecycle.deploy()

    assert "eks up" in str(failure.value), "name the command that scales up and deploys"
    assert [argv for argv in argvs(cluster) if argv[0] in ("kubectl", "helm")] == []
    assert not values.values_path().exists(), "and nothing was written for it either"


def test_deploy_gates_on_the_published_images_before_any_aws_or_kubectl_call(cluster, steps):
    """AC3: the release points at four ECR tags, so all four are checked before anything runs."""
    lifecycle.deploy()

    assert steps.at("require_published") == [0]


def test_deploy_aborts_when_the_published_images_are_missing_or_stale(
    cluster, steps, monkeypatch
):
    """AC3: the gate's refusal is the deploy's refusal -- nothing else has happened yet."""
    steps.patch(
        monkeypatch,
        images,
        "require_published",
        raises=SystemExit("error: no published image for agent"),
    )

    with pytest.raises(SystemExit, match="no published image"):
        lifecycle.deploy()

    assert argvs(cluster) == [], "no session, no state read, no cluster call"


def test_the_helm_upgrade_reads_the_static_values_before_the_generated_ones(cluster, steps):
    """AC6: two `-f` flags in that order and no `--set`, because Helm replaces lists.

    Both files set `components.*.envOverrides`. Generated-last wins the merge and the generated
    document carries the static entries through; generated-first would drop the static list --
    the agent's OTEL_EXPORTER_OTLP_ENDPOINT, its USE_VCR and its four optional secretKeyRefs --
    with nothing failing.
    """
    lifecycle.deploy()

    upgrade = one(cluster, "helm", "upgrade").argv
    files = [upgrade[index + 1] for index, flag in enumerate(upgrade) if flag == "-f"]
    assert files == [
        str(config.k8s_dir() / values.STATIC_VALUES),
        str(values.values_path()),
    ]
    assert [flag for flag in upgrade if flag.startswith("--set")] == [], "every value is a file"
    # Both files are readable at that moment, which is the other half of the contract: the
    # committed one is the checkout's, and the generated one carries the published tags.
    static = yaml.safe_load(Path(files[0]).read_text())
    assert static["components"]["chatbot"] == {"enabled": False}, "the committed file, not a stub"
    document = yaml.safe_load(Path(files[1]).read_text())
    assert [document["components"][service]["imageOverride"]["tag"] for service in core.IMAGES] == [
        TAG
    ] * len(core.IMAGES)


def test_no_secret_value_reaches_argv_and_every_secret_travels_on_stdin(cluster, steps):
    """The deploy's whole reason for assembling manifests in Python: `ps` shows argv to anyone."""
    lifecycle.deploy()

    for call in cluster.calls:
        for value in SECRET_VALUES:
            assert value not in call.line, f"{value} reached the command line of {call.line}"
    # The ClickStack collector's five keys, base64-encoded in a manifest fed to kubectl's stdin.
    assert secret(cluster, config.SECRET_CLICKSTACK) == CLICKSTACK
    assert secret(cluster, config.SECRET_OTLP_TOKEN) == {
        "OTLP_AUTH_TOKEN": CLICKSTACK["OTLP_AUTH_TOKEN"]
    }
    # The ECR password goes the same way, to `docker login --password-stdin`.
    assert one(cluster, "docker", "login").stdin == ECR_PASSWORD
    # And nothing secret is left on disk in the generated values either.
    for value in SECRET_VALUES:
        assert value not in values.values_path().read_text()


def test_with_langfuse_configured_the_secret_is_applied_and_the_exporter_appears(cluster, steps):
    """AC4: the four keys land in the Secret and the collector gets the exporter that reads it."""
    lifecycle.deploy()

    assert secret(cluster, config.SECRET_LANGFUSE) == {
        **LANGFUSE,
        "LANGFUSE_AUTH_HEADER": AUTH_HEADER,
    }
    assert deletion(config.SECRET_LANGFUSE) not in argvs(cluster)
    exporters = generated()["opentelemetry-collector"]["config"]["exporters"]
    assert values.LANGFUSE_EXPORTER in exporters
    assert exporters[values.LANGFUSE_EXPORTER]["headers"]["Authorization"] == (
        "${env:LANGFUSE_AUTH_HEADER}"
    ), "the collector expands it from the Secret; the value is never rendered"


def test_deploy_refuses_before_any_secret_when_langfuse_is_unconfigured(
    cluster, steps, redirected
):
    """AC1, AC3: Langfuse is mandatory now; the old "runs ClickStack-only" state is invalid.

    `config.load_langfuse_env` dies naming the missing keys, and it is read before the first
    kubectl call the same way `load_clickstack_env` already is -- so nothing, not even the
    namespaces, is created before the refusal.
    """
    write_env(redirected, {key: value for key, value in ENV.items() if key not in LANGFUSE})

    with pytest.raises(SystemExit) as failure:
        lifecycle.deploy()

    message = str(failure.value)
    for key in LANGFUSE:
        assert key in message, key
    assert argvs(cluster) == [], "no session, no state read, no cluster call"
    assert not values.values_path().exists()


def test_deploy_refuses_when_langfuse_is_half_configured(cluster, steps, redirected):
    """AC1: a partial set is a filled-in-half `.env`, not a request to deploy half a pipeline."""
    half = {key: value for key, value in ENV.items() if key != "LANGFUSE_SECRET_KEY"}
    write_env(redirected, half)

    with pytest.raises(SystemExit) as failure:
        lifecycle.deploy()

    assert "LANGFUSE_SECRET_KEY" in str(failure.value)
    assert argvs(cluster) == [], "no session, no state read, no cluster call"


def test_llm_credentials_are_applied_only_when_an_api_key_is_set(cluster, steps):
    """AC5: live mode needs the key; a scripted demo calls no model and must not carry one."""
    lifecycle.deploy()

    assert secret(cluster, config.SECRET_LLM) == {"API_KEY": API_KEY}
    assert deletion(config.SECRET_LLM) not in argvs(cluster)


def test_llm_credentials_are_deleted_when_no_api_key_is_set(cluster, steps, redirected):
    """AC5: the agent's reference to it is `optional: true`, so its absence is a valid deploy."""
    write_env(redirected, {key: value for key, value in ENV.items() if key != "API_KEY"})

    lifecycle.deploy()

    assert secret(cluster, config.SECRET_LLM) is None
    assert deletion(config.SECRET_LLM) in argvs(cluster)


def test_a_tunnel_that_will_not_start_does_not_fail_the_deploy(
    cluster, steps, monkeypatch, capsys
):
    """AC9: by then the release is installed, so a busy port is an inconvenience, not a failure."""
    steps.patch(
        monkeypatch,
        tunnel,
        "start",
        raises=SystemExit(f"error: port {PORT} is already in use by another process"),
    )

    lifecycle.deploy()

    out, err = capsys.readouterr()
    base = f"http://localhost:{PORT}/"
    for url in (base, f"{base}feature/", f"{base}loadgen/"):
        assert url in out, url
    assert "already in use" in err and "demo.py eks tunnel" in err
    assert one(cluster, "helm", "upgrade"), "the release still went in"


def test_deploy_prints_the_three_urls_of_a_tunnel_that_did_come_up(cluster, steps, capsys):
    """AC9: the storefront, the feature-flag UI and the load generator, all through one port."""
    lifecycle.deploy()

    out = capsys.readouterr().out
    base = f"http://localhost:{PORT}/"
    assert f"Storefront:      {base}" in out
    assert f"Feature flags:   {base}feature/" in out
    assert f"Load generator:  {base}loadgen/" in out


# --- up ------------------------------------------------------------------------------------


def test_up_scales_the_node_group_waits_for_the_nodes_then_deploys(cluster, steps, monkeypatch):
    """AC7: scale, wait for Ready nodes and CoreDNS, then hand over to deploy -- in that order."""
    cluster.reply(NG_DESIRED, stdout="0")
    steps.patch(monkeypatch, lifecycle, "deploy")

    lifecycle.up()

    assert argvs(cluster) == [
        *LOGIN,
        # The kubeconfig is written before the scale-up: an expired session or a cluster that
        # is gone then refuses in seconds rather than after five minutes of billed nodes.
        tofu_output(),
        *kubeconfig(),
        describe("nodegroup.status"),
        describe("nodegroup.scalingConfig.desiredSize"),
        *scale_to(NODE_COUNT),
        ["kubectl", "get", "nodes", "--no-headers"],
        [
            "kubectl",
            "-n",
            "kube-system",
            "rollout",
            "status",
            "deploy/coredns",
            f"--timeout={k8s.COREDNS_TIMEOUT}",
        ],
    ]
    assert steps.at("deploy") == [len(cluster.calls)], "deploy owns everything after the nodes"


def test_up_asks_for_the_node_count_by_keyword_not_by_position(cluster, steps, monkeypatch):
    """`wait_nodes_ready(count=None, timeout=NODES_TIMEOUT)`: position two is the timeout.

    A positional `wait_nodes_ready(2)` reads the same and does the same thing today, which is
    why this is worth pinning: the day the signature grows another parameter, a positional call
    would start waiting two seconds for the nodes and report them missing.
    """
    cluster.reply(NG_DESIRED, stdout="0")
    steps.patch(monkeypatch, lifecycle, "deploy")
    waited = []
    monkeypatch.setattr(k8s, "wait_nodes_ready", lambda *args, **kwargs: waited.append((args, kwargs)))

    lifecycle.up()

    assert waited == [((), {"count": NODE_COUNT})]


# --- down ----------------------------------------------------------------------------------


def test_down_closes_the_tunnel_removes_the_workloads_then_scales_to_zero(cluster, steps):
    """AC8: the demo goes before the collector, and the node group goes last."""
    lifecycle.down()

    assert steps.at("stop") == [0], "the tunnel points at pods that are about to be deleted"
    assert argvs(cluster) == [
        *LOGIN,
        tofu_output(),
        *kubeconfig(),
        [
            "helm",
            "uninstall",
            config.RELEASE,
            "-n",
            config.NS_DEMO,
            "--ignore-not-found",
            "--wait",
            "--timeout",
            "5m",
        ],
        [
            "kubectl",
            "delete",
            "ns",
            config.NS_DEMO,
            config.NS_CS,
            "--ignore-not-found",
            "--timeout=300s",
        ],
        describe("nodegroup.status"),
        describe("nodegroup.scalingConfig.desiredSize"),
        *scale_to(0),
    ]


def test_down_keep_only_scales_to_zero(cluster, steps):
    """AC8: the workloads stay in the API with nowhere to run and reschedule on the way up."""
    lifecycle.down(keep=True)

    assert steps.at("stop") == [0]
    assert [argv for argv in argvs(cluster) if argv[0] in ("kubectl", "helm")] == []
    assert argvs(cluster) == [
        *LOGIN,
        # `ng_scale` resolves the cluster and the node group from the state before it reads the
        # group's status, so the one `tofu output` of the process happens here instead of in
        # the kubeconfig that `--keep` skips.
        tofu_output(),
        describe("nodegroup.status"),
        describe("nodegroup.scalingConfig.desiredSize"),
        *scale_to(0),
    ]
