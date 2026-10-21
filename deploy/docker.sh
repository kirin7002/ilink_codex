#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."
docker compose version >/dev/null
docker info >/dev/null

case "${1:-help}" in
  build)
    build_args=()
    if [[ -n "${ILINK_BUILD_CA:-}" ]]; then
      build_args+=(--secret "id=proxy_ca,src=${ILINK_BUILD_CA}")
    fi
    docker build "${build_args[@]}" --target runtime -t ilink-codex:0.1.0 .
    docker build "${build_args[@]}" --target test -t ilink-codex:test .
    docker run --rm --network none --cap-drop ALL --security-opt no-new-privileges ilink-codex:test
    ;;
  sandbox-check)
    docker compose run --rm --no-deps --entrypoint python worker /app/deploy/sandbox-check.py
    ;;
  import-auth)
    auth_path="${2:-${HOME}/.codex/auth.json}"
    [[ -f "$auth_path" ]] || { echo 'Codex auth file missing. Use codex-login instead.' >&2; exit 1; }
    docker compose run --rm --no-deps -T --entrypoint python worker /app/deploy/import-auth.py < "$auth_path"
    ;;
  codex-login)
    docker compose run --rm --no-deps --entrypoint codex worker login --device-auth
    ;;
  login)
    docker compose run --rm --no-deps bridge login
    ;;
  up)
    docker compose run --rm --no-deps --entrypoint python worker /app/deploy/sandbox-check.py
    docker compose run --rm --no-deps --entrypoint codex worker login status
    docker compose run --rm --no-deps --entrypoint python bridge -c '
import json
from pathlib import Path
from ilink_codex.config import Allowlist
p=Path("/data/state")
c=json.loads((p/"creds.json").read_text())
assert c.get("bot_token") and c.get("account_id") and c.get("user_id"), "iLink login is incomplete"
assert Allowlist(p/"allowlist.json").allows(c["account_id"],c["user_id"]), "Owner is not allowed"
print("iLink credentials and owner allowlist present; live messaging still needs verification.")'
    docker compose up -d --no-build --wait --wait-timeout 180
    docker compose ps
    ;;
  status)
    docker compose ps
    ;;
  logs)
    docker compose logs --tail=100 -f
    ;;
  stop)
    docker compose stop
    ;;
  *)
    echo 'Usage: bash deploy/docker.sh {build|sandbox-check|import-auth [file]|codex-login|login|up|status|logs|stop}'
    ;;
esac
