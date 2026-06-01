@echo off
cd /d "%~dp0"
echo Deteniendo server.py y collector.py...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-CimInstance Win32_Process -Filter \"name = 'python.exe'\" | Where-Object { $_.CommandLine -like '*server.py*' -or $_.CommandLine -like '*collector.py*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
echo Listo.
pause
