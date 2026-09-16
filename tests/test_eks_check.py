"""launcher.eks.check: the offline tripwire for the whole EKS surface.

`demo.py eks check` is the only thing between a chart bump and a CrashLooping collector on a
live cluster, so what these tests are really about is that it cannot pass by accident. Every
assertion it makes is checked twice: once against a recorded `helm template` render, which must
come back clean, and once against that render with the thing it checks for broken, which must
come back named.

The recordings in `tests/renders/` are real output of the chart `config.CHART_VERSION` pins,
which is what keeps this offline: the command itself downloads the chart and is deliberately
not part of the default pytest run, and no test here reaches a process. Two assertions here
are about the cluster rather than the render -- that the recordings still carry the pinned
chart's label, and that the `vpc-cni` add-on in `deploy/eks/tofu/eks.tf` still enforces the
NetworkPolicy beside them -- because a selector asserted against a stale label set, or an
unenforced policy, is green here and open on the cluster. That OpenTofu is read, never
written.
"""

import argparse
import re
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


# The OpenTofu that decides whether the NetworkPolicy `eks deploy` applies is enforced at all.
# Read as text and never written: the identifiers of the live cluster live in the same tree.
TOFU_MAIN = "eks.tf"
# That decision, as the add-on spells it. Both character classes are bounded on purpose: the
# first cannot reach past the `vpc-cni` block's own closing brace into a sibling add-on's,
# because it is the CNI's policy agent that filters and the setting anywhere else enforces
# nothing; the second cannot leave the payload, but does let the setting sit at any position
# in it, so adding a second add-on setting above it is not a failure.
VPC_CNI_ENFORCEMENT = re.compile(
    r"vpc-cni\s*=\s*\{[^{}]*configuration_values\s*=\s*jsonencode\(\{[^}]*"
    r'enableNetworkPolicy\s*=\s*"true"'
)
# `helm.sh/chart` as the chart stamps it on every document it renders: name, then version.
CHART_LABEL = re.compile(r"^\s*helm\.sh/chart:\s*(\S+)\s*$", re.MULTILINE)


def recorded(name):
    """One recorded render, as the text `core.capture(*helm template).stdout` hands back."""
    return (Path(__file__).parent / "renders" / f"{name}.yaml").read_text()


def problems_for(rendered, env=check.CHECK_LANGFUSE_ENV):
    """Everything `check` finds wrong with a render, given the `.env` it was rendered from.

    `check.synthetic_langfuse_secret` rather than `config.load_langfuse_env`: the latter now
    dies on a credential-free environment, and `check.CHECK_ENV` (the "langfuse not configured"
    case) is deliberately one.
    """
    langfuse = check.synthetic_langfuse_secret(env)
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


def rendered_pod(service):
    """The pod template the recorded render gives `service`, found by its example image."""
    documents = check.load_documents(recorded(CONFIGURED))
    workloads = [d for d in documents if d.get("kind") in check.WORKLOAD_KINDS]
    workload = check.service_workload(workloads, service)
    assert workload is not None, f"the recorded render has no workload running our {service}"
    return workload["spec"]["template"]


def selects(selector, labels):
    """Whether a NetworkPolicy `matchLabels` selector matches a pod with `labels`."""
    return selector.items() <= labels.items()


def tofu_main():
    """The OpenTofu the cluster is built from, as text. Read only; the suite never writes it."""
    return (config.tofu_dir() / TOFU_MAIN).read_text()


def uncommented(tofu_text):
    """The OpenTofu with its comment lines dropped, so a disabled setting reads as disabled.

    Commenting a line out is how an editor turns a Tofu setting off, and it leaves the text
    the setting was written in behind -- so a guard that matches the raw file credits a
    setting that is no longer in effect. Only whole-line comments go: an inline comment after
    a live setting documents it rather than disabling it, and must not stop it matching. Both
    spellings HCL accepts are handled.
    """
    return "\n".join(
        line
        for line in tofu_text.splitlines()
        if not line.lstrip().startswith(("#", "//"))
    )


def enforcement_problems(tofu_text):
    """Whatever stops the agent's NetworkPolicy from being enforced, given the OpenTofu text.

    Text in rather than a path, so the failing input can be held in a test instead of written
    over `deploy/eks/tofu/`.
    """
    if VPC_CNI_ENFORCEMENT.search(uncommented(tofu_text)):
        return []
    return [
        f"the vpc-cni add-on in deploy/eks/tofu/{TOFU_MAIN} no longer carries"
        ' enableNetworkPolicy = "true" in its own configuration_values, which is the only thing'
        f" that makes deploy/eks/k8s/{config.NETWORK_POLICY_MANIFEST} real: without it the VPC"
        " CNI accepts that policy and filters nothing, and every pod in"
        f" {config.NS_DEMO} regains access to the agent and the live model credential it holds"
    ]


def chart_pin_problems(rendered, name):
    """Whatever says a recording is the output of a chart other than the pinned one.

    Text in for the same reason: a stale recording is something to assert about, not to commit.
    """
    pinned = f"{k8s.CHART_NAME}-{config.CHART_VERSION}"
    # EVERY label the demo chart stamped has to be the pin, not merely one of them. A recording
    # carries the label once per document it renders -- four, at this chart version -- so a
    # regeneration that was interrupted, or run against a half-updated cache, leaves a file
    # whose documents disagree with each other. Asking whether the pin appears AT ALL passes on
    # exactly that file, which is the stale render this guard exists to refuse. The set also
    # dedupes: without it the message repeats one label once per document.
    carried = sorted(
        {
            label
            for label in CHART_LABEL.findall(rendered)
            if label.startswith(f"{k8s.CHART_NAME}-")
        }
    )
    if carried == [pinned]:
        return []
    return [
        f"tests/renders/{name}.yaml carries {', '.join(carried) or 'no'} helm.sh/chart label"
        f" where config.CHART_VERSION pins {pinned}: re-run the regeneration command in that"
        " file's header, because everything these tests read out of it -- the pod labels the"
        " agent NetworkPolicy's selectors are checked against included -- is a stale render"
        " until you do"
    ]


@pytest.fixture
def staged(monkeypatch, fake_sh):
    """What the offline parse staged for kustomize, read while the temporary copy still exists."""
    seen = []

    def recording(*args, **kwargs):
        if [str(argument) for argument in args[:2]] == ["kubectl", "kustomize"]:
            directory = Path(args[2])
            seen.append({path.name: path.read_text() for path in directory.iterdir()})
        return fake_sh.run(*args, **kwargs)

    monkeypatch.setattr(core, "run", recording)
    return seen


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


def test_a_generated_env_value_the_render_disagrees_with_is_caught():
    """Names are not enough: the value is the thing the deployment actually runs.

    Every generated `envOverrides` value is derived -- from `.env`, from the build manifest, or
    from another value -- so a name can keep rendering while what it renders stops being what
    the values document asked for. A wrong model is a release talking to the wrong endpoint,
    and the names-only check reported nothing at all.

    The second case is the same mistake arriving by a different route: a generated literal that
    the render reads from a Secret instead. The reference is named in full, because a value
    that came from somewhere else is as wrong as a value that came back different.
    """
    changed = edited(
        recorded(CONFIGURED),
        "            - name: LLM_MODEL\n              value: example-model\n",
        "            - name: LLM_MODEL\n              value: other-model\n",
    )

    named(problems_for(changed), "agent workload", "LLM_MODEL", "'other-model'", "'example-model'")

    referenced = edited(
        recorded(CONFIGURED),
        "            - name: LLM_MODEL\n              value: example-model\n",
        "            - name: LLM_MODEL\n"
        "              valueFrom:\n"
        "                secretKeyRef:\n"
        "                  key: LLM_MODEL\n"
        "                  name: llm-credentials\n",
    )

    named(problems_for(referenced), "LLM_MODEL", "llm-credentials", "'example-model'")


def test_the_storefronts_derived_replay_flag_is_compared_by_value():
    """`PUBLIC_HYPERDX_ENABLED` is the key that made the values comparison necessary.

    It is the one generated value nothing reads from `.env`: `values.frontend_env` derives it
    from SESSION_REPLAY. It used to be a hardcoded `true` in demo-values.yaml, and when it
    moved the recordings went stale while the suite stayed green -- the name was still
    rendered, and the derived value happened to equal the literal it replaced. A deployment
    that renders the opposite of what SESSION_REPLAY asked for is now a reported problem.
    """
    rendered = edited(
        recorded(CONFIGURED),
        '            - name: PUBLIC_HYPERDX_ENABLED\n              value: "true"\n',
        '            - name: PUBLIC_HYPERDX_ENABLED\n              value: "false"\n',
    )

    named(
        problems_for(rendered),
        "frontend workload",
        "PUBLIC_HYPERDX_ENABLED",
        "'false'",
        "'true'",
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

    # The only place the recording carries a value a secret key could be written over is the
    # agent's LANGFUSE_PROJECT_ID, which is one of the generated values -- so overwriting it is
    # now caught twice: as a rendered credential, and as a render that disagrees with the
    # document it was made from. Both are asserted; neither is relaxed away to keep the count.
    key = edited(
        recorded(CONFIGURED), "example-project", check.CHECK_LANGFUSE_ENV["LANGFUSE_SECRET_KEY"]
    )
    reported = problems_for(key)
    assert len(reported) == 2, reported
    mentions(reported, "LANGFUSE_SECRET_KEY value", "renders LANGFUSE_PROJECT_ID as")


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


def test_check_uses_the_cluster_to_validate_the_manifests_when_one_answers(tools, fake_sh, renders):
    """A cluster is a bonus: it adds schema validation, and its absence changes the parse."""
    check.check()

    assert fake_sh.calls[4].argv == [
        "kubectl",
        "apply",
        "--dry-run=client",
        *[
            argument
            for name in config.STATIC_MANIFESTS
            for argument in ("-f", str(config.k8s_dir() / name))
        ],
    ]
    assert not any("kustomize" in line for line in fake_sh.lines())


def test_the_offline_parse_covers_every_manifest_the_deploy_applies_as_is(
    tools, fake_sh, renders, staged
):
    """AC5: the NetworkPolicy is parsed beside the collector, from copies of the real files.

    One kustomize build over both, so a missing `apiVersion` or a mistyped `kind` in either is
    named here rather than by the API server in the middle of a deploy.
    """
    fake_sh.reply("kubectl version", returncode=1)

    check.check()

    assert len(staged) == 1, staged
    assert set(staged[0]) == {"kustomization.yaml", *config.STATIC_MANIFESTS}
    kustomization = yaml.safe_load(staged[0]["kustomization.yaml"])
    assert kustomization == {"resources": list(config.STATIC_MANIFESTS)}
    for name in config.STATIC_MANIFESTS:
        assert staged[0][name] == (config.k8s_dir() / name).read_text(), (
            f"{name} was staged as something other than the committed file"
        )


def test_the_agent_network_policy_selects_the_pods_the_chart_actually_renders():
    """AC2: the policy's two selectors and its port, against the recorded render's labels.

    The whole risk in this manifest is a label that no pod carries: a `podSelector` that
    matches nothing leaves the agent open, and a `from` selector that matches nothing cuts the
    storefront off. Both are asserted against the pinned chart's own output, so a bump that
    renames `opentelemetry.io/name` fails here instead of in front of an audience.
    """
    policy = yaml.safe_load((config.k8s_dir() / config.NETWORK_POLICY_MANIFEST).read_text())
    agent = rendered_pod("agent")["metadata"]["labels"]
    frontend = rendered_pod("frontend")["metadata"]["labels"]
    port = rendered_pod("agent")["spec"]["containers"][0]["ports"][0]["containerPort"]

    assert policy["metadata"]["namespace"] == config.NS_DEMO
    assert policy["spec"]["policyTypes"] == ["Ingress"], (
        "egress stays open: the agent calls the model endpoint, the MCP server and the collector"
    )
    selector = policy["spec"]["podSelector"]["matchLabels"]
    assert selects(selector, agent) and not selects(selector, frontend), (
        f"the policy selects {selector}, which is not the agent pod alone"
    )

    rules = policy["spec"]["ingress"]
    assert len(rules) == 1, "one allowed caller; anything else is a second way to spend the key"
    for allowed in rules[0]["from"]:
        matched = allowed["podSelector"]["matchLabels"]
        assert selects(matched, frontend) and not selects(matched, agent), (
            f"the policy admits {matched}, which is not the storefront pod alone"
        )
    assert rules[0]["ports"] == [{"protocol": "TCP", "port": port}], (
        f"the agent's container listens on {port}"
    )


def test_the_addon_that_makes_the_agent_network_policy_enforceable_is_still_configured():
    """The selectors above are only half of it: the cluster has to be told to enforce them.

    `tofu validate` does not look inside an add-on's `configuration_values` payload and neither
    does `eks check`, so until this assertion the one line the whole protection rests on was
    checked by nothing at all. Dropping it leaves the suite green, the plan green and the apply
    green while every pod in the namespace quietly regains access to the agent's credential.
    """
    assert enforcement_problems(tofu_main()) == []

    # And it must not fire on a correct cluster: the payload is a map, so a second add-on
    # setting can land above this one without changing what the CNI enforces. A guard that
    # reddens on a legitimate edit is a guard the next editor loosens instead of reading.
    ordered = edited(
        tofu_main(),
        'enableNetworkPolicy = "true"',
        'enableWindowsIpam    = "false"\n        enableNetworkPolicy = "true"',
    )
    assert enforcement_problems(ordered) == []


def test_the_enforcement_guard_names_the_manifest_the_addon_setting_makes_real():
    """The guard above, watched failing -- otherwise its reach is a claim rather than a fact.

    Both failing inputs are text held here. `deploy/eks/tofu/` is never written by the suite:
    it is where the live cluster's identifiers live.
    """
    dropped = edited(tofu_main(), 'enableNetworkPolicy = "true"', "")
    named(
        enforcement_problems(dropped),
        "enableNetworkPolicy",
        config.NETWORK_POLICY_MANIFEST,
        "vpc-cni",
    )

    # The setting on some other add-on is the same inert cluster, because it is the vpc-cni
    # agent that filters. `vpc-cni = {}` closes before the payload, so a guard that credited a
    # `configuration_values` the add-on does not own would report nothing here.
    elsewhere = edited(tofu_main(), "vpc-cni = {", "vpc-cni = {}\n    other-addon = {")
    named(enforcement_problems(elsewhere), config.NETWORK_POLICY_MANIFEST)

    # Commenting the line out is the ordinary way to disable a Tofu setting, and it is the case
    # this guard missed when it was first written: the text stays in the file, so a guard that
    # searched the raw OpenTofu still matched and reported nothing while the CNI enforced
    # nothing. One case per spelling HCL accepts, because a guard proven against a DELETED
    # setting is not thereby proven against a DISABLED one.
    for comment in ("# ", "// "):
        disabled = edited(
            tofu_main(),
            'enableNetworkPolicy = "true"',
            f'{comment}enableNetworkPolicy = "true"',
        )
        named(
            enforcement_problems(disabled),
            "enableNetworkPolicy",
            config.NETWORK_POLICY_MANIFEST,
            "vpc-cni",
        )


def test_an_inline_comment_after_the_setting_does_not_disable_it():
    """The other edge of the same fix: only WHOLE-LINE comments are dropped.

    Stripping from the first `#` anywhere on a line would make a documented setting read as an
    absent one, so the guard would redden on an edit that changed nothing about the cluster --
    and a guard that reddens on a legitimate edit is one the next editor loosens.
    """
    annotated = edited(
        tofu_main(),
        'enableNetworkPolicy = "true"',
        'enableNetworkPolicy = "true" # the CNI agent filters; see deploy/eks/README.md',
    )
    assert enforcement_problems(annotated) == []


@pytest.mark.parametrize("name", [CONFIGURED, PLAIN])
def test_both_recordings_carry_the_chart_label_of_the_version_they_are_pinned_to(name):
    """The other half of the same protection: the selector guard reads these files, not a cluster.

    So it is only as true as they are fresh, and their freshness rests on a hand-run
    regeneration this repo has already missed once. A `config.CHART_VERSION` bump now fails
    here, instead of leaving the selectors passing against a label set the chart stopped
    rendering -- which is a `podSelector` matching nothing and an open agent, with the suite
    green.
    """
    assert chart_pin_problems(recorded(name), name) == []


def test_the_chart_pin_guard_names_the_stale_version_and_where_to_refresh_it():
    """That guard watched failing, on the recording a bump would leave behind."""
    pinned = f"{k8s.CHART_NAME}-{config.CHART_VERSION}"
    assert pinned in recorded(CONFIGURED), f"the recording no longer carries {pinned}"
    stale = recorded(CONFIGURED).replace(pinned, f"{k8s.CHART_NAME}-0.40.0")

    named(chart_pin_problems(stale, CONFIGURED), "0.40.0", pinned, "header")


def test_the_chart_pin_guard_refuses_a_partly_regenerated_recording():
    """The guard's SCOPE, not its behaviour: one stale document out of four has to be enough.

    The recording carries the chart label once per document it renders, so an interrupted
    regeneration -- or one run against a half-updated chart cache -- produces a file whose
    documents disagree. Asking whether the pin appears at all passed on that file, which is
    precisely the stale render the guard exists to refuse, and the version-bump case above
    could never have caught it because it rewrites every occurrence at once.
    """
    pinned = f"{k8s.CHART_NAME}-{config.CHART_VERSION}"
    fresh = recorded(CONFIGURED)
    assert fresh.count(pinned) > 1, "one label per document is what makes this case possible"

    # One document left behind, the rest regenerated.
    partial = fresh.replace(pinned, f"{k8s.CHART_NAME}-0.40.0", 1)
    assert partial.count(pinned) > 0, "the pin still appears, which is why the old guard passed"

    named(chart_pin_problems(partial, CONFIGURED), "0.40.0", pinned, "header")


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
