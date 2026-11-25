from __future__ import annotations

import asyncio
import hashlib
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Mapping, Sequence

import aiosqlite


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    statements: Sequence[str] = field(default_factory=tuple)
    columns: Mapping[str, Mapping[str, str]] = field(default_factory=dict)

    @property
    def checksum(self) -> str:
        payload = "\n".join(
            [self.name, *self.statements]
            + [
                f"{table}.{column}:{definition}"
                for table, columns in self.columns.items()
                for column, definition in columns.items()
            ]
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()


MIGRATIONS = (
    Migration(
        1,
        "guild scoped infrastructure",
        statements=(
            """CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, key)
            )""",
            """CREATE TABLE IF NOT EXISTS static_messages (
                guild_id INTEGER NOT NULL,
                panel_key TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                revision INTEGER NOT NULL DEFAULT 1,
                asset_digest TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, panel_key)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_static_messages_channel
                ON static_messages (guild_id, channel_id)""",
            """CREATE TABLE IF NOT EXISTS command_receipts (
                guild_id INTEGER NOT NULL,
                interaction_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                state TEXT NOT NULL,
                result_ref TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, interaction_id, kind)
            )""",
            """CREATE TABLE IF NOT EXISTS operation_outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                idempotency_key TEXT NOT NULL UNIQUE,
                guild_id INTEGER NOT NULL,
                kind TEXT NOT NULL,
                aggregate_id TEXT,
                payload_json TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'pending',
                attempts INTEGER NOT NULL DEFAULT 0,
                available_at TEXT NOT NULL,
                lease_until TEXT,
                external_id TEXT,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_outbox_claim
                ON operation_outbox (state, available_at, lease_until, id)""",
            """CREATE TABLE IF NOT EXISTS task_health (
                name TEXT PRIMARY KEY,
                state TEXT NOT NULL,
                last_started TEXT,
                last_success TEXT,
                last_failure TEXT,
                consecutive_failures INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                duration_ms INTEGER,
                updated_at TEXT NOT NULL
            )""",
        ),
    ),
    Migration(
        2,
        "durable Discord operation state",
        columns={
            "role_purchase_requests": {
                "discord_role_id": "INTEGER",
                "operation_state": "TEXT NOT NULL DEFAULT 'pending'",
                "lease_until": "TEXT",
                "last_error": "TEXT",
            },
            "giveaways": {
                "guild_id": "INTEGER",
                "winner_ids": "TEXT",
                "result_message_id": "INTEGER",
                "draw_version": "INTEGER NOT NULL DEFAULT 0",
                "announcement_state": "TEXT NOT NULL DEFAULT 'pending'",
            },
            "summit_lists": {"guild_id": "INTEGER"},
            "summit_vzp_lists": {"guild_id": "INTEGER"},
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_giveaway_announcement
                ON giveaways (status, announcement_state, ends_at)""",
            """CREATE INDEX IF NOT EXISTS idx_role_request_reconcile
                ON role_purchase_requests (status, operation_state, lease_until)""",
            """CREATE INDEX IF NOT EXISTS idx_summit_guild_launch
                ON summit_lists (guild_id, launched, launched_at, kind, creator_id)""",
        ),
    ),
    Migration(
        3,
        "statistics retention and aggregates",
        statements=(
            """CREATE TABLE IF NOT EXISTS stats_pending_deletions (
                message_id INTEGER PRIMARY KEY,
                deleted_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS stats_daily_messages (
                guild_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                deleted INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, day, channel_id, user_id)
            )""",
            """CREATE TABLE IF NOT EXISTS stats_daily_voice (
                guild_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                channel_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                seconds INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, day, channel_id, user_id)
            )""",
            """CREATE TABLE IF NOT EXISTS stats_daily_members (
                guild_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                joined INTEGER NOT NULL DEFAULT 0,
                left_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, day)
            )""",
            """CREATE TABLE IF NOT EXISTS stats_daily_actions (
                guild_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                action_kind TEXT NOT NULL,
                outcome TEXT NOT NULL,
                total INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, day, action_kind, outcome)
            )""",
            """CREATE TABLE IF NOT EXISTS stats_retention_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_messages_period
                ON stats_daily_messages (guild_id, day)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_voice_period
                ON stats_daily_voice (guild_id, day)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_members_period
                ON stats_daily_members (guild_id, day)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_actions_period
                ON stats_daily_actions (guild_id, day)""",
        ),
    ),
    Migration(
        4,
        "components v2 polls",
        statements=(
            """CREATE TABLE IF NOT EXISTS polls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                creator_id INTEGER NOT NULL,
                question TEXT NOT NULL,
                options_json TEXT NOT NULL,
                multiple INTEGER NOT NULL DEFAULT 0,
                anonymous INTEGER NOT NULL DEFAULT 0,
                closes_at TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed')),
                created_at TEXT NOT NULL,
                closed_at TEXT
            )""",
            """CREATE TABLE IF NOT EXISTS poll_votes (
                poll_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                option_index INTEGER NOT NULL,
                voted_at TEXT NOT NULL,
                PRIMARY KEY (poll_id, user_id, option_index),
                FOREIGN KEY (poll_id) REFERENCES polls(id) ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_polls_active
                ON polls (status, closes_at, guild_id)""",
            """CREATE INDEX IF NOT EXISTS idx_poll_votes_totals
                ON poll_votes (poll_id, option_index)""",
        ),
    ),
    Migration(
        5,
        "durable fundraiser panels",
        columns={
            "birthday_fundraisers": {
                "guild_id": "INTEGER",
                "message_id": "INTEGER",
            },
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_fundraiser_panels
                ON birthday_fundraisers (guild_id, channel_id, message_id)""",
        ),
    ),
    Migration(
        6,
        "poll ui reconciliation revisions",
        columns={
            "polls": {
                "ui_revision": "INTEGER NOT NULL DEFAULT 0",
                "synced_revision": "INTEGER NOT NULL DEFAULT -1",
            },
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_poll_ui_sync
                ON polls (synced_revision, ui_revision, status, closes_at)""",
        ),
    ),
    Migration(
        7,
        "persistent request approvals",
        columns={
            "requests": {
                "guild_id": "INTEGER",
                "channel_id": "INTEGER",
                "message_id": "INTEGER",
            },
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_request_publications
                ON requests (status, guild_id, message_id, type)""",
        ),
    ),
    Migration(
        8,
        "time ordered statistics maintenance",
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_stats_messages_retention
                ON stats_messages (created_at, message_id)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_members_retention
                ON stats_member_events (event_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_voice_retention
                ON stats_voice_sessions (ended_at, id)
                WHERE ended_at IS NOT NULL""",
            """CREATE INDEX IF NOT EXISTS idx_stats_channels_retention
                ON stats_channel_events (event_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_actions_retention
                ON stats_processed_actions (processed_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_tombstones_retention
                ON stats_pending_deletions (deleted_at, message_id)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_messages_retention
                ON stats_daily_messages (day)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_voice_retention
                ON stats_daily_voice (day)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_members_retention
                ON stats_daily_members (day)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_actions_retention
                ON stats_daily_actions (day)""",
        ),
    ),
    Migration(
        9,
        "durable request publication and decision leases",
        columns={
            "requests": {
                "payload_json": "TEXT",
                "publication_error": "TEXT",
                "desired_outcome": "TEXT",
                "review_token": "TEXT",
                "review_lease_until": "TEXT",
            },
        },
        statements=(
            """UPDATE requests
                SET review_lease_until = COALESCE(reviewed_at, created_at)
                WHERE status = 'processing' AND review_lease_until IS NULL""",
            """CREATE INDEX IF NOT EXISTS idx_request_review_lease
                ON requests (status, review_lease_until, desired_outcome)""",
        ),
    ),
    Migration(
        10,
        "bounded operational history",
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_outbox_retention
                ON operation_outbox (state, completed_at, updated_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_receipts_retention
                ON command_receipts (state, updated_at)""",
            """CREATE INDEX IF NOT EXISTS idx_polls_retention
                ON polls (status, closed_at, id)""",
        ),
    ),
    Migration(
        11,
        "durable request decision publication",
        columns={
            "requests": {
                "decision_reason": "TEXT",
                "presentation_state": "TEXT NOT NULL DEFAULT 'completed'",
            },
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_request_presentation
                ON requests (presentation_state, status, guild_id, id)""",
        ),
    ),
    Migration(
        12,
        "durable summit launches",
        columns={
            "summit_lists": {
                "launch_state": "TEXT NOT NULL DEFAULT 'idle'",
                "launch_requested_at": "TEXT",
            },
        },
        statements=(
            """UPDATE summit_lists SET launch_state = 'completed'
                WHERE launched = 1 AND launch_state != 'completed'""",
            """CREATE INDEX IF NOT EXISTS idx_summit_reconcile
                ON summit_lists (guild_id, kind, message_id, launch_state, id)""",
        ),
    ),
    Migration(
        13,
        "durable voice ownership transfers",
        columns={
            "voice_rooms": {
                "previous_owner_id": "INTEGER",
                "transfer_state": "TEXT NOT NULL DEFAULT 'completed'",
            },
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_voice_transfer_reconcile
                ON voice_rooms (transfer_state, channel_id)""",
        ),
    ),
    Migration(
        14,
        "durable shop purchases and publications",
        columns={
            "role_purchase_requests": {
                "guild_id": "INTEGER",
                "channel_id": "INTEGER",
                "publication_state": "TEXT NOT NULL DEFAULT 'pending'",
                "publication_error": "TEXT",
            },
        },
        statements=(
            """CREATE TABLE IF NOT EXISTS shop_purchase_intents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interaction_id INTEGER NOT NULL UNIQUE,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                purchase_kind TEXT NOT NULL
                    CHECK (purchase_kind IN ('color', 'vip')),
                role_name TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price > 0),
                primary_color INTEGER,
                secondary_color INTEGER,
                tertiary_color INTEGER,
                vip_until TEXT,
                vip_days INTEGER,
                state TEXT NOT NULL DEFAULT 'pending'
                    CHECK (state IN ('pending', 'provisioning', 'discord_created', 'completed', 'refunded')),
                discord_role_id INTEGER,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                completed_at TEXT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_shop_intents_reconcile
                ON shop_purchase_intents (state, guild_id, updated_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_shop_intents_retention
                ON shop_purchase_intents (state, completed_at, id)""",
            """CREATE INDEX IF NOT EXISTS idx_role_request_publication
                ON role_purchase_requests
                   (status, publication_state, guild_id, message_id, id)""",
        ),
    ),
    Migration(
        15,
        "idempotent processed action statistics",
        statements=(
            """CREATE TABLE IF NOT EXISTS stats_action_receipts (
                guild_id INTEGER NOT NULL,
                event_key TEXT NOT NULL,
                created_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, event_key)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_stats_action_receipts_retention
                ON stats_action_receipts (created_at, guild_id, event_key)""",
        ),
    ),
    Migration(
        16,
        "durable shop role sales and rejection presentation",
        columns={
            "role_purchase_requests": {
                "rejection_reason": "TEXT",
                "refund_amount": "INTEGER NOT NULL DEFAULT 0",
                # Existing historical requests were already presented by the
                # legacy flow. Only a new rejection explicitly switches this
                # field to pending inside the refund transaction.
                "presentation_state": "TEXT NOT NULL DEFAULT 'completed'",
            },
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_role_rejection_presentation
                ON role_purchase_requests
                   (status, presentation_state, guild_id, id)""",
            """CREATE INDEX IF NOT EXISTS idx_purchased_role_sale_recovery
                ON purchased_roles (active, user_id, id)""",
        ),
    ),
    Migration(
        17,
        "activity coin economy and private groups",
        statements=(
            """CREATE TABLE IF NOT EXISTS coin_activity_state (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                pending_milli INTEGER NOT NULL DEFAULT 0,
                last_message_reward_at TEXT,
                PRIMARY KEY (guild_id, user_id)
            )""",
            """CREATE TABLE IF NOT EXISTS coin_activity_ledger (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                source TEXT NOT NULL CHECK (source IN ('message', 'voice')),
                event_key TEXT NOT NULL,
                units_milli INTEGER NOT NULL DEFAULT 0,
                coins_awarded INTEGER NOT NULL DEFAULT 0,
                eligible INTEGER NOT NULL DEFAULT 1,
                occurred_at TEXT NOT NULL,
                UNIQUE (source, event_key)
            )""",
            """CREATE TABLE IF NOT EXISTS coin_voice_sessions (
                guild_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                last_accrued_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, user_id)
            )""",
            """CREATE TABLE IF NOT EXISTS coin_activity_daily (
                guild_id INTEGER NOT NULL,
                day TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                units_milli INTEGER NOT NULL DEFAULT 0,
                coins_awarded INTEGER NOT NULL DEFAULT 0,
                events INTEGER NOT NULL DEFAULT 0,
                eligible_events INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (guild_id, day, user_id, source)
            )""",
            """CREATE TABLE IF NOT EXISTS private_group_channels (
                channel_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                creator_id INTEGER NOT NULL,
                member_ids_json TEXT NOT NULL,
                role_ids_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_coin_activity_period
                ON coin_activity_ledger (guild_id, occurred_at, source)""",
            """CREATE INDEX IF NOT EXISTS idx_coin_activity_user
                ON coin_activity_ledger (guild_id, user_id, occurred_at)""",
            """CREATE INDEX IF NOT EXISTS idx_coin_daily_period
                ON coin_activity_daily (guild_id, day, source)""",
            """CREATE INDEX IF NOT EXISTS idx_private_groups_creator
                ON private_group_channels (guild_id, creator_id)""",
        ),
    ),
    Migration(
        18,
        "persistent magic conversion lists",
        statements=(
            """CREATE TABLE IF NOT EXISTS magi_lists (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                message_id INTEGER,
                creator_id INTEGER NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS magi_members (
                list_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                display_name TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('mage', 'assistant')),
                joined_at TEXT NOT NULL,
                PRIMARY KEY (list_id, user_id),
                FOREIGN KEY (list_id) REFERENCES magi_lists(id) ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_magi_lists_messages
                ON magi_lists (guild_id, channel_id, message_id)""",
            """CREATE INDEX IF NOT EXISTS idx_magi_members_kind
                ON magi_members (list_id, kind, joined_at)""",
        ),
    ),
    Migration(
        19,
        "phantom magic list participants",
        statements=(
            """CREATE TABLE IF NOT EXISTS magi_phantoms (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                list_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                name_key TEXT NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('mage', 'assistant')),
                joined_at TEXT NOT NULL,
                UNIQUE (list_id, name_key),
                FOREIGN KEY (list_id) REFERENCES magi_lists(id) ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_magi_phantoms_kind
                ON magi_phantoms (list_id, kind, joined_at)""",
        ),
    ),
    Migration(
        20,
        "remove magic list feature",
        statements=(
            "DROP TABLE IF EXISTS magi_phantoms",
            "DROP TABLE IF EXISTS magi_members",
            "DROP TABLE IF EXISTS magi_lists",
        ),
    ),
    Migration(
        21,
        "nickname requests anonymous relay and game online statistics",
        columns={
            "requests": {
                "nickname_status": "TEXT NOT NULL DEFAULT 'idle'",
                "nickname_requested_by": "INTEGER",
                "nickname_requested_at": "TEXT",
                "nickname_confirmed_at": "TEXT",
                "nickname_dm_message_id": "INTEGER",
            },
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_request_nickname_dm
                ON requests (nickname_dm_message_id, nickname_status)""",
            """CREATE TABLE IF NOT EXISTS anonymous_conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                number TEXT NOT NULL,
                author_id INTEGER NOT NULL,
                public_channel_id INTEGER NOT NULL,
                public_message_id INTEGER NOT NULL,
                log_channel_id INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed')),
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, number)
            )""",
            """CREATE TABLE IF NOT EXISTS anonymous_message_links (
                message_id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                direction TEXT NOT NULL
                    CHECK (direction IN ('public', 'log', 'staff_dm')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id)
                    REFERENCES anonymous_conversations(id) ON DELETE CASCADE
            )""",
            """CREATE INDEX IF NOT EXISTS idx_anonymous_conversation_author
                ON anonymous_conversations (guild_id, author_id, status, id)""",
            """CREATE INDEX IF NOT EXISTS idx_anonymous_links_conversation
                ON anonymous_message_links (conversation_id, created_at)""",
            """CREATE TABLE IF NOT EXISTS game_server_daily_online (
                server_key TEXT NOT NULL,
                server_name TEXT NOT NULL,
                day TEXT NOT NULL,
                peak_players INTEGER NOT NULL,
                minimum_players INTEGER NOT NULL,
                sample_sum INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                last_players INTEGER NOT NULL,
                last_sample_at TEXT NOT NULL,
                PRIMARY KEY (server_key, day)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_game_online_period
                ON game_server_daily_online (server_key, day)""",
        ),
    ),
    Migration(
        22,
        "anonymous request discussion threads",
        columns={
            "anonymous_conversations": {
                "thread_id": "INTEGER",
            },
        },
        statements=(
            "DROP INDEX IF EXISTS idx_anonymous_links_conversation",
            "ALTER TABLE anonymous_message_links RENAME TO anonymous_message_links_v21",
            """CREATE TABLE anonymous_message_links (
                message_id INTEGER PRIMARY KEY,
                conversation_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                direction TEXT NOT NULL
                    CHECK (direction IN ('public', 'log', 'staff_dm', 'thread')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (conversation_id)
                    REFERENCES anonymous_conversations(id) ON DELETE CASCADE
            )""",
            """INSERT INTO anonymous_message_links
               (message_id, conversation_id, channel_id, direction, created_at)
               SELECT message_id, conversation_id, channel_id, direction, created_at
               FROM anonymous_message_links_v21""",
            "DROP TABLE anonymous_message_links_v21",
            """CREATE INDEX idx_anonymous_links_conversation
                ON anonymous_message_links (conversation_id, created_at)""",
        ),
    ),
    Migration(
        23,
        "shared cosmetic colors and durable paid music",
        statements=(
            "DROP INDEX IF EXISTS idx_purchased_roles_user_active",
            "DROP INDEX IF EXISTS idx_purchased_roles_active_user",
            "DROP INDEX IF EXISTS idx_purchased_role_sale_recovery",
            "ALTER TABLE purchased_roles RENAME TO purchased_roles_v22",
            """CREATE TABLE purchased_roles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                role_id INTEGER NOT NULL,
                role_name TEXT NOT NULL,
                price INTEGER NOT NULL CHECK (price > 0),
                role_kind TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1,
                purchased_at TEXT NOT NULL,
                sold_at TEXT
            )""",
            """INSERT INTO purchased_roles
               (id, user_id, role_id, role_name, price, role_kind,
                active, purchased_at, sold_at)
               SELECT id, user_id, role_id, role_name, price, role_kind,
                      active, purchased_at, sold_at
               FROM purchased_roles_v22""",
            "DROP TABLE purchased_roles_v22",
            """CREATE INDEX idx_purchased_roles_user_active
                ON purchased_roles (user_id, active)""",
            """CREATE INDEX idx_purchased_roles_active_user
                ON purchased_roles (active, user_id)""",
            """CREATE INDEX idx_purchased_role_sale_recovery
                ON purchased_roles (active, user_id, id)""",
            """CREATE UNIQUE INDEX idx_purchased_role_active_user_role
                ON purchased_roles (user_id, role_id)
                WHERE active IN (1, 2)""",
            """CREATE TABLE IF NOT EXISTS music_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                interaction_id INTEGER NOT NULL UNIQUE,
                guild_id INTEGER NOT NULL,
                text_channel_id INTEGER NOT NULL,
                voice_channel_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                query TEXT NOT NULL,
                webpage_url TEXT NOT NULL,
                title TEXT NOT NULL,
                duration_seconds INTEGER NOT NULL CHECK (duration_seconds > 0),
                price INTEGER NOT NULL CHECK (price > 0),
                state TEXT NOT NULL DEFAULT 'queued'
                    CHECK (state IN (
                        'queued', 'playing', 'completed',
                        'cancelled', 'refunded', 'failed'
                    )),
                refund_reason TEXT,
                requested_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_music_tracks_guild_state
                ON music_tracks (guild_id, state, id)""",
            """CREATE INDEX IF NOT EXISTS idx_music_tracks_requester
                ON music_tracks (requester_id, requested_at, state)""",
        ),
    ),
    Migration(
        24,
        "anonymous closure timestamps and per-track music volume",
        columns={
            "anonymous_conversations": {
                "closed_at": "TEXT",
            },
            "music_tracks": {
                "volume_percent": "INTEGER NOT NULL DEFAULT 50",
            },
        },
    ),
    Migration(
        25,
        "static message payload fingerprints",
        columns={
            "static_messages": {
                "payload_digest": "TEXT",
            },
        },
    ),
    Migration(
        26,
        "durable music player sessions",
        statements=(
            """CREATE TABLE IF NOT EXISTS music_sessions (
                guild_id INTEGER PRIMARY KEY,
                text_channel_id INTEGER NOT NULL,
                message_id INTEGER,
                repeat_mode TEXT NOT NULL DEFAULT 'off'
                    CHECK (repeat_mode IN ('off', 'track', 'queue')),
                updated_at TEXT NOT NULL
            )""",
        ),
    ),
    Migration(
        27,
        "durable temporary voice room attribution",
        columns={
            "voice_rooms": {
                "creator_id": "INTEGER",
            },
            "stats_voice_sessions": {
                "room_creator_id": "INTEGER",
            },
            "stats_daily_voice": {
                "channel_name": "TEXT",
                "room_creator_id": "INTEGER",
            },
        },
        statements=(
            """UPDATE stats_voice_sessions
               SET room_creator_id = (
                   SELECT voice_rooms.creator_id
                   FROM voice_rooms
                   WHERE voice_rooms.channel_id = stats_voice_sessions.channel_id
               )
               WHERE room_creator_id IS NULL
                 AND EXISTS (
                     SELECT 1 FROM voice_rooms
                     WHERE voice_rooms.channel_id = stats_voice_sessions.channel_id
                       AND voice_rooms.creator_id IS NOT NULL
                 )""",
            """CREATE INDEX IF NOT EXISTS idx_stats_voice_room_creator
               ON stats_voice_sessions
                  (guild_id, room_creator_id, started_at)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_daily_voice_room_creator
               ON stats_daily_voice
                  (guild_id, room_creator_id, day)""",
        ),
    ),
    Migration(
        28,
        "statistics channel audience snapshots",
        columns={
            "stats_messages": {
                "family_visible": "INTEGER",
            },
            "stats_voice_sessions": {
                "family_visible": "INTEGER",
            },
            "stats_channel_events": {
                "family_visible": "INTEGER",
            },
            "stats_daily_messages": {
                "family_visible": "INTEGER",
            },
            "stats_daily_voice": {
                "family_visible": "INTEGER",
            },
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_stats_messages_family_visible
               ON stats_messages (guild_id, family_visible, created_at)""",
            """CREATE INDEX IF NOT EXISTS idx_stats_voice_family_visible
               ON stats_voice_sessions
                  (guild_id, family_visible, started_at)""",
        ),
    ),
    Migration(
        29,
        "recoverable ticket lifecycle and durable recruiting baseline",
        statements=(
            # Ticket provisioning used to be a single try block: the Discord
            # channel was created first and nothing recorded how far the
            # operation had progressed. A failure after channel creation left
            # an orphan channel and a retry created a second one. This table
            # is the recovery record; ``interaction_id`` is the idempotency key.
            """CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                interaction_id INTEGER NOT NULL UNIQUE,
                ticket_type TEXT NOT NULL,
                number TEXT NOT NULL,
                author_id INTEGER NOT NULL,
                channel_id INTEGER,
                message_id INTEGER,
                status TEXT NOT NULL DEFAULT 'provisioning',
                assignee_id INTEGER,
                closed_by INTEGER,
                last_error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                closed_at TEXT
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_tickets_channel
                ON tickets (channel_id)
                WHERE channel_id IS NOT NULL""",
            """CREATE INDEX IF NOT EXISTS idx_tickets_status
                ON tickets (guild_id, status, created_at)""",
            """CREATE INDEX IF NOT EXISTS idx_tickets_number
                ON tickets (guild_id, number)""",
            # Invite use counters lived only in process memory, so after a
            # restart the first join was attributed to whichever managed invite
            # happened to have the highest historical use count.
            """CREATE TABLE IF NOT EXISTS recruiter_invite_uses (
                guild_id INTEGER NOT NULL,
                invite_code TEXT NOT NULL,
                last_uses INTEGER NOT NULL DEFAULT 0,
                observed_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, invite_code)
            )""",
            # Joins the bot refuses to attribute automatically: no baseline
            # yet, several invites moved at once, or a delta greater than one.
            # Guessing here silently credits the wrong recruiter forever.
            """CREATE TABLE IF NOT EXISTS recruiting_unresolved_joins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                candidates TEXT NOT NULL DEFAULT '',
                resolved_at TEXT,
                resolved_by INTEGER,
                recruiter_id INTEGER
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_recruiting_unresolved_member
                ON recruiting_unresolved_joins (guild_id, member_id, joined_at)""",
            """CREATE INDEX IF NOT EXISTS idx_recruiting_unresolved_pending
                ON recruiting_unresolved_joins (guild_id, resolved_at)""",
            # The car-park expiry watermark advanced before the refresh
            # succeeded, so a failed pass permanently skipped that window.
            """CREATE TABLE IF NOT EXISTS job_watermarks (
                guild_id INTEGER NOT NULL,
                job TEXT NOT NULL,
                position TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, job)
            )""",
        ),
    ),
    Migration(
        30,
        "durable spillover for message events that carry Coins",
        statements=(
            # One volatile queue served both statistics and Coins. On overflow
            # the event was dropped, which silently lost a payment. Statistics
            # may be lost with a metric; money may not. Overflowing events land
            # here and are replayed through the identical ingest path, so the
            # existing coin_activity_ledger idempotency still applies.
            """CREATE TABLE IF NOT EXISTS message_event_spillover (
                message_id INTEGER PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                channel_name TEXT NOT NULL,
                user_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                content_eligible INTEGER NOT NULL DEFAULT 0,
                family_visible INTEGER,
                spilled_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_message_spillover_order
                ON message_event_spillover (spilled_at, message_id)""",
        ),
    ),
    Migration(
        31,
        "persistent operational metrics",
        statements=(
            # Metrics lived only in a bounded in-memory registry, so every
            # restart erased the counters an operator would use to answer
            # "is this getting worse?". Counters are restored on startup;
            # snapshots keep a short trend for after-the-fact diagnosis.
            """CREATE TABLE IF NOT EXISTS metric_counters (
                name TEXT PRIMARY KEY,
                value INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )""",
            """CREATE TABLE IF NOT EXISTS metric_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                captured_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_metric_snapshots_time
                ON metric_snapshots (captured_at, id)""",
        ),
    ),
    Migration(
        32,
        "fold global settings into guild settings",
        statements=(
            # Two tables meant the same thing. ``guild_settings`` was correct;
            # ``settings`` predates it and had no guild column, so the weekly
            # statistics message id lived in one and its week in the other.
            #
            # Guild 0 is the parking slot for rows migrated from the global
            # table. The application resolves them under the configured
            # GUILD_ID first and falls back to 0, back-filling as it goes, so
            # nothing is lost even though this migration cannot know the id.
            """INSERT INTO guild_settings (guild_id, key, value, updated_at)
               SELECT 0, key, value, '1970-01-01T00:00:00+00:00'
                 FROM settings
                WHERE NOT EXISTS (
                    SELECT 1 FROM guild_settings
                     WHERE guild_settings.guild_id = 0
                       AND guild_settings.key = settings.key
                )""",
            """CREATE INDEX IF NOT EXISTS idx_guild_settings_key
                ON guild_settings (key)""",
        ),
    ),
    Migration(
        33,
        "runtime settings for live reconfiguration",
        statements=(
            # User-facing routes and thresholds. Not guild_settings: that table
            # holds panel IDs and counters with a different lifecycle.
            """CREATE TABLE IF NOT EXISTS runtime_settings (
                guild_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                updated_by TEXT,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (guild_id, key)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_runtime_settings_guild
                ON runtime_settings (guild_id)""",
        ),
    ),
    Migration(
        34,
        "decision log for administrative and domain decisions",
        statements=(
            # Settings and owner bypass used to leave no durable "was → became"
            # trail. The journal is append-only: readers filter, writers only
            # insert, and expired rows leave through retention — not by id.
            """CREATE TABLE IF NOT EXISTS decision_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                correlation_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                actor_name TEXT NOT NULL,
                action TEXT NOT NULL,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                before TEXT,
                after TEXT,
                reason TEXT,
                source TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_decision_log_guild_created
                ON decision_log (guild_id, created_at)""",
            """CREATE INDEX IF NOT EXISTS idx_decision_log_guild_subject
                ON decision_log (guild_id, subject_type, subject_id)""",
            """CREATE INDEX IF NOT EXISTS idx_decision_log_guild_actor
                ON decision_log (guild_id, actor_id)""",
        ),
    ),
    Migration(
        35,
        "named domain permission grants on roles",
        statements=(
            # Domain work used to require Discord Administrator or a High role.
            # Grants live on roles and change at runtime; they are not stored
            # in bot_config.toml. Notification subscriptions never appear here.
            """CREATE TABLE IF NOT EXISTS permission_grants (
                guild_id INTEGER NOT NULL,
                permission TEXT NOT NULL,
                role_id INTEGER NOT NULL,
                PRIMARY KEY (guild_id, permission, role_id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_permission_grants_role
                ON permission_grants (guild_id, role_id)""",
        ),
    ),
    Migration(
        36,
        "family context, roster, ranks, clearances and membership history",
        statements=(
            # Domain roster for Memphis. Empty on purpose: Milton rows are not
            # copied. One active family_context per guild; archived rows stay.
            # sort_order is used instead of ORDER — reserved on PostgreSQL.
            """CREATE TABLE IF NOT EXISTS family_context (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                project TEXT NOT NULL,
                server_key TEXT NOT NULL,
                family_name TEXT NOT NULL,
                family_tag TEXT NOT NULL,
                direction TEXT NOT NULL,
                member_limit INTEGER CHECK (
                    member_limit IS NULL OR member_limit > 0
                ),
                created_at TEXT NOT NULL,
                archived_at TEXT
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_family_context_active_guild
                ON family_context (guild_id)
                WHERE archived_at IS NULL""",
            """CREATE INDEX IF NOT EXISTS idx_family_context_guild
                ON family_context (guild_id)""",
            """CREATE TABLE IF NOT EXISTS ranks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                sort_order INTEGER NOT NULL,
                discord_role_id INTEGER,
                is_staff INTEGER NOT NULL DEFAULT 0
                    CHECK (is_staff IN (0, 1)),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_ranks_family
                ON ranks (guild_id, family_id, sort_order)""",
            """CREATE TABLE IF NOT EXISTS members (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                ic_name TEXT NOT NULL,
                game_character_id INTEGER,
                joined_at TEXT NOT NULL,
                left_at TEXT,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'candidate', 'probation', 'active', 'leave', 'left'
                    )
                ),
                rank_id INTEGER,
                mentor_id INTEGER,
                direction TEXT,
                prime_time TEXT,
                verified_at TEXT,
                verified_by INTEGER,
                is_selected INTEGER NOT NULL DEFAULT 1
                    CHECK (is_selected IN (0, 1)),
                CHECK (
                    (status = 'left' AND is_selected = 0)
                    OR status != 'left'
                ),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (rank_id) REFERENCES ranks(id)
                    ON DELETE SET NULL,
                FOREIGN KEY (mentor_id) REFERENCES members(id)
                    ON DELETE SET NULL
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_members_selected_current
                ON members (family_id, discord_id)
                WHERE status != 'left' AND is_selected = 1""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_members_current_character
                ON members (family_id, discord_id, game_character_id)
                WHERE status != 'left' AND game_character_id IS NOT NULL""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_members_current_unnamed
                ON members (family_id, discord_id)
                WHERE status != 'left' AND game_character_id IS NULL""",
            """CREATE INDEX IF NOT EXISTS idx_members_family_status
                ON members (guild_id, family_id, status)""",
            """CREATE INDEX IF NOT EXISTS idx_members_family_discord
                ON members (guild_id, family_id, discord_id)""",
            """CREATE TABLE IF NOT EXISTS clearances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                activity TEXT NOT NULL,
                granted_by INTEGER NOT NULL,
                granted_at TEXT NOT NULL,
                expires_at TEXT,
                rules_version TEXT,
                revoked_at TEXT,
                revoked_reason TEXT,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (member_id) REFERENCES members(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_clearances_member
                ON clearances (guild_id, family_id, member_id)""",
            """CREATE TABLE IF NOT EXISTS member_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                event_type TEXT NOT NULL CHECK (
                    event_type IN (
                        'join', 'promote', 'demote', 'leave', 'exit'
                    )
                ),
                actor_id INTEGER NOT NULL,
                actor_name TEXT NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (member_id) REFERENCES members(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_member_events_member
                ON member_events (guild_id, family_id, member_id, id)""",
            """CREATE INDEX IF NOT EXISTS idx_member_events_discord
                ON member_events (guild_id, family_id, discord_id, id)""",
        ),
    ),
    Migration(
        37,
        "onboarding tracks mentors training and rule versions",
        columns={
            "clearances": {
                "review_required_at": "TEXT",
            },
            "recruiting_referrals": {
                "verified_at": "TEXT",
                "retained_at": "TEXT",
            },
        },
        statements=(
            """CREATE TABLE IF NOT EXISTS onboarding_tracks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                application_number TEXT,
                specialization TEXT NOT NULL,
                mentor_id INTEGER,
                current_step TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('active', 'completed', 'cancelled')
                ),
                started_at TEXT NOT NULL,
                probation_until TEXT,
                UNIQUE (family_id, member_id),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (member_id) REFERENCES members(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (mentor_id) REFERENCES members(id)
                    ON DELETE SET NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_onboarding_tracks_mentor
                ON onboarding_tracks (guild_id, family_id, mentor_id, status)""",
            """CREATE TABLE IF NOT EXISTS training_assignments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                track_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                mentor_id INTEGER,
                specialization TEXT NOT NULL,
                step_key TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('open', 'done', 'cancelled')
                ),
                created_at TEXT NOT NULL,
                completed_at TEXT,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (track_id) REFERENCES onboarding_tracks(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (member_id) REFERENCES members(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_training_assignments_member
                ON training_assignments (guild_id, family_id, member_id, status)""",
            """CREATE TABLE IF NOT EXISTS rules_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                activity TEXT NOT NULL,
                version TEXT NOT NULL,
                published_at TEXT NOT NULL,
                published_by INTEGER NOT NULL,
                UNIQUE (family_id, activity, version),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS promotion_criteria (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                body TEXT NOT NULL,
                published_at TEXT NOT NULL,
                published_by INTEGER NOT NULL,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS promotion_proposals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                criteria_id INTEGER NOT NULL,
                proposed_by INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('pending', 'accepted', 'rejected')
                ),
                reason TEXT,
                decided_by INTEGER,
                created_at TEXT NOT NULL,
                decided_at TEXT,
                UNIQUE (family_id, member_id, criteria_id, status),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (member_id) REFERENCES members(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (criteria_id) REFERENCES promotion_criteria(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_promotion_proposals_member
                ON promotion_proposals (guild_id, family_id, member_id, status)""",
        ),
    ),
    Migration(
        38,
        "contract catalog and runs",
        statements=(
            """CREATE TABLE IF NOT EXISTS contract_catalog (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                direction TEXT NOT NULL CHECK (
                    direction IN ('legal', 'criminal')
                ),
                title TEXT NOT NULL,
                required_upgrades TEXT NOT NULL DEFAULT '[]',
                required_resources TEXT NOT NULL DEFAULT '[]',
                team_min INTEGER NOT NULL,
                team_max INTEGER NOT NULL,
                instruction_url TEXT,
                verified_at TEXT,
                reward_note TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('draft', 'available', 'archived')
                ),
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_contract_catalog_family
                ON contract_catalog (guild_id, family_id, status)""",
            """CREATE TABLE IF NOT EXISTS contract_runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                catalog_id INTEGER NOT NULL,
                snapshot_json TEXT NOT NULL,
                responsible_discord_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'draft', 'available', 'in_work', 'report_submitted',
                        'accepted', 'rework', 'rejected', 'closed',
                        'cancelled', 'partial'
                    )
                ),
                issued_resources TEXT NOT NULL DEFAULT '[]',
                started_at TEXT,
                finished_at TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (catalog_id) REFERENCES contract_catalog(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_contract_runs_family
                ON contract_runs (guild_id, family_id, status)""",
            """CREATE INDEX IF NOT EXISTS idx_contract_runs_catalog
                ON contract_runs (catalog_id, status)""",
            """CREATE TABLE IF NOT EXISTS contract_run_participants (
                run_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                share_bps INTEGER NOT NULL,
                joined_at TEXT NOT NULL,
                PRIMARY KEY (run_id, discord_id),
                FOREIGN KEY (run_id) REFERENCES contract_runs(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_contract_run_participants_member
                ON contract_run_participants (guild_id, family_id, discord_id)""",
        ),
    ),
    Migration(
        39,
        "contract reports and review decisions",
        statements=(
            """CREATE TABLE IF NOT EXISTS contract_reports (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                run_id INTEGER NOT NULL,
                attempt INTEGER NOT NULL,
                idempotency_key TEXT NOT NULL UNIQUE,
                result_amount INTEGER NOT NULL,
                result_note TEXT,
                volumes_json TEXT NOT NULL DEFAULT '[]',
                duration_minutes INTEGER NOT NULL,
                wants_payout INTEGER NOT NULL DEFAULT 1,
                disputed INTEGER NOT NULL DEFAULT 0,
                detailed INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'submitted', 'accepted', 'rework', 'rejected', 'reversed'
                    )
                ),
                submitted_by INTEGER NOT NULL,
                submitted_at TEXT NOT NULL,
                reviewed_by INTEGER,
                reviewed_at TEXT,
                review_reason TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                UNIQUE (run_id, attempt),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (run_id) REFERENCES contract_runs(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_contract_reports_run
                ON contract_reports (guild_id, family_id, run_id, status)""",
            """CREATE TABLE IF NOT EXISTS contract_report_shares (
                report_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                PRIMARY KEY (report_id, discord_id),
                FOREIGN KEY (report_id) REFERENCES contract_reports(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS contract_report_evidence (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                report_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('link', 'attachment')),
                url TEXT,
                content_hash TEXT NOT NULL,
                byte_size INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                FOREIGN KEY (report_id) REFERENCES contract_reports(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_report_evidence_hash
                ON contract_report_evidence (guild_id, content_hash)""",
            """CREATE TABLE IF NOT EXISTS report_payout_obligations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                report_id INTEGER NOT NULL UNIQUE,
                run_id INTEGER NOT NULL,
                amount INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'cancelled')),
                created_at TEXT NOT NULL,
                FOREIGN KEY (report_id) REFERENCES contract_reports(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS contract_report_reversals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                report_id INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (report_id) REFERENCES contract_reports(id)
                    ON DELETE RESTRICT
            )""",
        ),
    ),
    Migration(
        40,
        "contract report profit lines",
        columns={
            "contract_catalog": {
                "profit_fields": "TEXT NOT NULL DEFAULT ''",
            },
            "contract_reports": {
                "cash_amount": "INTEGER",
                "reputation_delta": "INTEGER",
                "has_profit": "INTEGER NOT NULL DEFAULT 0",
            },
        },
        statements=(
            """CREATE TABLE IF NOT EXISTS contract_report_purchases (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                report_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                unit_price INTEGER NOT NULL,
                FOREIGN KEY (report_id) REFERENCES contract_reports(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_report_purchases_report
                ON contract_report_purchases (guild_id, family_id, report_id)""",
            """CREATE TABLE IF NOT EXISTS contract_report_expenses (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                report_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                amount INTEGER NOT NULL,
                FOREIGN KEY (report_id) REFERENCES contract_reports(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS contract_report_item_payouts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                report_id INTEGER NOT NULL,
                item_name TEXT NOT NULL,
                quantity INTEGER NOT NULL,
                valuation INTEGER NOT NULL,
                FOREIGN KEY (report_id) REFERENCES contract_reports(id)
                    ON DELETE RESTRICT
            )""",
        ),
    ),
    Migration(
        41,
        "staff queue overlay and appeals",
        statements=(
            """CREATE TABLE IF NOT EXISTS queue_claims (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                item_kind TEXT NOT NULL,
                item_id TEXT NOT NULL,
                assignee_id INTEGER,
                claimed_at TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                escalated_to INTEGER,
                escalated_at TEXT,
                delay_reason TEXT,
                last_transfer_reason TEXT,
                ping_user_id INTEGER,
                UNIQUE (guild_id, family_id, item_kind, item_id)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_queue_claims_assignee
                ON queue_claims (guild_id, family_id, assignee_id)""",
            """CREATE TABLE IF NOT EXISTS queue_transfers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                item_kind TEXT NOT NULL,
                item_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                from_id INTEGER,
                to_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_queue_transfers_item
                ON queue_transfers (guild_id, family_id, item_kind, item_id)""",
            """CREATE TABLE IF NOT EXISTS staff_appeals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                author_id INTEGER NOT NULL,
                accused_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('open', 'closed')),
                created_at TEXT NOT NULL
            )""",
            """CREATE INDEX IF NOT EXISTS idx_staff_appeals_open
                ON staff_appeals (guild_id, family_id, status, created_at)""",
        ),
    ),
    Migration(
        42,
        "family events roster and snapshots",
        statements=(
            """CREATE TABLE IF NOT EXISTS family_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                template_key TEXT NOT NULL,
                title TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                commander_id INTEGER NOT NULL,
                meetup TEXT NOT NULL,
                capacity INTEGER NOT NULL,
                reserve_capacity INTEGER NOT NULL DEFAULT 0,
                resource_plan TEXT NOT NULL DEFAULT '',
                f2_event_ref TEXT,
                snapshot_json TEXT NOT NULL,
                status TEXT NOT NULL CHECK (status IN ('scheduled', 'cancelled')),
                version INTEGER NOT NULL DEFAULT 1,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                cancelled_at TEXT,
                cancelled_by INTEGER,
                UNIQUE (guild_id, family_id, template_key, starts_at)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_family_events_schedule
                ON family_events (guild_id, family_id, starts_at, status)""",
            """CREATE TABLE IF NOT EXISTS event_roster (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                slot TEXT NOT NULL CHECK (slot IN ('main', 'reserve')),
                role_key TEXT,
                joined_at TEXT NOT NULL,
                UNIQUE (event_id, discord_id),
                FOREIGN KEY (event_id) REFERENCES family_events(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_event_roster_event
                ON event_roster (guild_id, family_id, event_id, slot)""",
        ),
    ),
    Migration(
        43,
        "event attendance lock and reserve queue",
        columns={
            "family_events": {
                "roster_locked_at": "TEXT",
                "roster_locked_by": "INTEGER",
            },
            "event_roster": {
                "attendance": "TEXT NOT NULL DEFAULT 'joined'",
                "voice_seen_at": "TEXT",
                "last_reason": "TEXT",
            },
        },
        statements=(
            """UPDATE event_roster SET attendance = 'reserve' WHERE slot = 'reserve'""",
            """CREATE TABLE IF NOT EXISTS event_attendance_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                discord_id INTEGER NOT NULL,
                from_status TEXT NOT NULL,
                to_status TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                FOREIGN KEY (event_id) REFERENCES family_events(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_event_attendance_log_event
                ON event_attendance_log (guild_id, family_id, event_id, id)""",
        ),
    ),
    Migration(
        44,
        "accounts ledger obligations and reconciliations",
        statements=(
            """CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                name TEXT NOT NULL,
                holder_kind TEXT NOT NULL,
                holder_id INTEGER NOT NULL DEFAULT 0,
                responsible_id INTEGER,
                context TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, currency, holder_kind, holder_id, context)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_accounts_family
                ON accounts (guild_id, family_id, context, archived)""",
            """CREATE TABLE IF NOT EXISTS ledger_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                operation TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                basis_kind TEXT NOT NULL,
                basis_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                source_ref TEXT,
                reversal_of INTEGER,
                UNIQUE (guild_id, family_id, account_id, basis_kind, basis_id, operation),
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_ledger_account
                ON ledger_entries (guild_id, family_id, account_id, id)""",
            """CREATE TABLE IF NOT EXISTS obligations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                basis_kind TEXT NOT NULL,
                basis_id TEXT NOT NULL,
                payee_id INTEGER NOT NULL,
                currency TEXT NOT NULL,
                amount INTEGER,
                terms_version TEXT,
                status TEXT NOT NULL CHECK (
                    status IN ('promised', 'approved', 'paid', 'cancelled')
                ),
                promised_by INTEGER,
                approved_by INTEGER,
                second_approved_by INTEGER,
                paid_by INTEGER,
                confirmed_by INTEGER,
                proof_ref TEXT,
                batch_id TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, basis_kind, basis_id),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_obligations_status
                ON obligations (guild_id, family_id, status, created_at)""",
            """CREATE TABLE IF NOT EXISTS account_reconciliations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                account_id INTEGER NOT NULL,
                game_balance INTEGER NOT NULL,
                book_balance INTEGER NOT NULL,
                delta INTEGER NOT NULL,
                explanation TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (account_id) REFERENCES accounts(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_reconciliations_account
                ON account_reconciliations (guild_id, family_id, account_id, id)""",
        ),
    ),
    Migration(
        45,
        "contribution merits rule versions and ledger",
        statements=(
            """CREATE TABLE IF NOT EXISTS contribution_rule_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                version INTEGER NOT NULL,
                contract_amount INTEGER NOT NULL CHECK (contract_amount >= 0),
                attendance_amount INTEGER NOT NULL CHECK (attendance_amount >= 0),
                mentoring_amount INTEGER NOT NULL CHECK (mentoring_amount >= 0),
                warehouse_amount INTEGER NOT NULL CHECK (warehouse_amount >= 0),
                logistics_amount INTEGER NOT NULL CHECK (logistics_amount >= 0),
                duty_amount INTEGER NOT NULL CHECK (duty_amount >= 0),
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, version),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_contribution_rules_family
                ON contribution_rule_versions (guild_id, family_id, version)""",
            """CREATE TABLE IF NOT EXISTS contribution_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                member_discord_id INTEGER NOT NULL,
                category TEXT NOT NULL,
                amount INTEGER NOT NULL CHECK (amount > 0),
                operation TEXT NOT NULL CHECK (operation IN ('award', 'reverse')),
                basis_kind TEXT NOT NULL,
                basis_id TEXT NOT NULL,
                rule_version INTEGER NOT NULL,
                actor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                reversal_of INTEGER,
                UNIQUE (
                    guild_id, family_id, member_discord_id,
                    basis_kind, basis_id, operation
                ),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_contribution_member
                ON contribution_entries (guild_id, family_id, member_discord_id, category)""",
            """CREATE TABLE IF NOT EXISTS contribution_bonus_marks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                period_key TEXT NOT NULL,
                marked_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, period_key),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
        ),
    ),
    Migration(
        46,
        "bonus periods and proposed shares",
        statements=(
            """CREATE TABLE IF NOT EXISTS bonus_periods (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                period_key TEXT NOT NULL,
                period_kind TEXT NOT NULL CHECK (period_kind IN ('week', 'rating')),
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                fund_amount INTEGER NOT NULL CHECK (fund_amount >= 0),
                mentor_fund INTEGER NOT NULL CHECK (mentor_fund >= 0),
                logistics_fund INTEGER NOT NULL CHECK (logistics_fund >= 0),
                min_points INTEGER NOT NULL CHECK (min_points >= 0),
                max_share_percent INTEGER NOT NULL CHECK (
                    max_share_percent >= 0 AND max_share_percent <= 100
                ),
                rule_version INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('draft', 'proposed', 'approved', 'cancelled')
                ),
                declared_by INTEGER NOT NULL,
                approved_by INTEGER,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, period_key),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_bonus_periods_family
                ON bonus_periods (guild_id, family_id, status, id)""",
            """CREATE TABLE IF NOT EXISTS bonus_shares (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                period_id INTEGER NOT NULL,
                member_discord_id INTEGER NOT NULL,
                pool TEXT NOT NULL CHECK (pool IN ('combat', 'mentoring', 'logistics')),
                points INTEGER NOT NULL CHECK (points >= 0),
                proposed_amount INTEGER NOT NULL CHECK (proposed_amount >= 0),
                adjusted_amount INTEGER NOT NULL CHECK (adjusted_amount >= 0),
                UNIQUE (period_id, member_discord_id, pool),
                FOREIGN KEY (period_id) REFERENCES bonus_periods(id) ON DELETE RESTRICT,
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_bonus_shares_period
                ON bonus_shares (period_id, member_discord_id)""",
        ),
    ),
    Migration(
        47,
        "storage locations, inventory movements and kit reserves",
        statements=(
            """CREATE TABLE IF NOT EXISTS storage_locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (
                    kind IN (
                        'warehouse', 'office_safe', 'gun_cabinet', 'pantry',
                        'wardrobe', 'garage', 'mat_truck', 'handout'
                    )
                ),
                name TEXT NOT NULL,
                capacity INTEGER NOT NULL CHECK (capacity >= 0),
                capacity_unit TEXT NOT NULL,
                mat_level INTEGER NOT NULL DEFAULT 0 CHECK (
                    mat_level >= 0 AND mat_level <= 3
                ),
                sold INTEGER NOT NULL DEFAULT 0 CHECK (sold IN (0, 1)),
                auctioned INTEGER NOT NULL DEFAULT 0 CHECK (auctioned IN (0, 1)),
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, kind),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_storage_locations_family
                ON storage_locations (guild_id, family_id, kind)""",
            """CREATE TABLE IF NOT EXISTS inventory_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                sku TEXT NOT NULL,
                title TEXT NOT NULL,
                unit TEXT NOT NULL,
                low_stock INTEGER NOT NULL DEFAULT 0 CHECK (low_stock >= 0),
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, sku),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS inventory_balances (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                location_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                on_hand INTEGER NOT NULL CHECK (on_hand >= 0),
                reserved INTEGER NOT NULL CHECK (reserved >= 0),
                CHECK (reserved <= on_hand),
                UNIQUE (location_id, item_id),
                FOREIGN KEY (location_id) REFERENCES storage_locations(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (item_id) REFERENCES inventory_items(id) ON DELETE RESTRICT,
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_inventory_balances_location
                ON inventory_balances (family_id, location_id, item_id)""",
            """CREATE TABLE IF NOT EXISTS inventory_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                from_location_id INTEGER,
                to_location_id INTEGER,
                operation TEXT NOT NULL CHECK (
                    operation IN (
                        'receive', 'reserve', 'release', 'issue',
                        'return', 'consume', 'adjust'
                    )
                ),
                basis_kind TEXT NOT NULL,
                basis_id TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                reason TEXT,
                created_at TEXT NOT NULL,
                UNIQUE (
                    guild_id, family_id, item_id, operation, basis_kind, basis_id
                ),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_inventory_movements_family
                ON inventory_movements (guild_id, family_id, created_at)""",
            """CREATE TABLE IF NOT EXISTS inventory_reservations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                location_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                event_id INTEGER,
                kit_id INTEGER,
                status TEXT NOT NULL CHECK (
                    status IN ('open', 'released', 'issued')
                ),
                actor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_inventory_res_event
                ON inventory_reservations (family_id, event_id, status)""",
            """CREATE TABLE IF NOT EXISTS inventory_kits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                requester_id INTEGER NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN (
                        'requested', 'reserved', 'issued', 'returned', 'cancelled'
                    )
                ),
                reserved_by INTEGER,
                issued_by INTEGER,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_inventory_kits_event
                ON inventory_kits (family_id, event_id, status)""",
            """CREATE TABLE IF NOT EXISTS inventory_kit_lines (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                kit_id INTEGER NOT NULL,
                item_id INTEGER NOT NULL,
                requested INTEGER NOT NULL CHECK (requested > 0),
                reserved INTEGER NOT NULL DEFAULT 0 CHECK (reserved >= 0),
                issued INTEGER NOT NULL DEFAULT 0 CHECK (issued >= 0),
                returned INTEGER NOT NULL DEFAULT 0 CHECK (returned >= 0),
                consumed INTEGER NOT NULL DEFAULT 0 CHECK (consumed >= 0),
                UNIQUE (kit_id, item_id),
                FOREIGN KEY (kit_id) REFERENCES inventory_kits(id) ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS inventory_reconciliations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                location_id INTEGER NOT NULL,
                note TEXT NOT NULL,
                actor_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (location_id) REFERENCES storage_locations(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_inventory_recon_location
                ON inventory_reconciliations (location_id, id)""",
            """CREATE TABLE IF NOT EXISTS inventory_tax_duties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                property_kind TEXT NOT NULL DEFAULT 'warehouse',
                due_at TEXT NOT NULL,
                responsible_id INTEGER NOT NULL,
                backup_id INTEGER NOT NULL,
                last_paid_at TEXT,
                UNIQUE (guild_id, family_id, property_kind),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
        ),
    ),
    Migration(
        48,
        "vehicle owner link, event bookings, family property",
        columns={
            "cars": {
                "guild_id": "INTEGER",
                "family_id": "INTEGER",
                "owner_member_id": "INTEGER",
                "purpose": "TEXT NOT NULL DEFAULT ''",
                "cargo_kg": "INTEGER NOT NULL DEFAULT 0",
                "restrictions": "TEXT NOT NULL DEFAULT ''",
                "access_note": "TEXT NOT NULL DEFAULT ''",
                "state": "TEXT NOT NULL DEFAULT 'active'",
                "version": "INTEGER NOT NULL DEFAULT 1",
            },
            "car_bookings": {
                "purpose": "TEXT NOT NULL DEFAULT ''",
                "event_id": "INTEGER",
                "driver_member_id": "INTEGER",
                "returned_at": "TEXT",
                "returned_by": "INTEGER",
                "warning": "TEXT NOT NULL DEFAULT ''",
            },
        },
        statements=(
            """CREATE TABLE IF NOT EXISTS family_properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (kind IN ('office', 'warehouse', 'other')),
                title TEXT NOT NULL,
                responsible_id INTEGER NOT NULL,
                backup_id INTEGER NOT NULL,
                due_at TEXT NOT NULL,
                last_paid_at TEXT,
                executed INTEGER NOT NULL DEFAULT 0,
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, kind, title),
                FOREIGN KEY (family_id) REFERENCES family_context(id) ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_cars_owner_member
                ON cars (owner_member_id, state)""",
            """CREATE INDEX IF NOT EXISTS idx_car_bookings_interval
                ON car_bookings (car_id, starts_at, ends_at)""",
            """CREATE INDEX IF NOT EXISTS idx_car_bookings_event
                ON car_bookings (event_id)""",
            """CREATE INDEX IF NOT EXISTS idx_family_properties_family
                ON family_properties (guild_id, family_id, kind)""",
        ),
    ),
    Migration(
        49,
        "org upgrade tree, season right, lore revisions",
        statements=(
            """CREATE TABLE IF NOT EXISTS org_upgrades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                key TEXT NOT NULL,
                title TEXT NOT NULL,
                track TEXT NOT NULL CHECK (
                    track IN ('legal_contracts', 'illegal_contracts', 'other')
                ),
                max_level INTEGER NOT NULL DEFAULT 9 CHECK (
                    max_level >= 1 AND max_level <= 9
                ),
                depends_on TEXT NOT NULL DEFAULT '',
                depends_level INTEGER NOT NULL DEFAULT 0 CHECK (depends_level >= 0),
                requires_org_level INTEGER NOT NULL DEFAULT 0
                    CHECK (requires_org_level >= 0),
                requires_contracts INTEGER NOT NULL DEFAULT 0
                    CHECK (requires_contracts >= 0),
                requires_sku TEXT NOT NULL DEFAULT '',
                requires_qty INTEGER NOT NULL DEFAULT 0 CHECK (requires_qty >= 0),
                cost_reputation INTEGER NOT NULL DEFAULT 0
                    CHECK (cost_reputation >= 0),
                source TEXT NOT NULL DEFAULT '',
                verified_at TEXT,
                lore_revision TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                UNIQUE (guild_id, family_id, key),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS org_upgrade_progress (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                upgrade_id INTEGER NOT NULL,
                current_level INTEGER NOT NULL DEFAULT 0 CHECK (
                    current_level >= 0 AND current_level <= 9
                ),
                UNIQUE (upgrade_id),
                FOREIGN KEY (upgrade_id) REFERENCES org_upgrades(id)
                    ON DELETE RESTRICT,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS org_development (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                org_level INTEGER NOT NULL DEFAULT 0 CHECK (org_level >= 0),
                spent_reputation INTEGER NOT NULL DEFAULT 0
                    CHECK (spent_reputation >= 0),
                direction_changed_at TEXT,
                cooldown_until TEXT,
                pending_direction TEXT,
                pending_spent INTEGER,
                pending_preview_at TEXT,
                goal_upgrade_id INTEGER,
                goal_assignee_id INTEGER,
                goal_note TEXT NOT NULL DEFAULT '',
                lore_revision TEXT NOT NULL DEFAULT '',
                lore_published_at TEXT,
                version INTEGER NOT NULL DEFAULT 1,
                UNIQUE (family_id),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS org_lore_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                kind TEXT NOT NULL CHECK (
                    kind IN ('upgrade', 'template', 'clearance')
                ),
                subject_key TEXT NOT NULL,
                lore_revision TEXT NOT NULL,
                status TEXT NOT NULL CHECK (
                    status IN ('needs_review', 'reviewed')
                ),
                UNIQUE (family_id, kind, subject_key, lore_revision),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS season_play_hours (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                member_id INTEGER NOT NULL,
                season_key TEXT NOT NULL,
                hours INTEGER NOT NULL CHECK (hours >= 0),
                entered_by INTEGER NOT NULL,
                entered_at TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'human' CHECK (source = 'human'),
                UNIQUE (family_id, member_id, season_key),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE TABLE IF NOT EXISTS family_seasons (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                season_key TEXT NOT NULL,
                starts_on TEXT NOT NULL,
                ends_on TEXT NOT NULL,
                influence_daily_limit INTEGER NOT NULL DEFAULT 0
                    CHECK (influence_daily_limit >= 0),
                prize_places TEXT NOT NULL DEFAULT '',
                half_loss_threshold INTEGER NOT NULL DEFAULT 0
                    CHECK (half_loss_threshold >= 0),
                announced_by INTEGER NOT NULL,
                announced_at TEXT NOT NULL,
                UNIQUE (family_id, season_key),
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE INDEX IF NOT EXISTS idx_org_upgrades_family
                ON org_upgrades (family_id, track, key)""",
            """CREATE INDEX IF NOT EXISTS idx_org_lore_reviews_open
                ON org_lore_reviews (family_id, status, lore_revision)""",
            """CREATE INDEX IF NOT EXISTS idx_season_hours_member
                ON season_play_hours (family_id, season_key, member_id)""",
        ),
    ),
    Migration(
        50,
        "last trusted external API snapshots",
        statements=(
            """CREATE TABLE IF NOT EXISTS external_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                source_key TEXT NOT NULL CHECK (
                    source_key IN ('rage_master', 'majestic_api')
                ),
                resource TEXT NOT NULL,
                payload_text TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                attribution TEXT NOT NULL DEFAULT '',
                UNIQUE (guild_id, source_key, resource)
            )""",
            """CREATE INDEX IF NOT EXISTS idx_external_snapshots_lookup
                ON external_snapshots (guild_id, source_key, resource)""",
        ),
    ),
    Migration(
        51,
        "family map catalog",
        statements=(
            """CREATE TABLE IF NOT EXISTS family_maps (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                family_id INTEGER NOT NULL,
                name TEXT NOT NULL,
                event_type TEXT NOT NULL,
                version TEXT NOT NULL,
                source TEXT NOT NULL,
                created_by INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                archived_at TEXT,
                archived_by INTEGER,
                FOREIGN KEY (family_id) REFERENCES family_context(id)
                    ON DELETE RESTRICT
            )""",
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_family_maps_active_name
                ON family_maps (family_id, name)
                WHERE archived_at IS NULL""",
            """CREATE INDEX IF NOT EXISTS idx_family_maps_family
                ON family_maps (family_id, archived_at)""",
        ),
    ),
    Migration(
        52,
        "archive mark for legacy summit gatherings",
        columns={
            "summit_lists": {"archived_at": "TEXT"},
            "summit_vzp_lists": {"archived_at": "TEXT"},
        },
        statements=(
            """CREATE INDEX IF NOT EXISTS idx_summit_lists_archived
                ON summit_lists (kind, archived_at)""",
            """CREATE INDEX IF NOT EXISTS idx_summit_vzp_lists_archived
                ON summit_vzp_lists (archived_at)""",
        ),
    ),
)


def _validate_migration_sequence() -> None:
    versions = [migration.version for migration in MIGRATIONS]
    expected = list(range(1, len(MIGRATIONS) + 1))
    if versions != expected:
        raise RuntimeError(
            f"Migration versions must be contiguous: expected {expected}, got {versions}"
        )


_validate_migration_sequence()


async def _columns(db: aiosqlite.Connection, table: str) -> set[str]:
    rows = await db.execute_fetchall(f"PRAGMA table_info({table})")
    return {str(row[1]) for row in rows}


async def _backup(db: aiosqlite.Connection, path: Path, version: int) -> Path | None:
    if not path.exists() or path.stat().st_size == 0:
        return None
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    destination = path.with_name(f"{path.name}.pre-v{version}.{stamp}.bak")
    target = sqlite3.connect(destination, check_same_thread=False)
    try:
        await db.backup(target)
    finally:
        target.close()
    backups = sorted(
        path.parent.glob(f"{path.name}.pre-v*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[7:]:
        stale.unlink(missing_ok=True)
    return destination


def _preflight_backup_sync(path: Path) -> Path | None:
    """Back up an existing database before legacy bootstrap can alter it."""

    if not path.exists() or path.stat().st_size == 0:
        return None
    source = sqlite3.connect(path, check_same_thread=False)
    try:
        table = source.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        current = 0
        if table:
            row = source.execute(
                "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
            ).fetchone()
            current = int(row[0]) if row else 0
        if current >= MIGRATIONS[-1].version:
            return None
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
        destination = path.with_name(
            f"{path.name}.pre-v{current + 1}.{stamp}.bak"
        )
        target = sqlite3.connect(destination, check_same_thread=False)
        try:
            source.backup(target)
        finally:
            target.close()
    finally:
        source.close()
    backups = sorted(
        path.parent.glob(f"{path.name}.pre-v*.bak"),
        key=lambda item: item.stat().st_mtime,
        reverse=True,
    )
    for stale in backups[7:]:
        stale.unlink(missing_ok=True)
    return destination


async def backup_before_schema(path: Path) -> Path | None:
    return await asyncio.to_thread(_preflight_backup_sync, path)


async def apply_migrations(
    db: aiosqlite.Connection, path: Path, *, create_backup: bool = True
) -> int:
    await db.execute(
        """CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        )"""
    )
    await db.commit()
    rows = await db.execute_fetchall(
        "SELECT version, checksum FROM schema_migrations ORDER BY version"
    )
    # Close the read transaction so the first BEGIN IMMEDIATE is not nested
    # (PostgreSQL warns: "there is already a transaction in progress").
    await db.commit()
    applied = {int(version): str(checksum) for version, checksum in rows}
    pending = [migration for migration in MIGRATIONS if migration.version not in applied]
    for migration in MIGRATIONS:
        checksum = applied.get(migration.version)
        if checksum is not None and checksum != migration.checksum:
            raise RuntimeError(
                f"Migration {migration.version} ({migration.name}) checksum changed"
            )
    if pending and create_backup:
        await _backup(db, path, pending[0].version)

    for migration in pending:
        await db.execute("BEGIN IMMEDIATE")
        try:
            for table, requested in migration.columns.items():
                existing = await _columns(db, table)
                for column, definition in requested.items():
                    if column not in existing:
                        await db.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
            for statement in migration.statements:
                await db.execute(statement)
            await db.execute(
                """INSERT INTO schema_migrations
                   (version, name, applied_at, checksum) VALUES (?, ?, ?, ?)""",
                (
                    migration.version,
                    migration.name,
                    datetime.now(UTC).isoformat(),
                    migration.checksum,
                ),
            )
            await db.commit()
        except BaseException:
            await db.rollback()
            raise
    return MIGRATIONS[-1].version
