"""launcher.eks.aws: the SSO session, the OpenTofu outputs, ECR, the node group, the schedule.

Every test runs offline through `fake_sh`, so what is asserted is the argv (and what went in on
stdin) rather than anything about AWS.
"""

import json
import os

import pytest

from launcher.eks import aws, config

REGISTRY = "111122223333.dkr.ecr.us-east-1.amazonaws.com"

# The `.env` every test here runs against: one profile, the one `aws_login()` is asked for.
ENV = {"AWS_PROFILE": "demo"}

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
def redirect_env():
    """The `.env` the shared `redirected` fixture writes for these tests.

    `aws_login()` exports AWS_PROFILE and AWS_REGION into the real process environment on
    purpose -- that is how kubectl's exec-auth plugin sees them -- and the shared fixture takes
    both back out again rather than leaving them set for whatever test runs next.
    """
    return ENV


# --- the session ---------------------------------------------------------------------------


def test_aws_login_exports_the_profile_and_region_and_reuses_a_valid_session(
    redirected, fake_sh, monkeypatch
):
    fake_sh.reply("aws configure list-profiles", stdout="default\ndemo\nother\n")

    aws.aws_login()

    assert "aws sso login --profile demo" not in fake_sh.lines(), "the session is still valid"
    assert os.environ["AWS_PROFILE"] == "demo", "inherited by every later aws/tofu/kubectl call"
    assert os.environ["AWS_REGION"] == config.DEFAULT_REGION


def test_aws_login_logs_in_again_when_the_sso_session_has_expired(redirected, fake_sh):
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


def test_aws_login_names_the_fix_when_the_profile_is_unset_or_unknown(redirected, fake_sh):
    (redirected / ".env").write_text("AWS_PROFILE=\n")
    with pytest.raises(SystemExit) as unset:
        aws.aws_login()
    assert "AWS_PROFILE" in str(unset.value)

    (redirected / ".env").write_text("AWS_PROFILE=typo\n")
    fake_sh.reply("aws configure list-profiles", stdout="default\ndemo\n")
    with pytest.raises(SystemExit) as unknown:
        aws.aws_login()
    assert "configure sso" in str(unknown.value), "the message carries the command that fixes it"


def test_aws_login_fails_when_the_login_did_not_take(redirected, fake_sh):
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
