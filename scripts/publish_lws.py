"""Run the first LWS deployment from the Windows PC with the trusted VPS host key."""
import base64
from hashlib import sha256
import io
from pathlib import Path
import re
import socket
import subprocess
import sys
import tarfile
import time

from dotenv import dotenv_values
import httpx

ROOT = Path(__file__).resolve().parents[1]
DOMAIN = "transcriptoryt.com"
IP = "185.98.139.30"
ASSET = "/assets/local-whisper-worker.js"


def installer_input() -> bytes:
    key = dotenv_values(ROOT / ".env", interpolate=False).get("GROQ_API_KEY") or ""
    if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
        raise ValueError("Revisa GROQ_API_KEY en el .env local.")
    body = (ROOT / "deploy/install-lws-native.sh").read_text(encoding="utf-8").encode()
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in (("install.sh", body), ("provider-key", key.encode())):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o600
            archive.addfile(info, io.BytesIO(data))
    return base64.b64encode(buffer.getvalue()) + b"\n"


def check_public() -> bool:
    records = {row[4][0] for row in socket.getaddrinfo(DOMAIN, 443, type=socket.SOCK_STREAM)}
    print("DNS en Windows:", ", ".join(sorted(records)), flush=True)
    if IP not in records:
        return False
    with httpx.Client(base_url=f"https://{DOMAIN}", trust_env=False, timeout=8) as client:
        if client.get("/api/health").raise_for_status().json().get("ok") is not True:
            return False
        asset = client.get(ASSET).raise_for_status().content
        expected = (ROOT / "static" / ASSET.lstrip("/")).read_bytes()
        return sha256(asset).digest() == sha256(expected).digest()


def deploy(report) -> int:
    # The credential goes to this VPS over SSH stdin. It never enters an argv or log.
    remote = """set -eu
umask 077
d=$(mktemp -d /root/.transcriptoryt-install.XXXXXXXX)
trap 'rm -f -- "$d/provider-key" "$d/install.sh"; rmdir -- "$d"' EXIT
base64 -d | tar -xzf - -C "$d"
bash -n "$d/install.sh" || exit 1
bash "$d/install.sh" "$d/provider-key" 2>&1
"""
    payload = installer_input()
    print("Cuando SSH pida la clave del VPS, ingresala y pulsa Enter.", flush=True)
    args = ["ssh.exe", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=12",
            "-o", "NumberOfPasswordPrompts=2", f"root@{IP}", remote]
    with subprocess.Popen(args, stdin=subprocess.PIPE, stdout=subprocess.PIPE) as process:
        assert process.stdin and process.stdout
        try:
            process.stdin.write(payload)
            process.stdin.close()
        except BrokenPipeError:
            pass
        for line in process.stdout:
            message = line.decode("utf-8", errors="replace")
            print(message, end="", flush=True)
            report.write(message)
            report.flush()
        return process.wait()


def main() -> int:
    sys.stdout.reconfigure(errors="replace")
    log = ROOT / "dist/publicacion-vps.txt"
    with log.open("w", encoding="utf-8", buffering=1) as report:
        status = deploy(report)
        if status:
            message = f"Instalacion interrumpida. Codigo SSH/instalador: {status}."
            print(message)
            print(message, file=report)
            return status
        for attempt in range(10):
            try:
                if check_public():
                    message = f"HTTPS verificado desde Windows: https://{DOMAIN}/"
                    print(message)
                    print(message, file=report)
                    return 0
            except (OSError, httpx.HTTPError, ValueError) as exc:
                print(f"Comprobacion HTTPS pendiente ({type(exc).__name__}).", flush=True)
            if attempt < 9:
                time.sleep(8)
        message = "La app responde en el VPS. Pendiente de verificar DNS y HTTPS publicos."
        print(message)
        print(message, file=report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
