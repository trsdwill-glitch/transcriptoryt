@echo off
setlocal
title TranscriptorYT - Publicacion
cd /d "%~dp0"
if not exist "scripts\publish_lws.py" cd /d "%USERPROFILE%\OneDrive\Desktop\transcriptoryt"
if not exist ".venv\Scripts\python.exe" (
    echo Abri este archivo desde la carpeta transcriptoryt del Escritorio.
    pause
    exit /b 1
)
".venv\Scripts\python.exe" -X utf8 -u "scripts\publish_lws.py"
set "result=%errorlevel%"
echo.
echo Informe de esta ejecucion: dist\publicacion-vps.txt
pause
exit /b %result%
