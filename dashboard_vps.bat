@echo off
REM Abre el dashboard de PRODUCCION (VPS) via tunel SSH.
REM El puerto 80/5000 del VPS esta bloqueado desde afuera; el tunel usa SSH (22), que si pasa.
REM Deja esta ventana abierta mientras uses el dashboard; cierra con Ctrl+C.
setlocal
set KEY=%USERPROFILE%\.ssh\vps_money_printer
set VPS=root@2.25.157.237
set LOCALPORT=15000

start "" http://localhost:%LOCALPORT%
echo Tunel abierto: http://localhost:%LOCALPORT%  ->  dashboard del VPS
echo (deja esta ventana abierta; Ctrl+C para cerrar)
ssh -i "%KEY%" -o BatchMode=yes -N -L %LOCALPORT%:127.0.0.1:5000 %VPS%
endlocal
