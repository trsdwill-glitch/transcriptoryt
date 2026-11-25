@echo off
setlocal EnableExtensions DisableDelayedExpansion
title TranscriptorYT - Subida al VPS
set "project=%~dp0"
if not exist "%project%dist\transcriptor-lws.zip" set "project=%USERPROFILE%\OneDrive\Desktop\transcriptoryt\"
set "zip=%project%dist\transcriptor-lws.zip"
set "report=%project%dist\estado-vps.txt"
set "host=root@185.98.139.30"
set "sshOptions=-o StrictHostKeyChecking=yes -o ConnectTimeout=10 -o NumberOfPasswordPrompts=1"
set "expected=7b9734b90e5ce152b01691407a446de51c99d3d0883e04e54ba72d74fbde67d4"
set "upload=/root/.transcriptor-lws-%RANDOM%-%RANDOM%.part"

if not exist "%zip%" (
    echo Falta dist\transcriptor-lws.zip. Coloca este archivo en la carpeta del proyecto.
    goto failed
)
echo 1. Subiendo la pagina al VPS. Cuando SSH pida la clave, ingresa la de root.
echo Los caracteres de la clave quedan ocultos. Pulsa Enter para continuar.
scp.exe %sshOptions% "%zip%" "%host%:%upload%"
if errorlevel 1 goto failed

echo 2. Comprobando el ZIP. SSH puede pedirte la clave una segunda vez.
set "check=echo %expected%  %upload% | sha256sum -c -"
set "install=mv -- %upload% /root/transcriptor-lws.zip"
set "system=hostname; uname -m; systemd-detect-virt; nproc; free -h; df -h /"
set "ports=ss -lntp '( sport = :80 or sport = :443 or sport = :8000 )'"
set "docker=if command -v docker >/dev/null; then docker version --format '{{.Server.Version}}'; docker compose version; else echo 'Docker pendiente'; fi"
set "dns=echo 'DNS IPv4'; getent ahostsv4 transcriptoryt.com; echo 'DNS IPv6'; getent ahostsv6 transcriptoryt.com"
ssh.exe %sshOptions% %host% "%check% && %install% && { %system%; %ports%; %docker%; %dns%; echo 'ZIP verificado en /root/transcriptor-lws.zip'; }" > "%report%"
if errorlevel 1 goto failed
type "%report%"
echo.
echo LISTO. El paquete esta en el VPS. El informe se guardo en dist\estado-vps.txt.
pause
exit /b 0

:failed
echo.
echo La operacion no termino. Deja esta ventana abierta para ver el mensaje.
pause
exit /b 1
