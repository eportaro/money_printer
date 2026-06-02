@echo off
REM Pulls the production (VPS) SQL Server data down and restores it into your LOCAL SQL Server.
setlocal
cd /d "%~dp0"
set KEY=%USERPROFILE%\.ssh\vps_money_printer
set VPS=root@2.25.157.237

echo [1/6] Generando backup en el VPS...
ssh -i "%KEY%" -o BatchMode=yes %VPS% "bash /root/money_printer/prod_backup.sh"
if errorlevel 1 goto :err

echo [2/6] Descargando backup a .\deploy\PolymarketBot_prod.bak ...
scp -i "%KEY%" -o BatchMode=yes %VPS%:/root/money_printer/deploy/PolymarketBot.bak ".\deploy\PolymarketBot_prod.bak"
if errorlevel 1 goto :err

echo [3/6] Levantando SQL Server local...
docker compose up -d sqlserver
if errorlevel 1 goto :err

echo [4/6] Cerrando app/collector local (para liberar la BD)...
docker compose stop app collector 1>nul 2>nul

echo [5/6] Copiando backup al contenedor local...
for /f %%i in ('docker compose ps -q sqlserver') do set SQLSRV=%%i
docker exec -u root %SQLSRV% mkdir -p /var/opt/mssql/backups
docker cp ".\deploy\PolymarketBot_prod.bak" %SQLSRV%:/var/opt/mssql/backups/PolymarketBot_prod.bak
if errorlevel 1 goto :err

echo Esperando a que SQL Server local este listo...
timeout /t 20 /nobreak 1>nul

echo [6/6] Restaurando en SQL Server local...
for /f "tokens=1,* delims==" %%a in ('findstr /b "MSSQL_SA_PASSWORD=" .env') do set LOCALPW=%%b
docker compose run --rm --no-deps app sqlcmd -S sqlserver,1433 -U sa -P "%LOCALPW%" -C -i /app/restore_prod.sql
if errorlevel 1 goto :err

echo.
echo LISTO: la data de produccion fue restaurada en tu SQL Server local.
echo Para levantar el stack local completo con esa data: docker compose --profile collector up -d
goto :end
:err
echo.
echo ERROR durante el pull. Revisa el mensaje anterior.
:end
endlocal
pause
