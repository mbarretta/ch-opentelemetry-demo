import json

import pytest
import yaml

from scripts import demo

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


def test_manifest_records_build_and_merges_per_service(inputs):
    demo.write_manifest("abc123def456", "linux/arm64", {"frontend": "sha256:1111"})
    manifest = json.loads((demo.RUNTIME / "images/manifest.json").read_text())
    assert manifest["tag"] == "abc123def456"
    assert manifest["platform"] == "linux/arm64"
    assert manifest["upstream_commit"] == demo.COMMIT
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
    with pytest.raises(SystemExit, match="astronomy-concierge-frontend:abc123def456.*build"):
        demo.require_images()
    monkeypatch.setattr(demo, "image_exists", lambda image: True)
    assert demo.require_images()["tag"] == "abc123def456"


def test_environment_points_compose_at_the_built_frontend_image(inputs, monkeypatch):
    monkeypatch.setattr(demo, "ROOT", demo.RUNTIME.parent)
    (demo.RUNTIME.parent / ".env").write_text("SHOP_PORT=8080\n")
    assert demo.environment()["CONCIERGE_FRONTEND_IMAGE"] == "astronomy-concierge-frontend:unbuilt"
    demo.write_manifest("abc123def456", "linux/arm64", {"frontend": "sha256:1111"})
    env = demo.environment()
    assert env["CONCIERGE_FRONTEND_IMAGE"] == "astronomy-concierge-frontend:abc123def456"
    assert env["DEMO_VERSION"] == "3.0.0", "the shop images stay on the released tag"


class ComposeLoader(yaml.SafeLoader):
    """Accepts Compose's `!reset` override tag."""


ComposeLoader.add_constructor("!reset", lambda loader, node: None)


def test_native_override_replaces_only_the_frontend_image():
    override = yaml.load((demo.ROOT / "compose.native.yaml").read_text(), Loader=ComposeLoader)
    assert set(override["services"]) == {"frontend"}
    frontend = override["services"]["frontend"]
    assert frontend["image"] == "${CONCIERGE_FRONTEND_IMAGE}"
    assert frontend["pull_policy"] == "never"


def test_root_dockerignore_excludes_secrets_and_runtime_state():
    rules = (demo.ROOT / ".dockerignore").read_text().splitlines()
    for rule in (".env", ".venv", ".upstream", ".cache", ".runtime/**", ".runtime/telemetry"):
        assert rule in rules, rule
    assert any(rule.endswith("*.jsonl") for rule in rules)
    assert "!.runtime/tools.py" in rules
