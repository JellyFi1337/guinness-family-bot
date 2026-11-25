@echo off
setlocal EnableExtensions
chcp 65001 >nul
cd /d "%~dp0"

set "COMPOSE_ENV=deploy\compose.env"
set "COMPOSE_FILE=compose.yaml"

where docker >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker CLI not found.
  exit /b 1
)

docker compose --env-file "%COMPOSE_ENV%" -f "%COMPOSE_FILE%" ps
echo.
docker compose --env-file "%COMPOSE_ENV%" -f "%COMPOSE_FILE%" logs --tail 50 bot
exit /b 0
