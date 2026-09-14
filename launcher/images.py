"""The four images: their build inputs, the content tag, staging, the manifest, and publishing.

Building and publishing are one story with two halves and one manifest between them: `build`
records what it built and for which platform, and `publish` pushes those images to the
account's ECR repositories, refuses a platform the cluster's nodes cannot run, and records
where they landed for `demo.py eks deploy` to point Helm at.
"""

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


def save_manifest(manifest):
    """Write the manifest, creating `.runtime/images` on the way."""
    path = manifest_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2) + "\n")
    return manifest


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
    return save_manifest(manifest)


def build_context(service):
    return core.RUNTIME / "build" if service in STAGED_IMAGES else core.ROOT


def require_known(services):
    """Refuse a service name that is not one of the four, naming the four.

    `build` and `publish` take the same `--service` argument, so they refuse it identically.
    """
    unknown = sorted(set(services) - set(core.IMAGES))
    if unknown:
        raise SystemExit(
            f"Unknown service(s) {', '.join(unknown)}; choose from {', '.join(core.IMAGES)}."
        )


def build(services, platform=None):
    require_known(services)
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


def publish(services, force=False):
    """Push locally built images to the account's ECR repositories and record them.

    The counterpart of `build` for the EKS target: it reads the manifest rather than the build
    inputs, refuses a platform the cluster's nodes cannot run, and writes
    `manifest.published[service]` (see PUBLISHED_FIELDS) for the deploy to point Helm at.

    This replaces `build-frontend.sh`, which built and pushed one image inside one script. The
    split is the reason for the platform gate: `build` runs wherever the developer is and
    records what it built for, and `publish` is the first step that knows what the node group
    runs, so it is the last chance to refuse an image that would only fail as a CrashLoop.

    Every requested service ends up recorded, whether or not this run pushed it: a tag already
    in the registry is skipped (that is what `--force` overrides), but the deploy's gate reads
    `manifest.published`, not ECR, so skipping the push must not mean skipping the record.
    """
    # Deferred: launcher.eks.lifecycle imports this module for require_published(), and
    # importing launcher.eks at module scope would make that a cycle through launcher.eks's
    # __init__.
    from .eks import aws

    require_known(services)
    core.need("aws", "docker", "tofu")
    manifest = require_built(services)
    tag, platform = manifest["tag"], manifest["platform"]

    aws.aws_login()
    node_platform = aws.tf_out("node_platform")
    if platform != node_platform:
        core.die(
            f"the images in {manifest_path()} were built for {platform} but the cluster's nodes "
            f"run {node_platform}; rebuild them with `demo.py build --platform {node_platform}`"
        )
    repositories = aws.tf_out("ecr_repository_urls")
    missing = sorted(service for service in services if service not in repositories)
    if missing:
        core.die(
            f"no ECR repository for {', '.join(missing)} in the OpenTofu outputs: "
            "run `demo.py eks apply` to create the four repositories"
        )

    pushing = [
        service
        for service in services
        if force or not aws.ecr_has_image(repositories[service], tag)
    ]
    for service in services:
        if service not in pushing:
            core.log(
                f"{repositories[service]}:{tag} is already in ECR; not pushing it again "
                "(use --force to push anyway)"
            )
    if pushing:
        aws.ecr_login()

    published = {}
    for service in services:
        repository = repositories[service]
        remote = f"{repository}:{tag}"
        if service in pushing:
            core.log(f"pushing {remote} ({platform})")
            core.run("docker", "tag", image_name(service, tag), remote)
            core.run("docker", "push", remote)
        published[service] = {
            "repository": repository,
            "tag": tag,
            "digest": aws.ecr_image_digest(repository, tag),
        }
    record_published(published)
    for service in services:
        print(f"{published[service]['repository']}:{tag} {published[service]['digest']}")
    print(f"Manifest: {manifest_path()}")
    print("Next: demo.py eks deploy (rolls the release onto these images)")


def record_published(published):
    """Merge per-service publish records into `manifest.published` and rewrite the manifest.

    A merge rather than a replace, so `publish --service frontend` does not unpublish the other
    three: the deploy's gate reads every service out of this one section.
    """
    manifest = read_manifest() or {}
    recorded = manifest.get("published") or {}
    recorded.update(published)
    manifest["published"] = recorded
    return save_manifest(manifest)


def require_built(services):
    """The manifest, once `build` has recorded those services and their images are still local.

    `publish` retags what is on this Docker host, so a recorded image that has since been
    pruned has to be caught here rather than by a `docker tag` failure.
    """
    manifest = read_manifest()
    hint = "run `demo.py build` first."
    if manifest is None:
        raise SystemExit(f"No build manifest at {manifest_path()}; {hint}")
    missing = missing_images(manifest, services)
    if missing:
        raise SystemExit(f"Missing local image(s): {', '.join(missing)}; {hint}")
    return manifest


def require_published():
    """The `published` section, once every service is in it at the current build tag.

    The gate `eks deploy` runs first, and it refuses in three shapes because the fix differs:
    nothing published at all, a service absent from an otherwise published set, and a set
    published at a tag the build inputs have moved past. The last one is the dangerous case --
    the Helm release would roll out happily, pointing at last week's images.
    """
    manifest = read_manifest()
    hint = "run `demo.py build` then `demo.py publish` first."
    if manifest is None:
        core.die(f"no build manifest at {manifest_path()}: {hint}")
    published = manifest.get("published") or {}
    if not published:
        core.die(f"nothing published from {manifest_path()} yet: {hint}")
    absent = [
        service
        for service in core.IMAGES
        if not all((published.get(service) or {}).get(field) for field in PUBLISHED_FIELDS)
    ]
    if absent:
        core.die(f"no published image for {', '.join(absent)}: {hint}")
    current = build_tag()
    stale = [service for service in core.IMAGES if published[service]["tag"] != current]
    if stale:
        core.die(
            f"the published image(s) for {', '.join(stale)} are at build tag "
            f"{published[stale[0]]['tag']}, but the current build inputs are {current}: {hint}"
        )
    return published


def missing_images(manifest, services):
    """Those services' recorded images that are not on this Docker host, as message fragments.

    A service the manifest has never seen and one whose image has been pruned are both missing,
    and both name themselves in the refusal that follows.
    """
    missing = []
    for service in services:
        recorded = manifest.get("images", {}).get(service)
        if recorded is None:
            missing.append(f"{service} (never built)")
        elif not image_exists(recorded["image"]):
            missing.append(recorded["image"])
    return missing


def require_images():
    """The native stack runs only from images recorded by `build`."""
    manifest = read_manifest()
    hint = "run `scripts/demo.py build` first."
    if manifest is None:
        raise SystemExit(f"No build manifest at {manifest_path()}; {hint}")
    missing = missing_images(manifest, core.IMAGES)
    if missing:
        raise SystemExit(f"Missing local image(s): {', '.join(missing)}; {hint}")
    current = build_tag()
    if manifest["tag"] != current:
        print(
            f"Warning: build inputs changed since the last build (tag {current}, manifest "
            f"{manifest['tag']}); run `scripts/demo.py build` to refresh the images."
        )
    return manifest
