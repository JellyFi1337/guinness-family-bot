from datetime import datetime


ALLOWED_TREASURY_ACTIONS = {"add", "remove"}
ALLOWED_MEMBER_ROLES = {"Young", "Main", "Friend", "Super Friend"}
MAX_STORED_AMOUNT = 9_000_000_000_000_000


def normalize_birthday(value: str) -> str:
    """Validate a day/month value and return the canonical DD.MM form."""
    value = value.strip()
    try:
        # Leap year 2000 keeps 29.02 valid for birthdays.
        parsed = datetime.strptime(f"{value}.2000", "%d.%m.%Y")
    except ValueError as exc:
        raise ValueError("Дата должна быть существующей датой в формате ДД.ММ") from exc
    return parsed.strftime("%d.%m")


def normalize_member_role(value: str) -> str:
    normalized = value.strip().casefold()
    role = next(
        (candidate for candidate in ALLOWED_MEMBER_ROLES if candidate.casefold() == normalized),
        None,
    )
    if role is None:
        raise ValueError(
            "Можно запросить только Young, Main, Friend или Super Friend"
        )
    return role


def normalize_positive_amount(value: int | str) -> int:
    try:
        amount = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Сумма должна быть целым числом") from exc
    if amount <= 0:
        raise ValueError("Сумма должна быть больше нуля")
    if amount > MAX_STORED_AMOUNT:
        raise ValueError(
            f"Сумма не должна превышать {MAX_STORED_AMOUNT:,}".replace(",", " ")
        )
    return amount


def normalize_treasury_action(value: str) -> str:
    action = value.strip().lower()
    if action not in ALLOWED_TREASURY_ACTIONS:
        raise ValueError("Действие должно быть 'add' или 'remove'")
    return action
