// Regenerate every public page, sitemap.xml and 404.html: `node scripts/build-tool-pages.mjs`.
// Page metadata lives in site-pages.mjs and page text in scripts/content/. A page's sitemap lastmod
// only changes when its generated HTML changes.
import { createHash } from "node:crypto";
import { readFile, writeFile, mkdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import copy from "../static/assets/tool-copy.js";
import { pages } from "./site-pages.mjs";

const site = "https://transcriptoryt.com";
const root = fileURLToPath(new URL("../static/", import.meta.url));
const source = async name => (await readFile(new URL(name, import.meta.url), "utf8")).replaceAll("\r\n", "\n");
const shell = await source("./page.html");
const form = await source("./tool-page.html");
const tools = ["audio", "youtube", "video"];
const date = new Date().toISOString().slice(0, 10);
const image = site + "/assets/social-card.png";
// The operator already named on the about, contact, privacy and terms pages.
const founder = { name: "Thiago Raul Solaeche Duarte",
  linkedin: "https://www.linkedin.com/in/thiago-raul-solaeche-duarte-8a3400265/" };
const find = (key, lang) => pages.find(p => p.key === key && p.lang === lang);
// Cache-busting query from the asset content, so a publish never mixes old and new files.
const versions = Object.fromEntries(await Promise.all(["style.css", "transcriber.css", "tool-copy.js", "app.js"].map(async name =>
  [name, createHash("sha256").update(await readFile(join(root, "assets", name))).digest("hex").slice(0, 10)])));
const asset = name => `/assets/${name}?v=${versions[name]}`;
const escape = text => text.replaceAll("&", "&amp;").replaceAll("<", "&lt;").replaceAll('"', "&quot;").replaceAll(">", "&gt;");

function fill(template, fields) {
  return template.replace(/{{(\w+)}}/g, (_, key) => {
    if (fields[key] === undefined) throw new Error(`Missing template value: ${key}`);
    return fields[key];
  });
}

function anchor(target, page) {
  const spanishTool = page.lang === "en" && target.lang === "es";
  const label = spanishTool ? { youtube: "YouTube to text (Spanish)", video: "Video to text (Spanish)" }[target.key] : target.label;
  const current = target.path === page.path ? ' aria-current="page"' : "";
  const language = spanishTool ? ' hreflang="es"' : "";
  return `<a href="${target.path}"${language}${current}>${label}</a>`;
}

// Public limit text comes from the production profile, the same file install-lws-native.sh
// turns into the server's environment. tests/test_pages.py checks it against app/config.py.
const production = Object.fromEntries((await readFile(new URL("../deploy/.env.example", import.meta.url), "utf8"))
  .split(/\r?\n/).filter(line => /^[A-Z_]+=/.test(line)).map(line => line.split("=", 2)));
const limits = {
  maxItems: Number(production.MAX_ITEMS), maxMb: Number(production.MAX_UPLOAD_MB),
  maxMin: Number(production.MAX_FILE_MIN), quotaMin: Number(production.DAILY_QUOTA_MIN),
};
if (Object.values(limits).some(value => !Number.isInteger(value) || value <= 0)) throw new Error("Invalid limits in deploy/.env.example");
if (Number(production.MAX_DOWNLOAD_MB) !== limits.maxMb) throw new Error("MAX_DOWNLOAD_MB must equal MAX_UPLOAD_MB: the text states one size per file.");
const quotaText = {
  es: `En el modo Servidor, cada usuario puede transcribir hasta ${limits.quotaMin} minutos de audio en un período móvil de 24 horas.`,
  en: `In Server mode, each user can transcribe up to ${limits.quotaMin} minutes of audio in a rolling 24-hour period.`,
};
const limitText = lang => copy.toolCopy("audio", lang).uploadHelp.replace(/\{(\w+)\}/g, (_, key) => String(limits[key]));

function toolForm(page) {
  const words = copy.toolCopy(page.key, page.lang);
  const fields = Object.fromEntries(Object.entries(words).map(([k, v]) => [k, escape(v)]));
  fields.initialUploadHelp = escape(limitText(page.lang));
  const template = form.replace(/ *<!-- links -->\n([\s\S]*?) *<!-- upload -->\n([\s\S]*?) *<!-- end inputs -->\n/,
    (_, links, upload) => page.key === "youtube" ? links + upload : upload + links);
  return fill(template, fields);
}

function schema(page, trail) {
  const graph = [];
  if (page.key === "audio") {
    graph.push({ "@type": "WebSite", "@id": site + "/#website", name: "transcriptoryt", url: site + "/",
      inLanguage: ["es", "en"], publisher: { "@id": site + "/#organization" } },
    { "@type": "Organization", "@id": site + "/#organization", name: "transcriptoryt",
      url: site + "/", logo: site + "/assets/logo-512.png",
      // Official brand profiles. Add each new profile here so Google ties it to the site.
      sameAs: ["https://www.youtube.com/@transcriptoryt", "https://x.com/transcriptoryt_",
        "https://bsky.app/profile/transcriptoryt.com"],
      founder: { "@type": "Person", "@id": site + "/#founder", name: founder.name, sameAs: [founder.linkedin] } });
  }
  if (tools.includes(page.key)) {
    graph.push({ "@type": "WebApplication", name: `transcriptoryt: ${page.label}`, url: site + page.path, inLanguage: page.lang,
      applicationCategory: "UtilitiesApplication", operatingSystem: "Any",
      offers: { "@type": "Offer", price: 0, priceCurrency: "USD" },
      featureList: page.lang === "es" ? ["Reconocimiento de voz en el navegador", "Enlaces y archivos en el servidor",
        "Descargas TXT, Markdown, DOCX, SRT y VTT", "Marcas de tiempo"] :
        ["On-device speech recognition", "Server transcription of links and files", "TXT, Markdown, DOCX, SRT and VTT downloads", "Timestamps"] });
  }
  if (trail.length) graph.push({ "@type": "BreadcrumbList", itemListElement: trail.map(({ name, path }, index) =>
    ({ "@type": "ListItem", position: index + 1, name, item: site + path })) });
  return JSON.stringify({ "@context": "https://schema.org", "@graph": graph }).replaceAll("<", "\\u003c");
}

function seoHead(page, equivalents, trail) {
  const es = page.lang === "es";
  const alt = es ? "transcriptoryt · Audio y video a texto" : "transcriptoryt · Audio and video to text";
  const canonical = site + page.path;
  const tags = [`<link rel="canonical" href="${canonical}">`];
  if (equivalents.length === 2) {
    for (const other of [...equivalents, { lang: "x-default", path: find(page.key, "es").path }]) {
      tags.push(`<link rel="alternate" hreflang="${other.lang}" href="${site}${other.path}">`);
    }
  }
  const og = { type: "website", site_name: "transcriptoryt", title: page.title, description: page.description,
    url: canonical, locale: es ? "es_LA" : "en_US", image, "image:width": "1200", "image:height": "630",
    "image:type": "image/png", "image:alt": alt };
  const twitter = { card: "summary_large_image", title: page.title, description: page.description, image, "image:alt": alt };
  for (const [key, value] of Object.entries(og)) tags.push(`<meta property="og:${key}" content="${escape(value)}">`);
  for (const [key, value] of Object.entries(twitter)) tags.push(`<meta name="twitter:${key}" content="${escape(value)}">`);
  tags.push(`<script type="application/ld+json">${schema(page, trail)}</script>`);
  return tags.join("\n  ");
}

async function render(page) {
  const es = page.lang === "es";
  const tool = tools.includes(page.key);
  const home = find("audio", page.lang).path;
  const equivalents = pages.filter(p => p.key === page.key);
  const trail = page.path === home || page.error ? [] : [
    { name: es ? "Inicio" : "Home", path: home }, { name: page.label, path: page.path },
  ];
  const navigation = keys => keys.map(key => anchor(find(key, page.lang) ?? find(key, "es"), page)).join("\n");
  const breadcrumb = trail.length ? `<nav class="breadcrumb" aria-label="${es ? "Ruta de navegación" : "Breadcrumb"}"><ol><li><a href="${home}">${trail[0].name}</a></li><li aria-current="page">${page.label}</li></ol></nav>` : "";
  let body;
  if (page.error) {
    body = '<h1>Página no encontrada</h1><p>La dirección que abriste no existe. Puedes volver a <a href="/">Audio a texto</a> o visitar la <a href="/en/" hreflang="en">portada en inglés</a>.</p>';
  } else {
    // {{limits}} and {{quota}} keep FAQ answers identical to the tool's own limit text.
    body = fill(await source(`./content/${page.lang}-${page.key}.html`),
      { limits: escape(limitText(page.lang)), quota: escape(quotaText[page.lang]) });
    if (tool) {
      const otherPages = tools.filter(key => key !== page.key);
      const related = `<h2>${es ? "Otras herramientas" : "Other tools"}</h2><ul>${otherPages.map(key =>
        `<li>${anchor(find(key, "es"), page)}</li>`).join("")}</ul>`;
      body = `<div class="hero"><h1>${escape(page.heading)}</h1><p>${escape(page.intro)}</p></div>\n${toolForm(page)}\n<article class="prose guide">${body}\n${related}</article>`;
    }
  }
  const switcher = equivalents.length === 2 ? equivalents.map(other => {
    const current = page === other ? ' aria-current="page"' : "";
    return `<a lang="${other.lang}" hreflang="${other.lang}" href="${other.path}" aria-label="${other.lang === "es" ? "Español" : "English"}"${current}>${other.lang.toUpperCase()}</a>`;
  }).join("") : '<a href="/en/" hreflang="en" aria-label="Inicio en inglés">EN · Inicio</a>';
  return fill(shell, {
    lang: page.lang, title: escape(page.title), description: escape(page.description), home,
    toolAttribute: tool ? ` data-tool="${page.key}"` : "",
    seoHead: page.error ? '<meta name="robots" content="noindex">' : seoHead(page, equivalents, trail),
    toolScripts: tool ? `<script src="${asset("tool-copy.js")}" defer></script>\n  <script src="${asset("app.js")}" defer></script>` : "",
    styleHref: asset("style.css"), transcriberHref: asset("transcriber.css"),
    skip: es ? "Saltar al contenido" : "Skip to content", navLabel: es ? "Navegación principal" : "Main navigation",
    primary: navigation([...tools, "about"]), footer: navigation([...tools, "about", "contact", "privacy", "terms"]),
    footerLabel: es ? "Pie de página" : "Footer", mainClass: tool ? "dashboard" : "prose",
    languages: `<div class="locale-switch" role="group" aria-label="${es ? "Idioma" : "Language"}">${switcher}</div>`,
    themeLabel: es ? "Cambiar tema" : "Toggle theme", dark: es ? "Usar tema oscuro" : "Use dark theme",
    light: es ? "Usar tema claro" : "Use light theme", breadcrumb, body,
  });
}

async function existing(file) {
  try { return (await readFile(file, "utf8")).replaceAll("\r\n", "\n"); }
  catch (error) { if (error.code === "ENOENT") return ""; throw error; }
}
const sitemap = join(root, "sitemap.xml");
const previous = new Map([...((await existing(sitemap)).matchAll(/<loc>([^<]+)<\/loc>\s*<lastmod>([^<]+)<\/lastmod>/g))].map(m => [m[1], m[2]]));
const entries = [];
for (const page of pages) {
  const file = join(root, page.path, "index.html");
  const html = (await render(page)).replaceAll("\r\n", "\n");
  const changed = await existing(file) !== html;
  if (changed) {
    await mkdir(dirname(file), { recursive: true });
    await writeFile(file, html);
  }
  const url = site + page.path;
  entries.push(`  <url><loc>${url}</loc><lastmod>${changed ? date : previous.get(url) ?? date}</lastmod></url>`);
}
await writeFile(sitemap, `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${entries.join("\n")}\n</urlset>\n`);
await writeFile(join(root, "404.html"), await render({ error: true, key: "404", lang: "es", title: "Página no encontrada | transcriptoryt", description: "Vuelve a las herramientas de transcripción." }));

