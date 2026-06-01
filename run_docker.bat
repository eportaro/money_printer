@echo off
setlocal

cd /d "%~dp0"

where docker >nul 2>nul
if errorlevel 1 (
    echo Docker no esta instalado o no esta en el PATH.
    pause
    exit /b 1
)

echo.
echo ==========================================
echo   Polymarket BTC Bot - Docker
echo ==========================================
echo.
echo 1. Levantar SQL Server + dashboard solamente
echo 2. Levantar SQL Server + dashboard + collector
echo 3. Ver logs en vivo
echo.
set /p choice="Elige una opcion [1/2/3]: "

if "%choice%"=="2" (
    docker compose --profile collector up -d --build
) else if "%choice%"=="3" (
    docker compose logs -f app collector
) else (
    docker compose up -d --build app
)

if errorlevel 1 (
    echo.
    echo No se pudo levantar Docker. Revisa Docker Desktop y los logs.
    pause
    exit /b 1
)

echo.
echo Listo. Dashboard:
echo http://localhost:5000
echo.
echo Para ver logs:
echo docker compose logs -f app collector
echo.
echo Para detener:
echo stop_docker.bat
echo.
pause
