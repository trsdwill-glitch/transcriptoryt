"""Run the app's real YouTube ingestion from the VPS, without calling Groq."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import hashlib
from importlib.metadata import version
import json
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest import fetch_audio, probe_url  # noqa: E402
from app.models import AppError  # noqa: E402
from app.security import is_ytdlp_url, validate_url  # noqa: E402


def read_urls(path: Path, minimum: int) -> list[str]:
    urls = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines()
            if line.strip() and not line.lstrip().startswith("#")]
    urls = list(dict.fromkeys(validate_url(url) for url in urls))
    if len(urls) < minimum or any(not is_ytdlp_url(url) for url in urls):
        raise ValueError(f"Provide at least {minimum} distinct allowed YouTube video URLs.")
    return urls


async def run(urls: list[str], report: Path) -> bool:
    results = []
    for index, url in enumerate(urls, 1):
        started = time.monotonic()
        entry: dict = {"index": index, "url_sha256": hashlib.sha256(url.encode()).hexdigest()}
        try:
            with tempfile.TemporaryDirectory(prefix="transcriptor-youtube-") as folder:
                probe = await probe_url(url)
                info = await fetch_audio(url, folder)
                entry.update(ok=True, duration_s=info.duration_s, probe_kind=probe.kind)
        except AppError as error:
            entry.update(ok=False, code=error.code)
        except Exception:
            entry.update(ok=False, code="INTERNAL")
        entry["elapsed_s"] = round(time.monotonic() - started, 2)
        results.append(entry)
        print(f"{index}/{len(urls)} {'OK' if entry['ok'] else entry['code']}", flush=True)
        # Persist after every input so interrupted runs still have an honest partial report.
        successes = sum(row["ok"] for row in results)
        rate = successes / len(results)
        payload = {"checked_at_utc": datetime.now(timezone.utc).isoformat(),
                   "yt_dlp_version": version("yt-dlp"), "planned": len(urls),
                   "tested": len(results), "successes": successes,
                   "success_percent": round(100 * rate, 2),
                   "decision": "continue" if rate >= .9 else "update_and_retry" if rate >= .7 else "review_options",
                   "complete": len(results) == len(urls), "results": results}
        report.parent.mkdir(parents=True, exist_ok=True)
        temporary = report.with_suffix(report.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        temporary.replace(report)
    print(f"Success: {successes}/{len(urls)} ({rate:.0%}). Report excludes URLs and transcript content.")
    return rate >= .9


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--urls", required=True, type=Path)
    parser.add_argument("--report", default=Path("data/youtube-report.json"), type=Path)
    parser.add_argument("--minimum", default=30, type=int)
    args = parser.parse_args()
    try:
        if args.minimum < 1:
            raise ValueError("minimum must be positive")
        return 0 if asyncio.run(run(read_urls(args.urls, args.minimum), args.report)) else 1
    except (ValueError, OSError, AppError):
        print("Invalid input: use a UTF-8 file with distinct public YouTube URLs, one per line; "
              f"minimum={args.minimum}. No downloads started or report unavailable.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
