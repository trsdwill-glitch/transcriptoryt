"""Check a running instance; --public-audio explicitly uses the real paid/quota API."""
from __future__ import annotations

import argparse
import hashlib
import io
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import httpx

PUBLIC_AUDIO = ("https://huggingface.co/datasets/Narsil/asr_dummy/resolve/"
                "8d141c84e3f84c54cd7bbaa851d24edd0f559734/1.flac")
PUBLIC_AUDIO_SHA256 = "30885601173f96b0d8ddd020dc959b055c6c1582b85a33e3fcab8c4b08ed94c2"


def check(base_url: str, public_audio: bool = False, audio_file: Path | None = None) -> None:
    parsed = urlsplit(base_url)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.path not in {"", "/"} or parsed.query or parsed.fragment):
        raise ValueError("Use an http(s) base URL without credentials.")
    with httpx.Client(base_url=base_url.rstrip("/"), timeout=30, trust_env=False) as client:
        for path in ("/", "/privacidad/", "/terminos/", "/acerca-de/", "/contacto/",
                     "/youtube-a-texto/", "/video-a-texto/", "/en/",
                     "/robots.txt", "/sitemap.xml", "/assets/fonts/InterVariable.ttf",
                     "/assets/vendor/transformers.min.js",
                     "/assets/vendor/ort-wasm-simd-threaded.asyncify.wasm",
                     "/assets/vendor/ort-wasm-simd-threaded.wasm"):
            with client.stream("GET", path) as response:
                response.raise_for_status()
                assert next(response.iter_bytes(), b""), f"Empty resource: {path}"
            print(f"OK {path}")
        assert client.get("/api/health").raise_for_status().json()["ok"] is True
        config = client.get("/api/config").raise_for_status().json()
        assert set(config["formats"]) == {"txt", "md", "docx", "srt", "vtt"}
        before_response = client.get("/api/quota").raise_for_status()
        assert before_response.headers["cache-control"] == "no-store"
        before = before_response.json()
        assert {"limit_min", "used_min", "remaining_min", "resets_in_s"} <= before.keys()
        assert before["remaining_min"] >= 0 and client.cookies.get("tid")
        print(f"OK API and quota (remaining_min={before['remaining_min']:.3f})")
        if not public_audio and audio_file is None:
            return
        if audio_file is not None:
            if audio_file.stat().st_size > 10 * 1024 * 1024:
                raise ValueError("The smoke fixture must be smaller than 10 MiB.")
            audio = audio_file.read_bytes()
            filename = audio_file.name
        else:
            # Download a bounded public fixture on the test client, then upload it normally.
            with httpx.stream("GET", PUBLIC_AUDIO, follow_redirects=True, timeout=60) as response:
                response.raise_for_status()
                data = bytearray()
                for block in response.iter_bytes():
                    data.extend(block)
                    if len(data) > 10 * 1024 * 1024:
                        raise ValueError("Public fixture exceeds 10 MiB.")
            audio, filename = bytes(data), "public-speech.flac"
            if hashlib.sha256(audio).hexdigest() != PUBLIC_AUDIO_SHA256:
                raise ValueError("Public fixture checksum mismatch.")
        print(f"Fixture bytes={len(audio)} sha256={hashlib.sha256(audio).hexdigest()}")
        response = client.post("/api/jobs", data={"format": "docx", "timestamps": "true",
                               "language": "auto"}, files={"files": (filename, audio)})
        response.raise_for_status()
        job_id = response.json()["job_id"]
        deadline = time.monotonic() + 600
        try:
            while time.monotonic() < deadline:
                result = client.get(f"/api/jobs/{job_id}").raise_for_status().json()
                if result["status"] in {"done", "error", "cancelled"}:
                    break
                time.sleep(3)
            else:
                raise TimeoutError("Transcription did not finish within 600 seconds.")
            if result["status"] != "done":
                code = (result.get("error") or {}).get("code", "UNKNOWN")
                raise RuntimeError(f"Server transcription failed: {code}")
            from docx import Document
            download_path = result["result"]["download_url"]
            if not download_path.startswith("/api/jobs/") or "://" in download_path:
                raise ValueError("Unexpected download URL.")
            document = client.get(download_path).raise_for_status().content
            paragraphs = Document(io.BytesIO(document)).paragraphs
            assert any(p.text.strip() for p in paragraphs[2:]), "DOCX has no transcript body"
            after = client.get("/api/quota").raise_for_status().json()
            assert after["used_min"] > before["used_min"], "Quota was not charged"
            assert result["items"][0]["duration_s"] > 0
            print(f"OK real transcription -> DOCX -> download; bytes={len(document)}; "
                  f"quota_delta_min={after['used_min'] - before['used_min']:.4f}")
        finally:
            # A completed result remains available for its normal TTL. Cancel unfinished work.
            snapshot = client.get(f"/api/jobs/{job_id}")
            if snapshot.is_success and snapshot.json()["status"] not in {"done", "error", "cancelled"}:
                client.delete(f"/api/jobs/{job_id}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    fixture = parser.add_mutually_exclusive_group()
    fixture.add_argument("--public-audio", action="store_true",
                         help="Use a public speech sample and consume real server quota/provider usage")
    fixture.add_argument("--audio-file", type=Path)
    args = parser.parse_args()
    try:
        check(args.base_url, args.public_audio, args.audio_file)
    except Exception as error:
        # Never expose exception payloads, response bodies, URLs or credentials.
        print(f"FAILED ({type(error).__name__}); check service configuration and health.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
