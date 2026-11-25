import asyncio
import json
from collections import Counter
from datetime import UTC, datetime, timedelta

import aiosqlite

from config import DATABASE_URL, DB_PATH, GUILD_ID
from infrastructure.database import (
    DATABASE_OPERATIONAL_ERRORS,
    checkpoint_database,
    close_database,
    configure_database,
    connection as _connect,
    database_backend,
    database_status,
    database_target,
    utc_now as _utc_now,
)
from infrastructure.transactions import begin_write
from infrastructure.migrations import apply_migrations, backup_before_schema


DEFAULT_COUNTERS = ("AB", "AR", "ANR", "HR", "VZP", "BT", "FI", "RR", "TICKET")
_INIT_LOCK = asyncio.Lock()
_INITIALIZED_PATH: str | None = None


async def _initialize_database() -> None:
    """Create every table in one migration-style transaction."""
    if database_backend() == "sqlite":
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        await backup_before_schema(DB_PATH)
    async with _connect() as db:
        await db.execute("PRAGMA journal_mode = WAL")
        await db.executescript(
            """
            CREATE TABLE IF NOT EXISTS counters (
                type TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0)
            );
            CREATE TABLE IF NOT EXISTS birthdays (
                user_id INTEGER PRIMARY KEY,
                date TEXT NOT NULL,
                name TEXT,
                added_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                number TEXT NOT NULL,
                type TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                content TEXT NOT NULL,
                created_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                reviewer_id INTEGER,
                reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ticket_counters (
                type TEXT PRIMARY KEY,
                count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0)
            );
            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS birthday_fundraisers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_user_id INTEGER NOT NULL,
                target_username TEXT NOT NULL,
                channel_id INTEGER NOT NULL UNIQUE,
                total_amount INTEGER NOT NULL DEFAULT 0 CHECK (total_amount >= 0),
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS fundraiser_contributions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fundraiser_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                contributed_at TEXT NOT NULL,
                FOREIGN KEY (fundraiser_id) REFERENCES birthday_fundraisers(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS treasury (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                balance INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS treasury_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount != 0),
                action TEXT NOT NULL CHECK (action IN ('add', 'remove')),
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS treasury_counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                count INTEGER NOT NULL DEFAULT 0 CHECK (count >= 0)
            );
            CREATE TABLE IF NOT EXISTS summit_vzp_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                capacity INTEGER NOT NULL CHECK (capacity > 0),
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                thread_id INTEGER,
                creator_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summit_vzp_positions (
                list_id INTEGER NOT NULL,
                position INTEGER NOT NULL,
                user_id INTEGER,
                username TEXT,
                PRIMARY KEY (list_id, position),
                FOREIGN KEY (list_id) REFERENCES summit_vzp_lists(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS summit_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                capacity INTEGER NOT NULL CHECK (capacity > 0),
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                creator_id INTEGER NOT NULL,
                is_open INTEGER NOT NULL DEFAULT 1,
                kind TEXT NOT NULL DEFAULT 'list',
                selected_map TEXT,
                selected_map_path TEXT,
                approved INTEGER NOT NULL DEFAULT 0,
                thread_id INTEGER,
                position_list_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS summit_list_members (
                list_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                priority INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (list_id, user_id),
                FOREIGN KEY (list_id) REFERENCES summit_lists(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS summit_list_reserves (
                list_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                username TEXT NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (list_id, user_id),
                FOREIGN KEY (list_id) REFERENCES summit_lists(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS voice_rooms (
                channel_id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                message_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS voice_owner_bans (
                owner_id INTEGER NOT NULL,
                banned_user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (owner_id, banned_user_id)
            );
            CREATE TABLE IF NOT EXISTS cars (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                plate TEXT NOT NULL UNIQUE,
                owner TEXT NOT NULL,
                added_by INTEGER NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS car_bookings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                car_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                ic_name TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                cancelled_at TEXT,
                cancelled_by INTEGER,
                FOREIGN KEY (car_id) REFERENCES cars(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS stats_messages (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                deleted_at TEXT,
                family_visible INTEGER
            );
            CREATE TABLE IF NOT EXISTS stats_member_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ('join', 'leave')),
                event_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stats_voice_sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                family_visible INTEGER
            );
            CREATE TABLE IF NOT EXISTS stats_channel_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type IN ('create', 'delete')),
                event_at TEXT NOT NULL,
                family_visible INTEGER
            );
            CREATE TABLE IF NOT EXISTS stats_processed_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                action_kind TEXT NOT NULL CHECK (action_kind IN ('ticket', 'application')),
                outcome TEXT NOT NULL,
                actor_id INTEGER,
                processed_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS coin_accounts (
                user_id INTEGER PRIMARY KEY,
                balance INTEGER NOT NULL DEFAULT 0 CHECK (balance >= 0),
                last_daily TEXT,
                vip_until TEXT,
                summit_emoji TEXT
            );
            CREATE TABLE IF NOT EXISTS coin_transactions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                action TEXT NOT NULL,
                related_user_id INTEGER,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS purchased_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price > 0),
                role_kind TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                purchased_at TEXT NOT NULL,
                sold_at TEXT
            );
            CREATE TABLE IF NOT EXISTS role_purchase_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                primary_color INTEGER NOT NULL,
                secondary_color INTEGER,
                tertiary_color INTEGER,
                price INTEGER NOT NULL CHECK (price > 0),
                status TEXT NOT NULL DEFAULT 'pending',
                message_id INTEGER,
                reviewer_id INTEGER,
                created_at TEXT NOT NULL,
                reviewed_at TEXT
            );
            CREATE TABLE IF NOT EXISTS ticket_assignments (
                channel_id INTEGER PRIMARY KEY,
                owner_id INTEGER NOT NULL,
                ticket_number TEXT NOT NULL,
                assigned_user_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS giveaways (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                prize TEXT NOT NULL,
                winner_count INTEGER NOT NULL CHECK (winner_count > 0),
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                creator_id INTEGER NOT NULL,
                ends_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                finished_at TEXT
            );
            CREATE TABLE IF NOT EXISTS giveaway_entries (
                giveaway_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (giveaway_id, user_id),
                FOREIGN KEY (giveaway_id) REFERENCES giveaways(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS recruiter_invites (
                guild_id INTEGER NOT NULL,
                recruiter_id INTEGER NOT NULL,
                invite_code TEXT NOT NULL,
                invite_url TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, recruiter_id),
                UNIQUE (guild_id, invite_code)
            );
            CREATE TABLE IF NOT EXISTS recruiting_referrals (
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                recruiter_id INTEGER NOT NULL,
                invite_code TEXT,
                joined_at TEXT NOT NULL,
                application_number TEXT,
                ic_name TEXT,
                applied_at TEXT,
                status TEXT NOT NULL DEFAULT 'joined'
                    CHECK (status IN ('joined', 'applied', 'accepted', 'rejected')),
                accepted_at TEXT,
                rejected_at TEXT,
                approved_by INTEGER,
                PRIMARY KEY (guild_id, member_id)
            );
            CREATE INDEX IF NOT EXISTS idx_stats_messages_period
                ON stats_messages (guild_id, created_at);
            CREATE INDEX IF NOT EXISTS idx_stats_voice_period
                ON stats_voice_sessions (guild_id, started_at, ended_at);
            CREATE INDEX IF NOT EXISTS idx_stats_members_period
                ON stats_member_events (guild_id, event_at);
            CREATE INDEX IF NOT EXISTS idx_stats_processed_period
                ON stats_processed_actions (guild_id, processed_at);
            CREATE INDEX IF NOT EXISTS idx_stats_channels_period
                ON stats_channel_events (guild_id, event_at);
            CREATE INDEX IF NOT EXISTS idx_stats_voice_active
                ON stats_voice_sessions (guild_id, user_id, ended_at);
            CREATE INDEX IF NOT EXISTS idx_car_bookings_active
                ON car_bookings (car_id, cancelled_at, ends_at);
            CREATE INDEX IF NOT EXISTS idx_car_bookings_user_active
                ON car_bookings (user_id, cancelled_at, ends_at);
            CREATE INDEX IF NOT EXISTS idx_purchased_roles_user_active
                ON purchased_roles (user_id, active);
            CREATE INDEX IF NOT EXISTS idx_purchased_roles_active_user
                ON purchased_roles (active, user_id);
            CREATE INDEX IF NOT EXISTS idx_role_requests_user_status
                ON role_purchase_requests (user_id, status);
            CREATE INDEX IF NOT EXISTS idx_role_requests_status_message
                ON role_purchase_requests (status, message_id);
            CREATE INDEX IF NOT EXISTS idx_coin_accounts_vip_until
                ON coin_accounts (vip_until);
            CREATE INDEX IF NOT EXISTS idx_giveaways_status_end
                ON giveaways (status, ends_at);
            CREATE INDEX IF NOT EXISTS idx_recruiting_recruiter
                ON recruiting_referrals (guild_id, recruiter_id, accepted_at);
            CREATE INDEX IF NOT EXISTS idx_recruiting_status
                ON recruiting_referrals (guild_id, status, applied_at);
            """
        )
        await db.executemany(
            "INSERT OR IGNORE INTO counters (type, count) VALUES (?, 0)",
            ((counter,) for counter in DEFAULT_COUNTERS),
        )
        columns = {
            column[1] for column in await db.execute_fetchall("PRAGMA table_info(summit_lists)")
        }
        migrations = {
            "kind": "TEXT NOT NULL DEFAULT 'list'",
            "selected_map": "TEXT",
            "selected_map_path": "TEXT",
            "approved": "INTEGER NOT NULL DEFAULT 0",
            "thread_id": "INTEGER",
            "position_list_id": "INTEGER",
            "launched": "INTEGER NOT NULL DEFAULT 0",
            "launched_at": "TEXT",
        }
        for column, definition in migrations.items():
            if column not in columns:
                await db.execute(
                    f"ALTER TABLE summit_lists ADD COLUMN {column} {definition}"
                )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_summit_launch_period "
            "ON summit_lists (launched, launched_at, kind, creator_id)"
        )
        request_columns = {
            column[1]
            for column in await db.execute_fetchall("PRAGMA table_info(requests)")
        }
        request_migrations = {
            "status": "TEXT NOT NULL DEFAULT 'pending'",
            "reviewer_id": "INTEGER",
            "reviewed_at": "TEXT",
        }
        for column, definition in request_migrations.items():
            if column not in request_columns:
                await db.execute(
                    f"ALTER TABLE requests ADD COLUMN {column} {definition}"
                )
        await db.execute(
            "CREATE INDEX IF NOT EXISTS idx_requests_number_status "
            "ON requests (number, status)"
        )
        await db.execute("INSERT OR IGNORE INTO treasury (id, balance) VALUES (1, 0)")
        await db.execute("INSERT OR IGNORE INTO treasury_counter (id, count) VALUES (1, 0)")
        await db.commit()
        await apply_migrations(db, DB_PATH, create_backup=False)


async def init_db(*, persistent: bool = False) -> None:
    """Initialize the schema once per process, even when every cog requests it."""
    global _INITIALIZED_PATH
    configure_database(
        DB_PATH,
        database_url=DATABASE_URL,
        persistent=True if persistent else None,
    )
    current_path = database_target()
    target_exists = database_backend() == "postgresql" or DB_PATH.exists()
    if _INITIALIZED_PATH == current_path and target_exists:
        return
    async with _INIT_LOCK:
        target_exists = database_backend() == "postgresql" or DB_PATH.exists()
        if _INITIALIZED_PATH == current_path and target_exists:
            return
        try:
            await _initialize_database()
        except DATABASE_OPERATIONAL_ERRORS as exc:
            await close_database()
            raise RuntimeError(
                f"Не удалось открыть базу данных {current_path}. "
                "Проверьте DATABASE_URL/DB_PATH, доступность сервера и права."
            ) from exc
        except BaseException:
            await close_database()
            raise
        _INITIALIZED_PATH = current_path


async def close_db() -> None:
    """Close database pools during graceful shutdown or reconfiguration."""

    await close_database()


async def checkpoint_db(*, truncate: bool = False) -> None:
    """Maintain query statistics (or checkpoint the SQLite test fallback)."""

    await checkpoint_database(truncate=truncate)


def get_database_pool_status() -> dict[str, int | bool | str]:
    return database_status()


async def _increment_counter(table: str, counter_type: str) -> int:
    if table not in {"counters", "ticket_counters"}:
        raise ValueError("Unsupported counter table")
    counter_type = counter_type.strip().upper()
    if not counter_type:
        raise ValueError("Counter type cannot be empty")

    async with _connect() as db:
        # Ticket and request numbers are the most contended write in the bot:
        # every form submission takes one. Under the old global advisory lock
        # this serialised every unrelated write in the process. A single
        # upsert-and-return is atomic on its own row, so concurrent callers now
        # queue only behind each other — and only per counter type.
        await begin_write(db, "counter", f"{table}:{counter_type}")
        try:
            await db.execute(
                f"""INSERT INTO {table} (type, count) VALUES (?, 1)
                    ON CONFLICT(type) DO UPDATE SET count = {table}.count + 1""",
                (counter_type,),
            )
            async with db.execute(
                f"SELECT count FROM {table} WHERE type = ?", (counter_type,)
            ) as cursor:
                row = await cursor.fetchone()
            await db.commit()
        except Exception:
            await db.rollback()
            raise
    return row[0]


async def get_next_number(counter_type: str) -> str:
    counter_type = counter_type.strip().upper()
    count = await _increment_counter("counters", counter_type)
    return f"{counter_type}-{count:04d}"


async def get_ticket_number(ticket_type: str) -> str:
    count = await _increment_counter("ticket_counters", ticket_type)
    return f"{count:04d}"


async def release_request_review(number: str, reviewer_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE requests SET status = 'pending', reviewer_id = NULL,
                                   review_token = NULL, review_lease_until = NULL
               WHERE id = (
                   SELECT id FROM requests
                   WHERE number = ? AND status = 'processing'
                     AND desired_outcome IS NULL
                   ORDER BY id DESC LIMIT 1
               ) AND reviewer_id = ?""",
            (number, reviewer_id),
        )
        await db.commit()


async def get_contributions(fundraiser_id: int):
    async with _connect() as db:
        async with db.execute(
            """SELECT user_id, MAX(username), SUM(amount)
               FROM fundraiser_contributions WHERE fundraiser_id = ?
               GROUP BY user_id ORDER BY MIN(id)""",
            (fundraiser_id,),
        ) as cursor:
            return await cursor.fetchall()


# Rows migrated out of the old global ``settings`` table land under this guild
# id, because migration 32 has no way to know which guild they belonged to.
LEGACY_SETTINGS_GUILD_ID = 0


def _settings_guild_id() -> int:
    """The guild every "global" setting actually belongs to.

    GUILD_ID is mandatory and the bot leaves any other server, so a setting
    without a guild has exactly one possible owner. Keeping two tables for the
    same concept is what let the weekly-statistics message id and its week be
    stored in different places.
    """

    return int(GUILD_ID or LEGACY_SETTINGS_GUILD_ID)


async def set_setting(key: str, value: str) -> None:
    """Store a guild-scoped setting under the configured guild."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "guild-setting", key)
        try:
            await db.execute(
                """INSERT INTO guild_settings (guild_id, key, value, updated_at)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(guild_id, key) DO UPDATE SET
                       value = excluded.value,
                       updated_at = excluded.updated_at""",
                (_settings_guild_id(), key, value, now),
            )
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def get_setting(key: str) -> str | None:
    """Read a setting, adopting any value left behind by the old table.

    Reads prefer the configured guild. A value still parked under the legacy
    guild id is returned and re-homed on the spot, so the fallback disappears
    on its own after the first read of each key rather than needing a
    one-shot script that someone has to remember to run.
    """

    guild_id = _settings_guild_id()
    async with _connect() as db:
        rows = await db.execute_fetchall(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (guild_id, key),
        )
        if rows:
            return str(rows[0][0])
        if guild_id == LEGACY_SETTINGS_GUILD_ID:
            return None
        legacy = await db.execute_fetchall(
            "SELECT value FROM guild_settings WHERE guild_id = ? AND key = ?",
            (LEGACY_SETTINGS_GUILD_ID, key),
        )
    if not legacy:
        return None
    value = str(legacy[0][0])
    await set_setting(key, value)
    return value


async def upsert_recruiter_invite(
    guild_id: int,
    recruiter_id: int,
    invite_code: str,
    invite_url: str,
    channel_id: int,
) -> None:
    now = _utc_now()
    async with _connect() as db:
        await db.execute(
            """INSERT INTO recruiter_invites
               (guild_id, recruiter_id, invite_code, invite_url, channel_id,
                active, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)
               ON CONFLICT(guild_id, recruiter_id) DO UPDATE SET
                   invite_code = excluded.invite_code,
                   invite_url = excluded.invite_url,
                   channel_id = excluded.channel_id,
                   active = 1,
                   updated_at = excluded.updated_at""",
            (guild_id, recruiter_id, invite_code, invite_url, channel_id, now, now),
        )
        await db.commit()


async def get_recruiter_invite(guild_id: int, recruiter_id: int):
    async with _connect() as db:
        async with db.execute(
            """SELECT recruiter_id, invite_code, invite_url, channel_id, active
               FROM recruiter_invites
               WHERE guild_id = ? AND recruiter_id = ?""",
            (guild_id, recruiter_id),
        ) as cursor:
            return await cursor.fetchone()


async def get_active_recruiter_invites(guild_id: int):
    async with _connect() as db:
        async with db.execute(
            """SELECT recruiter_id, invite_code, invite_url, channel_id
               FROM recruiter_invites WHERE guild_id = ? AND active = 1""",
            (guild_id,),
        ) as cursor:
            return await cursor.fetchall()


async def deactivate_recruiter_invite(guild_id: int, recruiter_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE recruiter_invites SET active = 0, updated_at = ?
               WHERE guild_id = ? AND recruiter_id = ?""",
            (_utc_now(), guild_id, recruiter_id),
        )
        await db.commit()


async def deactivate_recruiter_invite_by_code(guild_id: int, code: str) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE recruiter_invites SET active = 0, updated_at = ?
               WHERE guild_id = ? AND invite_code = ?""",
            (_utc_now(), guild_id, code),
        )
        await db.commit()


async def record_recruiting_join(
    guild_id: int, member_id: int, recruiter_id: int, invite_code: str | None
) -> bool:
    """Attribute a join unless that member already has an accepted referral."""
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "recruiting-referral", f"{guild_id}:{member_id}")
        async with db.execute(
            """SELECT status FROM recruiting_referrals
               WHERE guild_id = ? AND member_id = ?""",
            (guild_id, member_id),
        ) as cursor:
            existing = await cursor.fetchone()
        if existing and existing[0] == "accepted":
            await db.rollback()
            return False
        await db.execute(
            """INSERT INTO recruiting_referrals
               (guild_id, member_id, recruiter_id, invite_code, joined_at, status)
               VALUES (?, ?, ?, ?, ?, 'joined')
               ON CONFLICT(guild_id, member_id) DO UPDATE SET
                   recruiter_id = excluded.recruiter_id,
                   invite_code = excluded.invite_code,
                   joined_at = excluded.joined_at,
                   application_number = NULL,
                   ic_name = NULL,
                   applied_at = NULL,
                   status = 'joined',
                   accepted_at = NULL,
                   rejected_at = NULL,
                   approved_by = NULL""",
            (guild_id, member_id, recruiter_id, invite_code, now),
        )
        await db.commit()
        return True


async def mark_recruiting_application(
    guild_id: int, member_id: int, application_number: str, ic_name: str
):
    """Mark a tracked recruit as applied and return its recruiter id."""
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "recruiting-referral", f"{guild_id}:{member_id}")
        async with db.execute(
            """SELECT recruiter_id, status FROM recruiting_referrals
               WHERE guild_id = ? AND member_id = ?""",
            (guild_id, member_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            await db.rollback()
            return None
        if row[1] == "accepted":
            await db.rollback()
            return None
        await db.execute(
            """UPDATE recruiting_referrals SET application_number = ?,
               ic_name = ?, applied_at = ?, status = 'applied',
               rejected_at = NULL, approved_by = NULL
               WHERE guild_id = ? AND member_id = ?""",
            (application_number, ic_name, now, guild_id, member_id),
        )
        await db.commit()
        return row[0]


async def decide_recruiting_referral(
    guild_id: int,
    member_id: int,
    application_number: str,
    accepted: bool,
    reviewer_id: int,
):
    """Apply one family decision and return (recruiter_id, changed)."""
    now = _utc_now()
    target_status = "accepted" if accepted else "rejected"
    async with _connect() as db:
        await begin_write(db, "recruiting-referral", f"{guild_id}:{member_id}")
        async with db.execute(
            """SELECT recruiter_id, status, application_number
               FROM recruiting_referrals
               WHERE guild_id = ? AND member_id = ?""",
            (guild_id, member_id),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None or row[2] != application_number:
            await db.rollback()
            return None
        if row[1] == target_status:
            await db.rollback()
            return row[0], False
        if row[1] == "accepted":
            await db.rollback()
            return row[0], False
        await db.execute(
            """UPDATE recruiting_referrals SET status = ?,
               accepted_at = ?, rejected_at = ?, approved_by = ?
               WHERE guild_id = ? AND member_id = ?""",
            (
                target_status,
                now if accepted else None,
                None if accepted else now,
                reviewer_id,
                guild_id,
                member_id,
            ),
        )
        await db.commit()
        return row[0], True


async def get_recruiting_referrals(guild_id: int):
    async with _connect() as db:
        async with db.execute(
            """SELECT member_id, recruiter_id, joined_at, application_number,
                      ic_name, applied_at, status, accepted_at, approved_by
               FROM recruiting_referrals WHERE guild_id = ?""",
            (guild_id,),
        ) as cursor:
            return await cursor.fetchall()


async def _enqueue_treasury_publication(
    db,
    *,
    guild_id: int,
    transaction_number: int,
    payload: dict,
) -> None:
    """Persist the Discord publication intent in the financial transaction.

    Keeping this INSERT on the same SQLite connection/transaction guarantees
    that a committed balance change can never exist without a retryable
    publication job.
    """

    now = _utc_now()
    await db.execute(
        """INSERT OR IGNORE INTO operation_outbox
           (idempotency_key, guild_id, kind, aggregate_id, payload_json,
            available_at, created_at, updated_at)
           VALUES (?, ?, 'treasury.publish', ?, ?, ?, ?, ?)""",
        (
            f"treasury:{guild_id}:{transaction_number}:publish",
            guild_id,
            str(transaction_number),
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            now,
            now,
            now,
        ),
    )


async def get_treasury_message_id() -> int | None:
    value = await get_setting("treasury_message_id")
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


async def set_treasury_message_id(message_id: int) -> None:
    await set_setting("treasury_message_id", str(message_id))


async def reset_database_data() -> None:
    """Delete all bot data while preserving the database schema."""
    async with _connect() as db:
        await db.execute("PRAGMA foreign_keys = ON")
        try:
            await db.execute("BEGIN IMMEDIATE")
            for table in (
                "inventory_kit_lines",
                "inventory_kits",
                "inventory_reservations",
                "inventory_movements",
                "inventory_reconciliations",
                "inventory_balances",
                "inventory_tax_duties",
                "inventory_items",
                "storage_locations",
                "bonus_shares",
                "bonus_periods",
                "contribution_bonus_marks",
                "contribution_entries",
                "contribution_rule_versions",
                "account_reconciliations",
                "ledger_entries",
                "obligations",
                "accounts",
                "event_attendance_log",
                "event_roster",
                "family_events",
                "queue_claims",
                "queue_transfers",
                "staff_appeals",
                "contract_report_item_payouts",
                "contract_report_expenses",
                "contract_report_purchases",
                "contract_report_reversals",
                "contract_report_evidence",
                "contract_report_shares",
                "report_payout_obligations",
                "contract_reports",
                "contract_run_participants",
                "contract_runs",
                "contract_catalog",
                "promotion_proposals",
                "promotion_criteria",
                "training_assignments",
                "onboarding_tracks",
                "rules_versions",
                "clearances",
                "member_events",
                "members",
                "ranks",
                "season_play_hours",
                "org_lore_reviews",
                "org_upgrade_progress",
                "org_upgrades",
                "org_development",
                "family_seasons",
                "family_properties",
                "family_maps",
                "external_snapshots",
                "family_context",
                "permission_grants",
                "decision_log",
                "runtime_settings",
                "music_sessions",
                "music_tracks",
                "anonymous_message_links",
                "anonymous_conversations",
                "game_server_daily_online",
                "private_group_channels",
                "coin_voice_sessions",
                "coin_activity_ledger",
                "coin_activity_daily",
                "coin_activity_state",
                "recruiting_referrals",
                "recruiter_invites",
                "summit_vzp_positions",
                "summit_vzp_lists",
                "summit_list_members",
                "summit_list_reserves",
                "summit_lists",
                "voice_rooms",
                "voice_owner_bans",
                "giveaway_entries",
                "giveaways",
                "poll_votes",
                "polls",
                "ticket_assignments",
                "tickets",
                "recruiting_unresolved_joins",
                "recruiter_invite_uses",
                "message_event_spillover",
                "metric_counters",
                "metric_snapshots",
                "job_watermarks",
                "car_bookings",
                "cars",
                "stats_messages",
                "stats_member_events",
                "stats_voice_sessions",
                "stats_channel_events",
                "stats_processed_actions",
                "stats_action_receipts",
                "stats_pending_deletions",
                "stats_daily_messages",
                "stats_daily_voice",
                "stats_daily_members",
                "stats_daily_actions",
                "stats_retention_state",
                "purchased_roles",
                "shop_purchase_intents",
                "role_purchase_requests",
                "coin_transactions",
                "coin_accounts",
                "fundraiser_contributions",
                "birthday_fundraisers",
                "treasury_transactions",
                "birthdays",
                "requests",
                "settings",
                "guild_settings",
                "static_messages",
                "command_receipts",
                "operation_outbox",
                "task_health",
                "ticket_counters",
                "counters",
                "treasury_counter",
                "treasury",
            ):
                await db.execute(f"DELETE FROM {table}")
            await db.execute(
                """DELETE FROM sqlite_sequence
                   WHERE name IN (
                       'requests',
                       'birthday_fundraisers',
                       'fundraiser_contributions',
                       'treasury_transactions',
                       'summit_vzp_lists',
                       'summit_lists'
                       ,'cars'
                       ,'car_bookings'
                       ,'stats_member_events'
                       ,'stats_voice_sessions'
                       ,'stats_channel_events'
                       ,'stats_processed_actions'
                       ,'coin_transactions'
                       ,'purchased_roles'
                       ,'shop_purchase_intents'
                       ,'role_purchase_requests'
                       ,'giveaways'
                       ,'polls'
                       ,'recruiting_referrals'
                       ,'coin_activity_ledger'
                       ,'anonymous_message_links'
                       ,'anonymous_conversations'
                       ,'music_tracks'
                       ,'tickets'
                       ,'recruiting_unresolved_joins'
                       ,'family_context'
                       ,'members'
                       ,'family_events'
                       ,'contract_catalog'
                       ,'contract_runs'
                       ,'contract_reports'
                       ,'obligations'
                       ,'accounts'
                       ,'inventory_items'
                       ,'inventory_kits'
                       ,'bonus_periods'
                       ,'contribution_entries'
                       ,'permission_grants'
                       ,'decision_log'
                       ,'family_maps'
                   )"""
            )
            await db.executemany(
                "INSERT INTO counters (type, count) VALUES (?, 0)",
                ((counter,) for counter in DEFAULT_COUNTERS),
            )
            await db.execute("INSERT INTO treasury (id, balance) VALUES (1, 0)")
            await db.execute("INSERT INTO treasury_counter (id, count) VALUES (1, 0)")
            await db.commit()
        except Exception:
            await db.rollback()
            raise


async def _one(db: aiosqlite.Connection, query: str, params=()):
    async with db.execute(query, params) as cursor:
        return await cursor.fetchone()


async def create_vzp_summit(
    title: str,
    capacity: int,
    channel_id: int,
    creator_id: int,
    *,
    guild_id: int | None = None,
) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO summit_vzp_lists
               (title, capacity, channel_id, creator_id, created_at, guild_id)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (title, capacity, channel_id, creator_id, _utc_now(), guild_id),
        )
        list_id = cursor.lastrowid
        await db.executemany(
            "INSERT INTO summit_vzp_positions (list_id, position) VALUES (?, ?)",
            ((list_id, position) for position in range(1, capacity + 1)),
        )
        await db.commit()
        return list_id


async def set_vzp_summit_message(list_id: int, message_id: int, thread_id: int | None) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE summit_vzp_lists SET message_id = ?, thread_id = ? WHERE id = ?",
            (message_id, thread_id, list_id),
        )
        await db.commit()


async def set_vzp_summit_title(list_id: int, title: str) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE summit_vzp_lists SET title = ? WHERE id = ?", (title, list_id)
        )
        await db.commit()


async def expand_vzp_summit(list_id: int, amount: int, *, max_capacity: int = 25):
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        row = await _one(
            db, "SELECT capacity FROM summit_vzp_lists WHERE id = ?", (list_id,)
        )
        if row is None:
            await db.rollback()
            return "not_found", 0
        current = row[0]
        new_capacity = current + amount
        if amount < 1 or new_capacity > max_capacity:
            await db.rollback()
            return "limit", current
        await db.executemany(
            "INSERT INTO summit_vzp_positions (list_id, position) VALUES (?, ?)",
            ((list_id, position) for position in range(current + 1, new_capacity + 1)),
        )
        await db.execute(
            "UPDATE summit_vzp_lists SET capacity = ? WHERE id = ?",
            (new_capacity, list_id),
        )
        await db.commit()
        return "expanded", new_capacity


async def delete_vzp_summit(list_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM summit_vzp_positions WHERE list_id = ?", (list_id,))
        await db.execute("DELETE FROM summit_vzp_lists WHERE id = ?", (list_id,))
        await db.commit()


async def get_vzp_summit(list_id: int):
    async with _connect() as db:
        return await _one(
            db,
            """SELECT id, title, capacity, channel_id, message_id, thread_id, creator_id
               FROM summit_vzp_lists WHERE id = ?""",
            (list_id,),
        )


async def get_vzp_summit_by_thread(thread_id: int):
    async with _connect() as db:
        return await _one(
            db,
            """SELECT id, title, capacity, channel_id, message_id, thread_id, creator_id
               FROM summit_vzp_lists WHERE thread_id = ?""",
            (thread_id,),
        )


async def get_vzp_positions(list_id: int):
    async with _connect() as db:
        async with db.execute(
            """SELECT position, user_id, username FROM summit_vzp_positions
               WHERE list_id = ? ORDER BY position""",
            (list_id,),
        ) as cursor:
            return await cursor.fetchall()


async def vzp_take_position(list_id: int, position: int, user_id: int, username: str) -> str:
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        row = await _one(
            db,
            "SELECT user_id FROM summit_vzp_positions WHERE list_id = ? AND position = ?",
            (list_id, position),
        )
        if row is None:
            await db.rollback()
            return "invalid"
        if row[0] == user_id:
            await db.execute(
                "UPDATE summit_vzp_positions SET user_id = NULL, username = NULL WHERE list_id = ? AND position = ?",
                (list_id, position),
            )
            await db.commit()
            return "left"
        if row[0] is not None:
            await db.rollback()
            return "taken"
        already = await _one(
            db,
            "SELECT position FROM summit_vzp_positions WHERE list_id = ? AND user_id = ?",
            (list_id, user_id),
        )
        if already:
            await db.rollback()
            return "already"
        await db.execute(
            "UPDATE summit_vzp_positions SET user_id = ?, username = ? WHERE list_id = ? AND position = ?",
            (user_id, username, list_id, position),
        )
        await db.commit()
        return "taken_ok"


async def vzp_leave(list_id: int, user_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE summit_vzp_positions SET user_id = NULL, username = NULL
               WHERE list_id = ? AND user_id = ?""",
            (list_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def vzp_kick(list_id: int, user_id: int) -> bool:
    return await vzp_leave(list_id, user_id)


async def vzp_promote(list_id: int, user_id: int) -> bool:
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        current = await _one(
            db,
            "SELECT position FROM summit_vzp_positions WHERE list_id = ? AND user_id = ?",
            (list_id, user_id),
        )
        vacant = await _one(
            db,
            """SELECT position FROM summit_vzp_positions
               WHERE list_id = ? AND user_id IS NULL ORDER BY position LIMIT 1""",
            (list_id,),
        )
        if not current or not vacant or vacant[0] >= current[0]:
            await db.rollback()
            return False
        username = await _one(
            db,
            "SELECT username FROM summit_vzp_positions WHERE list_id = ? AND position = ?",
            (list_id, current[0]),
        )
        await db.execute(
            "UPDATE summit_vzp_positions SET user_id = NULL, username = NULL WHERE list_id = ? AND position = ?",
            (list_id, current[0]),
        )
        await db.execute(
            "UPDATE summit_vzp_positions SET user_id = ?, username = ? WHERE list_id = ? AND position = ?",
            (user_id, username[0], list_id, vacant[0]),
        )
        await db.commit()
        return True


async def create_summit_list(
    title: str,
    capacity: int,
    channel_id: int,
    creator_id: int,
    *,
    kind: str = "list",
    guild_id: int | None = None,
) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO summit_lists
               (title, capacity, channel_id, creator_id, kind, created_at, guild_id)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (title, capacity, channel_id, creator_id, kind, _utc_now(), guild_id),
        )
        await db.commit()
        return cursor.lastrowid


async def set_summit_list_message(list_id: int, message_id: int) -> None:
    async with _connect() as db:
        await db.execute("UPDATE summit_lists SET message_id = ? WHERE id = ?", (message_id, list_id))
        await db.commit()


async def get_summit_list(list_id: int):
    async with _connect() as db:
        return await _one(
            db,
            """SELECT id, title, capacity, channel_id, message_id, creator_id, is_open, kind,
                      selected_map, selected_map_path, approved, thread_id, position_list_id,
                      launched, launched_at
               FROM summit_lists WHERE id = ?""",
            (list_id,),
        )


async def launch_summit_list(list_id: int, thread_id: int) -> bool:
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE summit_lists SET launched = 1, is_open = 0, thread_id = ?,
                                      launched_at = ?
               WHERE id = ? AND launched = 0""",
            (thread_id, _utc_now(), list_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def set_vzp_gathering_map(list_id: int, name: str, path: str) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE summit_lists SET selected_map = ?, selected_map_path = ?
               WHERE id = ? AND kind = 'vzp_gathering'""",
            (name, path, list_id),
        )
        await db.commit()


async def set_vzp_gathering_thread(list_id: int, thread_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            "UPDATE summit_lists SET thread_id = ? WHERE id = ? AND kind = 'vzp_gathering'",
            (thread_id, list_id),
        )
        await db.commit()


async def set_vzp_gathering_position_list(
    list_id: int, position_list_id: int
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE summit_lists SET position_list_id = ?
               WHERE id = ? AND kind = 'vzp_gathering'""",
            (position_list_id, list_id),
        )
        await db.commit()


async def approve_vzp_gathering(list_id: int, position_list_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE summit_lists
               SET approved = 1, is_open = 0, position_list_id = ?, launched = 1,
                   launched_at = COALESCE(launched_at, ?)
               WHERE id = ? AND kind = 'vzp_gathering'""",
            (position_list_id, _utc_now(), list_id),
        )
        await db.commit()


async def get_summit_list_members(list_id: int):
    async with _connect() as db:
        async with db.execute(
            """SELECT user_id, username, priority FROM summit_list_members
               WHERE list_id = ? ORDER BY priority DESC, joined_at""",
            (list_id,),
        ) as cursor:
            return await cursor.fetchall()


async def get_summit_list_reserves(list_id: int):
    async with _connect() as db:
        async with db.execute(
            "SELECT user_id, username FROM summit_list_reserves WHERE list_id = ? ORDER BY joined_at",
            (list_id,),
        ) as cursor:
            return await cursor.fetchall()


async def join_summit_list(list_id: int, user_id: int, username: str, priority: int):
    """Return (result, bumped_user_id, bumped_username)."""
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        summit = await _one(db, "SELECT capacity, is_open FROM summit_lists WHERE id = ?", (list_id,))
        if not summit or not summit[1]:
            await db.rollback()
            return "closed", None, None
        existing = await _one(db, "SELECT 1 FROM summit_list_members WHERE list_id = ? AND user_id = ?", (list_id, user_id))
        if existing:
            await db.rollback()
            return "already", None, None
        await db.execute("DELETE FROM summit_list_reserves WHERE list_id = ? AND user_id = ?", (list_id, user_id))
        members = await db.execute_fetchall(
            """SELECT user_id, username, priority FROM summit_list_members
               WHERE list_id = ? ORDER BY priority, joined_at DESC""",
            (list_id,),
        )
        if len(members) < summit[0]:
            await db.execute(
                "INSERT INTO summit_list_members VALUES (?, ?, ?, ?, ?)",
                (list_id, user_id, username, priority, _utc_now()),
            )
            await db.commit()
            return "joined", None, None
        lowest = members[0]
        if priority <= lowest[2]:
            await db.rollback()
            return "full", None, None
        await db.execute("DELETE FROM summit_list_members WHERE list_id = ? AND user_id = ?", (list_id, lowest[0]))
        await db.execute(
            "INSERT INTO summit_list_reserves VALUES (?, ?, ?, ?)",
            (list_id, lowest[0], lowest[1], _utc_now()),
        )
        await db.execute(
            "INSERT INTO summit_list_members VALUES (?, ?, ?, ?, ?)",
            (list_id, user_id, username, priority, _utc_now()),
        )
        await db.commit()
        return "replaced", lowest[0], lowest[1]


async def join_contract_list(
    list_id: int, user_id: int, username: str, priority: int
) -> str:
    """Join a contract or regular list without reserves or replacement."""
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        contract = await _one(
            db,
            "SELECT capacity, is_open, kind FROM summit_lists WHERE id = ?",
            (list_id,),
        )
        if not contract or contract[2] not in {"contract", "list"} or not contract[1]:
            await db.rollback()
            return "closed"
        if await _one(
            db,
            "SELECT 1 FROM summit_list_members WHERE list_id = ? AND user_id = ?",
            (list_id, user_id),
        ):
            await db.rollback()
            return "already"
        count = await _one(
            db, "SELECT COUNT(*) FROM summit_list_members WHERE list_id = ?", (list_id,)
        )
        if count[0] >= contract[0]:
            await db.rollback()
            return "full"
        await db.execute(
            "INSERT INTO summit_list_members VALUES (?, ?, ?, ?, ?)",
            (list_id, user_id, username, priority, _utc_now()),
        )
        await db.commit()
        return "joined"


async def reserve_summit_list(list_id: int, user_id: int, username: str) -> str:
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        summit = await _one(db, "SELECT is_open FROM summit_lists WHERE id = ?", (list_id,))
        if not summit or not summit[0]:
            await db.rollback()
            return "closed"
        if await _one(db, "SELECT 1 FROM summit_list_members WHERE list_id = ? AND user_id = ?", (list_id, user_id)):
            await db.rollback()
            return "member"
        if await _one(db, "SELECT 1 FROM summit_list_reserves WHERE list_id = ? AND user_id = ?", (list_id, user_id)):
            await db.rollback()
            return "already"
        await db.execute("INSERT INTO summit_list_reserves VALUES (?, ?, ?, ?)", (list_id, user_id, username, _utc_now()))
        await db.commit()
        return "reserved"


async def move_summit_member_to_reserve(list_id: int, user_id: int) -> str:
    """Move a current participant to the reserve queue without losing their place."""
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        state = await _one(
            db,
            "SELECT launched, launch_state, approved FROM summit_lists WHERE id = ?",
            (list_id,),
        )
        if state is None or state[0] or state[2] or state[1] in {"pending", "completed"}:
            await db.rollback()
            return "locked"
        member = await _one(
            db,
            "SELECT username FROM summit_list_members WHERE list_id = ? AND user_id = ?",
            (list_id, user_id),
        )
        if member is None:
            await db.rollback()
            return "missing"
        await db.execute(
            "DELETE FROM summit_list_members WHERE list_id = ? AND user_id = ?",
            (list_id, user_id),
        )
        await db.execute(
            "DELETE FROM summit_list_reserves WHERE list_id = ? AND user_id = ?",
            (list_id, user_id),
        )
        await db.execute(
            "INSERT INTO summit_list_reserves VALUES (?, ?, ?, ?)",
            (list_id, user_id, member[0], _utc_now()),
        )
        await db.commit()
        return "moved"


async def swap_summit_member_with_reserve(
    list_id: int, member_id: int, reserve_id: int, reserve_priority: int
) -> tuple[str, str | None]:
    """Atomically exchange a main-list participant with a reserve participant."""
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        state = await _one(
            db,
            "SELECT launched, launch_state, approved FROM summit_lists WHERE id = ?",
            (list_id,),
        )
        if state is None or state[0] or state[2] or state[1] in {"pending", "completed"}:
            await db.rollback()
            return "locked", None
        member = await _one(
            db,
            "SELECT username FROM summit_list_members WHERE list_id = ? AND user_id = ?",
            (list_id, member_id),
        )
        reserve = await _one(
            db,
            "SELECT username FROM summit_list_reserves WHERE list_id = ? AND user_id = ?",
            (list_id, reserve_id),
        )
        if member is None or reserve is None:
            await db.rollback()
            return "invalid", None
        await db.execute(
            "DELETE FROM summit_list_members WHERE list_id = ? AND user_id = ?",
            (list_id, member_id),
        )
        await db.execute(
            "DELETE FROM summit_list_reserves WHERE list_id = ? AND user_id = ?",
            (list_id, reserve_id),
        )
        await db.execute(
            "INSERT INTO summit_list_reserves VALUES (?, ?, ?, ?)",
            (list_id, member_id, member[0], _utc_now()),
        )
        await db.execute(
            "INSERT INTO summit_list_members VALUES (?, ?, ?, ?, ?)",
            (list_id, reserve_id, reserve[0], reserve_priority, _utc_now()),
        )
        await db.commit()
        return "swapped", reserve[0]


async def leave_summit_list(list_id: int, user_id: int) -> str:
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        state = await _one(
            db,
            "SELECT launched, launch_state, approved FROM summit_lists WHERE id = ?",
            (list_id,),
        )
        if state is None:
            await db.rollback()
            return "missing"
        if state[0] or state[2] or state[1] in {"pending", "completed"}:
            await db.rollback()
            return "locked"
        member = await db.execute("DELETE FROM summit_list_members WHERE list_id = ? AND user_id = ?", (list_id, user_id))
        reserve = await db.execute("DELETE FROM summit_list_reserves WHERE list_id = ? AND user_id = ?", (list_id, user_id))
        await db.commit()
        return "member" if member.rowcount else "reserve" if reserve.rowcount else "missing"


async def promote_summit_reserve(list_id: int, priority: int):
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        summit = await _one(db, "SELECT capacity FROM summit_lists WHERE id = ?", (list_id,))
        count = await _one(db, "SELECT COUNT(*) FROM summit_list_members WHERE list_id = ?", (list_id,))
        reserve = await _one(db, "SELECT user_id, username FROM summit_list_reserves WHERE list_id = ? ORDER BY joined_at LIMIT 1", (list_id,))
        if not summit or count[0] >= summit[0] or not reserve:
            await db.rollback()
            return None
        await db.execute("DELETE FROM summit_list_reserves WHERE list_id = ? AND user_id = ?", (list_id, reserve[0]))
        await db.execute("INSERT INTO summit_list_members VALUES (?, ?, ?, ?, ?)", (list_id, reserve[0], reserve[1], priority, _utc_now()))
        await db.commit()
        return reserve


async def toggle_summit_list(list_id: int) -> bool | None:
    async with _connect() as db:
        await begin_write(db, "summit", list_id)
        row = await _one(
            db,
            "SELECT is_open, launched, launch_state FROM summit_lists WHERE id = ?",
            (list_id,),
        )
        if not row or row[1] or row[2] in {"pending", "completed"}:
            await db.rollback()
            return None
        value = not bool(row[0])
        await db.execute("UPDATE summit_lists SET is_open = ? WHERE id = ?", (value, list_id))
        await db.commit()
        return value


async def delete_summit_list(list_id: int) -> None:
    async with _connect() as db:
        linked = await _one(
            db, "SELECT position_list_id FROM summit_lists WHERE id = ?", (list_id,)
        )
        if linked and linked[0]:
            await db.execute(
                "DELETE FROM summit_vzp_positions WHERE list_id = ?", (linked[0],)
            )
            await db.execute("DELETE FROM summit_vzp_lists WHERE id = ?", (linked[0],))
        await db.execute("DELETE FROM summit_lists WHERE id = ?", (list_id,))
        await db.commit()


async def get_all_summit_views():
    async with _connect() as db:
        vzp = await db.execute_fetchall("SELECT id, message_id FROM summit_vzp_lists WHERE message_id IS NOT NULL")
        regular = await db.execute_fetchall("SELECT id, message_id FROM summit_lists WHERE message_id IS NOT NULL")
        return vzp, regular


async def record_stats_message(
    message_id: int,
    guild_id: int,
    channel_id: int,
    channel_name: str,
    user_id: int,
    created_at: str,
    family_visible: bool | None = True,
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT OR IGNORE INTO stats_messages
               (message_id, guild_id, channel_id, channel_name, user_id,
                created_at, family_visible)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                message_id,
                guild_id,
                channel_id,
                channel_name,
                user_id,
                created_at,
                None if family_visible is None else int(family_visible),
            ),
        )
        await db.commit()


async def record_stats_messages_batch(
    records, deleted_message_ids=()
) -> None:
    """Persist high-volume message events with one connection and transaction."""
    rows = tuple(
        (
            *record[:6],
            int(record[6]) if len(record) > 6 and record[6] is not None else 1,
        )
        for record in records
    )
    deleted_ids = tuple({int(message_id) for message_id in deleted_message_ids})
    if not rows and not deleted_ids:
        return
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "stats-ingest", "messages")
        if rows:
            await db.executemany(
                """INSERT OR IGNORE INTO stats_messages
                   (message_id, guild_id, channel_id, channel_name, user_id,
                    created_at, family_visible)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                rows,
            )
        if deleted_ids:
            await db.executemany(
                """INSERT INTO stats_pending_deletions (message_id, deleted_at)
                   VALUES (?, ?) ON CONFLICT(message_id) DO NOTHING""",
                ((message_id, now) for message_id in deleted_ids),
            )
        # A delete gateway event can arrive before its create event is flushed.
        # Tombstones make the update order-independent instead of losing it.
        await db.execute(
            """UPDATE stats_messages
               SET deleted_at = COALESCE(
                   deleted_at,
                   (SELECT deleted_at FROM stats_pending_deletions p
                    WHERE p.message_id = stats_messages.message_id)
               )
               WHERE message_id IN (SELECT message_id FROM stats_pending_deletions)"""
        )
        await db.execute(
            """DELETE FROM stats_pending_deletions
               WHERE message_id IN (SELECT message_id FROM stats_messages)"""
        )
        await db.commit()


async def mark_stats_messages_deleted(message_ids) -> None:
    ids = tuple({int(message_id) for message_id in message_ids})
    if not ids:
        return
    async with _connect() as db:
        now = _utc_now()
        await db.executemany(
            """INSERT INTO stats_pending_deletions (message_id, deleted_at)
               VALUES (?, ?) ON CONFLICT(message_id) DO NOTHING""",
            ((message_id, now) for message_id in ids),
        )
        await db.execute(
            """UPDATE stats_messages
               SET deleted_at = COALESCE(
                   deleted_at,
                   (SELECT deleted_at FROM stats_pending_deletions p
                    WHERE p.message_id = stats_messages.message_id)
               )
               WHERE message_id IN (SELECT message_id FROM stats_pending_deletions)"""
        )
        await db.execute(
            """DELETE FROM stats_pending_deletions
               WHERE message_id IN (SELECT message_id FROM stats_messages)"""
        )
        await db.commit()


async def record_stats_member_event(
    guild_id: int, user_id: int, event_type: str
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO stats_member_events
               (guild_id, user_id, event_type, event_at) VALUES (?, ?, ?, ?)""",
            (guild_id, user_id, event_type, _utc_now()),
        )
        await db.commit()


async def record_stats_channel_event(
    guild_id: int,
    channel_id: int,
    channel_name: str,
    event_type: str,
    family_visible: bool | None = True,
) -> None:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "stats-channel", guild_id)
        if event_type == "delete":
            # Discord may remove a channel without delivering a final voice
            # state event for every member. Close those sessions here so a
            # deleted channel cannot remain "active" until the next restart.
            await db.execute(
                """UPDATE stats_voice_sessions
                   SET room_creator_id = COALESCE(
                           room_creator_id,
                           (SELECT creator_id FROM voice_rooms
                            WHERE voice_rooms.channel_id = stats_voice_sessions.channel_id)
                       ),
                       channel_name = ?,
                       ended_at = ?
                   WHERE guild_id = ? AND channel_id = ? AND ended_at IS NULL""",
                (channel_name, now, guild_id, channel_id),
            )
        await db.execute(
            """INSERT INTO stats_channel_events
               (guild_id, channel_id, channel_name, event_type, event_at,
                family_visible)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                channel_id,
                channel_name,
                event_type,
                now,
                None if family_visible is None else int(family_visible),
            ),
        )
        await db.commit()


async def record_stats_processed_action(
    guild_id: int,
    action_kind: str,
    outcome: str,
    actor_id: int | None = None,
) -> None:
    if action_kind not in {"ticket", "application"}:
        raise ValueError("Unsupported processed action kind")
    async with _connect() as db:
        await db.execute(
            """INSERT INTO stats_processed_actions
               (guild_id, action_kind, outcome, actor_id, processed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (guild_id, action_kind, outcome, actor_id, _utc_now()),
        )
        await db.commit()


async def start_stats_voice_session(
    guild_id: int,
    user_id: int,
    channel_id: int,
    channel_name: str,
    family_visible: bool | None = True,
) -> None:
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "stats-voice", f"{guild_id}:{user_id}")
        await db.execute(
            """UPDATE stats_voice_sessions SET ended_at = ?
               WHERE guild_id = ? AND user_id = ? AND ended_at IS NULL""",
            (now, guild_id, user_id),
        )
        await db.execute(
            """INSERT INTO stats_voice_sessions
               (guild_id, user_id, channel_id, channel_name, started_at,
                room_creator_id, family_visible)
               VALUES (?, ?, ?, ?, ?,
                       (SELECT creator_id FROM voice_rooms WHERE channel_id = ?),
                       ?)
            """,
            (
                guild_id,
                user_id,
                channel_id,
                channel_name,
                now,
                channel_id,
                None if family_visible is None else int(family_visible),
            ),
        )
        await db.commit()


async def close_stats_voice_session(guild_id: int, user_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE stats_voice_sessions SET ended_at = ?
               WHERE guild_id = ? AND user_id = ? AND ended_at IS NULL""",
            (_utc_now(), guild_id, user_id),
        )
        await db.commit()


async def close_all_stats_voice_sessions(
    guild_id: int, ended_at: str | None = None
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE stats_voice_sessions SET ended_at = ?
               WHERE guild_id = ? AND ended_at IS NULL""",
            (ended_at or _utc_now(), guild_id),
        )
        await db.commit()


async def transition_stats_voice_session(
    guild_id: int,
    user_id: int,
    channel_id: int | None = None,
    channel_name: str | None = None,
    family_visible: bool | None = True,
) -> None:
    """Close the old voice session and optionally open the new one atomically."""
    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "stats-voice", f"{guild_id}:{user_id}")
        await db.execute(
            """UPDATE stats_voice_sessions SET ended_at = ?
               WHERE guild_id = ? AND user_id = ? AND ended_at IS NULL""",
            (now, guild_id, user_id),
        )
        if channel_id is not None and channel_name is not None:
            await db.execute(
                """INSERT INTO stats_voice_sessions
                   (guild_id, user_id, channel_id, channel_name, started_at,
                    room_creator_id, family_visible)
                   VALUES (?, ?, ?, ?, ?,
                           (SELECT creator_id FROM voice_rooms
                            WHERE channel_id = ?),
                           ?)""",
                (
                    guild_id,
                    user_id,
                    channel_id,
                    channel_name,
                    now,
                    channel_id,
                    None if family_visible is None else int(family_visible),
                ),
            )
        await db.commit()


async def reset_stats_voice_sessions(
    guild_id: int, ended_at: str | None, active_sessions
) -> None:
    """Recover voice tracking after restart with one batched transaction."""
    now = _utc_now()
    rows = tuple(active_sessions)
    async with _connect() as db:
        await begin_write(db, "stats-voice-reset", guild_id)
        recovery_end = ended_at or now
        await db.execute(
            """UPDATE stats_voice_sessions
               SET ended_at = CASE
                       WHEN started_at > ? THEN started_at
                       ELSE ?
                   END
               WHERE guild_id = ? AND ended_at IS NULL""",
            (recovery_end, recovery_end, guild_id),
        )
        if rows:
            await db.executemany(
                """INSERT INTO stats_voice_sessions
                   (guild_id, user_id, channel_id, channel_name, started_at,
                    room_creator_id, family_visible)
                   VALUES (?, ?, ?, ?, ?,
                           (SELECT creator_id FROM voice_rooms
                            WHERE channel_id = ?),
                           ?)""",
                (
                    (
                        guild_id,
                        user_id,
                        channel_id,
                        channel_name,
                        now,
                        channel_id,
                        None if family_visible is None else int(family_visible),
                    )
                    for user_id, channel_id, channel_name, *extra in rows
                    for family_visible in (
                        extra[0] if extra else True,
                    )
                ),
            )
        await db.commit()


async def get_weekly_stats_data(guild_id: int, start: str, end: str):
    async with _connect() as db:
        grouped_message_activity = await db.execute_fetchall(
            """SELECT channel_id, channel_name, user_id, family_visible,
                      COUNT(*) AS total,
                      SUM(CASE WHEN deleted_at IS NOT NULL THEN 1 ELSE 0 END)
               FROM stats_messages
               WHERE guild_id = ? AND created_at >= ? AND created_at < ?
               GROUP BY channel_id, channel_name, user_id, family_visible""",
            (guild_id, start, end),
        )
        channel_totals: Counter[tuple[int, str]] = Counter()
        channel_visibility: dict[tuple[int, str], bool] = {}
        user_totals: Counter[int] = Counter()
        message_activity = []
        deleted_total = 0
        for (
            channel_id,
            channel_name,
            user_id,
            family_visible,
            total,
            deleted,
        ) in grouped_message_activity:
            channel_totals[(channel_id, channel_name)] += total
            key = (channel_id, channel_name)
            channel_visibility[key] = (
                channel_visibility.get(key, False) or family_visible == 1
            )
            user_totals[user_id] += total
            deleted_total += deleted or 0
            message_activity.append((channel_id, user_id, total, deleted))
        message_summary = (
            sum(user_totals.values()),
            len(user_totals),
            deleted_total,
        )
        text_channels = [
            (
                channel_id,
                channel_name,
                total,
                channel_visibility[(channel_id, channel_name)],
            )
            for (channel_id, channel_name), total in channel_totals.most_common()
        ]
        user_messages = user_totals.most_common()
        member_events = await db.execute_fetchall(
            """SELECT event_type, COUNT(*) FROM stats_member_events
               WHERE guild_id = ? AND event_at >= ? AND event_at < ?
               GROUP BY event_type""",
            (guild_id, start, end),
        )
        voice_sessions = await db.execute_fetchall(
            """SELECT user_id, channel_id, channel_name, started_at, ended_at,
                      room_creator_id, family_visible
               FROM stats_voice_sessions WHERE guild_id = ?
                 AND started_at < ? AND COALESCE(ended_at, ?) > ?
               ORDER BY started_at, id""",
            (guild_id, end, _utc_now(), start),
        )
        channel_events = await db.execute_fetchall(
            """SELECT event_type, channel_id, channel_name, family_visible,
                      COUNT(*) FROM stats_channel_events
               WHERE guild_id = ? AND event_at >= ? AND event_at < ?
               GROUP BY event_type, channel_id, channel_name, family_visible
               ORDER BY event_type, COUNT(*) DESC""",
            (guild_id, start, end),
        )
        summit_leaders = await db.execute_fetchall(
            """SELECT kind, creator_id, COUNT(*) AS total FROM summit_lists
               WHERE guild_id = ? AND launched = 1
                 AND launched_at >= ? AND launched_at < ?
               GROUP BY kind, creator_id ORDER BY kind, total DESC""",
            (guild_id, start, end),
        )
        processed_actions = await db.execute_fetchall(
            """SELECT action_kind, outcome, COUNT(*)
               FROM stats_processed_actions
               WHERE guild_id = ? AND processed_at >= ? AND processed_at < ?
               GROUP BY action_kind, outcome""",
            (guild_id, start, end),
        )
        return {
            "message_summary": message_summary,
            "text_channels": text_channels,
            "user_messages": user_messages,
            "message_activity": message_activity,
            "member_events": member_events,
            "voice_sessions": voice_sessions,
            "channel_events": channel_events,
            "summit_leaders": summit_leaders,
            "processed_actions": processed_actions,
        }


async def get_coin_account(user_id: int):
    async with _connect() as db:
        account = await _one(
            db,
            "SELECT balance, last_daily, vip_until, summit_emoji FROM coin_accounts WHERE user_id = ?",
            (user_id,),
        )
        if account is not None:
            return account
        await db.execute(
            "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,)
        )
        await db.commit()
        return await _one(
            db,
            "SELECT balance, last_daily, vip_until, summit_emoji FROM coin_accounts WHERE user_id = ?",
            (user_id,),
        )


async def claim_daily_coins(user_id: int, day: str, amount: int) -> bool:
    async with _connect() as db:
        await begin_write(db, "coins", user_id)
        await db.execute("INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,))
        account = await _one(db, "SELECT last_daily FROM coin_accounts WHERE user_id = ?", (user_id,))
        if account[0] == day:
            await db.rollback()
            return False
        await db.execute(
            "UPDATE coin_accounts SET balance = balance + ?, last_daily = ? WHERE user_id = ?",
            (amount, day, user_id),
        )
        await db.execute(
            "INSERT INTO coin_transactions (user_id, amount, action, created_at) VALUES (?, ?, 'daily', ?)",
            (user_id, amount, _utc_now()),
        )
        await db.commit()
        return True


async def transfer_coins(sender_id: int, recipient_id: int, amount: int) -> bool:
    async with _connect() as db:
        await begin_write(db, "coins", sender_id)
        await db.executemany(
            "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)",
            ((sender_id,), (recipient_id,)),
        )
        balance = await _one(db, "SELECT balance FROM coin_accounts WHERE user_id = ?", (sender_id,))
        if amount < 1 or balance[0] < amount:
            await db.rollback()
            return False
        await db.execute("UPDATE coin_accounts SET balance = balance - ? WHERE user_id = ?", (amount, sender_id))
        await db.execute("UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?", (amount, recipient_id))
        await db.executemany(
            """INSERT INTO coin_transactions
               (user_id, amount, action, related_user_id, created_at)
               VALUES (?, ?, ?, ?, ?)""",
            (
                (sender_id, -amount, "pay_sent", recipient_id, _utc_now()),
                (recipient_id, amount, "pay_received", sender_id, _utc_now()),
            ),
        )
        await db.commit()
        return True


async def adjust_coins(user_id: int, amount: int, action: str = "admin") -> int:
    async with _connect() as db:
        await begin_write(db, "coins", user_id)
        await db.execute("INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,))
        balance = (await _one(db, "SELECT balance FROM coin_accounts WHERE user_id = ?", (user_id,)))[0]
        new_balance = max(0, balance + amount)
        actual = new_balance - balance
        await db.execute("UPDATE coin_accounts SET balance = ? WHERE user_id = ?", (new_balance, user_id))
        await db.execute(
            "INSERT INTO coin_transactions (user_id, amount, action, created_at) VALUES (?, ?, ?, ?)",
            (user_id, actual, action, _utc_now()),
        )
        await db.commit()
        return new_balance


async def remove_coins(user_id: int, amount: int, action: str) -> tuple[int, int]:
    """Remove up to the requested amount and return (removed, new_balance)."""
    if amount < 1:
        raise ValueError("Amount must be positive")
    async with _connect() as db:
        await begin_write(db, "coins", user_id)
        await db.execute(
            "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,)
        )
        balance = (
            await _one(
                db, "SELECT balance FROM coin_accounts WHERE user_id = ?", (user_id,)
            )
        )[0]
        removed = min(balance, amount)
        new_balance = balance - removed
        await db.execute(
            "UPDATE coin_accounts SET balance = ? WHERE user_id = ?",
            (new_balance, user_id),
        )
        await db.execute(
            """INSERT INTO coin_transactions
               (user_id, amount, action, created_at) VALUES (?, ?, ?, ?)""",
            (user_id, -removed, action, _utc_now()),
        )
        await db.commit()
        return removed, new_balance


async def reset_coins(user_id: int, action: str = "admin_reset") -> int:
    """Atomically zero an account and return the amount removed."""
    async with _connect() as db:
        await begin_write(db, "coins", user_id)
        await db.execute(
            "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,)
        )
        balance = (
            await _one(
                db, "SELECT balance FROM coin_accounts WHERE user_id = ?", (user_id,)
            )
        )[0]
        await db.execute(
            "UPDATE coin_accounts SET balance = 0 WHERE user_id = ?", (user_id,)
        )
        await db.execute(
            """INSERT INTO coin_transactions
               (user_id, amount, action, created_at) VALUES (?, ?, ?, ?)""",
            (user_id, -balance, action, _utc_now()),
        )
        await db.commit()
        return balance


async def spend_coins(user_id: int, amount: int, action: str) -> bool:
    async with _connect() as db:
        await begin_write(db, "coins", user_id)
        await db.execute("INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,))
        cursor = await db.execute(
            "UPDATE coin_accounts SET balance = balance - ? WHERE user_id = ? AND balance >= ?",
            (amount, user_id, amount),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            return False
        await db.execute(
            "INSERT INTO coin_transactions (user_id, amount, action, created_at) VALUES (?, ?, ?, ?)",
            (user_id, -amount, action, _utc_now()),
        )
        await db.commit()
        return True


async def set_vip_until(user_id: int, vip_until: str) -> None:
    async with _connect() as db:
        await db.execute("INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,))
        await db.execute("UPDATE coin_accounts SET vip_until = ? WHERE user_id = ?", (vip_until, user_id))
        await db.commit()


async def has_active_vip(user_id: int) -> bool:
    account = await get_coin_account(user_id)
    return bool(account[2] and account[2] > _utc_now())


async def set_summit_emoji(user_id: int, emoji: str) -> None:
    async with _connect() as db:
        await db.execute("INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,))
        await db.execute("UPDATE coin_accounts SET summit_emoji = ? WHERE user_id = ?", (emoji, user_id))
        await db.commit()


async def purchase_summit_emoji(user_id: int, emoji: str, price: int) -> str:
    """Validate VIP, charge the account and set its emoji atomically."""
    async with _connect() as db:
        await begin_write(db, "coins", user_id)
        await db.execute(
            "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,)
        )
        account = await _one(
            db,
            "SELECT balance, vip_until FROM coin_accounts WHERE user_id = ?",
            (user_id,),
        )
        if not account[1] or account[1] <= _utc_now():
            await db.rollback()
            return "no_vip"
        if price < 1 or account[0] < price:
            await db.rollback()
            return "insufficient"
        now = _utc_now()
        await db.execute(
            """UPDATE coin_accounts
               SET balance = balance - ?, summit_emoji = ? WHERE user_id = ?""",
            (price, emoji, user_id),
        )
        await db.execute(
            """INSERT INTO coin_transactions
               (user_id, amount, action, created_at)
               VALUES (?, ?, 'summit_emoji', ?)""",
            (user_id, -price, now),
        )
        await db.commit()
        return "ok"


async def get_active_summit_emojis(user_ids) -> dict[int, str]:
    ids = tuple({int(user_id) for user_id in user_ids})
    if not ids:
        return {}
    placeholders = ",".join("?" for _ in ids)
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT user_id, summit_emoji FROM coin_accounts
                WHERE user_id IN ({placeholders}) AND summit_emoji IS NOT NULL
                  AND vip_until > ?""",
            (*ids, _utc_now()),
        )
        return dict(rows)


async def register_purchased_role(
    user_id: int, role_id: int, role_name: str, price: int, role_kind: str
) -> None:
    async with _connect() as db:
        await db.execute(
            """INSERT INTO purchased_roles
               (user_id, role_id, role_name, price, role_kind, purchased_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (user_id, role_id, role_name, price, role_kind, _utc_now()),
        )
        await db.commit()


async def get_user_purchased_roles(user_id: int):
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT id, role_id, role_name, price, role_kind FROM purchased_roles
               WHERE user_id = ? AND active = 1 ORDER BY purchased_at""",
            (user_id,),
        )


async def get_active_purchased_role_owners() -> list[int]:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            """SELECT DISTINCT user_id FROM purchased_roles
               WHERE active = 1 ORDER BY user_id"""
        )
        return [row[0] for row in rows]


async def sell_purchased_role(purchase_id: int, user_id: int):
    async with _connect() as db:
        await begin_write(db, "shop-role-sale", purchase_id)
        purchase = await _one(
            db,
            """SELECT role_id, role_name, price FROM purchased_roles
               WHERE id = ? AND user_id = ? AND active = 1""",
            (purchase_id, user_id),
        )
        if purchase is None:
            await db.rollback()
            return None
        refund = purchase[2] // 2
        await db.execute(
            "UPDATE purchased_roles SET active = 0, sold_at = ? WHERE id = ?",
            (_utc_now(), purchase_id),
        )
        await db.execute("INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,))
        await db.execute("UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?", (refund, user_id))
        await db.execute(
            "INSERT INTO coin_transactions (user_id, amount, action, created_at) VALUES (?, ?, 'role_sale', ?)",
            (user_id, refund, _utc_now()),
        )
        await db.commit()
        return (*purchase, refund)


async def claim_purchased_role_sale(purchase_id: int, user_id: int):
    """Reserve a role sale before the corresponding Discord role is deleted."""
    async with _connect() as db:
        await begin_write(db, "shop-role-sale", purchase_id)
        purchase = await _one(
            db,
            """SELECT role_id, role_name, price FROM purchased_roles
               WHERE id = ? AND user_id = ? AND active = 1""",
            (purchase_id, user_id),
        )
        if purchase is None:
            await db.rollback()
            return None
        await db.execute(
            "UPDATE purchased_roles SET active = 2 WHERE id = ?", (purchase_id,)
        )
        await db.commit()
        return purchase


async def release_purchased_role_sale(purchase_id: int, user_id: int) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE purchased_roles SET active = 1
               WHERE id = ? AND user_id = ? AND active = 2""",
            (purchase_id, user_id),
        )
        await db.commit()


async def complete_purchased_role_sale(purchase_id: int, user_id: int):
    """Finalize a reserved role sale and issue its refund atomically."""
    async with _connect() as db:
        await begin_write(db, "shop-role-sale", purchase_id)
        purchase = await _one(
            db,
            """SELECT role_id, role_name, price FROM purchased_roles
               WHERE id = ? AND user_id = ? AND active = 2""",
            (purchase_id, user_id),
        )
        if purchase is None:
            await db.rollback()
            return None
        refund = purchase[2] // 2
        now = _utc_now()
        await db.execute(
            "UPDATE purchased_roles SET active = 0, sold_at = ? WHERE id = ?",
            (now, purchase_id),
        )
        await db.execute(
            "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,)
        )
        await db.execute(
            "UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?",
            (refund, user_id),
        )
        await db.execute(
            """INSERT INTO coin_transactions
               (user_id, amount, action, created_at)
               VALUES (?, ?, 'role_sale', ?)""",
            (user_id, refund, now),
        )
        await db.commit()
        return (*purchase, refund)


async def deactivate_purchased_role(purchase_id: int, user_id: int) -> bool:
    """Mark a deleted role inactive without issuing a refund."""
    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE purchased_roles SET active = 0, sold_at = ?
               WHERE id = ? AND user_id = ? AND active = 1""",
            (_utc_now(), purchase_id, user_id),
        )
        await db.commit()
        return cursor.rowcount > 0


async def get_richest_users(limit: int = 10):
    async with _connect() as db:
        return await db.execute_fetchall(
            "SELECT user_id, balance FROM coin_accounts ORDER BY balance DESC, user_id LIMIT ?",
            (max(1, min(limit, 25)),),
        )


async def get_expired_vip_users():
    async with _connect() as db:
        return await db.execute_fetchall(
            "SELECT user_id FROM coin_accounts WHERE vip_until IS NOT NULL AND vip_until <= ?",
            (_utc_now(),),
        )


async def clear_vip(user_id: int) -> None:
    async with _connect() as db:
        await db.execute("UPDATE coin_accounts SET vip_until = NULL WHERE user_id = ?", (user_id,))
        await db.commit()


async def create_role_purchase_request(
    user_id: int,
    role_name: str,
    primary_color: int,
    secondary_color: int | None,
    tertiary_color: int | None,
    price: int,
) -> int:
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO role_purchase_requests
               (user_id, role_name, primary_color, secondary_color, tertiary_color,
                price, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (
                user_id,
                role_name,
                primary_color,
                secondary_color,
                tertiary_color,
                price,
                _utc_now(),
            ),
        )
        await db.commit()
        return cursor.lastrowid


async def purchase_role_request(
    user_id: int,
    role_name: str,
    primary_color: int,
    secondary_color: int | None,
    tertiary_color: int | None,
    price: int,
    *,
    guild_id: int | None = None,
    channel_id: int | None = None,
) -> int | None:
    """Charge coins and create an approval request in one transaction."""
    if price < 1:
        raise ValueError("Price must be positive")
    async with _connect() as db:
        await begin_write(db, "coins", user_id)
        await db.execute(
            "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)", (user_id,)
        )
        charged = await db.execute(
            """UPDATE coin_accounts SET balance = balance - ?
               WHERE user_id = ? AND balance >= ?""",
            (price, user_id, price),
        )
        if charged.rowcount == 0:
            await db.rollback()
            return None
        now = _utc_now()
        await db.execute(
            """INSERT INTO coin_transactions
               (user_id, amount, action, created_at)
               VALUES (?, ?, 'purchase_custom_role', ?)""",
            (user_id, -price, now),
        )
        cursor = await db.execute(
            """INSERT INTO role_purchase_requests
               (user_id, role_name, primary_color, secondary_color, tertiary_color,
                price, created_at, guild_id, channel_id, publication_state)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending')""",
            (
                user_id,
                role_name,
                primary_color,
                secondary_color,
                tertiary_color,
                price,
                now,
                guild_id,
                channel_id,
            ),
        )
        request_id = int(cursor.lastrowid)
        if guild_id is not None:
            payload = json.dumps(
                {"request_id": request_id},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await db.execute(
                """INSERT OR IGNORE INTO operation_outbox
                   (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                    available_at, created_at, updated_at)
                   VALUES (?, ?, 'role.request.publish', ?, ?, ?, ?, ?)""",
                (
                    f"role-purchase:{request_id}:publish",
                    guild_id,
                    str(request_id),
                    payload,
                    now,
                    now,
                    now,
                ),
            )
        await db.commit()
        return request_id


async def set_role_purchase_request_message(
    request_id: int,
    message_id: int,
    *,
    guild_id: int | None = None,
    channel_id: int | None = None,
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE role_purchase_requests
               SET message_id = ?, guild_id = COALESCE(?, guild_id),
                   channel_id = COALESCE(?, channel_id),
                   publication_state = 'published', publication_error = NULL
               WHERE id = ?""",
            (message_id, guild_id, channel_id, request_id),
        )
        await db.commit()


async def set_role_purchase_publication_error(
    request_id: int, error: BaseException
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE role_purchase_requests
               SET publication_state = 'pending', publication_error = ?
               WHERE id = ? AND status = 'pending'""",
            (f"{type(error).__name__}: {error}"[:1_000], request_id),
        )
        await db.commit()


async def ensure_role_purchase_publication_outbox(
    request_id: int, guild_id: int
) -> None:
    now = _utc_now()
    payload = json.dumps({"request_id": request_id}, separators=(",", ":"))
    async with _connect() as db:
        await db.execute(
            """INSERT OR IGNORE INTO operation_outbox
               (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                available_at, created_at, updated_at)
               VALUES (?, ?, 'role.request.publish', ?, ?, ?, ?, ?)""",
            (
                f"role-purchase:{request_id}:publish",
                guild_id,
                str(request_id),
                payload,
                now,
                now,
                now,
            ),
        )
        await db.execute(
            """UPDATE operation_outbox
               SET state = 'retry', attempts = 0, available_at = ?,
                   lease_until = NULL, last_error = NULL, updated_at = ?
               WHERE idempotency_key = ? AND state = 'dead'""",
            (now, now, f"role-purchase:{request_id}:publish"),
        )
        await db.commit()


async def cancel_role_purchase_request_with_refund(
    request_id: int,
    user_id: int,
    refund: int,
    action: str = "purchase_refund",
) -> bool:
    """Cancel an unpublished request and refund it exactly once."""
    if refund < 0:
        raise ValueError("Refund cannot be negative")
    async with _connect() as db:
        await begin_write(db, "role-request", request_id)
        cursor = await db.execute(
            """UPDATE role_purchase_requests
               SET status = 'cancelled', reviewed_at = ?
               WHERE id = ? AND user_id = ? AND status = 'pending'
                 AND message_id IS NULL""",
            (_utc_now(), request_id, user_id),
        )
        if cursor.rowcount == 0:
            await db.rollback()
            return False
        if refund:
            await db.execute(
                "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)",
                (user_id,),
            )
            await db.execute(
                "UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?",
                (refund, user_id),
            )
            await db.execute(
                """INSERT INTO coin_transactions
                   (user_id, amount, action, created_at) VALUES (?, ?, ?, ?)""",
                (user_id, refund, action, _utc_now()),
            )
        await db.commit()
        return True


async def get_role_purchase_request(request_id: int):
    async with _connect() as db:
        return await _one(
            db,
            """SELECT id, user_id, role_name, primary_color, secondary_color,
                      tertiary_color, price, status, message_id
               FROM role_purchase_requests WHERE id = ?""",
            (request_id,),
        )


async def get_role_purchase_publication(request_id: int):
    async with _connect() as db:
        return await _one(
            db,
            """SELECT id, user_id, role_name, primary_color, secondary_color,
                      tertiary_color, price, status, message_id, guild_id,
                      channel_id, publication_state
               FROM role_purchase_requests WHERE id = ?""",
            (request_id,),
        )


async def get_pending_role_purchase_requests():
    async with _connect() as db:
        return await db.execute_fetchall(
            """SELECT id, message_id FROM role_purchase_requests
               WHERE status = 'pending' ORDER BY id"""
        )


async def count_pending_user_role_purchase_requests(user_id: int) -> int:
    async with _connect() as db:
        row = await _one(
            db,
            """SELECT COUNT(*) FROM role_purchase_requests
               WHERE user_id = ? AND status IN ('pending', 'processing')""",
            (user_id,),
        )
        return row[0]


async def claim_role_purchase_request(request_id: int, reviewer_id: int):
    now = datetime.now(UTC)
    lease_until = (now + timedelta(minutes=5)).isoformat()
    async with _connect() as db:
        await begin_write(db, "role-request", request_id)
        request = await _one(
            db,
            """SELECT id, user_id, role_name, primary_color, secondary_color,
                      tertiary_color, price, status, message_id
               FROM role_purchase_requests
               WHERE id = ? AND status = 'pending'""",
            (request_id,),
        )
        if request is None:
            await db.rollback()
            return None
        await db.execute(
            """UPDATE role_purchase_requests
               SET status = 'processing', reviewer_id = ?, reviewed_at = ?,
                   operation_state = 'processing', lease_until = ?, last_error = NULL
               WHERE id = ?""",
            (reviewer_id, now.isoformat(), lease_until, request_id),
        )
        await db.commit()
        return request


async def finish_role_purchase_request(
    request_id: int, status: str, reviewer_id: int
) -> None:
    if status not in {"approved", "rejected"}:
        raise ValueError("Unsupported role request status")
    async with _connect() as db:
        await db.execute(
            """UPDATE role_purchase_requests
               SET status = ?, reviewer_id = ?, reviewed_at = ? WHERE id = ?""",
            (status, reviewer_id, _utc_now(), request_id),
        )
        await db.commit()


async def complete_role_purchase_approval(
    request_id: int,
    reviewer_id: int,
    user_id: int,
    role_id: int,
    role_name: str,
    price: int,
) -> bool:
    """Register the Discord role and finalize its approval atomically."""
    async with _connect() as db:
        await begin_write(db, "role-request", request_id)
        request = await _one(
            db,
            """SELECT 1 FROM role_purchase_requests
               WHERE id = ? AND user_id = ? AND status = 'processing'
                 AND reviewer_id = ?
                 AND (discord_role_id IS NULL OR discord_role_id = ?)""",
            (request_id, user_id, reviewer_id, role_id),
        )
        if request is None:
            await db.rollback()
            return False
        now = _utc_now()
        await db.execute(
            """INSERT OR IGNORE INTO purchased_roles
               (user_id, role_id, role_name, price, role_kind, purchased_at)
               VALUES (?, ?, ?, ?, 'custom', ?)""",
            (user_id, role_id, role_name, price, now),
        )
        await db.execute(
            """UPDATE role_purchase_requests
               SET status = 'approved', reviewed_at = ?, discord_role_id = ?,
                   operation_state = 'completed', lease_until = NULL,
                   last_error = NULL WHERE id = ?""",
            (now, role_id, request_id),
        )
        await db.commit()
        return True


async def set_role_purchase_discord_role(
    request_id: int,
    reviewer_id: int,
    role_id: int,
    *,
    guild_id: int,
) -> bool:
    """Persist the external role ID and enqueue forward recovery atomically."""

    now = _utc_now()
    async with _connect() as db:
        await begin_write(db, "role-request", request_id)
        request = await _one(
            db,
            """SELECT user_id, role_name, price FROM role_purchase_requests
               WHERE id = ? AND reviewer_id = ? AND status = 'processing'""",
            (request_id, reviewer_id),
        )
        if request is None:
            await db.rollback()
            return False
        await db.execute(
            """UPDATE role_purchase_requests
               SET discord_role_id = ?, operation_state = 'discord_created',
                   lease_until = NULL, last_error = NULL WHERE id = ?""",
            (role_id, request_id),
        )
        key = f"role-request:{request_id}:approve"
        payload = json.dumps(
            {
                "request_id": request_id,
                "reviewer_id": reviewer_id,
                "user_id": request[0],
                "role_id": role_id,
                "role_name": request[1],
                "price": request[2],
            },
            ensure_ascii=False,
            separators=(",", ":"),
        )
        await db.execute(
            """INSERT OR IGNORE INTO operation_outbox
               (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                available_at, created_at, updated_at)
               VALUES (?, ?, 'role.approve', ?, ?, ?, ?, ?)""",
            (key, guild_id, str(request_id), payload, now, now, now),
        )
        await db.execute(
            """UPDATE operation_outbox
               SET state = 'retry', attempts = 0, available_at = ?,
                   lease_until = NULL, last_error = NULL, updated_at = ?
               WHERE idempotency_key = ? AND state = 'dead'""",
            (now, now, key),
        )
        await db.commit()
        return True


async def mark_role_purchase_needs_reconcile(
    request_id: int, reviewer_id: int, error: BaseException
) -> None:
    async with _connect() as db:
        await db.execute(
            """UPDATE role_purchase_requests
               SET operation_state = 'needs_reconcile', lease_until = NULL,
                   last_error = ?
               WHERE id = ? AND reviewer_id = ? AND status = 'processing'""",
            (f"{type(error).__name__}: {error}"[:1_000], request_id, reviewer_id),
        )
        await db.commit()


async def release_stale_role_purchase_claims() -> int:
    """Release claims that never created a Discord role after their lease expired."""

    async with _connect() as db:
        cursor = await db.execute(
            """UPDATE role_purchase_requests
               SET status = 'pending', reviewer_id = NULL, reviewed_at = NULL,
                   operation_state = 'pending', lease_until = NULL
               WHERE status = 'processing' AND discord_role_id IS NULL
                 AND lease_until IS NOT NULL AND lease_until <= ?""",
            (_utc_now(),),
        )
        await db.commit()
        return max(0, cursor.rowcount)


async def reject_role_purchase_with_refund(
    request_id: int,
    reviewer_id: int,
    refund: int,
    action: str,
    *,
    reason: str = "Заявка отклонена",
) -> bool:
    """Refund and durably queue the final Discord presentation atomically."""
    if refund < 0:
        raise ValueError("Refund cannot be negative")
    reason = reason.strip()[:500] or "Заявка отклонена"
    async with _connect() as db:
        await begin_write(db, "role-request", request_id)
        request = await _one(
            db,
            """SELECT user_id, guild_id FROM role_purchase_requests
               WHERE id = ? AND status = 'processing' AND reviewer_id = ?""",
            (request_id, reviewer_id),
        )
        if request is None:
            await db.rollback()
            return False
        now = _utc_now()
        await db.execute(
            "INSERT OR IGNORE INTO coin_accounts (user_id) VALUES (?)",
            (request[0],),
        )
        if refund:
            await db.execute(
                "UPDATE coin_accounts SET balance = balance + ? WHERE user_id = ?",
                (refund, request[0]),
            )
            await db.execute(
                """INSERT INTO coin_transactions
                   (user_id, amount, action, created_at) VALUES (?, ?, ?, ?)""",
                (request[0], refund, action, now),
            )
        await db.execute(
            """UPDATE role_purchase_requests
               SET status = 'rejected', reviewed_at = ?,
                   operation_state = 'completed', lease_until = NULL,
                   last_error = NULL, rejection_reason = ?, refund_amount = ?,
                   presentation_state = 'pending' WHERE id = ?""",
            (now, reason, refund, request_id),
        )
        if request[1] is not None:
            payload = json.dumps(
                {"request_id": request_id},
                ensure_ascii=False,
                separators=(",", ":"),
            )
            await db.execute(
                """INSERT OR IGNORE INTO operation_outbox
                   (idempotency_key, guild_id, kind, aggregate_id, payload_json,
                    available_at, created_at, updated_at)
                   VALUES (?, ?, 'role.reject.present', ?, ?, ?, ?, ?)""",
                (
                    f"role-request:{request_id}:reject-present",
                    int(request[1]),
                    str(request_id),
                    payload,
                    now,
                    now,
                    now,
                ),
            )
        await db.commit()
        return True


async def create_ticket_assignment(
    channel_id: int, owner_id: int, ticket_number: str
) -> None:
    now = _utc_now()
    async with _connect() as db:
        await db.execute(
            """INSERT INTO ticket_assignments
               (channel_id, owner_id, ticket_number, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(channel_id) DO UPDATE SET
                   owner_id = excluded.owner_id,
                   ticket_number = excluded.ticket_number,
                   updated_at = excluded.updated_at""",
            (channel_id, owner_id, ticket_number, now, now),
        )
        await db.commit()


async def get_ticket_assignment(channel_id: int):
    async with _connect() as db:
        return await _one(
            db,
            """SELECT owner_id, ticket_number, assigned_user_id
               FROM ticket_assignments WHERE channel_id = ?""",
            (channel_id,),
        )


async def assign_ticket(channel_id: int, user_id: int):
    async with _connect() as db:
        await begin_write(db, "ticket-channel", channel_id)
        row = await _one(
            db,
            "SELECT assigned_user_id FROM ticket_assignments WHERE channel_id = ?",
            (channel_id,),
        )
        if row is None:
            await db.rollback()
            return "missing", None
        if row[0] is not None:
            await db.rollback()
            return "assigned", row[0]
        await db.execute(
            """UPDATE ticket_assignments SET assigned_user_id = ?, updated_at = ?
               WHERE channel_id = ?""",
            (user_id, _utc_now(), channel_id),
        )
        await db.commit()
        return "ok", user_id


async def set_ticket_assignee(channel_id: int, user_id: int):
    async with _connect() as db:
        await begin_write(db, "ticket-channel", channel_id)
        row = await _one(
            db,
            "SELECT assigned_user_id FROM ticket_assignments WHERE channel_id = ?",
            (channel_id,),
        )
        if row is None:
            await db.rollback()
            return "missing", None
        await db.execute(
            """UPDATE ticket_assignments SET assigned_user_id = ?, updated_at = ?
               WHERE channel_id = ?""",
            (user_id, _utc_now(), channel_id),
        )
        await db.commit()
        return "ok", row[0]


async def unassign_ticket(channel_id: int, user_id: int, *, force: bool = False):
    async with _connect() as db:
        await begin_write(db, "ticket-channel", channel_id)
        row = await _one(
            db,
            "SELECT assigned_user_id FROM ticket_assignments WHERE channel_id = ?",
            (channel_id,),
        )
        if row is None:
            await db.rollback()
            return "missing", None
        if row[0] is None:
            await db.rollback()
            return "empty", None
        if not force and row[0] != user_id:
            await db.rollback()
            return "forbidden", row[0]
        old_user_id = row[0]
        await db.execute(
            """UPDATE ticket_assignments SET assigned_user_id = NULL, updated_at = ?
               WHERE channel_id = ?""",
            (_utc_now(), channel_id),
        )
        await db.commit()
        return "ok", old_user_id


async def delete_ticket_assignment(channel_id: int) -> None:
    async with _connect() as db:
        await db.execute("DELETE FROM ticket_assignments WHERE channel_id = ?", (channel_id,))
        await db.commit()


# Backward-compatible facade. New code imports domain repositories directly;
# these aliases keep third-party extensions and older tests working while the
# former monolithic persistence module is phased out.
from repositories.requests import (  # noqa: E402,F401
    claim_request_review,
    expire_request_review_attempt,
    fail_request_publication,
    finish_request_review,
    get_pending_request_publications,
    log_request,
    set_request_publication,
)
from repositories.treasury import (  # noqa: E402,F401
    add_treasury_transaction,
    get_last_transactions,
    get_treasury_balance,
    set_treasury_balance,
    set_treasury_balance_with_transaction,
)


# Extracted domain repositories, re-exported for existing imports.
from repositories.voice_rooms import (  # noqa: E402,F401
    add_voice_owner_ban,
    clear_voice_owner_bans,
    complete_voice_room_transfer,
    create_voice_room,
    delete_voice_room,
    get_all_voice_rooms,
    get_pending_voice_room_transfers,
    get_voice_owner_bans,
    get_voice_room,
    get_voice_room_by_owner,
    get_voice_rooms_snapshot,
    remove_voice_owner_ban,
    set_voice_room_message,
    set_voice_room_owner,
    transfer_voice_room_owner,
)
from repositories.cars import (  # noqa: E402,F401
    add_car,
    cancel_car_booking,
    car_bookings_ended_since,
    create_car_booking,
    edit_car,
    force_cancel_car_booking,
    get_active_car_bookings,
    get_active_user_car_bookings,
    get_cars_with_active_bookings,
    remove_car,
)
from repositories.giveaways import (  # noqa: E402,F401
    clear_giveaway_message,
    complete_giveaway_draw,
    create_giveaway,
    delete_giveaway,
    finish_giveaway,
    get_active_giveaways,
    get_giveaway,
    get_giveaway_draw,
    get_giveaway_entries,
    prepare_giveaway_draw,
    set_giveaway_message,
    toggle_giveaway_entry,
)
from repositories.birthdays import (  # noqa: E402,F401
    add_birthday,
    get_all_birthdays,
    remove_birthday,
)
from repositories.fundraisers import (  # noqa: E402,F401
    add_contribution,
    create_fundraiser,
    delete_contribution,
    delete_fundraiser_by_channel,
    get_fundraiser_by_channel,
    get_fundraiser_panels,
    set_fundraiser_message,
)
