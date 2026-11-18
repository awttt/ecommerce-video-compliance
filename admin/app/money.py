from decimal import Decimal, ROUND_HALF_UP

from app.errors import DomainError


def to_cents(value: object) -> int:
    try:
        quantized = Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except Exception as exc:
        raise DomainError(400, "金额格式不正确") from exc
    return int(quantized * 100)


def yuan(cents: int) -> float:
    return round(cents / 100, 2)
