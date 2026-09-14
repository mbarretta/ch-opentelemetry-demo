"""launcher.eks.k8s: kubeconfig, node readiness, Secrets on stdin, Helm, and the flagd file.

The point of most of these assertions is the argv: what reaches a command line is visible to
every local user through `ps`, so the Secret values have to be somewhere else (stdin), and the
`helm upgrade` argv is the deployment contract the static and generated values files hang off.
"""

import base64
import json

import pytest

from launcher import core
from launcher.eks import aws, config, k8s

OUTPUTS = {
    "region": {"value": "us-east-1"},
    "cluster_name": {"value": "otel-demo-eks"},
    "nodegroup_name": {"value": "otel-demo-eks-demo"},
    "node_count": {"value": 2},
}

# Every value here is a secret the tests then hunt for in the recorded argv.
CLICKSTACK = {
    "CLICKHOUSE_ENDPOINT": "https://ch.test:8443",
    "CLICKHOUSE_USER": "clickstack",
    "CLICKHOUSE_PASSWORD": "sensitive-clickhouse-password",
    "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE": "otel",
    "OTLP_AUTH_TOKEN": "sensitive-otlp-token",
}

FLAG_FILE = "/app/data/demo.flagd.json"


@pytest.fixture
def outputs(fake_sh, monkeypatch):
    """A fake shell with the OpenTofu outputs answered and the output cache emptied."""
    monkeypatch.setattr(aws, "_OUTPUTS", None)
    fake_sh.reply("tofu", stdout=json.dumps(OUTPUTS))
    return fake_sh


# --- the cluster connection ----------------------------------------------------------------


def test_kubeconfig_names_the_cluster_and_selects_its_context(outputs, monkeypatch):
    monkeypatch.setenv("AWS_PROFILE", "demo")

    k8s.kubeconfig()

    assert outputs.lines()[-2:] == [
        "aws eks update-kubeconfig --region us-east-1 --name otel-demo-eks "
        "--alias otel-demo-eks --profile demo",
        "kubectl config use-context otel-demo-eks",
    ], "the profile is embedded in the exec-auth entry, so plain kubectl works afterwards"


def test_kubeconfig_leaves_the_profile_flag_off_when_there_is_no_profile(outputs, monkeypatch):
    monkeypatch.delenv("AWS_PROFILE", raising=False)

    k8s.kubeconfig()

    assert "--profile" not in outputs.calls[-2].argv


def test_wait_nodes_ready_counts_ready_nodes_then_waits_for_coredns(outputs):
    outputs.reply(
        "kubectl get nodes",
        stdout=(
            "ip-10-0-1-1   Ready                      <none>   1m   v1.33.0\n"
            "ip-10-0-1-2   Ready                      <none>   1m   v1.33.0\n"
            "ip-10-0-1-3   NotReady                   <none>   1m   v1.33.0\n"
        ),
    )

    k8s.wait_nodes_ready()

    assert outputs.lines() == [
        f"tofu -chdir={config.tofu_dir()} output -json",
        "kubectl get nodes --no-headers",
        "kubectl -n kube-system rollout status deploy/coredns --timeout=300s",
    ], "the node count comes from the outputs when the caller does not name one"


def test_a_cordoned_node_does_not_count_as_ready(outputs):
    outputs.reply(
        "kubectl get nodes",
        stdout="ip-10-0-1-1   Ready,SchedulingDisabled   <none>   1m   v1.33.0\n",
    )

    # timeout=0 makes the first probe also the last one, which is the deadline branch without
    # ten minutes of real waiting.
    with pytest.raises(SystemExit) as failure:
        k8s.wait_nodes_ready(1, timeout=0)

    assert "0 of 1" in str(failure.value)


# --- namespaces, Secrets, manifests --------------------------------------------------------


def test_ensure_namespace_applies_a_manifest_so_a_second_call_is_not_an_error(fake_sh):
    k8s.ensure_namespace(config.NS_CS)
    k8s.ensure_namespace(config.NS_CS)

    assert fake_sh.lines() == ["kubectl apply -f -", "kubectl apply -f -"]
    assert json.loads(fake_sh.last.stdin) == {
        "apiVersion": "v1",
        "kind": "Namespace",
        "metadata": {"name": "clickstack"},
    }
    assert not [line for line in fake_sh.lines() if "create" in line], (
        "`create namespace` fails the second time; apply is the idempotent spelling"
    )


def test_secret_manifest_is_an_opaque_secret_with_base64_values_in_its_namespace():
    manifest = k8s.secret_manifest(config.SECRET_CLICKSTACK, config.NS_CS, CLICKSTACK)

    assert manifest["apiVersion"] == "v1"
    assert manifest["kind"] == "Secret"
    assert manifest["type"] == "Opaque"
    assert manifest["metadata"] == {"name": "clickstack-credentials", "namespace": "clickstack"}
    assert set(manifest["data"]) == set(CLICKSTACK)
    for key, value in CLICKSTACK.items():
        assert base64.b64decode(manifest["data"][key]).decode() == value
    assert not any(value in json.dumps(manifest["data"]) for value in CLICKSTACK.values()), (
        "`data` holds base64, not the values themselves (`stringData` would hold those)"
    )


def test_a_single_key_secret_keeps_the_value_exactly_as_given():
    manifest = k8s.secret_manifest(
        config.SECRET_OTLP_TOKEN, config.NS_DEMO, {"OTLP_AUTH_TOKEN": "sensitive-otlp-token"}
    )

    assert base64.b64decode(manifest["data"]["OTLP_AUTH_TOKEN"]) == b"sensitive-otlp-token", (
        "no trailing newline: the stored token is exactly what .env holds"
    )


def test_apply_manifest_pipes_the_document_to_kubectl_on_stdin(fake_sh, tmp_path, monkeypatch):
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "RUNTIME", tmp_path / ".runtime")
    manifest = k8s.secret_manifest(config.SECRET_CLICKSTACK, config.NS_CS, CLICKSTACK)

    k8s.apply_manifest(manifest, namespace=config.NS_CS)

    assert fake_sh.last.argv == ["kubectl", "-n", "clickstack", "apply", "-f", "-"]
    assert json.loads(fake_sh.last.stdin)["metadata"]["name"] == "clickstack-credentials"
    assert list(tmp_path.rglob("*.yaml")) == [], "nothing with a credential in it is written out"


def test_no_secret_value_reaches_argv_anywhere_in_the_secret_path(fake_sh):
    """The whole reason manifests are built in Python: `--from-literal` puts them in `ps`."""
    langfuse = {
        "LANGFUSE_BASE_URL": "https://lf.test",
        "LANGFUSE_PUBLIC_KEY": "pk-test",
        "LANGFUSE_SECRET_KEY": "sensitive-langfuse-secret",
        "LANGFUSE_AUTH_HEADER": config.langfuse_auth_header("pk-test", "sensitive-langfuse-secret"),
    }
    for name, namespace, data in (
        (config.SECRET_CLICKSTACK, config.NS_CS, CLICKSTACK),
        (config.SECRET_OTLP_TOKEN, config.NS_DEMO, {"OTLP_AUTH_TOKEN": "sensitive-otlp-token"}),
        (config.SECRET_LANGFUSE, config.NS_DEMO, langfuse),
        (config.SECRET_LLM, config.NS_DEMO, {"API_KEY": "sensitive-api-key"}),
    ):
        k8s.apply_manifest(k8s.secret_manifest(name, namespace, data), namespace=namespace)

    secrets = [
        CLICKSTACK["CLICKHOUSE_PASSWORD"],
        "sensitive-otlp-token",
        "sensitive-langfuse-secret",
        langfuse["LANGFUSE_AUTH_HEADER"],
        "sensitive-api-key",
    ]
    for call in fake_sh.calls:
        for secret in secrets:
            assert not any(secret in argument for argument in call.argv), (
                f"a secret value reached argv: {call.line}"
            )
    assert all(call.stdin for call in fake_sh.calls), "every one of them went in on stdin"


def test_delete_secret_tolerates_a_secret_that_was_never_created(fake_sh):
    k8s.delete_secret(config.SECRET_LANGFUSE, config.NS_DEMO)

    assert fake_sh.last.argv == [
        "kubectl",
        "-n",
        "otel-demo",
        "delete",
        "secret",
        "langfuse-credentials",
        "--ignore-not-found",
    ], "the deploy path deletes unconditionally, so absence must not be a failure"


# --- helm ----------------------------------------------------------------------------------


def test_helm_repo_ensure_is_a_no_op_on_a_re_run(fake_sh):
    k8s.helm_repo_ensure()

    assert fake_sh.lines() == [
        f"helm repo add {config.HELM_REPO_NAME} {config.HELM_REPO_URL} --force-update",
        f"helm repo update {config.HELM_REPO_NAME}",
    ], "--force-update replaces `repository name already exists` with a repair"


def test_helm_upgrade_args_is_the_exact_argv_the_deploy_mapping_records():
    args = k8s.helm_upgrade_args(
        [config.k8s_dir() / "demo-values.yaml", config.run_dir() / "values.generated.yaml"]
    )

    assert args == [
        "helm",
        "upgrade",
        "--install",
        "otel-demo",
        "open-telemetry/opentelemetry-demo",
        "--version",
        "0.41.0",
        "-n",
        "otel-demo",
        "-f",
        str(config.k8s_dir() / "demo-values.yaml"),
        "-f",
        str(config.run_dir() / "values.generated.yaml"),
        "--wait",
        "--timeout",
        "20m",
    ]


def test_the_static_values_file_stays_ahead_of_the_generated_one():
    args = k8s.helm_upgrade_args(["demo-values.yaml", "values.generated.yaml"])
    flags = [args[index + 1] for index, argument in enumerate(args) if argument == "-f"]

    assert flags == ["demo-values.yaml", "values.generated.yaml"], (
        "later -f wins in helm, so the generated image and collector values must come second"
    )
    assert "--set" not in args, "every value is in a file; nothing is spliced onto the argv"


# --- the flagd file ------------------------------------------------------------------------


def test_exec_read_goes_through_flagd_ui_because_flagd_has_no_shell(fake_sh):
    fake_sh.reply("kubectl -n otel-demo exec", stdout='{"flags": {}}')

    assert k8s.exec_read(FLAG_FILE) == '{"flags": {}}'
    assert fake_sh.last.argv == [
        "kubectl",
        "-n",
        "otel-demo",
        "exec",
        "deploy/flagd",
        "-c",
        "flagd-ui",
        "--",
        "cat",
        FLAG_FILE,
    ]


def test_exec_write_swaps_the_inode_so_flagds_fsnotify_watch_fires_once(fake_sh):
    k8s.exec_write(FLAG_FILE, '{"flags": {"x": 1}}')

    assert fake_sh.last.argv == [
        "kubectl",
        "-n",
        "otel-demo",
        "exec",
        "-i",
        "deploy/flagd",
        "-c",
        "flagd-ui",
        "--",
        "sh",
        "-c",
        f"cat > {FLAG_FILE}.tmp && mv {FLAG_FILE}.tmp {FLAG_FILE}",
    ]
    assert fake_sh.last.stdin == '{"flags": {"x": 1}}', (
        "the new file goes in on stdin, so no flag content is spliced into the shell command"
    )
