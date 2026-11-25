# Verificación del paquete de despliegue

Fecha: 22 de noviembre de 2026. Entorno: Windows; no hay Docker Engine ni distribución WSL disponible. No se compró ni publicó nada.

## Configuración de contenedores

Se analizaron `docker-compose.yml` y `compose.local.yml` con PyYAML instalado en el entorno local. Se comprobó la estructura de servicios, raíz de solo lectura, eliminación de capacidades, modo de producción/proxy en el perfil público y ausencia de puertos publicados para la app. El límite de cuerpo es exactamente `5 × 500 × 1048576 + 1048576 = 2622488576` bytes.

Esto acredita sintaxis YAML e invariantes estáticos, no la interpretación completa de Docker Compose ni la construcción/ejecución de imágenes Linux.

Se contrastó además el [Dockerfile oficial de Node 24 Bookworm slim](https://github.com/nodejs/docker-node/blob/main/24/bookworm-slim/Dockerfile); se incluye `libatomic1` junto a `libstdc++6` en la imagen Python que recibe el binario Node. El build ejecutará `node --version` para detectar bibliotecas faltantes en Linux.

## Scripts de shell

`sh -n` aprobó `smoke_test.sh`, `budget_status.sh` y `update_ytdlp.sh` utilizando el shell distribuido con Git en Windows. Se comprobó además que usan LF y `#!/bin/sh`. La ejecución inicial del parser dentro del aislamiento de herramientas falló por un permiso de MSYS; la repetición fuera de ese aislamiento completó los tres análisis. No se ejecutó el despliegue, el timer ni una actualización de yt-dlp.

## Configuración de Caddy

Se descargó el binario Windows oficial de [Caddy v2.11.9](https://github.com/caddyserver/caddy/releases/tag/v2.11.9) en `.tools/caddy/`, fuera del paquete de entrega. Antes de ejecutarlo se comparó el archivo ZIP con el SHA-512 publicado por el proyecto en `caddy_2.11.9_checksums.txt`:

```text
98753d5d42650383d00619ac70917a217141f9e92314185bf6c1b8b439cfcfdf8393da0ebb4675c8cb3b23d29a19aabe99dc5aca64fa1bfc3a1ba22509b06871
```

Con variables de prueba `DOMAIN=example.com`, `CONTACT_EMAIL=ops@example.com` y `MAX_REQUEST_BODY=2622488576`, se ejecutó:

```text
caddy.exe adapt --config deploy/Caddyfile --adapter caddyfile --validate
```

Resultado: salida 0, adaptación y validación aprobadas. Caddy emitió un aviso de que `header_up X-Forwarded-For` es redundante con su comportamiento por defecto; se conserva explícito porque la aplicación confía en el primer valor de esa cabecera y requiere reemplazar valores aportados por el cliente. No se abrió un servidor ni se solicitó un certificado. Esta prueba no acredita HTTPS público, DNS, red del VPS ni la imagen Linux de Caddy.

## Pendiente de entorno Linux

Ejecutar construcción de imágenes, suite POSIX y perfil local siguiendo `DESPLIEGUE.md`. Después de comprar el VPS: validar Compose/Caddy en Linux, arranque bajo límites y volúmenes, dominio/TLS, prueba Groq real y corpus YouTube desde su IP. La validación Windows de Caddy no sustituye esos controles.
