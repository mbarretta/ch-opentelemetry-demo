"""tests/conftest.py's own shared fixtures: the redirected checkout and the call recorder.

`redirected` is the piece of test infrastructure whose failure mode is silent. It is what keeps
a configured developer machine -- an exported CLICKHOUSE_ENDPOINT, an AWS_PROFILE, the
AGENT_MODE the suite sets for itself -- from deciding what the launcher tests see, because
`config.load_env()` layers the process environment over the root `.env` on purpose. Six modules
each had their own copy of that fixture and the copies scrubbed different keys, so which test
files were safe to run on a configured machine depended on the file. These tests pin the
consolidated contract: every key any of those copies scrubbed is scrubbed now, for every module
that asks, and the `.env` a module asked for still wins.

The junk values are exported per test with `monkeypatch.setenv` rather than inherited from the
shell the suite was launched in, and that is the whole point of doing it here: `tests/conftest.py`
sets AGENT_MODE, MCP_ENABLED and USE_VCR at import, before collection, so an AGENT_MODE exported
into `pytest` never reaches a test and proves nothing about this fixture. The contract has to be
asserted from inside the fixture, against an environment junked after conftest was imported.
"""

import os
from types import SimpleNamespace

import pytest
import test_eks_aws
import test_eks_deploy
import test_eks_infra
import test_eks_ops
import test_eks_tunnel
import test_smoke_queries
from conftest import REDIRECT_KEYS, Recorder

from launcher import core
from launcher.eks import config

# Every module that used to redirect `core.ROOT` for itself, with the `.env` mapping it asks the
# shared fixture to write and the keys its own copy of the fixture scrubbed. Both halves come
# from the live modules rather than a transcription, so a module that adds a key to its mapping
# is covered here the moment it does.
#
# The scrub sets are the drift this consolidation exists to remove, and they are the enumeration
# `conftest.REDIRECT_KEYS` is the union of: all twenty-one of `test_eks_deploy.py`'s keys against
# EKS_TUNNEL_PORT alone in `test_eks_tunnel.py` and nothing at all in `test_eks_config.py`.
MIGRATED = {
    "test_eks_deploy.py": (test_eks_deploy.ENV, tuple(test_eks_deploy.ENV)),
    "test_eks_ops.py": (test_eks_ops.ENV, ("AWS_PROFILE", "AWS_REGION", *test_eks_ops.ENV)),
    "test_eks_infra.py": (
        test_eks_infra.FULL_CLICKSTACK,
        ("AWS_REGION", "AWS_PROFILE", *config.CLICKSTACK_KEYS),
    ),
    "test_smoke_queries.py": (test_smoke_queries.ENV, tuple(test_smoke_queries.ENV)),
    "test_eks_tunnel.py": (test_eks_tunnel.ENV, tuple(test_eks_tunnel.ENV)),
    "test_eks_aws.py": (test_eks_aws.ENV, ("AWS_PROFILE", "AWS_REGION")),
    # The two that wrote no `.env` of their own: one fixture that scrubbed nothing, and one test
    # that redirected `core.ROOT` inline and scrubbed nothing either.
    "test_eks_config.py": ({}, ()),
    "test_eks_k8s.py": ({}, ()),
}

# A configured developer machine, as `.env.example` documents it and a presenter's shell holds
# it: the two ClickHouse credentials the collector reads, the agent's mode, and an AWS profile.
# Every value is junk, and none of them is a value any module's `.env` mapping holds -- which
# `test_the_redirected_env_wins_over_a_configured_machine` asserts rather than assumes.
JUNK = {
    "CLICKHOUSE_ENDPOINT": "https://junk.invalid:9999",
    "CLICKHOUSE_PASSWORD": "junk-clickhouse-password",
    "AGENT_MODE": "junk-mode",
    "AWS_PROFILE": "junk-profile",
}


@pytest.fixture
def exported_junk(monkeypatch):
    """The developer's machine, exported after `tests/conftest.py` was imported.

    Requested by `redirect_env` rather than by the tests, so that it is set up before
    `redirected` -- which scrubs -- rather than after it, which would assert nothing.
    """
    for key, value in JUNK.items():
        monkeypatch.setenv(key, value)
    return dict(JUNK)


@pytest.fixture(params=list(MIGRATED), ids=list(MIGRATED))
def migrated(request, exported_junk):
    """One migrated module: the `.env` mapping it asks for, and what its own copy scrubbed."""
    return MIGRATED[request.param]


@pytest.fixture
def redirect_env(migrated):
    """Overrides conftest's, so every test below runs once per migrated mapping."""
    mapping, _scrubbed = migrated
    return mapping


@pytest.fixture
def former_scrub(migrated):
    """The keys the copy of the fixture this run stands in for used to scrub for itself."""
    _mapping, scrubbed = migrated
    return scrubbed


def test_the_redirected_env_wins_over_a_configured_machine(
    redirected, redirect_env, former_scrub, exported_junk
):
    """The `.env` the module asked for decides, and nothing the shell exported does."""
    collisions = [key for key, value in exported_junk.items() if redirect_env.get(key) == value]
    assert collisions == [], "a junk value equal to the file's own value would assert nothing"

    env = config.load_env()

    assert {key: env.get(key) for key in redirect_env} == dict(redirect_env)
    for key in (*REDIRECT_KEYS, *redirect_env, *former_scrub):
        assert key not in os.environ, f"{key} was inherited from the shell"
    leaked = {key: value for key, value in env.items() if value in set(exported_junk.values())}
    assert leaked == {}, f"the machine's own configuration reached the test as {leaked}"


def test_the_redirected_checkout_holds_the_env_it_was_asked_for(redirected, redirect_env):
    assert core.ROOT == redirected, "every launcher path is resolved from here at call time"
    assert core.RUNTIME == redirected / ".runtime"
    assert dict(core.dotenv(redirected / ".env")) == dict(redirect_env)
    assert list(redirected.glob(".env")) == ([redirected / ".env"] if redirect_env else []), (
        "a module that asks for no mapping gets no `.env`, not an empty one"
    )


@pytest.mark.parametrize(("origin", "scrubbed"), sorted(MIGRATED.items()), ids=sorted(MIGRATED))
def test_the_union_scrubs_every_key_its_predecessors_did(origin, scrubbed):
    """No module may be less isolated than it was before its own copy of the fixture went away."""
    _mapping, keys = scrubbed
    missing = [key for key in keys if key not in REDIRECT_KEYS]
    assert missing == [], f"{origin} used to scrub {missing}, and conftest.REDIRECT_KEYS does not"


def test_the_recorder_answers_both_questions_asked_of_one_record(fake_sh, monkeypatch):
    """`args()` for tests/test_eks_infra.py and `at()` for tests/test_eks_deploy.py, one body."""
    collaborator = SimpleNamespace(tf_out=None, need=None)
    recorder = Recorder(fake_sh)
    recorder.patch(monkeypatch, collaborator, "tf_out", result=lambda name: f"{name}-value")
    recorder.patch(monkeypatch, collaborator, "need")

    assert collaborator.tf_out("cluster_name") == "cluster_name-value", (
        "a callable result is asked per call, which is how one stub answers every output name"
    )
    core.run("aws", "sts", "get-caller-identity")
    assert collaborator.need("kubectl") is None

    assert recorder.args("tf_out") == [("cluster_name",)]
    assert recorder.at("tf_out") == [0], "0 means it ran before any process did"
    assert recorder.at("need") == [1], "the one process had been recorded by then"
    assert recorder.names() == ["tf_out", "need"]
    assert recorder.called("need") and not recorder.called("aws_login")


def test_the_recorder_raises_what_it_was_handed_instead_of_answering(monkeypatch):
    collaborator = SimpleNamespace(down=None)
    recorder = Recorder()
    recorder.patch(monkeypatch, collaborator, "down", raises=RuntimeError("helm hung"))

    with pytest.raises(RuntimeError, match="helm hung"):
        collaborator.down()

    assert recorder.called("down")
    assert recorder.at("down") == [0], "a recorder built over no shell reports no position"


UNNAMED_KEY = "DEMO_A_KEY_THE_UNION_DOES_NOT_NAME"


class TestAKeyTheUnionDoesNotName:
    """A module can widen the scrub set, and only by naming a key in the `.env` it asks for.

    Which is what stops a module that adds a key to its own mapping from quietly becoming less
    isolated than it was: `redirected` scrubs its mapping's keys as well as `REDIRECT_KEYS`.
    """

    @pytest.fixture
    def redirect_env(self, monkeypatch):
        """Overrides the parameterised one above: one key, exported junk and all."""
        monkeypatch.setenv(UNNAMED_KEY, "from-the-shell")
        return {UNNAMED_KEY: "from-the-file"}

    def test_a_mapping_key_outside_the_union_is_scrubbed_too(self, redirected):
        assert UNNAMED_KEY not in REDIRECT_KEYS, "the whole point of this case is that it is not"
        assert UNNAMED_KEY not in os.environ
        assert config.load_env()[UNNAMED_KEY] == "from-the-file"


def assert_restored(before):
    """Every key `before` names holds what it held, which for None means it is unset again."""
    now = {key: os.environ.get(key) for key in before}
    assert now == before, "the environment the test was handed did not come back"


class TestTheEnvironmentComesBack:
    """What a test exports while a key is scrubbed does not outlive the test.

    `aws.aws_login()` exports AWS_PROFILE and AWS_REGION into the real environment on purpose,
    so this is the normal case rather than a corner of one, and it is why `redirected` saves and
    restores instead of calling `monkeypatch.delenv`: `delenv` records no undo entry for a key
    that was unset to begin with, and so never takes back out what the test put in.

    The check runs from a finalizer registered by a fixture `redirected` depends on, because
    fixture finalizers run in reverse order of setup: this one runs after `redirected` has torn
    down, and before `monkeypatch` undoes the two deletions that made the start state the same
    whatever the shell that launched the suite exports.
    """

    EXPORTED_BY_A_LOGIN = ("AWS_PROFILE", "AWS_REGION")

    @pytest.fixture
    def redirect_env(self, request, monkeypatch):
        for key in self.EXPORTED_BY_A_LOGIN:
            monkeypatch.delenv(key, raising=False)
        before = {key: os.environ.get(key) for key in self.EXPORTED_BY_A_LOGIN}
        request.addfinalizer(lambda: assert_restored(before))
        return {"AWS_PROFILE": "demo"}

    def test_a_key_the_test_exported_itself_is_taken_back_out(self, redirected):
        assert {key for key in self.EXPORTED_BY_A_LOGIN if key in os.environ} == set()

        for key, value in (("AWS_PROFILE", "demo"), ("AWS_REGION", "us-east-1")):
            os.environ[key] = value  # verbatim what aws.aws_login() does

        assert config.load_env()["AWS_REGION"] == "us-east-1", "the login is visible in the test"
