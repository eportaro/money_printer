@echo off
setlocal
cd /d "%~dp0"
title Polymarket BTC Bot

echo.
echo ==========================================
echo   POLYMARKET BTC BOT
echo ==========================================
echo.

if not exist "venv\Scripts\python.exe" (
    echo [ERROR] No existe venv\Scripts\python.exe
    echo Crea el entorno e instala dependencias:
    echo   python -m venv venv
    echo   venv\Scripts\activate
    echo   pip install -r requirements.txt
    pause
    exit /b 1
)

if not exist "model_artifacts\model.pkl" (
    echo [ERROR] Falta model_artifacts\model.pkl
    echo Ejecuta primero:
    echo   venv\Scripts\python.exe model.py
    pause
    exit /b 1
)

if not exist "model_artifacts\model_supabase.pkl" (
    echo [WARN] Falta model_artifacts\model_supabase.pkl
    echo Entrenando modelo historico simulado market-aware...
    venv\Scripts\python.exe scripts\train_historical_polymarket_sim.py
    if errorlevel 1 (
        echo [ERROR] Fallo el entrenamiento historico simulado.
        pause
        exit /b 1
    )
)

echo [OK] Arrancando collector en una ventana separada...
start "Polymarket Collector" cmd /k "cd /d ""%~dp0"" && venv\Scripts\python.exe collector.py"

echo [OK] Arrancando dashboard en esta ventana...
echo Dashboard: http://localhost:5000
start "" http://localhost:5000
venv\Scripts\python.exe server.py

pause
