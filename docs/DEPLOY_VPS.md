# Despliegue en VPS (Ubuntu 24.04, Docker) — 24/7

Guía para correr el bot 24/7 en el VPS sin depender de la PC. Probado para Hostinger KVM (8 GB RAM).

## 0. Resumen de lo que NO va en el repo (y hay que llevar aparte)

| Qué | Por qué | Cómo llega al VPS |
|---|---|---|
| `model_artifacts/model.pkl` | Modelo base (gitignored) | `scp` |
| `model_artifacts/model_supabase.pkl` | Modelo activo live (gitignored, no regenerable sin la BD) | `scp` |
| `deploy/PolymarketBot.bak` | Histórico de SQL Server (rondas, snapshots, etc.) | `scp` + restore |
| `.env` / `.env.docker` | Secretos (gitignored) | crear en el VPS desde los `.example` |

## 1. Preparar el VPS

```bash
ssh root@2.25.157.237
apt-get update && apt-get install -y docker.io docker-compose-plugin git
systemctl enable --now docker
git clone https://github.com/eportaro/money_printer.git
cd money_printer
```

## 2. Configurar secretos (con password NUEVO, no el de la PC)

```bash
cp .env.example .env            # si existe; si no, crea uno con DB_BACKEND=sqlserver
cp .env.docker.example .env.docker
# Edita ambos: define un password fuerte y úsalo en los dos sitios:
#   .env            -> MSSQL_SA_PASSWORD=...
#   .env.docker     -> SQLSERVER_CONNECTION=...PWD=<el mismo>...
nano .env
nano .env.docker
```

> El `docker-compose.yml` toma el password de `MSSQL_SA_PASSWORD` en `.env`. La cadena `SQLSERVER_CONNECTION` en `.env.docker` debe usar EXACTAMENTE el mismo password.

## 3. Copiar modelos y backup desde la PC (en otra terminal local)

```powershell
scp model_artifacts/model.pkl          root@2.25.157.237:~/money_printer/model_artifacts/
scp model_artifacts/model_supabase.pkl root@2.25.157.237:~/money_printer/model_artifacts/
scp model_artifacts/metrics_supabase.json root@2.25.157.237:~/money_printer/model_artifacts/
scp deploy/PolymarketBot.bak           root@2.25.157.237:~/money_printer/deploy/
```

## 4. Levantar SQL Server y restaurar el histórico

```bash
# Arranca solo la BD primero
docker compose up -d sqlserver
sleep 25   # espera a que SQL Server acepte conexiones

# Copia el .bak dentro del contenedor y restaura
docker compose exec -u root sqlserver mkdir -p /var/opt/mssql/backups
docker cp deploy/PolymarketBot.bak $(docker compose ps -q sqlserver):/var/opt/mssql/backups/PolymarketBot.bak

PWD_SA=$(grep '^MSSQL_SA_PASSWORD=' .env | cut -d= -f2-)
docker compose exec sqlserver /opt/mssql-tools18/bin/sqlcmd -S localhost -U sa -P "$PWD_SA" -C -Q \
 "RESTORE DATABASE [PolymarketBot] FROM DISK=N'/var/opt/mssql/backups/PolymarketBot.bak' WITH MOVE 'PolymarketBot' TO '/var/opt/mssql/data/PolymarketBot.mdf', MOVE 'PolymarketBot_log' TO '/var/opt/mssql/data/PolymarketBot_log.ldf', REPLACE, STATS=10"
```

> Nota: la imagen `mssql/server` no trae `mssql-tools18`. Si `sqlcmd` no existe en ese contenedor, corre el RESTORE desde el contenedor `app` apuntando a `-S sqlserver,1433` (ese sí tiene las tools), tras hacer `docker compose build app`.

## 5. Levantar todo

```bash
docker compose --profile collector up -d --build
docker compose logs -f app collector
```

Dashboard: `http://2.25.157.237:5000` (abre el puerto 5000 en el firewall de Hostinger; idealmente detrás de un reverse proxy con auth).

## 6. Verificar

```bash
curl -s http://localhost:5000/api/dataset-health | head
docker compose logs --since 2m collector | grep -i baseline
```

- El baseline de cada ronda debe quedar ~igual al precio (decenas de dólares), nunca ~$2,000 de diferencia.
- `model_version` en las señales debe ser el modelo activo.

## Notas de rendimiento (8 GB RAM)

- SQL Server usa ~3 GB (`MSSQL_MEMORY_LIMIT_MB`). App + collector ~1 GB. Sobra margen.
- La velocidad de las señales depende de la red a Coinbase/Polymarket, no de la CPU.
- `STRATEGY_LAB_AUTO_REFRESH=false` (ya en `.env.docker.example`) evita el subproceso de backtest por resolución y ahorra CPU.
- `restart: unless-stopped` mantiene todo vivo tras reinicios del VPS.
