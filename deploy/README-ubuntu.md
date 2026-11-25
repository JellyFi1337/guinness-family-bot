# Guinness Bot + PostgreSQL 18 на Ubuntu 24.04

Инструкция рассчитана на каталог `/home/guinnes_bot`, PostgreSQL на том же
сервере и системного пользователя `guinnes_bot`. Порт `5432` наружу не
открывается; Windows/PyCharm подключается через SSH-туннель.

## 1. Установить PostgreSQL и системные пакеты

```bash
sudo apt update
sudo apt install -y \
  postgresql-common ca-certificates curl \
  python3 python3-venv python3-pip build-essential ffmpeg

sudo /usr/share/postgresql-common/pgdg/apt.postgresql.org.sh
sudo apt update
sudo apt install -y postgresql-18 postgresql-client-18
sudo systemctl enable --now postgresql
```

Проверка:

```bash
sudo -u postgres psql -tAc "SHOW server_version;"
sudo -u postgres psql -tAc "SHOW listen_addresses;"
sudo ss -lntp | grep 5432
```

PostgreSQL должен слушать только `127.0.0.1`/`::1`. Не выполняйте
`ufw allow 5432`.

## 2. Создать роль и чистую базу

Сгенерируйте URL-safe пароль и сохраните его в менеджере паролей:

```bash
openssl rand -hex 32
```

Откройте консоль:

```bash
sudo -u postgres psql
```

Выполните:

```sql
CREATE ROLE guinnes_bot LOGIN;
\password guinnes_bot

CREATE DATABASE guinnes_bot
    OWNER guinnes_bot
    ENCODING 'UTF8'
    TEMPLATE template0;

REVOKE ALL ON DATABASE guinnes_bot FROM PUBLIC;
GRANT CONNECT, TEMPORARY ON DATABASE guinnes_bot TO guinnes_bot;
\q
```

Проверка:

```bash
psql -h 127.0.0.1 -U guinnes_bot -d guinnes_bot -c "SELECT version();"
```

## 3. Создать пользователя сервиса

```bash
sudo useradd --system --user-group \
  --home-dir /home/guinnes_bot \
  --shell /usr/sbin/nologin \
  guinnes_bot
```

Если пользователь уже существует, продолжайте. После клонирования проекта:

```bash
sudo chown -R guinnes_bot:guinnes_bot /home/guinnes_bot
```

## 4. Настроить секреты и обычные параметры

```bash
cd /home/guinnes_bot
sudo -u guinnes_bot cp .env.example .env
sudo -u guinnes_bot nano .env
```

В `.env` должны быть только секреты:

```dotenv
TOKEN=DISCORD_BOT_TOKEN
DATABASE_URL=postgresql://guinnes_bot:ПАРОЛЬ@127.0.0.1:5432/guinnes_bot
```

Для пароля из `openssl rand -hex 32` URL-кодирование не требуется.

Каналы, категории, роли, `GUILD_ID`, `BOT_OWNER_ID`, лимиты, статус и размер
пула PostgreSQL настраиваются в отдельном файле:

```bash
sudo -u guinnes_bot nano /home/guinnes_bot/bot_config.toml
```

```bash
sudo chown guinnes_bot:guinnes_bot /home/guinnes_bot/.env
sudo chmod 600 /home/guinnes_bot/.env
```

Проект загружает `.env` через `python-dotenv` и откажется запускаться, если в
нём оказались обычные настройки. На Linux права шире `0600` также считаются
ошибкой. Systemd задаёт `DATABASE_REQUIRE_POSTGRES=true`, поэтому отсутствие
`DATABASE_URL` завершит запуск ошибкой вместо создания локальной SQLite-базы.

`SHOWDOWN_STATE_PATH` направлен в `/var/lib/guinnes-bot/showdown_state.json`.
Каталог автоматически создаётся через `StateDirectory=`.

## 5. Установить Python-зависимости

```bash
sudo -u guinnes_bot python3 -m venv /home/guinnes_bot/.venv
sudo -u guinnes_bot \
  /home/guinnes_bot/.venv/bin/python -m pip install --upgrade pip wheel
sudo -u guinnes_bot \
  /home/guinnes_bot/.venv/bin/pip install --require-hashes \
  -r /home/guinnes_bot/requirements.lock
```

`requirements.lock` закрепляет точные версии и хеши — ровно ту комбинацию
библиотек, которую проверяет CI. Не устанавливайте `requirements.txt` на
сервере: там диапазоны версий, и `yt-dlp` обновляется еженедельно, поэтому
в прод попадёт непротестированный набор.

Проверка:

```bash
sudo -u guinnes_bot /home/guinnes_bot/.venv/bin/pip check
sudo -u guinnes_bot /home/guinnes_bot/.venv/bin/python -c \
  "import discord, psycopg, psycopg_pool; print(discord.__version__, psycopg.__version__)"
ffmpeg -version
```

## 6. Инициализировать схему

Это создаёт чистую PostgreSQL-схему и применяет все версионные миграции:

```bash
cd /home/guinnes_bot
sudo -u guinnes_bot \
  /home/guinnes_bot/.venv/bin/python -c \
  "import asyncio; from utils.database import init_db, close_db; r=asyncio.Runner(); r.run(init_db()); r.run(close_db()); r.close()"
```

Проверить миграции:

```bash
psql -h 127.0.0.1 -U guinnes_bot -d guinnes_bot \
  -c "SELECT version, name FROM schema_migrations ORDER BY version;"
```

Старый `guinness_bot.db` не импортируется и боту больше не нужен.

## 7. Установить systemd-unit

```bash
sudo install -o root -g root -m 0644 \
  /home/guinnes_bot/deploy/guinnes-bot.service \
  /etc/systemd/system/guinnes-bot.service

sudo systemd-analyze verify /etc/systemd/system/guinnes-bot.service
sudo systemctl daemon-reload
sudo systemctl enable --now guinnes-bot.service
```

Проверка:

```bash
sudo systemctl status guinnes-bot.service
sudo journalctl -u guinnes-bot.service -n 100 --no-pager
sudo journalctl -u guinnes-bot.service -f
```

Нормальный статус — `active (running)`. Основной лог — stdout в journald.
Файлы по умолчанию выключены. Чтобы писать их на диск:

```bash
sudo mkdir -p /var/lib/guinnes-bot/logs
sudo chown guinnes_bot:guinnes_bot /var/lib/guinnes-bot/logs
```

В `bot_config.toml` секция `[logging]`:

```toml
LOG_DIR = "/var/lib/guinnes-bot/logs"
```

Затем `sudo systemctl restart guinnes-bot.service`. Общий лог — `bot.log`,
ошибки с traceback — `bot.error.log`. Если каталог недоступен для записи,
бот пишет предупреждение с кодом `LOG_DIR_UNWRITABLE` и продолжает через stdout.
Не указывайте `./logs` в корне проекта: юнит не может туда писать.

## Подключение из Windows/PyCharm

Откройте PowerShell и держите туннель запущенным:

```powershell
ssh `
  -N `
  -o ExitOnForwardFailure=yes `
  -o ServerAliveInterval=30 `
  -L 127.0.0.1:55432:127.0.0.1:5432 `
  SERVER_USER@SERVER_IP
```

Локальный `.env` Windows:

```dotenv
DATABASE_URL=postgresql://guinnes_bot:ПАРОЛЬ@127.0.0.1:55432/guinnes_bot
```

Windows и Ubuntu используют одинаковый `bot_config.toml`.
Запускайте только один экземпляр Discord-бота: либо systemd, либо PyCharm.

## Обновление

```bash
cd /home/guinnes_bot
sudo systemctl stop guinnes-bot.service

sudo -u postgres pg_dump \
  --format=custom \
  --file="/var/lib/postgresql/guinnes_bot_$(date +%Y-%m-%d_%H-%M-%S).dump" \
  guinnes_bot

git pull --ff-only origin master

# Безопасно выполнять при каждом обновлении. При первом переходе команда
# перенесёт старые каналы/роли из .env; затем будет переносить 0 параметров.
sudo -u guinnes_bot \
  /home/guinnes_bot/.venv/bin/python \
  /home/guinnes_bot/scripts/migrate_env_to_toml.py

sudo -u guinnes_bot \
  /home/guinnes_bot/.venv/bin/pip install --require-hashes \
  -r /home/guinnes_bot/requirements.lock
sudo -u guinnes_bot /home/guinnes_bot/.venv/bin/pip check

sudo install -o root -g root -m 0644 \
  deploy/guinnes-bot.service \
  /etc/systemd/system/guinnes-bot.service
sudo systemctl daemon-reload
sudo systemctl start guinnes-bot.service
sudo journalctl -u guinnes-bot.service -n 50 --no-pager
```

Схема обновляется автоматически при запуске. Не изменяйте уже применённые
миграции задним числом: checksum остановит запуск. Мигратор сам выставляет
для `.env` права `0600`.

Перед отправкой изменений в Git:

```bash
python scripts/check_secrets.py --mode staged
```

## Управление

```bash
sudo systemctl restart guinnes-bot.service
sudo systemctl stop guinnes-bot.service
sudo systemctl start guinnes-bot.service
sudo systemctl disable --now guinnes-bot.service
```
