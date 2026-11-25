@echo off
setlocal EnableExtensions EnableDelayedExpansion
chcp 65001 >nul
cd /d "%~dp0"

set "COMPOSE_ENV=deploy\compose.env"
set "COMPOSE_ENV_EXAMPLE=deploy\compose.env.example"
set "COMPOSE_FILE=compose.yaml"
set "ENV_FILE=.env"
set "ENV_EXAMPLE=.env.example"
set "SECRETS_DIR=secrets"
set "DB_PASSWORD_FILE=secrets\db_password"
set "TOKEN_FILE=secrets\discord_token"

echo ========================================
echo   Guinness Bot — Docker start
echo ========================================
echo.

where docker >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker CLI not found. Install Docker Desktop and reopen the terminal.
  goto :fail
)

docker compose version >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker Compose plugin not found.
  goto :fail
)

docker info >nul 2>&1
if errorlevel 1 (
  echo [ERROR] Docker Engine is not running. Start Docker Desktop and wait until it is Ready.
  goto :fail
)

if not exist "%COMPOSE_FILE%" (
  echo [ERROR] Missing %COMPOSE_FILE% in %CD%
  goto :fail
)

if not exist "%COMPOSE_ENV%" (
  if exist "%COMPOSE_ENV_EXAMPLE%" (
    echo [INFO] Creating %COMPOSE_ENV% from example...
    copy /Y "%COMPOSE_ENV_EXAMPLE%" "%COMPOSE_ENV%" >nul
  ) else (
    echo [ERROR] Missing %COMPOSE_ENV% and example file.
    goto :fail
  )
)

if not exist "%ENV_FILE%" (
  if exist "%ENV_EXAMPLE%" (
    echo [INFO] Creating %ENV_FILE% from example — fill TOKEN_* and GUILD_ID_* before Discord login.
    copy /Y "%ENV_EXAMPLE%" "%ENV_FILE%" >nul
  ) else (
    echo [ERROR] Missing %ENV_FILE%. Copy .env.example to .env and fill secrets.
    goto :fail
  )
)

if not exist "%SECRETS_DIR%" mkdir "%SECRETS_DIR%"

if not exist "%DB_PASSWORD_FILE%" (
  echo [INFO] Generating %DB_PASSWORD_FILE%...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$pw = -join ((48..57) + (65..90) + (97..122) | Get-Random -Count 32 | ForEach-Object {[char]$_});" ^
    "[IO.File]::WriteAllText('%CD%\%DB_PASSWORD_FILE%', $pw)"
  if errorlevel 1 (
    echo [ERROR] Could not generate database password file.
    goto :fail
  )
)

if not exist "%TOKEN_FILE%" (
  echo [INFO] Creating empty %TOKEN_FILE% (tokens come from .env^).
  type nul > "%TOKEN_FILE%"
)

echo [INFO] Checking .env profile / guild...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ErrorActionPreference='Stop';" ^
  "$lines = Get-Content -LiteralPath '.env' -Encoding UTF8;" ^
  "$map = @{}; foreach ($l in $lines) { if ($l -match '^\s*#' -or $l -match '^\s*$') { continue }; if ($l -match '^([^=]+)=(.*)$') { $map[$Matches[1].Trim()] = $Matches[2].Trim() } };" ^
  "$profile = if ($map['BOT_PROFILE']) { $map['BOT_PROFILE'].ToLower() } else { 'test' };" ^
  "if ($profile -in @('family','prod','production')) { $profile = 'main' };" ^
  "if ($profile -in @('dev','development')) { $profile = 'test' };" ^
  "$guildKey = if ($profile -eq 'main') { 'GUILD_ID_MAIN' } else { 'GUILD_ID_TEST' };" ^
  "$tokenKey = if ($profile -eq 'main') { 'TOKEN_MAIN' } else { 'TOKEN_TEST' };" ^
  "$guild = $map[$guildKey]; if (-not $guild) { $guild = $map['GUILD_ID'] };" ^
  "$token = $map[$tokenKey]; if (-not $token) { $token = $map['TOKEN'] };" ^
  "Write-Host ('[INFO] BOT_PROFILE=' + $profile);" ^
  "if (-not $token) { Write-Host ('[ERROR] Missing ' + $tokenKey + ' (or TOKEN) in .env'); exit 2 };" ^
  "if (-not $guild) { Write-Host ('[ERROR] Missing ' + $guildKey + ' in .env — set Discord Server ID before start'); exit 3 };" ^
  "Write-Host ('[OK] ' + $guildKey + ' and token are set.');"
if errorlevel 1 (
  echo [ERROR] Fix .env and run docker-start.bat again.
  goto :fail
)

REM Resolve BOT_CONFIG path from compose.env (default example toml).
set "BOT_CONFIG_HOST_PATH="
for /f "usebackq tokens=1* delims==" %%A in (`findstr /B /I "BOT_CONFIG_HOST_PATH=" "%COMPOSE_ENV%" 2^>nul`) do (
  set "BOT_CONFIG_HOST_PATH=%%B"
)
if not defined BOT_CONFIG_HOST_PATH set "BOT_CONFIG_HOST_PATH=./deploy/bot_config.example.toml"
set "BOT_CONFIG_HOST_PATH=!BOT_CONFIG_HOST_PATH:/=\!"
if "!BOT_CONFIG_HOST_PATH:~0,2!"==".\" (
  rem ok relative
) else if "!BOT_CONFIG_HOST_PATH:~0,2!"=="./" (
  set "BOT_CONFIG_HOST_PATH=.\!BOT_CONFIG_HOST_PATH:~2!"
)
if not exist "!BOT_CONFIG_HOST_PATH!" (
  echo [WARN] BOT_CONFIG_HOST_PATH not found: !BOT_CONFIG_HOST_PATH!
  echo        Compose will fail until the TOML path in deploy\compose.env is fixed.
)

echo [INFO] Validating Compose config...
docker compose --env-file "%COMPOSE_ENV%" -f "%COMPOSE_FILE%" config --quiet
if errorlevel 1 (
  echo [ERROR] Compose config is invalid. Check deploy\compose.env paths and secrets.
  goto :fail
)

echo [INFO] Building and starting db + bot...
docker compose --env-file "%COMPOSE_ENV%" -f "%COMPOSE_FILE%" up -d --build --remove-orphans
if errorlevel 1 (
  echo [ERROR] Failed to start containers.
  goto :fail
)

echo.
echo [INFO] Waiting for PostgreSQL health...
set "PG_USER=guinness"
set "PG_DB=guinness_bot"
for /f "usebackq tokens=1* delims==" %%A in (`findstr /B /I "POSTGRES_USER=" "%COMPOSE_ENV%" 2^>nul`) do set "PG_USER=%%B"
for /f "usebackq tokens=1* delims==" %%A in (`findstr /B /I "POSTGRES_DB=" "%COMPOSE_ENV%" 2^>nul`) do set "PG_DB=%%B"
set "READY=0"
for /L %%I in (1,1,30) do (
  docker compose --env-file "%COMPOSE_ENV%" -f "%COMPOSE_FILE%" exec -T db pg_isready -U !PG_USER! -d !PG_DB! >nul 2>&1
  if not errorlevel 1 (
    set "READY=1"
    goto :db_ready
  )
  timeout /t 2 /nobreak >nul
)
:db_ready
if "!READY!"=="0" (
  echo [WARN] Database health check timed out — see logs below.
) else (
  echo [OK] PostgreSQL is ready.
)

echo.
echo [INFO] Container status:
docker compose --env-file "%COMPOSE_ENV%" -f "%COMPOSE_FILE%" ps
echo.
echo [INFO] Last bot logs:
docker compose --env-file "%COMPOSE_ENV%" -f "%COMPOSE_FILE%" logs --tail 40 bot
echo.
echo ----------------------------------------
echo  Useful commands:
echo    docker compose --env-file deploy\compose.env logs -f bot
echo    docker compose --env-file deploy\compose.env ps
echo    docker-stop.bat
echo ----------------------------------------
echo.
echo Done.
exit /b 0

:fail
echo.
echo Failed. Fix the error above and run docker-start.bat again.
echo.
pause
exit /b 1
