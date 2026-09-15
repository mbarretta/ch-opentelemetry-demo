"""Deploying the demo and moving the cluster between idle and up.

Ports `deploy.sh`, `up.sh` and `down.sh`.

`deploy` is the one command in this package where the *order* is the behaviour. Every refusal it
can make is made before the first cluster write, and every credential is in place before the
workload that reads it: a step out of place costs either a twenty-minute `helm --wait` on pods
that can never be scheduled, or a Secret sitting in a cluster that is not ready for it.

`up` and `down` are the idle switch around it. Between them the EKS control plane, the ECR
images and the OpenTofu state all stay, which is what makes coming back a five-minute job
rather than an hour's -- and what makes `down` worth running at the end of every session.
"""

import sys

from .. import core, images
from . import aws, config, k8s, tunnel, values

# The collector is one small pod pulling one pinned image, so it gets the five minutes the bash
# allowed it rather than the release's twenty.
COLLECTOR_ROLLOUT_TIMEOUT = "300s"
# `helm uninstall --wait` and the namespace deletion behind it. A namespace will not go while a
# finalizer is still running, and the release's own pods are the usual reason, which is why the
# uninstall waits first and the delete gets a ceiling of its own rather than hanging the CLI.
UNINSTALL_TIMEOUT = "5m"
NAMESPACE_DELETE_TIMEOUT = "300s"


def register(subparsers):
    """Declare `eks deploy`, `eks up` and `eks down`."""
    deploy_parser = subparsers.add_parser(
        "deploy", help="deploy the collector and the demo onto the running cluster, then tunnel"
    )
    deploy_parser.set_defaults(handler=lambda args: deploy())

    up_parser = subparsers.add_parser(
        "up", help="scale the node group up, wait for the nodes, then deploy"
    )
    up_parser.set_defaults(handler=lambda args: up())

    down_parser = subparsers.add_parser(
        "down", help="close the tunnel, remove the workloads and scale the node group to zero"
    )
    down_parser.add_argument(
        "--keep",
        action="store_true",
        help="scale to zero but leave the workloads in the API; they reschedule on the way up",
    )
    down_parser.set_defaults(handler=lambda args: down(keep=args.keep))


def deploy():
    """Deploy the ClickStack collector and the demo release, then open the tunnel.

    Ordered so that nothing expensive or half-done happens after a knowable refusal: the
    environment and the published-image gate first, then the AWS session and kubeconfig, then
    the zero-node refusal (deploying onto no nodes would leave every pod Pending until the
    20-minute `helm --wait` gave up), and only then the namespaces, the agent's NetworkPolicy,
    the Secrets, the collector and the release. Safe to re-run: every step is an apply or an
    upgrade.

    The order, and what each step is waiting for the one before it to have done:

    1. read and validate `.env`, and gate on `images.require_published()`
    2. AWS session and ECR login -- the first read of the OpenTofu state
    3. refuse a node group at zero, *before* the first kubectl call
    4. kubeconfig, then both namespaces (applied manifests, so a re-run is a no-op)
    5. the agent's NetworkPolicy, in the API before the release creates the pod it selects
    6. `clickstack-credentials`, which the collector in step 7 starts by reading
    7. the ClickStack collector: apply, wait for the roll-out, show its first log lines
    8. `clickstack-otlp-token`, the credential the demo's own collector presents to it
    9. `langfuse-credentials` and `llm-credentials`: applied, or deleted when `.env` dropped them
    10. the generated values, written from `.env` and the build manifest
    11. `helm upgrade --install` with the static and the generated values, in that order
    12. the tunnel, whose failure is tolerated
    13. the three URLs
    """
    # docker for the ECR login below; the other four are this command's whole tool set.
    core.need("aws", "docker", "tofu", "kubectl", "helm")

    # Everything `.env` decides, read and validated up front: a blank ClickStack key and a
    # half-configured Langfuse both refuse here, where nothing has been created yet, rather
    # than as a CrashLooping collector ten minutes in.
    env = config.load_env()
    clickstack = config.load_clickstack_env(env)
    langfuse = config.load_langfuse_env(env)
    llm = {key: env[key] for key in config.LLM_KEYS if env.get(key)}
    # The gate, ahead of every AWS and kubectl call: the release points at four ECR images by
    # repository and tag, and a deploy that cannot name all four at the current build tag would
    # roll out last week's demo perfectly happily.
    published = images.require_published()

    aws.aws_login()
    # The nodes pull from ECR with their own instance role, so this login is not what makes the
    # release work. It is here for two other reasons: it is the first read of the OpenTofu
    # outputs, so an unreadable state fails before the cluster is touched, and the usual answer
    # to the gate above refusing is `demo.py publish`, which then needs no login of its own.
    aws.ecr_login()

    # Before the first kubectl call, deliberately. Deploying onto zero nodes leaves every pod
    # Pending until the 20-minute `helm --wait` gives up, and by then the Secrets and the
    # collector are already in a cluster with nothing to run them on.
    if aws.ng_desired() == 0:
        core.die(
            "the node group is scaled to 0; run `demo.py eks up`, which scales it up and then "
            "deploys"
        )

    k8s.kubeconfig()
    k8s.ensure_namespace(config.NS_CS)
    k8s.ensure_namespace(config.NS_DEMO)

    restrict_agent_ingress()

    # The collector reads all five of these with `envFrom` and CrashLoops without any one of
    # them, so the Secret goes in before the manifest that references it.
    core.log(f"creating Secret {config.NS_CS}/{config.SECRET_CLICKSTACK}")
    k8s.apply_manifest(k8s.secret_manifest(config.SECRET_CLICKSTACK, config.NS_CS, clickstack))

    deploy_collector()

    # The demo's own gateway collector authenticates to the ClickStack collector with this
    # token: `demo-values.yaml` injects the Secret with `extraEnvsFrom` and the collector
    # expands `${env:OTLP_AUTH_TOKEN}` itself, so the token reaches neither the values nor the
    # rendered ConfigMap.
    core.log(f"creating Secret {config.NS_DEMO}/{config.SECRET_OTLP_TOKEN}")
    k8s.apply_manifest(
        k8s.secret_manifest(
            config.SECRET_OTLP_TOKEN,
            config.NS_DEMO,
            {"OTLP_AUTH_TOKEN": clickstack["OTLP_AUTH_TOKEN"]},
        )
    )
    apply_or_delete(config.SECRET_LANGFUSE, config.NS_DEMO, langfuse, "Langfuse credentials")
    apply_or_delete(config.SECRET_LLM, config.NS_DEMO, llm, ", ".join(config.LLM_KEYS))

    install_release(values.write_values(values.eks_values(env, published, langfuse)))
    open_tunnel()


def restrict_agent_ingress():
    """Apply the static NetworkPolicy that limits who may call the agent.

    With the namespace and before the release, not after it: the agent pod holds the live model
    credential and answers unauthenticated requests, so the policy that fronts it belongs in
    the API before the pod exists rather than for the second half of a `helm --wait`.

    Enforcement is the cluster's half of this, and it is not a given: the VPC CNI ignores
    NetworkPolicy objects unless the add-on is configured with `enableNetworkPolicy`, which
    `deploy/eks/tofu/eks.tf` does. A cluster applied before that went in accepts this manifest
    and filters nothing, so `eks apply` is what makes it take effect there.
    """
    core.log(f"restricting ingress to the agent ({config.NETWORK_POLICY_MANIFEST})")
    core.run("kubectl", "apply", "-f", config.k8s_dir() / config.NETWORK_POLICY_MANIFEST)


def deploy_collector():
    """Apply the static ClickStack collector manifest, wait for it, then show its first logs.

    Those log lines are the deploy's only window on the one thing nothing else can check: that
    the ClickHouse Cloud credentials the collector just read actually work. A collector that
    rolled out and is refusing every write says so there and nowhere else -- `eks verify` would
    otherwise be the first to notice, several minutes and one audience later.
    """
    core.log("deploying the ClickStack OTel collector")
    core.run("kubectl", "apply", "-f", config.k8s_dir() / config.COLLECTOR_MANIFEST)
    core.run(
        "kubectl",
        "-n",
        config.NS_CS,
        "rollout",
        "status",
        config.COLLECTOR_DEPLOYMENT,
        f"--timeout={COLLECTOR_ROLLOUT_TIMEOUT}",
    )
    core.log(f"ClickStack collector logs (last {config.COLLECTOR_LOG_TAIL} lines)")
    core.run(
        "kubectl",
        "-n",
        config.NS_CS,
        "logs",
        config.COLLECTOR_DEPLOYMENT,
        f"--tail={config.COLLECTOR_LOG_TAIL}",
    )


def apply_or_delete(name, namespace, data, description):
    """Create an optional Secret from `.env`, or delete it when `.env` no longer carries it.

    Deleted rather than skipped: a Secret left behind by an earlier run would go on feeding
    credentials to the collector and the agent after they were taken out of `.env`, and every
    reference to both Secrets is `optional: true` precisely so their absence is a valid
    deployment rather than a pod that will not start.
    """
    if data:
        core.log(f"creating Secret {namespace}/{name}")
        k8s.apply_manifest(k8s.secret_manifest(name, namespace, data))
        return
    core.log(f"no {description} in .env; removing Secret {namespace}/{name} if it is there")
    k8s.delete_secret(name, namespace)


def install_release(generated):
    """`helm upgrade --install` the demo from the static values and the generated ones.

    The `-f` order is the contract, and it lives here rather than in `k8s.helm_upgrade_args`:
    Helm merges maps but *replaces* lists, and both files set `components.*.envOverrides`. The
    generated document carries the static entries through (`values.env_overrides`), so
    static-then-generated keeps both halves, while the reverse order silently drops the static
    list -- the agent's collector endpoint, its `USE_VCR` and its four optional `secretKeyRef`s
    -- and nothing fails loudly. `eks check` renders from this same argv and fails on it.

    No `--set`: every value, the four image references included, comes from a file.
    """
    k8s.helm_repo_ensure()
    core.log(f"installing the OpenTelemetry demo (chart {config.CHART_VERSION})")
    core.run(*k8s.helm_upgrade_args([config.k8s_dir() / values.STATIC_VALUES, generated]))


def open_tunnel():
    """Open the tunnel to the storefront and print the three URLs. Failure is tolerated.

    `deploy.sh` spelled this `tunnel_start || true`, and the reason survives the port: by this
    point the release is installed and running, so a port already taken or a loop that has not
    answered yet is a local inconvenience, not a failed deploy. The URLs are printed either
    way, because `demo.py eks tunnel` is all it takes to make them work.

    A refusal (`SystemExit`) and a failure to spawn the detached loop at all (`OSError`) are
    both tolerated, which is the whole of what `|| true` covered. Nothing wider: a TypeError
    out of `start()` is a bug in this CLI and has no business being reported as a busy port.
    """
    port = tunnel.resolved_port()
    try:
        tunnel.start(port=port)
    except (SystemExit, OSError) as refusal:
        print(
            f"warning: the tunnel did not start ({refusal}); "
            "open it with `demo.py eks tunnel` once the port is free",
            file=sys.stderr,
        )
    base = tunnel.url(port)
    print()
    print(f"Storefront:      {base}")
    print(f"Feature flags:   {base}feature/")
    print(f"Load generator:  {base}loadgen/")
    print()
    print(
        "Check telemetry is landing with `demo.py eks verify`; "
        "reopen the tunnel with `demo.py eks tunnel`."
    )


def up():
    """Bring the demo up from idle: scale the node group, wait for the nodes, then deploy.

    The kubeconfig is written before the scale-up rather than with the rest of the cluster work
    in `deploy`: an expired session or a cluster that no longer exists then refuses in seconds,
    instead of after five minutes of nodes that are already being billed for.
    """
    # helm and docker belong to `deploy` rather than to this command, but a missing one is
    # worth knowing about before the node group starts costing money.
    core.need("aws", "docker", "tofu", "kubectl", "helm")

    aws.aws_login()
    k8s.kubeconfig()

    count = int(aws.tf_out("node_count"))
    aws.ng_scale(count)
    # By keyword: `wait_nodes_ready`'s first parameter is the node count and its second is the
    # timeout, so a positional call here would wait for 2 seconds' worth of nothing.
    k8s.wait_nodes_ready(count=count)

    # `deploy` owns the rest -- Secrets, collector, release, tunnel -- and its refusals are
    # this command's refusals.
    deploy()


def down(keep=False):
    """Return to the idle state: tunnel down, workloads removed, node group at zero.

    The control plane, the ECR images and the OpenTofu state stay, so `eks up` brings it back
    in minutes. Telemetry already written to ClickHouse Cloud is never touched.

    `--keep` skips the workload removal and only scales down. The Deployments and DaemonSets
    stay in the API with nowhere to run, and reschedule when the nodes come back, which makes
    the next `eks up` faster at the cost of a cluster whose objects all read as Pending.
    """
    # No docker: nothing here logs in to a registry.
    core.need("aws", "tofu", "kubectl", "helm")

    # Local, and a no-op when nothing is running, so it needs no session and goes first: the
    # port-forward is pointed at pods that are about to be deleted.
    tunnel.stop()

    aws.aws_login()

    if keep:
        core.log("--keep: leaving the workloads in place; they reschedule on the way up")
    else:
        k8s.kubeconfig()
        # The demo first and the collector second: the demo is what produces the telemetry, so
        # taking it down first spares the collector a burst of export failures. Only the demo
        # is a Helm release; the collector is a manifest, and its namespace goes with it.
        core.log(f"helm uninstall {config.RELEASE}")
        core.run(
            "helm",
            "uninstall",
            config.RELEASE,
            "-n",
            config.NS_DEMO,
            "--ignore-not-found",
            "--wait",
            "--timeout",
            UNINSTALL_TIMEOUT,
        )
        core.log(f"deleting namespaces {config.NS_DEMO} and {config.NS_CS}")
        core.run(
            "kubectl",
            "delete",
            "ns",
            config.NS_DEMO,
            config.NS_CS,
            "--ignore-not-found",
            f"--timeout={NAMESPACE_DELETE_TIMEOUT}",
        )

    # Last, so nothing above is racing a node group that is already going away.
    aws.ng_scale(0)

    print()
    core.log(
        "demo is down: node group at 0, control plane kept. "
        "Telemetry in ClickHouse Cloud is untouched."
    )
    print("Bring it back with: demo.py eks up")
