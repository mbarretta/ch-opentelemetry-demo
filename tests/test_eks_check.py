"""launcher.eks.check: the offline tripwire for the whole EKS surface.

`demo.py eks check` is the only thing between a chart bump and a CrashLooping collector on a
live cluster, so what these tests are really about is that it cannot pass by accident. Every
assertion it makes is checked twice: once against a recorded `helm template` render, which must
come back clean, and once against that render with the thing it checks for broken, which must
come back named.

The recordings in `tests/renders/` are real output of the pinned chart (0.41.0), which is what
keeps this offline: the command itself downloads the chart and is deliberately not part of the
default pytest run, and no test here reaches a process.
"""

import argparse
from pathlib import Path

import pytest
import yaml

from launcher import core
from launcher.eks import check, config, k8s, values

CONFIGURED = "langfuse-configured"
PLAIN = "langfuse-not-configured"

# The agent's `langfuse-credentials` reference for one key, exactly as the chart renders it.
# Removing it, or its `optional`, is what a Langfuse-less deployment would trip over.
SECRET_REFERENCE = """\
            - name: LANGFUSE_SECRET_KEY
              valueFrom:
                secretKeyRef:
                  key: LANGFUSE_SECRET_KEY
                  name: langfuse-credentials
                  optional: true
"""
# A workload the static values switch off. Appended to a render to check the assertion fires;
# `components.chatbot.enabled: false` is one line and a chart bump could rename it away.
CHATBOT = """\
---
apiVersion: apps/v1
kind: Deployment
metadata:
  name: chatbot
  namespace: otel-demo
spec:
  template:
    spec:
      containers:
        - name: chatbot
          image: ghcr.io/open-telemetry/demo:3.0.0-chatbot
"""


def recorded(name):
    """One recorded render, as the text `core.capture(*helm template).stdout` hands back."""
    return (Path(__file__).parent / "renders" / f"{name}.yaml").read_text()


def problems_for(rendered, env=check.CHECK_LANGFUSE_ENV):
    """Everything `check` finds wrong with a render, given the `.env` it was rendered from."""
    langfuse = config.load_langfuse_env(env)
    document = values.eks_values(env, check.example_images(), langfuse)
    return check.render_problems(rendered, document, langfuse)


def edited(rendered, before, after):
    """A render with one thing changed, refusing to test nothing if the recording moved on."""
    assert before in rendered, f"the recorded render no longer contains {before!r}"
    return rendered.replace(before, after, 1)


def relay(rendered):
    """The collector configuration the render puts in a ConfigMap."""
    configs = check.collector_configs(check.load_documents(rendered))
    assert list(configs) == ["otel-collector-agent/relay"], configs
    return configs["otel-collector-agent/relay"]


def named(problems, *expected):
    """Assert exactly one problem was reported and that it names each of `expected`."""
    assert len(problems) == 1, problems
    for fragment in expected:
        assert fragment in problems[0], problems[0]


def mentions(problems, *expected):
    """Assert each of `expected` is named by some problem, where one break shows up twice."""
    for fragment in expected:
        assert any(fragment in problem for problem in problems), (fragment, problems)


@pytest.fixture
def tools(monkeypatch):
    """Every external command present, and a record of which ones were required."""
    required = []

    def which(command):
        required.append(command)
        return f"/usr/bin/{command}"

    monkeypatch.setattr(core.shutil, "which", which)
    return required


@pytest.fixture
def values_files(monkeypatch, fake_sh):
    """The `-f` files of each `helm template`, read while they still exist.

    The generated document is written to a temporary directory that is gone by the time the
    command returns, so the only place to see what Helm was actually given is the call itself.
    """
    seen = []
    capture = core.capture

    def reading(*args, **kwargs):
        seen.append([Path(a).read_text() for a in args if str(a).endswith(".yaml")])
        return capture(*args, **kwargs)

    monkeypatch.setattr(core, "capture", reading)
    return seen


@pytest.fixture
def renders(monkeypatch):
    """Record what each render was asserted against, with the assertions themselves stubbed."""
    made = []

    def record(rendered, document, langfuse):
        made.append((rendered, document, langfuse))
        return []

    monkeypatch.setattr(check, "render_problems", record)
    return made


def test_both_recorded_renders_of_the_pinned_chart_are_clean():
    """The two renders the command makes, asserted the way the command asserts them."""
    assert problems_for(recorded(CONFIGURED)) == []
    assert problems_for(recorded(PLAIN), check.CHECK_ENV) == []


def test_a_connector_counts_as_a_definition_for_both_ends_it_is_wired_into():
    """`span_metrics` is a traces exporter and a metrics receiver, defined under `connectors`.

    Without this the clean renders above would report two problems, so the recording is
    checked for the shape that makes that assertion mean something.
    """
    parsed = relay(recorded(CONFIGURED))
    pipelines = parsed["service"]["pipelines"]

    assert "span_metrics" in parsed["connectors"]
    assert "span_metrics" not in parsed["exporters"] and "span_metrics" not in parsed["receivers"]
    assert "span_metrics" in pipelines["traces"]["exporters"]
    assert "span_metrics" in pipelines["metrics"]["receivers"]


def test_a_pipeline_naming_an_undefined_processor_is_caught():
    """The chart-bump tripwire: a renamed processor breaks every pipeline that runs it."""
    rendered = edited(
        recorded(CONFIGURED),
        "      transform/sanitize_spans:\n",
        "      transform/sanitize_spans_v2:\n",
    )

    reported = problems_for(rendered)

    assert len(reported) == 2, reported
    assert all("transform/sanitize_spans" in problem for problem in reported)
    assert any("pipeline traces names processors" in problem for problem in reported)
    assert any(f"pipeline {values.LANGFUSE_PIPELINE} names processors" in problem
               for problem in reported)


def test_a_pipeline_naming_an_undefined_exporter_is_caught():
    """The ClickStack exporter carries every signal; a rename leaves three dead pipelines."""
    rendered = edited(
        recorded(CONFIGURED), "      otlphttp/clickstack:\n", "      otlphttp/clickhouse:\n"
    )

    reported = problems_for(rendered)

    assert len(reported) == 3, reported
    assert all("names exporters that nothing defines: otlphttp/clickstack" in p for p in reported)


def test_an_undefined_extension_is_caught():
    rendered = edited(recorded(CONFIGURED), "      health_check:\n", "      healthcheck:\n")

    named(problems_for(rendered), "extensions that nothing defines", "health_check")


def test_the_agent_has_to_carry_the_optional_langfuse_references():
    """`eks deploy` deletes the Secret when `.env` has no Langfuse keys, so both halves matter."""
    # Dropping the whole entry also drops a name both values documents ask for, so the
    # environment check reports it as well; the missing reference is the point here.
    dropped = edited(recorded(CONFIGURED), SECRET_REFERENCE, "")
    mentions(problems_for(dropped), "no LANGFUSE_SECRET_KEY secretKeyRef", config.SECRET_LANGFUSE)

    required = edited(
        recorded(CONFIGURED),
        SECRET_REFERENCE,
        SECRET_REFERENCE.replace("                  optional: true\n", ""),
    )
    named(problems_for(required), "is not optional", "will not start")

    elsewhere = edited(
        recorded(CONFIGURED),
        SECRET_REFERENCE,
        SECRET_REFERENCE.replace("name: langfuse-credentials", "name: llm-credentials"),
    )
    named(problems_for(elsewhere), "reads llm-credentials/LANGFUSE_SECRET_KEY")


def test_all_four_image_overrides_have_to_reach_a_rendered_workload():
    """A release running two of our images and two released ones is a half-finished deploy."""
    rendered = edited(
        recorded(CONFIGURED), "example/mcp:check", "ghcr.io/open-telemetry/demo:3.0.0-mcp"
    )

    named(problems_for(rendered), "example/mcp:check", "mcp imageOverride")


def test_a_rendered_chatbot_is_refused():
    """The Gradio chat client is a second assistant with no session and no Langfuse prompt."""
    reported = problems_for(recorded(CONFIGURED) + CHATBOT)

    assert len(reported) == 2, reported
    assert any("a chatbot workload is rendered (chatbot)" in problem for problem in reported)
    assert any("runs a chatbot image" in problem for problem in reported)


def test_the_values_files_have_to_be_merged_in_the_order_helm_needs():
    """Helm replaces lists: the generated `envOverrides` wins only when its file is passed last.

    The two directions fail differently. A generated name missing from the render is what the
    reversed `-f` order produces -- the static list replaces the generated one wholesale. A
    static name missing is what a `values.env_overrides` that stopped carrying the static
    entries through produces. Both would deploy a release nobody looked at again.
    """
    generated_lost = edited(
        recorded(CONFIGURED),
        "            - name: LLM_MODEL\n              value: example-model\n",
        "",
    )
    named(
        problems_for(generated_lost),
        "agent workload is missing LLM_MODEL from the generated values",
        "demo-values.yaml first",
    )

    # AGENT_BIND is in both documents, the generated one having carried it through, so losing
    # it from the render is reported against both of them.
    static_lost = edited(
        recorded(CONFIGURED), "            - name: AGENT_BIND\n              value: 0.0.0.0\n", ""
    )
    reported = problems_for(static_lost)
    assert len(reported) == 2, reported
    mentions(
        reported,
        "missing AGENT_BIND from demo-values.yaml",
        "missing AGENT_BIND from the generated values",
    )


def test_the_langfuse_exporter_appears_exactly_with_the_credentials():
    """Each render is asserted against the credentials it was made with, so both ways fail.

    An `otlphttp` exporter whose `${env:LANGFUSE_BASE_URL}` is unset fails the collector's own
    validation, and a pipeline pointed at an exporter nobody defined does too -- which is why
    the command renders twice instead of trusting one of them.
    """
    unconfigured_render = problems_for(recorded(PLAIN), check.CHECK_LANGFUSE_ENV)
    assert len(unconfigured_render) == 2, unconfigured_render
    assert any("exports to ['debug']" in problem for problem in unconfigured_render)
    assert any(f"does not define {values.LANGFUSE_EXPORTER}" in p for p in unconfigured_render)

    configured_render = problems_for(recorded(CONFIGURED), check.CHECK_ENV)
    assert len(configured_render) == 2, configured_render
    assert any("CrashLoops the collector" in problem for problem in configured_render)
    assert any(
        f"exports to ['{values.LANGFUSE_EXPORTER}']" in problem for problem in configured_render
    )


def test_the_langfuse_pipeline_itself_has_to_be_in_the_render():
    """It carries the assistant's spans; the generated values are the only thing that adds it."""
    rendered = edited(
        recorded(CONFIGURED), f"        {values.LANGFUSE_PIPELINE}:\n", "        traces/unused:\n"
    )

    named(problems_for(rendered), f"has no {values.LANGFUSE_PIPELINE} pipeline")


def test_a_rendered_credential_is_refused():
    """Credentials reach the pods from Secrets; a rendered one would sit in `helm get values`."""
    langfuse = config.load_langfuse_env(check.CHECK_LANGFUSE_ENV)
    header = edited(
        recorded(CONFIGURED), "${env:LANGFUSE_AUTH_HEADER}", langfuse["LANGFUSE_AUTH_HEADER"]
    )
    named(problems_for(header), "LANGFUSE_AUTH_HEADER value", config.SECRET_LANGFUSE)

    payload = edited(
        recorded(CONFIGURED),
        "${env:LANGFUSE_AUTH_HEADER}",
        langfuse["LANGFUSE_AUTH_HEADER"].split(" ")[1],
    )
    named(problems_for(payload), "LANGFUSE_AUTH_HEADER value")

    key = edited(
        recorded(CONFIGURED), "example-project", check.CHECK_LANGFUSE_ENV["LANGFUSE_SECRET_KEY"]
    )
    named(problems_for(key), "LANGFUSE_SECRET_KEY value")


def test_an_endpoint_is_configuration_rather_than_a_credential():
    """The base URL and the storefront's trace links are rendered on purpose."""
    rendered = recorded(CONFIGURED)

    assert check.CHECK_LANGFUSE_ENV["LANGFUSE_BASE_URL"] in rendered
    assert problems_for(rendered) == []


def test_a_render_without_a_collector_configuration_is_not_a_pass():
    """A `helm template` that produced no collector at all would otherwise check out fine."""
    documents = recorded(CONFIGURED).split("---\n")
    without_configmap = "---\n".join(
        document for document in documents if "kind: ConfigMap" not in document
    )

    assert "no rendered ConfigMap holds a collector configuration" in problems_for(
        without_configmap
    )
    assert problems_for("") == ["helm template rendered nothing at all"]


def test_check_runs_every_offline_step_in_the_order_that_reports_the_cheapest_failure_first(
    tools, fake_sh, renders
):
    """AC2, AC3, AC5, AC6: tofu, the collector manifest, then the two renders. No cluster."""
    fake_sh.reply("kubectl version", returncode=1)

    check.check()

    chdir = f"-chdir={config.tofu_dir()}"
    assert set(tools) == {"tofu", "helm", "kubectl"}
    assert [call.argv for call in fake_sh.calls[:4]] == [
        ["tofu", chdir, "fmt", "-check"],
        ["tofu", chdir, "init", "-backend=false", "-input=false"],
        ["tofu", chdir, "validate"],
        ["kubectl", "version", "--request-timeout=3s"],
    ]
    kustomize = fake_sh.calls[4]
    assert kustomize.argv[:2] == ["kubectl", "kustomize"], "no cluster: the offline parse"
    assert not Path(kustomize.argv[2]).exists(), "the staged kustomization is temporary"
    assert "helm repo add" in fake_sh.lines()[5]
    assert "helm repo update" in fake_sh.lines()[6]
    assert len(fake_sh.calls) == 9, fake_sh.lines()


def test_check_renders_exactly_what_the_deploy_installs(tools, fake_sh, values_files, renders):
    """AC3, AC4: two renders, the same argv the deploy upgrades with, static values first."""
    fake_sh.reply("kubectl version", returncode=1)

    check.check()

    static = config.k8s_dir() / values.STATIC_VALUES
    assert len(renders) == 2, "the Langfuse exporter is conditional, so the chart renders twice"
    assert [langfuse != {} for _, _, langfuse in renders] == [True, False]

    for call in fake_sh.calls[-2:]:
        assert call.argv == check.helm_template_args([static, Path(call.argv[-1])])
    assert [files[0] for files in values_files] == [static.read_text()] * 2, (
        "demo-values.yaml is the first -f, because a later -f replaces its envOverrides lists"
    )
    for files, (_, document, _) in zip(values_files, renders, strict=True):
        assert yaml.safe_load(files[1]) == document, "helm read the document it was handed"
        assert [document["components"][service]["imageOverride"] for service in core.IMAGES] == [
            {"repository": f"example/{service}", "tag": check.EXAMPLE_TAG}
            for service in core.IMAGES
        ]


def test_check_uses_the_cluster_to_validate_the_manifest_when_one_answers(tools, fake_sh, renders):
    """A cluster is a bonus: it adds schema validation, and its absence changes the parse."""
    check.check()

    manifest = config.k8s_dir() / config.COLLECTOR_MANIFEST
    assert fake_sh.calls[4].argv == [
        "kubectl",
        "apply",
        "--dry-run=client",
        "-f",
        str(manifest),
    ]
    assert not any("kustomize" in line for line in fake_sh.lines())


def test_a_missing_tool_is_named_and_nothing_else_runs(monkeypatch, fake_sh):
    """AC5: a check that skips a step because the tool is absent has not checked it."""
    monkeypatch.setattr(core.shutil, "which", lambda command: None)

    with pytest.raises(SystemExit) as failure:
        check.check()

    for command in ("tofu", "helm", "kubectl"):
        assert command in str(failure.value)
    assert fake_sh.lines() == [], "nothing is validated before the tools are there"

    monkeypatch.setattr(core.shutil, "which", lambda command: None if command == "helm" else "/bin")
    with pytest.raises(SystemExit) as one_missing:
        check.check()
    assert "helm" in str(one_missing.value) and "tofu" not in str(one_missing.value)


def test_check_reports_every_problem_of_both_renders_at_once(tools, fake_sh, monkeypatch):
    """A chart bump breaks several assertions at once; one run should name all of them."""
    fake_sh.reply("kubectl version", returncode=1)
    monkeypatch.setattr(
        check, "render_problems", lambda rendered, document, langfuse: ["a", "b"]
    )

    with pytest.raises(SystemExit) as failure:
        check.check()

    message = str(failure.value)
    assert message.count("langfuse configured: a") == 1, message
    assert message.count("langfuse not configured: b") == 1, message
    assert k8s.CHART_NAME in message and config.CHART_VERSION in message


def test_check_leaves_the_deploys_own_generated_values_alone(
    tmp_path, monkeypatch, tools, fake_sh, renders
):
    """A read-only check that overwrote what `eks deploy` wrote would be a trap."""
    monkeypatch.setattr(core, "RUNTIME", tmp_path)
    fake_sh.reply("kubectl version", returncode=1)

    check.check()

    assert not values.values_path().exists()
    assert list(tmp_path.iterdir()) == []


def test_the_helm_argv_is_derived_from_the_deploys_own_and_refuses_to_guess(monkeypatch):
    """`helm template` renders the release the deploy installs, or says it cannot."""
    first, second = Path("/static.yaml"), Path("/generated.yaml")

    args = check.helm_template_args([first, second])

    assert args == [
        "helm",
        "template",
        config.RELEASE,
        f"{config.HELM_REPO_NAME}/{k8s.CHART_NAME}",
        "--version",
        config.CHART_VERSION,
        "-n",
        config.NS_DEMO,
        "-f",
        str(first),
        "-f",
        str(second),
    ]
    assert "--wait" not in args and "--timeout" not in args, "a render never waits"

    monkeypatch.setattr(k8s, "helm_upgrade_args", lambda files: ["helm", "install", "otel-demo"])
    with pytest.raises(SystemExit, match="helm_upgrade_args"):
        check.helm_template_args([first, second])

    # The values files have to survive the cut too: without them the chart renders its own
    # defaults, and every assertion would report that as a dozen unrelated problems.
    monkeypatch.setattr(
        k8s,
        "helm_upgrade_args",
        lambda files: ["helm", "upgrade", "--install", "otel-demo", "--wait", "-f", str(files[0])],
    )
    with pytest.raises(SystemExit, match="helm_upgrade_args"):
        check.helm_template_args([first, second])


def test_the_static_values_come_first_because_helm_replaces_lists():
    """The one ordering rule the whole generated-values design rests on."""
    files = check.release_values_files(Path("/tmp/values.generated.yaml"))

    assert files == [config.k8s_dir() / values.STATIC_VALUES, Path("/tmp/values.generated.yaml")]


def test_check_is_reachable_as_a_subcommand_and_renders_nothing_to_get_there():
    """AC7: `demo.py eks check` parses to the handler; the renders are the command's own work."""
    parser = argparse.ArgumentParser()
    check.register(parser.add_subparsers(dest="command"))

    args = parser.parse_args(["check"])

    assert callable(args.handler)
    assert (Path(__file__).parent / "renders").is_dir(), (
        "the assertions are tested against recordings, so the suite needs no chart"
    )
