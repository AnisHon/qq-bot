#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

if [[ -f .env ]]; then
  set -a
  # shellcheck disable=SC1091
  . ./.env
  set +a
fi

export APP_HOST="${APP_HOST:-0.0.0.0}"
export APP_PORT="${APP_PORT:-8080}"

UV_BIN="${UV_BIN:-/home/anishan/.local/bin/uv}"
exec "$UV_BIN" run uvicorn app.main:app --host "$APP_HOST" --port "$APP_PORT"

