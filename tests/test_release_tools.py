from __future__ import annotations

import asyncio
import hashlib
import io
import json
from pathlib import Path
import sys
import zipfile

import httpx
import pytest

from scripts import configure_site, package_release, smoke_server, test_ytdlp


def test_publication_values_preserve_html_json_and_javascript(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    name = 'A "quoted" & <script>name</script>'
    page = tmp_path / "index.html"
    page.write_text('<title>SITE_NAME</title><a href="mailto:CONTACT_EMAIL">OPERATOR_NAME</a>'
                    '<script type="application/ld+json">{"name":"SITE_NAME","url":"https://DOMAIN/"}</script>'
                    '<p>OPERATOR_ADDRESS LAST_UPDATED</p>', encoding="utf-8")
    javascript = tmp_path / "app.js"
    javascript.write_text('const title = "SITE_NAME";', encoding="utf-8")
    (tmp_path / "sitemap.xml").write_text('<loc>https://DOMAIN/</loc>', encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["configure_site", "--root", str(tmp_path), "--apply",
                        "--name", name, "--domain", "transcribe.example.com", "--email", "hello@example.com",
                        "--operator", "Someone & Co", "--address", 'A < B "Street"', "--date", "2026-11-22"])
    assert configure_site.main() == 0
    content = page.read_text(encoding="utf-8")
    assert '<title>A &quot;quoted&quot; &amp; &lt;script&gt;name&lt;/script&gt;</title>' in content
    assert "A &lt; B &quot;Street&quot;" in content
    payload = configure_site.JSON_SCRIPT.search(content)
    assert payload is not None
    assert "</script>" not in payload[2]
    assert json.loads(payload[2]) == {"name": name, "url": "https://transcribe.example.com/"}
    assert json.loads(javascript.read_text(encoding="utf-8").split("=", 1)[1].strip().rstrip(";")) == name
    assert configure_site.check(tmp_path) == []


@pytest.mark.parametrize(("option", "value"), [
    ("--domain", "example.com/<script>"), ("--domain", "https://example.com"),
    ("--email", 'a@example.com" onclick="alert(1)'), ("--name", "Injected\nline"),
    ("--operator", "SITE_NAME"), ("--date", "2026-02-30"),
])
def test_publication_rejects_unsafe_values_before_writing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, option: str, value: str,
) -> None:
    page = tmp_path / "index.html"
    original = "<title>SITE_NAME</title>"
    page.write_text(original, encoding="utf-8")
    arguments = ["configure_site", "--root", str(tmp_path), "--apply", "--name", "Example",
                 "--domain", "example.com", "--email", "hello@example.com", "--operator", "Someone",
                 "--address", "Example Street", "--date", "2026-11-22"]
    arguments[arguments.index(option) + 1] = value
    monkeypatch.setattr(sys, "argv", arguments)
    with pytest.raises(SystemExit) as error:
        configure_site.main()
    assert error.value.code == 2
    assert page.read_text(encoding="utf-8") == original


def test_marker_check_ignores_vendor_but_detects_public_metadata(tmp_path: Path) -> None:
    vendor = tmp_path / "assets/vendor"
    vendor.mkdir(parents=True)
    (vendor / "library.js").write_text("SITE_NAME", encoding="utf-8")
    (tmp_path / "robots.txt").write_text("Sitemap: https://DOMAIN/sitemap.xml", encoding="utf-8")
    assert configure_site.check(tmp_path) == ["robots.txt: DOMAIN"]


def release_fixture(root: Path) -> None:
    vendor = root / "static/assets/vendor"
    vendor.mkdir(parents=True)
    for name in package_release.RUNTIME:
        (vendor / name).write_bytes(b"fixture runtime")
    for relative in ("README.md", ".env.example", "deploy/.env.example", "deploy/health.sh",
                     "static/index.html", "static/assets/fonts/InterVariable.ttf", "app/main.py",
                     "docs/notes.md"):
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("public fixture\n", encoding="utf-8")


def test_release_excludes_local_secrets_and_manifest_matches_every_file(tmp_path: Path) -> None:
    root = tmp_path / "source"
    release_fixture(root)
    secrets = (".env", "deploy/.env.production", "deploy/provider.env", "deploy/cookies.txt",
               "deploy/credentials.json", "docs/private.key", "docs/backup.sqlite3", "docs/debug.log",
               "data/transcript.txt", "scripts/__pycache__/cached.pyc", "NOT_ALLOWED.txt")
    for relative in secrets:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("sensitive sentinel", encoding="utf-8")
    output = root / "dist/release.zip"
    digest, count = package_release.package(root, output)
    with zipfile.ZipFile(output) as archive:
        names = set(archive.namelist())
        assert not names.intersection(secrets)
        assert {".env.example", "deploy/.env.example", "app/main.py",
                "static/assets/fonts/InterVariable.ttf"} <= names
        assert count == len(names) - 1
        manifest = archive.read("SHA256SUMS").decode().splitlines()
        listed = {}
        for line in manifest:
            expected, relative = line.split("  ", 1)
            listed[relative] = expected
            assert hashlib.sha256(archive.read(relative)).hexdigest() == expected
        assert set(listed) == names - {"SHA256SUMS"}
        assert all(b"sensitive sentinel" not in archive.read(name) for name in names)
        assert archive.getinfo("deploy/health.sh").external_attr >> 16 & 0o777 == 0o755
    assert hashlib.sha256(output.read_bytes()).hexdigest() == digest
    assert output.with_suffix(".zip.sha256").read_text().strip() == f"{digest}  release.zip"
    second = root / "dist/second.zip"
    assert package_release.package(root, second)[0] == digest


def test_release_refuses_missing_browser_runtime(tmp_path: Path) -> None:
    release_fixture(tmp_path)
    (tmp_path / "static/assets/vendor" / package_release.RUNTIME[0]).unlink()
    with pytest.raises(ValueError, match="Missing browser runtime"):
        package_release.package(tmp_path, tmp_path / "dist/release.zip")
    assert not (tmp_path / "dist/release.zip").exists()


def test_release_refuses_output_inside_packaged_source(tmp_path: Path) -> None:
    release_fixture(tmp_path)
    output = tmp_path / "deploy/release.zip"
    output.write_bytes(b"previous release")
    with pytest.raises(ValueError, match="outside source directories"):
        package_release.package(tmp_path, output)
    assert output.read_bytes() == b"previous release"


@pytest.mark.parametrize("url", ["https://user:secret@example.com", "https://:secret@example.com"])
def test_smoke_rejects_credentials_before_creating_http_client(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden_client(*args: object, **kwargs: object) -> None:
        pytest.fail("Credentials must be rejected before creating a client")
    monkeypatch.setattr(smoke_server.httpx, "Client", forbidden_client)
    with pytest.raises(ValueError, match="credentials"):
        smoke_server.check(url)


def test_smoke_reports_failure_without_exception_payload(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fail(*args: object, **kwargs: object) -> None:
        raise RuntimeError("SECRET_KEY=sensitive sentinel https://user:pass@private.example")
    monkeypatch.setattr(smoke_server, "check", fail)
    monkeypatch.setattr(sys, "argv", ["smoke_server"])
    assert smoke_server.main() == 1
    captured = capsys.readouterr()
    assert "FAILED (RuntimeError)" in captured.err
    assert "sensitive sentinel" not in captured.out + captured.err
    assert "private.example" not in captured.out + captured.err


@pytest.mark.parametrize("has_transcript", [True, False])
def test_smoke_checks_docx_and_quota_without_printing_transcript(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    has_transcript: bool,
) -> None:
    from docx import Document
    document = Document()
    document.add_heading("Fixture title", level=1)
    document.add_paragraph("Fixture source and language metadata")
    if has_transcript:
        document.add_paragraph("[00:00] PRIVATE TRANSCRIPT SENTINEL")
    buffer = io.BytesIO()
    document.save(buffer)
    quota_reads = 0
    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal quota_reads
        path = request.url.path
        if path == "/api/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/api/config":
            return httpx.Response(200, json={"formats": ["txt", "md", "docx", "srt", "vtt"]})
        if path == "/api/quota":
            quota_reads += 1
            return httpx.Response(200, headers={"cache-control": "no-store", "set-cookie": "tid=fixture; Path=/"},
                                  json={"limit_min": 30, "used_min": 0 if quota_reads == 1 else 1,
                                        "remaining_min": 30 if quota_reads == 1 else 29, "resets_in_s": 1})
        if path == "/api/jobs":
            assert request.method == "POST"
            return httpx.Response(202, json={"job_id": "fixture"})
        if path == "/api/jobs/fixture":
            return httpx.Response(200, json={"status": "done", "result": {"download_url": "/api/jobs/fixture/download"},
                                            "items": [{"duration_s": 60}]})
        if path == "/api/jobs/fixture/download":
            return httpx.Response(200, content=buffer.getvalue())
        assert request.method == "GET"
        return httpx.Response(200, content=b"fixture public asset")
    original_client = httpx.Client
    def fake_client(**kwargs: object) -> httpx.Client:
        return original_client(**kwargs, transport=httpx.MockTransport(respond))
    monkeypatch.setattr(smoke_server.httpx, "Client", fake_client)
    audio = tmp_path / "test.flac"
    audio.write_bytes(b"fixture audio")
    if has_transcript:
        smoke_server.check("http://testserver", audio_file=audio)
    else:
        with pytest.raises(AssertionError):
            smoke_server.check("http://testserver", audio_file=audio)
    output = capsys.readouterr()
    if has_transcript:
        assert "OK real transcription -> DOCX -> download" in output.out
        assert "quota_delta_min=1.0000" in output.out
    else:
        assert "OK real transcription" not in output.out
    assert "PRIVATE TRANSCRIPT SENTINEL" not in output.out + output.err


def test_youtube_report_excludes_url_and_records_real_ingestion_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:
    from app.models import AppError, AudioInfo, RemoteProbe
    urls = ["https://www.youtube.com/watch?v=fixture0001", "https://youtu.be/fixture0002"]
    async def probe(url: str) -> RemoteProbe:
        return RemoteProbe("PRIVATE TITLE", 10, "ytdlp")
    async def fetch(url: str, folder: str) -> AudioInfo:
        if url == urls[1]:
            raise AppError("YOUTUBE_BLOCKED", "PRIVATE PROVIDER DETAILS")
        return AudioInfo(str(Path(folder) / "fixture.mp3"), 10, "PRIVATE TITLE", url)
    monkeypatch.setattr(test_ytdlp, "probe_url", probe)
    monkeypatch.setattr(test_ytdlp, "fetch_audio", fetch)
    monkeypatch.setattr(test_ytdlp, "version", lambda name: "fixture-version")
    report = tmp_path / "report.json"
    assert asyncio.run(test_ytdlp.run(urls, report)) is False
    content = report.read_text()
    payload = json.loads(content)
    assert payload["complete"] is True and payload["successes"] == 1
    assert payload["decision"] == "review_options"
    assert payload["results"][1]["code"] == "YOUTUBE_BLOCKED"
    output = capsys.readouterr()
    assert all(url not in content + output.out + output.err for url in urls)
    assert "PRIVATE" not in content + output.out + output.err
    assert not report.with_suffix(".json.tmp").exists()
