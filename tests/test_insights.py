from __future__ import annotations

import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.permissions import Permission, grant_role_permission
from core.settings import get_registry
from repositories.game_online import upsert_external_snapshot
from repositories.members import get_current_member, grant_clearance
from repositories.static_messages import set_guild_setting
from services.bonus import default_ranks
from services.event_planning import create_event, ensure_event_integrations, join_event
from services.external.base import (
    SOURCE_MAJESTIC_API,
    dump_payload,
    snapshot_from_row,
    unavailable_snapshot,
)
from services.insights import (
    collect_new_battle_cards,
    extract_org_rating,
    format_influence_change,
    influence_until_next_rank,
    lookup_market_price,
    render_battles_spec,
    render_market_spec,
    render_rating_spec,
    roster_gap_report,
    unavailable_panel_spec,
    unmatched_event_text,
)
from services.membership import (
    register_member,
    reset_obligation_sources,
    verify_in_game,
)
from services.panels import (
    SOURCE_UNAVAILABLE,
    STATE_UNAVAILABLE,
    PanelError,
    deploy_panel,
    format_panel_lines,
    list_panels,
    unavailable_reason,
)
from ui.message_specs import format_source_attribution
from utils import database

GUILD_ID = 9_200
FAMILY_ROLE_ID = 201
PERM_ROLE_ID = 202
STAFF_ID = 3_010
USER_A = 3_011
FAKE_KEY = "unit-test-insights-key"


def _snap(
    resource: str,
    payload: object,
    *,
    stale: bool = False,
    fetched_at: str = "2026-09-09T08:15:00+00:00",
):
    return snapshot_from_row(
        resource=resource,
        source_key=SOURCE_MAJESTIC_API,
        source_label="Majestic API",
        payload_text=dump_payload(payload),
        fetched_at=fetched_at,
        attribution="Источник: Majestic RP",
        stale=stale,
    )


def _role(role_id: int, name: str) -> SimpleNamespace:
    return SimpleNamespace(id=role_id, name=name)


def _guild(roles: list[SimpleNamespace]) -> SimpleNamespace:
    by_id = {role.id: role for role in roles}

    def get_role(role_id: int):
        return by_id.get(int(role_id))

    return SimpleNamespace(id=GUILD_ID, roles=roles, get_role=get_role, members=[])


def _actor(
    *,
    user_id: int,
    roles: list[SimpleNamespace],
    guild: SimpleNamespace,
    display_name: str = "officer",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=user_id,
        name=display_name,
        display_name=display_name,
        roles=roles,
        guild=guild,
        guild_id=guild.id,
    )


class FakeClient:
    def __init__(self, captures: object, wars: object | None = None) -> None:
        self._captures = captures
        self._wars = wars if wars is not None else []

    async def fetch_captures(self, guild_id: int):
        del guild_id
        return _snap("captures", self._captures)

    async def fetch_family_wars(self, guild_id: int):
        del guild_id
        return _snap("family-wars", self._wars)


class InsightPanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "insights.db",
        )
        self.path_patch.start()
        await database.init_db()
        get_registry().clear_cache()
        self._saved_key = os.environ.get("MAJESTIC_API_KEY")
        os.environ.pop("MAJESTIC_API_KEY", None)

    async def asyncTearDown(self) -> None:
        get_registry().clear_cache()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()
        if self._saved_key is None:
            os.environ.pop("MAJESTIC_API_KEY", None)
        else:
            os.environ["MAJESTIC_API_KEY"] = self._saved_key

    async def _fill_ready(self) -> None:
        registry = get_registry()
        await registry.set(
            GUILD_ID, "EXTERNAL_API_BASE_URL", "https://api.example.test", updated_by="t"
        )
        await registry.set(GUILD_ID, "EXTERNAL_API_VERSION", "v1", updated_by="t")
        await registry.set(GUILD_ID, "EXTERNAL_API_SERVER_ID", "RU19", updated_by="t")
        await registry.set(GUILD_ID, "EXTERNAL_API_RATE_LIMIT", 10, updated_by="t")
        await registry.set(
            GUILD_ID, "EXTERNAL_API_RATE_WINDOW_SECONDS", 60, updated_by="t"
        )
        await registry.set(
            GUILD_ID,
            "EXTERNAL_API_ATTRIBUTION",
            "Источник: Majestic RP",
            updated_by="t",
        )
        os.environ["MAJESTIC_API_KEY"] = FAKE_KEY

    def test_next_rank_gap_uses_bonus_scale(self) -> None:
        ranks = default_ranks()
        self.assertEqual(influence_until_next_rank(500, ranks), "до «Местные»: 800")
        self.assertEqual(influence_until_next_rank(499, ranks), "до «Чужаки»: 1")
        self.assertIn("Орден", influence_until_next_rank(11500, ranks))
        self.assertEqual(influence_until_next_rank(None, ranks), "нет данных об влиянии")
        self.assertEqual(format_influence_change(500, None), "изменение пока неизвестно")
        self.assertEqual(format_influence_change(500, 500), "0")
        self.assertNotEqual(format_influence_change(None, None), "0")

    def test_unavailable_source_render_has_no_zeros(self) -> None:
        spec = unavailable_panel_spec(code="INSIGHT.RATING", title="Рейтинг Memphis")
        blob = " ".join(
            [spec.summary or "", spec.next_step or ""]
            + [f"{title} {text}" for title, text in spec.details]
        )
        self.assertNotRegex(blob, r"(?<!\d)0(?!\d)")
        self.assertIn("не подставляет", spec.summary or "")
        empty = unavailable_snapshot(
            resource="rating-organizations/week",
            source_key=SOURCE_MAJESTIC_API,
            source_label="Majestic API",
            reason="таймаут",
            attribution="Источник: Majestic RP",
        )
        spec = render_rating_spec(
            family_name="Guinness",
            ranks=default_ranks(),
            periods={"week": empty, "period": empty, "season": empty},
            previous={},
        )
        text = " ".join(text for _title, text in spec.details)
        self.assertIn("нет в ответе", text)
        self.assertNotIn("место 0", text)
        self.assertNotIn("влияние 0", text)

    def test_source_attribution_and_timestamp(self) -> None:
        snapshot = _snap(
            "rating-organizations/week",
            {"name": "Guinness", "place": 4, "influence": 500},
            stale=True,
        )
        spec = render_rating_spec(
            family_name="Guinness",
            ranks=default_ranks(),
            periods={"week": snapshot, "period": snapshot, "season": snapshot},
            previous={"week": 400},
        )
        self.assertEqual(spec.source_attribution, "Источник: Majestic RP")
        self.assertEqual(spec.source_fetched_at, "2026-09-09T08:15:00+00:00")
        self.assertTrue(spec.source_stale)
        footnote = format_source_attribution(
            attribution=spec.source_attribution,
            source_label=spec.source_label,
            fetched_at=spec.source_fetched_at,
            stale=spec.source_stale,
        )
        self.assertIsNotNone(footnote)
        assert footnote is not None
        self.assertIn("11:15", footnote)
        self.assertIn("Обновление не удалось, показаны данные от 11:15", spec.summary or "")
        week = next(text for title, text in spec.details if title == "Неделя")
        self.assertIn("место 4", week)
        self.assertIn("влияние 500", week)
        self.assertIn("+100", week)
        self.assertIn("до «Местные»: 800", week)

    def test_foreign_families_are_not_listed(self) -> None:
        payload = [
            {"name": "OtherFam", "place": 1, "influence": 9000, "nick": "EnemyNick"},
            {"name": "Second", "place": 2, "influence": 8000},
        ]
        view = extract_org_rating(payload, "Guinness", "")
        self.assertFalse(view.found)
        spec = render_rating_spec(
            family_name="Guinness",
            ranks=default_ranks(),
            periods={
                "week": _snap("rating-organizations/week", payload),
                "period": _snap("rating-organizations/period", payload),
                "season": _snap("rating-organizations/season", payload),
            },
            previous={},
        )
        blob = " ".join(text for _title, text in spec.details)
        self.assertIn("не найдена", blob)
        self.assertNotIn("OtherFam", blob)
        self.assertNotIn("EnemyNick", blob)
        self.assertNotIn("9000", blob)

    async def test_panels_hidden_when_integration_off(self) -> None:
        reason = await unavailable_reason(GUILD_ID, "insights-rating")
        self.assertEqual(reason, SOURCE_UNAVAILABLE)
        items = await list_panels(GUILD_ID)
        keys = {item.key for item in items if item.key.startswith("insights-")}
        self.assertEqual(
            keys,
            {
                "insights-rating",
                "insights-battles",
                "insights-market",
                "insights-property",
            },
        )
        for item in items:
            if item.key.startswith("insights-"):
                self.assertEqual(item.state, STATE_UNAVAILABLE)
                self.assertEqual(item.reason, SOURCE_UNAVAILABLE)
        line = format_panel_lines(
            tuple(item for item in items if item.key == "insights-rating")
        )[0]
        self.assertIn("недоступна: источник не настроен", line)
        with self.assertRaises(PanelError) as ctx:
            await deploy_panel(SimpleNamespace(id=GUILD_ID), "insights-rating")
        self.assertEqual(ctx.exception.code, "PANELS.SOURCE_REQUIRED")

    async def test_market_hint_from_snapshot_is_estimate_with_date(self) -> None:
        hint = await lookup_market_price(GUILD_ID, "соль")
        self.assertIsNone(hint.amount)
        self.assertEqual(hint.source, "none")
        await self._fill_ready()
        await upsert_external_snapshot(
            GUILD_ID,
            SOURCE_MAJESTIC_API,
            "marketplace/items",
            dump_payload([{"name": "соль", "price": 42}]),
            "2026-09-09T08:15:00+00:00",
            "Источник: Majestic RP",
        )
        found = await lookup_market_price(GUILD_ID, "соль")
        self.assertEqual(found.amount, 42)
        self.assertIn("оценка", found.note.casefold())
        self.assertIn("11:15", found.note)
        missing = await lookup_market_price(GUILD_ID, "золото")
        self.assertIsNone(missing.amount)

    async def test_first_battle_fetch_does_not_spam(self) -> None:
        payload = [{"id": "c1", "title": "Квадрат", "result": "победа"}]
        client = FakeClient(payload)
        first = await collect_new_battle_cards(GUILD_ID, client=client)
        self.assertEqual(first, [])
        second = await collect_new_battle_cards(GUILD_ID, client=client)
        self.assertEqual(second, [])
        client = FakeClient(
            payload + [{"id": "c2", "title": "Бизнес", "result": "поражение"}]
        )
        third = await collect_new_battle_cards(GUILD_ID, client=client)
        self.assertEqual(len(third), 1)
        self.assertIn("Бизнес", third[0].title)
        self.assertIn("допишите вывод", third[0].next_step or "")
        self.assertNotIn("nick", third[0].summary or "")

    async def test_battles_panel_prefers_roster_report(self) -> None:
        captures = _snap("captures", [{"id": "c1", "title": "Квадрат"}])
        wars = unavailable_snapshot(
            resource="family-wars",
            source_key=SOURCE_MAJESTIC_API,
            source_label="Majestic API",
            reason="нет данных",
            attribution="Источник: Majestic RP",
        )
        spec = await render_battles_spec(captures=captures, wars=wars, events=())
        roster = next(text for title, text in spec.details if title == "Состав и допуски")
        self.assertEqual(roster, unmatched_event_text())
        self.assertIn("не сверял", roster)

    def test_market_panel_omits_missing_prices(self) -> None:
        spec = render_market_spec(
            (
                _snap(
                    "marketplace/items",
                    [{"name": "соль", "price": 10}, {"name": "безцены"}],
                ),
            )
        )
        catalog = " ".join(text for _title, text in spec.details)
        self.assertIn("оценка 10", catalog)
        self.assertIn("цена не указана", catalog)
        self.assertNotIn("оценка 0", catalog)
        self.assertTrue(spec.source_fetched_at)


class InsightRosterTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        await database.close_db()
        self.temp_dir = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(
            database,
            "DB_PATH",
            Path(self.temp_dir.name) / "insights-roster.db",
        )
        self.path_patch.start()
        await database.init_db()
        reset_obligation_sources()
        ensure_event_integrations()
        family = _role(FAMILY_ROLE_ID, "High")
        perms = _role(PERM_ROLE_ID, "Кадровик")
        self.guild = _guild([family, perms])
        self.staff = _actor(user_id=STAFF_ID, roles=[family, perms], guild=self.guild)
        self.alpha = _actor(
            user_id=USER_A, roles=[family], guild=self.guild, display_name="A"
        )
        for permission in (
            Permission.MEMBERS_MANAGE,
            Permission.MEMBERS_VIEW,
            Permission.EVENTS_CREATE,
            Permission.EVENTS_MANAGE_OWN,
            Permission.EVENTS_MANAGE_ALL,
        ):
            await grant_role_permission(GUILD_ID, permission, PERM_ROLE_ID)
        await register_member(
            self.staff, GUILD_ID, discord_id=STAFF_ID, ic_name="Staff"
        )
        await register_member(self.staff, GUILD_ID, discord_id=USER_A, ic_name="Alpha")
        await verify_in_game(self.staff, GUILD_ID, USER_A)

    async def asyncTearDown(self) -> None:
        reset_obligation_sources()
        await database.close_db()
        self.path_patch.stop()
        self.temp_dir.cleanup()

    async def test_roster_report_names_missing_clearance(self) -> None:
        await set_guild_setting(GUILD_ID, "profile.enabled_upgrades", "territory_control")
        start = (datetime.now(UTC) + timedelta(hours=2)).isoformat()
        end = (datetime.now(UTC) + timedelta(hours=4)).isoformat()
        event = await create_event(
            self.staff,
            GUILD_ID,
            "capt_square",
            starts_at=start,
            ends_at=end,
            meetup="офис организации",
            f2_event_ref="f2-square",
        )
        await join_event(self.alpha, GUILD_ID, event.id)
        report = await roster_gap_report(event)
        self.assertIn("в основе 1 из 20", report)
        self.assertIn("без допуска «territory_control»: 1", report)
        member = await get_current_member(event.family_id, USER_A)
        assert member is not None
        await grant_clearance(
            member_id=member.id,
            activity="territory_control",
            granted_by=STAFF_ID,
        )
        filled = await roster_gap_report(event)
        self.assertIn("допуск «territory_control» есть у всех записанных", filled)


if __name__ == "__main__":
    unittest.main()
