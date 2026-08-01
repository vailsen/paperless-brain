#!/usr/bin/env bash
# Undo scripts/test-image-deploy.sh: put the published image back on the server
# and remove the test build from both machines.
#
# Data is untouched — ./data, ./vaults and ./nicegui-storage are bind mounts and
# survive the container swap. Only the image and the overlay file go away.
set -euo pipefail

IMAGE="paperless-brain:dev"
OVERLAY="docker-compose.dev-image.yml"
KEEP_LOCAL=0

usage() {
    cat <<'EOF'
Usage: scripts/test-image-undeploy.sh [--keep-local]

  --keep-local   remove the test image on the server only, keep it here
EOF
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --keep-local) KEEP_LOCAL=1; shift ;;
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

echo "==> Restoring the published image on $DEPLOY_TARGET"

# Order matters: bring the stack up from the plain compose file first, so the
# container no longer references the test image, then delete it. Removing the
# image first would leave the running container holding a dangling reference.
ssh "$DEPLOY_TARGET" bash -s -- "$DEPLOY_REMOTE_DIR" "$OVERLAY" "$IMAGE" <<'REMOTE'
set -euo pipefail
remote_dir="$1"; overlay="$2"; image="$3"
cd "$remote_dir"
rm -f "$overlay"
docker compose up -d
docker image rm "$image" >/dev/null 2>&1 || echo "   (test image was not present on the server)"
echo "   Running image: $(docker compose config --images | tr '\n' ' ')"
REMOTE

if [[ $KEEP_LOCAL -eq 0 ]]; then
    echo "==> Removing the local test image"
    docker image rm "$IMAGE" >/dev/null 2>&1 || echo "   (not present locally)"
fi

echo
echo "==> Clean. The server runs the published image again."
echo "    To take a newer release:"
echo "    ssh $DEPLOY_TARGET \"cd $DEPLOY_REMOTE_DIR && docker compose pull && docker compose up -d\""
