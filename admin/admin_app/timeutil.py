from datetime import datetime, timedelta, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def shanghai_now() -> datetime:
    return utcnow() + timedelta(hours=8)


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds") + "Z"
