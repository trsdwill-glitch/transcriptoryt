# Proveedores y comprobaciones pendientes

Revisión documental: 22 de noviembre de 2026. No se accedió a una cuenta ni se aceptaron contratos. Revalidar las condiciones vigentes antes de publicar.

## Groq

El contrato aplicable a la API es el [Groq Services Agreement](https://console.groq.com/docs/legal/services-agreement), revisado con fecha de modificación 22 de junio de 2026. Su sección 3.1 permite integrar el servicio en una aplicación y ofrecerlo a usuarios finales; 3.2 prohíbe revender o arrendar la cuenta. La sección 5.1 contempla servicios gratuitos sujetos a límites o tiempo. De esto se infiere que no hay una prohibición general de integrar la API en una herramienta con publicidad por usar el nivel gratuito; **no es una garantía de capacidad gratuita permanente ni de aprobación del caso concreto**. También aplican la AUP y las condiciones del modelo. Verificar el plan y cualquier acuerdo particular de la cuenta.

No confundirlo con los [Terms of Use del sitio web de Groq](https://groq.com/terms-of-use), que excluyen expresamente los servicios cloud de su alcance.

La [documentación de voz](https://console.groq.com/docs/speech-to-text) consultada publica USD 0,04/hora para `whisper-large-v3-turbo`, mínimo facturable de diez segundos por petición y límites de archivo dependientes del plan. El backend convierte a MP3 mono y divide en tramos de 600 s; los archivos originales de 500 MiB no se envían como una sola petición. El presupuesto local es una estimación por duración, puede diferir por mínimos, reintentos y fallos, y no reemplaza el panel de facturación.

La [política de datos](https://console.groq.com/docs/your-data) distingue metadatos de uso y contenido, contempla conservación excepcional de entradas/salidas para fiabilidad o abuso y ofrece controles ZDR. Revisar esos controles en la cuenta; no afirmar que el proveedor borra todo al borrar nuestros temporales. La página pública de privacidad describe esta diferencia.

No apareció `.env` ni una variable `GROQ_API_KEY` configurada en esta sesión. No se llamó a Groq. La prueba exacta está en README y en la sección 9 de la guía. La muestra pública fijada procede de [Narsil/asr_dummy](https://huggingface.co/datasets/Narsil/asr_dummy/tree/8d141c84e3f84c54cd7bbaa851d24edd0f559734): `1.flac`, 183.318 bytes, duración medida con ffprobe 10,435 s, SHA-256 `30885601173f96b0d8ddd020dc959b055c6c1582b85a33e3fcab8c4b08ed94c2`. Se descargó y midió realmente; no se transcribió con API.

## YouTube y runtime

La [guía oficial EJS](https://github.com/yt-dlp/yt-dlp/wiki/EJS) requiere un motor JavaScript compatible y los scripts EJS. La imagen incluye Node activado explícitamente y `yt-dlp[default]`, que instala EJS compatible. No se descargan componentes remotos durante los trabajos. El pin de yt-dlp queda en `deploy/ytdlp-version.txt`; el procedimiento reconstruye y prueba la imagen en vez de modificar el contenedor en marcha. Guardar imagen, pin y lista de dependencias resueltas para repetir exactamente una instalación.

Los bloqueos de una IP de centro de datos solo se comprueban desde esa IP. `scripts/test_ytdlp.py` descarga e inspecciona mediante la misma ingesta de la app, sin consultar subtítulos ni llamar a Groq. El informe conserva hashes de las URLs, estados y duraciones. Completar una lista privada de 30 videos públicos distintos, vigentes y autorizados antes de ejecutarlo. Un sondeo de metadatos exitoso por sí solo no acredita la descarga.

## Navegador y cambios futuros

El audio local no se envía al proveedor de transcripción. La descarga de los pesos contacta Hugging Face; ver su [política de privacidad](https://huggingface.co/privacy). La preparación del runtime utiliza versiones y hashes fijados por `scripts/setup-browser-runtime.mjs`, conservando licencias. No se cambió el modelo Base ni se reabrió la optimización.

Las páginas preparadas describen Groq por defecto. Si se selecciona Cloudflare Workers AI, se debe revisar y adaptar la información del proveedor antes de publicar. Anuncios, analítica, muro publicitario y Turnstile siguen sin activarse: requieren cuenta/configuración, revisión de avisos y pruebas propias.
