@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "COMPOSE_ENV=deploy\compose.env"
set "COMPOSE_FILE=compose.yaml"

echo ========================================
echo   Guinness Bot — Docker stop
echo ========================================
echo.

where docker >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker CLI not found.
  goto :fail
)

docker info >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker Engine is not running.
  goto :fail
)

REM 1) Stop this Compose project (db + bot + optional diag).
if exist "%COMPOSE_ENV%" if exist "%COMPOSE_FILE%" (
  echo [INFO] Stopping Guinness Compose stack...
  docker compose --env-file "%COMPOSE_ENV%" -f "%COMPOSE_FILE%" down --remove-orphans
  if errorlevel 1 (
    echo [WARN] Compose down reported an error — continuing with global stop.
  ) else (
    echo [OK] Compose project stopped.
  )
) else (
  echo [WARN] Missing compose files — skipping project down.
)

REM 2) Stop any leftover containers whose name/label matches Guinness.
echo [INFO] Stopping leftover guinness* containers if any...
for /f "tokens=*" %%C in ('docker ps -aq --filter "name=guinness" 2^>nul') do (
  echo   - stopping %%C
  docker stop %%C >nul 2>&1
  docker rm %%C >nul 2>&1
)

REM 3) Stop ALL remaining running containers on this machine.
echo [INFO] Stopping all other running Docker containers...
set "ANY=0"
for /f "tokens=*" %%C in ('docker ps -q 2^>nul') do (
  set "ANY=1"
  echo   - stopping %%C
  docker stop %%C >nul 2>&1
)
if "!ANY!"=="0" (
  echo [OK] No running containers left.
) else (
  echo [OK] All running containers were signaled to stop.
)

echo.
echo [INFO] Current Docker status:
docker ps -a --format "table {{.Names}}\t{{.Status}}\t{{.Image}}"
echo.
echo Note: named volumes (Postgres data) are kept.
echo       To wipe DB too: docker compose --env-file deploy\compose.env down -v
echo.
echo Done.
exit /b 0

:fail
echo.
pause
exit /b 1
