@echo off
setlocal
cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
    echo Docker no esta instalado o no esta en el PATH.
    pause
    exit /b 1
)

echo Deteniendo contenedores del Polymarket Bot...
docker compose --profile collector down
echo Listo.
pause
