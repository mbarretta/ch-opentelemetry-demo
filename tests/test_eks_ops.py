"""launcher.eks.ops: the two reports -- is telemetry landing, and where does the deployment stand.

Both commands are reports, so what is asserted here is what they say and what they never say:
the queries `verify` issues, the trace id of the turn it drives, and the fact that the ClickHouse
password reaches `httpx` in process and appears in no argv. `status` is asserted to exit 0 in
both the idle and the running state, because a demo that is switched off is not a failure.

Every process goes through `fake_sh`, and every HTTP request through a real `httpx.Client` on a
`MockTransport`: `ops._httpx()` is the one seam, so the recorded requests carry the headers and
the bodies `ops` itself produced rather than any a test handed it.
"""

import base64
import json
import os
from collections import Counter

import httpx
import pytest

from launcher import collector, core, images
from launcher.eks import aws, config, ops

REGISTRY = "111122223333.dkr.ecr.us-east-1.amazonaws.com"
CLUSTER = "otel-demo-eks"
DATABASE = "otel"
PASSWORD = "sensitive-clickhouse-password"
TAG = "58acc12afb99"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CONVERSATION = "c0ffee00-dead-beef-cafe-000000000001"
PORT = 8080

ENV = {
    "AWS_PROFILE": "demo",
    "EKS_TUNNEL_PORT": str(PORT),
    "CLICKHOUSE_ENDPOINT": "https://ch.test:8443",
    "CLICKHOUSE_USER": "clickstack",
    "CLICKHOUSE_PASSWORD": PASSWORD,
    "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE": DATABASE,
    "OTLP_AUTH_TOKEN": "sensitive-otlp-token",
}

OUTPUTS = {
    "region": {"value": "us-east-1"},
    "cluster_name": {"value": CLUSTER},
    "nodegroup_name": {"value": f"{CLUSTER}-demo"},
    "node_count": {"value": 2},
    "ecr_registry": {"value": REGISTRY},
    "ecr_repository_urls": {
        "value": {
            service: f"{REGISTRY}/ch-opentelemetry-demo/{service}" for service in core.IMAGES
        }
    },
    "scheduler_name": {"value": f"{CLUSTER}-nightly-scale-down"},
}

# The two `describe-nodegroup` reads share a prefix; the longer one wins in `fake_sh`'s answer
# table, which is how one fake shell answers both the status and the desired-size question.
NG_DESIRED = (
    f"aws eks describe-nodegroup --cluster-name {CLUSTER} --nodegroup-name {CLUSTER}-demo "
    "--query nodegroup.scalingConfig.desiredSize"
)
NG_STATUS = f"aws eks describe-nodegroup --cluster-name {CLUSTER}"
# The one read `pod_phases()` makes, spelled out because `fake_sh` answers the longest matching
# prefix: a shorter `kubectl get pods -A` would lose to the fixture's entry rather than replace it.
PHASE_READ = "kubectl get pods -A --no-headers -o custom-columns=P:.status.phase"

# What ClickHouse's PrettyCompactMonoBlock actually looks like, trimmed to one row.
TABLE = "   ┌─signal─┬─count()─┐\n   │ traces │    4271 │\n   └────────┴─────────┘"
ZERO_REPLAY = "   ┌─events─┬─sessions─┐\n   │      0 │        0 │\n   └────────┴──────────┘"

TURN = {
    "contract_version": "1",
    "conversation_id": CONVERSATION,
    "request_id": "6f1c1d64-0e2c-4a1b-9f2f-6b0a1f2c3d4e",
    "reply": "The Explorascope 60AZ is a good first telescope for the moon at $99.",
    "product_refs": [],
    "cart_changed": False,
    "trace_id": TRACE_ID,
    "feedback_enabled": True,
}

PODS = """NAME                                         READY   STATUS    RESTARTS   AGE
otel-demo-frontend-7c9d8f6b54-4xq2m          1/1     Running   0          6m
otel-demo-otel-collector-5fff8f6b97-7ggzt    1/1     Running   0          6m
otel-demo-flagd-6c7f9d4b8c-2wz4p             2/2     Running   0          6m"""


class FakeHttpx:
    """The `httpx` module as `ops._httpx()` hands it back, with every client on a fake transport.

    The real library minus the network: `Client` forwards whatever `ops` passed -- base_url,
    auth, timeout -- into a real `httpx.Client` and adds only the transport. So a recorded
    request carries the Authorization header and the body `ops` produced, which is what makes
    the credential assertion below mean anything.
    """

    HTTPError = httpx.HTTPError

    def __init__(self, handler):
        self.handler = handler
        self.requests = []

    def Client(self, **kwargs):
        def handle(request):
            self.requests.append(request)
            return self.handler(request)

        return httpx.Client(transport=httpx.MockTransport(handle), **kwargs)

    def paths(self):
        return [request.url.path for request in self.requests]

    def bodies(self, path):
        """Every request body sent to that path, decoded."""
        return [
            request.content.decode() for request in self.requests if request.url.path == path
        ]

    def sql(self):
        return self.bodies("/")


def answer_everything(request):
    """The storefront answers one turn; ClickHouse answers a one-row table for every query."""
    if request.url.path == ops.ASSISTANT_PATH:
        return httpx.Response(200, json=TURN)
    return httpx.Response(200, text=TABLE)


def recorded_text(shell):
    """Every recorded process launch as one blob: the argv plus whatever went in on stdin."""
    return "\n".join(f"{call.line} {call.stdin or ''}" for call in shell.calls)


def exits_zero(action):
    """Run a report, failing the test if it exited at all. Both commands are reports, not gates."""
    try:
        return action()
    except SystemExit as exit_status:
        raise AssertionError(f"a report exited with {exit_status.code}") from exit_status


def absent_from_ecr(shell, tag=TAG):
    """Answer `describe-images` for that tag with the exit status a missing tag produces.

    `fake_sh` answers an unlisted call with a plain success, which for a probe that reads a
    return code means "present"; an absent image has to be asked for.
    """
    for service in core.IMAGES:
        shell.reply(
            "aws ecr describe-images --repository-name "
            f"ch-opentelemetry-demo/{service} --image-ids imageTag={tag}",
            returncode=1,
        )


def write_manifest(root, published=None, tag=TAG):
    """A build manifest under the redirected `.runtime`, with an optional published section."""
    path = root / ".runtime/images/manifest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {"tag": tag, "images": {service: {"image": service} for service in core.IMAGES}}
    if published is not None:
        manifest["published"] = published
    path.write_text(json.dumps(manifest))
    return path


@pytest.fixture
def redirected(tmp_path, monkeypatch):
    """A checkout of our own, with a filled-in `.env` and nothing inherited from the shell.

    `config.load_env()` layers the process environment over `.env`, so an exported CLICKHOUSE_*
    or AWS_PROFILE on the developer's machine would otherwise decide what these tests see.
    `aws.aws_login()` then exports AWS_PROFILE and AWS_REGION into the real environment on
    purpose -- that is how kubectl's exec-auth plugin sees them -- so both are saved and put
    back here rather than left set for whatever test runs next.
    """
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "RUNTIME", tmp_path / ".runtime")
    # Every external command is faked; `need` would still look for aws, tofu, kubectl and helm
    # on the developer's PATH, where a missing OpenTofu is not this module's problem.
    monkeypatch.setattr(core, "need", lambda *commands: None)
    # The process-wide `tofu output` cache outlives a redirected core.ROOT, so it starts empty.
    monkeypatch.setattr(aws, "_OUTPUTS", None)
    # The wait for the collector to flush is real time; nothing here is exporting anything.
    monkeypatch.setattr(ops, "SETTLE_SECONDS", 0)
    (tmp_path / ".env").write_text("".join(f"{key}={value}\n" for key, value in ENV.items()))

    saved = {key: os.environ.get(key) for key in ("AWS_PROFILE", "AWS_REGION", *ENV)}
    for key in saved:
        os.environ.pop(key, None)
    yield tmp_path
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


@pytest.fixture
def cluster(redirected, fake_sh):
    """A live session, readable OpenTofu outputs, and a cluster that answers kubectl."""
    fake_sh.reply("tofu", stdout=json.dumps(OUTPUTS))
    fake_sh.reply("aws configure list-profiles", stdout="default\ndemo\n")
    fake_sh.reply(f"kubectl -n {config.NS_DEMO} get pods", stdout=PODS)
    fake_sh.reply(NG_STATUS, stdout="ACTIVE")
    fake_sh.reply(NG_DESIRED, stdout="2")
    fake_sh.reply(PHASE_READ, stdout="Running\nRunning\nPending\nRunning\n")
    return fake_sh


@pytest.fixture
def http(monkeypatch):
    """Every `httpx` client `ops` builds, on a fake transport that records what it was sent."""
    fake = FakeHttpx(answer_everything)
    monkeypatch.setattr(ops, "_httpx", lambda: fake)
    return fake


# --- verify --------------------------------------------------------------------------------


def test_verify_issues_the_bash_queries_and_the_two_the_merge_made_possible(cluster, http):
    """verify.sh's row counts, service breakdown and replay pair, plus agent and route spans."""
    exits_zero(ops.verify)

    statements = "\n".join(http.sql())
    assert len(http.sql()) == 5, http.sql()
    for table in ("otel_traces", "otel_logs", "otel_metrics_sum", "otel_metrics_gauge"):
        assert f"{DATABASE}.{table}" in statements, table
    assert "GROUP BY ServiceName" in statements, "the per-service span breakdown"
    assert f"{DATABASE}.hyperdx_sessions" in statements, "the session replay count"
    assert "uniqExact(ResourceAttributes['rum.sessionId'])" in statements
    assert f"ServiceName = '{ops.AGENT_SERVICE}'" in statements, "the agent span count"
    assert "SpanAttributes['http.route'] LIKE '/api/assistant/%'" in statements
    # The route prefix is read from the module whose OTTL statements set `http.route`, so the
    # query cannot go on matching a prefix the storefront has stopped using.
    assert ops.ASSISTANT_ROUTE_PATTERN == f"{collector.ASSISTANT_PATH}/%"
    assert ops.ASSISTANT_PATH == f"{collector.ASSISTANT_PATH}/message"
    assert statements.count(f"FORMAT {ops.OUTPUT_FORMAT}") == 5, "every report is a table"
    assert f"INTERVAL {ops.WINDOW}" in statements


def test_verify_drives_one_assistant_turn_before_it_counts_anything(cluster, http, capsys):
    """The turn is the demo's proof, and its spans have to be inside the window queried."""
    assert exits_zero(ops.verify) == TRACE_ID

    assert http.paths()[0] == ops.ASSISTANT_PATH, "the turn runs first, then the counts"
    assert http.paths().count(ops.ASSISTANT_PATH) == 1
    request = json.loads(http.bodies(ops.ASSISTANT_PATH)[0])
    assert set(request) == {
        "shop_session_id",
        "request_id",
        "message",
        "currency_code",
        "budget",
    }, "MessageRequest forbids extra keys, and a first turn names no conversation or scenario"
    assert TRACE_ID in capsys.readouterr().out, "the trace id to paste into both UIs"


def test_the_clickhouse_password_reaches_httpx_in_process_and_no_argv(cluster, http, capsys):
    exits_zero(ops.verify)

    credentials = base64.b64encode(f"{ENV['CLICKHOUSE_USER']}:{PASSWORD}".encode()).decode()
    queries = [request for request in http.requests if request.url.path == "/"]
    assert queries, "no query was issued"
    for request in queries:
        assert request.headers["authorization"] == f"Basic {credentials}"

    # The point of the port: `ps` publishes argv to every user on the machine, so the password
    # may appear in no command line and in nothing piped to one.
    assert PASSWORD not in recorded_text(cluster), cluster.lines()
    assert PASSWORD not in "\n".join(str(request.url) for request in http.requests)
    assert PASSWORD not in capsys.readouterr().out


def test_a_zero_session_replay_count_is_reported_not_failed(cluster, monkeypatch, capsys):
    """Replay needs a browser to have visited the storefront, so zero is news, not a fault."""
    replayless = FakeHttpx(
        lambda request: (
            httpx.Response(200, json=TURN)
            if request.url.path == ops.ASSISTANT_PATH
            else httpx.Response(200, text=ZERO_REPLAY)
        )
    )
    monkeypatch.setattr(ops, "_httpx", lambda: replayless)

    assert exits_zero(ops.verify) == TRACE_ID

    out = capsys.readouterr().out
    assert "session replay events and distinct sessions" in out
    assert ops.REPLAY_NOTE in out, "the zero is explained where it is printed"


def test_verify_still_counts_rows_when_the_tunnel_is_not_up(cluster, monkeypatch, capsys):
    """The SQL half says whether anything is landing at all, which is worth knowing either way."""

    def refuse_the_turn(request):
        if request.url.path == ops.ASSISTANT_PATH:
            raise httpx.ConnectError("connection refused", request=request)
        return httpx.Response(200, text=TABLE)

    down = FakeHttpx(refuse_the_turn)
    monkeypatch.setattr(ops, "_httpx", lambda: down)

    assert exits_zero(ops.verify) is None, "no trace id, and no exception either"

    assert len(down.sql()) == 5, "every report still ran"
    assert "the assistant turn did not complete" in capsys.readouterr().err


def test_verify_reports_the_collector_pods_and_a_tail_of_the_logs(cluster, http, capsys):
    exits_zero(ops.verify)

    lines = cluster.lines()
    assert f"kubectl -n {config.NS_CS} get pods" in lines
    assert (
        f"kubectl -n {config.NS_CS} logs {config.COLLECTOR_DEPLOYMENT} "
        f"--tail={config.COLLECTOR_LOG_TAIL}" in lines
    ), "the Deployment, not a pod name that changes on every redeploy"
    out = capsys.readouterr().out
    assert "otel-demo-otel-collector-5fff8f6b97-7ggzt" in out, "the gateway collector's pod"
    assert "otel-demo-flagd-6c7f9d4b8c-2wz4p" not in out, "and only it, out of some thirty pods"


def test_the_pod_filter_keeps_the_header_and_says_when_there_is_no_collector():
    filtered = ops.collector_pods(PODS)

    assert filtered.splitlines()[0].startswith("NAME"), "a table needs its header"
    assert len(filtered.splitlines()) == 2
    assert ops.COLLECTOR_POD in ops.collector_pods("")
    assert config.NS_DEMO in ops.collector_pods(PODS.splitlines()[0])


def test_a_database_name_that_is_not_an_identifier_is_refused():
    """The name is interpolated into every statement, so a typo must not become a statement."""
    assert ops.require_identifier("otel_v2") == "otel_v2"
    for name in ("", "otel; DROP TABLE x", "otel.traces", "2otel"):
        with pytest.raises(SystemExit, match="not a plain database name"):
            ops.require_identifier(name)


# --- status --------------------------------------------------------------------------------


def test_status_exits_zero_with_the_node_group_scaled_to_zero(cluster, capsys):
    """Idle is the overnight state and the cheap one; reporting it is not a failure."""
    cluster.reply(NG_DESIRED, stdout="0")
    write_manifest(core.ROOT, published={})

    exits_zero(ops.status)

    assert "kubectl get nodes" not in cluster.lines(), "no node list for a cluster with no nodes"
    out = capsys.readouterr().out
    assert "desiredSize=0 status=ACTIVE" in out
    assert "scaled to zero" in out
    assert "tunnel: not running" in out


def test_status_exits_zero_with_the_stack_up(cluster, capsys):
    write_manifest(
        core.ROOT,
        published={
            service: {
                "repository": f"{REGISTRY}/ch-opentelemetry-demo/{service}",
                "tag": TAG,
                "digest": "sha256:" + "0" * 64,
            }
            for service in core.IMAGES
        },
    )

    exits_zero(ops.status)

    lines = cluster.lines()
    assert "kubectl get nodes" in lines, "desiredSize is 2, so the nodes are worth listing"
    assert f"helm list -n {config.NS_DEMO}" in lines
    out = capsys.readouterr().out
    assert "desiredSize=2 status=ACTIVE" in out
    for service in core.IMAGES:
        assert f"{service}: ch-opentelemetry-demo/{service}:{TAG} present" in out


def test_pod_phases_are_counted_rather_than_piped_through_uniq(cluster):
    phases = ops.pod_phases()

    assert isinstance(phases, Counter)
    assert phases == Counter({"Running": 3, "Pending": 1})
    assert ops.pod_phase_report() == "    3 Running\n    1 Pending"


def test_pod_phases_distinguish_an_empty_cluster_from_an_unreadable_one(cluster):
    cluster.reply(PHASE_READ, stdout="")
    assert ops.pod_phases() == Counter()
    assert ops.pod_phase_report() == "    no pods"

    cluster.reply(PHASE_READ, stderr="connection refused", returncode=1)
    assert ops.pod_phases() is None
    assert "could not list pods" in ops.pod_phase_report()


def test_status_tells_no_manifest_apart_from_built_but_not_published(cluster, capsys):
    """Different answers because the fix is different: `build` first, or `publish` next."""
    exits_zero(ops.status)
    nothing_built = capsys.readouterr().out
    assert "no build manifest" in nothing_built
    assert "demo.py build" in nothing_built
    assert "aws ecr describe-images" not in "\n".join(cluster.lines()), "nothing to look up yet"

    write_manifest(core.ROOT, published={})
    absent_from_ecr(cluster)
    exits_zero(ops.status)

    built = capsys.readouterr().out
    for service in core.IMAGES:
        assert f"{service}: ch-opentelemetry-demo/{service}:{TAG} absent" in built
        assert "built but not published" in built
    assert "demo.py publish" in built


def test_a_manifest_without_a_tag_is_named_rather_than_looked_up(cluster, capsys):
    """`imageTag=None` would come back absent for all four and name the wrong fix."""
    write_manifest(core.ROOT, published={}, tag=None)

    exits_zero(ops.status)

    assert "records no tag" in capsys.readouterr().out
    assert "aws ecr describe-images" not in "\n".join(cluster.lines())


def test_status_reports_a_publish_the_build_has_moved_on_from(cluster, capsys):
    stale = "0000deadbeef"
    write_manifest(
        core.ROOT,
        published={
            service: {
                "repository": f"{REGISTRY}/ch-opentelemetry-demo/{service}",
                "tag": stale,
                "digest": "sha256:" + "0" * 64,
            }
            for service in core.IMAGES
        },
    )
    # Present at the tag it was pushed at, absent at the tag the current manifest records.
    absent_from_ecr(cluster)

    exits_zero(ops.status)

    out = capsys.readouterr().out
    for service in core.IMAGES:
        assert f"{service}: ch-opentelemetry-demo/{service}:{TAG} absent" in out
    assert f"last published at {stale}" in out


def test_status_reports_the_nightly_schedule_and_the_identity(cluster):
    write_manifest(core.ROOT, published={})

    exits_zero(ops.status)

    lines = cluster.lines()
    assert any(line.startswith("aws sts get-caller-identity --query") for line in lines)
    schedule = OUTPUTS["scheduler_name"]["value"]
    assert any(
        line.startswith(f"aws scheduler get-schedule --name {schedule}") for line in lines
    ), lines


def test_status_survives_a_release_and_a_pod_list_that_are_not_there(cluster, capsys):
    """A release nobody installed and pods that cannot be listed are answers, not faults.

    Only the read-only reports are refused here. A failing `update-kubeconfig` is the other
    kind of failure -- no cluster, or no session -- and `status` is meant to exit non-zero on
    that rather than report an empty cluster.
    """
    write_manifest(core.ROOT, published={})
    absent_from_ecr(cluster)
    refused = "the server could not find the requested resource"
    for read in ("kubectl get nodes", PHASE_READ, f"helm list -n {config.NS_DEMO}"):
        cluster.reply(read, stderr=refused, returncode=1)

    exits_zero(ops.status)

    assert "could not list pods" in capsys.readouterr().out


def test_the_manifest_path_is_the_one_build_writes(redirected):
    """The report reads `images.read_manifest()`, so a moved manifest cannot go unnoticed."""
    assert write_manifest(redirected, published={}) == images.manifest_path()
