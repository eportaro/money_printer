RESTORE DATABASE [PolymarketBot] FROM DISK = N'/var/opt/mssql/backups/PolymarketBot_prod.bak'
WITH MOVE 'PolymarketBot' TO '/var/opt/mssql/data/PolymarketBot.mdf',
     MOVE 'PolymarketBot_log' TO '/var/opt/mssql/data/PolymarketBot_log.ldf',
     REPLACE, STATS = 20;
