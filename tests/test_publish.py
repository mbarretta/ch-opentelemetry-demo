"""`demo.py publish`: the four images into ECR, with the platform gate build-frontend.sh lacked.

Every test runs offline through `fake_sh`, so what is asserted is the argv, what went in on
stdin, and the manifest that comes back out -- never a registry. The retired bash pushed one
image and trusted the operator to have built it for the node group's architecture; the gate
asserted here is the reason that trust is no longer needed.
"""

import json

import pytest

from launcher import core, images
from launcher.eks import aws, config

REGISTRY = "111122223333.dkr.ecr.us-east-1.amazonaws.com"
REPOSITORIES = {service: f"{REGISTRY}/ch-opentelemetry-demo/{service}" for service in core.IMAGES}
TAG = "abc123def456"
PLATFORM = "linux/arm64"
PASSWORD = "sensitive-ecr-password"

# `tofu output -json` as the CLI prints it; aws.tf_outputs() unwraps `value`.
OUTPUTS = {
    "region": {"value": "us-east-1"},
    "node_platform": {"value": PLATFORM},
    "ecr_registry": {"value": REGISTRY},
    "ecr_repository_urls": {"value": REPOSITORIES},
}

# One distinguishable digest per service, so a misfiled record shows up as the wrong service's.
DIGESTS = {
    service: f"sha256:{index}{index}{index}{index}"
    for index, service in enumerate(core.IMAGES, start=1)
}

# The presence probe carries the tag but no --query; the digest read adds one. Both prefixes
# name the repository down to its last segment, because `.../frontend` is also a prefix of
# `.../frontend-proxy` and an answer keyed on the shorter one would answer for both.
PROBE = (
    "aws ecr describe-images --repository-name ch-opentelemetry-demo/{service} "
    "--image-ids imageTag={tag}"
)
DIGEST_QUERY = PROBE + " --query"


def digest_answers(shell, tag=TAG):
    """Answer the digest read for each service.

    `ecr_has_image` and `ecr_image_digest` are both `aws ecr describe-images`; the digest read
    is the one that carries `--query`, and the answer table matches the longest prefix, so these
    entries override a shorter `aws ecr describe-images` presence probe.
    """
    for service, digest in DIGESTS.items():
        shell.reply(DIGEST_QUERY.format(service=service, tag=tag), stdout=digest + "\n")


class Needed:
    """A recorded `core.need` or `aws.aws_login`, shaped like a call so it keeps its place.

    `fake_sh` records processes; these two start none, and the order publish does them in
    relative to the processes is exactly what one of the tests below is about.
    """

    def __init__(self, *argv):
        self.argv = list(argv)
        self.stdin = None
        self.kwargs = {}

    @property
    def line(self):
        return " ".join(self.argv)


@pytest.fixture
def cluster(fake_sh, monkeypatch, tmp_path):
    """A built manifest, the OpenTofu outputs, and an AWS session that is already established.

    `aws_login` is recorded rather than run: the session, the profile export and the `aws sso
    login` fallback are tested in tests/test_eks_aws.py, and what matters here is only that
    publish establishes it before it reads an output or logs docker in.
    """
    monkeypatch.setattr(core, "RUNTIME", tmp_path / "runtime")
    # The parsed outputs are cached process-wide and outlive the RUNTIME redirection above.
    monkeypatch.setattr(aws, "_OUTPUTS", None)
    monkeypatch.setattr(images, "image_exists", lambda image: True)
    monkeypatch.setattr(core, "need", lambda *names: fake_sh.calls.append(Needed(*names)))
    monkeypatch.setattr(aws, "aws_login", lambda: fake_sh.calls.append(Needed("aws_login")))
    fake_sh.reply("tofu", stdout=json.dumps(OUTPUTS))
    fake_sh.reply("aws ecr get-login-password", stdout=PASSWORD + "\n")
    digest_answers(fake_sh)
    return fake_sh


def build_manifest(platform=PLATFORM, tag=TAG, services=None):
    """Record a build of `services` at `tag`, the way `demo.py build` leaves the manifest."""
    services = core.IMAGES if services is None else services
    return images.write_manifest(
        tag, platform, {service: f"sha256:{service}" for service in services}
    )


def present(shell, *services, tag=TAG):
    """Report those services' tags as already in ECR, and every other service's as absent."""
    shell.reply("aws ecr describe-images", returncode=254)
    for service in services:
        shell.reply(PROBE.format(service=service, tag=tag), returncode=0)
    digest_answers(shell, tag)


# --- the happy path -------------------------------------------------------------------------


def test_publish_pushes_each_service_to_its_own_repository_and_records_the_manifest(cluster):
    build_manifest()
    present(cluster)

    images.publish(list(core.IMAGES))

    lines = cluster.lines()
    for service, repository in REPOSITORIES.items():
        remote = f"{repository}:{TAG}"
        tag_line = f"docker tag astronomy-concierge-{service}:{TAG} {remote}"
        assert tag_line in lines, lines
        assert f"docker push {remote}" in lines, lines
        assert lines.index(tag_line) < lines.index(f"docker push {remote}"), (
            "the local image is retagged before it is pushed"
        )

    published = images.read_manifest()["published"]
    assert set(published) == set(core.IMAGES)
    for service, repository in REPOSITORIES.items():
        assert published[service] == {
            "repository": repository,
            "tag": TAG,
            "digest": DIGESTS[service],
        }
        assert set(published[service]) == set(images.PUBLISHED_FIELDS)


def test_the_ecr_password_reaches_docker_on_stdin_and_never_argv(cluster):
    build_manifest()
    present(cluster)

    images.publish(list(core.IMAGES))

    login = [call for call in cluster.calls if call.argv[:2] == ["docker", "login"]]
    assert len(login) == 1, "one login for the whole push, as build-frontend.sh did"
    assert login[0].argv == [
        "docker",
        "login",
        "--username",
        "AWS",
        "--password-stdin",
        REGISTRY,
    ]
    assert login[0].stdin == PASSWORD
    assert not any(PASSWORD in argument for call in cluster.calls for argument in call.argv), (
        "argv is readable by every local user; the password goes in on stdin only"
    )


def test_publish_establishes_the_session_before_it_reads_an_output_or_logs_in(cluster):
    build_manifest()
    present(cluster)

    images.publish(list(core.IMAGES))

    lines = cluster.lines()
    login = next(index for index, line in enumerate(lines) if line.startswith("docker login"))
    outputs = lines.index(f"tofu -chdir={config.tofu_dir()} output -json")
    assert lines.index("aws docker tofu") == 0, "the PATH check comes before anything else"
    assert lines.index("aws_login") < outputs < login


# --- the platform gate ----------------------------------------------------------------------


def test_publish_refuses_a_platform_the_nodes_cannot_run_and_names_the_fix(cluster):
    build_manifest(platform="linux/amd64")
    present(cluster)

    with pytest.raises(SystemExit) as failure:
        images.publish(list(core.IMAGES))

    message = str(failure.value)
    assert "linux/amd64" in message and PLATFORM in message
    assert f"demo.py build --platform {PLATFORM}" in message, message
    assert not any(call.argv[0] == "docker" for call in cluster.calls), (
        "a wrong-architecture image is refused before it is logged in for, let alone pushed"
    )
    assert images.read_manifest().get("published") is None


# --- skip unless --force --------------------------------------------------------------------


def test_a_tag_already_in_ecr_is_skipped_and_recorded_anyway(cluster, capsys):
    build_manifest()
    present(cluster, "frontend", "agent", "mcp", "frontend-proxy")

    images.publish(list(core.IMAGES))

    lines = cluster.lines()
    assert not any(line.startswith("docker push") for line in lines), lines
    assert not any(line.startswith("docker login") for line in lines), (
        "nothing to push means nothing to log in for"
    )
    assert "--force" in capsys.readouterr().out
    published = images.read_manifest()["published"]
    assert set(published) == set(core.IMAGES), (
        "an image already in the registry is still published: the deploy's gate reads this "
        "section, not ECR"
    )
    assert published["agent"]["digest"] == DIGESTS["agent"]


def test_force_pushes_a_tag_that_is_already_in_ecr(cluster):
    build_manifest()
    present(cluster, *core.IMAGES)

    images.publish(["frontend"], force=True)

    lines = cluster.lines()
    assert f"docker push {REPOSITORIES['frontend']}:{TAG}" in lines, lines
    probes = [line for line in lines if line.startswith(PROBE.format(service="frontend", tag=TAG))]
    assert all("--query" in probe for probe in probes), (
        "--force does not ask whether the tag is already there; it pushes"
    )


def test_only_the_absent_services_are_pushed(cluster):
    build_manifest()
    present(cluster, "frontend", "mcp")

    images.publish(list(core.IMAGES))

    pushed = [line for line in cluster.lines() if line.startswith("docker push")]
    assert pushed == [
        f"docker push {REPOSITORIES['agent']}:{TAG}",
        f"docker push {REPOSITORIES['frontend-proxy']}:{TAG}",
    ]


# --- the manifest merge ---------------------------------------------------------------------


def test_publishing_one_service_merges_into_the_published_section(cluster):
    build_manifest()
    present(cluster)

    images.publish(["frontend"])
    assert set(images.read_manifest()["published"]) == {"frontend"}

    images.publish(["agent"])

    published = images.read_manifest()["published"]
    assert set(published) == {"frontend", "agent"}, "publishing agent must not unpublish frontend"
    assert published["frontend"]["digest"] == DIGESTS["frontend"]
    assert published["agent"]["repository"] == REPOSITORIES["agent"]


def test_record_published_keeps_the_rest_of_the_manifest(cluster):
    build_manifest()

    images.record_published({"agent": {"repository": "r", "tag": TAG, "digest": "sha256:aa"}})
    images.record_published({"mcp": {"repository": "r2", "tag": TAG, "digest": "sha256:bb"}})

    manifest = images.read_manifest()
    assert manifest["tag"] == TAG and manifest["platform"] == PLATFORM
    assert set(manifest["images"]) == set(core.IMAGES), "the build records survive a publish"
    assert set(manifest["published"]) == {"agent", "mcp"}


# --- refusals before any push ----------------------------------------------------------------


def test_publish_refuses_an_unknown_service(cluster):
    with pytest.raises(SystemExit, match="nosuchservice"):
        images.publish(["nosuchservice"])


def test_publish_refuses_without_a_manifest_or_without_the_local_image(cluster, monkeypatch):
    with pytest.raises(SystemExit) as unbuilt:
        images.publish(["frontend"])
    assert "demo.py build" in str(unbuilt.value)
    assert str(images.manifest_path()) in str(unbuilt.value)

    build_manifest(services=["frontend"])
    with pytest.raises(SystemExit, match=r"agent \(never built\)"):
        images.publish(["frontend", "agent"])

    monkeypatch.setattr(images, "image_exists", lambda image: False)
    with pytest.raises(SystemExit) as gone:
        images.publish(["frontend"])
    assert f"astronomy-concierge-frontend:{TAG}" in str(gone.value)
    assert not any(call.argv[0] == "docker" for call in cluster.calls)


def test_publish_refuses_a_service_with_no_ecr_repository(cluster):
    build_manifest()
    present(cluster)
    partial = OUTPUTS | {
        "ecr_repository_urls": {
            "value": {service: REPOSITORIES[service] for service in ("frontend", "agent")}
        }
    }
    cluster.reply("tofu", stdout=json.dumps(partial))

    with pytest.raises(SystemExit) as failure:
        images.publish(list(core.IMAGES))

    message = str(failure.value)
    assert "mcp" in message and "frontend-proxy" in message
    assert "eks apply" in message, message


# --- the digest read ------------------------------------------------------------------------


def test_ecr_image_digest_queries_the_registry_and_accepts_a_repository_url(cluster):
    digest = aws.ecr_image_digest(REPOSITORIES["frontend"], TAG)

    assert digest == DIGESTS["frontend"]
    assert cluster.last.argv == [
        "aws",
        "ecr",
        "describe-images",
        "--repository-name",
        "ch-opentelemetry-demo/frontend",
        "--image-ids",
        f"imageTag={TAG}",
        "--query",
        "imageDetails[0].imageDigest",
        "--output",
        "text",
    ], "the registry host is not part of the repository name"


def test_ecr_image_digest_refuses_the_cli_s_word_for_nothing(cluster):
    cluster.reply(DIGEST_QUERY.format(service="frontend", tag=TAG), stdout="None\n")

    with pytest.raises(SystemExit) as failure:
        aws.ecr_image_digest(REPOSITORIES["frontend"], TAG)

    assert "ch-opentelemetry-demo/frontend" in str(failure.value)
    assert TAG in str(failure.value)


def test_ecr_image_digest_refuses_an_error_exit_rather_than_raising(cluster):
    """`describe-images` errors on a tag it cannot find; that is the reachable failure.

    A tag deleted between the presence probe and the digest read, an expired session and a
    throttle all land here, and `SystemExit` rather than `CalledProcessError` is the assertion:
    `fake_sh` raises the latter for a non-zero exit whenever the caller left `check` on.
    """
    cluster.reply(DIGEST_QUERY.format(service="frontend", tag=TAG), returncode=254)

    with pytest.raises(SystemExit) as failure:
        aws.ecr_image_digest(REPOSITORIES["frontend"], TAG)

    assert "ch-opentelemetry-demo/frontend" in str(failure.value)
    assert TAG in str(failure.value)


# --- the deploy's gate ----------------------------------------------------------------------


STALE = "0000deadbeef"
OLDER = "1111feedface"


@pytest.fixture
def gate(cluster, monkeypatch):
    """The manifest, with the current build tag pinned rather than computed.

    `build_tag()` hashes the pinned upstream checkout through `core.capture`, which `fake_sh`
    has replaced for the duration; what the tag is computed from is tests/test_build.py's
    subject, and all this gate cares about is whether the published tag equals it.
    """
    monkeypatch.setattr(images, "build_tag", lambda: TAG)
    return cluster


def published_at(tag, services=None):
    """A `published` section for `services` at `tag`, as `publish` would have written it."""
    services = core.IMAGES if services is None else services
    return {
        service: {"repository": REPOSITORIES[service], "tag": tag, "digest": DIGESTS[service]}
        for service in services
    }


def test_require_published_rejects_nothing_absent_and_stale_with_distinct_messages(gate):
    messages = {}

    with pytest.raises(SystemExit) as no_manifest:
        images.require_published()
    messages["no manifest"] = str(no_manifest.value)
    assert str(images.manifest_path()) in messages["no manifest"]

    build_manifest()
    with pytest.raises(SystemExit) as nothing:
        images.require_published()
    messages["nothing published"] = str(nothing.value)

    images.record_published(published_at(TAG, ["frontend", "agent", "mcp"]))
    with pytest.raises(SystemExit) as absent:
        images.require_published()
    messages["one absent"] = str(absent.value)
    assert "frontend-proxy" in messages["one absent"]
    assert "frontend-proxy" not in messages["nothing published"], (
        "a service absent from a published set names itself; nothing published cannot"
    )

    images.record_published(published_at(STALE))
    with pytest.raises(SystemExit) as stale:
        images.require_published()
    messages["stale"] = str(stale.value)
    assert STALE in messages["stale"] and TAG in messages["stale"], (
        "the published tag and the current one, so the operator can see which way round it is"
    )

    assert len(set(messages.values())) == len(messages), messages
    for case, message in messages.items():
        assert "demo.py publish" in message, case


def test_require_published_names_every_distinct_stale_tag(gate):
    """Publishing service by service as the inputs move leaves more than one stale tag."""
    build_manifest()
    published = published_at(TAG)
    published["frontend"]["tag"] = STALE
    published["agent"]["tag"] = OLDER
    images.record_published(published)

    with pytest.raises(SystemExit) as failure:
        images.require_published()

    message = str(failure.value)
    assert STALE in message and OLDER in message, message
    assert "frontend" in message and "agent" in message, message


def test_require_published_returns_the_section_when_every_service_is_current(gate):
    build_manifest()
    images.record_published(published_at(TAG))

    assert images.require_published() == published_at(TAG)


def test_require_published_rejects_an_entry_that_is_missing_a_field(gate):
    build_manifest()
    incomplete = published_at(TAG)
    del incomplete["mcp"]["digest"]
    images.record_published(incomplete)

    with pytest.raises(SystemExit, match="mcp"):
        images.require_published()


def test_publish_records_what_the_gate_then_accepts(gate):
    """The round trip the EKS runbook depends on: build, publish, deploy."""
    build_manifest()
    present(gate)

    images.publish(list(core.IMAGES))

    assert images.require_published() == published_at(TAG)
