#!/usr/bin/env bash
# Rebuild the note vault's editor bundle (app_ui/vault_editor/dist/).
# Dev only — the output is committed, so neither the server nor the Docker
# build needs Node. Versions are pinned in package.json / package-lock.json.
set -euo pipefail
cd "$(dirname "$0")/../app_ui/vault_editor"
npm ci --no-audit --no-fund
npm run build
