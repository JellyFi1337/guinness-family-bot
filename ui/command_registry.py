"""Single source of truth for slash commands referenced in user-facing text.

Every hint the bot writes ("используйте `/coins daily`") used to be a free-form
string. Renaming a command group left those strings behind — `/daily`, `/pay`,
`/role_order` and `/skip` all survived their own move into groups and pointed
users at commands that no longer exist.

Reference commands through :func:`cmd` instead. ``tests/test_command_registry``
walks the live command tree and fails when this registry and the tree disagree,
so a rename breaks CI instead of the user's day.
"""

from __future__ import annotations

from typing import Iterable

# Qualified names exactly as Discord resolves them: "<group> <subcommand>",
# or a bare name for a root-level command. Keep this list sorted by group.
COMMANDS: frozenset[str] = frozenset(
    {
        # /admin
        "admin setup",
        "admin preflight",
        "admin config_ids",
        "admin birthday_add",
        "admin birthday_remove",
        "admin treasury_set",
        "admin transaction_add",
        "admin cooldown_access",
        # /audit
        "audit list",
        "audit show",
        # /car
        "car add",
        "car remove",
        "car edit",
        "car booking_cancel",
        "car show",
        "car assign",
        "car recon",
        "car reserve",
        "car return",
        "car property",
        "car property_mark",
        # /coins
        "coins balance",
        "coins daily",
        "coins pay",
        "coins top",
        "coins grant",
        "coins take",
        "coins reset",
        "coins role_order",
        # /config
        "config show",
        "config set",
        "config reset",
        "config diff",
        "config export",
        # /contract
        "contract catalog",
        "contract start",
        "contract take",
        "contract my",
        # /event
        "event templates",
        "event create",
        "event list",
        "event show",
        "event roster",
        "event join",
        "event leave",
        "event cancel",
        "event reschedule",
        "event commander",
        "event ready",
        "event lock",
        "event attendance",
        "event excuse",
        "event replace",
        "event reserve",
        "event stats",
        "event voice_hint",
        "event calendar",
        "event outcome",
        "event map_add",
        "event map_list",
        "event map_archive",
        # /dev
        "dev health",
        "dev status",
        "dev_reset_server",
        # /fundraiser
        "fundraiser create",
        "fundraiser contribution_remove",
        # /giveaway
        "giveaway create",
        "giveaway reroll",
        # /group
        "group create",
        # /members
        "members list",
        "members card",
        "members register",
        "members probation",
        "members verify",
        "members leave",
        "members promote",
        # /applications
        "applications cutoff",
        # /training
        "training mentees",
        "training assign",
        "training complete",
        "training advance",
        "training mentor",
        "training clearance",
        "training rules",
        "training criteria",
        "training propose",
        "training decide",
        # /bonus
        "bonus declare",
        "bonus preview",
        "bonus adjust",
        "bonus approve",
        "bonus show",
        "bonus my",
        "bonus ranks",
        # /stock
        "stock locations",
        "stock item",
        "stock receive",
        "stock reserve",
        "stock release",
        "stock kit_request",
        "stock kit_reserve",
        "stock kit_issue",
        "stock kit_return",
        "stock kit_show",
        "stock consume",
        "stock adjust",
        "stock reconcile",
        "stock alerts",
        "stock mat_upgrade",
        "stock sell",
        "stock auction",
        "stock tax",
        # /treasury
        "treasury accounts",
        "treasury show",
        "treasury legacy",
        "treasury from_report",
        "treasury obligation",
        "treasury approve",
        "treasury batch",
        "treasury pay",
        "treasury confirm",
        "treasury cancel",
        "treasury reconcile",
        "treasury reverse",
        # /help
        "help overview",
        "help cooldown",
        # /music
        "music play",
        "music queue",
        "music now",
        "music volume",
        "music skip",
        "music cancel",
        "music mode",
        "music pause",
        "music resume",
        "music disconnect",
        "music stats",
        # /panels
        "panels list",
        "panels deploy",
        "panels redeploy",
        "panels repair",
        # /poll
        "poll create",
        # /progress
        "progress tree",
        "progress define",
        "progress advance",
        "progress goal",
        "progress show",
        "progress direction_preview",
        "progress direction",
        "progress hours",
        "progress season",
        "progress lore",
        "progress review",
        "progress announce_season",
        "progress org_level",
        # /queue
        "queue list",
        "queue claim",
        "queue transfer",
        "queue escalate",
        "queue appeal",
        "queue show",
        "queue summary",
        # /staff
        "staff panel",
        # /recruiting
        "recruiting link",
        "recruiting update",
        "recruiting stats",
        "recruiting assign",
        "recruiting unresolved",
        "recruiting resolve",
        # /report
        "report submit",
        "report review",
        "report reverse",
        "report show",
        "report stats",
        # /setup
        "setup start",
        "setup status",
        # /showdown
        "showdown toggle",
        # /stats
        "stats weekly",
        "stats update",
        "stats server_online",
        "stats economy",
        "stats economy_config",
        # /summit
        "summit list_create",
        "summit contract_create",
        # /voice
        "voice limit",
        "voice kick",
        "voice ban",
        "voice unban",
        "voice access",
        "voice name",
        "voice transfer",
        "voice delete",
        # root-level
        "me",
        "vabority",
    }
)


# Group names are not invokable on their own, but Discord shows the subcommand
# picker when a user types one, so referencing a bare group in help text is
# legitimate. They are tracked separately from invokable commands.
GROUPS: frozenset[str] = frozenset(
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
        "event",
        "fundraiser",
        "giveaway",
        "group",
        "help",
        "members",
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
        "voice",
    }
)

# Everything that may legally appear as a `/…` reference in user-facing text.
REFERENCEABLE: frozenset[str] = COMMANDS | GROUPS


class UnknownCommand(KeyError):
    """Raised when user-facing text references a command that does not exist."""


def cmd(qualified_name: str) -> str:
    """Return the display form of a slash command, e.g. ``/coins daily``.

    Raises :class:`UnknownCommand` for a name absent from :data:`COMMANDS`, so a
    typo or a stale reference fails at import time rather than reaching a user.
    """

    name = qualified_name.strip().lstrip("/")
    if name not in COMMANDS:
        raise UnknownCommand(
            f"{qualified_name!r} is not a registered command; "
            "update ui/command_registry.COMMANDS together with the command tree"
        )
    return f"/{name}"


def code(qualified_name: str) -> str:
    """Same as :func:`cmd`, wrapped in inline code formatting."""

    return f"`{cmd(qualified_name)}`"


def tree_command_names(tree) -> set[str]:
    """Collect every invokable qualified name from a live command tree.

    Groups themselves are not invokable and are excluded; only leaf commands
    (and root-level commands) are returned.
    """

    from discord import app_commands

    names: set[str] = set()
    for command in tree.walk_commands():
        if isinstance(command, app_commands.Group):
            continue
        names.add(command.qualified_name)
    return names


def tree_group_names(tree) -> set[str]:
    """Collect every group name from a live command tree."""

    from discord import app_commands

    return {
        command.qualified_name
        for command in tree.walk_commands()
        if isinstance(command, app_commands.Group)
    }


def diff_against_tree(tree) -> tuple[set[str], set[str]]:
    """Return ``(missing_from_registry, missing_from_tree)``."""

    live = tree_command_names(tree)
    return live - COMMANDS, set(COMMANDS - live)


def diff_groups_against_tree(tree) -> tuple[set[str], set[str]]:
    """Same as :func:`diff_against_tree`, for command groups."""

    live = tree_group_names(tree)
    return live - GROUPS, set(GROUPS - live)


def format_diff(missing_from_registry: Iterable[str], missing_from_tree: Iterable[str]) -> str:
    parts = []
    extra = sorted(missing_from_registry)
    stale = sorted(missing_from_tree)
    if extra:
        parts.append("отсутствуют в реестре: " + ", ".join(extra))
    if stale:
        parts.append("в реестре, но не зарегистрированы: " + ", ".join(stale))
    return "; ".join(parts)
