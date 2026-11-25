# Informe de preparación antes de comprar dominio y VPS

Fecha: 22 de noviembre de 2026. No se compró, publicó ni modificó un servicio externo. El informe anterior se conserva en `docs/referencias/INFORME_INTEGRACION_ANTERIOR.md` como evidencia histórica.

## Qué quedó preparado

- `deploy/Dockerfile`: Python 3.12, ffmpeg/ffprobe, Node 24.22.3 y bibliotecas necesarias, yt-dlp fijado con EJS compatible, usuario no root, healthcheck e imagen de pruebas separada.
- Compose de producción con Caddy 2.11.9 para HTTPS automático, un proceso Uvicorn, límites de recursos, raíz de solo lectura, volúmenes persistentes y puerto de la app cerrado al host. Caddy reemplaza la IP reenviada: no permite que el visitante elija la IP de su cuota mediante XFF. Inicio con DNS directo; Cloudflare proxificado exige otra configuración.
- Producción propuesta: 30 minutos por usuario y elemento, 500 MiB por archivo, hasta cinco elementos, resultados por una hora y presupuesto estimado de USD 0,50/día UTC. Los valores de desarrollo heredados se conservan aparte y se explican.
- Perfil Docker local sin dominio, imagen de pruebas Linux, smoke HTTP, consulta del presupuesto y actualización de yt-dlp mediante pin exacto, reconstrucción, pruebas y recuperación de la imagen previa si falla el nuevo arranque. Timer diario de comprobación, sin reinicio automático.
- Privacidad, términos, contacto y acerca de, con contenido sobre el producto real; títulos/descripciones, canónicas, navegación, robots y sitemap de las cinco páginas existentes. `ads.txt` sin identificador ficticio. Configurador de los seis marcadores y comprobación antes de publicar.
- Prueba YouTube equivalente al script que faltaba: usa la ingesta real, descarga e inspecciona medios, informa por códigos/hashes sin URLs ni texto y no llama a Groq. Plantilla para completar 30 enlaces vigentes y autorizados, más comando dentro del contenedor desde la IP del VPS.
- ZIP `dist/transcriptor-lws.zip` generado con runtime del navegador y licencias, manifiesto SHA-256 interno y hash del ZIP aparte. Excluye `.env`, datos, cookies, cachés, herramientas temporales y corpus privado. `scripts/package_release.py` permite regenerarlo de forma determinista para los mismos archivos.
- Guía `deploy/DESPLIEGUE.md`: acceso SSH, Ubuntu, firewall, Docker, configuración, DNS/HTTPS, pruebas, actualizaciones, copias, celular y Search Console. Diferencias con los documentos anteriores en `docs/DIFERENCIAS_ETAPA.md`; revisión de datos reales en `docs/REVISION_PUBLICACION.md` y proveedores en `docs/PROVEEDORES.md`.

El motor local Whisper Base multilingüe se conserva. Los tiempos de 25/26 s para 33 s y nueve minutos para 38 min 46 s son evidencia aportada y aceptada por el propietario, no mediciones repetidas aquí. No se reabrió su optimización.

## Pruebas del backend y herramientas

Entorno ejecutado: Windows, Python 3.13.15 y ffmpeg/ffprobe disponibles. El destino Linux usa Python 3.12 y todavía requiere construcción real.

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -W error -rs --tb=short
node --test tests/test_local_formats.mjs
```

Resultados reales: **Ruff aprobado; 366 pruebas Python aprobadas, 50 omitidas y cero fallos en 37,58 s; seis pruebas Node aprobadas**. También se comprobó sintaxis JavaScript de la interfaz, worker y preparación del runtime. Las 50 omisiones siguen siendo pruebas de ejecutables falsos que requieren POSIX. El aislamiento de herramientas bloqueó incluso la creación de un bucle asyncio mínimo; la ejecución fuera de ese aislamiento completó la suite. Las ejecuciones bloqueadas se interrumpieron y no se cuentan como resultados.

La suite de integración mantiene reales FastAPI, SQLite, ingesta, ffprobe, ffmpeg, creación de tramos, proveedor Groq y renderizadores, simulando únicamente su respuesta HTTP. Cubre subida → transcripción → descarga en los cinco formatos, marcas, combinados/ZIP, consumo y rechazo de cuota, cancelación/reembolso y limpieza/caducidad. **No acredita una llamada real a Groq.**

Se añadieron 19 casos para navegación/SEO y herramientas de entrega: canónicas/sitemap, enlaces sin 404, FAQ coherente, escape HTML/JSON/JavaScript de marcadores, rechazo de valores inseguros antes de escribir, exclusión de secretos, manifiesto/permisos/reproducibilidad, comprobación de DOCX con cuerpo y cuota, errores sin secretos e informe YouTube sin URLs. Los tests del script YouTube simulan sus funciones de ingesta; no acreditan descargas reales.

## Arranque, portada y cuota

Se arrancó Uvicorn real en `127.0.0.1:18765`, con un directorio de prueba separado de los datos de uso. La comprobación HTTP `scripts/smoke_server.py` aprobó:

- Portada y las cuatro páginas informativas.
- `robots.txt`, `sitemap.xml`, Transformers.js y las variantes WASM.
- `/api/health`, los cinco formatos de `/api/config`, `/api/quota`, cookie de identidad y `Cache-Control: no-store`.

El navegador abrió realmente la portada, cambió el idioma y navegó a privacidad, en una ventana estrecha; se inspeccionó su presentación. Esto no es una prueba de transcripción móvil ni de HTTPS. La cuota del entorno de desarrollo mostraba 120 minutos, coherente con su configuración. La configuración de producción prepara 30.

## Configuración de despliegue y Linux

Se analizaron ambos Compose con un parser YAML y se comprobaron sus invariantes. Los tres scripts de shell pasaron `sh -n` con el shell de Git. Se descargó Caddy oficial para Windows, se verificó su SHA-512 publicado y se ejecutó `adapt --validate` con el Caddyfile preparado: aprobado sin iniciar un servidor ni solicitar certificados. Ver comandos, alcance y hash en `deploy/VERIFICACION_LOCAL.md`.

**No se construyó ni arrancó Docker/Linux.** Docker no está instalado y WSL informa que no está instalado. También se consultó VirtualBox: el programa existe, pero no tiene máquinas registradas ni en ejecución. No se instaló software de sistema ni se creó una VM. La guía deja los comandos para ejecutar la imagen de pruebas POSIX y el Compose local cuando se disponga de Docker; esa comprobación puede hacerse antes de comprar si se habilita Linux por separado.

## Groq real

No se encontró `.env` local ni `GROQ_API_KEY` configurada en el entorno. No se mostró ni utilizó ninguna clave, y **no se realizó una transcripción real con Groq**.

Sí se descargó una muestra pública fijada por revisión de Hugging Face, se verificó su contenido y se midió con ffprobe: 183.318 bytes, 10,435 s. Su URL/revisión y SHA-256 están fijados en el script y documentados en `docs/PROVEEDORES.md`.

Con una clave en `.env`, arrancar como indica README y ejecutar, sin pasar la clave en la línea de comandos:

```powershell
.\.venv\Scripts\python.exe scripts/smoke_server.py --base-url http://127.0.0.1:8000 --public-audio
```

El comando usa la API real de la app, descarga/abre un DOCX con cuerpo y exige aumento de uso de cuota. No imprime transcripciones. También existe el comando HTTPS dentro del contenedor en la guía. Esta prueba puede hacerse antes de comprar cuando haya clave.

## Datos y comprobaciones que faltan

Antes de publicar: completar nombre, dominio, correo atendido, responsable, dirección y fecha; revisar las condiciones legales con jurisdicción/proveedores reales; crear secretos privados; seleccionar el corpus de 30 videos autorizados y comprobar la cuenta/límites/retención del proveedor. Las páginas son borradores preparados, no una certificación legal.

Tras disponer del dominio/VPS y publicar: DNS y certificado/renovación, arranque y límites en Linux real si no se probaron antes, cuota detrás del proxy, alcance externo de puertos, descarga/transcripción real desde esa red, YouTube desde la IP LWS, carga/disco, teléfono por HTTPS y otras redes, y envío/lectura de sitemap en Search Console. Indexación y aprobación AdSense dependen de terceros.

La arquitectura actual sigue usando API externa, SQLite y cola en memoria. No se presentan como terminadas GPU propia/Redis, reducción de cuota al 70%, reputación VPN/Turnstile, anuncios/muro, analítica/panel/alertas, páginas por formato ni localización SEO. La guía distingue estas diferencias para que la compra no se apoye en funciones o capacidad no demostradas.
