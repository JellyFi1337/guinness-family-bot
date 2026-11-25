# Тестовый запуск Guinness Bot на Discord-сервере

Дата: 09.09.2026. Краткая инструкция для smoke-теста на **отдельной** тестовой гильдии.
Полная структура каналов/ролей: [DISCORD_SERVER_SETUP.md](DISCORD_SERVER_SETUP.md).
Полная приёмка T01–T30: [OPERATOR_RUNBOOK.md](OPERATOR_RUNBOOK.md).

## Важно до старта

- Один процесс бота = один Discord-токен = один активный `GUILD_ID` из `.env`. Не запускайте два экземпляра с одним токеном.
- Сервер, которого нет в `.env` для текущего `BOT_PROFILE`, бот **покидает автоматически** (при старте, после resume и при invite).
- Переключатель в `.env`: `BOT_PROFILE=test` или `BOT_PROFILE=main` (алиасы `family`/`prod` → main).
- `.env` — единственный источник identity: `BOT_PROFILE`, `TOKEN_*`, `GUILD_ID_*`, `BOT_OWNER_ID`, `COMMAND_*`.
- Эти ключи **запрещены** в `bot_config.toml` (каналы/роли/экономика остаются в TOML).
- Автосоздание «закрытых» каналов в `/setup` пока ненадёжно (R06–R09). **Создавайте структуру вручную и привязывайте по ID.**
- Денежные/казначейские сценарии на этой ревизии — только smoke; не принимайте реальные выплаты.

---

## A. Что настроить в Discord (бот сам этого не сделает)

### A1. Developer Portal

1. [Discord Developer Portal](https://discord.com/developers/applications) → New Application → Bot (отдельно для test и main).
2. Включите privileged intents: **Server Members Intent** и **Message Content Intent**.
3. В `.env` заполните профиль (токен в чат не отправляйте):

   ```env
   BOT_PROFILE=test
   TOKEN_TEST=...ваш тестовый токен...
   GUILD_ID_TEST=...ID тестового сервера...
   BOT_OWNER_ID=...ваш User ID...
   ```

   Для семейного основного сервера: `BOT_PROFILE=main`, `TOKEN_MAIN`, `GUILD_ID_MAIN`.
4. Installation → Guild Install, scopes: `bot` + `applications.commands`.
5. Пригласите **соответствующего** бота только на нужный сервер (test→тест, main→семья).

### A2. Права роли бота на сервере

Выдайте роли бота (не полагайтесь на Administrator как постоянный режим):

| Обязательно для smoke | Для рекрутинга / инвайтов |
| --- | --- |
| View Channel, Send Messages, Embed Links, Attach Files, Read Message History | Manage Server (Manage Guild) |
| Manage Messages, Manage Channels, Manage Roles | Create Invite |
| Manage Threads, Create Public Threads, Send Messages in Threads | |
| Connect, Speak, Move Members | |

Роль бота должна стоять **выше** ролей, которые он выдаёт: Guest, Young, Main, Friend, Super Friend, Help, Contract, VIP и покупные роли магазина. Owner / Dep. Owner можно оставить выше бота.

### A3. Интеграции (slash-команды)

**Настройки сервера → Интеграции → ваш бот.**  
У многих групп в коде стоит `default_permissions=Permissions.none()` — по умолчанию команды скрыты. Разрешите нужные группы ролям Owner/High/специалистам. Это **отдельно** от внутренних permission grants бота.

### A4. Минимальная структура сервера (создать вручную)

Включите Developer Mode (Discord → Advanced), чтобы копировать ID.

**Категории (минимум для пилота):**

| Имя | Кто видит | Зачем |
| --- | --- | --- |
| `🌐・PUBLIC・🌐` | @everyone | заявки, вход |
| `🏠・FAMILY・🏠` | Main/Young (+нужные), не весь everyone | панели семьи, события, помощь |
| `🔰・HIGH TOOLS ・🔰` | только бот + Owner/Dep. Owner/High | очередь, финансы, аудит, логи заявок |
| `🏷️・TICKETS・🏷️` | закрыта для everyone; тикеты открывает бот | обращения |

**Как закрыть служебную категорию:** у @everyone запретить View Channel; разрешить боту и ролям High/Owner. Проверяйте участником **без** Administrator.

**Каналы минимума:**

| Канал | Категория | Назначение |
| --- | --- | --- |
| `🐳・заявки-в-семью` | PUBLIC | кнопка заявки |
| `старт-и-правила` | FAMILY | семейные панели / `/me` |
| `помощь` | FAMILY | вызов помощи |
| `события` | FAMILY | события |
| `отчёты` | FAMILY | панель контрактов |
| `🛟・help` | FAMILY или HIGH TOOLS | доставка Help |
| `очередь-сс` | HIGH TOOLS | staff queue (не светить Main) |
| `📋・заявки-в-семью` | HIGH TOOLS | логи заявок |
| `выплаты` | HIGH TOOLS (узкий overwrite) | казна |
| `💱・транзакции` | HIGH TOOLS | лог транзакций |
| `аудит` | HIGH TOOLS | действия / решения |

**Роли минимума:** `Owner`, `Dep. Owner`, `High`, `Guest`, `Young`, `Main`.  
По желанию: `Friend`, `Super Friend`, `Help`, `Contract`, `🪙 VIP`.  
Не создавайте устаревшие VZP-роли на новом сервере.

**Иерархия ролей (сверху вниз, упрощённо):** Owner → Dep. Owner → **роль бота** → High → Main → Young → Guest → остальные.

### A5. Данные, которые бот не выдумает

После старта человек заполняет: имя семьи, сезон, timezone, направление, улучшения, лимит состава, каталог контрактов, правила взносов/премий, каталог склада. Без этого часть `/event` / `/contract` / склада останется пустой — это нормально для первого smoke.

### A6. Тестировщики

Владелец с Administrator **не** проверяет ACL. Нужны 3–4 аккаунта: Guest, Main, High/кадровик, (опционально) казначей отдельно от согласующего.

---

## B. Что уже подготовлено в репозитории

Локально (не в Git):

| Файл | Статус |
| --- | --- |
| `.env` | шаблон с `BOT_PROFILE`, `TOKEN_*`, `GUILD_ID_*` — **заполните `GUILD_ID_TEST` и отдельный `TOKEN_TEST`** |
| `deploy/compose.env` | volume prefix `guinness-test`, монтирует `.env` |
| `deploy/bot_config.test.toml` | каналы/роли (GUILD_ID из `.env` важнее) |
| `secrets/db_password` | сгенерирован |
| `secrets/discord_token` | может быть пустым — токены берутся из `.env` |

---

## C. Запуск

### Windows — BAT (рекомендуется)

Из корня репозитория (двойной клик или из cmd):

```bat
docker-start.bat
docker-status.bat
docker-stop.bat
```

`docker-start.bat` поднимает PostgreSQL + bot, ждёт healthcheck БД и показывает логи.  
`docker-stop.bat` гасит стек Guinness и все остальные запущенные контейнеры Docker на машине.  
Тома Postgres **не** удаляются (данные сохраняются).

### Вариант 1 — Docker CLI

1. В `.env` для теста:

   ```env
   BOT_PROFILE=test
   TOKEN_TEST=...
   GUILD_ID_TEST=...
   BOT_OWNER_ID=...
   ```

2. В PowerShell из корня репозитория:

   ```powershell
   docker compose --env-file deploy/compose.env config --quiet
   docker compose --env-file deploy/compose.env up -d --build
   docker compose --env-file deploy/compose.env ps
   docker compose --env-file deploy/compose.env logs --tail 100 bot
   ```

Ожидается: `db` healthy, `bot` Up, в логе `Discord profile=test guild_id=...`, без `TOKEN_FILE file is empty`.

Переключение на семейный сервер: в `.env` поставьте `BOT_PROFILE=main` (и заполненные `TOKEN_MAIN` / `GUILD_ID_MAIN`), затем `docker compose ... up -d` (пересоздаст bot).

### Вариант 2 — локальный Python + PostgreSQL

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install --require-hashes -r requirements.lock
# .env: BOT_PROFILE + TOKEN_* + GUILD_ID_* + DATABASE_URL
python main.py
```

---

## D. Настройка после онлайн

1. Создайте роль `High`, выдайте себе, заполните в TOML/`/config` списки `[access]` (`HIGH_TOOLS_ALLOWED_ROLE_IDS` и др.), **один раз перезапустите** бота (посев grants текущего кода).
2. `/setup start` → профиль → структура → роли → полномочия → панели. Для уже созданных объектов выбирайте **привязать**, не «создать».
3. `/admin preflight` — разберите каждую critical-строку.
4. `/admin setup` или `/panels deploy` — публикация панелей.
5. Smoke без денег: `/help overview`, `/me`, заявка с Guest, тикет, видимость HIGH TOOLS с Main (должна быть закрыта).

Остановка Docker:

```powershell
docker compose --env-file deploy/compose.env down
```

Данные БД остаются в volume `guinness-test_*`, пока не сделаете `down -v`.
