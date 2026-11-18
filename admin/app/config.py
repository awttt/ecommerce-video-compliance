"""读取环境变量。管理密钥只来自环境变量，不进数据库。"""

from functools import lru_cache
from pathlib import Path

from pydantic import BaseModel


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB = ROOT / "data" / "admin.db"


class Settings(BaseModel):
    database_url: str
    jwt_secret: str
    relay_base_url: str
    relay_gateway_url: str
    relay_admin_key: str
    relay_group_id: str
    relay_group_name: str
    timezone: str


@lru_cache
def get_settings() -> Settings:
    import os

    url = os.environ.get("EVC_ADMIN_DATABASE_URL", "").strip()
    if not url:
        DEFAULT_DB.parent.mkdir(parents=True, exist_ok=True)
        url = f"sqlite:///{DEFAULT_DB}"
    return Settings(
        database_url=url,
        jwt_secret=os.environ.get("EVC_ADMIN_JWT_SECRET", "").strip() or "dev-evc-admin-change-me-32bytes!!",
        relay_base_url=os.environ.get("EVC_RELAY_BASE_URL", "https://token4u.cc/api/v1").rstrip("/"),
        relay_gateway_url=os.environ.get("EVC_RELAY_GATEWAY_URL", "https://token4u.cc/v1").rstrip("/"),
        relay_admin_key=os.environ.get("EVC_RELAY_ADMIN_KEY", "").strip(),
        relay_group_id=os.environ.get("EVC_RELAY_GROUP_ID", "").strip(),
        relay_group_name=os.environ.get("EVC_RELAY_GROUP_NAME", "电商审核专属-bot创建").strip()
        or "电商审核专属-bot创建",
        timezone=os.environ.get("EVC_RELAY_TIMEZONE", "Asia/Shanghai").strip() or "Asia/Shanghai",
    )
