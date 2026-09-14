"""The four images: their build inputs, the content tag over them, staging, and the manifest."""

import hashlib
import json
import shutil
import sys
import tarfile

from . import core, upstream

STAGED_IMAGES = {"frontend"}
# Upstream files whose content, together with the overlay and patches, determines the tag.
FRONTEND_INPUTS = ("src/frontend/Dockerfile", "src/frontend/package-lock.json")
STAGED_DOCKERIGNORE = """
###################################
# astronomy-concierge staging: keep secrets, tooling state, and captures out of the context
.git
**/.env
.venv
.runtime
.upstream
.cache
**/*.jsonl
src/frontend/node_modules
src/frontend/.next
src/frontend/tsconfig.tsbuildinfo
src/frontend/cypress/screenshots
src/frontend/cypress/videos
src/frontend/cypress/downloads
###################################
"""


def overlay_files():
    """(path relative to src/frontend, source) pairs; the overlay's own README is documentation."""
    if not core.OVERLAY.is_dir():
        return []
    files = [path for path in core.OVERLAY.rglob("*") if path.is_file()]
    return sorted(
        (str(path.relative_to(core.OVERLAY)), path)
        for path in files
        if path != core.OVERLAY / "README.md"
    )


def patch_files():
    return sorted(core.PATCHES.glob("*.patch")) if core.PATCHES.is_dir() else []


def source_files(root):
    """(path relative to root, path) pairs for a directory the images copy.

    Bytecode caches and Markdown are not build inputs: .dockerignore keeps them out of the
    context, and like the overlay README they must not change the tag.
    """
    return sorted(
        (str(path.relative_to(root)), path)
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in {".pyc", ".md"}
    )


def base_images():
    """service -> released image reference pinned by digest, from docker/base-images.json."""
    recorded = json.loads((core.DOCKER / "base-images.json").read_text())["images"]
    return {service: f"{entry['image']}@{entry['digest']}" for service, entry in recorded.items()}


def build_tag():
    """Short digest over everything that shapes the four images.

    Frontend: upstream commit, released Dockerfile and lockfile, overlay, patches. Agent, mcp,
    proxy: docker/ (Dockerfiles with their base digests, base-images.json, the Envoy template),
    concierge/, prompts/, and the corrected tools.py derived from the pin and TOOLS_PATCH.
    """
    digest = hashlib.sha256()

    def add(label, data):
        digest.update(f"{label}\0{len(data)}\0".encode())
        digest.update(data)

    add("upstream", core.COMMIT.encode())
    for path in FRONTEND_INPUTS:
        add(path, upstream.upstream_file(path))
    for relative, path in overlay_files():
        add(f"overlay/{relative}", path.read_bytes())
    for path in patch_files():
        add(f"patch/{path.name}", path.read_bytes())
    add("tools.py", upstream.corrected_tools().encode())
    inputs = (("docker", core.DOCKER), ("concierge", core.CONCIERGE), ("prompts", core.PROMPTS))
    for label, root in inputs:
        for relative, path in source_files(root):
            add(f"{label}/{relative}", path.read_bytes())
    return digest.hexdigest()[:12]


def stage():
    """Export the pinned upstream commit to .runtime/build, then add the overlay and patches.

    The export is its own index-only git repository so `git apply` resolves paths against the
    staged tree (inside the project repository they would silently be skipped) and so
    `git -C .runtime/build diff -- src/frontend` shows exactly what the patches change.
    """
    upstream.verify_upstream()
    build = core.RUNTIME / "build"
    if build.is_symlink() or build.is_file():
        build.unlink()
    elif build.exists():
        shutil.rmtree(build)
    build.mkdir(parents=True)
    export = ("git", "-C", core.UPSTREAM, "archive", "--format=tar", core.COMMIT)
    with (
        core.popen(*export) as archive,
        tarfile.open(fileobj=archive.stdout, mode="r|") as tar,
    ):
        tar.extractall(build, filter="data")
    if archive.returncode != 0:
        raise SystemExit(f"git archive of upstream {core.COMMIT} failed.")
    ignore = build / ".dockerignore"
    ignore.write_text(ignore.read_text() + STAGED_DOCKERIGNORE)
    core.run("git", "-c", "init.defaultBranch=main", "init", "-q", str(build))
    core.run("git", "-C", str(build), "-c", "core.safecrlf=false", "add", "-A")
    frontend = build / "src/frontend"
    overlay = overlay_files()
    for relative, source in overlay:
        target = frontend / relative
        if target.exists():
            print(f"Overlay file {relative} replaces an upstream file; prefer a patch for edits.")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    patches = patch_files()
    for patch in patches:
        check = core.capture(
            "git", "-C", build, "apply", "--check", patch, check=False, stderr=core.PIPE
        )
        if check.returncode != 0:
            raise SystemExit(
                f"Patch {patch.name} does not apply to upstream {core.TAG}:\n{check.stderr.strip()}"
            )
        core.run("git", "-C", str(build), "apply", str(patch))
    print(
        f"Staged upstream {core.TAG} ({core.COMMIT[:12]}) with {len(overlay)} overlay files and "
        f"{len(patches)} patches in {build}."
    )


def host_platform():
    return core.capture(
        "docker", "version", "--format", "{{.Server.Os}}/{{.Server.Arch}}"
    ).stdout.strip()


def image_name(service, tag):
    return f"{core.IMAGE_PREFIX}-{service}:{tag}"


def image_variable(service):
    return f"CONCIERGE_{service.upper().replace('-', '_')}_IMAGE"


def image_exists(image):
    probe = core.run(
        "docker", "image", "inspect", image, check=False, stdout=core.DEVNULL, stderr=core.DEVNULL
    )
    return probe.returncode == 0


# The manifest's optional `published` section, written by `demo.py publish` and read by the EKS
# deploy: published[service] = {"repository": ECR repository URL, "tag": the build tag that was
# pushed, "digest": the registry manifest digest}. It is the contract between publishing an image
# and pointing a Helm release at it, and write_manifest() carries it through untouched.
PUBLISHED_FIELDS = ("repository", "tag", "digest")


def manifest_path():
    return core.RUNTIME / "images/manifest.json"


def read_manifest():
    """The recorded build, or None before the first one.

    Shape: tag, platform, upstream_commit, contract_version, base_images, images[service] =
    {image, id}, and the optional published[service] = {repository, tag, digest}.
    """
    path = manifest_path()
    return json.loads(path.read_text()) if path.exists() else None


def import_root():
    """Make the concierge package importable when this file runs as a script."""
    if str(core.ROOT) not in sys.path:
        sys.path.insert(0, str(core.ROOT))


def contract_version():
    import_root()
    from concierge.contract import CONTRACT_VERSION

    return CONTRACT_VERSION


def write_manifest(tag, platform, image_ids):
    """Record the build; services built separately keep their entries.

    Only the keys listed here are rewritten, so a `published` section (see PUBLISHED_FIELDS)
    round-trips through a later build of one service instead of being dropped.
    """
    manifest = read_manifest() or {}
    images = manifest.get("images", {})
    for service, image_id in image_ids.items():
        images[service] = {"image": image_name(service, tag), "id": image_id}
    manifest.update(
        {
            "tag": tag,
            "platform": platform,
            "upstream_commit": core.COMMIT,
            "contract_version": contract_version(),
            "base_images": base_images(),
            "images": images,
        }
    )
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


def build_context(service):
    return core.RUNTIME / "build" if service in STAGED_IMAGES else core.ROOT


def build(services, platform=None):
    unknown = sorted(set(services) - set(core.IMAGES))
    if unknown:
        raise SystemExit(
            f"Unknown service(s) {', '.join(unknown)}; choose from {', '.join(core.IMAGES)}."
        )
    if STAGED_IMAGES & set(services):
        stage()
    upstream.write_tools()
    tag = build_tag()
    platform = platform or host_platform()
    image_ids = {}
    for service in services:
        image = image_name(service, tag)
        context = build_context(service)
        print(f"Building {image} for {platform} from {context}/{core.IMAGES[service]}")
        core.run(
            "docker",
            "build",
            "--platform",
            platform,
            "--file",
            str(context / core.IMAGES[service]),
            "--tag",
            image,
            str(context),
        )
        image_ids[service] = core.capture(
            "docker", "image", "inspect", "--format", "{{.Id}}", image
        ).stdout.strip()
    manifest = write_manifest(tag, platform, image_ids)
    for service in services:
        print(f"{manifest['images'][service]['image']} {manifest['images'][service]['id']}")
    print(f"Manifest: {manifest_path()}")


def require_images():
    """The native stack runs only from images recorded by `build`."""
    manifest = read_manifest()
    hint = "run `scripts/demo.py build` first."
    if manifest is None:
        raise SystemExit(f"No build manifest at {manifest_path()}; {hint}")
    missing = []
    for service in core.IMAGES:
        recorded = manifest["images"].get(service)
        if recorded is None:
            missing.append(f"{service} (never built)")
        elif not image_exists(recorded["image"]):
            missing.append(recorded["image"])
    if missing:
        raise SystemExit(f"Missing local image(s): {', '.join(missing)}; {hint}")
    current = build_tag()
    if manifest["tag"] != current:
        print(
            f"Warning: build inputs changed since the last build (tag {current}, manifest "
            f"{manifest['tag']}); run `scripts/demo.py build` to refresh the images."
        )
    return manifest
