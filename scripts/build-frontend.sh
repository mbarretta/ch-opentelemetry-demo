#!/usr/bin/env bash
# Build the demo frontend with the ClickStack browser SDK compiled in and push
# it to the ECR repository created by ./demo.sh apply.
#
#   ./demo.sh build-frontend            build and push unless the tag is already in ECR
#   ./demo.sh build-frontend --force    build and push even if it is
#
# deploy calls this for you when the image is missing, so a from-scratch
# apply -> deploy needs no separate step. Run it with --force after editing
# the frontend source or the patch.
#
# Why an image build at all: the upstream `frontend` image has no HyperDX SDK in
# its bundle, and there is no runtime hook to inject one, so the SDK has to be
# added to package.json and compiled by `next build`. PUBLIC_HYPERDX_ENABLED
# (set in k8s/demo-values.yaml) only *activates* code that must already be in
# the bundle.
#
# The image tag is deterministic -- the pinned demo commit plus a hash of the
# patch -- so the same sources always produce the same tag, and deploy can tell
# from ECR alone whether a build is needed.
#
# The build runs on the Mac with docker buildx for the node group's platform
# (linux/arm64 for Graviton), which is native on Apple Silicon: no emulation,
# no cross-platform "exec format error" on the nodes.
set -euo pipefail

cd "$(dirname "$0")/.."
. scripts/lib/common.sh
. scripts/lib/aws.sh
. scripts/lib/k8s.sh

usage() { echo "usage: ./demo.sh build-frontend [--force]" >&2; }

FORCE=false
case "${1:-}" in
  "") ;;
  --force) FORCE=true ;;
  *)
    usage
    exit 1
    ;;
esac

need aws tofu docker git

# The demo source checkout. Gitignored; overridable to reuse an existing one
# (for example DEMO_DIR=../202609-offsite-workshop/observability-workshop/opentelemetry-demo).
DEMO_DIR="${DEMO_DIR:-opentelemetry-demo}"
DEMO_REPO=https://github.com/open-telemetry/opentelemetry-demo.git

# The demo source is gitignored, so a fresh clone of this repo does not have it.
# Fetch it rather than failing: deploy depends on this build, so a missing
# checkout would otherwise make a clean clone undeployable.
ensure_demo_checkout() {
  if [[ -d "$DEMO_DIR/src/frontend" ]]; then
    # An existing checkout is respected as-is (it may carry your own edits), but
    # it is no use without the SDK wiring -- building without it yields an image
    # where PUBLIC_HYPERDX_ENABLED activates code that is not in the bundle, and
    # replay silently never starts.
    if grep -q '@hyperdx/browser' "$DEMO_DIR/src/frontend/package.json" 2>/dev/null; then
      return 0
    fi
    log "demo checkout present but missing the SDK wiring; applying $SDK_PATCH"
    git -C "$DEMO_DIR" apply "$REPO_ROOT/$SDK_PATCH"
    return 0
  fi

  log "fetching the OpenTelemetry demo source at $DEMO_REF"
  # Blobless partial clone: the build context is under 1 MB, and none of the
  # history is needed -- but a specific commit has to remain reachable, which
  # rules out --depth 1 on a branch.
  git clone --no-checkout --filter=blob:none "$DEMO_REPO" "$DEMO_DIR"
  git -C "$DEMO_DIR" checkout --detach "$DEMO_REF"

  log "applying $SDK_PATCH (the ClickStack browser SDK wiring)"
  git -C "$DEMO_DIR" apply --check "$REPO_ROOT/$SDK_PATCH" \
    || die "$SDK_PATCH does not apply to $DEMO_REF -- the pin and the patch have drifted apart"
  git -C "$DEMO_DIR" apply "$REPO_ROOT/$SDK_PATCH"
}

aws_login
ECR_REPO_URL="$(tf_out ecr_repository_url)"
ECR_REGISTRY="$(tf_out ecr_registry)"
NODE_PLATFORM="$(tf_out node_platform)"
# The repository lives in the region the cluster was applied in, which wins
# over whatever envvars.aws says.
AWS_REGION="$(tf_out region)"
export AWS_REGION

TAG="$(frontend_tag)"
IMAGE="$ECR_REPO_URL:$TAG"

if [[ "$FORCE" == false ]] && ecr_has_image "$TAG"; then
  log "$IMAGE is already in ECR; nothing to build (use --force to rebuild)"
  exit 0
fi

ensure_demo_checkout

log "logging docker in to $ECR_REGISTRY"
aws ecr get-login-password --region "$AWS_REGION" \
  | docker login --username AWS --password-stdin "$ECR_REGISTRY"

log "building $IMAGE for $NODE_PLATFORM (first run downloads the npm tree; buildx caches it after)"
# The Dockerfile COPYs ./src/frontend/* relative to the demo root, so the demo
# root is the build context (its .dockerignore drops node_modules and .next).
# `-f` is resolved against the current directory, not the context, hence the
# $DEMO_DIR prefix. Provenance and SBOM attestations are disabled because they
# turn the pushed tag into an image index with an extra unknown/unknown entry,
# which ECR renders confusingly and which makes `docker buildx imagetools
# inspect` show two platforms instead of the one that was built.
docker buildx build --platform "$NODE_PLATFORM" --provenance=false --sbom=false \
  -f "$DEMO_DIR/src/frontend/Dockerfile" -t "$IMAGE" --push "$DEMO_DIR"

echo
echo "Pushed $IMAGE"
echo "Next: ./demo.sh deploy (rolls the frontend onto this image)"
