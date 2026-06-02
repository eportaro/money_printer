#!/bin/bash
# Runs ON the VPS: backs up the production SQL Server DB and leaves the .bak in deploy/.
set -e
cd /root/money_printer
SQLSRV=$(docker compose ps -q sqlserver)
PW=$(grep ^MSSQL_SA_PASSWORD= .env | cut -d= -f2-)
docker exec -u root "$SQLSRV" mkdir -p /var/opt/mssql/backups
docker compose run --rm --no-deps app sqlcmd -S sqlserver,1433 -U sa -P "$PW" -C -i /app/backup.sql
docker cp "$SQLSRV":/var/opt/mssql/backups/PolymarketBot.bak /root/money_printer/deploy/PolymarketBot.bak
echo BACKUP_READY
