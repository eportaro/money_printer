@echo off
setlocal

cd /d "%~dp0"

if exist ".venv\Scripts\activate.bat" (
    call ".venv\Scripts\activate.bat"
)

echo Starting Jupyter Lab...
echo If this fails, run: pip install -r requirements-dev.txt
jupyter lab notebooks
