# Preparación e instalación posterior en Ubuntu / LWS

Paquete preparado el 22 de noviembre de 2026. **En esta etapa no se compra ni se publica nada.** Los comandos que modifican el VPS se ejecutan después de comprarlo. El proyecto actual usa Groq (o Cloudflare Workers AI), SQLite y una cola en memoria; el VPS no necesita GPU ni Redis. El navegador mantiene Whisper Base multilingüe ya validado por el propietario.

## 1. Lo que contiene el paquete

- Imagen Python 3.12 sobre Debian Bookworm, ffmpeg/ffprobe, Node 24.22.3, yt-dlp fijado y su componente EJS compatible; aplicación sin privilegios de root.
- Docker Compose con una única instancia de Uvicorn, Caddy para HTTPS automático, límites de CPU/RAM/procesos y raíz de contenedor de solo lectura.
- Volúmenes para SQLite, archivos temporales/resultados y certificados. La app no publica el puerto 8000 al exterior.
- `static/assets/vendor/` con Transformers.js, los dos recorridos WASM y licencias. La imagen falla al construir si faltan archivos esenciales. Los pesos Whisper se descargan al dispositivo desde Hugging Face al iniciar su primera transcripción; no se alojan en este VPS.
- Perfil local separado, imagen de pruebas, comprobación HTTP y comandos para probar Groq y YouTube.

Las versiones principales de Node, Caddy y yt-dlp están fijadas. Python conserva la rama 3.12 y apt obtiene actualizaciones de Bookworm; las dependencias transitivas de pip se resuelven al construir. Por eso **no se promete una reconstrucción idéntica byte por byte**. Cada imagen guarda `pip freeze` en `/opt/transcriptor/installed-packages.txt`. Para repetir exactamente un despliegue, conservar la imagen construida con `docker save`, su hash y los archivos de configuración privados.

## 2. Prueba Linux antes de comprar (si se instala Docker por separado)

Esta computadora no tenía Docker ni una distribución WSL utilizable durante la preparación. No se instaló software del sistema ni se comprobó una construcción Linux. Si más adelante se instala Docker Desktop con contenedores Linux, estos comandos de PowerShell no necesitan dominio ni claves:

```powershell
Set-Location 'C:\Users\thiag\OneDrive\Desktop\transcriptoryt'
docker build --target tests -t transcriptor-tests -f deploy/Dockerfile .
docker run --rm --read-only --tmpfs /tmp:rw,nosuid,size=536870912 transcriptor-tests
docker run --rm --entrypoint python transcriptor-tests -m ruff check .
docker compose -f deploy/compose.local.yml up -d --build --wait
.\.venv\Scripts\python.exe scripts/smoke_server.py --base-url http://127.0.0.1:8080
docker compose -f deploy/compose.local.yml logs --tail 50
docker compose -f deploy/compose.local.yml down
```

La suite usa ffmpeg real y proveedor simulado: no acredita Groq real. El perfil local permite abrir `http://localhost:8080/?mode=local`; `localhost` es un contexto seguro para el navegador, pero eso no acredita TLS público ni celulares. El volumen local se conserva al hacer `down`.

En Ubuntu, los mismos comandos funcionan sustituyendo el ejecutable Python de Windows por `python3`. Antes de construir una copia obtenida con git, comprobar que `static/assets/vendor/` existe: ese directorio estaba excluido por `.gitignore` en el proyecto original. Si falta, ejecutar `node scripts/setup-browser-runtime.mjs` con Node 22 o superior. El script usa versiones y hashes fijados. El paquete de entrega debe contenerlo ya preparado.

## 3. Qué comprar después

Elegir un VPS Ubuntu 24.04 LTS de 64 bits, preferiblemente 2 vCPU y 4 GB RAM, con Docker permitido, IP pública y acceso SSH. Reservar al menos 25 GB de disco y revisar espacio libre: un lote puede subir cinco archivos de 500 MiB, más audio convertido y resultados. El modo local no consume CPU de transcripción en el VPS. La capacidad real simultánea y el ancho de banda quedan por medir en el servidor elegido; no es una previsión de capacidad para un millón de visitas.

Anotar `VPS_IP`, el usuario inicial que entregue LWS y el acceso a su consola de recuperación. Elegir un dominio y un correo de contacto atendido antes de completar los marcadores.

## 4. Primer acceso y acceso administrativo

En PowerShell, si todavía no hay una llave SSH destinada a este servidor:

```powershell
ssh-keygen -t ed25519 -f "$env:USERPROFILE\.ssh\transcriptor_lws"
Get-Content "$env:USERPROFILE\.ssh\transcriptor_lws.pub"
ssh USUARIO_INICIAL@VPS_IP
```

No compartir el archivo sin `.pub`. En el VPS, con el acceso inicial autorizado:

```bash
sudo apt update
sudo apt upgrade -y
sudo apt install -y ufw unattended-upgrades ca-certificates curl openssl python3 unzip
sudo adduser transcriptor
sudo usermod -aG sudo transcriptor
sudo install -d -m 700 -o transcriptor -g transcriptor /home/transcriptor/.ssh
sudo nano /home/transcriptor/.ssh/authorized_keys
sudo chown transcriptor:transcriptor /home/transcriptor/.ssh/authorized_keys
sudo chmod 600 /home/transcriptor/.ssh/authorized_keys
```

Pegar **solo** el contenido de la llave pública. Mantener abierta la sesión inicial y abrir otra terminal:

```powershell
ssh -i "$env:USERPROFILE\.ssh\transcriptor_lws" transcriptor@VPS_IP
```

Comprobar `sudo -v` en esa segunda sesión. Solo después, crear en el VPS `/etc/ssh/sshd_config.d/00-transcriptor.conf`:

```text
PermitRootLogin no
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
```

```bash
sudo sshd -t
sudo systemctl reload ssh
sudo ufw allow OpenSSH
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw allow 443/udp
sudo ufw enable
sudo ufw status
sudo dpkg-reconfigure -plow unattended-upgrades
```

Volver a comprobar una conexión nueva por llave antes de cerrar la sesión inicial. Revisar `sudo sshd -T` si LWS aporta reglas propias: debe mostrar contraseña desactivada. Si se utiliza otro puerto SSH, permitir ese puerto antes de activar UFW. Docker puede publicar puertos saltándose UFW; esta composición publica únicamente 80/443 y nunca 8000. [Documentación oficial de Docker sobre Ubuntu y firewall](https://docs.docker.com/engine/install/ubuntu/).

## 5. Instalar Docker Engine y Compose

Usar el repositorio oficial, con el usuario `transcriptor`:

```bash
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
. /etc/os-release
printf 'Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' "$VERSION_CODENAME" "$(dpkg --print-architecture)" | sudo tee /etc/apt/sources.list.d/docker.sources >/dev/null
sudo apt update
sudo apt install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo systemctl enable --now docker
sudo docker run --rm hello-world
sudo usermod -aG docker transcriptor
```

Cerrar y volver a abrir SSH; después ejecutar `docker version` y `docker compose version`. El grupo `docker` permite administrar el host: añadir solo al operador. Si había paquetes Docker no oficiales, resolver sus conflictos según la [guía de instalación oficial](https://docs.docker.com/engine/install/ubuntu/); no desinstalar componentes ajenos a ciegas.

## 6. Subir y configurar el paquete

En el VPS:

```bash
sudo install -d -o transcriptor -g transcriptor -m 750 /opt/transcriptor
```

Desde PowerShell, subir el archivo de entrega generado, sustituyendo su nombre exacto:

```powershell
scp -i "$env:USERPROFILE\.ssh\transcriptor_lws" '.\dist\transcriptor-lws.zip' transcriptor@VPS_IP:/opt/transcriptor/
```

Comprobar su SHA-256 con `Get-FileHash .\dist\transcriptor-lws.zip` en Windows y `sha256sum transcriptor-lws.zip` en Ubuntu. En el VPS, revisar `unzip -l transcriptor-lws.zip` y extraer con `unzip transcriptor-lws.zip` dentro de `/opt/transcriptor`. Los comandos siguientes se ejecutan donde están `app/`, `static/` y `deploy/`.

```bash
cd /opt/transcriptor
umask 077
cp deploy/.env.example deploy/.env
openssl rand -hex 32
openssl rand -hex 32
nano deploy/.env
chmod 600 deploy/.env
```

Poner valores diferentes en `SECRET_KEY` y `HASH_SALT`, la clave privada en `GROQ_API_KEY`, el dominio sin `https://` ni rutas en `DOMAIN`, y el correo real en `CONTACT_EMAIL`. No ejecutar `cat deploy/.env`, `docker compose config` sin `--quiet` ni compartir capturas de sus valores. La clave nunca se introduce en la interfaz pública.

Configuración inicial de producción:

| Variable | Valor preparado | Motivo |
|---|---:|---|
| `DAILY_QUOTA_MIN` / `MAX_FILE_MIN` | 120 / 120 | Decisión del 24-11-2026; ventana móvil de 24 h |
| `IP_QUOTA_MULTIPLIER` | 3 | Mayor margen para IP compartida |
| `MAX_ITEMS` | 5 | Lote máximo |
| `MAX_UPLOAD_MB` / `MAX_DOWNLOAD_MB` | 500 / 500 | MiB por archivo (no hay cuota total de bytes por usuario); medir disco y red |
| `MAX_REQUEST_BODY` | 2622488576 | Cinco archivos de 500 MiB + 1 MiB de campos |
| `DAILY_BUDGET_USD` | 0.50 | Tope inicial de reservas estimadas, día UTC |
| `COST_PER_HOUR_USD` | 0.04 | Valor heredado, revisar tarifa real del modelo |
| `MAX_CONCURRENT_JOBS` / `PROVIDER_CONCURRENCY` | 1 / 2 | Inicio conservador para VPS pequeño |
| `RESULT_TTL_S` | 3600 | Descarga por una hora |
| `JOB_TIMEOUT_S` | 3600 | Incluye la espera en cola; margen para un elemento de 120 min |

Si se cambian `MAX_ITEMS` o `MAX_UPLOAD_MB`, recalcular `MAX_REQUEST_BODY = MAX_ITEMS × MAX_UPLOAD_MB × 1048576 + 1048576`. La app aplica además sus límites por archivo, duración, cuota y presupuesto. No usa el presupuesto como límite real de facturación de Groq; revisar también el panel del proveedor. No está implementada la reducción automática de cuota al 70% de presupuesto ni el cubo VPN/Turnstile.

Completar nombre, titular responsable, dirección/región aplicable, correo y fecha de las páginas con `scripts/configure_site.py` y revisar la documentación legal. Este comando modifica la copia preparada en el VPS; usar datos reales:

```bash
python3 scripts/configure_site.py --name 'NOMBRE REAL' --domain dominio-real.com --email contacto@dominio-real.com --operator 'TITULAR REAL' --address 'DIRECCIÓN REAL' --date AAAA-MM-DD --apply
python3 scripts/configure_site.py --check
```

La fecha usa formato ISO y se sustituye por el día efectivo de revisión. No reemplazar marcadores por datos inventados. Esta configuración del contenido no escribe secretos ni sustituye `deploy/.env`.

## 7. DNS y HTTPS público (después de comprar)

Crear en el proveedor DNS un registro **A** para el dominio elegido hacia `VPS_IP`. Añadir **AAAA** solo si se ha probado la conectividad IPv6 de ese VPS. No crear `www` si no se va a configurar como dominio adicional: esta configuración sirve un único dominio canónico.

Si se usa Cloudflare para DNS, comenzar con la nube gris, **solo DNS**. Caddy obtiene y renueva el certificado cuando el DNS apunta al VPS y los puertos 80/443 son alcanzables. El navegador debe abrir `https://DOMINIO_REAL` sin advertencias. [HTTPS automático de Caddy](https://caddyserver.com/docs/automatic-https).

No activar la nube naranja con esta configuración: Caddy fija `X-Forwarded-For` a la conexión que recibe para evitar falsificaciones. Con Cloudflare proxificado la conexión sería la del CDN y agruparía la cuota de visitantes. Para activarlo luego hay que configurar y verificar una cadena de proxies de confianza restringida a los rangos oficiales de Cloudflare, impedir el acceso directo al origen y revisar sus límites de subida vigentes. El contrato antiguo proponía Cloudflare sin cubrir estos ajustes. [Cabeceras y proxies de confianza de Caddy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy).

## 8. Construcción, pruebas y arranque

Primero construir y ejecutar las pruebas Linux sin credenciales:

```bash
docker build --target tests -t transcriptor-tests -f deploy/Dockerfile .
docker run --rm --read-only --tmpfs /tmp:rw,nosuid,size=536870912 transcriptor-tests
docker run --rm --entrypoint python transcriptor-tests -m ruff check .
```

Después comprobar configuración sin mostrar sus secretos y validar Caddy:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml config --quiet
docker compose --env-file deploy/.env -f deploy/docker-compose.yml run --rm --no-deps caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
docker compose --env-file deploy/.env -f deploy/docker-compose.yml up -d --build --wait --wait-timeout 180
docker compose --env-file deploy/.env -f deploy/docker-compose.yml ps
sh deploy/smoke_test.sh https://DOMINIO_REAL
docker compose --env-file deploy/.env -f deploy/docker-compose.yml logs --tail 50
```

`--wait` acredita el healthcheck de app; la prueba HTTPS acredita también el dominio, TLS y proxy. El healthcheck solo significa que la aplicación responde: no comprueba Groq ni YouTube. El primer certificado puede tardar; si falla, revisar DNS A/AAAA, firewall y acceso a 80/443 antes de repetir.

Comprobar desde otra red que `http://VPS_IP:8000` no responde. Revisar `docker compose ... ps` y `sudo ss -lntup`: solo Caddy debe publicar los puertos de la web. La app lee exclusivamente el XFF fijado por Caddy; Uvicorn tiene desactivado su procesamiento automático de proxies. No añadir un puerto publicado para la app.

## 9. Prueba real de Groq

Sin clave disponible no se acredita ninguna llamada real. Tras configurar la clave y arrancar:

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec -T app python scripts/smoke_server.py --base-url https://DOMINIO_REAL --public-audio
```

El script utiliza un audio público corto, ejecuta un trabajo y descarga/abre el DOCX; consume cuota y puede consumir crédito del proveedor. Ver su salida sin compartir la clave ni transcripciones ajenas. Se ejecuta dentro del contenedor para usar sus dependencias Python fijadas; la conexión HTTPS recorre el dominio y Caddy. Si se prueba un archivo propio, consultar `docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec -T app python scripts/smoke_server.py --help`. Mantener la clave en `deploy/.env` y comprobar la facturación directamente en Groq.

## 10. YouTube: decisión desde la IP del VPS

Que funcione en la computadora no demuestra que funcione desde una IP de centro de datos. No se marca como validado hasta ejecutar esta prueba desde el VPS real.

Copiar `deploy/youtube-urls.example.txt` a `deploy/youtube-urls.txt` y completar **30 videos públicos diferentes** que se tenga derecho a descargar, una URL por línea, cortos y por debajo de `MAX_FILE_MIN` (120 minutos). Seleccionar contenido representativo de los casos previstos, no una repetición del mismo enlace; conservar la lista en privado junto al informe para repetir la prueba. No usar listas de reproducción ni videos privados/restringidos. La plantilla no inventa 30 URLs sin verificarlas.

```bash
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec -T app node --version
docker compose --env-file deploy/.env -f deploy/docker-compose.yml exec -T app python -c 'from importlib.metadata import version; print("yt-dlp=" + version("yt-dlp")); print("yt-dlp-ejs=" + version("yt-dlp-ejs"))'
docker compose --env-file deploy/.env -f deploy/docker-compose.yml run --rm --no-deps -v "$PWD/deploy/youtube-urls.txt:/corpus.txt:ro" app python scripts/test_ytdlp.py --urls /corpus.txt --report /data/youtube-report.json --minimum 30
docker compose --env-file deploy/.env -f deploy/docker-compose.yml cp app:/data/youtube-report.json ./youtube-report.json
```

La lista se monta de solo lectura y no entra en la imagen. `run` ejecuta solo el script, no otra instancia de la API; comparte el volumen `/data` para guardar el informe. Esta prueba descarga/inspecciona audio mediante la ingesta real, sin llamar a Groq ni consumir su cuota. Verificarlo en la ayuda del script antes de ejecutarlo si se ha modificado.

| Éxito de las 30 descargas | Decisión inicial |
|---|---|
| Al menos 90% | Mantener YouTube y vigilar fallos; no garantiza éxito futuro |
| Desde 70% y menos de 90% | Actualizar yt-dlp/EJS, comprobar Node y repetir con la misma lista |
| Menos de 70% | Frenar gastos, documentar fallos y decidir si ofrecer solo archivos/enlaces directos |

No contratar automáticamente proxies ni introducir cookies personales. Son opciones posteriores con costes, restricciones y riesgos de cuenta propios; no arreglan todos los bloqueos. El entorno usa `yt-dlp[default]` porque instala EJS compatible; Node se activa explícitamente y no se descargan scripts remotos durante trabajos. [Configuración EJS oficial de yt-dlp](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## 11. Probar desde un celular con HTTPS

Después de que el certificado público funcione, abrir `https://DOMINIO_REAL` en el teléfono mediante Wi-Fi y datos móviles. No usar `http://IP:8000` ni aceptar certificados con advertencias para validar WebGPU.

1. Comprobar portada, navegación a las cuatro páginas, cambio de tema y diseño sin desplazamiento horizontal.
2. En **En este dispositivo**, elegir un audio propio corto (MP3/WAV), esperar la descarga inicial del modelo con pantalla activa y comprobar progreso, cancelación y exportación. El motor puede elegir WASM; no se exige WebGPU a todos los móviles. Repetir con `?mode=local&engine=wasm` si se quiere comprobar el respaldo.
3. Comprobar en la pestaña Red de un navegador conectado por depuración que el audio local no se envía a `/api/jobs`; hay tráfico del sitio/modelo. Comparar la cuota antes y después: debe conservarse.
4. En **Servidor**, procesar un archivo corto y descargar el resultado; refrescar y comprobar cuota persistente. No confundir cambiar de red con recuperar cuota: cookie y huella siguen contando.
5. Probar TXT, MD, DOCX, SRT y VTT en las aplicaciones que realmente se usarán; anotar modelo de teléfono, sistema, navegador, tamaño/duración, modo y resultado. En iOS probar Safari y en Android Chrome; ampliar después a los navegadores de la especificación.

Registrar por separado tiempo de primera descarga del modelo y tiempo de transcripción. La velocidad local ya aceptada en esta computadora no se vuelve a optimizar en esta etapa. HTTPS público, compatibilidad y estabilidad reales de móviles siguen pendientes hasta disponer del dominio/VPS (o de un entorno HTTPS independiente autorizado).

## 12. Operación y actualizaciones

Todos los comandos se ejecutan desde la raíz. Abreviatura para una sesión SSH:

```bash
dc() { docker compose --env-file deploy/.env -f deploy/docker-compose.yml "$@"; }
dc ps
dc logs --tail 100 app caddy
dc restart app
sh deploy/budget_status.sh
df -h
docker system df
```

La cifra de `budget_status.sh` es la reserva/coste estimado local del día UTC. `/api/quota` muestra cuota del visitante, no facturación global. Revisar también `docs/PROVEEDORES.md` y las condiciones del proveedor antes de abrir el servicio. No activar logs de acceso o depuración HTTP sin revisar su contenido. Caddy no tiene access log configurado; sus errores operativos pueden incluir datos de peticiones o de red: restringir acceso a Docker y no publicar esos logs sin revisar/redactar.

**Un solo proceso/instancia.** No usar `--scale app=2` ni añadir workers: la cola y las descargas viven en memoria. Reiniciar cancela trabajos activos; el servicio no recupera enlaces de resultados anteriores. Programar mantenimiento y avisar antes de reiniciar. El barrido elimina resultados vencidos periódicamente; además se comprueba el vencimiento al descargar.

Para actualizar el código, conservar una copia de la imagen y del paquete anterior, incorporar el paquete nuevo, repetir pruebas y reconstruir:

```bash
docker image tag transcriptor:local transcriptor:previous
dc build app
dc up -d --no-deps --wait --wait-timeout 120 app
sh deploy/smoke_test.sh https://DOMINIO_REAL
```

Para volver a la imagen anterior, `docker image tag transcriptor:previous transcriptor:local` y `dc up -d --no-deps --wait app`. Conservar también el código/configuración anterior; revisar migraciones antes de asumir que cualquier versión futura permite retroceso.

**Actualizar yt-dlp reproduciblemente:** YouTube cambia con frecuencia. Comprobar una versión no reinicia nada:

```bash
sh deploy/update_ytdlp.sh --check
```

En una ventana de mantenimiento, usar la versión exacta revisada (formato de ejemplo, sustituir por la versión encontrada) o resolver la estable actual:

```bash
sh deploy/update_ytdlp.sh YYYY.MM.DD
# Alternativa: resuelve y guarda la versión exacta antes de construir.
sh deploy/update_ytdlp.sh --latest
```

El script modifica el pin, construye una imagen de pruebas, ejecuta la suite Linux y luego reconstruye/reemplaza la app. Si el arranque del candidato falla, restaura el pin y la imagen previa cuando existe. No hace `pip install` en producción. Conservar el nuevo `deploy/ytdlp-version.txt`, el `pip freeze` y la imagen; repetir las 30 descargas. No acredita por sí solo el éxito de YouTube.

Se incluye una **comprobación diaria**, sin actualización automática ni reinicios imprevistos:

```bash
sudo cp deploy/transcriptor-ytdlp-check.service /etc/systemd/system/
sudo cp deploy/transcriptor-ytdlp-check.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now transcriptor-ytdlp-check.timer
systemctl list-timers transcriptor-ytdlp-check.timer
journalctl -u transcriptor-ytdlp-check.service --since '7 days ago'
```

Los archivos presuponen `/opt/transcriptor` y usuario `transcriptor`; corregirlos si se eligió otra ubicación. El timer registra que hay una versión, no envía correo. Revisarlo habitualmente o añadir una alerta después. Para Caddy/Node/Python y el host, revisar actualizaciones de seguridad, cambiar el pin cuando corresponda, reconstruir y repetir pruebas; no mantener imágenes antiguas indefinidamente.

**Copias de seguridad:** guardar `deploy/.env` cifrado, la versión del paquete y la imagen. No respaldar audio ni resultados. Si se quiere preservar el consumo tras una pérdida de disco, respaldar SQLite de forma consistente con su API de backup (no copiar solo el `.sqlite3` mientras está en WAL), y protegerlo como datos seudónimos. Conservar el volumen `app_data` entre recreaciones; `docker compose down -v` lo elimina y reinicia cuotas. Conservar los volúmenes de Caddy evita perder sus certificados/cuenta ACME. No incluir secretos ni estos datos en el paquete público.

## 13. Revisión antes de publicar y Search Console

Reemplazar y revisar los seis marcadores **SITE_NAME, DOMAIN, CONTACT_EMAIL, OPERATOR_NAME, OPERATOR_ADDRESS y LAST_UPDATED**. Verificar páginas reales, privacidad del proveedor, conservación, anuncios/consentimiento y la normativa aplicable al titular/visitantes. No anunciar GPU propia, modo Small, extracción local universal de videos, bloqueo publicitario, Turnstile, cubos VPN, idiomas o analítica que todavía no existan.

`ads.txt` no debe contener un identificador de editor inventado. Completarlo únicamente cuando AdSense proporcione los datos y se active su integración. No pedir aprobación alegando que ya hay anuncios/consentimiento configurados. La aprobación de AdSense queda fuera de este paquete.

Con el sitio ya público y `robots.txt`/`sitemap.xml` usando el dominio real:

1. Entrar en [Google Search Console](https://search.google.com/search-console/), añadir una propiedad **Dominio** y copiar su registro TXT al DNS. Confirmar la verificación; mantener el TXT. También puede elegirse prefijo de URL HTTPS y un método permitido. [Ayuda oficial de verificación](https://support.google.com/webmasters/answer/9008080).
2. Abrir `https://DOMINIO_REAL/sitemap.xml`: debe devolver 200 y solo URLs canónicas de páginas existentes. Verificar `https://DOMINIO_REAL/robots.txt`, sin bloqueo general de producción.
3. En **Sitemaps**, introducir `sitemap.xml` (o su URL completa, según la propiedad) y enviar. Comprobar el estado procesado y corregir errores. El envío no garantiza indexación. [Guía oficial de sitemaps](https://developers.google.com/search/docs/crawling-indexing/sitemaps/build-sitemap).
4. Usar **Inspección de URLs** con la portada y las páginas principales; comprobar que Google puede acceder y que la canónica es correcta. No enviar endpoints de API, archivos temporales ni descargas de trabajos.

## 14. Qué solo se puede comprobar después

DNS/propiedad del dominio, certificado público y renovación, acceso desde redes móviles, rendimiento y disco del VPS, bloqueo de YouTube según su IP, llamadas reales de Groq si falta clave, límites/costes efectivos del proveedor, Search Console/indexación y AdSense. Las pruebas con proveedor simulado y un archivo preparado localmente no sustituyen esas verificaciones. Registrar cada resultado sin secretos en el informe operativo.
