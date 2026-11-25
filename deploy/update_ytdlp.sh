#!/bin/sh
# Rebuild immutable images; never pip-install into a running container.
set -eu
project_dir=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$project_dir"
mode=${1:---check}
case "$mode" in
  --check|--latest) ;;
  *) python3 - "$mode" <<'PY'
import re, sys
if not re.fullmatch(r"[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]+)?", sys.argv[1]):
    raise SystemExit("Usage: sh deploy/update_ytdlp.sh --check | --latest | YYYY.MM.DD")
PY
  ;;
esac
previous=$(tr -d '\r\n' < deploy/ytdlp-version.txt)
if [ "$mode" = --check ] || [ "$mode" = --latest ]; then
  candidate=$(python3 - <<'PY'
import json, re, urllib.request
with urllib.request.urlopen("https://pypi.org/pypi/yt-dlp/json", timeout=30) as response:
    version = json.load(response)["info"]["version"]
if not re.fullmatch(r"[0-9]{4}\.[0-9]{1,2}\.[0-9]{1,2}(?:\.[0-9]+)?", version):
    raise SystemExit("Unexpected stable version from PyPI.")
print(version)
PY
)
else
  candidate=$mode
fi
if [ "$mode" = --check ]; then
  printf 'Pinned yt-dlp: %s; latest stable: %s. No files or containers changed.\n' "$previous" "$candidate"
  exit 0
fi
if [ "$previous" = "$candidate" ]; then
  printf 'yt-dlp is already pinned to %s.\n' "$candidate"
  exit 0
fi
test -f deploy/.env || { printf 'Create deploy/.env first.\n' >&2; exit 1; }
old_image=$(docker image inspect transcriptor:local --format '{{.Id}}' 2>/dev/null || true)
restore_pin() { printf '%s\n' "$previous" > deploy/ytdlp-version.txt; }
trap restore_pin EXIT HUP INT TERM
printf '%s\n' "$candidate" > deploy/ytdlp-version.txt
docker build --target tests -f deploy/Dockerfile -t transcriptor:ytdlp-candidate-tests .
docker run --rm --read-only --tmpfs /tmp:rw,nosuid,size=536870912 transcriptor:ytdlp-candidate-tests
docker compose --env-file deploy/.env -f deploy/docker-compose.yml build app
if ! docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --no-deps --wait --wait-timeout 90 app; then
  restore_pin
  if [ -n "$old_image" ]; then
    docker image tag "$old_image" transcriptor:local
    docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --no-deps --wait --wait-timeout 90 app
  fi
  printf 'Candidate startup failed; previous pin/image restored when available.\n' >&2
  exit 1
fi
trap - EXIT HUP INT TERM
printf 'Installed yt-dlp %s. Keep deploy/ytdlp-version.txt with this release.\n' "$candidate"
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec -T app python -c 'from importlib.metadata import version; print("yt-dlp=" + version("yt-dlp")); print("yt-dlp-ejs=" + version("yt-dlp-ejs"))'
printf 'Repeat the 30-link YouTube test from this VPS IP before calling it verified.\n'
