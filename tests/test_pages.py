"""Crawlable pages, language alternates, structured data and URL normalization of the static site."""
from __future__ import annotations

from collections import Counter
from dataclasses import replace
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit
import xml.etree.ElementTree as ET

import httpx
import pytest
import pytest_asyncio


SITE = "https://transcriptoryt.com"
PAIRS = {"/": "/en/", "/acerca-de/": "/en/about/", "/contacto/": "/en/contact/",
         "/privacidad/": "/en/privacy/", "/terminos/": "/en/terms/"}
SPANISH_ONLY = ("/youtube-a-texto/", "/video-a-texto/")
TOOLS = ("/", "/youtube-a-texto/", "/video-a-texto/", "/en/")


class Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[str] = []
        self.lang = ""
        self.alternates: dict[str, str] = {}
        self.title = ""
        self.meta: dict[str, str] = {}
        self.canonical = ""
        self.h1 = 0
        self.schemas: list[dict] = []
        self.text = ""
        self._capture: str | None = None
        self._schema = ""
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = {key: value or "" for key, value in attrs}
        if tag == "html":
            self.lang = attributes.get("lang", "")
        elif tag == "link" and attributes.get("rel") == "alternate":
            assert attributes["hreflang"] not in self.alternates
            self.alternates[attributes["hreflang"]] = attributes["href"]
        elif tag == "link" and attributes.get("rel") == "canonical":
            self.canonical = attributes["href"]
        elif tag == "meta" and ("name" in attributes or "property" in attributes):
            self.meta[attributes.get("name") or attributes["property"]] = attributes.get("content", "")
        elif tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        elif tag == "title":
            self._capture = "title"
        elif tag == "h1":
            self.h1 += 1
        if tag == "script" and attributes.get("type") == "application/ld+json":
            self._capture = "schema"
            self._schema = ""
        elif tag in {"script", "style", "template"}:
            self._skip += 1

    def handle_data(self, data: str) -> None:
        if self._capture == "title":
            self.title += data
        elif self._capture == "schema":
            self._schema += data
        elif not self._skip:
            self.text += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._capture == "schema":
            self.schemas.append(json.loads(self._schema))
            self._capture = None
        elif tag in {"script", "style", "template"}:
            self._skip -= 1
        elif tag == "title":
            self._capture = None


@pytest_asyncio.fixture
async def client(monkeypatch: pytest.MonkeyPatch):
    from app import main

    monkeypatch.setattr(main, "settings", replace(main.settings, trusted_proxy=False, env="development"))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as value:
        yield value


async def load_pages(client: httpx.AsyncClient) -> dict[str, Page]:
    sitemap = await client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    root = ET.fromstring(sitemap.content)
    entries = root.findall("{*}url")
    pages: dict[str, Page] = {}
    for entry in entries:
        url = entry.findtext("{*}loc") or ""
        assert url.startswith(SITE + "/") and url.endswith("/") and "?" not in url
        assert len(entry.findtext("{*}lastmod") or "") == 10
        path = urlsplit(url).path
        response = await client.get(path)
        assert response.status_code == 200, path
        assert response.headers["content-type"].startswith("text/html")
        page = Page()
        page.feed(response.text)
        assert page.canonical == url
        pages[path] = page
    assert set(pages) == {*PAIRS, *PAIRS.values(), *SPANISH_ONLY}
    return pages


@pytest.mark.asyncio
async def test_pages_have_unique_metadata_and_language(client: httpx.AsyncClient) -> None:
    pages = await load_pages(client)
    assert len({page.title for page in pages.values()}) == len(pages)
    assert len({page.meta["description"] for page in pages.values()}) == len(pages)
    for path, page in pages.items():
        assert page.lang == ("en" if path.startswith("/en/") else "es")
        assert page.h1 == 1, path
        assert len(page.title) <= 65 and len(page.meta["description"]) <= 160, path
        assert "keywords" not in page.meta
        assert page.meta["og:url"] == page.canonical
        assert page.meta["og:locale"] == ("en_US" if page.lang == "en" else "es_LA")
        assert page.meta["twitter:card"] == "summary_large_image"
        for key in ("og:title", "og:description", "og:type", "og:site_name", "og:image", "og:image:alt"):
            assert page.meta[key], (path, key)
    assert pages["/"].title == "Transcribir audio a texto gratis y sin registro | transcriptoryt"
    assert pages["/youtube-a-texto/"].title == "Transcribir video de YouTube a texto gratis | transcriptoryt"
    assert pages["/video-a-texto/"].title == "Video a texto gratis: transcribe tus videos | transcriptoryt"


@pytest.mark.asyncio
async def test_hreflang_is_reciprocal_only_between_equivalents(client: httpx.AsyncClient) -> None:
    pages = await load_pages(client)
    for es, en in PAIRS.items():
        expected = {"es": SITE + es, "en": SITE + en, "x-default": SITE + es}
        assert pages[es].alternates == expected
        assert pages[en].alternates == expected
        assert {"es": es, "en": en} == {lang: urlsplit(link).path for lang, link in expected.items() if lang != "x-default"}
        for path, other in ((es, en), (en, es)):
            assert other in pages[path].links, f"{path} must link to its translation"
    for path in SPANISH_ONLY:
        assert pages[path].alternates == {}


@pytest.mark.asyncio
async def test_structured_data(client: httpx.AsyncClient) -> None:
    pages = await load_pages(client)
    for path, page in pages.items():
        assert len(page.schemas) == 1, path
        graph = page.schemas[0]["@graph"]
        types = Counter(item["@type"] for item in graph)
        serialized = json.dumps(page.schemas)
        assert "FAQPage" not in serialized and "aggregateRating" not in serialized and '"review' not in serialized
        if path in TOOLS:
            app = next(item for item in graph if item["@type"] == "WebApplication")
            assert app["url"] == page.canonical and app["inLanguage"] == page.lang
            assert app["applicationCategory"] == "UtilitiesApplication" and app["operatingSystem"] == "Any"
            assert app["offers"]["price"] == 0 and app["offers"]["priceCurrency"] == "USD"
            assert app["featureList"]
        if path in {"/", "/en/"}:
            assert types["WebSite"] == types["Organization"] == 1
        else:
            crumbs = next(item for item in graph if item["@type"] == "BreadcrumbList")["itemListElement"]
            assert crumbs[0]["item"] == SITE + ("/en/" if path.startswith("/en/") else "/")
            assert crumbs[-1]["item"] == page.canonical


@pytest.mark.asyncio
async def test_internal_links_resolve_and_every_page_is_linked(client: httpx.AsyncClient) -> None:
    pages = await load_pages(client)
    incoming: Counter[str] = Counter()
    checked: dict[str, int] = {}
    for path, page in pages.items():
        for link in set(page.links):
            if not link.startswith("/") or link.startswith("//"):
                continue
            target = urlsplit(link).path
            if target not in checked:
                checked[target] = (await client.get(target)).status_code
            assert checked[target] == 200, (path, link)
            if target != path:
                incoming[target] += 1
    for path in pages:
        assert incoming[path] >= 2, path


@pytest.mark.asyncio
async def test_seo_text_is_in_initial_html(client: httpx.AsyncClient) -> None:
    pages = await load_pages(client)
    expected = {
        "/": ("Cómo convertir audio a texto en 3 pasos", "Preguntas frecuentes", "Otras herramientas"),
        "/youtube-a-texto/": ("Cómo convertir un video de YouTube a texto en 3 pasos", "YouTube puede rechazar"),
        "/video-a-texto/": ("Cómo convertir video a texto en 3 pasos", "Otras herramientas"),
    }
    for path, phrases in expected.items():
        for phrase in phrases:
            assert phrase in pages[path].text, (path, phrase)


@pytest.mark.asyncio
@pytest.mark.parametrize(("source", "target"), [
    ("/about.html", "/en/about/"), ("/contact.html", "/en/contact/"), ("/privacy.html", "/en/privacy/"),
    ("/terms.html", "/en/terms/"), ("/index.html", "/"), ("/en", "/en/"), ("/en/index.html", "/en/"),
    ("/youtube-a-texto", "/youtube-a-texto/"), ("/video-a-texto/index.html", "/video-a-texto/"),
    ("/acerca-de", "/acerca-de/"), ("/?lang=en", "/en/"), ("/en/privacy/?lang=es", "/privacidad/"),
    ("/transcribir-video-de-youtube-a-texto/", "/youtube-a-texto/"), ("/en/youtube-to-text/", "/youtube-a-texto/"),
])
async def test_legacy_and_variant_urls_redirect_once(client: httpx.AsyncClient, source: str, target: str) -> None:
    response = await client.get(source)
    assert response.status_code == 301
    assert response.headers["location"] == target
    assert (await client.get(target)).status_code == 200


@pytest.mark.asyncio
async def test_alias_hosts_redirect_to_canonical_origin(client: httpx.AsyncClient) -> None:
    headers = {"X-Site-Alias": "transcriptoryt.com"}
    response = await client.get("/about.html", headers=headers)
    assert (response.status_code, response.headers["location"]) == (301, SITE + "/en/about/")
    response = await client.get("/youtube-a-texto/?mode=local", headers=headers)
    assert (response.status_code, response.headers["location"]) == (301, SITE + "/youtube-a-texto/?mode=local")


@pytest.mark.asyncio
async def test_tool_query_parameters_are_not_redirected(client: httpx.AsyncClient) -> None:
    for path in ("/?mode=local", "/youtube-a-texto/?mock=1"):
        assert (await client.get(path)).status_code == 200


@pytest.mark.asyncio
async def test_missing_page_is_a_real_404(client: httpx.AsyncClient) -> None:
    for path in ("/no-existe/", "/404.html"):
        response = await client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        page = Page()
        page.feed(response.text)
        assert page.meta["robots"] == "noindex" and page.h1 == 1
        assert "/" in page.links and "/youtube-a-texto/" in page.links


ROOT = Path(__file__).resolve().parents[1]
# Owner decision of 2026-11-24: per user over 24 h and per file.
EXPECTED_LIMITS = {"MAX_ITEMS": 5, "MAX_UPLOAD_MB": 500, "MAX_DOWNLOAD_MB": 500,
                   "MAX_FILE_MIN": 120, "DAILY_QUOTA_MIN": 120}


def env_file(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    return dict(line.split("=", 1) for line in lines if re.match(r"[A-Z_]+=", line))


def limit_sentences() -> dict[str, str]:
    copy = (ROOT / "static/assets/tool-copy.js").read_text(encoding="utf-8")
    sentences = {}
    for lang in ("en", "es"):
        block = re.search(rf"\n  {lang}: \{{(.*?)\n  \}},", copy, re.S)[1]
        template = re.search(r'uploadHelp: "([^"]+)"', block)[1]
        values = {"maxItems": EXPECTED_LIMITS["MAX_ITEMS"], "maxMb": EXPECTED_LIMITS["MAX_UPLOAD_MB"],
                  "maxMin": EXPECTED_LIMITS["MAX_FILE_MIN"]}
        sentences[lang] = re.sub(r"\{(\w+)\}", lambda match: str(values[match[1]]), template)
    return sentences


def test_limit_configuration_is_consistent() -> None:
    from app.config import Settings

    defaults = Settings()
    for source in (env_file(ROOT / "deploy/.env.example"), env_file(ROOT / ".env.example")):
        for name, value in EXPECTED_LIMITS.items():
            assert int(source[name]) == value, name
    for name, value in EXPECTED_LIMITS.items():
        assert getattr(defaults, name.lower()) == value, name
    production = env_file(ROOT / "deploy/.env.example")
    body = EXPECTED_LIMITS["MAX_ITEMS"] * EXPECTED_LIMITS["MAX_UPLOAD_MB"] * 1048576 + 1048576
    assert int(production["MAX_REQUEST_BODY"]) == body
    assert f"{{$MAX_REQUEST_BODY:{body}}}" in (ROOT / "deploy/Caddyfile").read_text(encoding="utf-8")
    app_js = (ROOT / "static/assets/app.js").read_text(encoding="utf-8")
    assert f"max_upload_mb: {EXPECTED_LIMITS['MAX_UPLOAD_MB']}," in app_js  # ?mock=1 configuration
    assert f"max_file_min: {EXPECTED_LIMITS['MAX_FILE_MIN']}," in app_js
    assert f"max_upload_mb ?? {EXPECTED_LIMITS['MAX_UPLOAD_MB']}" in app_js
    assert f"max_file_min ?? {EXPECTED_LIMITS['MAX_FILE_MIN']}" in app_js


@pytest.mark.asyncio
async def test_limit_text_matches_tool_and_configuration(client: httpx.AsyncClient) -> None:
    sentences = limit_sentences()
    config = (await client.get("/api/config")).json()
    assert (config["max_items"], config["max_upload_mb"], config["max_file_min"]) == (
        EXPECTED_LIMITS["MAX_ITEMS"], EXPECTED_LIMITS["MAX_UPLOAD_MB"], EXPECTED_LIMITS["MAX_FILE_MIN"])
    quota = {"es": f"hasta {EXPECTED_LIMITS['DAILY_QUOTA_MIN']} minutos de audio en un período móvil de 24 horas",
             "en": f"up to {EXPECTED_LIMITS['DAILY_QUOTA_MIN']} minutes of audio in a rolling 24-hour period"}
    for path in TOOLS:
        lang = "en" if path.startswith("/en/") else "es"
        html = (await client.get(path)).text
        upload_help = re.search(r'<span id="upload-help">([^<]*)</span>', html)[1]
        assert upload_help == sentences[lang], path
        page = Page()
        page.feed(html)
        # Once in the tool, once in the FAQ answer, with the same wording.
        assert page.text.count(sentences[lang]) == 2, path
        assert quota[lang] in page.text, path
    for file in [*(ROOT / "static").rglob("*.html"), *(ROOT / "scripts/content").glob("*.html")]:
        text = file.read_text(encoding="utf-8")
        for stale in ("30 minutos", "30 minutes", "30 min ", "MiB", "100 MB", "per upload"):
            assert stale not in text, (file, stale)


@pytest.mark.asyncio
async def test_robots_and_api_noindex(client: httpx.AsyncClient) -> None:
    robots = (await client.get("/robots.txt")).text
    assert "Allow: /\n" in robots and "Disallow: /api/" in robots
    assert "/assets" not in robots and f"Sitemap: {SITE}/sitemap.xml" in robots
    response = await client.get("/api/health")
    assert response.headers["x-robots-tag"] == "noindex"


@pytest.mark.asyncio
async def test_bluesky_domain_handle(client: httpx.AsyncClient) -> None:
    # Bluesky verifies @transcriptoryt.com by reading this exact DID from the domain.
    response = await client.get("/.well-known/atproto-did")
    assert response.status_code == 200
    assert response.text.strip() == "did:plc:n4lxrukfc632tfzra5wqeyke"
