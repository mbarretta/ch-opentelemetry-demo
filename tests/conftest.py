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
