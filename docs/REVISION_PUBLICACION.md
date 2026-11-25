# Revisión de contenido antes de publicar

Preparado el 22 de noviembre de 2026. Las páginas describen el producto actual. Son borradores operativos con datos del responsable pendientes; no acreditan cumplimiento legal ni aprobación de AdSense.

## Datos que faltan

Reemplazar en la copia de publicación todos estos marcadores, incluidos HTML, JavaScript, robots y sitemap. El script de preparación de la versión pública debe detectar cualquier marcador restante.

| Marcador | Dato real que corresponde |
|---|---|
| `SITE_NAME` | Nombre público, igual en portada, títulos y datos estructurados. |
| `DOMAIN` | Dominio canónico sin protocolo, ruta ni barra final. Elegir una sola variante, por ejemplo sin `www`, y redirigir las demás. |
| `CONTACT_EMAIL` | Correo que el responsable pueda recibir y responder. Probar el enlace `mailto:` y la recepción. |
| `OPERATOR_NAME` | Nombre legal de la persona o entidad responsable. |
| `OPERATOR_ADDRESS` | Dirección de contacto válida para las obligaciones aplicables; decidir con los datos y jurisdicción reales. |
| `LAST_UPDATED` | Fecha real en que se revisen y aprueben los textos para su publicación. |

No inventar identidad comercial, dirección, correo, jurisdicción, representante o registro de empresa. No publicar los marcadores como información al usuario. Las páginas públicas están en inglés; el selector español de la herramienta no crea versiones SEO `/es/` ni `hreflang`.

## Privacidad y términos

Revisar con los datos reales del operador, el alojamiento y las regiones atendidas:

- Base jurídica para procesar audio, identificadores de cuota y consultas de soporte; evaluar el tratamiento de cookies y huella ligera según las normas aplicables. No se ha declarado una exención universal de consentimiento.
- Información obligatoria del responsable, contactos para derechos, autoridad competente y plazos/proceso de respuesta. El texto actual evita prometer plazos aún no definidos.
- Proveedores efectivos, ubicación del VPS, transferencias internacionales y contratos de tratamiento. El borrador público describe Groq como proveedor activo; si se configura Cloudflare Workers AI hay que sustituir esa descripción y revisar su política antes de habilitarlo.
- Configuración real de retención de Groq y controles de su cuenta; no afirmar que el proveedor borra todo al terminar. Groq distingue metadatos de uso y contenido y describe controles de retención. Revisar sus términos y elegibilidad comercial con la cuenta real.
- Retención y acceso al correo de soporte, logs del host/proxy, respaldos y snapshots. El código limita los logs de aplicación; esa configuración no controla registros del proveedor de hosting. Evitar respaldar temporales/resultados; fijar y publicar plazos adicionales si se conservan datos.
- Tratamiento de grabaciones sensibles, menores y permisos para grabar/transcribir. Verificar texto y mecanismo para reportes de derechos con la jurisdicción real.
- Términos de fuentes externas y permisos de uso; que un video sea público no autoriza por sí mismo su descarga o transcripción.

Datos comprobables en el código: cookie `tid` con un año de duración renovada en cada respuesta; preferencias/tema en `localStorage`; modelo local en caché del navegador; huella ligera del navegador; hashes con sal de cookie/huella/IP en SQLite con registros de uso y reservas de 25 h; IP sin hash en memoria para el límite de peticiones hasta 1 h; trabajo/fuente/título en RAM aproximadamente 25 h; resultados descargables según `RESULT_TTL_S` (producción propuesta: 3600 s), barrido cada 300 s. Los totales del presupuesto por día se eliminan cuando son anteriores a la fecha UTC actual menos tres días. Ante apagado, el barrido se retoma en el arranque.

El modo local no sube el audio, pero visita la API para configuración/cuota y descarga archivos de modelo de Hugging Face cuando hacen falta. No equivale a una visita sin tráfico de red. No se reabrió el motor ni se cambió la meta de velocidad validada por el usuario.

## SEO básico preparado

- Portada y cuatro páginas informativas con título y descripción distintos, URL canónica absoluta y navegación común; todas enlazadas desde la herramienta.
- `robots.txt` permite el contenido público y aparta `/api/`. Esto orienta a rastreadores; la protección de resultados depende de la identidad comprobada por la API, no de robots.
- `sitemap.xml` enumera solo las cinco páginas existentes. No contiene resultados, parámetros de prueba, idiomas inexistentes ni páginas por formato aún no creadas.
- `SoftwareApplication` y `FAQPage` en la portada describen funciones reales. Las FAQ estáticas y los mensajes del frontend evitan fijar límites antiguos: la API y la herramienta muestran los valores activos.
- `ads.txt` contiene comentarios y ninguna cuenta ficticia. Sustituirlo por el registro exacto autorizado cuando exista una red aprobada. No hay anuncios, analítica ni muro de bloqueadores activos.

Después del dominio y HTTPS: comprobar canónicas, redirecciones, sitemap y robots en la URL pública; eliminar cualquier bloqueo/noindex que se haya añadido en una prueba privada; enviar sitemap mediante Search Console como indica la guía de despliegue. No se garantiza rastreo, indexación, posiciones ni resultados enriquecidos. Las ocho o diez páginas por caso de uso, localización SEO y anuncios son trabajo posterior.

## Fuentes oficiales consultadas

- [Groq: Your Data](https://console.groq.com/docs/your-data): diferencia datos de uso, retención de contenido en casos de fiabilidad/abuso y controles ZDR. El contenido no se conserva por defecto para inferencia, pero no es una promesa universal de retención cero.
- [Hugging Face: Privacy](https://huggingface.co/privacy): describe información de conexión y dispositivo recogida al usar sus servicios.
- [Google: introducción a robots.txt](https://developers.google.com/search/docs/crawling-indexing/robots/intro): robots controla rastreo, no acceso privado ni exclusión garantizada del índice.
- [Google: construir y enviar sitemap](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap): usar URLs canónicas y enviar el sitemap no garantiza indexación.

Revisar estas políticas de nuevo cuando se configure la cuenta y antes de publicar; el contenido de terceros puede cambiar.
