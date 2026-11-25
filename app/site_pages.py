"""URL normalization for the static website. API routes are mounted separately."""
from urllib.parse import urlencode

from starlette.datastructures import Headers, QueryParams, URL
from starlette.exceptions import HTTPException
from starlette.responses import FileResponse, RedirectResponse, Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope


LANGUAGE_PAIRS = {
    "/": "/en/",
    "/acerca-de/": "/en/about/",
    "/contacto/": "/en/contact/",
    "/privacidad/": "/en/privacy/",
    "/terminos/": "/en/terms/",
}
PAGES = {*LANGUAGE_PAIRS, *LANGUAGE_PAIRS.values(), "/youtube-a-texto/", "/video-a-texto/"}
LEGACY = {
    **{path.removeprefix("/en").rstrip("/") + ".html": path
       for path in LANGUAGE_PAIRS.values() if path != "/en/"},
    "/transcribir-video-de-youtube-a-texto": "/youtube-a-texto/",
    "/en/youtube-to-text": "/youtube-a-texto/",
}


class SitePages(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        url = URL(scope=scope)
        if scope["method"] not in {"GET", "HEAD"}:
            return await super().get_response(path, scope)
        if url.path == "/api" or url.path.startswith("/api/"):
            raise HTTPException(status_code=404)

        # Caddy sets this only on the HTTP/www virtual hosts and strips it on HTTPS.
        alias_origin = Headers(scope=scope).get("x-site-alias") == "transcriptoryt.com"
        prefix = "https://transcriptoryt.com" if alias_origin else ""
        slug = url.path.removesuffix("/index.html").rstrip("/")
        target = LEGACY.get(slug, slug + "/")
        if target in PAGES:
            params = QueryParams(url.query)
            language = params.get("lang")
            query = url.query
            if language in {"es", "en"}:
                for es, en in LANGUAGE_PAIRS.items():
                    if target in (es, en):
                        target = es if language == "es" else en
                        break
                query = urlencode([(k, v) for k, v in params.multi_items() if k != "lang"])
            if alias_origin or url.path != target or query != url.query:
                return RedirectResponse(prefix + target + ("?" + query if query else ""), status_code=301)
        elif alias_origin:
            return RedirectResponse(prefix + url.path + ("?" + url.query if url.query else ""), status_code=301)

        if url.path == "/404.html":
            return FileResponse(self.directory / "404.html", status_code=404, headers={"X-Robots-Tag": "noindex"})
        return await super().get_response(path, scope)

