from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def shanghai_now() -> datetime:
    return utcnow() + timedelta(hours=8)


def business_timezone_name() -> str:
    """用量和消耗的日、月边界。配置缺省或写错时用 Asia/Shanghai。"""
    from admin_app.config import get_settings

    name = (get_settings().timezone or "Asia/Shanghai").strip() or "Asia/Shanghai"
    try:
        ZoneInfo(name)
    except Exception:
        return "Asia/Shanghai"
    return name


def business_today() -> date:
    return datetime.now(ZoneInfo(business_timezone_name())).date()


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat(timespec="seconds") + "Z"
