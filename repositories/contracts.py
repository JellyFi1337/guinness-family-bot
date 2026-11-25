"""Family contract catalog, runs and participant shares."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from infrastructure.database import connection as _connect, utc_now
from infrastructure.transactions import begin_write

CATALOG_STATUSES = frozenset({"draft", "available", "archived"})
RUN_STATUSES = frozenset(
    {
        "draft",
        "available",
        "in_work",
        "report_submitted",
        "accepted",
        "rework",
        "rejected",
        "closed",
        "cancelled",
        "partial",
    }
)
JOINABLE_RUN_STATUSES = frozenset({"available", "in_work"})
OPEN_RUN_STATUSES = frozenset(
    {
        "draft",
        "available",
        "in_work",
        "report_submitted",
        "accepted",
        "rework",
        "rejected",
        "partial",
    }
)
DIRECTIONS = frozenset({"legal", "criminal"})
SHARE_TOTAL_BPS = 10_000

_CATALOG_COLUMNS = (
    "id, guild_id, family_id, direction, title, required_upgrades, "
    "required_resources, team_min, team_max, instruction_url, verified_at, "
    "reward_note, status, created_at, updated_at, created_by, profit_fields"
)
_RUN_COLUMNS = (
    "id, guild_id, family_id, catalog_id, snapshot_json, "
    "responsible_discord_id, status, issued_resources, started_at, "
    "finished_at, version, created_at, updated_at"
)


@dataclass(frozen=True, slots=True)
class CatalogItem:
    id: int
    guild_id: int
    family_id: int
    direction: str
    title: str
    required_upgrades: tuple[str, ...]
    required_resources: tuple[str, ...]
    team_min: int
    team_max: int
    instruction_url: str | None
    verified_at: str | None
    reward_note: str | None
    status: str
    created_at: str
    updated_at: str
    created_by: int
    profit_fields: str


@dataclass(frozen=True, slots=True)
class RunParticipant:
    run_id: int
    guild_id: int
    family_id: int
    discord_id: int
    share_bps: int
    joined_at: str


@dataclass(frozen=True, slots=True)
class ContractRun:
    id: int
    guild_id: int
    family_id: int
    catalog_id: int
    snapshot: dict[str, Any]
    responsible_discord_id: int
    status: str
    issued_resources: tuple[str, ...]
    started_at: str | None
    finished_at: str | None
    version: int
    created_at: str
    updated_at: str
    participants: tuple[RunParticipant, ...]


def _inserted_id(cursor: Any) -> int:
    row_id = int(cursor.lastrowid or 0)
    if row_id <= 0:
        raise RuntimeError("insert did not return an id")
    return row_id


def _parse_str_list(raw: object) -> tuple[str, ...]:
    if not raw:
        return ()
    if isinstance(raw, (list, tuple)):
        return tuple(str(item) for item in raw if str(item).strip())
    text = str(raw).strip()
    if not text:
        return ()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return tuple(part.strip() for part in text.split(",") if part.strip())
    if isinstance(parsed, list):
        return tuple(str(item) for item in parsed if str(item).strip())
    return ()


def _dump_list(values: tuple[str, ...] | list[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)


def _parse_snapshot(raw: object) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    text = str(raw or "").strip() or "{}"
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("contract snapshot must be an object")
    return parsed


def _to_catalog(row: Any) -> CatalogItem:
    return CatalogItem(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        direction=str(row[3]),
        title=str(row[4]),
        required_upgrades=_parse_str_list(row[5]),
        required_resources=_parse_str_list(row[6]),
        team_min=int(row[7]),
        team_max=int(row[8]),
        instruction_url=str(row[9]) if row[9] else None,
        verified_at=str(row[10]) if row[10] else None,
        reward_note=str(row[11]) if row[11] else None,
        status=str(row[12]),
        created_at=str(row[13]),
        updated_at=str(row[14]),
        created_by=int(row[15]),
        profit_fields=str(row[16] or ""),
    )


def _to_participant(row: Any) -> RunParticipant:
    return RunParticipant(
        run_id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        discord_id=int(row[3]),
        share_bps=int(row[4]),
        joined_at=str(row[5]),
    )


def _to_run(row: Any, participants: tuple[RunParticipant, ...]) -> ContractRun:
    return ContractRun(
        id=int(row[0]),
        guild_id=int(row[1]),
        family_id=int(row[2]),
        catalog_id=int(row[3]),
        snapshot=_parse_snapshot(row[4]),
        responsible_discord_id=int(row[5]),
        status=str(row[6]),
        issued_resources=_parse_str_list(row[7]),
        started_at=str(row[8]) if row[8] else None,
        finished_at=str(row[9]) if row[9] else None,
        version=int(row[10]),
        created_at=str(row[11]),
        updated_at=str(row[12]),
        participants=participants,
    )


def snapshot_from_catalog(item: CatalogItem) -> dict[str, Any]:
    return {
        "catalog_id": item.id,
        "direction": item.direction,
        "instruction_url": item.instruction_url,
        "required_resources": list(item.required_resources),
        "required_upgrades": list(item.required_upgrades),
        "reward_note": item.reward_note,
        "team_max": item.team_max,
        "team_min": item.team_min,
        "title": item.title,
        "verified_at": item.verified_at,
        "profit_fields": item.profit_fields,
    }


async def insert_catalog_item(
    *,
    guild_id: int,
    family_id: int,
    direction: str,
    title: str,
    required_upgrades: tuple[str, ...],
    required_resources: tuple[str, ...],
    team_min: int,
    team_max: int,
    instruction_url: str | None,
    verified_at: str | None,
    reward_note: str | None,
    created_by: int,
    status: str = "draft",
    profit_fields: str = "",
) -> CatalogItem:
    if direction not in DIRECTIONS:
        raise ValueError("unknown contract direction")
    if status not in CATALOG_STATUSES:
        raise ValueError("unknown catalog status")
    stamp = utc_now()
    async with _connect() as db:
        cursor = await db.execute(
            """INSERT INTO contract_catalog (
                    guild_id, family_id, direction, title, required_upgrades,
                    required_resources, team_min, team_max, instruction_url,
                    verified_at, reward_note, status, created_at, updated_at,
                    created_by, profit_fields
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                guild_id,
                family_id,
                direction,
                title,
                _dump_list(required_upgrades),
                _dump_list(required_resources),
                team_min,
                team_max,
                instruction_url,
                verified_at,
                reward_note,
                status,
                stamp,
                stamp,
                created_by,
                profit_fields,
            ),
        )
        item_id = _inserted_id(cursor)
        await db.commit()
    item = await get_catalog_item(item_id)
    if item is None:
        raise RuntimeError("catalog insert did not persist")
    return item


async def get_catalog_item(item_id: int) -> CatalogItem | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_CATALOG_COLUMNS} FROM contract_catalog WHERE id = ?",
            (item_id,),
        )
    return _to_catalog(rows[0]) if rows else None


async def list_catalog(
    family_id: int, *, include_archived: bool = False
) -> list[CatalogItem]:
    async with _connect() as db:
        if include_archived:
            rows = await db.execute_fetchall(
                f"""SELECT {_CATALOG_COLUMNS} FROM contract_catalog
                    WHERE family_id = ?
                    ORDER BY status, title COLLATE NOCASE, id""",
                (family_id,),
            )
        else:
            rows = await db.execute_fetchall(
                f"""SELECT {_CATALOG_COLUMNS} FROM contract_catalog
                    WHERE family_id = ? AND status != 'archived'
                    ORDER BY status, title COLLATE NOCASE, id""",
                (family_id,),
            )
    return [_to_catalog(row) for row in rows]


async def set_catalog_status(item_id: int, status: str) -> CatalogItem | None:
    if status not in CATALOG_STATUSES:
        raise ValueError("unknown catalog status")
    stamp = utc_now()
    async with _connect() as db:
        await db.execute(
            """UPDATE contract_catalog
               SET status = ?, updated_at = ?
               WHERE id = ?""",
            (status, stamp, item_id),
        )
        await db.commit()
    return await get_catalog_item(item_id)


async def update_catalog_item(
    item_id: int,
    *,
    title: str | None = None,
    required_upgrades: tuple[str, ...] | None = None,
    required_resources: tuple[str, ...] | None = None,
    team_min: int | None = None,
    team_max: int | None = None,
    instruction_url: str | None = None,
    verified_at: str | None = None,
    reward_note: str | None = None,
    profit_fields: str | None = None,
) -> CatalogItem | None:
    current = await get_catalog_item(item_id)
    if current is None:
        return None
    stamp = utc_now()
    async with _connect() as db:
        await db.execute(
            """UPDATE contract_catalog
               SET title = ?, required_upgrades = ?, required_resources = ?,
                   team_min = ?, team_max = ?, instruction_url = ?,
                   verified_at = ?, reward_note = ?, profit_fields = ?,
                   updated_at = ?
               WHERE id = ?""",
            (
                title if title is not None else current.title,
                _dump_list(
                    required_upgrades
                    if required_upgrades is not None
                    else current.required_upgrades
                ),
                _dump_list(
                    required_resources
                    if required_resources is not None
                    else current.required_resources
                ),
                team_min if team_min is not None else current.team_min,
                team_max if team_max is not None else current.team_max,
                (
                    instruction_url
                    if instruction_url is not None
                    else current.instruction_url
                ),
                verified_at if verified_at is not None else current.verified_at,
                reward_note if reward_note is not None else current.reward_note,
                profit_fields if profit_fields is not None else current.profit_fields,
                stamp,
                item_id,
            ),
        )
        await db.commit()
    return await get_catalog_item(item_id)


async def _participants_for(db: Any, run_id: int) -> tuple[RunParticipant, ...]:
    rows = await db.execute_fetchall(
        """SELECT run_id, guild_id, family_id, discord_id, share_bps, joined_at
           FROM contract_run_participants
           WHERE run_id = ?
           ORDER BY joined_at, discord_id""",
        (run_id,),
    )
    return tuple(_to_participant(row) for row in rows)


async def get_run(run_id: int) -> ContractRun | None:
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"SELECT {_RUN_COLUMNS} FROM contract_runs WHERE id = ?",
            (run_id,),
        )
        if not rows:
            return None
        participants = await _participants_for(db, run_id)
    return _to_run(rows[0], participants)


async def list_open_runs(family_id: int) -> list[ContractRun]:
    placeholders = ", ".join("?" for _ in OPEN_RUN_STATUSES)
    async with _connect() as db:
        rows = await db.execute_fetchall(
            f"""SELECT {_RUN_COLUMNS} FROM contract_runs
                WHERE family_id = ? AND status IN ({placeholders})
                ORDER BY created_at, id""",
            (family_id, *sorted(OPEN_RUN_STATUSES)),
        )
        runs: list[ContractRun] = []
        for row in rows:
            participants = await _participants_for(db, int(row[0]))
            runs.append(_to_run(row, participants))
    return runs


async def list_runs_for_member(
    family_id: int, discord_id: int, *, open_only: bool = True
) -> list[ContractRun]:
    status_filter = ""
    params: list[object] = [family_id, discord_id]
    if open_only:
        placeholders = ", ".join("?" for _ in OPEN_RUN_STATUSES)
        status_filter = f"AND r.status IN ({placeholders})"
        params.extend(sorted(OPEN_RUN_STATUSES))
    async with _connect() as db:
        prefixed = ", ".join(f"r.{name.strip()}" for name in _RUN_COLUMNS.split(","))
        rows = await db.execute_fetchall(
            f"""SELECT {prefixed}
                FROM contract_runs r
                JOIN contract_run_participants p ON p.run_id = r.id
                WHERE r.family_id = ? AND p.discord_id = ? {status_filter}
                ORDER BY r.created_at, r.id""",
            tuple(params),
        )
        runs: list[ContractRun] = []
        seen: set[int] = set()
        for row in rows:
            run_id = int(row[0])
            if run_id in seen:
                continue
            seen.add(run_id)
            participants = await _participants_for(db, run_id)
            runs.append(_to_run(row, participants))
    return runs


async def insert_run(
    *,
    guild_id: int,
    family_id: int,
    catalog_id: int,
    snapshot: dict[str, Any],
    responsible_discord_id: int,
    share_bps: int,
    issued_resources: tuple[str, ...] = (),
    status: str = "in_work",
) -> ContractRun:
    if status not in RUN_STATUSES:
        raise ValueError("unknown run status")
    stamp = utc_now()
    started = stamp if status in {"in_work", "available"} else None
    async with _connect() as db:
        await begin_write(db, "contract-run-create", f"{family_id}:{catalog_id}")
        cursor = await db.execute(
            """INSERT INTO contract_runs (
                    guild_id, family_id, catalog_id, snapshot_json,
                    responsible_discord_id, status, issued_resources,
                    started_at, finished_at, version, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, 1, ?, ?)""",
            (
                guild_id,
                family_id,
                catalog_id,
                json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                responsible_discord_id,
                status,
                _dump_list(issued_resources),
                started,
                stamp,
                stamp,
            ),
        )
        run_id = _inserted_id(cursor)
        await db.execute(
            """INSERT INTO contract_run_participants (
                   run_id, guild_id, family_id, discord_id, share_bps, joined_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (
                run_id,
                guild_id,
                family_id,
                responsible_discord_id,
                share_bps,
                stamp,
            ),
        )
        await db.commit()
    run = await get_run(run_id)
    if run is None:
        raise RuntimeError("run insert did not persist")
    return run


async def join_run(
    *,
    run_id: int,
    guild_id: int,
    family_id: int,
    discord_id: int,
    share_bps: int,
    team_max: int,
) -> str:
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "contract-run", run_id)
        rows = await db.execute_fetchall(
            "SELECT status, version FROM contract_runs WHERE id = ?",
            (run_id,),
        )
        if not rows:
            return "missing"
        status = str(rows[0][0])
        version = int(rows[0][1])
        if status not in JOINABLE_RUN_STATUSES:
            return "closed"
        parts = await db.execute_fetchall(
            """SELECT discord_id, share_bps
               FROM contract_run_participants WHERE run_id = ?""",
            (run_id,),
        )
        if any(int(row[0]) == discord_id for row in parts):
            return "duplicate"
        if len(parts) >= team_max:
            return "full"
        taken = sum(int(row[1]) for row in parts)
        if taken + share_bps > SHARE_TOTAL_BPS:
            return "share"
        await db.execute(
            """INSERT INTO contract_run_participants (
                   run_id, guild_id, family_id, discord_id, share_bps, joined_at
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (run_id, guild_id, family_id, discord_id, share_bps, stamp),
        )
        cursor = await db.execute(
            """UPDATE contract_runs
               SET version = version + 1, updated_at = ?
               WHERE id = ? AND version = ?""",
            (stamp, run_id, version),
        )
        if int(cursor.rowcount or 0) != 1:
            return "conflict"
        await db.commit()
    return "ok"


async def set_run_status(
    run_id: int,
    status: str,
    *,
    expected_version: int | None = None,
    finished: bool = False,
) -> ContractRun | None:
    if status not in RUN_STATUSES:
        raise ValueError("unknown run status")
    stamp = utc_now()
    async with _connect() as db:
        await begin_write(db, "contract-run", run_id)
        if expected_version is None:
            await db.execute(
                """UPDATE contract_runs
                   SET status = ?, finished_at = ?, version = version + 1,
                       updated_at = ?
                   WHERE id = ?""",
                (status, stamp if finished else None, stamp, run_id),
            )
        else:
            cursor = await db.execute(
                """UPDATE contract_runs
                   SET status = ?, finished_at = ?, version = version + 1,
                       updated_at = ?
                   WHERE id = ? AND version = ?""",
                (
                    status,
                    stamp if finished else None,
                    stamp,
                    run_id,
                    expected_version,
                ),
            )
            if int(cursor.rowcount or 0) != 1:
                await db.rollback()
                return None
        await db.commit()
    return await get_run(run_id)
