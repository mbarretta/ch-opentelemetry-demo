"""launcher.eks.config: the EKS target's names, paths, and the keys it reads from `.env`."""

import base64

import pytest

from launcher import core, stack
from launcher.eks import config

FULL_CLICKSTACK = {
    "CLICKHOUSE_ENDPOINT": "https://ch.test:8443",
    "CLICKHOUSE_USER": "clickstack",
    "CLICKHOUSE_PASSWORD": "sensitive-clickhouse",
    "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE": "otel",
    "OTLP_AUTH_TOKEN": "sensitive-token",
}
FULL_LANGFUSE = {
    "LANGFUSE_BASE_URL": "https://lf.test",
    "LANGFUSE_PUBLIC_KEY": "pk-test",
    "LANGFUSE_SECRET_KEY": "sensitive-lf",
}


@pytest.fixture
def redirected(tmp_path, monkeypatch):
    """A checkout and a .runtime of our own, so nobody's real `.env` is read."""
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "RUNTIME", tmp_path / ".runtime")
    return tmp_path


def test_the_state_location_is_the_one_the_live_cluster_already_uses():
    """Renaming either of these orphans the state of the running cluster."""
    assert config.STATE_BUCKET_PREFIX == "otel-demo-eks-tfstate-"
    assert config.STATE_KEY == "otel-demo-eks/terraform.tfstate"


def test_paths_are_resolved_at_call_time(redirected):
    assert config.tofu_dir() == redirected / "deploy/eks/tofu"
    assert config.k8s_dir() == redirected / "deploy/eks/k8s"
    assert config.run_dir() == redirected / ".runtime/eks"


def test_load_env_reads_the_root_env_under_the_process_environment(redirected, monkeypatch):
    (redirected / ".env").write_text("AWS_PROFILE=from-file\nEKS_TUNNEL_PORT=8080\n")
    assert config.load_env()["AWS_PROFILE"] == "from-file"
    monkeypatch.setenv("AWS_PROFILE", "from-shell")
    values = config.load_env()
    assert values["AWS_PROFILE"] == "from-shell", "the shell wins, as it does for the laptop"
    assert values["EKS_TUNNEL_PORT"] == "8080"


def test_load_clickstack_env_names_every_blank_or_missing_key_at_once():
    partial = {**FULL_CLICKSTACK, "CLICKHOUSE_PASSWORD": "", "OTLP_AUTH_TOKEN": ""}
    del partial["CLICKHOUSE_ENDPOINT"]

    with pytest.raises(SystemExit) as failure:
        config.load_clickstack_env(partial)

    message = str(failure.value)
    for key in ("CLICKHOUSE_ENDPOINT", "CLICKHOUSE_PASSWORD", "OTLP_AUTH_TOKEN"):
        assert key in message, key
    assert "CLICKHOUSE_USER" not in message, "a key that is filled in must not be reported"
    assert "create-user.sql" in message, "name the file that creates the ClickHouse user"


def test_load_clickstack_env_returns_exactly_the_five_collector_keys():
    loaded = config.load_clickstack_env({**FULL_CLICKSTACK, "SHOP_PORT": "8080"})
    assert loaded == FULL_CLICKSTACK
    assert set(loaded) == set(config.CLICKSTACK_KEYS)


def test_load_langfuse_env_is_all_or_nothing():
    assert config.load_langfuse_env({"SHOP_PORT": "8080"}) == {}, "Langfuse stays optional"

    with pytest.raises(SystemExit) as failure:
        config.load_langfuse_env({"LANGFUSE_BASE_URL": "https://lf.test"})
    message = str(failure.value)
    assert "LANGFUSE_PUBLIC_KEY" in message and "LANGFUSE_SECRET_KEY" in message

    secret = config.load_langfuse_env(FULL_LANGFUSE)
    assert set(secret) == set(config.LANGFUSE_SECRET_KEYS)
    assert secret["LANGFUSE_AUTH_HEADER"] == config.langfuse_auth_header("pk-test", "sensitive-lf")


def test_langfuse_auth_header_is_basic_base64_of_the_key_pair():
    header = config.langfuse_auth_header("pk-test", "sk-test")
    scheme, encoded = header.split(" ")
    assert scheme == "Basic"
    assert base64.b64decode(encoded).decode() == "pk-test:sk-test"


def test_the_auth_header_is_the_same_one_the_compose_stack_sends(redirected):
    """One definition of the credential on both targets, or a trace lands in only one place."""
    (redirected / ".env").write_text(
        "".join(f"{key}={value}\n" for key, value in FULL_LANGFUSE.items())
    )

    env = stack.environment()

    assert env["LANGFUSE_AUTH_HEADER"] == config.langfuse_auth_header(
        env["LANGFUSE_PUBLIC_KEY"], env["LANGFUSE_SECRET_KEY"]
    )


def test_env_example_documents_every_eks_key_with_a_comment():
    lines = (core.ROOT / ".env.example").read_text().splitlines()
    documented = {
        line.split("=", 1)[0]: lines[index - 1]
        for index, line in enumerate(lines)
        if "=" in line and not line.startswith("#")
    }
    expected = (
        "SESSION_REPLAY",
        "AWS_PROFILE",
        "AWS_REGION",
        "EKS_TUNNEL_PORT",
        *config.CLICKSTACK_KEYS,
    )
    for key in expected:
        assert key in documented, key
        assert documented[key].startswith("#"), f"{key} has no comment above it"
    values = dict(core.dotenv(core.ROOT / ".env.example"))
    assert values["SESSION_REPLAY"] == "auto"
    assert values["AWS_REGION"] == config.DEFAULT_REGION
    assert values["CLICKHOUSE_USER"] == "clickstack"
    assert values["HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE"] == "otel"
    assert values["EKS_TUNNEL_PORT"] == config.DEFAULT_TUNNEL_PORT
    for key in ("CLICKHOUSE_ENDPOINT", "CLICKHOUSE_PASSWORD", "OTLP_AUTH_TOKEN", "AWS_PROFILE"):
        assert values[key] == "", f"{key} is a secret or account-specific: it ships blank"
