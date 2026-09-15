# The suite's mechanical inventories: the hand-kept lists and the recorded files that an edit
# made somewhere else obliges. Each is a real guard that names what moved when it fails, and
# each fails in a file the person making that edit is not touching -- so this list is here to
# make the obligation discoverable before the failure rather than after it. Read it as "if you
# edit the left, update the right".
#
# - A README (README.md, deploy/eks/README.md, docker/README.md, frontend/overlay/README.md):
#   `BARE_SPANS` in `tests/test_docs.py` pins every bare command span each of the four shows,
#   and `MENTIONS` pins the spans that name a piece of the CLI instead of showing a line to
#   run. `RETIRED` pins the pre-merge names no document may use outside the root README's
#   History note, and the root's own outline and EKS runbook are pinned by `SECTIONS`,
#   `FIRST_RUN` and `EVERYDAY`.
# - A `raise SystemExit("...")` added under launcher/ or scripts/, or either tree renamed:
#   `REFUSAL_TREES` in `tests/test_core.py` is the scanned pair -- scripts/ included, since it
#   holds the other operator-facing entry points -- and
#   `test_every_refusal_goes_through_die_and_int_exits_are_left_alone` pins the refusal sites
#   by file: messages in launcher/core.py alone, and the tunnel's two integer exit codes.
# - `subprocess` imported anywhere under launcher/, scripts/, tests/ or concierge/:
#   `IMPORT_TREES` in `tests/test_core.py` is that scan's four trees, and
#   `test_subprocess_is_imported_in_core_alone_so_the_process_surface_stays_one_place` pins
#   launcher/core.py as the only importer.
# - A key added to a module's `redirect_env` mapping, or a former per-module scrub set changed:
#   `REDIRECT_KEYS` below is the union those scrub sets were consolidated into, and `MIGRATED`
#   in `tests/test_conftest.py` holds the enumeration this tuple has to keep covering.
# - `config.CHART_VERSION` bumped: both recordings in `tests/renders/` have to be regenerated,
#   because every assertion read out of them is a stale render until they are.
#   `chart_pin_problems` in `tests/test_eks_check.py` compares each recording's `helm.sh/chart`
#   label against the pin and names the regeneration command.
# - The `vpc-cni` add-on in `deploy/eks/tofu/eks.tf`: `enforcement_problems` in
#   `tests/test_eks_check.py` pins the one setting, `enableNetworkPolicy = "true"` in that
#   add-on's own `configuration_values`, that makes the agent NetworkPolicy enforceable on the
#   cluster. That OpenTofu is read as text and never written.
# - `config.STATIC_MANIFESTS` edited: `unapplied_static_manifests` and
#   `test_every_manifest_eks_check_parses_is_one_the_deploy_actually_applies` in
#   `tests/test_eks_deploy.py` pin the tuple `eks check` parses against the manifests
#   `eks deploy` really applies, so an entry nothing applies is named rather than trusted.
# - An `eks` subcommand implemented: `IMPLEMENTED` in `tests/test_cli.py` names the test file
#   that owns each landed handler, and the dispatch guard calls every subcommand outside it.
#
# Six of these are paired with a committed negative test that provokes the guard with the
# failure it exists to catch -- the two scans in `tests/test_core.py`, the chart pin and the
# add-on setting in `tests/test_eks_check.py`, the manifest tuple in `tests/test_eks_deploy.py`
# and the dispatch guard in `tests/test_cli.py` -- and each of those sits directly below the
# guard it feeds. The README lists and the `REDIRECT_KEYS` union are pinned by assertion only.

import argparse
import io
import os
import shlex

os.environ["USE_VCR"] = "False"
os.environ["MCP_ENABLED"] = "False"
os.environ["AGENT_MODE"] = "scripted"
os.environ["GRADIO_ANALYTICS_ENABLED"] = "False"

import pytest  # noqa: E402
from opentelemetry import trace  # noqa: E402
from opentelemetry.sdk.trace import TracerProvider  # noqa: E402
from opentelemetry.sdk.trace.export import SimpleSpanProcessor  # noqa: E402
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,  # noqa: E402
)

from concierge.telemetry import ConversationProcessor  # noqa: E402
from launcher import core  # noqa: E402

exporter = InMemorySpanExporter()
provider = TracerProvider()
provider.add_span_processor(ConversationProcessor())
provider.add_span_processor(SimpleSpanProcessor(exporter))
trace.set_tracer_provider(provider)


@pytest.fixture
def spans():
    exporter.clear()
    yield exporter


class Call:
    """One recorded process launch: the argv, whatever was fed to stdin, and the keywords."""

    def __init__(self, argv, kwargs):
        self.argv = argv
        # What `input=` fed the process: the manifests and flag files the launcher pipes to
        # `kubectl apply -f -`, recorded so a test can assert both that a secret reached the
        # process and that it stayed out of argv. Every other keyword, a `stdin=` stream
        # among them, is kept as it was passed in `kwargs`.
        self.stdin = kwargs.get("input")
        self.kwargs = kwargs

    @property
    def line(self):
        """The call as a shell command line, which is what prefix answers match against."""
        return shlex.join(self.argv)

    def __repr__(self):
        return f"Call({self.line!r})"


def as_bytes(output):
    """An answer as bytes, whether the test wrote it as text or as bytes."""
    return output if isinstance(output, bytes) else output.encode()


class FakeProcess:
    """What `core.popen()` hands back: a finished process over a fixed stdout."""

    def __init__(self, argv, stdout, returncode):
        self.args = argv
        self.stdout = io.BytesIO(as_bytes(stdout))
        self.returncode = returncode

    def wait(self, timeout=None):
        return self.returncode

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        self.stdout.close()
        return False


class FakeShell:
    """The three `core` chokepoints, recorded instead of run.

    Every launcher module reaches the outside world through `core.run`, `core.capture` or
    `core.popen`, so faking those three is enough to run any command offline. Calls are
    recorded in order with their argv and stdin; answers come from a table keyed by command-line
    prefix, longest prefix first, so a test can set up `aws sts get-caller-identity` once and
    still override `aws sts get-caller-identity --query Account` for one case.

    Unanswered calls succeed with empty output, which is what most commands do: only the ones a
    test actually reads from need an entry.
    """

    def __init__(self):
        self.calls = []
        self.answers = {}

    def reply(self, prefix, stdout="", stderr="", returncode=0):
        """Answer every call whose command line starts with `prefix`."""
        self.answers[prefix] = (stdout, stderr, returncode)

    def lines(self):
        """Every recorded call as a command line, in order."""
        return [call.line for call in self.calls]

    @property
    def last(self):
        return self.calls[-1]

    def record(self, args, kwargs):
        call = Call([str(argument) for argument in args], kwargs)
        self.calls.append(call)
        return call

    def answer(self, call):
        matched = [prefix for prefix in self.answers if call.line.startswith(prefix)]
        if not matched:
            return "", "", 0
        return self.answers[max(matched, key=len)]

    def run(self, *args, check=True, **kwargs):
        call = self.record(args, kwargs)
        stdout, stderr, returncode = self.answer(call)
        if check and returncode:
            raise core.subprocess.CalledProcessError(returncode, call.argv, stdout, stderr)
        return core.subprocess.CompletedProcess(call.argv, returncode, stdout, stderr)

    def capture(self, *args, check=True, text=True, **kwargs):
        call = self.record(args, kwargs)
        stdout, stderr, returncode = self.answer(call)
        if check and returncode:
            raise core.subprocess.CalledProcessError(returncode, call.argv, stdout, stderr)
        if not text:
            stdout, stderr = as_bytes(stdout), as_bytes(stderr)
        return core.subprocess.CompletedProcess(call.argv, returncode, stdout, stderr)

    def popen(self, *args, **kwargs):
        call = self.record(args, kwargs)
        stdout, _, returncode = self.answer(call)
        return FakeProcess(call.argv, stdout, returncode)


@pytest.fixture
def fake_sh(monkeypatch):
    """Record every subprocess the launcher would start, and answer from a prefix table."""
    shell = FakeShell()
    monkeypatch.setattr(core, "run", shell.run)
    monkeypatch.setattr(core, "capture", shell.capture)
    monkeypatch.setattr(core, "popen", shell.popen)
    return shell


# What the test modules import from here by name -- `Recorder`, `write_env` and
# `REDIRECT_KEYS` -- they import as `from conftest import ...`, deliberately not as
# `from tests.conftest import ...`. The second form re-executes this file under a second module
# name: a second TracerProvider is registered and the `spans` fixture above ends up reading an
# exporter that nothing writes to.
class Recorder:
    """A collaborator outside the module under test, recorded instead of run.

    Two different questions get asked of the same record, which is why both accessors live
    here: what a collaborator was passed (`args()`, which is the assertion in
    `tests/test_eks_infra.py`, where this class is `Stubs`) and where its call sits in the
    sequence of processes (`at()`, the assertion in `tests/test_eks_deploy.py`, where it is
    `Steps`). Neither module wants the other's accessor, but both had the same body.

    A position is the number of calls `fake_sh` had recorded when the stub ran, so 0 means it
    ran before any process did. It is only meaningful for a recorder built over a shell; one
    built without reports 0 for every call.
    """

    def __init__(self, shell=None):
        self.shell = shell
        self.calls = []

    def stub(self, name, result=None, raises=None):
        """A stand-in for `name` that records the call, then raises or answers.

        A callable `result` is called with the positional arguments and what it returns is the
        answer, which is how a stub answers per-argument rather than once for every call
        (`aws.tf_out` is asked for one output name at a time). Any other `result` is answered
        as it is.
        """

        def action(*args, **kwargs):
            self.calls.append((name, args, len(self.shell.calls) if self.shell else 0))
            if raises is not None:
                raise raises
            return result(*args) if callable(result) else result

        return action

    def patch(self, monkeypatch, module, name, **answer):
        monkeypatch.setattr(module, name, self.stub(name, **answer))

    def names(self):
        """Every recorded call's name, in order, which is what an ordering assertion reads."""
        return [called for called, _, _ in self.calls]

    def args(self, name):
        """The positional arguments of every recorded call to `name`, in order."""
        return [args for called, args, _ in self.calls if called == name]

    def at(self, name):
        """The process count at each call to `name`: 0 means it ran before any process did."""
        return [processes for called, _, processes in self.calls if called == name]

    def called(self, name):
        return bool(self.args(name))


# Every key `redirected` removes from the process environment, on top of the keys of whatever
# `.env` mapping the requesting module asks it to write.
#
# This is the union of what six per-module copies of that fixture scrubbed before they were
# consolidated here, and the union is the whole point: the copies had drifted, so which test
# files were safe to run on a configured developer machine depended on the file.
# `tests/test_eks_deploy.py` scrubbed all twenty-one keys below; `tests/test_eks_ops.py` its own
# seven plus AWS_REGION; `tests/test_eks_infra.py` only AWS_PROFILE, AWS_REGION and the five
# `config.CLICKSTACK_KEYS`; `tests/test_smoke_queries.py` its own nine; `tests/test_eks_tunnel.py`
# EKS_TUNNEL_PORT alone; `tests/test_eks_config.py` nothing at all; and the copy inlined in
# `tests/test_eks_aws.py` only the AWS pair. `tests/test_conftest.py` holds that enumeration as
# data and fails if this tuple drops any of it.
#
# There is deliberately no per-module scrub list to go with `redirect_env`: a module scrubbing
# its own choice of keys is exactly how the six copies drifted apart, so the set is shared and
# a module can only widen it by naming a key in the `.env` it asks for.
REDIRECT_KEYS = (
    # The AWS session. `aws.aws_login()` exports both into the real environment on purpose --
    # that is how kubectl's exec-auth plugin sees them -- so they also have to come back out
    # after any test that runs a login.
    "AWS_PROFILE",
    "AWS_REGION",
    # The port the tunnel binds and every client of it then reads back.
    "EKS_TUNNEL_PORT",
    # The agent's own configuration. AGENT_MODE and MCP_ENABLED are set for the whole suite at
    # the top of this file, before collection, so without them here a redirected test would read
    # this file's values rather than the `.env` it asked for.
    "AGENT_MODE",
    "MCP_ENABLED",
    "LLM_BASE_URL",
    "LLM_MODEL",
    "API_KEY",
    "ASSISTANT_DEMO_DETAILS",
    # The ClickStack collector's five credentials (`config.CLICKSTACK_KEYS`) and the trace link
    # the agent builds from the same deployment.
    "CLICKHOUSE_ENDPOINT",
    "CLICKHOUSE_USER",
    "CLICKHOUSE_PASSWORD",
    "HYPERDX_OTEL_EXPORTER_CLICKHOUSE_DATABASE",
    "OTLP_AUTH_TOKEN",
    "CLICKSTACK_TRACE_URL_TEMPLATE",
    # Langfuse: the credential pair and base URL the SDK reads, and the three the UI links the
    # demo prints are built from.
    "LANGFUSE_BASE_URL",
    "LANGFUSE_PUBLIC_KEY",
    "LANGFUSE_SECRET_KEY",
    "LANGFUSE_PROMPT_LABEL",
    "LANGFUSE_PROJECT_ID",
    "LANGFUSE_PUBLIC_URL",
)


def write_env(root, values):
    """Write `values` as the `.env` of a redirected checkout."""
    (root / ".env").write_text("".join(f"{key}={value}\n" for key, value in values.items()))


@pytest.fixture
def redirect_env():
    """The mapping `redirected` writes to the `.env` of its checkout: nothing, by default.

    Override it in a module whose tests need a filled-in `.env`:

        @pytest.fixture
        def redirect_env():
            return ENV

    Its keys are scrubbed from the process environment along with `REDIRECT_KEYS`, so a module
    that adds a key to its own mapping cannot become less isolated than it was.
    """
    return {}


@pytest.fixture
def redirected(tmp_path, monkeypatch, redirect_env):
    """A checkout of our own, with the `.env` the module asked for and nothing from the shell.

    `config.load_env()` layers the process environment over the root `.env`, so an exported
    CLICKHOUSE_ENDPOINT, AWS_PROFILE or AGENT_MODE on the developer's machine would otherwise
    decide what these tests see -- and this file exports two of them itself. Every key in
    `REDIRECT_KEYS` and every key of `redirect_env` is removed for the duration of the test and
    put back afterwards.

    Saved and restored rather than `monkeypatch.delenv`-ed, deliberately: `delenv` records no
    undo entry for a key that was not set to begin with, so a key the test then exports itself
    would outlive it. `aws.aws_login()` exports AWS_PROFILE and AWS_REGION into the real
    environment on purpose -- that is how kubectl's exec-auth plugin sees them -- which makes
    that the normal case here rather than a corner of one. `tests/test_cli.py` spells that same
    save-and-restore as a `restored(*keys)` context manager, for the two keys a provoked login
    exports mid-test; the shape is shared deliberately and the code is not, because the body of
    this fixture is what every test module requests by name and moving it is a refactor rather
    than a guard.

    Modules that need more than a checkout and an `.env` override this fixture and request it,
    adding their own patches on top (see `tests/test_eks_deploy.py`).
    """
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "RUNTIME", tmp_path / ".runtime")
    scrubbed = {key: os.environ.pop(key, None) for key in (*REDIRECT_KEYS, *redirect_env)}
    if redirect_env:
        write_env(tmp_path, redirect_env)
    yield tmp_path
    for key, value in scrubbed.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


def subparsers_action(parser):
    """The subparser action a parser declares, or None when it declares no subcommands.

    `_actions` and `_SubParsersAction` are argparse internals with no public equivalent, which
    is the reason this and `takes_a_value` below are the suite's only two readers of them: an
    upgrade that moves them breaks these two functions rather than one line per test module.
    `tests/test_cli.py` wants the choices and treats an absence as a failure;
    `tests/test_docs.py` wants the action and has documented command groups that legitimately
    have none, so the shared piece is the lookup and neither contract moves here.
    """
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def takes_a_value(parser, option):
    """Whether `option` is one of `parser`'s own options and expects a value after it.

    The other half of the internals above, here for the same reason: `option_strings` and
    `nargs` are read in the file that reads `_actions`, not once per test module. `nargs != 0`
    is the whole distinction -- `build --platform <arch>` takes a value that the sentence
    around it supplies, where `up --dev` stands alone -- and `tests/test_docs.py` is the
    caller, because that is how it tells a README span naming an option from one showing a
    command to run.
    """
    return any(option in action.option_strings and action.nargs != 0 for action in parser._actions)
