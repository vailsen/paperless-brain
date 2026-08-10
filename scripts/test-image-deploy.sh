#!/usr/bin/env bash
# Build the working tree as a local image and run it on the deploy target,
# without GitHub, without GHCR, without a release tag.
#
# The remote stack keeps its own docker-compose.yml untouched. The image swap
# lives in a separate overlay file that is only active because both scripts pass
# it explicitly with -f. A plain `docker compose up -d` on the server therefore
# falls back to the published image — that is the intended escape hatch, and the
# reason nothing here edits the real compose file.
#
# Undo with scripts/test-image-undeploy.sh.
set -euo pipefail

IMAGE="paperless-brain:dev"
OVERLAY="docker-compose.dev-image.yml"
BUILD_ARGS=()
ASSUME_YES=0

usage() {
    cat <<'EOF'
Usage: scripts/test-image-deploy.sh [--lean] [--yes]

  --lean   build without crawl4ai/Chromium (smaller image, faster transfer)
  --yes    do not ask before restarting the remote container
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --lean) BUILD_ARGS+=(--build-arg LEAN=1); shift ;;
        --yes|-y) ASSUME_YES=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

cd "$(dirname "$0")/.."

[[ -f .deploy.env ]] || { echo "Missing .deploy.env (DEPLOY_TARGET, DEPLOY_REMOTE_DIR)" >&2; exit 1; }
# shellcheck disable=SC1091
source .deploy.env
: "${DEPLOY_TARGET:?DEPLOY_TARGET not set in .deploy.env}"
: "${DEPLOY_REMOTE_DIR:?DEPLOY_REMOTE_DIR not set in .deploy.env}"

echo "==> Target: $DEPLOY_TARGET:$DEPLOY_REMOTE_DIR"

if [[ $ASSUME_YES -eq 0 ]]; then
    read -r -p "This stops the running PaperlessBrain there and starts the test build. Continue? [y/N] " reply
    [[ "$reply" =~ ^[Yy]$ ]] || { echo "Aborted."; exit 1; }
fi

echo "==> Building $IMAGE"
# The +"..." form keeps `set -u` happy when no --lean was passed (empty array).
docker build ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} -t "$IMAGE" .

# The build just moved the :dev tag off the previous image, so that one is now
# dangling here as well. Same rule as on the target: dangling only, never `-a`.
# This is a machine-wide prune — it clears untagged leftovers from other builds
# too, which is what dangling means and what the reclaimed size will reflect.
echo "==> Pruning superseded local images"
docker image prune -f | sed 's/^/    /'

# zstd is roughly three times faster than gzip on a multi-GB image layer set and
# is present on most modern distros; gzip -1 is the portable fallback. The
# receiving side has to match, so the decompressor is chosen in the same branch.
if command -v zstd >/dev/null 2>&1 && ssh "$DEPLOY_TARGET" 'command -v zstd >/dev/null 2>&1'; then
    COMPRESS=(zstd -3 -T0); DECOMPRESS="zstd -d"
    echo "==> Shipping image (zstd)"
else
    COMPRESS=(gzip -1); DECOMPRESS="gunzip"
    echo "==> Shipping image (gzip)"
fi

docker save "$IMAGE" | "${COMPRESS[@]}" | ssh "$DEPLOY_TARGET" "$DECOMPRESS | docker load"

echo "==> Writing overlay and restarting the remote stack"
ssh "$DEPLOY_TARGET" "cat > '$DEPLOY_REMOTE_DIR/$OVERLAY'" <<EOF
# Written by scripts/test-image-deploy.sh — remove with test-image-undeploy.sh.
# Only takes effect when passed explicitly via -f.
services:
  paperless-brain:
    image: $IMAGE
    pull_policy: never
EOF

ssh "$DEPLOY_TARGET" "cd '$DEPLOY_REMOTE_DIR' && docker compose -f docker-compose.yml -f '$OVERLAY' up -d"

# Every deploy moves the :dev tag, which leaves the previous build behind as an
# untagged image — multiple GB each on a box that has no reason to keep them.
# Dangling only, never `-a`: that would also drop the published GHCR image,
# which is exactly what the revert path needs to still be there.
echo "==> Pruning superseded images on the target"
ssh "$DEPLOY_TARGET" "docker image prune -f" | sed 's/^/    /'

echo
echo "==> Test build is live on $DEPLOY_TARGET"
echo "    Logs:   ssh $DEPLOY_TARGET \"cd $DEPLOY_REMOTE_DIR && docker compose logs -f\""
echo "    Revert: scripts/test-image-undeploy.sh"
echo
echo "    Note: a plain 'docker compose up -d' on the server drops back to the"
echo "    published image, because the overlay is only read via -f."
