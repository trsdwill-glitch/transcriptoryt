"""Replace publication markers in a release copy, or check that none remain."""
from __future__ import annotations

import argparse
from datetime import date
import html
import json
from pathlib import Path
import re
import sys

TOKENS = ("SITE_NAME", "DOMAIN", "CONTACT_EMAIL", "OPERATOR_NAME", "OPERATOR_ADDRESS", "LAST_UPDATED")
JSON_SCRIPT = re.compile(r'(<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>)(.*?)(</script>)', re.S)


def site_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file()
                  and p.suffix in {".html", ".txt", ".xml", ".js"} and "vendor" not in p.parts)


def render(content: str, values: dict[str, str], is_html: bool) -> str:
    def replace(text: str, escape: str) -> str:
        for token, value in values.items():
            if escape == "json":
                value = json.dumps(value, ensure_ascii=False)[1:-1].replace("<", "\\u003c")
            elif escape == "html":
                value = html.escape(value, quote=True)
            text = text.replace(token, value)
        return text
    if not is_html:
        # The only markers in app.js are branding strings; keep JavaScript strings safe.
        return replace(content, "json")
    result, previous = [], 0
    for match in JSON_SCRIPT.finditer(content):
        result.extend((replace(content[previous:match.start()], "html"), match[1],
                       replace(match[2], "json"), match[3]))
        previous = match.end()
    result.append(replace(content[previous:], "html"))
    return "".join(result)


def check(root: Path) -> list[str]:
    unresolved = []
    for path in site_files(root):
        content = path.read_text(encoding="utf-8")
        found = [token for token in TOKENS if token in content]
        if found:
            unresolved.append(f"{path.relative_to(root)}: {', '.join(found)}")
    return unresolved


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "static")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true")
    mode.add_argument("--apply", action="store_true", help="Modify this release copy in place")
    for option in ("name", "domain", "email", "operator", "address", "date"):
        parser.add_argument(f"--{option}")
    args = parser.parse_args()
    if not args.root.is_dir():
        parser.error("Static directory is missing.")
    if args.apply:
        raw = (args.name, args.domain, args.email, args.operator, args.address, args.date)
        if any(not value or any(ord(c) < 32 for c in value) for value in raw):
            parser.error("All six values are required and must not contain control characters.")
        if not re.fullmatch(r"(?=.{1,253}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.)+[A-Za-z]{2,63}", args.domain):
            parser.error("Domain must be a hostname without protocol, path or port.")
        if not re.fullmatch(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,63}", args.email):
            parser.error("Invalid email address.")
        try:
            date.fromisoformat(args.date)
        except ValueError:
            parser.error("Date must be YYYY-MM-DD.")
        values = dict(zip(TOKENS, raw))
        if any(token in value for value in raw for token in TOKENS):
            parser.error("Values must not contain publication markers.")
        for path in site_files(args.root):
            content = path.read_text(encoding="utf-8")
            rendered = render(content, values, path.suffix == ".html")
            if content != rendered:
                path.write_text(rendered, encoding="utf-8", newline="\n")
    unresolved = check(args.root)
    if unresolved:
        print("Publication markers remain:\n" + "\n".join(unresolved), file=sys.stderr)
        return 1
    print("OK: no publication markers remain. Review legal facts before publishing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
