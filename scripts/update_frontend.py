"""Publish the static frontend to the existing native LWS installation over one SSH session."""
from __future__ import annotations

import base64
import ctypes
from hashlib import sha256
from html import escape
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from urllib.error import HTTPError
from urllib.request import urlopen
import zipfile


FILES = (
    "index.html", "en/index.html", "youtube-a-texto/index.html", "video-a-texto/index.html",
    "acerca-de/index.html", "contacto/index.html", "privacidad/index.html", "terminos/index.html",
    "en/about/index.html", "en/contact/index.html", "en/privacy/index.html", "en/terms/index.html",
    "sitemap.xml", "robots.txt", "404.html", "assets/social-card.png",
    "assets/style.css", "assets/transcriber.css", "assets/tool-copy.js", "assets/app.js",
    "assets/local-whisper-worker.js", "assets/brand-mark.svg", "assets/logo-512.png",
    ".well-known/atproto-did",
)
PUBLIC = "https://transcriptoryt.com"
SSH_HOST = "root@185.98.139.30"


class VerificationTags(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tokens = set()

    def handle_starttag(self, tag, attrs):
        fields = dict(attrs)
        if tag == "meta" and fields.get("name", "").lower() == "google-site-verification":
            if fields.get("content"):
                self.tokens.add(fields["content"])


def preserve_verification(new: bytes, previous: bytes) -> bytes:
    original, incoming = VerificationTags(), VerificationTags()
    original.feed(previous.decode("utf-8"))
    incoming.feed(new.decode("utf-8"))
    tags = "".join(f'<meta name="google-site-verification" content="{escape(token)}">\n'
                   for token in sorted(original.tokens - incoming.tokens))
    return new.replace(b"</head>", tags.encode() + b"</head>", 1)


def read_url(base: str, path: str) -> bytes:
    try:
        response = urlopen(base + path, timeout=12)
    except HTTPError as error:
        if error.code != 404 or path.split("?")[0] != "/404.html":
            raise
        response = error
    with response:
        return response.read()


def verify(base: str, files: dict[str, bytes]) -> None:
    if json.loads(read_url(base, "/api/health")).get("ok") is not True:
        raise RuntimeError("La API no responde correctamente.")
    for name, expected in files.items():
        path = "/" + name.removesuffix("index.html")
        actual = read_url(base, path + "?v=" + sha256(expected).hexdigest()[:12])
        if name == "index.html":
            expected = preserve_verification(expected, actual)
        if actual != expected:
            raise RuntimeError(f"El contenido de {path} no coincide con esta actualización.")


def exchange(left: Path, right: Path) -> None:
    # Linux renameat2 exchanges both non-empty directories in one filesystem operation.
    libc = ctypes.CDLL(None, use_errno=True)
    if libc.renameat2(-100, os.fsencode(left), -100, os.fsencode(right), 2) != 0:
        code = ctypes.get_errno()
        raise OSError(code, os.strerror(code))


def install(payload: bytes, digest: str) -> None:
    import fcntl

    if sha256(payload).hexdigest() != digest:
        raise ValueError("El paquete recibido está incompleto.")
    with zipfile.ZipFile(io.BytesIO(payload)) as bundle:
        if sorted(bundle.namelist()) != sorted(FILES):
            raise ValueError("Contenido inesperado en la actualización.")
        files = {name: bundle.read(name) for name in FILES}

    with open("/run/transcriptoryt-install.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        directory = subprocess.check_output(
            ["systemctl", "show", "transcriptoryt.service", "-p", "WorkingDirectory", "--value"],
            text=True,
        ).strip()
        release = Path(directory).resolve(strict=True)
        if release.parent != Path("/opt/transcriptoryt/releases"):
            raise ValueError("No se encontró la instalación activa de transcriptoryt.")
        if not (release / "app/site_pages.py").is_file():
            raise ValueError("La migración SEO requiere primero la versión completa con sus rutas y Caddyfile.")
        live = release / "static"
        home = live / "index.html"
        files["index.html"] = preserve_verification(files["index.html"], home.read_bytes())
        if all((live / name).is_file() and (live / name).read_bytes() == data for name, data in files.items()):
            verify("http://127.0.0.1:8000", files)
            print("Esta versión ya está activa.")
            return
        backup = Path(tempfile.mkdtemp(prefix=".frontend-", dir=release))
        candidate = backup / "static"
        keep = False
        try:
            # Hard links keep the backup small. All changed entries are replaced, never edited in place.
            shutil.copytree(live, candidate, copy_function=os.link, symlinks=True)
            for name, data in files.items():
                target = candidate / name
                target.parent.mkdir(parents=True, exist_ok=True)
                target.unlink(missing_ok=True)
                target.write_bytes(data)
                target.chmod(0o644)
            exchange(live, candidate)
            keep = True
            try:
                verify("http://127.0.0.1:8000", files)
            except BaseException:
                exchange(live, candidate)
                keep = False
                print("Se restauró la portada anterior.", flush=True)
                raise
            print(f"Actualización activa. Copia anterior: {candidate}", flush=True)
        finally:
            if not keep:
                shutil.rmtree(backup)


def publish(root: Path) -> None:
    files = {name: (root / "static" / name).read_bytes() for name in FILES}
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in files.items():
            bundle.writestr(name, data)
    payload = buffer.getvalue()
    script = base64.b64encode(Path(__file__).read_bytes()).decode("ascii")
    command = f"""python3 -c "import base64;exec(base64.b64decode('{script}'))" --remote {sha256(payload).hexdigest()}"""
    print("Para actualizar transcriptoryt.com, ingresa la contraseña del VPS cuando SSH la pida.", flush=True)
    subprocess.run(["ssh", "-T", "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=12",
                    "-o", "NumberOfPasswordPrompts=2", SSH_HOST, command], input=payload, check=True)
    print("Verificando la portada, el logo y el motor en el dominio público…", flush=True)
    verify(PUBLIC, files)
    print(f"PUBLICADO Y VERIFICADO: {PUBLIC}/", flush=True)


if __name__ == "__main__":
    try:
        if len(sys.argv) == 3 and sys.argv[1] == "--remote":
            install(sys.stdin.buffer.read(), sys.argv[2])
        else:
            publish(Path(__file__).resolve().parents[1])
    except Exception as error:
        sys.exit(f"No se completó la publicación o su verificación: {error}")
