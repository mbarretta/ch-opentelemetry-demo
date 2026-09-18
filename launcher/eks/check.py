"""Offline validation of the whole EKS surface: no AWS credentials, no cluster.

Ports `check.sh`. What it catches is the class of mistake the unit tests cannot: values the
chart rejects, a collector pipeline naming a processor nobody defines, a manifest Kubernetes
will not parse. It may download pinned OpenTofu providers and the demo chart, which is why it
is a subcommand and not part of the default pytest run.

The chart is rendered twice, because the Langfuse exporter is conditional, and every assertion
is made against both renders. Each render is asserted as a whole rather than per step, so one
run names every problem it found instead of the first.

Missing tools are named and fail the command. A check that skips is not a check: `check.sh`
skipped shellcheck when it was absent, which meant a laptop without it reported OK while
nothing had been checked.

TF_PLUGIN_CACHE_DIR, which the retired `lib/common.sh` exported before `tofu init`, is
deliberately not set here or in `infra`. OpenTofu reads it from the environment itself and the
launcher passes its own environment through untouched, so `export
TF_PLUGIN_CACHE_DIR=~/.terraform.d/plugin-cache` (or `plugin_cache_dir` in `~/.tofurc`, the
supported per-machine setting) still shares one provider cache across checkouts. What is
dropped is the bash's default and its `mkdir -p`: a code path the unit tests execute must not
create directories in the presenter's home.
"""

import tempfile
from pathlib import Path

from .. import core
from . import config, infra, k8s, values

# The workload kinds a rendered chart can put a container in. The manifests parsed alongside
# the render are `config.STATIC_MANIFESTS`: `eks deploy` applies the same files.
WORKLOAD_KINDS = ("Deployment", "DaemonSet", "StatefulSet")

# The image references the render is done with. The real ones are account-specific (the ECR URL
# carries the account id) and content-derived, and the chart only needs a non-empty pair, so a
# recognisable example is both enough and self-describing in a failure message.
EXAMPLE_REPOSITORY = "example"
EXAMPLE_TAG = "check"

# The `.env` the render is done against: synthetic, so the same two documents are rendered on
# every laptop, and so `check` works on a checkout whose `.env` is still empty. Nothing is read
# from the real environment -- a render asserted against values that vary per machine would
# either fail for the wrong reason or have to stop asserting.
CHECK_ENV = {
    "AGENT_MODE": "live",
    "MCP_ENABLED": "True",
    "LLM_BASE_URL": "https://llm.example/v1",
    "LLM_MODEL": "example-model",
    "LANGFUSE_PROMPT_LABEL": "production",
    "LANGFUSE_PROJECT_ID": "example-project",
    "LANGFUSE_PUBLIC_URL": "https://langfuse.example",
    "CLICKSTACK_TRACE_URL_TEMPLATE": "https://clickstack.example/search?trace={trace_id}",
    "ASSISTANT_DEMO_DETAILS": "true",
}
# The same deployment with Langfuse configured. The keys are obviously fake because they are
# never used: at deploy time these four values live in the `langfuse-credentials` Secret and the
# collector expands them itself, so all the render needs is their presence -- which is also why
# the render is checked for them afterwards (none of them may appear in it).
CHECK_LANGFUSE_ENV = {
    **CHECK_ENV,
    "LANGFUSE_BASE_URL": "https://langfuse.example",
    "LANGFUSE_PUBLIC_KEY": "pk-lf-example-not-a-key",
    "LANGFUSE_SECRET_KEY": "sk-lf-example-not-a-key",
}
# Which of the `langfuse-credentials` keys are credentials: the pair and the header derived from
# it. `LANGFUSE_BASE_URL` is an endpoint, and the agent's `LANGFUSE_PUBLIC_URL` beside it is
# rendered deliberately -- the Demo details panel turns it into trace links.
CREDENTIAL_KEYS = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY", "LANGFUSE_AUTH_HEADER")

# The two renders, in the order they run: the fuller configuration first, so a break in the
# shared half is reported before the Langfuse-less variant repeats it.
RENDERS = (("langfuse configured", CHECK_LANGFUSE_ENV), ("langfuse not configured", CHECK_ENV))


def register(subparsers):
    """Declare `eks check`."""
    parser = subparsers.add_parser(
        "check", help="validate the OpenTofu, the static manifests and the rendered chart"
    )
    parser.set_defaults(handler=lambda args: check())


def check():
    """Run `tofu fmt/init/validate`, parse the static manifests, render the chart twice.

    Twice because the Langfuse exporter is conditional: both the configured and the
    unconfigured generated values have to render, and each rendering is asserted against -- the
    four image overrides landed, every processor and exporter a pipeline names is defined, the
    agent Deployment carries the optional `langfuse-credentials` references, and there is no
    chatbot Deployment. Missing tools are named rather than silently skipped.
    """
    core.need("tofu", "helm", "kubectl")
    check_tofu()
    check_static_manifests()
    check_rendered_chart()
    print()
    core.log("check complete: OpenTofu, the static manifests and both renders are valid")


def check_tofu():
    """Format, initialise and validate `deploy/eks/tofu` without touching the S3 backend.

    `-backend=false` is what makes this credential-free: the providers are still installed and
    every resource is still validated, but no state is read, so the check runs on a laptop with
    no AWS session. It leaves a `.terraform/` directory without a backend state file, which is
    the distinction `infra.initialised()` reads.
    """
    core.log("tofu fmt -check")
    infra.tofu("fmt", "-check")
    core.log("tofu init -backend=false")
    infra.tofu("init", "-backend=false", "-input=false", stdout=core.DEVNULL)
    core.log("tofu validate")
    infra.tofu("validate")


def check_static_manifests():
    """Parse every manifest `eks deploy` applies as-is, the way the cluster will read it.

    The ClickStack collector and the agent's NetworkPolicy, in one parse rather than one each:
    both are `config.STATIC_MANIFESTS`, both fail the same way -- accepted by this command and
    rejected by the API server -- and a run that names both missing files at once is one run.

    `kubectl apply --dry-run=client` still needs a live API server for schema validation and
    resource discovery, so it only runs when one answers. With no cluster -- the normal case
    for this command -- `kubectl kustomize` is the offline equivalent: it parses every document
    and requires apiVersion, kind and metadata.name on each.
    """
    manifests = [config.k8s_dir() / name for name in config.STATIC_MANIFESTS]
    missing = [str(manifest) for manifest in manifests if not manifest.is_file()]
    if missing:
        core.die(f"missing {', '.join(missing)}: `eks deploy` applies these manifests as-is")

    named = ", ".join(config.STATIC_MANIFESTS)
    if api_server_answers():
        core.log(f"kubectl apply --dry-run=client -f {named}")
        flags = [argument for manifest in manifests for argument in ("-f", manifest)]
        core.run("kubectl", "apply", "--dry-run=client", *flags, stdout=core.DEVNULL)
        return

    core.log(f"kubectl kustomize {named} (no cluster reachable; offline parse)")
    with tempfile.TemporaryDirectory() as directory:
        staged = Path(directory)
        for manifest in manifests:
            (staged / manifest.name).write_text(manifest.read_text())
        resources = "".join(f"  - {name}\n" for name in config.STATIC_MANIFESTS)
        (staged / "kustomization.yaml").write_text(f"resources:\n{resources}")
        core.run("kubectl", "kustomize", staged, stdout=core.DEVNULL)


def api_server_answers():
    """Whether an API server answers, on the short timeout `check.sh` used.

    A cluster is a bonus here, never a requirement: no answer selects the offline parse above
    rather than failing.

    Named for the probe it is rather than `cluster_reachable`: this module imports `infra`,
    whose `cluster_reachable(cluster)` asks AWS whether a named EKS cluster still exists. Two
    live names, one signature apart, reading as the same question is how a caller ends up
    passing the wrong one.
    """
    probe = core.run(
        "kubectl",
        "version",
        "--request-timeout=3s",
        check=False,
        stdout=core.DEVNULL,
        stderr=core.DEVNULL,
    )
    return probe.returncode == 0


def check_rendered_chart():
    """Render the pinned chart twice and assert both renders, naming every problem at once."""
    k8s.helm_repo_ensure()
    problems = []
    for label, env in RENDERS:
        core.log(f"helm template {config.CHART_VERSION} ({label})")
        rendered, document, langfuse = render(env)
        found = render_problems(rendered, document, langfuse)
        problems += [f"{label}: {problem}" for problem in found]
    if problems:
        core.die(
            f"the rendered chart ({config.HELM_REPO_NAME}/{k8s.CHART_NAME} "
            f"{config.CHART_VERSION}) is not what the deployment needs:\n  - "
            + "\n  - ".join(problems)
        )


def render(env):
    """Render the release from the generated values `env` produces, with the example images.

    Returns the rendered manifests, the generated values document behind them and the Langfuse
    credentials it was generated with -- the three things every assertion below is made against.

    The generated values go to a temporary file, not to `values.values_path()`: the example
    images and synthetic environment here are not a deployment, and overwriting the document a
    real `eks deploy` wrote would be a surprising side effect of a read-only check.
    """
    langfuse = synthetic_langfuse_secret(env)
    document = values.eks_values(env, example_images(), langfuse)
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "values.generated.yaml"
        path.write_text(dump(document))
        rendered = core.capture(*helm_template_args(release_values_files(path))).stdout
    return rendered, document, langfuse


def synthetic_langfuse_secret(env):
    """The `langfuse-credentials` Secret's shape for one of the two synthetic renders.

    Checks `config.LANGFUSE_KEYS` presence directly rather than calling `config.load_langfuse_env`,
    which now dies on a credential-free environment. That refusal is real `eks deploy` behaviour
    and exactly what this offline render must not trip over: `CHECK_ENV` (the "langfuse not
    configured" render) is deliberately credential-free, to validate the manifest's
    `optional: true` secretKeyRef defense-in-depth on its own, without a live cluster or a live
    loader refusal in the way.

    Both of this module's own synthetic environments are all-or-nothing by construction
    (`CHECK_ENV`, `CHECK_LANGFUSE_ENV`), but the assertion below still names a half-configured
    one explicitly rather than letting `config.langfuse_secret` raise a bare `KeyError` on it, in
    case a future synthetic environment here stops being.
    """
    present = [key for key in config.LANGFUSE_KEYS if env.get(key)]
    if not present:
        return {}
    assert len(present) == len(config.LANGFUSE_KEYS), (
        f"synthetic env is half-configured for Langfuse: {present}"
    )
    return config.langfuse_secret(env)


def dump(document):
    """A values document as the YAML text `helm template -f` reads."""
    import yaml

    return yaml.safe_dump(document, sort_keys=False)


def example_images():
    """`manifest.published`-shaped example references for all four of our images.

    Example references rather than the build manifest, so the check needs no `demo.py publish`
    -- and asserting these four landed in the render is the same assertion a real deploy needs
    about its four ECR references.
    """
    return {
        service: {"repository": f"{EXAMPLE_REPOSITORY}/{service}", "tag": EXAMPLE_TAG}
        for service in core.IMAGES
    }


def example_image(service):
    """The container image an override for `service` has to produce."""
    return f"{EXAMPLE_REPOSITORY}/{service}:{EXAMPLE_TAG}"


def release_values_files(generated):
    """The release's values files in the one order that works: static first, generated second.

    Helm merges maps but *replaces* lists, and both files set `components.*.envOverrides`. The
    generated document carries the static entries through (see `values.env_overrides`), so
    generated-last keeps both halves; the reverse order silently drops every generated entry --
    the agent's model, the frontend's demo-details flag -- and is exactly what the rendered
    environment is checked for below.
    """
    return [config.k8s_dir() / values.STATIC_VALUES, generated]


def helm_template_args(values_paths):
    """`helm template` argv for the release, derived from the argv the deploy installs with.

    Derived rather than restated so the check cannot drift from the deploy: same release, same
    chart, same pinned version, same namespace and the same `-f` order. `--wait` and its
    timeout are install-time concerns and end the part a render shares.
    """
    upgrade = k8s.helm_upgrade_args(values_paths)
    args = []
    if upgrade[:3] == ["helm", "upgrade", "--install"] and "--wait" in upgrade:
        args = ["helm", "template", *upgrade[3 : upgrade.index("--wait")]]
    # The values files have to survive the cut as well. Without them the chart renders its own
    # defaults, which every assertion below would report as a dozen unrelated problems.
    if any(str(path) not in args for path in values_paths):
        core.die(
            "k8s.helm_upgrade_args is no longer `helm upgrade --install ... -f ... --wait`, so "
            "`eks check` can no longer render what `eks deploy` installs"
        )
    return args


def render_problems(rendered, generated, langfuse):
    """Everything wrong with one render of the chart, as a list of sentences.

    `generated` is the values document this render was made from and `langfuse` the credentials
    it was made with, both of which decide what the render has to contain.

    A list rather than a `die` per check: a chart bump tends to break several of these at once,
    and one run that names all of them is one run.
    """
    documents = load_documents(rendered)
    if not documents:
        return ["helm template rendered nothing at all"]
    # A list, not a mapping by name: names are unique per kind rather than per render, and a
    # workload dropped by a name collision is one nothing below would check.
    workloads = [document for document in documents if document.get("kind") in WORKLOAD_KINDS]
    return [
        *image_problems(workloads),
        *chatbot_problems(workloads),
        *agent_secret_problems(workloads),
        *environment_problems(workloads, generated),
        *collector_problems(documents, langfuse),
        *secret_leak_problems(rendered, langfuse),
    ]


def load_documents(rendered):
    """The non-empty documents of a `helm template` render."""
    import yaml

    return [document for document in yaml.safe_load_all(rendered) if document]


def containers(workload):
    """Every container of a workload, init containers included."""
    spec = workload.get("spec", {}).get("template", {}).get("spec", {})
    return [*spec.get("initContainers", []), *spec.get("containers", [])]


def images(workloads):
    """Every container image in the render."""
    return {
        container.get("image") for workload in workloads for container in containers(workload)
    }


def service_workload(workloads, service):
    """The workload running our image for `service`, found by that image rather than by name.

    By image because the image is the thing being checked: a workload named `agent` that runs
    the released image is the failure this looks for, not the workload it is looking for.
    """
    wanted = example_image(service)
    for workload in workloads:
        if any(container.get("image") == wanted for container in containers(workload)):
            return workload
    return None


def image_problems(workloads):
    """All four `imageOverride` repository/tag pairs have to reach a rendered workload."""
    rendered = images(workloads)
    return [
        f"no rendered workload runs {example_image(service)}: the {service} imageOverride "
        "(repository and tag) did not reach the release"
        for service in core.IMAGES
        if example_image(service) not in rendered
    ]


def chatbot_problems(workloads):
    """The demo's Gradio chatbot stays off: our assistant is in the storefront.

    Both halves are checked, because `components.chatbot.enabled: false` is one line in the
    static values and a chart bump could rename the component out from under it.
    """
    problems = [
        f"a chatbot workload is rendered ({workload['metadata']['name']}): "
        "components.chatbot.enabled is not taking"
        for workload in workloads
        if "chatbot" in workload["metadata"]["name"]
    ]
    return problems + [
        f"a rendered workload runs a chatbot image ({image})"
        for image in images(workloads)
        if image and "chatbot" in image
    ]


def agent_secret_problems(workloads):
    """The agent's three `langfuse-credentials` references, each still `optional: true`.

    Optional is the load-bearing half: `eks deploy` creates the Secret only when `.env` carries
    the keys and deletes it when it does not, so without `optional` a Langfuse-less deployment
    would leave the agent pod unable to start.

    An agent workload that is not in the render at all is `image_problems`' finding, not this
    one -- which is why there is nothing to report here in that case rather than nothing wrong.
    """
    agent = service_workload(workloads, "agent")
    if agent is None:
        return []
    environment = rendered_environment(agent)
    problems = []
    for key in config.LANGFUSE_KEYS:
        reference = (environment.get(key) or {}).get("valueFrom", {}).get("secretKeyRef")
        if not reference:
            problems.append(
                f"the agent Deployment has no {key} secretKeyRef: the optional "
                f"{config.SECRET_LANGFUSE} reference is not in the render"
            )
        elif reference.get("name") != config.SECRET_LANGFUSE or reference.get("key") != key:
            problems.append(
                f"the agent's {key} reads {reference.get('name')}/{reference.get('key')} "
                f"instead of {config.SECRET_LANGFUSE}/{key}"
            )
        elif reference.get("optional") is not True:
            problems.append(
                f"the agent's {key} reference to {config.SECRET_LANGFUSE} is not optional: "
                "the pod will not start on a deployment without Langfuse"
            )
    return problems


def rendered_environment(workload):
    """The workload's environment as a name -> entry mapping, first container wins.

    The demo's components run one container each; flagd is the exception and is not one of
    ours. A later entry never wins in Kubernetes either -- the first definition of a name is
    the one the kubelet applies -- so first is also the accurate reading.
    """
    environment = {}
    for container in containers(workload):
        for entry in container.get("env") or []:
            environment.setdefault(entry.get("name"), entry)
    return environment


def environment_problems(workloads, generated):
    """Both values files reached the rendered environment, which is what `-f` order decides.

    The expected names are derived from the two documents rather than listed, so this keeps
    checking the right thing as either file grows. A name missing from the static half means
    `values.env_overrides` stopped carrying the static entries through; a name missing from the
    generated half means the generated file was passed to Helm first, where the static list
    replaces it wholesale.

    The generated half is then checked by *value* as well, which `value_problems` explains: a
    name can keep rendering long after what it renders stopped being what the document asked
    for, and that is a release running the wrong configuration rather than a cosmetic drift.

    A service with no workload in the render is skipped here because `image_problems` has
    already reported it; there is no environment to read either way.
    """
    static = values.static_values().get("components", {})
    components = generated.get("components", {})
    problems = []
    for service in core.IMAGES:
        workload = service_workload(workloads, service)
        if workload is None:
            continue
        rendered = rendered_environment(workload)
        for source, document in (("demo-values.yaml", static), ("the generated values", components)):
            expected = {entry.get("name") for entry in component_overrides(document, service)}
            missing = sorted(name for name in expected if name not in rendered)
            if missing:
                problems.append(
                    f"the {service} workload is missing {', '.join(missing)} from {source}: "
                    "helm replaces lists, so the values files have to be passed "
                    "demo-values.yaml first"
                )
        problems += value_problems(service, rendered, component_overrides(components, service))
    return problems


def component_overrides(document, service):
    """One component's `envOverrides` entries out of a values document's `components` map.

    Not `env_overrides`, which is `values.env_overrides` -- the three-argument function that
    *builds* these lists, cited by name in this module twice. Reading a list out of a
    document and assembling one are not the same question.
    """
    return (document.get(service) or {}).get("envOverrides") or []


def value_problems(service, rendered, entries):
    """Every generated `envOverrides` value reached the render as the value it was generated as.

    Names alone are not enough, and the storefront's session-replay flag is why: every
    generated value is *derived* -- from `.env`, from the build manifest, or from another value
    -- so a name goes on rendering while what it renders stops being what the document asked
    for. That is the wrong model, the wrong endpoint, or a session recorded against the
    operator's instruction, and none of it moves a name.

    Only the generated half is compared this way. Its entries always carry a literal value,
    while the static half sets three of the agent's from `langfuse-credentials` `secretKeyRef`s
    -- entries with no literal to compare, which is what the `expected is None` skip passes
    over.

    A name the render dropped entirely is the missing-name finding above, not this one, so it
    is passed over here rather than reported twice.
    """
    problems = []
    for entry in entries:
        name, expected = entry.get("name"), entry.get("value")
        if expected is None or name not in rendered:
            continue
        found = entry_value(rendered[name])
        if found == expected:
            continue
        problems.append(
            f"the {service} workload renders {name} as {found!r} rather than the generated "
            f"{expected!r}: the render disagrees with the values document it was made from"
        )
    return problems


def entry_value(entry):
    """What a rendered env entry carries: its literal value, or the reference it reads instead.

    The literal is stringified because YAML decides the type and Kubernetes does not: an
    unquoted `value: 3` parses as an int against a values document whose every generated value
    is `str()`-ed. An entry with no `value` at all is an empty one, which is how the kubelet
    reads it -- reporting it as `None` against a generated `""` would be a problem that is not
    one. A reference comes back as the mapping it is, which can never compare equal to a
    literal and so gets named in full: a generated value that arrived from a Secret is as
    wrong as one that arrived with the wrong text.
    """
    if "valueFrom" in entry:
        return entry["valueFrom"]
    return str(entry.get("value", ""))


def collector_configs(documents):
    """Every rendered collector configuration, keyed by the ConfigMap that carries it.

    The subchart writes its configuration into one ConfigMap key (`relay` in chart 0.41.2), so
    the key is found by shape rather than by name: any value that parses as a mapping with
    `service.pipelines` in it is a collector configuration.
    """
    import yaml

    found = {}
    for document in documents:
        if document.get("kind") != "ConfigMap":
            continue
        for key, value in (document.get("data") or {}).items():
            try:
                parsed = yaml.safe_load(value)
            except yaml.YAMLError:
                continue
            if isinstance(parsed, dict) and (parsed.get("service") or {}).get("pipelines"):
                found[f"{document['metadata']['name']}/{key}"] = parsed
    return found


def collector_problems(documents, langfuse):
    """The chart-bump tripwire: no pipeline may name a component nobody defines.

    The collector refuses to start on an undefined reference, so a chart that renames or drops
    a processor -- `transform/sanitize_spans` and `gen_ai_normalizer` are both ours to lose --
    has to fail here rather than as a CrashLooping pod on a live cluster. Connectors count as
    definitions for both receivers and exporters, which is what `span_metrics` is.
    """
    configs = collector_configs(documents)
    if not configs:
        return ["no rendered ConfigMap holds a collector configuration"]

    problems = []
    for name, parsed in configs.items():
        connectors = set(parsed.get("connectors") or {})
        for pipeline, definition in parsed["service"]["pipelines"].items():
            for kind in ("receivers", "processors", "exporters"):
                defined = set(parsed.get(kind) or {})
                if kind != "processors":
                    defined |= connectors
                missing = [
                    component
                    for component in definition.get(kind) or []
                    if component not in defined
                ]
                if missing:
                    problems.append(
                        f"{name} pipeline {pipeline} names {kind} that nothing defines: "
                        f"{', '.join(missing)}"
                    )
        extensions = set(parsed.get("extensions") or {})
        unknown = [
            extension
            for extension in parsed["service"].get("extensions") or []
            if extension not in extensions
        ]
        if unknown:
            problems.append(f"{name} enables extensions that nothing defines: {', '.join(unknown)}")
        problems += langfuse_pipeline_problems(name, parsed, langfuse)
    return problems


def langfuse_pipeline_problems(name, parsed, langfuse):
    """The conditional half: the Langfuse pipeline is always there, its exporter is not.

    An `otlphttp` exporter whose `${env:LANGFUSE_BASE_URL}` is unset fails the collector's own
    validation, so the exporter appears exactly with the credentials while the pipeline that
    filters the assistant's spans stays valid either way -- exporting to `debug`, the way the
    laptop exports to its preview file.
    """
    pipeline = parsed["service"]["pipelines"].get(values.LANGFUSE_PIPELINE)
    if pipeline is None:
        return [f"{name} has no {values.LANGFUSE_PIPELINE} pipeline"]

    expected = values.LANGFUSE_EXPORTER if langfuse else values.PREVIEW_EXPORTER
    problems = []
    if pipeline.get("exporters") != [expected]:
        problems.append(
            f"{name} pipeline {values.LANGFUSE_PIPELINE} exports to "
            f"{pipeline.get('exporters')} rather than [{expected}]"
        )
    defined = values.LANGFUSE_EXPORTER in (parsed.get("exporters") or {})
    if defined and not langfuse:
        problems.append(
            f"{name} defines {values.LANGFUSE_EXPORTER} with no Langfuse credentials: an "
            "otlphttp exporter with an unset ${env:...} endpoint CrashLoops the collector"
        )
    if langfuse and not defined:
        problems.append(f"{name} does not define {values.LANGFUSE_EXPORTER}")
    return problems


def secret_leak_problems(rendered, langfuse):
    """No credential may be rendered: they travel as `${env:...}` and as `secretKeyRef`s.

    The key pair and the header derived from it, not the base URL and not
    `LANGFUSE_PUBLIC_URL`: an endpoint is configuration, and the storefront's trace links are
    rendered on purpose. The header is also checked without its `Basic ` scheme, because the
    base64 payload is the credential itself.

    The values are the synthetic ones above, so a hit is a leak in the code rather than a
    leaked credential -- which is the point of checking it with fakes.
    """
    problems = []
    for key in CREDENTIAL_KEYS:
        value = langfuse.get(key)
        payloads = [part for part in (value, (value or "").split(" ")[-1]) if part]
        if any(payload in rendered for payload in payloads):
            problems.append(
                f"the render contains the Langfuse {key} value: credentials belong in the "
                f"{config.SECRET_LANGFUSE} Secret, not in a rendered manifest"
            )
    return problems
