import asyncio
import logging
from datetime import UTC, datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from services.static_messages import StaticPayload, upsert_static_message
from ui.messages import (
    edit_message_v2,
    edit_original_response_v2,
    followup_v2,
    respond_v2,
    send_v2,
)

from config import CHANNELS, ROLES, SHOP_PRICES, ROLE_REJECTION_REASONS
from ui.command_registry import code
from utils.discord_lookup import (
    get_role,
    get_text_channel,
    request_ping_content,
    request_ping_mentions,
    has_high_tools_access,
    is_high_tools_interaction,
)
from utils.locks import KeyedLockPool
from ui.design import Accent
from ui.templates import notice, public_panel
from utils.embeds import apply_accent
from utils.views import SafeModal, SafeView, stop_registered_view
from utils.access import administrator_or_owner, has_administrator_access, is_bot_owner
from repositories.shop import (
    RoleRejectionPresentation,
    RoleSale,
    ShopPurchaseIntent,
    claim_role_sale,
    complete_role_sale,
    complete_shop_purchase,
    count_active_role_holders,
    create_shop_purchase_intent,
    ensure_role_rejection_outbox,
    ensure_role_sale_outbox,
    ensure_shop_purchase_outbox,
    get_pending_role_rejection_presentations,
    get_recoverable_role_approval_claims,
    get_reserved_role_sales,
    get_role_rejection_presentation,
    get_role_sale,
    get_reusable_color_role_ids,
    get_shop_purchase_intent,
    get_unfinished_shop_purchase_intents,
    mark_role_rejection_presented,
    mark_shop_intent_provisioning,
    refund_shop_purchase,
    release_role_approval_claim,
    set_shop_intent_discord_role,
    set_shop_purchase_error,
)
from repositories.stats import record_processed_action_once
from ui.layouts import marker_component_id
from ui.message_specs import MessageSpec, MessageTone, Outcome
from utils.database import (
    adjust_coins,
    clear_voice_owner_bans,
    claim_daily_coins,
    claim_role_purchase_request,
    complete_role_purchase_approval,
    clear_vip,
    get_coin_account,
    get_all_summit_views,
    get_expired_vip_users,
    get_richest_users,
    get_pending_role_purchase_requests,
    get_role_purchase_publication,
    get_role_purchase_request,
    count_pending_user_role_purchase_requests,
    get_summit_list,
    get_summit_list_members,
    get_user_purchased_roles,
    get_active_purchased_role_owners,
    get_voice_owner_bans,
    get_voice_room_by_owner,
    has_active_vip,
    record_stats_processed_action,
    reject_role_purchase_with_refund,
    remove_coins,
    reset_coins,
    purchase_role_request,
    purchase_summit_emoji,
    deactivate_purchased_role,
    set_setting,
    set_role_purchase_request_message,
    set_role_purchase_publication_error,
    ensure_role_purchase_publication_outbox,
    set_role_purchase_discord_role,
    mark_role_purchase_needs_reconcile,
    transfer_coins,
)


logger = logging.getLogger(__name__)
MSK = timezone(timedelta(hours=3), name="MSK")
SHOP_MESSAGE_KEY = "shop_message_id"
ROLE_MARK = "🪙"
MAX_COIN_OPERATION = 1_000_000_000
_SHOP_USER_LOCKS = KeyedLockPool[int]()
_SHOP_GUILD_LOCKS = KeyedLockPool[int]()
_SHOP_VIP_ROLES: dict[int, discord.Role] = {}
_SHOP_INTENT_LOCKS = KeyedLockPool[int]()
_SHOP_REQUEST_LOCKS = KeyedLockPool[int]()
_SHOP_ACTION_LOCKS = KeyedLockPool[str]()


def _role_used_by_other_member(
    guild: discord.Guild,
    role: discord.Role,
    excluded_user_id: int,
) -> bool:
    role_members = getattr(role, "members", None)
    if role_members is not None:
        return any(member.id != excluded_user_id for member in role_members)
    members = getattr(guild, "members", ())
    if isinstance(members, dict):
        members = members.values()
    return any(
        member.id != excluded_user_id and role in getattr(member, "roles", ())
        for member in members
    )


def _message_has_marker(message: discord.Message, marker: str) -> bool:
    expected = marker_component_id(marker)

    def visit(component) -> bool:
        return getattr(component, "id", None) == expected or any(
            visit(child) for child in getattr(component, "children", ())
        )

    return any(visit(component) for component in message.components)


async def _find_marked_message(
    channel,
    marker: str,
    *,
    bot_user_id: int | None,
    limit: int = 1_000,
) -> discord.Message | None:
    async for candidate in channel.history(limit=limit):
        if (
            (bot_user_id is None or candidate.author.id == bot_user_id)
            and _message_has_marker(candidate, marker)
        ):
            return candidate
    return None


async def _log_role_action(
    guild: discord.Guild,
    text: str,
    *,
    outcome: Outcome,
    event_key: str | None = None,
) -> bool:
    lock_key = event_key or f"legacy:{guild.id}:{text}"
    async with _SHOP_ACTION_LOCKS.lock(lock_key):
        is_first_attempt = True
        if outcome in {Outcome.APPROVED, Outcome.REJECTED}:
            if event_key is None:
                await record_stats_processed_action(
                    guild.id,
                    "application",
                    outcome.value,
                )
            else:
                is_first_attempt = await record_processed_action_once(
                    guild.id,
                    event_key,
                    "application",
                    outcome.value,
                )
        channel = get_text_channel(guild, "request_actions")
        if channel is None:
            logger.warning("Role action log channel is unavailable for %s", event_key)
            return False
        marker = f"shop-role-action:{event_key}" if event_key else None
        try:
            if marker and not is_first_attempt:
                existing = await _find_marked_message(
                    channel,
                    marker,
                    bot_user_id=guild.me.id if guild.me else None,
                )
                if existing is not None:
                    return True
            approved = outcome is Outcome.APPROVED
            details = text.lstrip("✅❌ ").strip()
            details = details.replace(" by ", "\nОбработал: ")
            spec = notice(
                code="SHOP.ROLE_DECISION",
                title="Решение по кастомной роли",
                summary=details,
                next_step="Дополнительных действий не требуется.",
                tone=MessageTone.SUCCESS if approved else MessageTone.ERROR,
                reference=event_key,
            )
            await send_v2(channel, embed=spec, component_marker=marker)
            return True
        except discord.HTTPException:
            logger.exception("Failed to log role purchase action")
            return False


def _price(value: int) -> str:
    return f"{value:,} Guinn Coins".replace(",", " ")


def _colour(value: str) -> discord.Colour:
    raw = value.strip().removeprefix("#")
    if len(raw) != 6:
        raise ValueError("HEX должен иметь формат #RRGGBB")
    try:
        return discord.Colour(int(raw, 16))
    except ValueError as exc:
        raise ValueError("HEX должен иметь формат #RRGGBB") from exc


def shop_embed() -> MessageSpec:
    return apply_accent(
        public_panel(
            code="PANEL.SHOP",
            title="🪙 Магазин Guinn Coins",
            purpose=(
                f"Получайте валюту через {code('coins daily')}, переводите её через "
                f"{code('coins pay')} и покупайте "
                "косметические возможности. Покупные роли отмечены символом 🪙. "
                "Без VIP доступна одна роль «Цвет» и одна кастомная; с VIP — до трёх ролей."
            ),
            rules=(
                (
                    "🎨 Цветная роль",
                    f"{_price(SHOP_PRICES['color_role'])}\n"
                    "Роль называется «Цвет», оттенок выбираете вы.",
                ),
                (
                    "✨ Кастомная роль",
                    f"{_price(SHOP_PRICES['custom_role'])}\n"
                    "Своё название, основной HEX и градиент при поддержке сервера.",
                ),
                (
                    "👑 VIP",
                    f"Неделя: {_price(SHOP_PRICES['vip_week'])}\n"
                    f"Месяц: {_price(SHOP_PRICES['vip_month'])}\n"
                    "Доступ к постоянному voice ban-листу и VIP-эмодзи в списках.",
                ),
                (
                    "💠 VIP-эмодзи",
                    f"{_price(SHOP_PRICES['summit_emoji'])}\n"
                    f"Показывается рядом с вами в {code('summit list_create')} и "
                    f"{code('summit contract_create')}, пока активен VIP.",
                ),
                (
                    "♻️ Продажа роли",
                    "Купленную косметическую роль можно удалить и вернуть **50%** её стоимости. "
                    f"С активным VIP порядок своих ролей меняется через {code('coins role_order')}.",
                ),
            ),
            action=f"Ежедневная награда: {_price(SHOP_PRICES['daily'])}",
        ),
        Accent.TREASURY,
    )


async def _refresh_user_summits(guild: discord.Guild, user_id: int) -> None:
    from cogs.summits import refresh_contract_message, refresh_summit_list_message

    _, regular = await get_all_summit_views()
    for list_id, _ in regular:
        summit = await get_summit_list(list_id)
        if not summit or summit[7] not in {"list", "contract"}:
            continue
        members = await get_summit_list_members(list_id)
        if not any(member[0] == user_id for member in members):
            continue
        if summit[7] == "contract":
            await refresh_contract_message(guild, list_id)
        else:
            await refresh_summit_list_message(guild, list_id)


async def _order_cosmetic_roles(
    guild: discord.Guild,
    user_id: int,
    preferred_role_ids: list[int] | None = None,
) -> list[discord.Role]:
    """Keep custom roles above colour roles while preserving user order per group."""
    purchases = await get_user_purchased_roles(user_id)
    kind_by_id = {purchase[1]: purchase[4] for purchase in purchases}
    roles_by_id = {
        role_id: role
        for role_id in kind_by_id
        if (role := guild.get_role(role_id)) is not None
    }
    current = sorted(roles_by_id.values(), key=lambda role: role.position, reverse=True)
    preferred = [
        roles_by_id[role_id]
        for role_id in (preferred_role_ids or [])
        if role_id in roles_by_id
    ]
    preferred.extend(role for role in current if role not in preferred)
    ordered = sorted(
        preferred,
        key=lambda role: 0 if kind_by_id.get(role.id) == "custom" else 1,
    )
    positions = sorted((role.position for role in ordered), reverse=True)
    if len(ordered) > 1:
        await guild.edit_role_positions(
            positions=dict(zip(ordered, positions, strict=True)),
            reason=f"Guinn Coins role order for user {user_id}",
        )
    return ordered


async def update_shop_message(guild: discord.Guild) -> discord.Message | None:
    channel = get_text_channel(guild, "shop")
    if channel is None:
        raise RuntimeError(f"Канал #{CHANNELS['shop']} не найден")

    def build() -> StaticPayload:
        embed = shop_embed()
        return StaticPayload(
            embed=apply_accent(embed, Accent.TREASURY),
            view=ShopView(),
            revision=2,
        )

    message = await upsert_static_message(
        guild=guild,
        channel=channel,
        panel_key="shop",
        build=build,
        legacy_setting_key=SHOP_MESSAGE_KEY,
        legacy_match=lambda item: bool(
            item.embeds and item.embeds[0].title == "🪙 Магазин Guinn Coins"
        ),
    )
    if message is None:
        return None
    await set_setting(SHOP_MESSAGE_KEY, str(message.id))
    return message


async def _create_cosmetic_role_unlocked(
    interaction: discord.Interaction,
    *,
    name: str,
    primary: str,
    secondary: str | None,
    tertiary: str | None,
    price: int,
    kind: str,
):
    purchases = await get_user_purchased_roles(interaction.user.id)
    vip_active = await has_active_vip(interaction.user.id)
    pending = await count_pending_user_role_purchase_requests(interaction.user.id)
    if vip_active:
        if len(purchases) + pending >= 3:
            await followup_v2(interaction,
                "С активным VIP можно иметь не более трёх купленных ролей, включая заявки на рассмотрении.",
                ephemeral=True,
            )
            return
    elif any(purchase[4] == kind for purchase in purchases) or (
        kind == "custom" and pending
    ):
        await followup_v2(interaction,
            "Без VIP можно иметь только одну цветную и одну кастомную роль.",
            ephemeral=True,
        )
        return
    try:
        primary_colour = _colour(primary)
        secondary_colour = _colour(secondary) if secondary and secondary.strip() else None
        tertiary_colour = _colour(tertiary) if tertiary and tertiary.strip() else None
    except ValueError as exc:
        await followup_v2(interaction, str(exc), ephemeral=True)
        return
    if kind == "color":
        owned_role_ids = {
            int(purchase[1])
            for purchase in purchases
            if purchase[4] == "color"
        }
        if any(
            role.id in owned_role_ids
            and role.name == f"{ROLE_MARK} Цвет"
            and role.colour.value == primary_colour.value
            for role in interaction.guild.roles
        ):
            await followup_v2(
                interaction,
                "У вас уже есть цветная роль с таким оттенком.",
                ephemeral=True,
            )
            return
    role_name = f"{ROLE_MARK} {'Цвет' if kind == 'color' else name.strip()}"[:100]
    if kind == "custom":
        approval_channel = get_text_channel(interaction.guild, "role_approval")
        if approval_channel is None:
            await followup_v2(interaction,
                f"Канал #{CHANNELS['role_approval']} не найден. Средства не списаны.",
                ephemeral=True,
            )
            return
        request_id = await purchase_role_request(
            interaction.user.id,
            role_name,
            primary_colour.value,
            secondary_colour.value if secondary_colour else None,
            tertiary_colour.value if tertiary_colour else None,
            price,
            guild_id=interaction.guild.id,
            channel_id=approval_channel.id,
        )
        if request_id is None:
            await followup_v2(interaction, "Недостаточно Guinn Coins.", ephemeral=True)
            return
        published = False
        try:
            cog = interaction.client.get_cog("ShopCog")
            if isinstance(cog, ShopCog):
                await cog.publish_role_request(request_id, interaction.guild)
                published = True
        except Exception as exc:
            logger.exception("Failed to publish role purchase request %s", request_id)
            await set_role_purchase_publication_error(request_id, exc)
        await followup_v2(interaction,
            (
                f"Название **{role_name}** отправлено старшему составу на рассмотрение. "
                "Роль будет выдана после проверки."
                if published
                else "Покупка сохранена. Заявка будет доставлена старшему составу "
                "автоматически после восстановления связи с Discord."
            ),
            ephemeral=True,
        )
        return
    intent = await create_shop_purchase_intent(
        interaction_id=interaction.id,
        guild_id=interaction.guild.id,
        user_id=interaction.user.id,
        purchase_kind="color",
        role_name=role_name,
        price=price,
        primary_color=primary_colour.value,
        secondary_color=secondary_colour.value if secondary_colour else None,
        tertiary_color=tertiary_colour.value if tertiary_colour else None,
    )
    if intent is None:
        await followup_v2(interaction, "Недостаточно Guinn Coins.", ephemeral=True)
        return
    role_id = None
    try:
        cog = interaction.client.get_cog("ShopCog")
        if isinstance(cog, ShopCog):
            role_id = await cog.fulfill_purchase_intent(intent.id)
    except Exception as exc:
        logger.exception("Shop purchase %s queued for retry", intent.id)
        await set_shop_purchase_error(intent.id, exc)
    current = await get_shop_purchase_intent(intent.id)
    if current is not None and current.state == "refunded":
        await followup_v2(
            interaction,
            "Discord не позволил выдать роль. Средства автоматически возвращены.",
            ephemeral=True,
        )
        return
    if current is not None and current.state == "completed":
        gradient_note = " с градиентом" if secondary_colour else ""
        await followup_v2(
            interaction,
            f"Роль <@&{role_id or current.discord_role_id}>{gradient_note} "
            f"куплена за {_price(price)}.",
            ephemeral=True,
        )
        return
    await followup_v2(
        interaction,
        "Покупка сохранена и будет завершена автоматически после восстановления связи с Discord.",
        ephemeral=True,
    )


async def _create_cosmetic_role(
    interaction: discord.Interaction,
    *,
    name: str,
    primary: str,
    secondary: str | None,
    tertiary: str | None,
    price: int,
    kind: str,
):
    await interaction.response.defer(ephemeral=True)
    async with _SHOP_USER_LOCKS.lock(interaction.user.id):
        await _create_cosmetic_role_unlocked(
            interaction,
            name=name,
            primary=primary,
            secondary=secondary,
            tertiary=tertiary,
            price=price,
            kind=kind,
        )


def _role_approval_embed(
    request_id: int,
    user_id: int,
    role_name: str,
    primary: int,
    secondary: int | None,
    tertiary: int | None,
    price: int,
    *,
    status: str = "🟡 Ожидает одобрения",
    reviewer_id: int | None = None,
) -> MessageSpec:
    colors = f"Основной: `#{primary:06X}`"
    if secondary is not None:
        colors += f"\nВторой: `#{secondary:06X}`"
    if tertiary is not None:
        colors += f"\nТретий: `#{tertiary:06X}`"
    details: list[tuple[str, str]] = [
        ("Покупатель", f"<@{user_id}>"),
        ("Название", role_name),
        ("Стоимость", _price(price)),
        ("Цвета", colors),
        ("Статус", status),
    ]
    if reviewer_id:
        details.append(("Рассмотрел", f"<@{reviewer_id}>"))
    pending = "ожидает" in status.casefold()
    return notice(
        code="SHOP.ROLE_REQUEST",
        title=f"Одобрение кастомной роли #{request_id:04d}",
        details=tuple(details),
        next_step=(
            "Проверьте заявку и зафиксируйте решение кнопками под карточкой."
            if pending
            else "Дополнительных действий не требуется."
        ),
        reference=f"#{request_id:04d}",
    )


async def _edit_role_decision(
    message: discord.Message,
    spec: MessageSpec,
    *,
    approved: bool,
) -> None:
    spec = apply_accent(spec, Accent.SUCCESS if approved else Accent.ERROR)
    await edit_message_v2(message, embed=spec, view=None)


def _can_review_roles(interaction: discord.Interaction) -> bool:
    user = interaction.user
    if not isinstance(user, discord.Member):
        return False
    if is_bot_owner(user):
        return True
    if is_high_tools_interaction(interaction):
        return has_high_tools_access(interaction)
    names = {role.name for role in user.roles}
    return (
        has_administrator_access(user)
        or ROLES["staff"] in names
        or ROLES["high"] in names
        or ROLES["owner"] in names
        or ROLES["dep_owner"] in names
    )


class RoleRejectReasonSelect(discord.ui.Select):
    def __init__(self, approval_view: "RoleApprovalView", source: discord.Message):
        options = [
            discord.SelectOption(label=reason[:100], value=reason[:100])
            for reason in ROLE_REJECTION_REASONS
        ] or [discord.SelectOption(label="Другое", value="Другое")]
        super().__init__(placeholder="Выберите причину отклонения", options=options)
        self.approval_view = approval_view
        self.source = source

    async def callback(self, interaction: discord.Interaction):
        await self.approval_view.reject_with_reason(
            interaction, self.source, self.values[0]
        )


class RoleRejectReasonView(SafeView):
    def __init__(self, approval_view: "RoleApprovalView", source: discord.Message):
        super().__init__(timeout=180)
        self.add_item(RoleRejectReasonSelect(approval_view, source))


class RoleApprovalView(SafeView):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id

    async def _claim(self, interaction: discord.Interaction):
        if not _can_review_roles(interaction):
            await respond_v2(interaction,
                "Одобрять роли может только старший состав.", ephemeral=True
            )
            return None
        await interaction.response.defer(ephemeral=True)
        request = await claim_role_purchase_request(
            self.request_id, interaction.user.id
        )
        if request is None:
            await followup_v2(interaction,
                "Эта заявка уже рассмотрена.", ephemeral=True
            )
            return None
        return request

    @discord.ui.button(
        label="Одобрить",
        style=discord.ButtonStyle.success,
        custom_id="role_approval:approve",
    )
    async def approve(self, interaction: discord.Interaction, button):
        async with _SHOP_REQUEST_LOCKS.lock(self.request_id):
            await self._approve_locked(interaction)

    async def _approve_locked(self, interaction: discord.Interaction) -> None:
        request = await self._claim(interaction)
        if request is None:
            return
        _, user_id, role_name, primary, secondary, tertiary, price, _, _ = request
        member = interaction.guild.get_member(user_id)
        if member is None:
            refunded = await reject_role_purchase_with_refund(
                self.request_id,
                interaction.user.id,
                price,
                "role_rejection_refund",
                reason="Пользователь покинул сервер",
            )
            if not refunded:
                raise RuntimeError("Role request state changed during review")
            try:
                await _edit_role_decision(
                    interaction.message,
                    _role_approval_embed(
                        self.request_id,
                        user_id,
                        role_name,
                        primary,
                        secondary,
                        tertiary,
                        price,
                        status="🔴 Отклонено: пользователь покинул сервер",
                        reviewer_id=interaction.user.id,
                    ),
                    approved=False,
                )
            except discord.HTTPException:
                logger.exception(
                    "Role rejection %s presentation queued for retry",
                    self.request_id,
                )
            await followup_v2(interaction,
                "Пользователь не найден, покупка возвращена.", ephemeral=True
            )
            await _log_role_action(
                interaction.guild,
                f"❌ Кастомная роль #{self.request_id:04d} отклонена "
                f"{interaction.user.mention}: пользователь покинул сервер.",
                outcome=Outcome.REJECTED,
                event_key=f"custom-role:{self.request_id}:rejected",
            )
            return
        role = None
        try:
            kwargs = {}
            features = {feature.casefold() for feature in interaction.guild.features}
            if "enhanced_role_colors" in features:
                kwargs["secondary_color"] = (
                    discord.Colour(secondary) if secondary is not None else None
                )
                kwargs["tertiary_color"] = (
                    discord.Colour(tertiary) if tertiary is not None else None
                )
            role = await interaction.guild.create_role(
                name=role_name,
                colour=discord.Colour(primary),
                reason=f"role-request:{self.request_id} approved by {interaction.user}",
                **kwargs,
            )
            persisted = await set_role_purchase_discord_role(
                self.request_id,
                interaction.user.id,
                role.id,
                guild_id=interaction.guild.id,
            )
            if not persisted:
                raise RuntimeError("Role request state changed before role persistence")
            await member.add_roles(role, reason="Approved Guinn Coins role")
            completed = await complete_role_purchase_approval(
                self.request_id,
                interaction.user.id,
                user_id,
                role.id,
                role.name,
                price,
            )
            if not completed:
                raise RuntimeError("Role request state changed during approval")
        except Exception as exc:
            logger.exception("Failed to grant purchased role request %s", self.request_id)
            rollback_confirmed = role is None
            if role is not None:
                try:
                    await role.delete(reason="Role approval rollback")
                    rollback_confirmed = True
                except discord.NotFound:
                    rollback_confirmed = True
                except discord.HTTPException:
                    logger.exception("Failed to delete role after approval rollback")
            refunded = False
            if rollback_confirmed:
                refunded = await reject_role_purchase_with_refund(
                    self.request_id,
                    interaction.user.id,
                    price,
                    "role_creation_refund",
                    reason="Не удалось выдать роль; средства возвращены",
                )
            if not rollback_confirmed or not refunded:
                await mark_role_purchase_needs_reconcile(
                    self.request_id,
                    interaction.user.id,
                    exc,
                )
            try:
                decision = _role_approval_embed(
                    self.request_id,
                    user_id,
                    role_name,
                    primary,
                    secondary,
                    tertiary,
                    price,
                    status=(
                        "🔴 Ошибка выдачи, средства возвращены"
                        if refunded
                        else "🟡 Выдача будет восстановлена автоматически; возврат не выполнен"
                    ),
                    reviewer_id=interaction.user.id,
                )
                if refunded:
                    await _edit_role_decision(
                        interaction.message,
                        decision,
                        approved=False,
                    )
                else:
                    await edit_message_v2(
                        interaction.message,
                        embed=decision,
                        view=None,
                    )
            except discord.HTTPException:
                logger.exception("Failed to update rejected role approval message")
            await followup_v2(
                interaction,
                (
                    "Не удалось выдать роль, средства возвращены."
                    if refunded
                    else "Discord не подтвердил откат роли. Возврат не выполнен, "
                    "операция поставлена на безопасное восстановление."
                ),
                ephemeral=True,
            )
            return

        try:
            await _order_cosmetic_roles(interaction.guild, user_id)
        except discord.HTTPException:
            logger.exception("Failed to order approved role for %s", user_id)
        try:
            await _edit_role_decision(
                interaction.message,
                _role_approval_embed(
                    self.request_id,
                    user_id,
                    role_name,
                    primary,
                    secondary,
                    tertiary,
                    price,
                    status="🟢 Одобрено и выдано",
                    reviewer_id=interaction.user.id,
                ),
                approved=True,
            )
        except discord.HTTPException:
            logger.exception("Failed to update approved role request message")
        try:
            await send_v2(
                member,
                f"Ваша кастомная роль **{role_name}** одобрена и выдана.",
                component_marker=f"shop-role-dm:{self.request_id}:approved",
            )
        except discord.HTTPException:
            pass
        await _log_role_action(
            interaction.guild,
            f"✅ Кастомная роль #{self.request_id:04d}: {member.mention} получил "
            f"**{role_name}**; одобрил {interaction.user.mention}.",
            outcome=Outcome.APPROVED,
            event_key=f"custom-role:{self.request_id}:approved",
        )
        await followup_v2(interaction, "Роль одобрена и выдана.", ephemeral=True)

    @discord.ui.button(
        label="Отклонить",
        style=discord.ButtonStyle.danger,
        custom_id="role_approval:reject",
    )
    async def reject(self, interaction: discord.Interaction, button):
        if not _can_review_roles(interaction):
            await respond_v2(interaction,
                "Одобрять роли может только старший состав.", ephemeral=True
            )
            return
        await respond_v2(interaction,
            "Укажите причину отклонения:",
            view=RoleRejectReasonView(self, interaction.message),
            ephemeral=True,
        )

    async def reject_with_reason(
        self,
        interaction: discord.Interaction,
        source_message: discord.Message,
        reason: str,
    ):
        async with _SHOP_REQUEST_LOCKS.lock(self.request_id):
            await self._reject_with_reason_locked(
                interaction,
                source_message,
                reason,
            )

    async def _reject_with_reason_locked(
        self,
        interaction: discord.Interaction,
        source_message: discord.Message,
        reason: str,
    ):
        if not _can_review_roles(interaction):
            await respond_v2(interaction, "Нет прав.", ephemeral=True)
            return
        # Claiming can wait on SQLite under load; acknowledge the selector
        # before touching persistence so Discord's interaction token remains
        # valid.  The selector itself is an ephemeral message update.
        await interaction.response.defer()
        request = await claim_role_purchase_request(
            self.request_id, interaction.user.id
        )
        if request is None:
            await edit_original_response_v2(interaction,
                content="Эта заявка уже рассмотрена.", view=None
            )
            return
        _, user_id, role_name, primary, secondary, tertiary, price, _, _ = request
        refund = price // 2
        refunded = await reject_role_purchase_with_refund(
            self.request_id,
            interaction.user.id,
            refund,
            "role_rejection_refund",
            reason=reason,
        )
        if not refunded:
            raise RuntimeError("Role request state changed during rejection")
        await edit_original_response_v2(
            interaction,
            content=(
                f"Заявка отклонена. Причина: **{reason}**\n"
                f"Возвращено пользователю **{_price(refund)}** (50%)."
            ),
            view=None,
        )
        try:
            await _edit_role_decision(
                source_message,
                _role_approval_embed(
                    self.request_id,
                    user_id,
                    role_name,
                    primary,
                    secondary,
                    tertiary,
                    price,
                    status=(
                        f"🔴 Отклонено\nПричина: {reason}\n"
                        f"Возврат: {_price(refund)} (50%)"
                    ),
                    reviewer_id=interaction.user.id,
                ),
                approved=False,
            )
        except discord.HTTPException:
            logger.exception(
                "Role rejection %s presentation queued for retry",
                self.request_id,
            )
        member = interaction.guild.get_member(user_id)
        if member:
            try:
                await send_v2(member,
                    f"Название кастомной роли **{role_name}** отклонено. "
                    f"Причина: {reason}. Возвращено {_price(refund)} (50%).",
                    component_marker=f"shop-role-dm:{self.request_id}:rejected",
                )
            except discord.HTTPException:
                pass
        await _log_role_action(
            interaction.guild,
            f"❌ Кастомная роль #{self.request_id:04d} для <@{user_id}> отклонена "
            f"{interaction.user.mention}. Причина: {reason}. Возврат: {_price(refund)}.",
            outcome=Outcome.REJECTED,
            event_key=f"custom-role:{self.request_id}:rejected",
        )


class RolePurchaseModal(SafeModal):
    def __init__(self, custom: bool):
        super().__init__(title="Кастомная роль" if custom else "Цветная роль")
        self.custom = custom
        if custom:
            self.role_name = discord.ui.TextInput(label="Название роли", max_length=80)
            self.add_item(self.role_name)
        self.primary = discord.ui.TextInput(label="Основной HEX", placeholder="#FFD700", max_length=7)
        self.add_item(self.primary)
        if custom:
            self.secondary = discord.ui.TextInput(label="Второй HEX для градиента", required=False, max_length=7)
            self.tertiary = discord.ui.TextInput(label="Третий HEX для голографии", required=False, max_length=7)
            self.add_item(self.secondary)
            self.add_item(self.tertiary)

    async def on_submit(self, interaction: discord.Interaction):
        await _create_cosmetic_role(
            interaction,
            name=str(self.role_name) if self.custom else interaction.user.display_name,
            primary=str(self.primary),
            secondary=str(self.secondary) if self.custom else None,
            tertiary=str(self.tertiary) if self.custom else None,
            price=SHOP_PRICES["custom_role" if self.custom else "color_role"],
            kind="custom" if self.custom else "color",
        )


class VipSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Выберите срок VIP",
            options=[
                discord.SelectOption(label="VIP на неделю", description=_price(SHOP_PRICES["vip_week"]), value="week"),
                discord.SelectOption(label="VIP на месяц", description=_price(SHOP_PRICES["vip_month"]), value="month"),
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        async with _SHOP_USER_LOCKS.lock(interaction.user.id):
            await self._purchase(interaction)

    async def _purchase(self, interaction: discord.Interaction):
        period = self.values[0]
        price = SHOP_PRICES["vip_week" if period == "week" else "vip_month"]
        days = 7 if period == "week" else 30
        intent = await create_shop_purchase_intent(
            interaction_id=interaction.id,
            guild_id=interaction.guild.id,
            user_id=interaction.user.id,
            purchase_kind="vip",
            role_name=ROLES["vip"],
            price=price,
            vip_days=days,
        )
        if intent is None:
            await followup_v2(interaction, "Недостаточно Guinn Coins.", ephemeral=True)
            return
        try:
            cog = interaction.client.get_cog("ShopCog")
            if isinstance(cog, ShopCog):
                await cog.fulfill_purchase_intent(intent.id)
        except Exception as exc:
            logger.exception("VIP purchase %s queued for retry", intent.id)
            await set_shop_purchase_error(intent.id, exc)
        current = await get_shop_purchase_intent(intent.id)
        if current is not None and current.state == "refunded":
            await followup_v2(
                interaction,
                "Discord не позволил активировать VIP. Средства автоматически возвращены.",
                ephemeral=True,
            )
            return
        if current is None or current.state != "completed":
            await followup_v2(
                interaction,
                "Покупка VIP сохранена и будет завершена автоматически после восстановления связи с Discord.",
                ephemeral=True,
            )
            return
        until = datetime.fromisoformat(current.vip_until)
        await followup_v2(interaction,
            f"VIP активирован до <t:{int(until.timestamp())}:F>.", ephemeral=True
        )


class VipSelectView(SafeView):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(VipSelect())


class EmojiSelect(discord.ui.Select):
    def __init__(self):
        super().__init__(
            placeholder="Выберите VIP-эмодзи",
            options=[discord.SelectOption(label=f"Статус {emoji}", emoji=emoji, value=emoji) for emoji in ("⭐", "💎", "🔥", "👑", "⚡")],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        price = SHOP_PRICES["summit_emoji"]
        async with _SHOP_USER_LOCKS.lock(interaction.user.id):
            result = await purchase_summit_emoji(
                interaction.user.id, self.values[0], price
            )
        if result == "no_vip":
            await followup_v2(interaction,
                "Для VIP-эмодзи нужен активный VIP.", ephemeral=True
            )
            return
        if result == "insufficient":
            await followup_v2(interaction, "Недостаточно Guinn Coins.", ephemeral=True)
            return
        await _refresh_user_summits(interaction.guild, interaction.user.id)
        await followup_v2(interaction, f"VIP-эмодзи {self.values[0]} выбран.", ephemeral=True)


class EmojiSelectView(SafeView):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(EmojiSelect())


class SellRoleSelect(discord.ui.Select):
    def __init__(self, roles):
        super().__init__(
            placeholder="Выберите роль для продажи",
            options=[
                discord.SelectOption(label=name[:100], description=f"Возврат: {_price(price // 2)}"[:100], value=str(purchase_id))
                for purchase_id, _, name, price, _ in roles[:25]
            ],
        )

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        purchase_id = int(self.values[0])
        async with _SHOP_USER_LOCKS.lock(interaction.user.id):
            sale = await claim_role_sale(
                purchase_id,
                interaction.user.id,
                guild_id=interaction.guild.id,
                source="user_sale",
            )
            if sale is None:
                await followup_v2(interaction,
                    "Эта роль уже недоступна.", ephemeral=True
                )
                return
            try:
                cog = interaction.client.get_cog("ShopCog")
                if not isinstance(cog, ShopCog):
                    raise RuntimeError("Shop service is unavailable")
                async with _SHOP_INTENT_LOCKS.lock(-purchase_id):
                    sold = await cog._process_role_sale(interaction.guild, sale)
            except (discord.HTTPException, RuntimeError):
                logger.exception("Purchased role sale %s queued for retry", purchase_id)
                await followup_v2(interaction,
                    "Продажа сохранена и завершится автоматически после восстановления связи с Discord.",
                    ephemeral=True,
                )
                return
        await followup_v2(interaction,
            f"Роль продана. Возвращено {_price(sold.refund)}.", ephemeral=True
        )


class SellRoleView(SafeView):
    def __init__(self, roles):
        super().__init__(timeout=180)
        self.add_item(SellRoleSelect(roles))


class ShopView(SafeView):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Цветная роль", style=discord.ButtonStyle.primary, custom_id="shop:color_role")
    async def color_role(self, interaction, button):
        await interaction.response.send_modal(RolePurchaseModal(False))

    @discord.ui.button(label="Кастомная роль", style=discord.ButtonStyle.primary, custom_id="shop:custom_role")
    async def custom_role(self, interaction, button):
        await interaction.response.send_modal(RolePurchaseModal(True))

    @discord.ui.button(label="Купить VIP", style=discord.ButtonStyle.success, custom_id="shop:vip")
    async def vip(self, interaction, button):
        await respond_v2(interaction, "Выберите срок:", view=VipSelectView(), ephemeral=True)

    @discord.ui.button(label="VIP-эмодзи", style=discord.ButtonStyle.secondary, custom_id="shop:emoji")
    async def emoji(self, interaction, button):
        await respond_v2(interaction, "Выберите эмодзи:", view=EmojiSelectView(), ephemeral=True)

    @discord.ui.button(label="Продать роль", style=discord.ButtonStyle.danger, custom_id="shop:sell_role")
    async def sell(self, interaction, button):
        await interaction.response.defer(ephemeral=True)
        roles = await get_user_purchased_roles(interaction.user.id)
        if not roles:
            await followup_v2(interaction, "У вас нет купленных ролей.", ephemeral=True)
            return
        await followup_v2(interaction, "Выберите роль:", view=SellRoleView(roles), ephemeral=True)


class ShopCog(
    commands.GroupCog,
    group_name="coins",
    group_description="Guinn Coins и купленные роли",
):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._registered_role_requests: dict[int, RoleApprovalView] = {}
        self._restored_role_order_guilds: set[int] = set()
        self._role_order_restore_lock = asyncio.Lock()

    @staticmethod
    async def _role_snapshot(
        guild: discord.Guild,
        snapshots: dict[int, dict[int, discord.Role]],
    ) -> dict[int, discord.Role]:
        snapshot = snapshots.get(guild.id)
        if snapshot is None:
            snapshot = {role.id: role for role in await guild.fetch_roles()}
            snapshots[guild.id] = snapshot
        return snapshot

    async def _find_marker_message(
        self, channel: discord.TextChannel, marker: str, *, limit: int = 1_000
    ) -> discord.Message | None:
        return await _find_marked_message(
            channel,
            marker,
            bot_user_id=self.bot.user.id if self.bot.user else None,
            limit=limit,
        )

    def _register_role_request_view(
        self, request_id: int, message_id: int
    ) -> RoleApprovalView:
        existing = self._registered_role_requests.get(message_id)
        if existing is not None:
            return existing
        view = RoleApprovalView(request_id)
        self.bot.add_view(view, message_id=message_id)
        self._registered_role_requests[message_id] = view
        return view

    async def publish_role_request(
        self, request_id: int, guild: discord.Guild
    ) -> int:
        """Adopt or recreate an approval panel identified by its V2 marker."""

        async with _SHOP_REQUEST_LOCKS.lock(request_id):
            request = await get_role_purchase_publication(request_id)
            if request is None:
                raise RuntimeError(f"Role purchase request {request_id} is missing")
            if request[7] != "pending":
                return int(request[8] or 0)
            channel = (
                guild.get_channel(int(request[10]))
                if request[10] is not None
                else None
            )
            if not isinstance(channel, discord.TextChannel):
                channel = get_text_channel(guild, "role_approval")
            if not isinstance(channel, discord.TextChannel):
                raise RuntimeError("Role approval channel is unavailable")

            message = None
            if request[8] is not None:
                try:
                    message = await channel.fetch_message(int(request[8]))
                except discord.NotFound:
                    message = None
            marker = f"role-purchase:{request_id}"
            if message is None:
                message = await self._find_marker_message(channel, marker)
            view = RoleApprovalView(request_id)
            if message is None:
                embed = _role_approval_embed(
                    request_id,
                    int(request[1]),
                    str(request[2]),
                    int(request[3]),
                    int(request[4]) if request[4] is not None else None,
                    int(request[5]) if request[5] is not None else None,
                    int(request[6]),
                )
                message = await send_v2(
                    channel,
                    content=request_ping_content(guild),
                    embed=embed,
                    view=view,
                    component_marker=marker,
                    allowed_mentions=request_ping_mentions(guild),
                )
            else:
                expected_marker = marker_component_id(marker)

                def visit(component) -> bool:
                    return getattr(component, "id", None) == expected_marker or any(
                        visit(child) for child in getattr(component, "children", ())
                    )

                has_marker = any(visit(item) for item in message.components)
                if not message.flags.components_v2 or not has_marker:
                    embed = _role_approval_embed(
                        request_id,
                        int(request[1]),
                        str(request[2]),
                        int(request[3]),
                        int(request[4]) if request[4] is not None else None,
                        int(request[5]) if request[5] is not None else None,
                        int(request[6]),
                    )
                    await edit_message_v2(
                        message,
                        content=message.content or request_ping_content(guild),
                        embed=embed,
                        view=view,
                        component_marker=marker,
                    )
            old_message_id = int(request[8]) if request[8] is not None else None
            if old_message_id is not None and old_message_id != message.id:
                old_view = self._registered_role_requests.pop(old_message_id, None)
                if old_view is not None:
                    stop_registered_view(old_view)
            await set_role_purchase_request_message(
                request_id,
                message.id,
                guild_id=guild.id,
                channel_id=channel.id,
            )
            self._register_role_request_view(request_id, message.id)
            return message.id

    async def _publish_role_request_outbox(self, operation) -> int:
        request_id = int(operation.payload["request_id"])
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
        try:
            return await self.publish_role_request(request_id, guild)
        except Exception as exc:
            await set_role_purchase_publication_error(request_id, exc)
            raise

    async def _adopt_intent_role(
        self, guild: discord.Guild, intent: ShopPurchaseIntent
    ) -> discord.Role | None:
        if intent.discord_role_id is not None:
            role = guild.get_role(intent.discord_role_id)
            if role is not None:
                return role
        if intent.purchase_kind == "vip":
            return discord.utils.get(guild.roles, name=intent.role_name) or _SHOP_VIP_ROLES.get(
                guild.id
            )
        if intent.state == "pending":
            return None

        marker = f"shop-purchase:{intent.id}"
        try:
            created_after = discord.Object(
                id=discord.utils.time_snowflake(
                    datetime.fromisoformat(intent.created_at) - timedelta(seconds=5)
                )
            )
            async for entry in guild.audit_logs(
                limit=500,
                action=discord.AuditLogAction.role_create,
                after=created_after,
            ):
                if entry.reason != marker or entry.target is None:
                    continue
                role_id = getattr(entry.target, "id", None)
                if role_id is not None:
                    return guild.get_role(role_id) or entry.target
        except discord.Forbidden:
            logger.warning(
                "Cannot inspect audit log while reconciling shop intent %s",
                intent.id,
            )

        created_at = datetime.fromisoformat(intent.created_at)
        candidates = [
            role
            for role in guild.roles
            if role.name == intent.role_name
            and role.created_at >= created_at - timedelta(seconds=5)
        ]
        if len(candidates) == 1:
            return candidates[0]
        return None

    async def _find_role_request_orphan(
        self,
        guild: discord.Guild,
        request_id: int,
        reviewed_at: str | None,
        role_snapshots: dict[int, dict[int, discord.Role]],
    ) -> discord.Role | None:
        """Prove whether a crashed custom-role approval created a live role."""

        # A REST role snapshot avoids making a duplicate while the Gateway
        # cache is delayed.  Audit reason is the idempotency marker; matching
        # by user-controlled role name would be unsafe.
        live_roles = await self._role_snapshot(guild, role_snapshots)
        after = None
        if reviewed_at:
            after = discord.Object(
                id=discord.utils.time_snowflake(
                    datetime.fromisoformat(reviewed_at) - timedelta(seconds=30)
                )
            )
        marker = f"role-request:{request_id}"
        async for entry in guild.audit_logs(
            limit=500,
            action=discord.AuditLogAction.role_create,
            after=after,
        ):
            if not str(entry.reason or "").startswith(marker):
                continue
            role_id = getattr(entry.target, "id", None)
            return live_roles.get(role_id)
        return None

    async def _recover_role_approval_claims(
        self,
        role_snapshots: dict[int, dict[int, discord.Role]],
    ) -> int:
        failures = 0
        for claim in await get_recoverable_role_approval_claims():
            guild = self.bot.get_guild(claim.guild_id)
            if guild is None:
                failures += 1
                continue
            try:
                role = (
                    guild.get_role(claim.discord_role_id)
                    if claim.discord_role_id is not None
                    else await self._find_role_request_orphan(
                        guild,
                        claim.request_id,
                        claim.reviewed_at,
                        role_snapshots,
                    )
                )
                if role is not None:
                    persisted = await set_role_purchase_discord_role(
                        claim.request_id,
                        claim.reviewer_id,
                        role.id,
                        guild_id=guild.id,
                    )
                    if not persisted:
                        raise RuntimeError(
                            f"Role approval claim {claim.request_id} changed during recovery"
                        )
                elif claim.discord_role_id is None:
                    # Audit log plus the fresh REST role list prove that the
                    # previous attempt never left a role behind.
                    await release_role_approval_claim(
                        claim.request_id, claim.reviewer_id
                    )
                else:
                    # The role ID was durably recorded but the external role
                    # disappeared before delivery. Let the outbox refund it.
                    await set_role_purchase_discord_role(
                        claim.request_id,
                        claim.reviewer_id,
                        claim.discord_role_id,
                        guild_id=guild.id,
                    )
            except asyncio.CancelledError:
                raise
            except (discord.Forbidden, discord.HTTPException, RuntimeError):
                failures += 1
                logger.exception(
                    "Failed to recover custom role approval %s",
                    claim.request_id,
                )
        return failures

    async def _apply_vip_voice_access(
        self, guild: discord.Guild, member: discord.Member
    ) -> None:
        room = await get_voice_room_by_owner(member.id)
        voice_channel = guild.get_channel(room[0]) if room else None
        if not room or not isinstance(voice_channel, discord.VoiceChannel):
            return
        try:
            banned_ids = await get_voice_owner_bans(member.id)
            for banned_id in banned_ids:
                banned_member = guild.get_member(banned_id)
                if banned_member is not None:
                    await voice_channel.set_permissions(
                        banned_member, view_channel=False, connect=False
                    )
            if room[2]:
                from cogs.voice_rooms import room_embed

                control = await voice_channel.fetch_message(room[2])
                await edit_message_v2(
                    control,
                    embed=room_embed(member.id, banned_ids, vip_access=True),
                )
        except discord.HTTPException:
            logger.exception("Failed to restore VIP voice bans for %s", member.id)

    async def fulfill_purchase_intent(
        self,
        intent_id: int,
        *,
        role_snapshots: dict[int, dict[int, discord.Role]] | None = None,
    ) -> int | None:
        """Idempotently bring one paid intent to its Discord + DB final state."""

        async with _SHOP_INTENT_LOCKS.lock(intent_id):
            intent = await get_shop_purchase_intent(intent_id)
            if intent is None:
                raise RuntimeError(f"Shop purchase intent {intent_id} is missing")
            if intent.state == "completed":
                return intent.discord_role_id
            if intent.state == "refunded":
                return None
            guild = self.bot.get_guild(intent.guild_id)
            if guild is None:
                raise RuntimeError(f"Guild {intent.guild_id} is unavailable")
            role = await self._adopt_intent_role(guild, intent)
            if role is None and intent.state == "provisioning":
                started = datetime.fromisoformat(intent.updated_at)
                if datetime.now(UTC) - started < timedelta(seconds=30):
                    raise RuntimeError("Waiting for Discord role marker propagation")
            member = guild.get_member(intent.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(intent.user_id)
                except discord.NotFound:
                    if intent.purchase_kind == "color" and role is not None:
                        holders = await count_active_role_holders(role.id)
                        if holders == 0 and not _role_used_by_other_member(
                            guild,
                            role,
                            intent.user_id,
                        ):
                            try:
                                await role.delete(
                                    reason=f"shop-purchase:{intent.id}:member-left"
                                )
                            except discord.NotFound:
                                pass
                            except discord.HTTPException as exc:
                                await set_shop_purchase_error(intent.id, exc)
                                raise
                    await refund_shop_purchase(intent.id, "shop_member_missing_refund")
                    return None

            role_added = False
            role_created = False
            try:
                if role is None and intent.state == "pending":
                    if not await mark_shop_intent_provisioning(intent.id):
                        raise RuntimeError("Shop intent provisioning was claimed")
                if intent.purchase_kind == "vip":
                    async with _SHOP_GUILD_LOCKS.lock(guild.id):
                        role = await self._adopt_intent_role(guild, intent)
                        if role is None:
                            role = await guild.create_role(
                                name=intent.role_name,
                                colour=0xF1C40F,
                                reason=f"shop-purchase:{intent.id}",
                            )
                            _SHOP_VIP_ROLES[guild.id] = role
                elif role is None:
                    async with _SHOP_GUILD_LOCKS.lock(guild.id):
                        role = await self._adopt_intent_role(guild, intent)
                        if role is None and intent.purchase_kind == "color":
                            reusable_ids = await get_reusable_color_role_ids(
                                guild.id,
                                intent.primary_color or 0,
                            )
                            snapshots = role_snapshots if role_snapshots is not None else {}
                            live_roles = await self._role_snapshot(guild, snapshots)
                            role = next(
                                (
                                    live_roles[role_id]
                                    for role_id in reusable_ids
                                    if role_id in live_roles
                                    and live_roles[role_id].name == intent.role_name
                                    and live_roles[role_id].colour.value
                                    == (intent.primary_color or 0)
                                ),
                                None,
                            )
                        if role is None:
                            kwargs = {}
                            features = {
                                feature.casefold() for feature in guild.features
                            }
                            if "enhanced_role_colors" in features:
                                kwargs["secondary_color"] = (
                                    discord.Colour(intent.secondary_color)
                                    if intent.secondary_color is not None
                                    else None
                                )
                                kwargs["tertiary_color"] = (
                                    discord.Colour(intent.tertiary_color)
                                    if intent.tertiary_color is not None
                                    else None
                                )
                            role = await guild.create_role(
                                name=intent.role_name,
                                colour=discord.Colour(intent.primary_color or 0),
                                reason=f"shop-purchase:{intent.id}",
                                **kwargs,
                            )
                            role_created = True
                            if role_snapshots is not None:
                                role_snapshots.setdefault(guild.id, {})[role.id] = role
                if role is None:
                    raise RuntimeError("Discord role provisioning returned no role")
                if intent.discord_role_id != role.id:
                    persisted = await set_shop_intent_discord_role(intent.id, role.id)
                    if not persisted:
                        refreshed = await get_shop_purchase_intent(intent.id)
                        if refreshed is None or refreshed.state not in {
                            "discord_created",
                            "completed",
                        }:
                            raise RuntimeError("Shop intent state changed during provisioning")
                if role not in member.roles:
                    await member.add_roles(
                        role,
                        reason=f"shop-purchase:{intent.id}",
                    )
                    role_added = True
                if intent.purchase_kind == "vip":
                    await self._apply_vip_voice_access(guild, member)
                    await _refresh_user_summits(guild, member.id)
                completed = await complete_shop_purchase(intent.id, role.id)
                if not completed:
                    raise RuntimeError("Shop intent could not be finalized")
            except (discord.Forbidden, discord.NotFound) as exc:
                cleanup_confirmed = role is None
                try:
                    if (
                        intent.purchase_kind == "color"
                        and role is not None
                        and role_created
                        and await count_active_role_holders(role.id) == 0
                    ):
                        await role.delete(reason=f"shop-purchase:{intent.id}:rollback")
                        cleanup_confirmed = True
                    elif intent.purchase_kind == "vip":
                        if role is not None and role_added:
                            await member.remove_roles(
                                role,
                                reason=f"shop-purchase:{intent.id}:rollback",
                            )
                        cleanup_confirmed = True
                except discord.NotFound:
                    cleanup_confirmed = True
                except discord.HTTPException:
                    logger.exception("Shop intent %s cleanup failed", intent.id)
                if cleanup_confirmed:
                    await refund_shop_purchase(intent.id, "shop_discord_refund")
                    return None
                await set_shop_purchase_error(intent.id, exc)
                raise

            if intent.purchase_kind == "color":
                try:
                    await _order_cosmetic_roles(guild, intent.user_id)
                except discord.HTTPException:
                    logger.exception("Failed to order role for intent %s", intent.id)
            return role.id

    async def _fulfill_purchase_outbox(self, operation) -> int | None:
        return await self.fulfill_purchase_intent(int(operation.payload["intent_id"]))

    async def _process_role_sale(
        self,
        guild: discord.Guild,
        sale: RoleSale,
    ) -> RoleSale:
        """Delete the external role and finish the already-reserved refund."""

        current = await get_role_sale(sale.purchase_id)
        if current is None:
            raise RuntimeError(f"Purchased role {sale.purchase_id} is missing")
        if current.active == 0:
            return current
        if current.active != 2 or current.user_id != sale.user_id:
            raise RuntimeError(f"Purchased role {sale.purchase_id} is not reserved")

        role = guild.get_role(current.role_id)
        if role is None:
            # A fresh REST snapshot distinguishes a deleted role from a delayed
            # Gateway cache and prevents an early refund while the role lives.
            role = discord.utils.get(await guild.fetch_roles(), id=current.role_id)
        remaining_holders = await count_active_role_holders(
            current.role_id,
            exclude_purchase_id=current.purchase_id,
        )
        if role is not None:
            try:
                if remaining_holders or _role_used_by_other_member(
                    guild,
                    role,
                    current.user_id,
                ):
                    member = guild.get_member(current.user_id)
                    if member is None:
                        try:
                            member = await guild.fetch_member(current.user_id)
                        except discord.NotFound:
                            member = None
                    if member is not None and role in member.roles:
                        await member.remove_roles(
                            role,
                            reason=f"shop-role-sale:{sale.purchase_id}:shared",
                        )
                else:
                    await role.delete(reason=f"shop-role-sale:{sale.purchase_id}")
            except discord.NotFound:
                pass

        completed = await complete_role_sale(sale.purchase_id, sale.user_id)
        if completed is not None:
            return completed
        current = await get_role_sale(sale.purchase_id)
        if current is not None and current.active == 0:
            return current
        raise RuntimeError(f"Purchased role sale {sale.purchase_id} was not finalized")

    async def _reconcile_role_sale(self, operation) -> int:
        data = operation.payload
        purchase_id = int(data["purchase_id"])
        async with _SHOP_INTENT_LOCKS.lock(-purchase_id):
            sale = await get_role_sale(purchase_id)
            if sale is None:
                raise RuntimeError(f"Purchased role {purchase_id} is missing")
            guild = self.bot.get_guild(operation.guild_id)
            if guild is None:
                raise RuntimeError(f"Guild {operation.guild_id} is unavailable")
            completed = await self._process_role_sale(guild, sale)

            member = guild.get_member(completed.user_id)
            if member is None:
                try:
                    member = await guild.fetch_member(completed.user_id)
                except discord.NotFound:
                    member = None
            source = str(data.get("source", "user_sale"))
            if member is not None and source != "vip_expired":
                marker = f"shop-role-sale-dm:{completed.purchase_id}"
                try:
                    dm = member.dm_channel or await member.create_dm()
                    existing = await _find_marked_message(
                        dm,
                        marker,
                        bot_user_id=self.bot.user.id if self.bot.user else None,
                    )
                    if existing is None:
                        live_role = guild.get_role(completed.role_id)
                        shared = (
                            await count_active_role_holders(completed.role_id)
                        ) > 0 or (
                            live_role is not None
                            and _role_used_by_other_member(
                                guild,
                                live_role,
                                completed.user_id,
                            )
                        )
                        await send_v2(
                            dm,
                            f"Продажа роли завершена. Роль **{completed.role_name}** "
                            f"{'снята с вас' if shared else 'удалена'}, "
                            f"возвращено {_price(completed.refund)} (50%).",
                            component_marker=marker,
                        )
                except (discord.Forbidden, discord.NotFound):
                    # Closed DMs must not block an already completed refund.
                    pass
                except discord.HTTPException:
                    raise
            return completed.role_id

    async def _guild_for_legacy_role_sale(
        self, sale: RoleSale
    ) -> discord.Guild | None:
        for guild in self.bot.guilds:
            if guild.get_role(sale.role_id) is not None:
                return guild
        if len(self.bot.guilds) == 1:
            return self.bot.guilds[0]
        for guild in self.bot.guilds:
            try:
                if discord.utils.get(await guild.fetch_roles(), id=sale.role_id):
                    return guild
            except discord.HTTPException:
                # In a multi-guild legacy recovery we must prove the role is
                # absent everywhere before a refund can be queued.
                return None
        return next(
            (guild for guild in self.bot.guilds if guild.get_member(sale.user_id)),
            None,
        )

    async def _present_role_rejection(
        self,
        guild: discord.Guild,
        rejection: RoleRejectionPresentation,
    ) -> int:
        channel = (
            guild.get_channel(rejection.channel_id)
            if rejection.channel_id is not None
            else None
        )
        if not isinstance(channel, discord.TextChannel):
            channel = get_text_channel(guild, "role_approval")
        if not isinstance(channel, discord.TextChannel):
            raise RuntimeError("Role approval channel is unavailable")

        marker = f"role-purchase:{rejection.request_id}"
        message = None
        if rejection.message_id is not None:
            try:
                message = await channel.fetch_message(rejection.message_id)
            except discord.NotFound:
                message = None
        if message is None:
            message = await self._find_marker_message(channel, marker)

        status = f"🔴 Отклонено\nПричина: {rejection.reason}"
        if rejection.refund:
            status += f"\nВозврат: {_price(rejection.refund)}"
        embed = _role_approval_embed(
            rejection.request_id,
            rejection.user_id,
            rejection.role_name,
            rejection.primary_color,
            rejection.secondary_color,
            rejection.tertiary_color,
            rejection.price,
            status=status,
            reviewer_id=rejection.reviewer_id,
        )
        if message is None:
            spec = apply_accent(embed, Accent.ERROR)
            message = await send_v2(
                channel,
                embed=spec,
                view=None,
                component_marker=marker,
            )
            await set_role_purchase_request_message(
                rejection.request_id,
                message.id,
                guild_id=guild.id,
                channel_id=channel.id,
            )
        else:
            await _edit_role_decision(message, embed, approved=False)

        member = guild.get_member(rejection.user_id)
        if member is None:
            try:
                member = await guild.fetch_member(rejection.user_id)
            except discord.NotFound:
                member = None
        if member is not None:
            dm_marker = f"shop-role-dm:{rejection.request_id}:rejected"
            try:
                dm = member.dm_channel or await member.create_dm()
                if await _find_marked_message(
                    dm,
                    dm_marker,
                    bot_user_id=self.bot.user.id if self.bot.user else None,
                ) is None:
                    refund_text = (
                        f" Возвращено {_price(rejection.refund)}."
                        if rejection.refund
                        else ""
                    )
                    await send_v2(
                        dm,
                        f"Заявка на кастомную роль **{rejection.role_name}** отклонена. "
                        f"Причина: {rejection.reason}.{refund_text}",
                        component_marker=dm_marker,
                    )
            except (discord.Forbidden, discord.NotFound):
                pass
            except discord.HTTPException:
                raise

        logged = await _log_role_action(
            guild,
            f"❌ Кастомная роль #{rejection.request_id:04d} для "
            f"<@{rejection.user_id}> отклонена <@{rejection.reviewer_id}>. "
            f"Причина: {rejection.reason}. Возврат: {_price(rejection.refund)}.",
            outcome=Outcome.REJECTED,
            event_key=f"custom-role:{rejection.request_id}:rejected",
        )
        if not logged:
            raise RuntimeError("Role rejection audit is unavailable")
        await mark_role_rejection_presented(rejection.request_id)
        return message.id

    async def _reconcile_role_rejection(self, operation) -> int:
        request_id = int(operation.payload["request_id"])
        async with _SHOP_REQUEST_LOCKS.lock(request_id):
            rejection = await get_role_rejection_presentation(request_id)
            if rejection is None:
                raise RuntimeError(f"Rejected role request {request_id} is missing")
            guild = self.bot.get_guild(rejection.guild_id)
            if guild is None:
                raise RuntimeError(f"Guild {rejection.guild_id} is unavailable")
            if rejection.presentation_state == "completed":
                return int(rejection.message_id or 0)
            return await self._present_role_rejection(guild, rejection)

    async def reconcile_shop_purchases(self) -> None:
        role_snapshots: dict[int, dict[int, discord.Role]] = {}
        failures = await self._recover_role_approval_claims(role_snapshots)
        default_guild = self.bot.guilds[0] if len(self.bot.guilds) == 1 else None
        pending_requests = await get_pending_role_purchase_requests()
        active_message_ids = {
            int(message_id)
            for _request_id, message_id in pending_requests
            if message_id is not None
        }
        for message_id in set(self._registered_role_requests) - active_message_ids:
            view = self._registered_role_requests.pop(message_id)
            stop_registered_view(view)
        for request_id, _message_id in pending_requests:
            request = await get_role_purchase_publication(int(request_id))
            guild = (
                self.bot.get_guild(int(request[9]))
                if request is not None and request[9] is not None
                else default_guild
            )
            if guild is None:
                failures += 1
                continue
            await ensure_role_purchase_publication_outbox(int(request_id), guild.id)
            try:
                await self.publish_role_request(int(request_id), guild)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.exception("Failed to reconcile role request %s", request_id)
        for intent in await get_unfinished_shop_purchase_intents():
            await ensure_shop_purchase_outbox(intent)
            try:
                await self.fulfill_purchase_intent(
                    intent.id,
                    role_snapshots=role_snapshots,
                )
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.exception("Failed to reconcile shop intent %s", intent.id)
        for sale in await get_reserved_role_sales():
            guild = await self._guild_for_legacy_role_sale(sale)
            if guild is None:
                failures += 1
                continue
            await ensure_role_sale_outbox(
                sale,
                guild_id=guild.id,
                source="user_sale",
            )
        for rejection in await get_pending_role_rejection_presentations():
            await ensure_role_rejection_outbox(rejection)
            guild = self.bot.get_guild(rejection.guild_id)
            if guild is None:
                failures += 1
                continue
            try:
                async with _SHOP_REQUEST_LOCKS.lock(rejection.request_id):
                    await self._present_role_rejection(guild, rejection)
            except asyncio.CancelledError:
                raise
            except Exception:
                failures += 1
                logger.exception(
                    "Failed to reconcile rejected role request %s",
                    rejection.request_id,
                )
        if failures:
            raise RuntimeError(f"Could not reconcile {failures} shop operation(s)")

    async def cog_load(self):
        self.bot.add_view(ShopView())
        for request_id, message_id in await get_pending_role_purchase_requests():
            if message_id is not None:
                self._register_role_request_view(int(request_id), int(message_id))
        operation_worker = getattr(self.bot, "operation_worker", None)
        if operation_worker is not None:
            operation_worker.register("role.approve", self._reconcile_role_approval)
            operation_worker.register("role.reject.present", self._reconcile_role_rejection)
            operation_worker.register("role.request.publish", self._publish_role_request_outbox)
            operation_worker.register("shop.role.sale", self._reconcile_role_sale)
            operation_worker.register("shop.fulfill", self._fulfill_purchase_outbox)
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            supervisor.start_periodic(
                "shop-vip-expiration",
                self.vip_expiration,
                interval=600,
                timeout=300,
            )
            supervisor.start_periodic(
                "shop-purchase-reconcile",
                self.reconcile_shop_purchases,
                interval=60,
                timeout=300,
            )

    async def cog_unload(self):
        supervisor = getattr(self.bot, "task_supervisor", None)
        if supervisor is not None:
            await supervisor.stop_task("shop-vip-expiration")
            await supervisor.stop_task("shop-purchase-reconcile")
        operation_worker = getattr(self.bot, "operation_worker", None)
        if operation_worker is not None:
            operation_worker.unregister("role.approve")
            operation_worker.unregister("role.reject.present")
            operation_worker.unregister("role.request.publish")
            operation_worker.unregister("shop.role.sale")
            operation_worker.unregister("shop.fulfill")

    async def _reconcile_role_approval(self, operation) -> int:
        request_id = int(operation.payload["request_id"])
        async with _SHOP_REQUEST_LOCKS.lock(request_id):
            return await self._reconcile_role_approval_unlocked(operation)

    async def _reconcile_role_approval_unlocked(self, operation) -> int:
        data = operation.payload
        request_id = int(data["request_id"])
        reviewer_id = int(data["reviewer_id"])
        user_id = int(data["user_id"])
        role_id = int(data["role_id"])
        request = await get_role_purchase_request(request_id)
        if request is None:
            raise RuntimeError("Role request no longer exists")
        if request[7] == "rejected":
            return role_id
        guild = self.bot.get_guild(operation.guild_id)
        if guild is None:
            raise RuntimeError("Role request guild is unavailable")
        role = guild.get_role(role_id)
        member = guild.get_member(user_id)
        if role is None:
            role = discord.utils.get(await guild.fetch_roles(), id=role_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                member = None
        if role is None or member is None:
            if request[7] != "processing":
                # A completed request can outlive a manually removed role or a
                # departed member; there is no unfinished financial operation.
                return role_id
            if role is not None:
                try:
                    await role.delete(
                        reason=f"role-request:{request_id}:owner-missing"
                    )
                except discord.NotFound:
                    pass
                except discord.HTTPException:
                    # Never refund while an orphan role may still exist.
                    raise
            refunded = await reject_role_purchase_with_refund(
                request_id,
                reviewer_id,
                int(data["price"]),
                "role_recovery_refund",
                reason="Выдачу роли не удалось восстановить",
            )
            if not refunded:
                raise RuntimeError("Missing role approval could not be refunded")
            approval_channel = get_text_channel(guild, "role_approval")
            if approval_channel is not None and request[8]:
                try:
                    message = await approval_channel.fetch_message(int(request[8]))
                    await _edit_role_decision(
                        message,
                        _role_approval_embed(
                            request_id,
                            user_id,
                            request[2],
                            request[3],
                            request[4],
                            request[5],
                            request[6],
                            status="🔴 Отклонено: выдачу не удалось восстановить",
                            reviewer_id=reviewer_id,
                        ),
                        approved=False,
                    )
                except (discord.NotFound, discord.Forbidden):
                    pass
            if member is not None:
                try:
                    dm = member.dm_channel or await member.create_dm()
                    dm_marker = f"shop-role-dm:{request_id}:rejected"
                    if await _find_marked_message(
                        dm,
                        dm_marker,
                        bot_user_id=self.bot.user.id if self.bot.user else None,
                    ) is None:
                        await send_v2(
                            dm,
                            f"Выдачу вашей кастомной роли **{request[2]}** "
                            f"не удалось восстановить. Стоимость {_price(int(data['price']))} возвращена.",
                            component_marker=dm_marker,
                        )
                except (discord.Forbidden, discord.NotFound):
                    pass
                except discord.HTTPException:
                    raise
            logged = await _log_role_action(
                guild,
                f"❌ Кастомная роль #{request_id:04d} для <@{user_id}> "
                f"отклонена при восстановлении; стоимость возвращена.",
                outcome=Outcome.REJECTED,
                event_key=f"custom-role:{request_id}:rejected",
            )
            if not logged:
                raise RuntimeError("Recovered role rejection audit is unavailable")
            return role_id
        if role not in member.roles:
            await member.add_roles(
                role,
                reason=f"Recover role-request:{request_id}",
            )
        if request[7] == "processing":
            completed = await complete_role_purchase_approval(
                request_id,
                reviewer_id,
                user_id,
                role_id,
                str(data["role_name"]),
                int(data["price"]),
            )
            if not completed:
                raise RuntimeError("Role approval state changed during recovery")
        approval_channel = get_text_channel(guild, "role_approval")
        if approval_channel is not None and request[8]:
            try:
                message = await approval_channel.fetch_message(int(request[8]))
                await _edit_role_decision(
                    message,
                    _role_approval_embed(
                        request_id,
                        user_id,
                        request[2],
                        request[3],
                        request[4],
                        request[5],
                        request[6],
                        status="🟢 Одобрено и выдано",
                        reviewer_id=reviewer_id,
                    ),
                    approved=True,
                )
            except (discord.NotFound, discord.Forbidden):
                pass
        await _order_cosmetic_roles(guild, user_id)
        try:
            dm = member.dm_channel or await member.create_dm()
            dm_marker = f"shop-role-dm:{request_id}:approved"
            if await _find_marked_message(
                dm,
                dm_marker,
                bot_user_id=self.bot.user.id if self.bot.user else None,
            ) is None:
                await send_v2(
                    dm,
                    f"Ваша кастомная роль **{request[2]}** одобрена и выдана.",
                    component_marker=dm_marker,
                )
        except (discord.Forbidden, discord.NotFound):
            pass
        except discord.HTTPException:
            raise
        logged = await _log_role_action(
            guild,
            f"✅ Кастомная роль #{request_id:04d}: <@{user_id}> получил "
            f"**{request[2]}**; одобрил <@{reviewer_id}>.",
            outcome=Outcome.APPROVED,
            event_key=f"custom-role:{request_id}:approved",
        )
        if not logged:
            raise RuntimeError("Recovered role approval audit is unavailable")
        return role_id

    @commands.Cog.listener()
    async def on_ready(self):
        async with self._role_order_restore_lock:
            owners = await get_active_purchased_role_owners()
            for guild in self.bot.guilds:
                if guild.id in self._restored_role_order_guilds:
                    continue
                semaphore = asyncio.Semaphore(5)

                async def restore(
                    user_id: int,
                    guild: discord.Guild = guild,
                    semaphore: asyncio.Semaphore = semaphore,
                ) -> None:
                    # Both are loop variables. The gather below finishes before
                    # the next iteration today, so late binding happens to work
                    # — binding them makes that guarantee explicit instead of
                    # accidental.
                    async with semaphore:
                        try:
                            await _order_cosmetic_roles(guild, user_id)
                        except discord.HTTPException:
                            logger.exception(
                                "Failed to restore cosmetic role order for %s",
                                user_id,
                            )

                await asyncio.gather(*(restore(user_id) for user_id in owners))
                self._restored_role_order_guilds.add(guild.id)

    @app_commands.command(name="balance", description="Показать баланс Guinn Coins")
    @app_commands.checks.cooldown(2, 5.0, key=lambda interaction: interaction.user.id)
    async def balance(self, interaction: discord.Interaction, пользователь: discord.Member | None = None):
        await interaction.response.defer(ephemeral=True)
        target = пользователь or interaction.user
        account = await get_coin_account(target.id)
        vip = "не активен"
        if account[2] and account[2] > datetime.now(UTC).isoformat():
            vip = f"до <t:{int(datetime.fromisoformat(account[2]).timestamp())}:F>"
        await followup_v2(interaction,
            f"Баланс {target.mention}: **{_price(account[0])}**\nVIP: {vip}", ephemeral=True
        )

    @app_commands.command(name="daily", description="Получить ежедневные Guinn Coins")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def daily(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        day = datetime.now(MSK).date().isoformat()
        claimed = await claim_daily_coins(interaction.user.id, day, SHOP_PRICES["daily"])
        await followup_v2(interaction,
            f"Получено **{_price(SHOP_PRICES['daily'])}**."
            if claimed else "Сегодня вы уже получали ежедневную награду.",
            ephemeral=True,
        )

    @app_commands.command(name="pay", description="Перевести Guinn Coins пользователю")
    @app_commands.checks.cooldown(1, 3.0, key=lambda interaction: interaction.user.id)
    async def pay(
        self,
        interaction: discord.Interaction,
        пользователь: discord.Member,
        количество: app_commands.Range[int, 1, MAX_COIN_OPERATION],
    ):
        if пользователь.bot or пользователь.id == interaction.user.id:
            await respond_v2(interaction, "Выберите другого пользователя, который не является ботом.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        paid = await transfer_coins(interaction.user.id, пользователь.id, количество)
        await followup_v2(interaction,
            f"Переведено {пользователь.mention}: **{_price(количество)}**."
            if paid else "Недостаточно Guinn Coins.",
            ephemeral=True,
        )

    @app_commands.command(name="top", description="Показать самых богатых пользователей")
    @app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
    async def balance_stats(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        rows = await get_richest_users(10)
        lines = "\n".join(f"**{i}.** <@{uid}> — {_price(balance)}" for i, (uid, balance) in enumerate(rows, 1))
        spec = notice(
            code="SHOP.TOP",
            title="🏦 Самые богатые участники",
            summary=lines or "Пока нет данных.",
            next_step="Дополнительных действий не требуется.",
        )
        await followup_v2(interaction, embed=spec, ephemeral=True)

    @app_commands.command(name="grant", description="Выдать Guinn Coins пользователю")
    @administrator_or_owner()
    async def coins_grant(
        self,
        interaction: discord.Interaction,
        пользователь: discord.Member,
        количество: app_commands.Range[int, 1, MAX_COIN_OPERATION],
    ):
        if not has_administrator_access(interaction.user):
            await respond_v2(interaction,
                "Команда доступна только администраторам.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        balance = await adjust_coins(пользователь.id, количество, "admin_grant")
        await followup_v2(interaction,
            f"Выдано {пользователь.mention}: {_price(количество)}. Баланс: {_price(balance)}.", ephemeral=True
        )

    @app_commands.command(name="take", description="Снять Guinn Coins с баланса")
    @administrator_or_owner()
    async def coins_take(
        self,
        interaction: discord.Interaction,
        пользователь: discord.Member,
        количество: app_commands.Range[int, 1, MAX_COIN_OPERATION],
    ):
        if not has_administrator_access(interaction.user):
            await respond_v2(interaction,
                "Команда доступна только администраторам.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        removed, balance = await remove_coins(
            пользователь.id, количество, "admin_take"
        )
        await followup_v2(interaction,
            f"Снято у {пользователь.mention}: {_price(removed)}. Баланс: {_price(balance)}.",
            ephemeral=True,
        )

    @app_commands.command(name="reset", description="Обнулить Guinn Coins пользователя")
    @administrator_or_owner()
    async def coins_reset(
        self, interaction: discord.Interaction, пользователь: discord.Member
    ):
        if not has_administrator_access(interaction.user):
            await respond_v2(interaction,
                "Команда доступна только администраторам.", ephemeral=True
            )
            return
        await interaction.response.defer(ephemeral=True)
        removed = await reset_coins(пользователь.id)
        await followup_v2(interaction,
            f"Баланс {пользователь.mention} обнулён. Снято: {_price(removed)}. Текущий баланс: {_price(0)}.",
            ephemeral=True,
        )

    @app_commands.command(name="role_order", description="Изменить порядок своих купленных ролей")
    @app_commands.checks.cooldown(1, 10.0, key=lambda interaction: interaction.user.id)
    async def role_order(
        self,
        interaction: discord.Interaction,
        роль: discord.Role,
        позиция: app_commands.Range[int, 1, 3],
    ):
        await interaction.response.defer(ephemeral=True)
        if not await has_active_vip(interaction.user.id):
            await followup_v2(interaction,
                "Изменение порядка купленных ролей доступно только с активным VIP.",
                ephemeral=True,
            )
            return
        purchases = await get_user_purchased_roles(interaction.user.id)
        role_ids = {purchase[1] for purchase in purchases}
        if роль.id not in role_ids:
            await followup_v2(interaction,
                "Можно переставлять только свои активные купленные роли.", ephemeral=True
            )
            return
        roles = [interaction.guild.get_role(role_id) for role_id in role_ids]
        roles = sorted((item for item in roles if item), key=lambda item: item.position, reverse=True)
        roles.remove(роль)
        roles.insert(min(позиция - 1, len(roles)), роль)
        try:
            roles = await _order_cosmetic_roles(
                interaction.guild,
                interaction.user.id,
                [item.id for item in roles],
            )
        except discord.HTTPException:
            logger.exception("Failed to reorder purchased roles for %s", interaction.user.id)
            await followup_v2(interaction,
                "Не удалось изменить порядок ролей.", ephemeral=True
            )
            return
        effective_position = roles.index(роль) + 1
        await followup_v2(interaction,
            f"Роль {роль.mention} установлена на позицию **{effective_position}**. "
            "Кастомные роли всегда располагаются выше роли «Цвет».",
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        for purchase in await get_user_purchased_roles(member.id):
            purchase_id, role_id, _, _, role_kind = purchase
            if role_kind != "custom":
                continue
            role = member.guild.get_role(role_id)
            if role is not None:
                try:
                    await role.delete(reason="Custom role owner left the server")
                except discord.HTTPException:
                    logger.exception("Failed to delete departed member role %s", role_id)
                    continue
            await deactivate_purchased_role(purchase_id, member.id)

    async def vip_expiration(self):
        for (user_id,) in await get_expired_vip_users():
            purchases = await get_user_purchased_roles(user_id)
            for guild in self.bot.guilds:
                member = guild.get_member(user_id)
                role = get_role(guild, "vip")
                if member and role and role in member.roles:
                    try:
                        await member.remove_roles(role, reason="Guinn Coins VIP expired")
                    except discord.HTTPException:
                        logger.exception("Failed to remove expired VIP from %s", user_id)
                room = await get_voice_room_by_owner(user_id)
                channel = guild.get_channel(room[0]) if room else None
                if room and isinstance(channel, discord.VoiceChannel):
                    for banned_id in await get_voice_owner_bans(user_id):
                        banned_member = guild.get_member(banned_id)
                        if banned_member is not None:
                            try:
                                await channel.set_permissions(
                                    banned_member,
                                    overwrite=None,
                                    reason="Guinn Coins VIP expired",
                                )
                            except discord.HTTPException:
                                logger.exception(
                                    "Failed to clear expired VIP voice ban %s", banned_id
                                )
                    if room[2]:
                        try:
                            from cogs.voice_rooms import room_embed

                            message = await channel.fetch_message(room[2])
                            await edit_message_v2(message,
                                embed=room_embed(user_id, (), vip_access=False)
                            )
                        except discord.HTTPException:
                            logger.exception("Failed to refresh expired VIP room %s", room[0])
            all_roles_processed = True
            for purchase_id, role_id, _, _, _ in purchases:
                target_guild = next(
                    (
                        guild
                        for guild in self.bot.guilds
                        if guild.get_role(role_id) is not None
                    ),
                    self.bot.guilds[0] if len(self.bot.guilds) == 1 else None,
                )
                if target_guild is None:
                    all_roles_processed = False
                    continue
                sale = await claim_role_sale(
                    purchase_id,
                    user_id,
                    guild_id=target_guild.id,
                    source="vip_expired",
                )
                if sale is None:
                    continue
                try:
                    async with _SHOP_INTENT_LOCKS.lock(-purchase_id):
                        await self._process_role_sale(target_guild, sale)
                except (discord.HTTPException, RuntimeError):
                    logger.exception(
                        "Expired VIP role sale %s queued for retry", purchase_id
                    )
                    all_roles_processed = False
            if await get_reserved_role_sales(user_id=user_id):
                all_roles_processed = False
            if not all_roles_processed:
                logger.warning(
                    "VIP cleanup for %s will be retried because some roles failed",
                    user_id,
                )
                continue
            await clear_voice_owner_bans(user_id)
            await clear_vip(user_id)
            for guild in self.bot.guilds:
                member = guild.get_member(user_id)
                if member is not None:
                    try:
                        await send_v2(member,
                            "Ваш VIP закончился. Купленные роли удалены с возвратом "
                            "50% стоимости, voice ban-лист очищен и VIP-возможности отключены.",
                            component_marker=f"shop-vip-expired:{user_id}",
                        )
                    except discord.HTTPException:
                        pass
                await _refresh_user_summits(guild, user_id)


async def setup(bot: commands.Bot):
    await bot.add_cog(ShopCog(bot))
