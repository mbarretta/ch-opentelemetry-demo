"""launcher.eks.infra: the state bucket, the cluster lifecycle, and the nightly schedule.

These four commands are the ones where a wrong argv costs real money or real state: an orphaned
control plane, a state bucket deleted while it still held the only copy of the state, a nightly
schedule silently stripped of its target. So the assertions here are about argv and ordering,
not about return values.
"""

import json
import shlex

import pytest
from conftest import Recorder, write_env

from launcher import core
from launcher.eks import aws, config, infra, k8s, lifecycle, tunnel

ACCOUNT = "123456789012"
BUCKET = config.STATE_BUCKET_PREFIX + ACCOUNT
CLUSTER = "otel-demo-eks"

# The shape `aws scheduler get-schedule --output json` returns. Every writable field the API can
# answer with is present, including the ones `deploy/eks/tofu/scheduler.tf` does not set
# (KmsKeyArn, StartDate, EndDate): a schedule that has them must not lose them to a state flip.
SCHEDULE = {
    "Arn": f"arn:aws:scheduler:us-east-1:{ACCOUNT}:schedule/default/{CLUSTER}-nightly-scale-down",
    "Name": f"{CLUSTER}-nightly-scale-down",
    "GroupName": "default",
    "ScheduleExpression": "cron(0 3 * * ? *)",
    "ScheduleExpressionTimezone": "America/New_York",
    "Description": f"Scale the {CLUSTER} demo node group to 0 every night.",
    "State": "ENABLED",
    "KmsKeyArn": f"arn:aws:kms:us-east-1:{ACCOUNT}:key/1a2b3c4d-5e6f-7081-92a3-b4c5d6e7f809",
    "StartDate": "2026-09-09T12:00:00+00:00",
    "EndDate": "2027-09-09T12:00:00+00:00",
    "ActionAfterCompletion": "NONE",
    "FlexibleTimeWindow": {"Mode": "OFF"},
    "Target": {
        "Arn": "arn:aws:scheduler:::aws-sdk:eks:updateNodegroupConfig",
        "RoleArn": f"arn:aws:iam::{ACCOUNT}:role/{CLUSTER}-scheduler",
        "Input": json.dumps(
            {
                "ClusterName": CLUSTER,
                "NodegroupName": "demo",
                "ScalingConfig": {"MinSize": 0, "MaxSize": 2, "DesiredSize": 0},
            }
        ),
        "RetryPolicy": {"MaximumEventAgeInSeconds": 3600, "MaximumRetryAttempts": 3},
    },
    "CreationDate": "2026-09-09T12:00:00.123000+00:00",
    "LastModificationDate": "2026-09-10T03:00:00.456000+00:00",
}

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

TF_OUTPUTS = {"cluster_name": CLUSTER, "scheduler_name": SCHEDULE["Name"]}


# The collaborators outside this module, recorded rather than run. What they were passed is the
# assertion here, which is `args()`; the same body records process positions for
# tests/test_eks_deploy.py, where it is `Steps`.
Stubs = Recorder


@pytest.fixture
def redirect_env():
    """The `.env` the shared `redirected` fixture writes, so no real one is read.

    Full ClickStack and full Langfuse: `init` and `apply` need neither to succeed, but a fully
    configured `.env` is the default state these tests are otherwise about, so a test that wants
    a blank key writes its own `.env` with `write_env` instead of relying on this default.
    """
    return {**FULL_CLICKSTACK, **FULL_LANGFUSE}


@pytest.fixture
def stubbed(monkeypatch):
    """AWS identity, the cluster, the tunnel and the workloads: all faked, all recorded."""
    stubs = Stubs()
    stubs.patch(monkeypatch, core, "need")
    stubs.patch(monkeypatch, aws, "aws_login")
    stubs.patch(monkeypatch, aws, "state_bucket", result=BUCKET)
    stubs.patch(monkeypatch, aws, "tf_out", result=lambda name: TF_OUTPUTS[name])
    stubs.patch(monkeypatch, aws, "scheduler_get", result=dict(SCHEDULE))
    stubs.patch(monkeypatch, k8s, "helm_repo_ensure")
    stubs.patch(monkeypatch, k8s, "kubeconfig")
    stubs.patch(monkeypatch, tunnel, "stop")
    stubs.patch(monkeypatch, lifecycle, "down")
    return stubs


def initialised(root):
    """Leave behind what `tofu init` leaves behind: the S3 backend's state file."""
    backend = root / "deploy/eks/tofu/.terraform"
    backend.mkdir(parents=True)
    (backend / "terraform.tfstate").write_text('{"backend": {"type": "s3"}}\n')
    return backend


def find(shell, *prefix):
    """Every recorded call whose argv starts with `prefix`."""
    return [call for call in shell.calls if call.argv[: len(prefix)] == list(prefix)]


def one(shell, *prefix):
    matched = find(shell, *prefix)
    assert len(matched) == 1, f"expected exactly one {' '.join(prefix)} call, got {matched}"
    return matched[0]


def positions(shell, *prefix):
    return [
        index for index, call in enumerate(shell.calls) if call.argv[: len(prefix)] == list(prefix)
    ]


def answers_in_turn(monkeypatch, shell, prefix, outputs):
    """Answer `prefix` with each output in turn, repeating the last one.

    The fake shell answers a prefix with one fixed output, which a loop that reads until the
    answer is empty would never leave. This is what lets `purge_bucket`'s real loop run.
    """
    remaining = list(outputs)
    capture = shell.capture

    def sequenced(*args, **kwargs):
        if shlex.join(str(argument) for argument in args).startswith(prefix):
            shell.reply(prefix, remaining.pop(0) if len(remaining) > 1 else remaining[0])
        return capture(*args, **kwargs)

    monkeypatch.setattr(core, "capture", sequenced)


def object_listing(versions, markers):
    """A `list-object-versions` answer with that many object versions and delete markers."""
    return {
        "Versions": [
            {"Key": "otel-demo-eks/terraform.tfstate", "VersionId": f"version-{index}"}
            for index in range(versions)
        ],
        "DeleteMarkers": [
            {"Key": "otel-demo-eks/terraform.tfstate", "VersionId": f"marker-{index}"}
            for index in range(markers)
        ],
    }


# --- init ----------------------------------------------------------------------------------


def test_init_requires_every_command_the_whole_deployment_uses(fake_sh, stubbed, redirected):
    """Named up front, on the one command a presenter runs before anything else."""
    infra.init()

    assert stubbed.args("need") == [("aws", "tofu", "docker", "git", "kubectl", "helm")]


def test_init_creates_the_state_bucket_without_a_location_constraint_in_us_east_1(
    fake_sh, stubbed, redirected
):
    """us-east-1 rejects an explicit LocationConstraint, which is a hard create-bucket failure."""
    fake_sh.reply("aws s3api head-bucket", returncode=255)

    infra.init()

    create = one(fake_sh, "aws", "s3api", "create-bucket")
    assert create.argv == [
        "aws", "s3api", "create-bucket", "--bucket", BUCKET, "--region", "us-east-1",
    ]
    assert "--create-bucket-configuration" not in create.argv


def test_init_names_the_location_constraint_outside_us_east_1(fake_sh, stubbed, redirected):
    write_env(redirected, {**FULL_CLICKSTACK, "AWS_REGION": "eu-west-1"})
    fake_sh.reply("aws s3api head-bucket", returncode=255)

    infra.init()

    create = one(fake_sh, "aws", "s3api", "create-bucket")
    assert create.argv[-2:] == ["--create-bucket-configuration", "LocationConstraint=eu-west-1"]
    assert create.argv[create.argv.index("--region") + 1] == "eu-west-1"
    assert "-backend-config=region=eu-west-1" in one(fake_sh, "tofu", *chdir(), "init").argv


def test_init_does_not_recreate_a_bucket_that_already_exists(fake_sh, stubbed, redirected):
    """Re-running init is the documented way to repair a checkout, so it has to be a no-op."""
    fake_sh.reply("aws s3api head-bucket", returncode=0)

    infra.init()

    assert find(fake_sh, "aws", "s3api", "create-bucket") == []
    # Still hardened: an interrupted first run can leave an unversioned bucket behind, and an
    # unversioned bucket is one where a clobbered state file is gone for good.
    assert one(fake_sh, "aws", "s3api", "put-bucket-versioning").argv[-1] == "Status=Enabled"
    block = one(fake_sh, "aws", "s3api", "put-public-access-block").argv[-1]
    assert block == (
        "BlockPublicAcls=true,IgnorePublicAcls=true,"
        "BlockPublicPolicy=true,RestrictPublicBuckets=true"
    )


def chdir():
    """The `-chdir=` argument every tofu call in this module carries."""
    return (f"-chdir={config.tofu_dir()}",)


def test_init_supplies_the_backend_the_live_cluster_already_uses(fake_sh, stubbed, redirected):
    """Constraint 3: move the bucket or the key and the state of the running cluster is orphaned."""
    infra.init()

    init = one(fake_sh, "tofu", *chdir(), "init")
    assert init.argv == [
        "tofu",
        f"-chdir={config.tofu_dir()}",
        "init",
        "-input=false",
        f"-backend-config=bucket={BUCKET}",
        "-backend-config=key=otel-demo-eks/terraform.tfstate",
        "-backend-config=region=us-east-1",
    ]
    # The names come from config and the discovered account, not from anything spelled here.
    assert f"-backend-config=bucket={config.STATE_BUCKET_PREFIX}{ACCOUNT}" in init.argv
    assert f"-backend-config=key={config.STATE_KEY}" in init.argv
    assert stubbed.called("state_bucket")


def test_init_registers_the_chart_repository_and_reports_the_next_command(
    fake_sh, stubbed, redirected, capsys
):
    infra.init()

    assert stubbed.called("helm_repo_ensure")
    assert stubbed.called("aws_login")
    out = capsys.readouterr()
    assert "Next: demo.py eks apply" in out.out
    assert out.err == "", "a fully filled-in .env warns about nothing"


def test_init_warns_about_blank_clickstack_keys_rather_than_exiting(
    fake_sh, stubbed, redirected, capsys
):
    """AC7: the collector keys are a deploy-time requirement, not an init-time one."""
    write_env(
        redirected,
        {**FULL_CLICKSTACK, **FULL_LANGFUSE, "CLICKHOUSE_PASSWORD": "", "OTLP_AUTH_TOKEN": ""},
    )

    infra.init()

    out = capsys.readouterr()
    assert "CLICKHOUSE_PASSWORD" in out.err and "OTLP_AUTH_TOKEN" in out.err
    assert "CLICKHOUSE_USER" not in out.err, "a key that is filled in must not be reported"
    assert "create-user.sql" in out.err
    assert "init complete" in out.out, "the warning must not stop init"
    assert "then run: demo.py eks apply" in out.out
    assert find(fake_sh, "tofu", *chdir(), "init"), "the backend is still initialised"


# --- apply ---------------------------------------------------------------------------------


def test_apply_refuses_before_init_and_never_reaches_tofu_apply(fake_sh, stubbed, redirected):
    """AC4: `tofu apply` on an uninitialised backend fails with a message nobody can act on."""
    (redirected / "deploy/eks/tofu/.terraform").mkdir(parents=True)  # `eks check` leaves this

    with pytest.raises(SystemExit) as failure:
        infra.apply()

    assert "eks init" in str(failure.value)
    assert find(fake_sh, "tofu", *chdir(), "apply") == []
    assert not stubbed.called("aws_login"), "no session is established for a refused apply"


def test_apply_runs_tofu_apply_interactively_and_reports_the_next_steps(
    fake_sh, stubbed, redirected, capsys
):
    """AC8: tofu's own approval prompt reads the inherited stdin, so nothing may redirect it."""
    initialised(redirected)

    infra.apply()

    apply = one(fake_sh, "tofu", *chdir(), "apply")
    assert apply.argv == ["tofu", f"-chdir={config.tofu_dir()}", "apply"]
    assert apply.kwargs == {}, "stdin, stdout and stderr all stay inherited"
    assert "-input=false" not in apply.argv and "-auto-approve" not in apply.argv

    assert stubbed.called("kubeconfig")
    assert one(fake_sh, "kubectl", "get", "nodes").argv == ["kubectl", "get", "nodes", "-o", "wide"]
    assert find(fake_sh, "tofu", *chdir(), "output")
    assert not stubbed.called("ng_scale"), "a first apply has no idle node group to protect"

    out = capsys.readouterr()
    for step in ("demo.py build", "demo.py publish", "demo.py eks deploy"):
        assert step in out.out, step
    assert out.err == "", "a fully filled-in .env warns about nothing"


def test_apply_warns_about_blank_clickstack_and_langfuse_keys_rather_than_exiting(
    fake_sh, stubbed, redirected, capsys
):
    """AC2: `apply` calls the same `warn_blank_keys` `init` does, extended for Langfuse.

    Still a warning, not a refusal: `apply` is infrastructure-only and genuinely does not need
    either credential to succeed, unlike `eks deploy`.
    """
    initialised(redirected)
    write_env(
        redirected,
        {
            **FULL_CLICKSTACK,
            **FULL_LANGFUSE,
            "OTLP_AUTH_TOKEN": "",
            "LANGFUSE_SECRET_KEY": "",
        },
    )

    infra.apply()

    out = capsys.readouterr()
    assert "OTLP_AUTH_TOKEN" in out.err and "LANGFUSE_SECRET_KEY" in out.err
    assert "CLICKHOUSE_USER" not in out.err, "a key that is filled in must not be reported"
    assert find(fake_sh, "tofu", *chdir(), "apply"), "the warning must not stop apply"


def test_apply_yes_skips_the_confirmation_prompt(fake_sh, stubbed, redirected):
    initialised(redirected)

    infra.apply(yes=True)

    assert one(fake_sh, "tofu", *chdir(), "apply").argv[-1] == "-auto-approve"


def test_apply_scales_an_idle_node_group_up_for_addon_updates_and_back_down_after(
    fake_sh, stubbed, redirected, monkeypatch
):
    """A coredns/kube-proxy/vpc-cni version bump needs somewhere to reschedule pods: at
    desiredSize=0 there is none, and `tofu apply` blocks for the add-on's 20-minute timeout and
    then fails (this is what actually happened against a live idle cluster)."""
    initialised(redirected)
    fake_sh.reply(
        shlex.join(["tofu", *chdir(), "output", "-json"]),
        stdout=json.dumps({**TF_OUTPUTS, "nodegroup_name": "demo", "node_count": 2}),
    )
    stubbed.patch(monkeypatch, aws, "ng_status", result="ACTIVE")
    stubbed.patch(monkeypatch, aws, "ng_desired", result=0)
    stubbed.patch(monkeypatch, aws, "ng_scale")
    stubbed.patch(
        monkeypatch, aws, "tf_out", result=lambda name: {**TF_OUTPUTS, "node_count": 2}[name]
    )

    infra.apply()

    assert stubbed.args("ng_scale") == [(2,), (0,)], "up to node_count first, back to 0 after"


def test_apply_warns_that_the_ecr_consolidation_replaces_the_old_repository(
    fake_sh, stubbed, redirected, capsys
):
    """The ECR name is ForceNew and there is no `moved` block: the images go with it."""
    initialised(redirected)

    infra.apply()

    printed = capsys.readouterr().out
    assert "otel-demo-frontend" in printed
    assert "ch-opentelemetry-demo/" in printed
    assert "re-publish" in printed


# --- destroy -------------------------------------------------------------------------------


def test_destroy_closes_the_tunnel_and_takes_the_workloads_down_first(
    fake_sh, stubbed, redirected, capsys
):
    infra.destroy()

    assert stubbed.called("stop"), "the tunnel holds a port-forward against a cluster going away"
    assert stubbed.called("down")
    assert find(fake_sh, "aws", "eks", "describe-cluster"), "down only runs for a live cluster"
    order = stubbed.names()
    assert order.index("stop") < order.index("down"), "the port-forward goes first"
    destroy = one(fake_sh, "tofu", *chdir(), "destroy")
    assert destroy.argv == ["tofu", f"-chdir={config.tofu_dir()}", "destroy"]
    assert destroy.kwargs == {}, "tofu asks for confirmation on the inherited stdin"
    assert "destroy complete" in capsys.readouterr().out


def test_destroy_skips_down_when_the_state_names_no_cluster(
    fake_sh, stubbed, redirected, monkeypatch, capsys
):
    """A half-destroyed or already-destroyed cluster must not stop the destroy."""
    monkeypatch.setattr(aws, "tf_out", stubbed.stub("tf_out", raises=SystemExit("no state")))

    infra.destroy()

    assert not stubbed.called("down")
    assert "skipping down" in capsys.readouterr().out
    assert find(fake_sh, "tofu", *chdir(), "destroy"), "the infrastructure still goes away"


def test_destroy_continues_when_down_fails(fake_sh, stubbed, redirected, monkeypatch, capsys):
    monkeypatch.setattr(lifecycle, "down", stubbed.stub("down", raises=RuntimeError("helm hung")))

    infra.destroy()

    assert "did not complete cleanly" in capsys.readouterr().err
    assert find(fake_sh, "tofu", *chdir(), "destroy")


def test_destroy_keeps_the_state_bucket_without_purge_state(fake_sh, stubbed, redirected):
    """The emptied bucket is what a later init/apply reuses, so plain destroy never touches it."""
    infra.destroy()

    assert find(fake_sh, "aws", "s3api", "list-object-versions") == []
    assert find(fake_sh, "aws", "s3api", "delete-objects") == []
    assert find(fake_sh, "aws", "s3api", "delete-bucket") == []


def test_destroy_purge_state_deletes_every_version_and_marker_in_batches_of_at_most_1000(
    fake_sh, stubbed, redirected, monkeypatch, capsys
):
    """AC6: delete-objects takes at most 1000 keys, and the bucket goes only after the last batch."""
    backend = initialised(redirected)
    listing = object_listing(versions=1500, markers=700)
    answers_in_turn(
        monkeypatch, fake_sh, "aws s3api list-object-versions", [json.dumps(listing), "{}"]
    )

    infra.destroy(purge_state=True)

    payloads = [
        json.loads(call.argv[call.argv.index("--delete") + 1])
        for call in find(fake_sh, "aws", "s3api", "delete-objects")
    ]
    assert [len(payload["Objects"]) for payload in payloads] == [1000, 1000, 200]
    assert all(payload["Quiet"] is True for payload in payloads)

    deleted = [tuple(sorted(entry.items())) for payload in payloads for entry in payload["Objects"]]
    expected = [
        tuple(sorted({"Key": entry["Key"], "VersionId": entry["VersionId"]}.items()))
        for entry in listing["Versions"] + listing["DeleteMarkers"]
    ]
    assert sorted(deleted) == sorted(expected), "every version and every delete marker, once"

    bucket_deleted = positions(fake_sh, "aws", "s3api", "delete-bucket")
    assert len(bucket_deleted) == 1
    assert bucket_deleted[0] > max(positions(fake_sh, "aws", "s3api", "delete-objects"))
    assert not backend.exists(), "the backend config still named the bucket that is now gone"
    assert "To start over" in capsys.readouterr().out


def test_purge_state_keeps_the_bucket_when_a_batch_fails(fake_sh, stubbed, redirected):
    """A reported deletion that did not happen is worse than a failure: the bucket stays."""
    fake_sh.reply("aws s3api list-object-versions", json.dumps(object_listing(3, 0)))
    fake_sh.reply("aws s3api delete-objects", returncode=1)

    with pytest.raises(core.subprocess.CalledProcessError):
        infra.destroy(purge_state=True)

    assert find(fake_sh, "aws", "s3api", "delete-bucket") == []


def test_purge_state_stops_when_a_key_cannot_be_deleted(fake_sh, stubbed, redirected):
    """delete-objects reports an undeletable key in its body and still exits 0.

    Without reading the body the purge loop would re-list the same version for ever and the
    operator would be told nothing.
    """
    fake_sh.reply("aws s3api list-object-versions", json.dumps(object_listing(2, 0)))
    fake_sh.reply(
        "aws s3api delete-objects",
        json.dumps(
            {
                "Errors": [
                    {
                        "Key": "otel-demo-eks/terraform.tfstate",
                        "VersionId": "version-0",
                        "Code": "AccessDenied",
                        "Message": "Access Denied",
                    }
                ]
            }
        ),
    )

    with pytest.raises(SystemExit) as failure:
        infra.destroy(purge_state=True)

    assert "AccessDenied" in str(failure.value)
    assert "still there" in str(failure.value), "say that the state survived"
    assert find(fake_sh, "aws", "s3api", "delete-bucket") == []


def test_purge_state_reports_a_bucket_that_is_already_gone(
    fake_sh, stubbed, redirected, capsys
):
    fake_sh.reply("aws s3api head-bucket", returncode=255)

    infra.destroy(purge_state=True)

    assert "nothing to purge" in capsys.readouterr().out
    assert find(fake_sh, "aws", "s3api", "delete-bucket") == []


def test_delete_batches_covers_both_groups_and_respects_the_limit():
    listing = object_listing(versions=5, markers=4)

    batches = infra.delete_batches(listing, limit=4)

    assert [len(batch) for batch in batches] == [4, 4, 1]
    assert infra.delete_batches({}) == [], "an empty bucket needs no delete-objects call"
    assert infra.DELETE_BATCH_LIMIT == 1000, "the delete-objects API limit"


# --- nightly -------------------------------------------------------------------------------


def passed_flags(call):
    """The `--flag value` pairs of an `aws ... update-schedule` argv."""
    assert call.argv[:3] == ["aws", "scheduler", "update-schedule"]
    return dict(zip(call.argv[3::2], call.argv[4::2], strict=True))


def test_the_schedule_fixture_covers_every_field_the_command_feeds_back():
    """Otherwise the test below would pass while quietly exercising half the mapping."""
    assert set(infra.SCHEDULE_FLAGS) <= set(SCHEDULE)
    assert set(infra.SCHEDULE_READ_ONLY) <= set(SCHEDULE)


@pytest.mark.parametrize("state,expected", [("off", "DISABLED"), ("on", "ENABLED")])
def test_nightly_feeds_back_every_field_get_schedule_returned(
    fake_sh, stubbed, redirected, state, expected
):
    """AC5: update-schedule replaces the schedule, so an omitted field is a field destroyed."""
    infra.nightly(state)

    passed = passed_flags(one(fake_sh, "aws", "scheduler", "update-schedule"))
    assert passed["--state"] == expected

    for field, value in SCHEDULE.items():
        if field in infra.SCHEDULE_READ_ONLY:
            assert field not in infra.SCHEDULE_FLAGS, field
            assert str(value) not in passed.values(), f"{field} is not settable"
            continue
        flag = infra.SCHEDULE_FLAGS[field]
        assert flag in passed, f"{field} would be reset by this call"
        if field == "State":
            continue
        assert passed[flag] == (
            json.dumps(value) if isinstance(value, (dict, list)) else str(value)
        ), field

    # The name is the one OpenTofu owns, not one assembled here.
    assert passed["--name"] == SCHEDULE["Name"] == TF_OUTPUTS["scheduler_name"]


def test_nightly_omits_the_fields_the_schedule_does_not_have(
    fake_sh, stubbed, redirected, monkeypatch
):
    """`--description ""` is a value, not an omission, and some flags reject the empty string."""
    minimal = {
        key: value
        for key, value in SCHEDULE.items()
        if key not in ("Description", "KmsKeyArn", "StartDate", "EndDate")
    }
    monkeypatch.setattr(aws, "scheduler_get", lambda: {**minimal, "Description": ""})

    infra.nightly("on")

    passed = passed_flags(one(fake_sh, "aws", "scheduler", "update-schedule"))
    for flag in ("--description", "--kms-key-arn", "--start-date", "--end-date"):
        assert flag not in passed, flag
    assert passed["--schedule-expression"] == SCHEDULE["ScheduleExpression"]
    assert passed["--target"] == json.dumps(SCHEDULE["Target"])


def test_nightly_reports_a_field_it_would_otherwise_drop_in_silence(
    fake_sh, stubbed, redirected, monkeypatch, capsys
):
    """A field AWS adds later is a field this command would reset; say so."""
    monkeypatch.setattr(aws, "scheduler_get", lambda: {**SCHEDULE, "SomeNewField": "value"})

    infra.nightly("off")

    assert "SomeNewField" in capsys.readouterr().err
    assert find(fake_sh, "aws", "scheduler", "update-schedule"), "the flip still happens"


def test_nightly_prints_the_schedule_it_left_behind(fake_sh, stubbed, redirected):
    infra.nightly("off")

    report = fake_sh.calls[-1]
    assert report.argv[:3] == ["aws", "scheduler", "get-schedule"]
    assert report.argv[-2:] == ["--output", "table"]
    assert "State" in report.argv[report.argv.index("--query") + 1]
    assert positions(fake_sh, "aws", "scheduler", "get-schedule")[0] > max(
        positions(fake_sh, "aws", "scheduler", "update-schedule")
    ), "the report is read back after the update, not before"


def test_nightly_refuses_anything_but_on_or_off(fake_sh, stubbed, redirected):
    with pytest.raises(SystemExit) as failure:
        infra.nightly("maybe")

    assert "on or off" in str(failure.value)
    assert fake_sh.calls == []
