import json
import re
import shlex
import shutil

import pytest
import yaml

from scripts import demo

DOCKERFILES = {
    "agent": demo.DOCKER / "agent.Dockerfile",
    "mcp": demo.DOCKER / "mcp.Dockerfile",
    "frontend-proxy": demo.DOCKER / "frontend-proxy.Dockerfile",
}

# One context line, as `git diff` always emits; `git apply` rejects zero-context hunks.
PATCH_TEMPLATE = """--- a/src/frontend/README.md
+++ b/src/frontend/README.md
@@ -1,2 +1,2 @@
-{old}
+{new}
 
"""


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    overlay = tmp_path / "overlay"
    patches = tmp_path / "patches"
    overlay.mkdir()
    patches.mkdir()
    monkeypatch.setattr(demo, "OVERLAY", overlay)
    monkeypatch.setattr(demo, "PATCHES", patches)
    monkeypatch.setattr(demo, "RUNTIME", tmp_path / "runtime")
    return overlay, patches


def upstream_readme_first_line():
    return demo.upstream_file("src/frontend/README.md").decode().splitlines()[0]


def test_tag_is_stable_and_changes_with_overlay_and_patches(inputs):
    overlay, patches = inputs
    (overlay / "README.md").write_text("docs only; excluded from the build inputs\n")
    empty = demo.build_tag()
    assert demo.build_tag() == empty
    assert len(empty) == 12 and int(empty, 16) >= 0

    component = overlay / "components/Assistant/Panel.tsx"
    component.parent.mkdir(parents=True)
    component.write_text("export const Panel = () => null;\n")
    with_overlay = demo.build_tag()
    assert with_overlay != empty

    component.write_text("export const Panel = () => <div />;\n")
    assert demo.build_tag() != with_overlay

    (overlay / "README.md").write_text("changed docs\n")
    assert demo.build_tag() == demo.build_tag()
    edited = demo.build_tag()
    component.write_text("export const Panel = () => null;\n")
    assert demo.build_tag() == with_overlay, "overlay README must not influence the tag"
    assert edited != with_overlay

    first = upstream_readme_first_line()
    (patches / "0001-readme.patch").write_text(PATCH_TEMPLATE.format(old=first, new="# Patched"))
    assert demo.build_tag() != with_overlay


def test_stage_exports_pin_copies_overlay_and_applies_patches(inputs):
    overlay, patches = inputs
    build = demo.RUNTIME / "build"
    build.mkdir(parents=True)
    (build / "stale.txt").write_text("left over from an earlier stage\n")
    (overlay / "README.md").write_text("docs only\n")
    component = overlay / "components/Assistant/Panel.tsx"
    component.parent.mkdir(parents=True)
    component.write_text("export const Panel = () => null;\n")
    first = upstream_readme_first_line()
    (patches / "0001-readme.patch").write_text(PATCH_TEMPLATE.format(old=first, new="# Patched"))

    demo.stage()

    assert build.is_dir() and not build.is_symlink()
    assert not (build / "stale.txt").exists(), "restaging must start from scratch"
    assert (build / "src/frontend/Dockerfile").read_bytes() == demo.upstream_file(
        "src/frontend/Dockerfile"
    )
    assert (build / "src/frontend/protos/demo.ts").exists()
    assert (build / "src/frontend/components/Assistant/Panel.tsx").read_text() == component.read_text()
    patched = (build / "src/frontend/README.md").read_text()
    assert patched.splitlines()[0] == "# Patched"
    assert "docs only" not in patched, "the overlay README is documentation, not an overlay file"
    ignored = (build / ".dockerignore").read_text().splitlines()
    for rule in (".git", "**/.env", ".venv", ".runtime", ".upstream", ".cache", "**/*.jsonl"):
        assert rule in ignored, rule
    assert "src/frontend/node_modules" in ignored


def test_stage_rejects_upstream_pin_mismatch(inputs, monkeypatch):
    monkeypatch.setattr(demo, "COMMIT", "0" * 40)
    with pytest.raises(SystemExit, match="Expected upstream 0{40}"):
        demo.stage()
    assert not (demo.RUNTIME / "build").exists()


def test_stage_rejects_patch_that_does_not_apply(inputs):
    _, patches = inputs
    (patches / "0002-broken.patch").write_text(
        PATCH_TEMPLATE.format(old="this line is not in the upstream README", new="# Nope")
    )
    with pytest.raises(SystemExit, match="0002-broken.patch"):
        demo.stage()


@pytest.fixture
def agent_inputs(tmp_path, monkeypatch):
    """Copies of the agent-side build inputs the tag covers, so a test can edit them."""
    copies = {}
    for name in ("concierge", "prompts", "docker"):
        target = tmp_path / name
        shutil.copytree(
            getattr(demo, name.upper()), target, ignore=shutil.ignore_patterns("__pycache__")
        )
        monkeypatch.setattr(demo, name.upper(), target)
        copies[name] = target
    return copies


def test_tag_changes_with_prompts_concierge_tools_and_docker_inputs(
    inputs, agent_inputs, monkeypatch
):
    base = demo.build_tag()
    assert demo.build_tag() == base

    prompt = agent_inputs["prompts"] / "concierge-v1.txt"
    prompt.write_text(prompt.read_text() + "\nAlways mention the return policy.\n")
    with_prompt = demo.build_tag()
    assert with_prompt != base, "prompts are baked into the agent image"

    (agent_inputs["concierge"] / "extra.py").write_text("VALUE = 1\n")
    with_code = demo.build_tag()
    assert with_code != with_prompt
    (agent_inputs["concierge"] / "__pycache__").mkdir()
    (agent_inputs["concierge"] / "__pycache__/extra.cpython-314.pyc").write_bytes(b"\x00")
    assert demo.build_tag() == with_code, "bytecode caches are not build inputs"
    (agent_inputs["docker"] / "README.md").write_text("docs only\n")
    assert demo.build_tag() == with_code, "documentation is not a build input"

    envoy = agent_inputs["docker"] / "envoy.tmpl.yaml"
    envoy.write_text(envoy.read_text().replace("timeout: 120s", "timeout: 150s"))
    with_proxy = demo.build_tag()
    assert with_proxy != with_code

    bases = agent_inputs["docker"] / "base-images.json"
    bases.write_text(bases.read_text().replace("sha256:", "sha256:0", 1))
    assert demo.build_tag() != with_proxy, "base digests are build inputs"

    with_bases = demo.build_tag()
    first_old, first_new = demo.TOOLS_PATCH[0]
    edited_patch = ((first_old, first_new + "  # changed"), *demo.TOOLS_PATCH[1:])
    monkeypatch.setattr(demo, "TOOLS_PATCH", edited_patch)
    assert demo.build_tag() != with_bases, "the corrected tools.py is a build input"


def test_manifest_records_build_and_merges_per_service(inputs):
    demo.write_manifest("abc123def456", "linux/arm64", {"frontend": "sha256:1111"})
    manifest = json.loads((demo.RUNTIME / "images/manifest.json").read_text())
    assert manifest["tag"] == "abc123def456"
    assert manifest["platform"] == "linux/arm64"
    assert manifest["upstream_commit"] == demo.COMMIT
    assert manifest["contract_version"] == "1"
    assert manifest["base_images"] == demo.base_images()
    assert set(manifest["base_images"]) == {"agent", "mcp", "frontend-proxy"}
    assert manifest["images"]["frontend"] == {
        "image": "astronomy-concierge-frontend:abc123def456",
        "id": "sha256:1111",
    }
    demo.write_manifest("abc123def456", "linux/arm64", {"other": "sha256:2222"})
    merged = json.loads((demo.RUNTIME / "images/manifest.json").read_text())
    assert set(merged["images"]) == {"frontend", "other"}


def test_up_refuses_to_start_without_built_images(inputs, monkeypatch):
    with pytest.raises(SystemExit, match="scripts/demo.py build"):
        demo.require_images()
    demo.write_manifest("abc123def456", "linux/arm64", {"frontend": "sha256:1111"})
    monkeypatch.setattr(demo, "image_exists", lambda image: False)
    with pytest.raises(SystemExit) as failure:
        demo.require_images()
    message = str(failure.value)
    assert "astronomy-concierge-frontend:abc123def456" in message
    for service in ("agent", "mcp", "frontend-proxy"):
        assert f"{service} (never built)" in message, message
    assert "scripts/demo.py build" in message
    monkeypatch.setattr(demo, "image_exists", lambda image: True)
    with pytest.raises(SystemExit, match="agent \\(never built\\)"):
        demo.require_images()
    demo.write_manifest(
        "abc123def456",
        "linux/arm64",
        {"agent": "sha256:2", "mcp": "sha256:3", "frontend-proxy": "sha256:4"},
    )
    assert demo.require_images()["tag"] == "abc123def456"


def test_environment_points_compose_at_the_built_images(inputs, monkeypatch):
    monkeypatch.setattr(demo, "ROOT", demo.RUNTIME.parent)
    (demo.RUNTIME.parent / ".env").write_text("SHOP_PORT=8080\n")
    unbuilt = demo.environment()
    assert unbuilt["CONCIERGE_FRONTEND_IMAGE"] == "astronomy-concierge-frontend:unbuilt"
    assert unbuilt["CONCIERGE_FRONTEND_PROXY_IMAGE"] == "astronomy-concierge-frontend-proxy:unbuilt"
    demo.write_manifest(
        "abc123def456",
        "linux/arm64",
        {
            "frontend": "sha256:1",
            "agent": "sha256:2",
            "mcp": "sha256:3",
            "frontend-proxy": "sha256:4",
        },
    )
    env = demo.environment()
    assert env["CONCIERGE_FRONTEND_IMAGE"] == "astronomy-concierge-frontend:abc123def456"
    assert env["CONCIERGE_AGENT_IMAGE"] == "astronomy-concierge-agent:abc123def456"
    assert env["CONCIERGE_MCP_IMAGE"] == "astronomy-concierge-mcp:abc123def456"
    assert (
        env["CONCIERGE_FRONTEND_PROXY_IMAGE"] == "astronomy-concierge-frontend-proxy:abc123def456"
    )
    assert env["DEMO_VERSION"] == "3.0.0", "the shop images stay on the released tag"


class ComposeLoader(yaml.SafeLoader):
    """Accepts Compose's override tags: `!reset` reads as None, `!override` as its plain value."""


ComposeLoader.add_constructor("!reset", lambda loader, node: None)
ComposeLoader.add_constructor(
    "!override",
    lambda loader, node: (
        loader.construct_sequence(node)
        if isinstance(node, yaml.SequenceNode)
        else loader.construct_mapping(node)
        if isinstance(node, yaml.MappingNode)
        else loader.construct_scalar(node)
    ),
)


def environment_of(service):
    """Compose list-form environment (KEY=VALUE) as a mapping."""
    return dict(entry.split("=", 1) for entry in service["environment"])


def compose_override(name):
    return yaml.load((demo.ROOT / name).read_text(), Loader=ComposeLoader)["services"]


def test_native_override_selects_the_four_images_and_drops_the_chatbot_dependency():
    services = compose_override("compose.native.yaml")
    assert set(services) == {"frontend", "agent", "mcp", "frontend-proxy", "chatbot"}
    for service in ("frontend", "agent", "mcp", "frontend-proxy"):
        assert services[service]["image"] == "${%s}" % demo.image_variable(service), service
        assert services[service]["pull_policy"] == "never"
        assert "volumes" not in services[service] or services[service]["volumes"] is None, (
            "the native stack runs from images: any volumes key must be a !reset"
        )
    assert "chatbot" in services["frontend-proxy"]["depends_on"]
    assert services["frontend-proxy"]["depends_on"]["chatbot"] is None, "must be a !reset"
    assert services["chatbot"]["profiles"] == ["debug"]
    frontend_env = environment_of(services["frontend"])
    assert frontend_env["AGENT_BASE_URL"] == "${AGENT_BASE_URL:-http://agent:8010}"
    assert frontend_env["ASSISTANT_TRANSPORT"] == "${ASSISTANT_TRANSPORT:-live}"


def test_source_mounts_live_only_in_the_dev_override():
    dev = compose_override("compose.dev.yaml")
    assert set(dev) == {"agent", "mcp"}
    assert "${CONCIERGE_ROOT}/concierge:/app/concierge:ro" in dev["agent"]["volumes"]
    assert "${CONCIERGE_ROOT}/prompts:/app/prompts:ro" in dev["agent"]["volumes"]
    assert (
        "${CONCIERGE_ROOT}/.runtime/tools.py:/app/src/agents/tools.py:ro" in dev["agent"]["volumes"]
    )
    assert (
        "${CONCIERGE_ROOT}/.runtime/tools.py:/app/src/mcp_server/tools.py:ro"
        in dev["mcp"]["volumes"]
    )
    concierge = compose_override("compose.concierge.yaml")
    for service in ("agent", "mcp"):
        assert "volumes" not in concierge[service], f"{service} mounts belong in compose.dev.yaml"
        assert "command" not in concierge[service], f"{service} runs its image entry point"
    # The Gradio client has no image of its own; it is the released chatbot plus the package.
    assert any(":/app/concierge:ro" in mount for mount in concierge["chatbot"]["volumes"])
    for service in ("frontend-proxy", "flagd", "flagd-ui", "otel-collector"):
        assert service in concierge, f"{service} overrides stay in compose.concierge.yaml"


def test_up_flags_select_dev_mounts_and_the_debug_chatbot(inputs, monkeypatch):
    calls = []
    monkeypatch.setattr(demo, "run", lambda *args, **kwargs: calls.append(args))
    monkeypatch.setattr(demo, "generate_config", lambda env: None)
    env = {"SHOP_PORT": "8080"}

    demo.compose(env, ["up", "-d"])
    default = shlex.join(calls[-1])
    assert "compose.native.yaml" in default
    assert "compose.dev.yaml" not in default and "--profile" not in default

    demo.compose(env, ["up", "-d"], dev=True)
    assert "compose.dev.yaml" in shlex.join(calls[-1])
    files = [arg for arg in calls[-1] if str(arg).endswith(".yaml")]
    assert files.index(str(demo.ROOT / "compose.dev.yaml")) > files.index(
        str(demo.ROOT / "compose.native.yaml")
    ), "dev mounts override the native images"

    demo.compose(env, ["up", "-d"], debug_chatbot=True)
    argv = [str(arg) for arg in calls[-1]]
    assert argv.index("--profile") + 1 == argv.index("debug") < argv.index("up")


def test_root_dockerignore_excludes_secrets_and_runtime_state():
    rules = (demo.ROOT / ".dockerignore").read_text().splitlines()
    for rule in (".env", ".venv", ".upstream", ".cache", ".runtime/**", ".runtime/telemetry"):
        assert rule in rules, rule
    assert any(rule.endswith("*.jsonl") for rule in rules)
    assert "!.runtime/tools.py" in rules


def test_root_dockerignore_keeps_docs_compose_and_env_out_of_the_agent_contexts():
    rules = (demo.ROOT / ".dockerignore").read_text().splitlines()
    for rule in (".env.example", "compose*.yaml", "**/*.md", "frontend", "tests", "scripts"):
        assert rule in rules, rule
    assert "!.env.example" not in rules, "the example env names model and agent addresses"


def dockerfile_instructions(path):
    text = re.sub(r"\\\n\s*", " ", path.read_text())
    return [
        line.split(None, 1)
        for line in text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


def test_dockerfiles_extend_the_pinned_released_images():
    bases = demo.base_images()
    recorded = json.loads((demo.DOCKER / "base-images.json").read_text())
    assert "docker buildx imagetools inspect" in recorded["resolved_with"]
    for service, path in DOCKERFILES.items():
        instructions = dockerfile_instructions(path)
        froms = [args for keyword, args in instructions if keyword == "FROM"]
        assert froms == [bases[service]], service
        assert re.fullmatch(
            rf"ghcr\.io/open-telemetry/demo:3\.0\.0-{service}@sha256:[0-9a-f]{{64}}", bases[service]
        )
        assert recorded["images"][service]["platforms"].keys() >= {"linux/amd64", "linux/arm64"}


def test_python_images_bake_the_package_prompts_and_corrected_tools():
    expectations = {
        "agent": ("src/agents/tools.py", "concierge.run_agent"),
        "mcp": ("src/mcp_server/tools.py", "concierge.run_mcp"),
    }
    for service, (tools_target, module) in expectations.items():
        instructions = dockerfile_instructions(DOCKERFILES[service])
        copies = [args.split() for keyword, args in instructions if keyword == "COPY"]
        assert ["concierge", "/app/concierge"] in copies, service
        assert ["prompts", "/app/prompts"] in copies, service
        assert [".runtime/tools.py", f"/app/{tools_target}"] in copies, service
        cmds = [json.loads(args) for keyword, args in instructions if keyword == "CMD"]
        assert cmds == [["python", "-m", module]], service


def test_proxy_image_replaces_only_the_envoy_template():
    instructions = dockerfile_instructions(DOCKERFILES["frontend-proxy"])
    copies = [args for keyword, args in instructions if keyword == "COPY"]
    assert copies == ["--chown=envoy:envoy docker/envoy.tmpl.yaml /home/envoy/envoy.tmpl.yaml"]
    assert not any(
        keyword in {"ENTRYPOINT", "CMD", "USER", "WORKDIR"} for keyword, _ in instructions
    )


def envoy_routes(config):
    manager = config["static_resources"]["listeners"][0]["filter_chains"][0]["filters"][0]
    return manager["typed_config"]["route_config"]["virtual_hosts"][0]["routes"]


def envoy_clusters(config):
    return {cluster["name"]: cluster for cluster in config["static_resources"]["clusters"]}


def test_envoy_template_adds_the_assistant_route_and_keeps_the_rest():
    ours_text = (demo.DOCKER / "envoy.tmpl.yaml").read_text()
    upstream_text = demo.upstream_file("src/frontend-proxy/envoy.tmpl.yaml").decode()
    ours, upstream = yaml.safe_load(ours_text), yaml.safe_load(upstream_text)

    routes = envoy_routes(ours)
    assistant = [route for route in routes if route["match"] == {"prefix": "/api/assistant/"}]
    assert len(assistant) == 1
    assert assistant[0]["route"]["cluster"] == "frontend-assistant"
    timeout = assistant[0]["route"]["timeout"]
    assert timeout.endswith("s") and float(timeout[:-1]) >= 100
    catch_all = [index for index, route in enumerate(routes) if route["match"] == {"prefix": "/"}]
    assert catch_all == [len(routes) - 1], "the catch-all stays last"
    assert routes.index(assistant[0]) < catch_all[0]

    kept = [route for route in routes if route is not assistant[0]]
    expected = [route for route in envoy_routes(upstream) if "chatbot" not in json.dumps(route)]
    assert kept == expected, "every non-chatbot upstream route survives with the same timeout"

    clusters, released = envoy_clusters(ours), envoy_clusters(upstream)
    assert set(clusters) == set(released) - {"chatbot"} | {"frontend-assistant"}
    for name, cluster in clusters.items():
        if name == "frontend-assistant":
            # The storefront under a second cluster name, so the egress span Envoy emits for an
            # assistant request (which carries no URL) can be told apart by the collector.
            expected = {
                **released["frontend"],
                "name": "frontend-assistant",
                "load_assignment": {
                    **released["frontend"]["load_assignment"],
                    "cluster_name": "frontend-assistant",
                },
            }
            assert cluster == expected
        else:
            assert cluster == released[name], name

    placeholders = set(re.findall(r"\$\{[A-Z_]+\}", ours_text))
    assert placeholders == set(re.findall(r"\$\{[A-Z_]+\}", upstream_text)) - {
        "${CHATBOT_HOST}",
        "${CHATBOT_PORT}",
    }
    for name in ("admin", "layered_runtime"):
        assert ours[name] == upstream[name]
