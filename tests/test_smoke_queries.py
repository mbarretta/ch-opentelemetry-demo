"""`scripts/smoke.py --target eks`: the trace it demands, and the one query it asks for it.

Offline throughout. The turn itself is not driven here -- `native_turn` needs a storefront and
an OTLP endpoint on the other end of a tunnel -- so it is replaced by a recorder that reports
the URL it was handed. Everything around it is asserted: the target flag and what it dispatches
to, the statement issued to ClickHouse, the four properties a trace has to have before the run
passes, the poll's budget, the Langfuse read and its deliberate absence, and the fact that
neither credential reaches a process or the console.

Both HTTP seams are patched separately because the two clients are built in two modules:
ClickHouse's in `launcher.eks.ops` (reused rather than reimplemented) and Langfuse's in
`scripts.smoke`.

Two checks do start a process, and only these two: `__debug__` is a compile-time constant, so
what `python -O` does to the guards can only be seen from a child interpreter started that way.
They reach no backend -- both refuse before anything is driven.
"""

import base64
import json
import sys

import httpx
import pytest

import scripts.smoke as smoke
from launcher import cli, core
from launcher.eks import config, ops, tunnel

DATABASE = "otel"
PASSWORD = "sensitive-clickhouse-password"
SECRET_KEY = "sk-lf-sensitive-secret"
PUBLIC_KEY = "pk-lf-public"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
CONVERSATION = "c0ffee00-dead-beef-cafe-000000000001"
SHOP_SESSION = "7f3a1b2c-0000-4000-8000-000000000002"
PARENT = "aaaa000000000001"
PORT = 8080

ENV = {
    "EKS_TUNNEL_PORT": str(PORT),
    "CLICKHOUSE_ENDPOINT": "https://ch.test:8443",
    "CLICKHOUSE_USER": "clickstack",
    "CLICKHOUSE_PASSWORD": PASSWORD,
    "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE": DATABASE,
    "OTLP_AUTH_TOKEN": "sensitive-otlp-token",
    "LANGFUSE_BASE_URL": "https://cloud.langfuse.test",
    "LANGFUSE_PUBLIC_KEY": PUBLIC_KEY,
    "LANGFUSE_SECRET_KEY": SECRET_KEY,
}

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

LANGFUSE_TRACE = {"id": TRACE_ID, "name": "concierge.turn", "sessionId": CONVERSATION}


def span(service, name, span_id, parent="", route=""):
    """One `otel_traces` row as the query's aliases hand it back."""
    return {
        "service": service,
        "name": name,
        "spanId": span_id,
        "parentSpanId": parent,
        "route": route,
    }


# One healthy turn as ClickHouse holds it: the synthetic browser root, Envoy, the storefront's
# server span with the assistant route on it, the agent's turn, and the catalog tool call.
SPANS = [
    span("frontend-web", "assistant.turn", PARENT),
    span("frontend-proxy", "ingress POST", "bbbb000000000002", PARENT),
    span(
        "frontend",
        "POST /api/assistant/message",
        "cccc000000000003",
        "bbbb000000000002",
        route="/api/assistant/message",
    ),
    span("agent", "concierge.turn", "dddd000000000004", "cccc000000000003"),
    span("product-catalog", "oteldemo.ProductCatalogService/GetProduct", "eeee000000000005",
         "dddd000000000004"),
]


def rows(spans):
    """Those rows as ClickHouse's JSONEachRow answers them."""
    return "".join(json.dumps(row) + "\n" for row in spans)


def without(service):
    """The healthy span set minus one service, which is how each property is made to fail."""
    return [row for row in SPANS if row["service"] != service]


class FakeHttpx:
    """The `httpx` module with every client on a recording transport.

    A real `httpx.Client` is built with whatever the code under test passed -- base_url, auth,
    timeout -- and only its transport is replaced, so a recorded request carries the
    Authorization header the code produced rather than one a test wrote by hand. That is what
    makes the credential assertions below mean anything.
    """

    # The one attribute of the real module the code under test reads, and it must be the real
    # class: `smoke` catches `httpx.HTTPError` around both backends' requests.
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

    def bodies(self):
        return [request.content.decode() for request in self.requests]


class Answers:
    """A handler that answers each call from a list, repeating the last answer forever.

    An answer that is an exception is raised instead of returned, which is how a backend that
    does not answer at all -- a refused connection, a DNS failure -- is put in front of the poll.
    """

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = 0

    def __call__(self, request):
        answer = self.answers[min(self.calls, len(self.answers) - 1)]
        self.calls += 1
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def redirect_env():
    """The `.env` the shared `redirected` fixture writes for these tests."""
    return ENV


@pytest.fixture
def redirected(redirected, monkeypatch):
    """The shared checkout with its `.runtime` made and the script's own copy of the path.

    `smoke.RUNTIME` is patched as well as `core.RUNTIME`: the script binds the name at import
    on purpose (it patches nothing when it runs for real), so the module attribute is the seam.
    """
    monkeypatch.setattr(smoke, "RUNTIME", redirected / ".runtime")
    (redirected / ".runtime").mkdir()
    return redirected


@pytest.fixture
def clickhouse(monkeypatch):
    """ClickHouse on a recording transport, answering the healthy span set by default."""
    fake = FakeHttpx(Answers(httpx.Response(200, text=rows(SPANS))))
    monkeypatch.setattr(ops, "_httpx", lambda: fake)
    return fake


@pytest.fixture
def langfuse(monkeypatch):
    """Langfuse on a recording transport, answering the trace by default."""
    fake = FakeHttpx(Answers(httpx.Response(200, json=LANGFUSE_TRACE)))
    monkeypatch.setattr(smoke, "httpx", fake)
    return fake


@pytest.fixture
def unhurried(monkeypatch):
    """The poll's waiting, recorded instead of slept."""
    slept = []
    monkeypatch.setattr(smoke.time, "sleep", slept.append)
    return slept


@pytest.fixture
def tunnelled(monkeypatch):
    """A tunnel that answers, and a turn recorded instead of driven. Returns the recorded URLs."""
    monkeypatch.setattr(tunnel, "probe", lambda port, **kwargs: True)
    driven = []

    def native_turn(shop_url):
        driven.append(shop_url)
        return TURN, SHOP_SESSION, PARENT

    monkeypatch.setattr(smoke, "native_turn", native_turn)
    return driven


# --- the target flag -----------------------------------------------------------------------


def test_the_target_flag_defaults_to_local_and_offers_the_cli_targets():
    """No flag is the laptop, as it was before `--target` existed."""
    assert smoke.parse_args([]).target == "local"
    assert smoke.parse_args(["--target", "local"]).target == "local"
    assert smoke.parse_args(["--target", "eks"]).target == "eks"
    # The same two names `demo.py scenario --target` takes, from the same tuple.
    assert smoke.TARGETS == cli.TARGETS == ("local", "eks")
    with pytest.raises(SystemExit):
        smoke.parse_args(["--target", "minikube"])


def test_main_runs_the_laptop_smoke_unless_the_target_is_eks(monkeypatch):
    """The legacy `/prompt` scenarios live in the local path, and only the local path."""
    run = []
    monkeypatch.setattr(smoke, "smoke_local", lambda: run.append("local"))
    monkeypatch.setattr(smoke, "smoke_eks", lambda: run.append("eks"))

    smoke.main([])
    smoke.main(["--target", "local"])
    smoke.main(["--target", "eks"])

    assert run == ["local", "local", "eks"]


# --- the ClickHouse query ------------------------------------------------------------------


def test_the_query_reads_otel_traces_by_trace_id_in_the_configured_database(clickhouse):
    statement = smoke.eks_span_sql(DATABASE, TRACE_ID)

    assert f"FROM {DATABASE}.otel_traces" in statement
    assert f"TraceId = '{TRACE_ID}'" in statement
    assert f"INTERVAL {ops.WINDOW}" in statement, "the window `eks verify` reports over"
    # Aliased to the keys the capture-file helpers use, so `orphans()` reads both the same way.
    for alias in ("AS service", "AS name", "AS spanId", "AS parentSpanId", "AS route"):
        assert alias in statement, alias
    assert "SpanAttributes['http.route'] AS route" in statement

    with ops.clickhouse_client({**ENV}) as client:
        assert smoke.eks_spans(client, DATABASE, TRACE_ID) == SPANS
    body = clickhouse.bodies()[0]
    assert body.endswith("FORMAT JSONEachRow"), "a gate reads rows, it does not print a table"
    assert "\n    " not in body, "the statement is trimmed, as `eks verify`'s are"


def test_a_trace_id_that_is_not_thirty_two_hex_digits_never_reaches_the_statement():
    """The id is interpolated into a statement and a URL, so its shape is insisted on."""
    assert smoke.require_trace_id(TRACE_ID) == TRACE_ID
    for bad in (None, "", "4bf92f35", TRACE_ID.upper(), f"{TRACE_ID}' OR 1=1 --", TRACE_ID + "0"):
        with pytest.raises(SystemExit, match="not a trace id"):
            smoke.eks_span_sql(DATABASE, bad)


def optimised(program):
    """`program` in a child interpreter with asserts compiled out, the way `python -O` runs it.

    A real child rather than a patched flag: `__debug__` is a compile-time constant, so whether
    a guard survives optimisation can only be seen from an interpreter that was started that
    way. The first line of every program is the control -- it disappears under -O, so a child
    that reports it was not optimised after all and everything asserted about it means nothing.
    """
    finished = core.run(
        sys.executable,
        "-O",
        "-c",
        f"import sys; sys.path.insert(0, {str(core.ROOT)!r})\n"
        "assert False, 'the child interpreter still has asserts enabled'\n" + program,
        check=False,
        stdout=core.PIPE,
        stderr=core.PIPE,
        text=True,
    )
    assert "still has asserts enabled" not in finished.stderr, finished.stderr
    return finished


def test_the_trace_id_guard_still_refuses_with_asserts_compiled_out():
    """The id crosses a trust boundary, so `python -O` may not be what turns the guard off.

    It arrives in the storefront's JSON from inside the cluster and is interpolated into a
    statement ClickHouse Cloud runs as the collector's write user, so an `assert` -- which the
    interpreter is free to compile out -- was the wrong shape for it.
    """
    refused = optimised(
        "import scripts.smoke as smoke\n"
        "print(smoke.eks_span_sql('otel', \"' OR 1=1 --\"))\n"
    )

    assert refused.returncode != 0, refused.stdout
    assert "not a trace id" in refused.stderr
    assert "otel_traces" not in refused.stdout, "the statement was never built"

    passed = optimised(
        f"import scripts.smoke as smoke\nprint(smoke.require_trace_id({TRACE_ID!r}))\n"
    )

    assert passed.returncode == 0, passed.stderr
    assert TRACE_ID in passed.stdout, "a real id still goes through"


def test_the_run_refuses_to_start_when_asserts_are_compiled_out():
    """Every other check in the script is an assert, so an optimised run would pass vacuously."""
    refused = optimised("import scripts.smoke as smoke\nsmoke.main(['--target', 'eks'])\n")

    assert refused.returncode != 0
    assert "will not run with asserts compiled out" in refused.stderr
    assert "polling" not in refused.stdout, "it refuses before it drives a turn or a query"


def test_a_missing_clickstack_credential_is_reported_by_the_config_helper(redirected):
    """The five keys are read through `config.load_clickstack_env`, message and all."""
    (redirected / ".env").write_text("EKS_TUNNEL_PORT=8080\n")

    with pytest.raises(SystemExit, match="blank or missing in .env: CLICKHOUSE_ENDPOINT"):
        smoke.smoke_eks()


# --- the four properties -------------------------------------------------------------------


def test_a_complete_trace_satisfies_every_property():
    assert smoke.eks_trace_failures(SPANS, PARENT) == []


@pytest.mark.parametrize(
    "spans, expected",
    [
        (without("product-catalog"), "no spans from product-catalog"),
        (without("agent"), "no spans from agent"),
        (SPANS + [span("agent", "concierge.turn", "dddd000000000006", "cccc000000000003")],
         "2 concierge.turn spans, expected exactly one"),
        (without("frontend-proxy"), "spans whose parent is not in the trace"),
        ([{**row, "route": ""} for row in SPANS],
         "no frontend span carries http.route = /api/assistant/message"),
        ([{**row, "service": "frontend-proxy"} if row["route"] else row for row in SPANS],
         "no frontend span carries http.route = /api/assistant/message"),
    ],
)
def test_each_property_fails_on_its_own(spans, expected):
    """Service superset, one turn, no orphans, and the route: each one alone fails the run."""
    failures = smoke.eks_trace_failures(spans, PARENT)

    assert any(expected in failure for failure in failures), failures


def test_the_browser_parent_has_to_be_the_trace_it_reported():
    """The turn's synthetic root is named by span id, not merely by its service."""
    assert "synthetic browser parent" in " ".join(
        smoke.eks_trace_failures(SPANS, "ffff000000000009")
    )


# --- the poll ------------------------------------------------------------------------------


def test_the_poll_waits_for_the_collector_and_returns_as_soon_as_the_trace_is_whole(
    clickhouse, unhurried, capsys
):
    """The gateway batches, ClickStack batches again, and ClickHouse inserts in parts."""
    assert smoke.EKS_POLL_SECONDS == 60, "the budget the acceptance criteria name"
    clickhouse.handler = Answers(
        httpx.Response(200, text=""),
        httpx.Response(200, text=rows(without("product-catalog"))),
        httpx.Response(200, text=rows(SPANS)),
    )

    with ops.clickhouse_client({**ENV}) as client:
        assert smoke.check_eks_trace(client, DATABASE, TRACE_ID, PARENT) == SPANS

    assert clickhouse.handler.calls == 3, "it stops at the first whole answer"
    assert unhurried == [smoke.EKS_POLL_INTERVAL] * 2
    assert "product-catalog" in capsys.readouterr().out, "the services it did correlate"


def test_the_poll_gives_up_after_its_budget_and_says_what_was_missing(
    clickhouse, unhurried, monkeypatch
):
    monkeypatch.setattr(smoke, "EKS_POLL_SECONDS", 0)
    clickhouse.handler = Answers(httpx.Response(200, text=rows(without("agent"))))

    with ops.clickhouse_client({**ENV}) as client:
        with pytest.raises(AssertionError) as failure:
            smoke.check_eks_trace(client, DATABASE, TRACE_ID, PARENT)

    message = str(failure.value)
    assert TRACE_ID in message and f"{DATABASE}.otel_traces" in message
    assert "no spans from agent" in message
    assert unhurried == [], "the deadline ends the poll instead of one more wait"


def test_a_clickhouse_that_is_not_answering_yet_is_waited_out_not_raised(
    clickhouse, unhurried
):
    """A Cloud service that has idled answers the first query after it wakes with a 503."""
    clickhouse.handler = Answers(
        httpx.Response(503, text="Service Unavailable"),
        httpx.ConnectError("connection reset"),
        httpx.Response(200, text=rows(SPANS)),
    )

    with ops.clickhouse_client({**ENV}) as client:
        assert smoke.check_eks_trace(client, DATABASE, TRACE_ID, PARENT) == SPANS

    assert unhurried == [smoke.EKS_POLL_INTERVAL] * 2


def test_a_clickhouse_that_never_answers_fails_the_run_saying_so(
    clickhouse, unhurried, monkeypatch
):
    monkeypatch.setattr(smoke, "EKS_POLL_SECONDS", 0)
    clickhouse.handler = Answers(httpx.Response(503, text="Service Unavailable"))

    with ops.clickhouse_client({**ENV}) as client:
        with pytest.raises(AssertionError, match="ClickHouse did not answer the query"):
            smoke.check_eks_trace(client, DATABASE, TRACE_ID, PARENT)


# --- Langfuse ------------------------------------------------------------------------------


def test_langfuse_is_read_by_trace_id_with_the_key_pair_in_a_header(redirected, langfuse):
    configured = config.load_langfuse_env()

    assert smoke.check_langfuse_trace(TRACE_ID, configured) == LANGFUSE_TRACE

    assert langfuse.paths() == [f"{smoke.LANGFUSE_TRACE_PATH}/{TRACE_ID}"]
    request = langfuse.requests[0]
    assert str(request.url).startswith(ENV["LANGFUSE_BASE_URL"])
    credentials = base64.b64encode(f"{PUBLIC_KEY}:{SECRET_KEY}".encode()).decode()
    assert request.headers["authorization"] == f"Basic {credentials}"
    # The same credentials `config.langfuse_auth_header` derives for the collector.
    assert request.headers["authorization"] == config.langfuse_auth_header(
        PUBLIC_KEY, SECRET_KEY
    )


def test_langfuse_is_polled_while_it_is_still_ingesting(redirected, langfuse, unhurried):
    langfuse.handler = Answers(
        httpx.Response(404, json={"message": "Trace not found"}),
        httpx.Response(500, text="upstream error"),
        httpx.Response(200, json=LANGFUSE_TRACE),
    )

    assert smoke.check_langfuse_trace(TRACE_ID, config.load_langfuse_env()) == LANGFUSE_TRACE

    assert len(unhurried) == 2


def test_a_langfuse_that_does_not_answer_at_all_is_a_failure_not_a_traceback(
    redirected, langfuse, unhurried
):
    """An unreachable base URL reads as the same kind of failure a 500 does."""
    langfuse.handler = Answers(
        httpx.ConnectError("nodename nor servname provided"),
        httpx.Response(200, json=LANGFUSE_TRACE),
    )

    assert smoke.check_langfuse_trace(TRACE_ID, config.load_langfuse_env()) == LANGFUSE_TRACE

    assert unhurried == [smoke.EKS_POLL_INTERVAL]


def test_langfuse_failing_for_the_whole_budget_fails_the_run(
    redirected, langfuse, unhurried, monkeypatch
):
    monkeypatch.setattr(smoke, "EKS_POLL_SECONDS", 0)
    langfuse.handler = Answers(httpx.Response(404, json={"message": "Trace not found"}))

    with pytest.raises(AssertionError, match="Langfuse has no trace with that id"):
        smoke.check_langfuse_trace(TRACE_ID, config.load_langfuse_env())


def test_langfuse_is_skipped_rather_than_failed_when_it_is_not_configured(langfuse, capsys):
    """Langfuse is optional on both targets; opting out is not a failed check."""
    assert smoke.check_langfuse_trace(TRACE_ID, {}) is None

    assert langfuse.requests == [], "no read, and nothing to authenticate it with"
    assert "Langfuse is not configured" in capsys.readouterr().out


def test_a_half_configured_langfuse_is_refused_by_the_config_helper(redirected):
    """`load_langfuse_env` is the one place that rule lives; smoke does not re-implement it."""
    (redirected / ".env").write_text(
        "".join(
            f"{key}={value}\n" for key, value in ENV.items() if key != "LANGFUSE_SECRET_KEY"
        )
    )

    with pytest.raises(SystemExit, match="Langfuse is half configured"):
        smoke.smoke_eks()


# --- the whole EKS run ---------------------------------------------------------------------


def test_the_eks_run_drives_one_turn_through_the_tunnel_and_asserts_both_backends(
    redirected, clickhouse, langfuse, tunnelled, capsys
):
    smoke.smoke_eks()

    assert tunnelled == [f"http://localhost:{PORT}"], "the turn goes through the port-forward"
    assert clickhouse.paths() == ["/"], "one ClickHouse query, on the HTTP interface"
    assert langfuse.paths() == [f"{smoke.LANGFUSE_TRACE_PATH}/{TRACE_ID}"]
    for path in clickhouse.paths() + langfuse.paths():
        assert "prompt" not in path, "the legacy /prompt block belongs to the laptop target"
    assert json.loads((redirected / ".runtime/smoke-results.json").read_text()) == [TURN]
    out = capsys.readouterr().out
    assert TRACE_ID in out and CONVERSATION in out and SHOP_SESSION in out


def test_the_run_stops_with_a_pointer_to_the_tunnel_when_nothing_answers(
    redirected, clickhouse, langfuse, tunnelled, monkeypatch
):
    monkeypatch.setattr(tunnel, "probe", lambda port, **kwargs: False)

    with pytest.raises(AssertionError, match="demo.py eks tunnel"):
        smoke.smoke_eks()

    assert tunnelled == [], "no turn is driven into a tunnel that is not there"
    assert clickhouse.requests == []


def test_neither_credential_reaches_a_process_or_the_console(
    redirected, clickhouse, langfuse, tunnelled, fake_sh, capsys
):
    """No argv at all on this path: the two backends are read in process, over HTTPS."""
    smoke.smoke_eks()

    assert fake_sh.calls == [], "the EKS smoke run starts no process to leak a secret into"
    out = capsys.readouterr()
    for secret in (PASSWORD, SECRET_KEY, ENV["OTLP_AUTH_TOKEN"]):
        assert secret not in out.out and secret not in out.err
    sent = [str(request.url) for request in clickhouse.requests + langfuse.requests]
    assert PASSWORD not in "\n".join(sent) and SECRET_KEY not in "\n".join(sent)
    # Where they do travel: an Authorization header on each backend's own request.
    assert clickhouse.requests[0].headers["authorization"].startswith("Basic ")
    assert langfuse.requests[0].headers["authorization"].startswith("Basic ")
