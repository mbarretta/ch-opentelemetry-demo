"""launcher.eks.aws: the SSO session, the OpenTofu outputs, ECR, the node group, the schedule.

Every test runs offline through `fake_sh`, so what is asserted is the argv (and what went in on
stdin) rather than anything about AWS.
"""

import json
import os

import pytest

from launcher import core
from launcher.eks import aws, config

REGISTRY = "111122223333.dkr.ecr.us-east-1.amazonaws.com"

# `tofu output -json` as the CLI prints it: every output wrapped in its type and value, which is
# why aws.tf_outputs() unwraps `value` before handing the mapping out.
OUTPUTS = {
    "region": {"value": "us-east-1"},
    "cluster_name": {"value": "otel-demo-eks"},
    "nodegroup_name": {"value": "otel-demo-eks-demo"},
    "node_count": {"value": 2},
    "node_platform": {"value": "linux/arm64"},
    "ecr_registry": {"value": REGISTRY},
    "ecr_repository_urls": {
        "value": {
            "frontend": REGISTRY + "/ch-opentelemetry-demo/frontend",
            "agent": REGISTRY + "/ch-opentelemetry-demo/agent",
        }
    },
    "scheduler_name": {"value": "otel-demo-eks-nightly-scale-down"},
}

# The two `describe-nodegroup` reads share a prefix; the longer one wins in the answer table,
# which is how one fake shell answers both the status and the desired-size question.
NG_STATUS_QUERY = "aws eks describe-nodegroup --cluster-name otel-demo-eks"
NG_DESIRED_QUERY = (
    "aws eks describe-nodegroup --cluster-name otel-demo-eks --nodegroup-name "
    "otel-demo-eks-demo --query nodegroup.scalingConfig.desiredSize"
)

SCHEDULE = {
    "Name": "otel-demo-eks-nightly-scale-down",
    "Description": "Scale the otel-demo-eks demo node group to 0 every night.",
    "ScheduleExpression": "cron(0 20 * * ? *)",
    "ScheduleExpressionTimezone": "America/New_York",
    "State": "ENABLED",
    "FlexibleTimeWindow": {"Mode": "OFF"},
    "Target": {
        "Arn": "arn:aws:scheduler:::aws-sdk:eks:updateNodegroupConfig",
        "RoleArn": "arn:aws:iam::111122223333:role/otel-demo-eks-scheduler",
        "Input": '{"ClusterName":"otel-demo-eks","NodegroupName":"demo"}',
    },
}


@pytest.fixture
def outputs(fake_sh, monkeypatch):
    """A `tofu output -json` answer, with the process-wide output cache emptied first.

    The cache is what AC "one `tofu output` per process" is about, so it has to start empty in
    every test and must not leak into the next one; monkeypatch restores it either way.
    """
    monkeypatch.setattr(aws, "_OUTPUTS", None)
    fake_sh.reply("tofu", stdout=json.dumps(OUTPUTS))
    return fake_sh


@pytest.fixture
def session(monkeypatch, tmp_path):
    """A checkout with a filled-in `.env` and nothing inherited from the developer's shell.

    `aws_login()` exports AWS_PROFILE and AWS_REGION into the real process environment on
    purpose -- that is how kubectl's exec-auth plugin sees them -- so the fixture saves and
    restores both itself rather than leaving them set for whatever test runs next.
    """
    monkeypatch.setattr(core, "ROOT", tmp_path)
    monkeypatch.setattr(core, "RUNTIME", tmp_path / ".runtime")
    (tmp_path / ".env").write_text("AWS_PROFILE=demo\n")
    saved = {key: os.environ.get(key) for key in ("AWS_PROFILE", "AWS_REGION")}
    for key in saved:
        os.environ.pop(key, None)
    yield tmp_path
    for key, value in saved.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value


# --- the session ---------------------------------------------------------------------------


def test_aws_login_exports_the_profile_and_region_and_reuses_a_valid_session(
    session, fake_sh, monkeypatch
):
    fake_sh.reply("aws configure list-profiles", stdout="default\ndemo\nother\n")

    aws.aws_login()

    assert "aws sso login --profile demo" not in fake_sh.lines(), "the session is still valid"
    assert os.environ["AWS_PROFILE"] == "demo", "inherited by every later aws/tofu/kubectl call"
    assert os.environ["AWS_REGION"] == config.DEFAULT_REGION


def test_aws_login_logs_in_again_when_the_sso_session_has_expired(session, fake_sh):
    fake_sh.reply("aws configure list-profiles", stdout="demo\n")
    # The first identity probe fails and the one after the login succeeds, which is how an
    # expired-then-renewed SSO session reads from outside the browser.
    probes = iter([255, 0])
    table = fake_sh.answer

    def answer(call):
        if call.line.startswith("aws sts get-caller-identity"):
            return "", "the SSO session has expired", next(probes)
        return table(call)

    fake_sh.answer = answer

    aws.aws_login()

    lines = fake_sh.lines()
    assert "aws sso login --profile demo" in lines
    assert lines[-1].startswith("aws sts get-caller-identity"), (
        "the session is probed again after the login rather than assumed to have worked"
    )


def test_aws_login_names_the_fix_when_the_profile_is_unset_or_unknown(session, fake_sh):
    (session / ".env").write_text("AWS_PROFILE=\n")
    with pytest.raises(SystemExit) as unset:
        aws.aws_login()
    assert "AWS_PROFILE" in str(unset.value)

    (session / ".env").write_text("AWS_PROFILE=typo\n")
    fake_sh.reply("aws configure list-profiles", stdout="default\ndemo\n")
    with pytest.raises(SystemExit) as unknown:
        aws.aws_login()
    assert "configure sso" in str(unknown.value), "the message carries the command that fixes it"


def test_aws_login_fails_when_the_login_did_not_take(session, fake_sh):
    fake_sh.reply("aws configure list-profiles", stdout="demo\n")
    fake_sh.reply("aws sts get-caller-identity", returncode=255)

    with pytest.raises(SystemExit) as failure:
        aws.aws_login()

    assert "demo" in str(failure.value)


def test_state_bucket_is_the_prefix_plus_the_account_id(fake_sh):
    fake_sh.reply("aws sts get-caller-identity --query Account", stdout="111122223333\n")

    assert aws.state_bucket() == config.STATE_BUCKET_PREFIX + "111122223333"


# --- OpenTofu outputs ---------------------------------------------------------------------


def test_tofu_output_is_read_once_per_process_however_many_callers_ask(outputs):
    assert aws.tf_out("cluster_name") == "otel-demo-eks"
    assert aws.tf_out("nodegroup_name") == "otel-demo-eks-demo"
    assert aws.tf_outputs()["ecr_registry"].endswith(".amazonaws.com")

    tofu_calls = [line for line in outputs.lines() if line.startswith("tofu")]
    assert len(tofu_calls) == 1, "the cache answers every question after the first"
    assert tofu_calls[0] == f"tofu -chdir={config.tofu_dir()} output -json"


def test_refresh_re_reads_the_outputs_after_an_apply(outputs):
    aws.tf_outputs()
    aws.tf_outputs(refresh=True)

    assert len([line for line in outputs.lines() if line.startswith("tofu")]) == 2


def test_map_and_list_outputs_survive_because_the_read_is_json(outputs):
    assert aws.tf_out("ecr_repository_urls")["agent"].endswith("/ch-opentelemetry-demo/agent")
    assert aws.tf_out("node_count") == 2, "not the string `-raw` would have printed"


def test_a_failed_read_keeps_tofus_reason_next_to_the_apply_hint(fake_sh, monkeypatch):
    monkeypatch.setattr(aws, "_OUTPUTS", None)
    fake_sh.reply("tofu", stderr="Error: Backend initialization required\n", returncode=1)

    with pytest.raises(SystemExit) as failure:
        aws.tf_outputs()

    message = str(failure.value)
    assert "Backend initialization required" in message
    assert "eks apply" in message


def test_an_unknown_output_is_named_rather_than_returned_as_none(outputs):
    with pytest.raises(SystemExit) as failure:
        aws.tf_out("no_such_output")

    assert "no_such_output" in str(failure.value)


# --- ECR ----------------------------------------------------------------------------------


def test_ecr_login_keeps_the_password_on_stdin_and_out_of_argv(outputs):
    outputs.reply("aws ecr get-login-password", stdout="sensitive-ecr-password\n")

    registry = aws.ecr_login()

    assert registry == OUTPUTS["ecr_registry"]["value"]
    login = [call for call in outputs.calls if call.argv[0] == "docker"][0]
    assert login.argv == [
        "docker",
        "login",
        "--username",
        "AWS",
        "--password-stdin",
        registry,
    ]
    assert login.stdin == "sensitive-ecr-password"
    assert not any("sensitive" in argument for call in outputs.calls for argument in call.argv), (
        "the password reaches docker on stdin only; argv is readable by every local user"
    )


def test_ecr_has_image_reads_the_probes_status_and_accepts_a_repository_url(outputs):
    url = OUTPUTS["ecr_repository_urls"]["value"]["frontend"]
    outputs.reply("aws ecr describe-images", returncode=254)
    assert aws.ecr_has_image(url, "58acc12afb99") is False, "a missing tag is a plain no"
    assert outputs.last.argv == [
        "aws",
        "ecr",
        "describe-images",
        "--repository-name",
        "ch-opentelemetry-demo/frontend",
        "--image-ids",
        "imageTag=58acc12afb99",
    ], "the registry host is not part of the repository name"

    outputs.reply("aws ecr describe-images", returncode=0)
    assert aws.ecr_has_image("ch-opentelemetry-demo/frontend", "58acc12afb99") is True

    aws.ecr_has_image("my.app/frontend", "58acc12afb99")
    assert outputs.last.argv[4] == "my.app/frontend", (
        "a dot is legal in a repository name, so only a registry host is stripped"
    )


# --- the managed node group ---------------------------------------------------------------


def test_ng_desired_and_ng_status_read_one_field_each(outputs):
    outputs.reply("aws eks describe-nodegroup", stdout="2\n")
    assert aws.ng_desired() == 2
    assert outputs.last.argv[-4:] == [
        "--query",
        "nodegroup.scalingConfig.desiredSize",
        "--output",
        "text",
    ]

    outputs.reply("aws eks describe-nodegroup", stdout="ACTIVE\n")
    assert aws.ng_status() == "ACTIVE"


def test_ng_scale_is_a_no_op_when_the_size_is_already_what_was_asked_for(outputs):
    outputs.reply(NG_STATUS_QUERY, stdout="ACTIVE\n")
    outputs.reply(NG_DESIRED_QUERY, stdout="2\n")

    aws.ng_scale(2)

    assert not [line for line in outputs.lines() if "update-nodegroup-config" in line], (
        "EKS rejects an update that changes nothing"
    )
    assert not [line for line in outputs.lines() if "wait nodegroup-active" in line]


def test_ng_scale_waits_out_an_update_in_flight_before_asking_for_another(outputs):
    outputs.reply(NG_STATUS_QUERY, stdout="UPDATING\n")
    outputs.reply(NG_DESIRED_QUERY, stdout="0\n")

    aws.ng_scale(2)

    lines = outputs.lines()
    waits = [index for index, line in enumerate(lines) if "wait nodegroup-active" in line]
    updates = [index for index, line in enumerate(lines) if "update-nodegroup-config" in line]
    assert waits and updates, "it waited and then scaled"
    assert waits[0] < updates[0], (
        "scaling a node group that is still UPDATING is a ResourceInUseException"
    )
    assert waits[-1] > updates[0], "and it waits for the new size to be in place too"
    assert "--scaling-config minSize=0,maxSize=2,desiredSize=2" in lines[updates[0]], (
        "maxSize stays at node_count so scaling back up needs no second change"
    )


# --- the nightly schedule -----------------------------------------------------------------


def test_scheduler_set_state_feeds_every_field_back_because_update_replaces_the_schedule(outputs):
    outputs.reply("aws scheduler get-schedule", stdout=json.dumps(SCHEDULE))

    aws.scheduler_set_state("off")

    update = [call for call in outputs.calls if "update-schedule" in call.line][0]
    # Everything after `aws scheduler update-schedule` is flag/value pairs.
    pairs = dict(zip(update.argv[3::2], update.argv[4::2]))
    assert update.argv[:4] == [
        "aws",
        "scheduler",
        "update-schedule",
        "--name",
    ]
    assert pairs["--state"] == "DISABLED", "`off` is the CLI's word; DISABLED is the API's"
    assert pairs["--schedule-expression"] == SCHEDULE["ScheduleExpression"]
    assert pairs["--schedule-expression-timezone"] == SCHEDULE["ScheduleExpressionTimezone"]
    assert json.loads(pairs["--flexible-time-window"]) == SCHEDULE["FlexibleTimeWindow"]
    assert json.loads(pairs["--target"]) == SCHEDULE["Target"]
    assert pairs["--description"] == SCHEDULE["Description"]


def test_scheduler_set_state_takes_on_off_or_the_api_words_and_refuses_anything_else(outputs):
    outputs.reply("aws scheduler get-schedule", stdout=json.dumps(SCHEDULE))

    aws.scheduler_set_state("ENABLED")
    update = [call for call in outputs.calls if "update-schedule" in call.line][0]
    assert "ENABLED" in update.argv

    with pytest.raises(SystemExit):
        aws.scheduler_set_state("maybe")


def test_scheduler_get_returns_the_schedule_as_the_api_describes_it(outputs):
    outputs.reply("aws scheduler get-schedule", stdout=json.dumps(SCHEDULE))

    assert aws.scheduler_get()["State"] == "ENABLED"
    assert outputs.last.argv == [
        "aws",
        "scheduler",
        "get-schedule",
        "--name",
        OUTPUTS["scheduler_name"]["value"],
        "--output",
        "json",
    ]


# --- the state bucket ---------------------------------------------------------------------


def test_purge_state_bucket_deletes_versions_in_batches_and_the_bucket_last(fake_sh):
    versions = [{"Key": f"k{index}", "VersionId": f"v{index}"} for index in range(1500)]
    listings = iter(
        [
            json.dumps({"Versions": versions[:1000], "DeleteMarkers": versions[1000:]}),
            json.dumps({"Versions": [], "DeleteMarkers": []}),
        ]
    )

    def answer(call):
        if "list-object-versions" in call.line:
            return next(listings), "", 0
        return "", "", 0

    fake_sh.answer = answer

    aws.purge_state_bucket("otel-demo-eks-tfstate-111122223333")

    deletes = [call for call in fake_sh.calls if "delete-objects" in call.line]
    assert len(deletes) == 1
    payload = json.loads(deletes[0].argv[deletes[0].argv.index("--delete") + 1])
    assert len(payload["Objects"]) == 1000, "delete-objects takes at most 1000 keys per call"
    assert payload["Quiet"] is True
    assert payload["Objects"][0] == {"Key": "k0", "VersionId": "v0"}
    assert fake_sh.lines()[-1] == (
        "aws s3api delete-bucket --bucket otel-demo-eks-tfstate-111122223333"
    ), "the bucket goes last: delete-bucket fails with BucketNotEmpty while a version is left"


def test_purge_state_bucket_stops_rather_than_spinning_when_a_version_will_not_delete(fake_sh):
    """delete-objects exits 0 while reporting per-key failures, so no progress has to be fatal."""
    fake_sh.reply(
        "aws s3api list-object-versions",
        stdout=json.dumps({"Versions": [{"Key": "locked", "VersionId": "v1"}]}),
    )

    with pytest.raises(SystemExit) as failure:
        aws.purge_state_bucket("otel-demo-eks-tfstate-111122223333")

    assert "locked" in str(failure.value)
    assert not [line for line in fake_sh.lines() if "delete-bucket" in line], (
        "the bucket is still not empty, so deleting it would fail with BucketNotEmpty"
    )
