import time
from collections import OrderedDict
from collections.abc import Callable

import discord

from core.log_context import bind_interaction, report_interaction_error
from ui.messages import respond_v2
from utils.access import is_bot_owner

_COMPONENT_HITS: OrderedDict[
    tuple[int | None, int, int | None, str], float
] = OrderedDict()
_COMPONENT_COOLDOWN_SECONDS = 1.5
_MAX_COMPONENT_HITS = 20_000


def stop_registered_view(view: discord.ui.View | discord.ui.LayoutView) -> None:
    """Remove a registered view through discord.py's supported lifecycle."""

    # Client.add_view installs a removal callback on the view. discord.py does
    # not expose Client.remove_view; stop() invokes that callback and also
    # cancels the view timeout task.
    if not view.is_finished():
        view.stop()


def stop_matching_persistent_views(
    client: discord.Client,
    predicate: Callable[[discord.ui.View | discord.ui.LayoutView], bool],
) -> int:
    """Stop persistent views, including Components v2 compatibility wrappers."""

    stopped = 0
    for registered in tuple(client.persistent_views):
        if not isinstance(registered, (discord.ui.View, discord.ui.LayoutView)):
            continue
        legacy = getattr(registered, "legacy_view", None)
        if predicate(registered) or (
            isinstance(legacy, (discord.ui.View, discord.ui.LayoutView))
            and predicate(legacy)
        ):
            stop_registered_view(registered)
            stopped += 1
    return stopped


def _record_component_hit(
    key: tuple[int | None, int, int | None, str], now: float
) -> float:
    previous = _COMPONENT_HITS.get(key, 0.0)
    _COMPONENT_HITS[key] = now
    _COMPONENT_HITS.move_to_end(key)
    while len(_COMPONENT_HITS) > _MAX_COMPONENT_HITS:
        _COMPONENT_HITS.popitem(last=False)
    return previous


class SafeView(discord.ui.View):
    """View that reports component failures without leaking internals to users."""

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bind_interaction(interaction, source="component")
        supervisor = getattr(interaction.client, "task_supervisor", None)
        if (
            supervisor is not None
            and supervisor.paused
            and not is_bot_owner(interaction.user)
        ):
            await respond_v2(
                interaction,
                "Бот выполняет техническое обслуживание. Повторите действие позже.",
                ephemeral=True,
            )
            return False
        if is_bot_owner(interaction.user):
            return True
        custom_id = "unknown"
        if interaction.data:
            custom_id = str(interaction.data.get("custom_id", "unknown"))
        message_id = interaction.message.id if interaction.message is not None else None
        key = (interaction.guild_id, interaction.user.id, message_id, custom_id)
        now = time.monotonic()
        previous = _record_component_hit(key, now)
        if now - previous < _COMPONENT_COOLDOWN_SECONDS:
            await respond_v2(interaction,
                "Действие уже обрабатывается. Подождите немного.", ephemeral=True
            )
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item,
    ) -> None:
        await report_interaction_error(
            interaction,
            error,
            code="COMPONENT.INTERNAL",
            summary="Не удалось выполнить действие.",
            retryable=True,
            item=item,
            source="component",
            alert_key="command:unhandled",
        )


class SafeModal(discord.ui.Modal):
    """Modal that gives the submitter a safe error instead of timing out."""

    cooldown_seconds = 2.0

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        # Discord deprecated top-level TextInput fields in favour of Label
        # containers (API component type 18). Keep existing attribute names and
        # callbacks intact while emitting only the current modal structure.
        for child in tuple(self.children):
            if isinstance(child, discord.ui.TextInput):
                self.remove_item(child)
                self.add_item(child)

    def add_item(self, item: discord.ui.Item):
        if isinstance(item, discord.ui.TextInput):
            label = item._underlying.label or "Поле"
            item._underlying.label = None
            item.row = None
            item = discord.ui.Label(text=str(label)[:45], component=item)
        return super().add_item(item)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bind_interaction(interaction, source="modal")
        supervisor = getattr(interaction.client, "task_supervisor", None)
        if (
            supervisor is not None
            and supervisor.paused
            and not is_bot_owner(interaction.user)
        ):
            await respond_v2(
                interaction,
                "Бот выполняет техническое обслуживание. Повторите действие позже.",
                ephemeral=True,
            )
            return False
        if is_bot_owner(interaction.user):
            return True
        key = (
            interaction.guild_id,
            interaction.user.id,
            None,
            "modal:"
            f"{type(self).__module__}.{type(self).__qualname__}",
        )
        now = time.monotonic()
        previous = _record_component_hit(key, now)
        if now - previous < self.cooldown_seconds:
            await respond_v2(interaction,
                "Форма уже обрабатывается. Подождите немного.", ephemeral=True
            )
            return False
        return True

    async def on_error(
        self,
        interaction: discord.Interaction,
        error: Exception,
        item: discord.ui.Item | None = None,
    ) -> None:
        # discord.ui.Modal inherits BaseView.on_error(interaction, error, item);
        # a two-argument override raises TypeError inside the handler that is
        # supposed to be reporting the original failure.
        await report_interaction_error(
            interaction,
            error,
            code="MODAL.INTERNAL",
            summary="Не удалось обработать форму. Проверьте данные и повторите.",
            retryable=True,
            item=item,
            source="modal",
            alert_key="command:unhandled",
        )
