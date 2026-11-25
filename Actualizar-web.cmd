@echo off
setlocal
title Actualizar transcriptoryt.com
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -X utf8 -u "scripts\update_frontend.py"
) else (
    python -X utf8 -u "scripts\update_frontend.py"
)
set "result=%errorlevel%"
echo.
pause
exit /b %result%
