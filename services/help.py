"""Profile-aware help copy: member and senior-staff surfaces."""

from __future__ import annotations

from core.profile import ServerProfile
from ui.command_registry import cmd, code
from ui.design import Accent
from ui.message_specs import MessageSpec
from ui.templates import notice

AUDIENCE_MEMBER = "member"
AUDIENCE_STAFF = "staff"
AUDIENCE_CHOICES: tuple[tuple[str, str], ...] = (
    ("Участник", AUDIENCE_MEMBER),
    ("Старший состав", AUDIENCE_STAFF),
)

_MAT_TRUCK = ("mat_truck_l1", "mat_truck_l2", "mat_truck_l3")


def _open(profile: ServerProfile, *upgrades: str) -> bool:
    return any(profile.requires(key) for key in upgrades)


def _member_details(profile: ServerProfile) -> list[tuple[str, str]]:
    details: list[tuple[str, str]] = [
        (
            "С чего начать",
            "Большинство действий не требует запоминать команды: откройте "
            f"личную панель {cmd('me')} или нажмите кнопку под сообщением бота. "
            "Ответы видны только вам.",
        ),
        (
            "Вступить в семью",
            "Кнопка в канале заявок. Укажите идентификатор персонажа Memphis "
            "и данные о себе. Решение появится после рассмотрения.",
        ),
        (
            "Мои задачи и семейные контракты",
            "Взятый контракт появляется во вкладке «Мои задачи». Инструкция "
            "прикреплена к этой задаче: тема, сервер, источник, дата проверки, "
            f"ответственный и версия. Каталог — {cmd('contract catalog')}, "
            f"запуск — {cmd('contract start')}.",
        ),
        (
            "События и отчёт",
            f"Ближайшие события — {cmd('event list')} и вкладка «События». "
            "Когда нужно сдать отчёт, форма откроется во вкладке «Сдать отчёт».",
        ),
        (
            "Ресурсы и прогресс",
            "Выданные комплекты — вкладка «Ресурсы». Склад организации и "
            "имущество семьи показывает старший состав. «Мой прогресс» — "
            "репутация, влияние и сезонное право, без выдуманных чисел.",
        ),
        (
            "Помощь человеку",
            "Кнопка «Нужна помощь» в канале помощи. Опишите ситуацию и при "
            "необходимости приложите до пяти изображений.",
        ),
        (
            "Голосовые комнаты",
            "Зайдите в канал создания комнаты. Лимит, имя, доступ и передача "
            f"владения: {code('voice limit')}, {code('voice name')}, "
            f"{code('voice kick')}, {code('voice ban')}, {code('voice unban')}, "
            f"{code('voice access')}, {code('voice transfer')}, {code('voice delete')}.",
        ),
        (
            "Guinn Coins и магазин",
            f"{code('coins balance')} — баланс, {code('coins daily')} — ежедневная "
            f"награда, {code('coins pay')} — перевод. Это валюта Discord-сервера, "
            "не Dark Coins проекта. Ставки задаёт старший состав.",
        ),
    ]
    if _open(profile, "territory_control"):
        details.append(
            (
                "Капт и контроль территорий",
                "Запись в состав капта — через события семьи. Лимиты состава "
                f"читает профиль, числа из воздуха бот не подставляет. "
                f"Создать событие может старший состав: {cmd('event create')}.",
            )
        )
    if _open(profile, "air_cargo"):
        details.append(
            (
                "ВЗА",
                "Воздушные грузовые рейсы доступны, пока улучшение открыто. "
                f"Расписание и запись — {cmd('event list')}.",
            )
        )
    if _open(profile, *_MAT_TRUCK):
        details.append(
            (
                "Матовозка",
                "Маршруты матовозки открываются по уровню улучшения семьи. "
                "Условия конкретной поездки — в карточке задачи, не в этой справке.",
            )
        )
    if _open(profile, "psn"):
        details.append(
            (
                "ПСН и офис организации",
                "Имущество и офис организации ведутся в снабжении. Карточка "
                f"объекта — {cmd('car property')}.",
            )
        )
    if _open(profile, "workshops"):
        details.append(
            (
                "Мастерские",
                f"Окна мастерских читаются из календаря семьи. Список — {cmd('event list')}.",
            )
        )
    if _open(profile, "dealers"):
        details.append(
            (
                "Дилеры",
                f"Окна дилеров — в календаре. Запись на событие: {cmd('event list')}.",
            )
        )
    for key, title, summary in (
        ("fort_zancudo", "Fort Zancudo", "Шаблон события доступен, пока улучшение открыто."),
        ("cayo_perico", "Cayo Perico", "Шаблон события доступен, пока улучшение открыто."),
        ("bank_robbery", "Ограбление банка", "Шаблон доступен только при открытом улучшении."),
        ("business_robbery", "Ограбление бизнеса", "Шаблон доступен только при открытом улучшении."),
        ("player_robbery", "Ограбление игрока", "Шаблон доступен только при открытом улучшении."),
        ("truck_intercept", "Перехват грузовика", "Шаблон доступен только при открытом улучшении."),
    ):
        if _open(profile, key):
            details.append((title, f"{summary} {cmd('event create')}."))
    return details


def _staff_details(profile: ServerProfile) -> list[tuple[str, str]]:
    details: list[tuple[str, str]] = [
        (
            "Требует решения",
            f"Живая очередь заявок, отчётов и обращений: {cmd('queue list')} "
            f"или панель {cmd('staff panel')}.",
        ),
        (
            "Состав",
            f"Реестр семьи, приём и допуски: {cmd('members list')}.",
        ),
        (
            "План событий",
            f"Календарь и составы: {cmd('event list')}, {cmd('event create')}. "
            "Шаблоны активностей появляются только при открытом улучшении.",
        ),
        (
            "Выплаты",
            f"Счета Memphis, обязательства и подтверждение: {cmd('treasury accounts')}.",
        ),
        (
            "Снабжение",
            "Склад организации, комплекты и транспорт: "
            f"{cmd('stock alerts')}, {cmd('car show')}.",
        ),
        (
            "Развитие",
            "Дерево улучшений, сезон и сведения. Игровые числа не выдумываются: "
            f"{cmd('progress tree')}, {cmd('progress lore')}.",
        ),
        (
            "Настройка сервера",
            f"Мастер каналов — {cmd('setup start')}. Рекомендуемые имена: "
            "старт-и-правила, мои-задачи, события, отчёты, помощь; закрытые "
            "очередь-сс, выплаты, снабжение, аудит. Живые настройки — "
            f"{cmd('config show')}, панели — {cmd('panels list')}.",
        ),
        (
            "Кулдауны",
            f"{cmd('help cooldown')} меняет задержку справки, формы помощи, "
            f"{cmd('event create')} и {cmd('contract start')}. Ноль отключает ограничение.",
        ),
    ]
    details.extend(_member_details(profile))
    return details


def build_help_spec(profile: ServerProfile, audience: str) -> MessageSpec:
    if audience == AUDIENCE_STAFF:
        details = _staff_details(profile)
        title = "Справка для старшего состава"
        summary = (
            "Показаны только функции активного профиля Memphis. "
            "Если улучшение не открыто, соответствующего раздела здесь нет. "
            "Инструкции к игровым заданиям живут на карточке задачи, не в этой стене текста."
        )
        code_key = "HELP.STAFF"
    else:
        details = _member_details(profile)
        title = "Справка для участника"
        summary = (
            "Краткий указатель по доступным функциям Guinness на Majestic RP Memphis. "
            "Правила конкретной задачи — во вкладке «Мои задачи», с источником и датой проверки."
        )
        code_key = "HELP.MEMBER"
    return notice(
        code=code_key,
        title=title,
        summary=summary,
        details=tuple(details),
        next_step="Эта справка видна только вам.",
        accent=Accent.SERVICE,
    )


def help_index_spec() -> MessageSpec:
    return notice(
        code="HELP.OVERVIEW",
        title="Справка Guinness Bot",
        summary=(
            "Две справки: для участника и для старшего состава. "
            "Выберите аудиторию в этой же команде. Разделы, которые зависят от "
            "улучшений организации, появляются только после их открытия."
        ),
        details=(
            (
                "Участник",
                f"Личная панель {cmd('me')}, семейные контракты, события, отчёты, помощь.",
            ),
            (
                "Старший состав",
                "Очередь, состав, план событий, выплаты, снабжение, развитие и настройка.",
            ),
        ),
        next_step="Эта справка видна только вам.",
        accent=Accent.SERVICE,
    )

