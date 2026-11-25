"""Create an allowlisted release ZIP with browser runtime and checksums, without local data."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORIES = {"app", "static", "scripts", "tests", "deploy", "docs"}
FILES = {"README.md", "INFORME.md", "requirements.txt", "pyproject.toml", ".env.example",
         ".gitignore", ".dockerignore"}
EXCLUDED = {"__pycache__", ".pytest_cache", ".ruff_cache", "node_modules", ".venv", "data"}
SUFFIXES = {".py", ".mjs", ".js", ".css", ".html", ".txt", ".md", ".xml", ".sh",
            ".yml", ".yaml", ".toml", ".conf", ".example", ".wasm", ".service", ".timer"}
BRAND_ASSETS = {"static/assets/fonts/InterVariable.ttf", "static/assets/brand-mark.svg",
                "static/assets/social-card.png", "static/assets/logo-512.png",
                # Bluesky domain handle @transcriptoryt.com.
                "static/.well-known/atproto-did"}
RUNTIME = ("transformers.min.js", "Transformers-LICENSE.txt", "ONNXRuntime-LICENSE.txt",
           "ort-wasm-simd-threaded.mjs", "ort-wasm-simd-threaded.wasm",
           "ort-wasm-simd-threaded.asyncify.mjs", "ort-wasm-simd-threaded.asyncify.wasm")


def release_files(root: Path) -> list[Path]:
    paths = []
    for path in root.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(root)
        if relative.parts[0] not in DIRECTORIES and relative.as_posix() not in FILES:
            continue
        if any(part in EXCLUDED for part in relative.parts):
            continue
        if ((path.name.startswith(".env") or path.name.endswith(".env"))
                and not path.name.endswith(".example")):
            continue
        if path.suffix.lower() in {".pyc", ".log", ".sqlite3", ".db", ".pem", ".key"}:
            continue
        if (path.suffix.lower() not in SUFFIXES
                and path.name not in {"Dockerfile", "Caddyfile"}
                and relative.as_posix() not in FILES | BRAND_ASSETS):
            continue
        if re.search(r"(?:^|[-_.])(cookies?|credentials?|secrets?|tokens?)(?:[-_.]|$)", path.name, re.I):
            continue
        if path.name in {"youtube-urls.txt", "youtube-report.json"}:
            continue
        paths.append(path)
    return sorted(paths, key=lambda p: p.relative_to(root).as_posix())


def package(root: Path, output: Path) -> tuple[str, int]:
    output = output.resolve()
    try:
        relative_output = output.relative_to(root.resolve())
    except ValueError:
        relative_output = None
    if relative_output is not None and relative_output.parts[0] in DIRECTORIES:
        raise ValueError("Write the package outside source directories, for example dist/.")
    for name in RUNTIME:
        if not (root / "static/assets/vendor" / name).is_file():
            raise ValueError(f"Missing browser runtime {name}; run node scripts/setup-browser-runtime.mjs")
    paths = release_files(root)
    output.parent.mkdir(parents=True, exist_ok=True)
    checksums = []
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in paths:
            relative = path.relative_to(root).as_posix()
            data = path.read_bytes()
            checksums.append(f"{hashlib.sha256(data).hexdigest()}  {relative}")
            item = zipfile.ZipInfo(relative, date_time=(2026, 11, 22, 0, 0, 0))
            item.compress_type = zipfile.ZIP_DEFLATED
            item.create_system = 3
            item.external_attr = (0o100755 if path.suffix == ".sh" else 0o100644) << 16
            archive.writestr(item, data)
        manifest = zipfile.ZipInfo("SHA256SUMS", date_time=(2026, 11, 22, 0, 0, 0))
        manifest.create_system = 3
        manifest.external_attr = 0o100644 << 16
        archive.writestr(manifest, "\n".join(checksums) + "\n")
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + ".sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    return digest, len(paths)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist/transcriptor-lws.zip")
    args = parser.parse_args()
    digest, count = package(ROOT, args.output)
    print(f"Packaged {count} files: {args.output}\nSHA256 {digest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
