import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import discord

from cogs.music import (
    GuildMusicState,
    MusicControlsView,
    MusicCog,
    _FFMPEG_BEFORE_OPTIONS,
    _allowed_url,
    _discard_scheduled_replays,
    _has_pending_music_work,
    _player_embed,
    _schedule_repeat,
)
from repositories.music import (
    complete_music_track,
    create_paid_music_track,
    delete_music_session,
    get_music_session,
    get_music_statistics,
    mark_music_track_playing,
    refund_interrupted_music_tracks,
    refund_music_track,
    set_music_track_volume,
    upsert_music_session,
)
from utils import database


class MusicRepositoryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "music.db"
        self.path_patch = patch.object(database, "DB_PATH", self.db_path)
        self.path_patch.start()
        await database.init_db()

    async def asyncTearDown(self):
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def _track(
        self,
        interaction_id: int,
        user_id: int,
        price: int = 500,
        volume_percent: int = 50,
    ):
        return await create_paid_music_track(
            interaction_id=interaction_id,
            guild_id=1,
            text_channel_id=10,
            voice_channel_id=20,
            requester_id=user_id,
            query="test",
            webpage_url="https://www.youtube.com/watch?v=test",
            title="Test track",
            duration_seconds=180,
            price=price,
            volume_percent=volume_percent,
        )

    async def test_music_commands_are_limited_to_voice_channel_chats(self):
        cog = MusicCog(SimpleNamespace())
        voice_chat = object.__new__(discord.VoiceChannel)
        allowed = SimpleNamespace(guild=SimpleNamespace(), channel=voice_chat)
        self.assertTrue(await cog.interaction_check(allowed))

        responder = AsyncMock()
        denied = SimpleNamespace(
            guild=SimpleNamespace(),
            channel=SimpleNamespace(),
        )
        with patch.dict(
            MusicCog.interaction_check.__globals__,
            {"respond_v2": responder},
        ):
            self.assertFalse(await cog.interaction_check(denied))
        responder.assert_awaited_once()

    async def test_music_charge_and_refund_are_exactly_once(self):
        await database.adjust_coins(42, 1_000, "grant")
        track = await self._track(100, 42)
        self.assertIsNotNone(track)
        self.assertEqual((await database.get_coin_account(42))[0], 500)

        refunds = await asyncio.gather(
            *(refund_music_track(track.id, "vote_skip") for _ in range(8))
        )
        self.assertEqual(sum(refunds), 1)
        self.assertEqual((await database.get_coin_account(42))[0], 1_000)

    async def test_restart_refunds_only_unfinished_tracks(self):
        for user_id in (51, 52):
            await database.adjust_coins(user_id, 1_000, "grant")
        finished = await self._track(101, 51)
        await self._track(102, 52)  # deliberately left unfinished
        self.assertTrue(await mark_music_track_playing(finished.id))
        self.assertTrue(await complete_music_track(finished.id))

        self.assertEqual(await refund_interrupted_music_tracks(), 1)
        self.assertEqual(await refund_interrupted_music_tracks(), 0)
        self.assertEqual((await database.get_coin_account(51))[0], 500)
        self.assertEqual((await database.get_coin_account(52))[0], 1_000)

        stats = await get_music_statistics(1)
        self.assertEqual(stats["requested"], 2)
        self.assertEqual(stats["completed"], 1)
        self.assertEqual(stats["refunded"], 1)

    def test_direct_music_urls_are_fail_closed(self):
        self.assertTrue(_allowed_url("https://music.youtube.com/watch?v=abc"))
        self.assertFalse(_allowed_url("http://127.0.0.1/internal"))
        self.assertFalse(_allowed_url("file:///etc/passwd"))
        self.assertFalse(_allowed_url("https://youtube.com.example.org/video"))

    async def test_track_volume_is_persisted_and_can_be_adjusted_while_playing(self):
        await database.adjust_coins(60, 1_000, "grant")
        track = await self._track(103, 60, volume_percent=35)
        self.assertEqual(track.volume_percent, 35)
        self.assertTrue(await mark_music_track_playing(track.id))
        self.assertTrue(await set_music_track_volume(track.id, 70))

        refreshed = await self._track(103, 60)
        self.assertEqual(refreshed.volume_percent, 70)

    def test_player_controls_cover_repeat_queue_pause_and_disconnect(self):
        view = MusicControlsView(MusicCog(object()))
        custom_ids = {item.custom_id for item in view.children}

        self.assertIn("music:volume_down", custom_ids)
        self.assertIn("music:volume_up", custom_ids)
        self.assertIn("music:repeat_track", custom_ids)
        self.assertIn("music:loop_queue", custom_ids)
        self.assertIn("music:pause_resume", custom_ids)
        self.assertIn("music:disconnect", custom_ids)
        self.assertEqual(len(view.children), 9)

        active = MusicControlsView(MusicCog(object()), repeat_mode="queue", paused=True)
        self.assertEqual(active.loop_queue.style.name, "success")
        self.assertEqual(active.pause_resume.label, "Продолжить")

    def test_play_has_optional_volume_and_volume_command_is_registered(self):
        play_parameters = {parameter.name: parameter for parameter in MusicCog.play.parameters}

        self.assertIn("громкость", play_parameters)
        self.assertFalse(play_parameters["громкость"].required)
        self.assertEqual(MusicCog.music_volume.name, "volume")
        self.assertEqual(MusicCog.music_mode.name, "mode")
        self.assertEqual(MusicCog.music_disconnect.name, "disconnect")

    def test_ffmpeg_reconnects_network_and_http_stream_failures(self):
        self.assertIn("-reconnect_on_network_error 1", _FFMPEG_BEFORE_OPTIONS)
        self.assertIn("-reconnect_on_http_error 4xx,5xx", _FFMPEG_BEFORE_OPTIONS)
        self.assertIn("-reconnect_delay_max 5", _FFMPEG_BEFORE_OPTIONS)
        self.assertNotIn("reconnect_delay_total_max", _FFMPEG_BEFORE_OPTIONS)
        self.assertIn("-rw_timeout 15000000", _FFMPEG_BEFORE_OPTIONS)

    async def test_volume_change_updates_source_state_repository_and_player(self):
        await database.adjust_coins(70, 1_000, "grant")
        track = await self._track(104, 70, volume_percent=30)

        class Member:
            def __init__(self, user_id: int):
                self.id = user_id

        source = SimpleNamespace(volume=0.3)
        public_message = SimpleNamespace(id=901)
        state = GuildMusicState(
            current=track,
            current_source=source,
            current_volume_percent=30,
            control_message=public_message,
            control_channel_id=10,
            persisted_session=(10, 901, "off"),
        )
        cog = MusicCog(object())
        cog._states[1] = state
        interaction = SimpleNamespace(
            guild=SimpleNamespace(id=1),
            user=Member(70),
            message=public_message,
            guild_id=1,
            channel_id=10,
            response=SimpleNamespace(defer=AsyncMock()),
        )
        respond = AsyncMock()
        callback_globals = MusicCog.handle_volume.__globals__
        refresh = AsyncMock(return_value=public_message)

        with (
            patch.dict(
                callback_globals,
                {
                    "set_music_track_volume": AsyncMock(return_value=True),
                    "followup_v2": respond,
                },
            ),
            patch.object(callback_globals["discord"], "Member", Member),
            patch.object(cog, "_refresh_player", refresh),
        ):
            await cog.handle_volume(interaction, target=65)

        self.assertEqual(source.volume, 0.65)
        self.assertEqual(state.current_volume_percent, 65)
        self.assertEqual(state.track_volumes[track.id], 65)
        refresh.assert_awaited_once()
        respond.assert_awaited_once()

    async def test_music_session_round_trip_persists_panel_and_repeat_mode(self):
        session = await upsert_music_session(
            guild_id=1,
            text_channel_id=10,
            message_id=777,
            repeat_mode="track",
        )
        self.assertEqual(session.message_id, 777)
        self.assertEqual(session.repeat_mode, "track")

        updated = await upsert_music_session(
            guild_id=1,
            text_channel_id=11,
            message_id=778,
            repeat_mode="queue",
        )
        self.assertEqual(updated.text_channel_id, 11)
        self.assertEqual((await get_music_session(1)).repeat_mode, "queue")
        self.assertTrue(await delete_music_session(1))
        self.assertIsNone(await get_music_session(1))

    async def test_database_reset_removes_durable_music_session(self):
        await upsert_music_session(
            guild_id=1,
            text_channel_id=10,
            message_id=779,
            repeat_mode="queue",
        )

        await database.reset_database_data()

        self.assertIsNone(await get_music_session(1))

    async def test_player_refresh_edits_existing_message_instead_of_sending(self):
        await database.adjust_coins(80, 1_000, "grant")
        track = await self._track(105, 80)
        message = SimpleNamespace(id=9001)
        state = GuildMusicState(
            current=track,
            control_message=message,
            control_channel_id=track.text_channel_id,
        )
        cog = MusicCog(object())
        edit = AsyncMock(return_value=message)
        send = AsyncMock()
        persist = AsyncMock()
        guild = SimpleNamespace(id=track.guild_id, voice_client=None)
        callback_globals = MusicCog._refresh_player.__globals__

        with patch.dict(
            callback_globals,
            {
                "edit_message_v2": edit,
                "send_v2": send,
                "upsert_music_session": persist,
            },
        ):
            refreshed = await cog._refresh_player(guild, state)
            refreshed_again = await cog._refresh_player(guild, state)

        self.assertIs(refreshed, message)
        self.assertIs(refreshed_again, message)
        self.assertEqual(edit.await_count, 2)
        send.assert_not_awaited()
        persist.assert_awaited_once()
        embed = edit.await_args.kwargs["embed"]
        self.assertIn(track.title, embed.summary or "")
        self.assertEqual(embed.title, "Музыкальный плеер")

    async def test_disconnect_cleanup_deletes_player_and_durable_session(self):
        await upsert_music_session(
            guild_id=1,
            text_channel_id=10,
            message_id=9002,
            repeat_mode="queue",
        )
        message = SimpleNamespace(id=9002, delete=AsyncMock())
        state = GuildMusicState(
            control_message=message,
            control_channel_id=10,
            repeat_mode="queue",
        )
        cog = MusicCog(object())
        guild = SimpleNamespace(id=1)

        await cog._delete_player(guild, state)

        message.delete.assert_awaited_once()
        self.assertIsNone(await get_music_session(1))
        self.assertIsNone(state.control_message)
        self.assertIsNone(state.control_channel_id)

    async def test_failed_panel_delete_keeps_canonical_session_for_retry(self):
        await upsert_music_session(
            guild_id=1,
            text_channel_id=10,
            message_id=9003,
            repeat_mode="off",
        )
        message = SimpleNamespace(id=9003)
        state = GuildMusicState(
            control_message=message,
            control_channel_id=10,
            persisted_session=(10, 9003, "off"),
        )
        cog = MusicCog(object())
        guild = SimpleNamespace(id=1)

        with patch.object(
            cog,
            "_delete_discord_message",
            AsyncMock(return_value=False),
        ):
            deleted = await cog._delete_player(guild, state)

        self.assertFalse(deleted)
        self.assertIsNotNone(await get_music_session(1))
        self.assertIs(state.control_message, message)

    async def test_shutdown_guild_refunds_orphaned_queue_without_player_task(self):
        await database.adjust_coins(95, 1_000, "grant")
        track = await self._track(114, 95)
        state = GuildMusicState()
        state.queue.append(track)
        voice = SimpleNamespace(is_connected=lambda: False)
        guild = SimpleNamespace(id=1, voice_client=voice)
        cog = MusicCog(object())
        cog._states[1] = state

        with patch.object(cog, "_delete_player", AsyncMock(return_value=True)):
            count, panel_deleted = await cog.shutdown_guild(
                guild,
                reason="test_reset",
            )

        self.assertEqual(count, 1)
        self.assertTrue(panel_deleted)
        self.assertEqual((await database.get_coin_account(95))[0], 1_000)
        self.assertEqual(list(state.queue), [])

    async def test_stale_component_cannot_replace_canonical_player(self):
        canonical = SimpleNamespace(id=9004)
        stale = SimpleNamespace(id=9005, delete=AsyncMock())
        state = GuildMusicState(
            control_message=canonical,
            control_channel_id=10,
            persisted_session=(10, 9004, "off"),
        )
        interaction = SimpleNamespace(
            message=stale,
            guild_id=1,
            channel_id=10,
        )
        cog = MusicCog(object())
        respond = AsyncMock()

        with patch.dict(
            MusicCog._bind_canonical_control.__globals__,
            {"respond_v2": respond},
        ):
            accepted = await cog._bind_canonical_control(interaction, state)

        self.assertFalse(accepted)
        self.assertIs(state.control_message, canonical)
        stale.delete.assert_awaited_once()
        respond.assert_awaited_once()

    async def test_repeat_modes_requeue_without_duplicate_charge(self):
        await database.adjust_coins(90, 1_000, "grant")
        first = await self._track(106, 90)
        second = await self._track(107, 90)

        track_state = GuildMusicState(repeat_mode="track")
        track_state.queue.append(second)
        _schedule_repeat(track_state, first)
        self.assertEqual([item.id for item in track_state.queue], [first.id, second.id])

        queue_state = GuildMusicState(repeat_mode="queue")
        queue_state.queue.append(second)
        _schedule_repeat(queue_state, first)
        self.assertEqual([item.id for item in queue_state.queue], [second.id, first.id])
        self.assertEqual((await database.get_coin_account(90))[0], 0)

        off_state = GuildMusicState(repeat_mode="off")
        _schedule_repeat(off_state, first)
        self.assertEqual(off_state.played_track_ids, set())
        self.assertEqual(list(off_state.queue), [])

    async def test_leaving_queue_loop_drops_only_scheduled_replays(self):
        await database.adjust_coins(92, 1_500, "grant")
        played = await self._track(109, 92)
        unplayed = await self._track(110, 92)
        another_unplayed = await self._track(111, 92)
        state = GuildMusicState(repeat_mode="queue")
        state.played_track_ids.add(played.id)
        state.queue.extend((unplayed, played, another_unplayed))

        _discard_scheduled_replays(state)

        self.assertEqual(
            [item.id for item in state.queue],
            [unplayed.id, another_unplayed.id],
        )

    async def test_idle_timeout_recheck_detects_late_queue_work(self):
        await database.adjust_coins(93, 1_000, "grant")
        track = await self._track(112, 93)
        state = GuildMusicState()
        self.assertFalse(_has_pending_music_work(state))

        state.queue.append(track)
        self.assertTrue(_has_pending_music_work(state))
        state.queue.clear()
        state.wake.set()
        self.assertTrue(_has_pending_music_work(state))

    async def test_idle_timeout_keeps_player_task_and_consumes_late_track(self):
        await database.adjust_coins(94, 1_000, "grant")
        track = await self._track(113, 94)

        class Bot:
            closed = False

            def is_closed(self):
                return self.closed

        class Voice:
            def __init__(self):
                self.connected = True
                self.channel = SimpleNamespace(id=20)
                self.disconnect = AsyncMock(side_effect=self._disconnect)

            async def _disconnect(self, *, force):
                self.connected = False

            def is_connected(self):
                return self.connected

        bot = Bot()
        voice = Voice()
        guild = SimpleNamespace(id=1, voice_client=voice)
        state = GuildMusicState(control_channel_id=10)
        cog = MusicCog(bot)
        refresh = AsyncMock()
        delete = AsyncMock()

        async def play_late_track(*args, **kwargs):
            bot.closed = True
            return False

        next_track = AsyncMock(side_effect=play_late_track)
        wait_calls = 0

        # ASYNC109 asks for asyncio.timeout; this stands in for asyncio.wait_for
        # and has to keep its signature.
        async def timeout_then_wake(awaitable, *, timeout):  # noqa: ASYNC109
            nonlocal wait_calls
            wait_calls += 1
            awaitable.close()
            if wait_calls == 1:
                raise TimeoutError
            state.queue.append(track)
            state.wake.set()
            return True

        with (
            patch.object(cog, "_refresh_player", refresh),
            patch.object(cog, "_next_track", next_track),
            patch.object(cog, "_delete_player", delete),
            patch.object(asyncio, "wait_for", new=timeout_then_wake),
        ):
            await cog._player_loop(guild, state)

        voice.disconnect.assert_awaited_once_with(force=True)
        next_track.assert_awaited_once()
        self.assertEqual(next_track.await_args.args[2].id, track.id)
        self.assertEqual(refresh.await_count, 2)

    def test_elapsed_time_does_not_advance_while_paused(self):
        state = GuildMusicState(
            started_monotonic=100.0,
            paused=True,
            paused_since=145.0,
        )
        self.assertEqual(MusicCog(object())._elapsed(state), 45.0)

    async def test_player_embed_is_informative(self):
        await database.adjust_coins(91, 1_000, "grant")
        track = await self._track(108, 91)
        embed = _player_embed(
            track=track,
            queued=[track],
            elapsed_seconds=30,
            volume_percent=65,
            repeat_mode="queue",
            paused=False,
            voice_channel_id=20,
        )
        fields = dict(embed.details)
        self.assertIn("Прогресс", fields)
        self.assertIn("Сессия", fields)
        self.assertIn("Цикл всей очереди", fields["Сессия"])
        self.assertIn("65%", fields["Сессия"])

        idle = _player_embed(
            track=None,
            queued=[],
            elapsed_seconds=0,
            volume_percent=50,
            repeat_mode="off",
            paused=False,
            voice_channel_id=None,
            idle_disconnected=True,
        )
        self.assertIn("отключился от голосового", idle.summary or "")
        self.assertIn("/music disconnect", idle.next_step or "")


if __name__ == "__main__":
    unittest.main()
