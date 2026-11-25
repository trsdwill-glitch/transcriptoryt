# Plan de autoridad SEO: transcriptoryt.com

Fecha: 25 de noviembre de 2026.

## Diagnóstico

**On-site (bien resuelto):** las 12 URL del sitemap responden 200, http y www redirigen con 301 a `https://transcriptoryt.com/`, robots.txt solo bloquea `/api/`, no hay `X-Robots-Tag`, cada página tiene su canónica, hreflang es/en, OG/Twitter y schema (WebSite, Organization, WebApplication, BreadcrumbList). Las páginas de herramienta tienen unas 900 palabras.

**Off-site (el problema real):** el dominio es nuevo y no tiene enlaces entrantes ni perfiles de marca. Hoy Google no tiene ninguna señal externa de que el sitio exista ni de que sea fiable. Ahí está el trabajo de autoridad.

**Schema de marca:** `Organization` usa `/assets/logo-512.png` y su lista `sameAs` (en `scripts/build-tool-pages.mjs`) ya incluye YouTube, X `@transcriptoryt_` y Bluesky `transcriptoryt.bsky.social` (25 nov). Cada perfil nuevo que se cree hay que agregarlo ahí, regenerar las páginas con `node scripts/build-tool-pages.mjs` y publicar con `Actualizar-web.cmd`.

## Principios

- **Sí:** enlaces de sitios reales y temáticos (educación, periodismo, podcasting, productividad, accesibilidad), directorios curados y comunidades donde la herramienta resuelva una duda concreta.
- **No:** comprar enlaces, PBN, paquetes de «300 directorios», intercambios masivos ni comentarios spam. Con un dominio nuevo, eso puede provocar una acción manual o hacer que los enlaces se ignoren.
- Ritmo natural: unos pocos enlaces buenos por semana, no 100 en un día.

## Fase 1: entidad de marca (semana 1)

Crear perfiles con el mismo nombre, logo y descripción, todos enlazando a la web. Fijan la entidad y alimentan `sameAs`.

| Perfil | Por qué |
|---|---|
| Canal de YouTube «transcriptoryt» | Encaja con el producto: demos de 30-60 s («cómo pasar un video de YouTube a texto») con enlace en la descripción. |
| X / Bluesky / Threads | Perfiles de marca baratos de mantener. |
| LinkedIn (página de empresa o perfil personal del autor) | Apoya la señal de E-E-A-T de «quién está detrás». |
| GitHub (si se publica algo del proyecto, aunque sea un ejemplo o el formateador SRT/VTT) | Es un enlace de dominio fuerte y atrae a público técnico. |

## Fase 2: directorios curados (semanas 1-3)

Enviar a pocos directorios, pero buenos. Comprobar en cada uno si el listado es gratis y si el enlace es `dofollow`, porque cambian a menudo.

- **AlternativeTo:** registrarlo como alternativa a Otter, HappyScribe, TurboScribe y Transkriptor. Es la fuente de enlaces y tráfico más relevante para este nicho.
- **SaaSHub**, **Product Hunt** (preparar el lanzamiento: un martes-jueves, con imágenes y primer comentario), **BetaList**, **Uneed**, **Fazier**, **Indie Hackers** (producto + post de construcción).
- **Directorios de IA:** TopAI.tools, Turbo0, Futurepedia y There's An AI For That. Revisar las condiciones; TAAFT es de pago.
- **En español:** directorios de recursos TIC para docentes (p. ej. blogs tipo «herramientas TIC», guías de bibliotecas universitarias sobre transcripción). Son los enlaces de mayor calidad temática.

## Fase 3: menciones editoriales (semanas 2-8)

1. **Listados «mejores transcriptores gratis»:** buscar artículos en español que comparen herramientas gratuitas (blogs de productividad, educación, podcasting y periodismo; no los de competidores como HappyScribe o Sonix, que no van a enlazarte). Escribir a cada autor con la plantilla de abajo.
2. **Bibliotecas y centros de apoyo universitarios:** muchas publican guías de «herramientas para transcribir entrevistas» para tesistas. Que el modo «En este dispositivo» no suba el audio es un argumento fuerte para investigación con datos sensibles.
3. **Comunidades:** responder con ayuda real en Reddit (r/podcasting, r/Teachers, r/estudiantes, r/SideProject), foros de podcasters y grupos de docentes. Divulgar siempre que eres el autor.
4. **Activo enlazable (opcional, gran retorno):** una guía o estudio propio, p. ej. «Precisión de Whisper en español por acento: 30 audios probados» o «Cómo transcribir entrevistas para tu tesis sin subir el audio». Es lo que la gente cita y enlaza de forma natural.

### Plantilla de contacto

> Asunto: Una herramienta gratuita para tu artículo sobre transcripción
>
> Hola, [nombre]:
>
> Leí tu artículo «[título]». Lo encontré muy útil, sobre todo [detalle concreto].
>
> Hice transcriptoryt.com, un transcriptor gratuito y sin registro. Transcribe archivos y enlaces de YouTube, y tiene un modo que procesa el audio en el propio navegador, sin subirlo, pensado para entrevistas o material sensible. Exporta TXT, Word, SRT y VTT.
>
> Si te parece que encaja en tu lista, me encantaría que lo consideraras. Y si tienes alguna crítica, también me sirve.
>
> Gracias, [nombre]

Personalizar siempre la primera línea. No enviar en masa.

## Kit de textos para perfiles y directorios

**Nombre:** transcriptoryt
**Categoría:** Productividad / Audio / Herramientas de IA
**Precio:** gratis, sin cuenta (límite diario de uso)

**Lema (ES):** Audio y video a texto, gratis y sin registro.
**Tagline (EN):** Free audio & video to text. No sign-up.

**Corta (ES, ~150 caracteres):** Transcribe audio, video y enlaces de YouTube a texto gratis y sin registro. Descarga TXT, Word, SRT o VTT, o transcribe sin subir el audio.

**Short (EN):** Transcribe audio, video and YouTube links to text for free, no account. Download TXT, Word, SRT or VTT, or transcribe on-device without uploading.

**Larga (ES):** transcriptoryt convierte audio y video a texto sin crear una cuenta. Tiene dos modos: «Servidor», que acepta archivos de hasta 500 MiB y enlaces como YouTube, y «En este dispositivo», que ejecuta Whisper en el navegador para que el audio no salga de tu equipo. Exporta TXT, Markdown, DOCX y subtítulos SRT/VTT con marcas de tiempo. Pensado para estudiantes, periodistas, podcasters y docentes.

**Long (EN):** transcriptoryt turns audio and video into text without an account. Server mode handles files up to 500 MiB and links such as YouTube; on-device mode runs Whisper in your browser so the audio never leaves your computer. Export TXT, Markdown, DOCX and SRT/VTT subtitles with timestamps. Built for students, journalists, podcasters and teachers.

**Imágenes necesarias:** logo PNG 512×512, `social-card.png` (ya existe, 1200×630), 3-4 capturas (portada, transcripción en curso, descarga, modo móvil) y un GIF de 15 s.

## Medición

- Search Console → **Enlaces → Sitios externos con más enlaces**: revisar cada semana.
- Search Console → **Rendimiento**: impresiones de «transcribir audio a texto», «youtube a texto» y «video a texto».
- Llevar una hoja con: fecha, sitio, URL, estado (enviado/publicado/rechazado) y si el enlace es dofollow.
- Expectativa realista: las primeras señales tardan de 4 a 8 semanas; las palabras clave principales son muy competidas (VEED, HappyScribe, TurboScribe), así que conviene empezar por long-tail («transcribir entrevista sin subir audio», «youtube a srt gratis»).
