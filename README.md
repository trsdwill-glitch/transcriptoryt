# transcriptoryt: transcriptor gratuito de audio y video

**En producción en [transcriptoryt.com](https://transcriptoryt.com/)**: audio, video y enlaces de YouTube a texto, gratis y sin registro.

[YouTube a texto](https://transcriptoryt.com/youtube-a-texto/) · [Video a texto](https://transcriptoryt.com/video-a-texto/) · [English](https://transcriptoryt.com/en/) · Licencia [AGPL-3.0](LICENSE)

La aplicación integra FastAPI, una interfaz web sin compilación y dos modos:

- **Servidor:** archivos de audio/video y enlaces compatibles, mediante Groq (Whisper large-v3-turbo) o Cloudflare Workers AI. Cuotas con SQLite, presupuesto estimado y cola en memoria.
- **En este dispositivo:** un archivo de audio compatible con el navegador. Los móviles y equipos con poca memoria usan Whisper Tiny multilingüe cuantizado y WebAssembly de un solo hilo, con un límite local de 10 minutos y 24 MiB. El escritorio normalmente usa Whisper Base con WebGPU y respaldo WASM. No sube el audio ni consume cuota; descarga el modelo desde Hugging Face al necesitarlo. Incluye progreso por tramos y cancelación.

Ambos exportan TXT, Markdown, DOCX, SRT y VTT.

## Actualizar la web pública

En Windows, abre `Actualizar-web.cmd` desde la carpeta del proyecto. Publica la interfaz y el motor de navegador en la instalación activa de LWS, verifica los archivos por HTTPS y conserva una copia de la interfaz anterior en el VPS. La contraseña de SSH se introduce en la ventana del actualizador.

## Paquete y guía

- **Guía paso a paso:** [deploy/DESPLIEGUE.md](deploy/DESPLIEGUE.md), con Ubuntu, Docker, HTTPS, configuración, operación, celular, Groq, YouTube y Search Console.
- **Resultado y evidencia:** [INFORME.md](INFORME.md).
- **Diferencias de alcance:** [docs/DIFERENCIAS_ETAPA.md](docs/DIFERENCIAS_ETAPA.md).
- **Datos por completar antes de publicar:** [docs/REVISION_PUBLICACION.md](docs/REVISION_PUBLICACION.md).
- **Condiciones y datos de proveedores:** [docs/PROVEEDORES.md](docs/PROVEEDORES.md).

Generar de nuevo el archivo de entrega, sin `.env`, datos, cookies ni cachés:

```powershell
.\.venv\Scripts\python.exe scripts/package_release.py
```

Produce `dist/transcriptor-lws.zip`, su SHA-256 y un manifiesto `SHA256SUMS` dentro del ZIP. Incluye el runtime de navegador aunque esté ignorado por Git. En una copia recién obtenida por Git, prepararlo antes con `node scripts/setup-browser-runtime.mjs` (descargas de versiones y hashes fijados).

El ZIP conserva marcadores **SITE_NAME, DOMAIN, CONTACT_EMAIL, OPERATOR_NAME, OPERATOR_ADDRESS y LAST_UPDATED**. No está destinado a publicarse con ellos. En una copia extraída del paquete, completar los datos con `scripts/configure_site.py --apply`, revisarlos y comprobar con `--check`; la guía contiene el comando completo. Los secretos de producción se configuran aparte en `deploy/.env`, a partir de `deploy/.env.example`.

## Ejecutar en Windows

Requiere Python, Node para preparar el runtime, ffmpeg y ffprobe en PATH. El destino Docker usa Python 3.12; esta computadora dispone de Python 3.13.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
node scripts/setup-browser-runtime.mjs
Copy-Item .env.example .env
.\.venv\Scripts\python.exe -c "import secrets; print(secrets.token_hex(32)); print(secrets.token_hex(32))"
```

Poner los dos valores distintos en `SECRET_KEY` y `HASH_SALT`. Para probar el servidor, configurar también `GROQ_API_KEY` en `.env`; nunca compartirlo. Para el modo local no se necesita una clave.

```powershell
.\.venv\Scripts\python.exe -m uvicorn app.main:app --env-file .env --host 127.0.0.1 --port 8000 --no-access-log
```

Abrir `http://localhost:8000/` o `http://localhost:8000/?mode=local`. La primera descarga del modelo requiere internet. Para comprobar WASM por separado, usar `?mode=local&engine=wasm`. Los archivos locales admitidos dependen de la decodificación de audio del navegador.

**Usar una sola instancia y un solo worker.** Reiniciar cancela los trabajos y no recupera descargas anteriores. SQLite persiste las cuotas. No exponer Uvicorn directamente en internet; `TRUSTED_PROXY=true` solo se usa con el proxy preparado.

## Pruebas

```powershell
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -X utf8 -m pytest -q -W error -rs --tb=short
node --test tests/test_local_formats.mjs
.\.venv\Scripts\python.exe scripts/smoke_server.py --base-url http://127.0.0.1:8000
```

La suite procesa audio con ffmpeg real y simula el proveedor; no consume una clave. Algunos tests de procesos requieren POSIX. La guía incluye la imagen Docker `tests` para ejecutar esos casos en Linux, y un Compose local sin dominio ni credenciales.

Con la aplicación arrancada con una clave real, este comando usa una muestra pública de voz de 10,435 s, verifica su hash, la sube al servidor, espera el resultado, descarga/abre DOCX y comprueba consumo de cuota:

```powershell
.\.venv\Scripts\python.exe scripts/smoke_server.py --base-url http://127.0.0.1:8000 --public-audio
```

Consume uso real del proveedor. No imprime la clave ni el texto. También admite `--audio-file ruta` para un audio propio pequeño. La disponibilidad del modelo, las cuotas y la facturación reales se comprueban en la cuenta del proveedor.

## Configuración y límites

`.env.example`, `deploy/.env.example` y los valores por defecto de `app/config.py` usan los mismos límites: **120 minutos de audio por usuario en 24 horas móviles, 120 minutos y 500 MiB por archivo**, cinco elementos por trabajo, una hora de resultados y presupuesto estimado de USD 0,50/día UTC. No existe una cuota total de bytes por usuario: los 500 MiB se aplican a cada archivo. Los textos públicos se generan con esos mismos números (`scripts/build-tool-pages.mjs` lee `deploy/.env.example`) y `tests/test_pages.py` falla si divergen. Revisar disco, límites reales del proveedor y coste antes de abrir tráfico.

La reserva de enlaces directos de duración desconocida usa `MAX_FILE_MIN` antes de descargar y se ajusta después. Un lote puede rechazarse por esa reserva aun si su duración final sería menor. El presupuesto local no es una consulta a facturación real ni aplica todavía la reducción al 70% prevista por la especificación original.

Las notas de planificación originales (`docs/referencias/`) son históricas y no se publican en el repositorio. La petición actual y la especificación final tienen prioridad; no se afirma que todas las fases de la especificación estén implementadas.

## Licencia

Copyright © 2026 Thiago Raul Solaeche Duarte.

Este programa es software libre: puedes redistribuirlo y modificarlo bajo los términos de la [GNU Affero General Public License v3.0](LICENSE). Si ofreces una versión modificada como servicio en red, la AGPL te obliga a poner su código fuente a disposición de sus usuarios.

Las bibliotecas de terceros (Transformers.js, ONNX Runtime, la fuente Inter) conservan sus propias licencias, incluidas en `static/assets/vendor/` y `static/assets/fonts/`.
