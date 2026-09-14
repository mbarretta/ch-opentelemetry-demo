"""Is telemetry landing, and where does the deployment stand.

Ports `verify.sh` and `status.sh`. Both are reports rather than assertions. They answer the two
questions asked in front of an audience -- is the trace pipeline working, and is this costing
money right now -- and a discouraging answer is still an answer, so neither command turns one
into a non-zero exit. What does fail is an unusable session or unreadable state: that is not the
demo being off, it is the report being unable to say anything at all.

`verify` is the demo's proof. The claim is that ONE trace id from one assistant turn reaches
both ClickStack and Langfuse out of a single collector with two exporters, so `verify` drives a
live turn through the tunnel, prints the trace id that turn reports, and then counts what landed
in ClickHouse Cloud -- the turn first, so its spans fall inside the window the queries read.
The Langfuse half of the same id is checked in the Langfuse UI; nothing here can see it.

The ClickHouse credentials reach `httpx` as an `auth=` keyword and leave as an Authorization
header. `verify.sh` needed a curl config file on a process-substitution fd for that, because
`ps` shows every argument of a running process to every user on the box; in-process there is no
argv to keep them out of.

`httpx` is imported on use rather than at import time: `launcher.cli` imports this module, and
`eks tunnel --loop` re-invokes that CLI from a bare interpreter with no virtualenv on its path
(see `tunnel`'s module docstring), so a top-level third-party import here would stop the
detached tunnel from starting.
"""

import re
import sys
import time
from collections import Counter
from uuid import uuid4

from .. import collector, core, images
from . import aws, config, k8s, tunnel

# The window every query reads, in ClickHouse's own interval syntax. `otel_traces`, `otel_logs`
# and `hyperdx_sessions` time-stamp with `Timestamp`; the metrics tables use `TimeUnix`.
WINDOW = "15 MINUTE"
# The same window in the words a report heading wants it in.
WINDOW_LABEL = "15 minutes"
# ClickHouse renders the result table itself, so a report comes back ready to print. MonoBlock:
# one table for the whole result rather than one per block the server happened to send.
OUTPUT_FORMAT = "PrettyCompactMonoBlock"
QUERY_TIMEOUT = 60
# The demo's own service names, as the resource attributes reach ClickHouse.
AGENT_SERVICE = "agent"
# Every storefront route the assistant owns -- the turn, the scoped cart mutation, the feedback
# and status reads -- as a LIKE pattern, and the single route one turn goes to. Both are built
# from `collector.ASSISTANT_PATH`, because that is the module whose OTTL statements *set* the
# `http.route` attribute the query below reads: a prefix that moved in one place and not the
# other would leave this report answering zero rows instead of saying it had lost the route.
ASSISTANT_ROUTE_PATTERN = f"{collector.ASSISTANT_PATH}/%"
ASSISTANT_PATH = f"{collector.ASSISTANT_PATH}/message"
ASSISTANT_MESSAGE = "Find a beginner telescope for the moon under $150"
ASSISTANT_BUDGET = 150
ASSISTANT_CURRENCY = "USD"
# One turn is an LLM call and its tools; `scripts/smoke.py` allows the same turn 110 seconds.
TURN_TIMEOUT = 110
# How much of the reply is worth showing: enough to see the agent answered the question asked.
REPLY_CHARS = 200
# The collector batches spans before it exports, so the turn's own spans are not in ClickHouse
# the instant the storefront answers. Waiting here is the difference between the counts below
# including the turn just driven and appearing to prove nothing.
SETTLE_SECONDS = 10
# The ClickStack collector is addressed as its Deployment, not as a pod: kubectl resolves it to
# a live pod, so the rollout-generated pod name never has to be hardcoded here -- it changes on
# every redeploy.
COLLECTOR_DEPLOYMENT = "deploy/clickstack-otel-collector"
COLLECTOR_POD = "otel-collector"
LOG_TAIL = 40
# A zero here is the one count that routinely means nothing is wrong, so it says so itself.
REPLAY_NOTE = (
    "zero is information, not a failure: session replay is recorded by the browser, so it "
    "counts only once somebody has visited the storefront through the tunnel."
)
# What `eks status` reports about the nightly scale-down; `infra.nightly` prints the same three
# fields after it flips the state.
SCHEDULE_QUERY = (
    "{State: State, ScheduleExpression: ScheduleExpression, Timezone: ScheduleExpressionTimezone}"
)
# A bare SQL identifier, which is all a database name may be here (see `require_identifier`).
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")


def register(subparsers):
    """Declare `eks verify` and `eks status`."""
    verify_parser = subparsers.add_parser(
        "verify", help="confirm telemetry is reaching ClickHouse Cloud, and report what is not"
    )
    verify_parser.set_defaults(handler=lambda args: verify())

    status_parser = subparsers.add_parser(
        "status", help="who you are, what is running, and what the nightly schedule will do"
    )
    status_parser.set_defaults(handler=lambda args: status())


def _httpx():
    """The HTTP client library, imported on use. See the module docstring for why not at import."""
    import httpx

    return httpx


def report_command(*args):
    """Run a read-only kubectl, helm or aws call whose failure is information, not a reason to stop.

    `verify` and `status` are reports: a namespace that does not exist yet, a release that was
    never installed, a schedule that has been destroyed are all answers. The command's own
    stderr is left visible, so the reason stays on screen; what is suppressed is turning that
    reason into a traceback and an exit status that says the demo is broken.
    """
    return core.run(*args, check=False)


def require_identifier(database):
    """Refuse a database name that is not a bare identifier.

    The name is interpolated into every statement below, exactly as it was into the bash's. It
    comes from the operator's own `.env`, so this is a typo guard rather than a trust boundary --
    but a typo that turns a report into some other statement is worth catching before it runs.
    """
    if not IDENTIFIER.match(database or ""):
        core.die(
            "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE is not a plain database name: "
            f"{database!r}"
        )
    return database


def queries(database):
    """Every SQL report `verify` issues, in order, as (title, statement, note) triples.

    The first three are `verify.sh`'s, unchanged in content: the four signal row counts it read
    in one UNION (traces, logs, metrics sum, metrics gauge), the per-service span breakdown, and
    the session-replay pair. The last two are what merging the two repositories makes possible,
    and they are the half of the demo the EKS repo never had -- the agent's own spans, and the
    storefront routes the assistant answers on.
    """
    recent = f"now() - INTERVAL {WINDOW}"
    return (
        (
            f"rows written in the last {WINDOW_LABEL} to the database: {database}",
            f"""
            SELECT 'traces' AS signal, count() FROM {database}.otel_traces
            WHERE Timestamp > {recent}
            UNION ALL
            SELECT 'logs', count() FROM {database}.otel_logs WHERE Timestamp > {recent}
            UNION ALL
            SELECT 'metrics (sum)', count() FROM {database}.otel_metrics_sum
            WHERE TimeUnix > {recent}
            UNION ALL
            SELECT 'metrics (gauge)', count() FROM {database}.otel_metrics_gauge
            WHERE TimeUnix > {recent}
            """,
            "",
        ),
        (
            "services reporting spans",
            f"""
            SELECT ServiceName, count() AS spans FROM {database}.otel_traces
            WHERE Timestamp > {recent}
            GROUP BY ServiceName ORDER BY spans DESC
            """,
            "",
        ),
        (
            f"session replay events and distinct sessions (last {WINDOW_LABEL})",
            # The patched frontend's replay events land in their own table, and the session id is
            # a resource attribute, so distinct browser sessions are counted from it.
            f"""
            SELECT count() AS events, uniqExact(ResourceAttributes['rum.sessionId']) AS sessions
            FROM {database}.hyperdx_sessions WHERE Timestamp > {recent}
            """,
            REPLAY_NOTE,
        ),
        (
            f"agent spans, and the turns they belong to (last {WINDOW_LABEL})",
            f"""
            SELECT count() AS spans, uniqExact(TraceId) AS traces, max(Timestamp) AS latest
            FROM {database}.otel_traces
            WHERE Timestamp > {recent} AND ServiceName = '{AGENT_SERVICE}'
            """,
            "",
        ),
        (
            f"storefront spans on the assistant routes ({ASSISTANT_ROUTE_PATTERN})",
            # The storefront's server spans carry the matched Next.js route, which is what makes
            # one turn's path through the frontend findable without knowing its trace id.
            f"""
            SELECT SpanAttributes['http.route'] AS route, count() AS spans,
                   uniqExact(TraceId) AS traces
            FROM {database}.otel_traces
            WHERE Timestamp > {recent}
                  AND SpanAttributes['http.route'] LIKE '{ASSISTANT_ROUTE_PATTERN}'
            GROUP BY route ORDER BY spans DESC
            """,
            "",
        ),
    )


def clickhouse_client(clickstack, timeout=QUERY_TIMEOUT):
    """An HTTP client bound to the ClickHouse endpoint, holding the credentials in process.

    `auth=` is the whole point: httpx turns the pair into an Authorization header on each
    request, so the password lives in this process and in the TLS session and nowhere else --
    not in argv, which `ps` publishes to every user on the machine, and not in a file on disk,
    which is what the bash had to write on a process-substitution fd to avoid argv.
    """
    httpx = _httpx()
    return httpx.Client(
        base_url=clickstack["CLICKHOUSE_ENDPOINT"],
        auth=(clickstack["CLICKHOUSE_USER"], clickstack["CLICKHOUSE_PASSWORD"]),
        timeout=timeout,
    )


def query(client, sql):
    """POST one statement to the ClickHouse HTTP interface and return the table it printed.

    The statement is the request body, sent raw rather than form-encoded, because ClickHouse
    reads a POST body as the query verbatim. A non-200 is reported and its body returned rather
    than raised: the remaining reports are worth running even when one of them names a table
    this deployment has never written to.
    """
    statement = f"{trim(sql)}\nFORMAT {OUTPUT_FORMAT}"
    response = client.post("/", content=statement.encode())
    if response.status_code != 200:
        print(f"warning: ClickHouse answered {response.status_code}", file=sys.stderr)
    return response.text.rstrip()


def trim(sql):
    """One statement with the source indentation taken back out of it."""
    return "\n".join(line.strip() for line in sql.strip().splitlines())


def storefront_client(port, timeout=TURN_TIMEOUT):
    """An HTTP client bound to the tunnelled storefront. No credentials: the routes are public."""
    httpx = _httpx()
    return httpx.Client(base_url=tunnel.url(port), timeout=timeout)


def assistant_request():
    """The one live turn `verify` drives, in the storefront's contract shape.

    A fresh shop session and request id per run, no `conversation_id` (this is a first turn) and
    no `scenario`: the deployed flag state is part of what is being verified, so the turn must
    not override it. The model is `concierge.contract.MessageRequest`, which forbids extra keys.
    """
    return {
        "shop_session_id": str(uuid4()),
        "request_id": str(uuid4()),
        "message": ASSISTANT_MESSAGE,
        "currency_code": ASSISTANT_CURRENCY,
        "budget": ASSISTANT_BUDGET,
    }


def assistant_turn(port=None):
    """Drive one assistant turn through the tunnel; return the trace id it reports, or None.

    This is the demo's claim in a single call -- storefront, gateway collector, agent, both
    exporters -- and the id printed here is the id to paste into ClickStack and into Langfuse.
    It runs before the queries so its spans fall inside the window they count.

    A tunnel that is not up, or a storefront that answers an error, is reported and returns
    None. The SQL half of `verify` is what says whether anything at all is landing, and it is
    worth running either way.
    """
    httpx = _httpx()
    port = tunnel.resolved_port(port)
    endpoint = tunnel.url(port).rstrip("/") + ASSISTANT_PATH
    core.log(f"one assistant turn through the tunnel ({endpoint})")
    try:
        with storefront_client(port) as client:
            response = client.post(ASSISTANT_PATH, json=assistant_request())
            response.raise_for_status()
            answer = response.json()
    except (httpx.HTTPError, ValueError) as failure:
        print(
            f"warning: the assistant turn did not complete ({failure}). Is the tunnel up "
            "(`demo.py eks tunnel status`) and the release healthy (`demo.py eks status`)?",
            file=sys.stderr,
        )
        return None

    trace_id = answer.get("trace_id")
    if not trace_id:
        print("warning: the storefront answered without a trace id", file=sys.stderr)
        return None
    print(f"    trace id {trace_id} (conversation {answer.get('conversation_id')})")
    print("    the same id is one Langfuse trace; paste it into both UIs")
    reply = " ".join((answer.get("reply") or "").split())
    if reply:
        print(f"    reply: {reply[:REPLY_CHARS]}{'...' if len(reply) > REPLY_CHARS else ''}")
    return trace_id


def collector_pods(listing):
    """The header and the otel-collector rows out of a `kubectl get pods` table.

    Filtered here rather than piped through grep, which is what the bash had to do: an early
    `grep` exit would make a failed kubectl read look like a namespace with no collector in it.
    """
    lines = (listing or "").splitlines()
    matched = [line for line in lines if COLLECTOR_POD in line and not line.startswith("NAME")]
    if not matched:
        return f"    no {COLLECTOR_POD} pods in namespace {config.NS_DEMO}"
    return "\n".join([line for line in lines if line.startswith("NAME")] + matched)


def report_collector():
    """Both collectors' pods and the ClickStack collector's recent logs.

    Both, because there are two: the demo's own gateway collector in `otel-demo`, where the
    assistant spans arrive, and the ClickStack collector in `clickstack`, which is what writes
    them to ClickHouse Cloud. A zero row count above is nearly always one of the two
    CrashLooping, which is why the logs come with the counts rather than on request.
    """
    core.log(f"collector pod state in {config.NS_CS}")
    report_command("kubectl", "-n", config.NS_CS, "get", "pods")

    print()
    core.log(f"gateway collector pods in {config.NS_DEMO}")
    listing = core.capture("kubectl", "-n", config.NS_DEMO, "get", "pods", check=False)
    if listing.returncode:
        print(f"    could not list pods in {config.NS_DEMO} (see the error above)")
    else:
        print(collector_pods(listing.stdout))

    print()
    core.log(f"ClickStack collector logs (last {LOG_TAIL} lines)")
    report_command(
        "kubectl", "-n", config.NS_CS, "logs", COLLECTOR_DEPLOYMENT, f"--tail={LOG_TAIL}"
    )


def verify():
    """Drive one assistant turn, count what reached ClickHouse Cloud, then show the collectors.

    The credentials travel as request headers through `httpx`, never in argv. Traces, logs,
    metrics and session-replay counts are reported; a zero count for replay is reported rather
    than failed, because replay needs a browser to have visited the storefront.

    Returns the trace id of the turn it drove, or None when it could not drive one.
    """
    # curl is gone: the queries are issued in process. aws and tofu are still needed for the
    # session and the cluster name, kubectl for the collector pods and their logs.
    core.need("aws", "tofu", "kubectl")
    clickstack = config.load_clickstack_env()
    database = require_identifier(clickstack["HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE"])

    aws.aws_login()
    k8s.kubeconfig()

    trace_id = assistant_turn()
    if trace_id:
        core.log(f"waiting {SETTLE_SECONDS}s for the collector to export that turn")
        time.sleep(SETTLE_SECONDS)

    with clickhouse_client(clickstack) as client:
        for title, sql, note in queries(database):
            print()
            core.log(title)
            print(query(client, sql))
            if note:
                print(f"    {note}")

    print()
    report_collector()
    return trace_id


def pod_phases():
    """Every pod's phase across all namespaces, counted; None when the pods cannot be listed.

    `collections.Counter` over one `custom-columns` read, where the bash piped `sort | uniq -c`.
    A cluster scaled to zero has no pods at all, which is an answer and not an error, so that
    case is an empty Counter rather than a failure.
    """
    listing = core.capture(
        "kubectl",
        "get",
        "pods",
        "-A",
        "--no-headers",
        "-o",
        "custom-columns=P:.status.phase",
        check=False,
    )
    if listing.returncode:
        return None
    return Counter((listing.stdout or "").split())


def pod_phase_report():
    """The pod-phase counts as indented lines, commonest first, or why there are none."""
    phases = pod_phases()
    if phases is None:
        return "    could not list pods (see the error above)"
    if not phases:
        return "    no pods"
    return "\n".join(f"    {count} {phase}" for phase, count in phases.most_common())


def image_report():
    """One line per service: whether ECR holds its image at the manifest tag, and what to do.

    Three distinguishable answers, because the fix differs for each. No manifest at all means
    nothing has ever been built in this checkout, so `demo.py build` comes first. A manifest
    with no `published` entry for the service means the image was built and never pushed, which
    is `demo.py publish`. A published entry whose tag is not the manifest's means the build
    inputs moved on since that push, so the release would run yesterday's image.
    """
    manifest = images.read_manifest()
    if manifest is None:
        return [
            f"    no build manifest at {images.manifest_path()}: nothing has been built here. "
            "Run `demo.py build` and then `demo.py publish`."
        ]

    tag = manifest.get("tag")
    if not tag:
        # Probing ECR for the literal `imageTag=None` would report all four absent and then
        # name the wrong fix for it.
        return [
            f"    the build manifest at {images.manifest_path()} records no tag; "
            "re-run `demo.py build`"
        ]
    published = manifest.get("published") or {}
    # `.get`, not `aws.tf_out`: a repository the outputs do not name is worth one line of report
    # rather than the end of the report.
    repositories = aws.tf_outputs().get("ecr_repository_urls") or {}
    lines = []
    for service in core.IMAGES:
        recorded = published.get(service) or {}
        repository = recorded.get("repository") or repositories.get(service)
        if not repository:
            lines.append(
                f"    {service}: no ECR repository in the OpenTofu outputs; run "
                "`demo.py eks apply`"
            )
            continue
        name = aws.repository_name(repository)
        if aws.ecr_has_image(repository, tag):
            lines.append(f"    {service}: {name}:{tag} present")
        elif not recorded.get("tag"):
            lines.append(
                f"    {service}: {name}:{tag} absent -- built but not published; run "
                "`demo.py publish`"
            )
        elif recorded["tag"] != tag:
            lines.append(
                f"    {service}: {name}:{tag} absent -- last published at {recorded['tag']}, "
                "so the build inputs have changed since; run `demo.py publish`"
            )
        else:
            lines.append(
                f"    {service}: {name}:{tag} recorded as published but missing from the "
                "repository; run `demo.py publish` again"
            )
    return lines


def status():
    """Report identity, node group, pods, release, tunnel, published images and schedule.

    Exits 0 in both the idle and the up state: a non-zero exit means something is actually
    broken (authentication, missing state), not that the demo is switched off.
    """
    core.need("aws", "tofu", "kubectl", "helm")
    aws.aws_login()

    core.log("identity")
    report_command(
        "aws",
        "sts",
        "get-caller-identity",
        "--query",
        "{Account: Account, Arn: Arn}",
        "--output",
        "table",
    )

    core.log("node group")
    # Both reads resolved into locals first, the way the bash did it: a failing read inside the
    # line below would print an empty field and carry on as if it had answered.
    desired = aws.ng_desired()
    nodegroup_status = aws.ng_status()
    print(f"    desiredSize={desired} status={nodegroup_status}")

    core.log("cluster")
    k8s.kubeconfig()
    if desired > 0:
        report_command("kubectl", "get", "nodes")
    else:
        # Not a fault: scaling to zero overnight is how the demo stops costing money, and the
        # node list of a cluster with no nodes is a slow way of printing nothing.
        print("    scaled to zero (`demo.py eks up` to start it)")

    core.log("pods by phase (all namespaces)")
    print(pod_phase_report())

    core.log(f"helm releases in {config.NS_DEMO}")
    report_command("helm", "list", "-n", config.NS_DEMO)

    core.log("tunnel")
    tunnel.status()

    core.log("published images")
    for line in image_report():
        print(line)

    core.log("nightly scale-down schedule")
    schedule = aws.tf_out("scheduler_name")
    report_command(
        "aws",
        "scheduler",
        "get-schedule",
        "--name",
        schedule,
        "--query",
        SCHEDULE_QUERY,
        "--output",
        "table",
    )
