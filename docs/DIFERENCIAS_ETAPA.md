# Diferencias y alcance de la preparación local

Revisión: 22 de noviembre de 2026. La solicitud de este chat y la especificación final del 16 de noviembre tienen prioridad sobre los documentos históricos de `docs/referencias/`. Estos últimos describen un contrato anterior y una guía de trabajo; no acreditan funcionalidades ni pruebas ejecutadas.

Esta entrega prepara el producto existente para instalarlo en Ubuntu con Docker Compose. La compra del dominio y VPS, la publicación y la aprobación de servicios externos quedan para después. Una guía o un archivo de configuración preparado no equivale a un despliegue comprobado.

## Decisiones para esta etapa

| Tema | Especificación final / referencia anterior | Producto que se prepara |
|---|---|---|
| Servidor | Especificación final: GPU propia o alquilada, faster-whisper, Redis. Contrato anterior: API externa, SQLite. | Se conserva FastAPI con Groq por defecto, Cloudflare Workers AI como alternativa, cuotas SQLite y cola en memoria. No requiere GPU en el VPS. |
| Frontend | Especificación final: Next.js, TypeScript y Tailwind. Contrato: HTML/CSS/JS. | Se conserva la interfaz integrada en HTML/CSS/JS y las páginas estáticas. No se introduce una migración de framework para empaquetar. |
| Motor local | Especificación: modelos base y small. | Whisper Base multilingüe con WebGPU y respaldo WASM. El usuario ya validó velocidad y exportaciones; no se cambia ni se reabre su optimización. |
| Cuota y tamaño | Especificación: 30 minutos por usuario y archivo; propuesta inicial de subida de 500 MB. Contrato y valores de desarrollo: 120 minutos y 100 MiB. | El perfil de producción prepara 30 minutos, máximo 30 por elemento y 500 MiB por archivo. El entorno de desarrollo conserva 120 minutos y 100 MiB. Se usa MiB explícitamente: 500 MiB son 524.288.000 bytes. |
| Presupuesto | Especificación: reducción de cuota al 70 % y pausa/cola al 100 %. | El código reserva costo estimado y rechaza al exceder el presupuesto configurado. No implementa la reducción al 70 %. El saldo local no es la facturación de Groq. |
| Elección de modo | Especificación: elección automática por dispositivo/aceleración y cambio manual. | Selector manual; el modo predeterminado actual es Servidor. Sin cuota puede usarse el modo local. No se promete selección automática. |
| Entrada local | Especificación amplia de audio/video, enlaces y lotes. | El modo local acepta un archivo que el decodificador del navegador pueda leer. Los enlaces y lotes son del modo servidor; no existe descarga de enlaces en el navegador ni conversión universal de video local. |
| Contenedores de medios | Especificación: cualquier formato que ffmpeg pueda leer. | El servidor permite una lista explícita de contenedores habituales para evitar entradas como listas de reproducción que abran red o archivos arbitrarios. No se promete todo formato existente de ffmpeg. |
| Trabajos y reintentos | Especificación: cola persistente y un reintento de trabajo. | Cola y resultados accesibles mediante un único proceso Uvicorn. Las peticiones al proveedor tienen hasta tres reintentos; no se reintenta todo el trabajo una vez. Un reinicio pierde el seguimiento de trabajos y las descargas anteriores. |
| Seguridad de peticiones | Especificación: límite en todos los endpoints y reto anti-bot. | Límite por IP sobre la API, cuota por cookie/huella/IP y protección SSRF de enlaces directos. No hay Turnstile ni clasificación/reputación VPN. |
| Anuncios y medición | Especificación final: AdSense, muro de bloqueadores, consentimiento, GA4, Sentry, panel y alertas. Contrato anterior: espacios reservados. | Se mantienen espacios sin anuncios activos. No hay muro, rastreadores, panel ni alertas operativas. Incorporarlos exige configuración y revisión posterior. |
| Idiomas y SEO | Especificación: inglés inicial, páginas por caso de uso y localización SEO posterior. | La herramienta ofrece inglés/español; las cuatro páginas informativas públicas se preparan en inglés. SEO básico para las cinco páginas existentes; sin rutas `/es/` o `/pt/`, hreflang ni páginas casi duplicadas inventadas. |

El modo local no consume cuota de audio del servidor. Los tiempos aportados por el usuario —25/26 segundos para 33 segundos de audio y nueve minutos para 38 min 46 s— se registran como validación del usuario, no como mediciones repetidas en esta entrega. La compatibilidad completa de Chrome, Edge, Safari y Firefox en escritorio y móvil sigue siendo una matriz por comprobar con dispositivos reales.

## Cambios respecto de la guía histórica de despliegue

- `scripts/test_ytdlp.py` se mencionaba como existente, pero faltaba. Se prepara un equivalente que usa la ingesta real, mide sondeo y descarga, guarda informes sin URLs completas y no llama a Groq. La viabilidad de YouTube se decide con enlaces permitidos desde la IP real del VPS; una prueba desde Windows no sustituye esa evidencia.
- yt-dlp requiere su entorno JavaScript y componentes EJS compatibles, además de ffmpeg. El paquete y el procedimiento de actualización fijan versiones para poder reconstruir y volver a una versión conocida. Tener el ejecutable instalado no prueba que YouTube acepte la IP.
- Los archivos de ejecución del modo local se generan con paquetes y hashes fijados y deben estar dentro del paquete aunque `static/assets/vendor/` esté ignorado por Git. Los pesos del modelo se descargan de Hugging Face bajo demanda y pueden permanecer en caché del navegador; no se incluyen todos los pesos en el ZIP.
- El límite del proxy debe admitir la suma máxima de un lote y el margen multipart, además de respetar el límite individual de la aplicación. Los límites de subida de un CDN externo pueden ser menores; el inicio propuesto usa DNS directo al VPS.
- `TRUSTED_PROXY=true` sólo es correcto con un proxy controlado que sobrescriba las cabeceras de IP. La aplicación no debe publicar su puerto al exterior. Añadir Cloudflare delante requiere revisar la cadena de confianza; no basta con activar su proxy.
- Respaldar solamente `.env`, como sugería la guía anterior, pierde las cuotas y el presupuesto SQLite. La guía nueva distingue configuración y contadores persistentes de los audios/resultados temporales. No se deben crear copias ordinarias de transcripciones sin una política de retención expresa.
- Las páginas legales tienen marcadores de identidad, dominio, correo y fecha. Describen el producto actual; no certifican cumplimiento ni contienen datos comerciales inventados. Ver `REVISION_PUBLICACION.md` para las revisiones dependientes del operador, la cuenta del proveedor y el alojamiento reales.

## Límites de privacidad y operación que deben seguir visibles

Los logs de aplicación omiten audio, texto, URLs completas y claves. Esa decisión no configura los registros de LWS, el sistema operativo, el proveedor de inferencia ni futuros servicios externos. La IP se procesa al recibir conexiones; la cuota guarda hashes con sal, pero el límite de peticiones conserva IP en memoria hasta una hora.

La cookie firmada dura un año y se renueva al responder. Los registros de cuota/reservas se eliminan después de 25 horas mediante un barrido periódico. Los metadatos de trabajo, incluidos fuente y título, pueden permanecer en RAM unas 25 horas. El resultado deja de ser descargable al vencer `RESULT_TTL_S`; su borrado físico sin consultas puede demorarse hasta el siguiente barrido de cinco minutos. Si el servicio está apagado, la limpieza se retoma al arrancar.

El servidor borra temporales al terminar, fallar o cancelar y limpia temporales huérfanos al arrancar. Los límites de memoria y CPU del contenedor no sustituyen vigilancia del espacio de disco. Se debe comprobar capacidad y comportamiento bajo carga antes de ampliar el tráfico.

El paquete se prepara para un solo proceso de aplicación y concurrencia limitada. No acredita capacidad para un millón de visitas al mes, Core Web Vitals reales, continuidad durante reinicios ni escalado horizontal. Las cuotas y el presupuesto estimado limitan uso; no garantizan que la factura del proveedor sea idéntica a las reservas locales, especialmente ante reintentos, fallos o cancelaciones.

## Evidencia que depende del entorno

En esta computadora se encontró Windows y Python 3.13; no se encontró Docker y WSL informa que no está instalado. Por tanto, los archivos Linux preparados y las comprobaciones estáticas no autorizan a afirmar que la imagen arrancó o que pasaron las pruebas POSIX con Python 3.12. La guía deja los comandos de construcción, pruebas y smoke para ejecutarlos cuando exista Linux/Docker.

El informe final separa las pruebas Windows realmente ejecutadas, cualquier prueba real de Groq si hubiera clave local, y las verificaciones pendientes. HTTPS público, certificado, DNS, prueba móvil desde otra red, YouTube desde la IP LWS y envío del sitemap a Search Console sólo podrán completarse al disponer del dominio/VPS y publicarlos. La indexación y aprobación AdSense son decisiones externas, no resultados prometidos por el paquete.
