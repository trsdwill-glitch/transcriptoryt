#!/usr/bin/env bash
set -Eeuo pipefail
umask 022
trap 'echo "Instalacion interrumpida en la linea $LINENO." >&2' ERR
key_file=$1
domain=transcriptoryt.com
release_hash=7b9734b90e5ce152b01691407a446de51c99d3d0883e04e54ba72d74fbde67d4
base=/opt/transcriptoryt
release=$base/releases/$release_hash
private=/etc/transcriptoryt
data=/var/lib/transcriptoryt
proxy_data=/var/lib/transcriptoryt-caddy
tools=$base/bin

cd /root
echo "$release_hash  transcriptor-lws.zip" | sha256sum --check
[[ $(uname -m) == x86_64 && $(id -u) == 0 ]]
[[ -d /run/systemd/system ]]
exec 9>/run/transcriptoryt-install.lock
flock -n 9 || { echo "Ya hay otra instalacion en curso."; exit 1; }
export DEBIAN_FRONTEND=noninteractive
install -d -m 0755 "$tools" "$base/releases"
install -d -m 0700 "$private"
work=$(mktemp -d "$base/.setup.XXXXXXXX")
trap 'rm -rf -- "$work"' EXIT

if [[ ! -x /usr/bin/ffmpeg || ! -x /usr/bin/unzip || ! -x /usr/bin/curl ]] || ! dpkg -s python3-venv >/dev/null 2>&1; then
    echo "Instalando paquetes de Ubuntu..."
    apt-get update -qq
    apt-get install -y --no-install-recommends python3-venv ffmpeg unzip curl ca-certificates xz-utils
fi
if [[ ! -d $release ]]; then
    mkdir -m 0755 "$work/site"
    unzip -qo /root/transcriptor-lws.zip -d "$work/site"
    (cd "$work/site" && sha256sum --check --quiet SHA256SUMS && python3 scripts/configure_site.py --check)
    mv -T "$work/site" "$release"
fi
if [[ ! -x $base/venv/bin/python ]]; then python3 -m venv "$base/venv"; fi
if [[ ! -f $base/venv/.installed-$release_hash ]]; then
    echo "Instalando las dependencias del transcriptor..."
    "$base/venv/bin/python" -m pip install --no-cache-dir --disable-pip-version-check -r "$release/deploy/requirements-runtime.txt" "yt-dlp[default]==$(cat "$release/deploy/ytdlp-version.txt")"
    "$base/venv/bin/python" -m pip check
    touch "$base/venv/.installed-$release_hash"
fi
fetch() { curl --proto '=https' --tlsv1.2 --fail --silent --show-error --location --retry 3 "$1" -o "$2"; }
if [[ ! -x $tools/node ]]; then
    echo "Instalando Node para los enlaces de YouTube..."
    version=24.22.3
    archive=node-v$version-linux-x64.tar.xz
    upstream=https://nodejs.org/dist/v$version
    fetch "$upstream/SHASUMS256.txt" "$work/node-checksums"
    awk -v name="$archive" '$2 == name {print}' "$work/node-checksums" > "$work/node.sha256"
    test -s "$work/node.sha256"
    fetch "$upstream/$archive" "$work/$archive"
    (cd "$work" && sha256sum --check node.sha256 && tar -xJf "$archive" "node-v$version-linux-x64/bin/node")
    install -m 0755 "$work/node-v$version-linux-x64/bin/node" "$tools/node"
fi
"$tools/node" --version
if [[ ! -x $tools/caddy ]]; then
    echo "Instalando Caddy para HTTPS..."
    fetch https://github.com/caddyserver/caddy/releases/download/v2.11.9/caddy_2.11.9_linux_amd64.tar.gz "$work/caddy.tar.gz"
    (cd "$work" && echo '0e85544340290a16ce4bfb76fe7d65e9de1bf3a6e67f5ccfff30b30d27ea4c73e078077b7bf3e58ae60b6b12e44327705287059c775bca5782e8ee85ea7edd75  caddy.tar.gz' | sha512sum --check && tar -xzf caddy.tar.gz caddy)
    install -m 0755 "$work/caddy" "$tools/caddy"
fi
for service in transcriptoryt transcriptoryt-caddy; do
    if ! id "$service" >/dev/null 2>&1; then
        useradd --system --user-group --home-dir "/var/lib/$service" --no-create-home --shell /usr/sbin/nologin "$service"
    fi
    install -d -m 0700 -o "$service" -g "$service" "/var/lib/$service"
done
install -d -m 0755 "$base/config/yt-dlp"
sed "s|/usr/local/bin/node|$tools/node|" "$release/deploy/yt-dlp.conf" > "$base/config/yt-dlp/config"

# Keep quota signatures stable on retries. Only root/systemd reads this file.
python3 - "$release" "$key_file" "$private/app.env" <<'PY'
import os, re, secrets, sys, tempfile
from pathlib import Path
root, source, dest = map(Path, sys.argv[1:])
key = source.read_text().strip()
if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
    raise SystemExit("La clave de Groq tiene un formato inesperado.")
template = (root / "deploy/.env.example").read_text()
old = dict(line.split("=", 1) for line in dest.read_text().splitlines() if "=" in line and not line.startswith("#")) if dest.exists() else {}
values = {"DATA_DIR": "/var/lib/transcriptoryt", "GROQ_API_KEY": key,
          "DOMAIN": "transcriptoryt.com", "SECRET_KEY": old.get("SECRET_KEY") or secrets.token_hex(32),
          "HASH_SALT": old.get("HASH_SALT") or secrets.token_hex(32)}
email = re.search(r'mailto:([^"?#]+)', (root / "static/contacto/index.html").read_text())
if not email or not re.fullmatch(r"[\w.+-]+@[\w.-]+", email[1]):
    raise SystemExit("Falta el correo publico de contacto.")
values["CONTACT_EMAIL"] = email[1]
lines = []
for line in template.splitlines():
    name = line.split("=", 1)[0]
    lines.append(f"{name}={values[name]}" if name in values else line)
with tempfile.NamedTemporaryFile(mode="w", dir=dest.parent, delete=False) as file:
    file.write("\n".join(lines) + "\n")
os.replace(file.name, dest)
PY
contact=$(sed -n 's/^CONTACT_EMAIL=//p' "$private/app.env")
sed -e "s/{\$DOMAIN}/$domain/g" -e "s/{\$CONTACT_EMAIL}/$contact/g" -e 's/app:8000/127.0.0.1:8000/' "$release/deploy/Caddyfile" > "$base/config/Caddyfile"
"$tools/caddy" validate --config "$base/config/Caddyfile" --adapter caddyfile

cat > "$work/transcriptoryt.service" <<UNIT
[Unit]
Description=TranscriptorYT API
After=network.target
[Service]
User=transcriptoryt
Group=transcriptoryt
WorkingDirectory=$release
EnvironmentFile=$private/app.env
Environment=HOME=$data XDG_CONFIG_HOME=$base/config PYTHONDONTWRITEBYTECODE=1
Environment=PATH=$base/venv/bin:$tools:/usr/local/bin:/usr/bin:/bin
ExecStart=$base/venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --no-access-log --no-proxy-headers --timeout-graceful-shutdown 30
UMask=0077
NoNewPrivileges=yes
TasksMax=192
MemoryMax=2G
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
cat > "$work/transcriptoryt-caddy.service" <<UNIT
[Unit]
Description=TranscriptorYT HTTPS
After=network.target transcriptoryt.service
[Service]
User=transcriptoryt-caddy
Group=transcriptoryt-caddy
Environment=HOME=$proxy_data XDG_DATA_HOME=$proxy_data/data XDG_CONFIG_HOME=$proxy_data/config
ExecStart=$tools/caddy run --config $base/config/Caddyfile --adapter caddyfile
AmbientCapabilities=CAP_NET_BIND_SERVICE
CapabilityBoundingSet=CAP_NET_BIND_SERVICE
NoNewPrivileges=yes
UMask=0077
Restart=on-failure
RestartSec=3
[Install]
WantedBy=multi-user.target
UNIT
for service in transcriptoryt transcriptoryt-caddy; do
    install -m 0644 "$work/$service.service" "/etc/systemd/system/$service.service"
done
systemctl daemon-reload
systemctl enable transcriptoryt.service transcriptoryt-caddy.service
# restart, not start: on an update the services are already running the previous release.
systemctl restart transcriptoryt.service
if ! curl -fsS --retry 12 --retry-connrefused --retry-delay 1 http://127.0.0.1:8000/api/health; then
    journalctl -u transcriptoryt.service -n 15 --no-pager; exit 1
fi
echo
"$base/venv/bin/python" "$release/scripts/smoke_server.py" --base-url http://127.0.0.1:8000
systemctl restart transcriptoryt-caddy.service
if ! curl --noproxy '*' -fsS --retry 8 --retry-connrefused --retry-delay 1 -o /dev/null --resolve "$domain:80:127.0.0.1" "http://$domain/"; then
    journalctl -u transcriptoryt-caddy.service -n 15 --no-pager; exit 1
fi
echo "Servicios del VPS activos. Comprobando el dominio desde la PC."
ip -6 -o address show scope global
