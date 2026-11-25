"""Paging for lists that outgrow one Discord message.

The birthday panel and the car list were rendered as one long string and cut at
the 4000-character Components v2 budget. Everyone past the cut simply was not
shown — no page marker, no way to reach them, and the list silently got worse as
the family grew.

A persistent panel keeps showing a useful summary; the full list moves to an
ephemeral pager, so paging is per-viewer and never rewrites a shared message.

Shared-channel panels that must survive a restart live in ``ui.navigation``:
their page, filter and sort are encoded in ``custom_id``. This module stays
the personal ephemeral pager — do not move that state here.
"""

from __future__ import annotations

import discord

from core.log_context import bind_interaction, report_interaction_error

PAGE_CHARACTER_LIMIT = 1_800


def paginate_lines(
    lines: list[str],
    *,
    limit: int = PAGE_CHARACTER_LIMIT,
    separator: str = "\n",
) -> list[str]:
    """Split rendered lines into pages without ever cutting a line in half.

    A line longer than ``limit`` gets its own page rather than being dropped:
    a truncated entry is still an entry the reader can see and report.
    """

    pages: list[str] = []
    current: list[str] = []
    used = 0
    for line in lines:
        cost = len(line) + (len(separator) if current else 0)
        if current and used + cost > limit:
            pages.append(separator.join(current))
            current, used = [], 0
            cost = len(line)
        current.append(line)
        used += cost
    if current:
        pages.append(separator.join(current))
    return pages or [""]


def format_page_caption(page: int, total: int) -> str:
    """1-based caption used by persistent navigators and ephemeral footers."""

    safe_total = total if total > 0 else 1
    safe_page = page if page > 0 else 1
    return f"страница {safe_page} из {safe_total}"


class EphemeralPager(discord.ui.View):
    """Per-viewer paging over a pre-rendered list of pages.

    Ephemeral by construction: the message belongs to the one person who asked
    for it, so paging cannot disturb anyone else's view of a shared panel.
    """

    def __init__(
        self,
        pages: list[str],
        *,
        title: str,
        colour: int,
        owner_id: int,
        footer: str | None = None,
        timeout: float = 180.0,
    ) -> None:
        super().__init__(timeout=timeout)
        self.pages = pages or [""]
        self.title = title
        self.colour = colour
        self.owner_id = owner_id
        self.footer = footer
        self.index = 0
        self._sync_buttons()

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        bind_interaction(interaction, source="component")
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message(
                "Этот список открыт другим участником.", ephemeral=True
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
            code="UI.PAGER_FAILED",
            summary="Не удалось переключить страницу.",
            retryable=True,
            item=item,
            source="component",
        )

    def embed(self) -> discord.Embed:
        embed = discord.Embed(
            title=self.title,
            description=self.pages[self.index] or "Записей нет.",
            colour=discord.Colour(self.colour),
        )
        parts = [f"Страница {self.index + 1} из {len(self.pages)}"]
        if self.footer:
            parts.append(self.footer)
        embed.set_footer(text=" • ".join(parts))
        return embed

    def _sync_buttons(self) -> None:
        self.previous_page.disabled = self.index <= 0
        self.next_page.disabled = self.index >= len(self.pages) - 1

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.edit_message(embed=self.embed(), view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = max(0, self.index - 1)
        await self._show(interaction)

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(
        self, interaction: discord.Interaction, button: discord.ui.Button
    ) -> None:
        self.index = min(len(self.pages) - 1, self.index + 1)
        await self._show(interaction)


async def send_paged_list(
    interaction: discord.Interaction,
    lines: list[str],
    *,
    title: str,
    colour: int,
    empty: str = "Записей нет.",
    footer: str | None = None,
) -> None:
    """Reply with the full list, paged, visible only to the requester."""

    if not lines:
        await interaction.followup.send(empty, ephemeral=True)
        return
    pages = paginate_lines(lines)
    pager = EphemeralPager(
        pages,
        title=title,
        colour=colour,
        owner_id=interaction.user.id,
        footer=footer,
    )
    await interaction.followup.send(
        embed=pager.embed(),
        view=pager if len(pages) > 1 else discord.utils.MISSING,
        ephemeral=True,
    )
