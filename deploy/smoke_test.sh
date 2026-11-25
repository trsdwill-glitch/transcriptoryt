#!/bin/sh
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
# Use the image's pinned Python dependencies; the Ubuntu host needs only python3.
exec docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec -T app \
  python scripts/smoke_server.py --base-url "${1:-http://127.0.0.1:8000}"
