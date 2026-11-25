import unittest
import tempfile
import asyncio
from pathlib import Path
from unittest.mock import patch

import discord
from discord.ext import commands

from utils import database


class ExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_extensions_load_and_register_commands(self):
        bot = commands.Bot(command_prefix="!", intents=discord.Intents.default())
        bot._ready = asyncio.Event()
        bot._ready.set()
        extensions = (
            "cogs.interactions",
            "cogs.anonymous_relay",
            "cogs.help",
            "cogs.admin",
            "cogs.setup_wizard",
            "cogs.config_panel",
            "cogs.panels",
            "cogs.audit",
            "cogs.members",
            "cogs.applications",
            "cogs.training",
            "cogs.contracts",
            "cogs.reports",
            "cogs.staff_queue",
            "cogs.staff_panel",
            "cogs.events",
            "cogs.treasury_v2",
            "cogs.bonus",
            "cogs.logistics",
            "cogs.progression",
            "cogs.insights",
            "cogs.member_panel",
            "cogs.roster_watch",
            "cogs.fundraiser",
            "cogs.summits",
            "cogs.voice_rooms",
            "cogs.car_park",
            "cogs.recruiting",
            "cogs.weekly_stats",
            "cogs.shop",
            "cogs.music",
            "cogs.polls",
            "cogs.giveaways",
            "cogs.groups",
            "cogs.showdown",
            "cogs.dev",
            "cogs.technical_status",
        )
        with tempfile.TemporaryDirectory() as temp_dir, patch.object(
            database, "DB_PATH", Path(temp_dir) / "test.db"
        ):
            await database.init_db()
            try:
                for extension in extensions:
                    await bot.load_extension(extension)
                command_names = {command.name for command in bot.tree.get_commands()}
                self.assertEqual(
                    command_names,
                    {
                        "admin",
                        "applications",
                        "audit",
                        "bonus",
                        "car",
                        "coins",
                        "config",
                        "contract",
                        "dev",
                        "dev_reset_server",
                        "event",
                        "fundraiser",
                        "giveaway",
                        "help",
                        "me",
                        "members",
                        "group",
                        "music",
                        "panels",
                        "poll",
                        "progress",
                        "queue",
                        "recruiting",
                        "report",
                        "setup",
                        "showdown",
                        "staff",
                        "stats",
                        "stock",
                        "summit",
                        "training",
                        "treasury",
                        "vabority",
                        "voice",
                    },
                )
                poll = next(
                    command for command in bot.tree.get_commands() if command.name == "poll"
                )
                poll_create = poll.get_command("create")
                self.assertIsNotNone(poll_create)
                self.assertNotIn(
                    "канал",
                    {parameter.name for parameter in poll_create.parameters},
                )
                music = next(
                    command
                    for command in bot.tree.get_commands()
                    if command.name == "music"
                )
                self.assertEqual(
                    {
                        "play",
                        "queue",
                        "now",
                        "volume",
                        "skip",
                        "cancel",
                        "mode",
                        "pause",
                        "resume",
                        "disconnect",
                        "stats",
                    },
                    {command.name for command in music.commands},
                )
                dev = next(
                    command for command in bot.tree.get_commands() if command.name == "dev"
                )
                self.assertEqual(
                    {"health", "status"} - {command.name for command in dev.commands},
                    set(),
                )
                self.assertEqual(dev.default_permissions.value, 0)
                admin = next(
                    command
                    for command in bot.tree.get_commands()
                    if command.name == "admin"
                )
                self.assertIn(
                    "cooldown_access",
                    {command.name for command in admin.commands},
                )
                car = next(
                    command
                    for command in bot.tree.get_commands()
                    if command.name == "car"
                )
                self.assertIn(
                    "booking_cancel",
                    {command.name for command in car.commands},
                )
                dev_reset = next(
                    command
                    for command in bot.tree.get_commands()
                    if command.name == "dev_reset_server"
                )
                self.assertEqual(dev_reset.description, ".")
                self.assertEqual(dev_reset.default_permissions.value, 0)
                vabority = next(
                    command
                    for command in bot.tree.get_commands()
                    if command.name == "vabority"
                )
                self.assertEqual(vabority.description, ".")
                self.assertEqual(vabority.default_permissions.value, 0)
            finally:
                await bot.close()
