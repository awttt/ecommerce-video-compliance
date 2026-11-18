"""用中转站原来的登录接口核对邮箱和密码。

浏览器只把邮箱和密码交给本机后台。后台转发到中转站的 /auth/login，
不把中转站的 access token 发给浏览器，也不写入数据库。
"""

from __future__ import annotations

import re
import secrets
import threading
import time

import httpx

from app.config import get_settings

SUPER_ADMIN_EMAIL = "350944364@qq.com"
USER_AGENT = "ecommerce-video-compliance/0.1.0"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PENDING_SECONDS = 300
_PENDING_LIMIT = 32

# 测试可以换成 httpx.MockTransport。正式运行保持 None，走真实网络。
_transport: httpx.BaseTransport | None = None
_pending: dict[str, tuple[str, str, float]] = {}
_lock = threading.Lock()

_REASON = {
    "INVALID_CREDENTIALS": (400, "邮箱或密码不正确"),
    "INVALID_EMAIL": (400, "邮箱格式不正确"),
    "USER_NOT_ACTIVE": (403, "这个账号已停用"),
    "USER_INACTIVE": (403, "这个账号已停用"),
    "BACKEND_MODE_ADMIN_ONLY": (403, "中转站处于后台模式，普通用户不能登录"),
    "TURNSTILE_VERIFICATION_FAILED": (400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    "TENCENT_CAPTCHA_VERIFICATION_FAILED": (400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    "ALIYUN_CAPTCHA_VERIFICATION_FAILED": (400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    "TOKEN_EXPIRED": (401, "登录已过期，请重新登录"),
    "ACCESS_TOKEN_EXPIRED": (401, "登录已过期，请重新登录"),
    "INVALID_TOKEN": (401, "登录已过期，请重新登录"),
    "TOKEN_REVOKED": (401, "登录已过期，请重新登录"),
}

_TEXT = (
    ("invalid email or password", 400, "邮箱或密码不正确"),
    ("invalid or expired 2fa", 400, "两步验证已过期，请重新登录"),
    ("backend mode", 403, "中转站处于后台模式，普通用户不能登录"),
    ("user self-service is disabled", 403, "中转站处于后台模式，普通用户不能登录"),
    ("turnstile", 400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    ("captcha", 400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    ("too many", 429, "操作太频繁，请稍后再试"),
)

_CJK = re.compile(r"[\u4e00-\u9fff]")


class RelayLoginError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status if 400 <= status <= 599 else 502


def normalize_email(value: str) -> str:
    cleaned = value.strip().lower()
    if not _EMAIL.fullmatch(cleaned):
        raise RelayLoginError("邮箱格式不正确", 400)
    return cleaned


def explain_login_error(status: int, body: object) -> RelayLoginError:
    payload = body if isinstance(body, dict) else {}
    reason = str(payload.get("reason") or "").strip().upper()
    message = str(payload.get("message") or "").strip()
    mapped = _REASON.get(reason)
    if mapped is None:
        lowered = message.lower()
        for needle, code, text in _TEXT:
            if needle in lowered:
                mapped = (code, text)
                break
    if mapped is not None:
        return RelayLoginError(mapped[1], mapped[0])
    if status == 429:
        return RelayLoginError("操作太频繁，请稍后再试", 429)
    if status in {401, 403} and not _CJK.search(message):
        if status == 403:
            return RelayLoginError("中转站拒绝了这次登录", 403)
        return RelayLoginError("邮箱或密码不正确", 401)
    if status >= 500 or status == 0:
        return RelayLoginError("中转站暂时不可用，请稍后再试", 502 if status == 0 else status)
    if _CJK.search(message) and len(message) <= 180 and "sk-" not in message.lower():
        return RelayLoginError(message, status if status >= 400 else 400)
    return RelayLoginError("中转站拒绝了这次请求", status if status >= 400 else 400)


def post_auth(path: str, payload: dict) -> dict:
    """POST 中转站账号接口，只返回 data。payload 里有密码或临时令牌，不要写进日志。"""
    url = f"{get_settings().relay_base_url.rstrip('/')}{path}"
    headers = {
        "Accept": "application/json",
        "Accept-Language": "zh-CN",
        "User-Agent": USER_AGENT,
    }
    kwargs: dict = {
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "trust_env": _transport is None,
        "headers": {"User-Agent": USER_AGENT},
    }
    if _transport is not None:
        kwargs["transport"] = _transport
    try:
        with httpx.Client(**kwargs) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.TimeoutException as exc:
        raise RelayLoginError("连接大语言模型超时，请稍后再试", 504) from exc
    except httpx.HTTPError as exc:
        raise RelayLoginError("连不上大语言模型，请检查网络", 502) from exc
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = body.get("code") if isinstance(body, dict) else None
    if response.status_code >= 400 or code not in (None, 0):
        raise explain_login_error(response.status_code, body)
    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    return data if isinstance(data, dict) else {}


def login_password(email: str, password: str) -> dict:
    return post_auth("/auth/login", {"email": email, "password": password})


def login_totp(temp_token: str, totp_code: str) -> dict:
    return post_auth("/auth/login/2fa", {"temp_token": temp_token, "totp_code": totp_code})


def _purge_locked(now: float) -> None:
    expired = [key for key, item in _pending.items() if item[2] <= now]
    for key in expired:
        _pending.pop(key, None)


def remember_2fa(email: str, temp_token: str) -> str:
    """暂存两步验证的临时令牌，只把一次性 challenge 交给浏览器。"""
    now = time.monotonic()
    challenge = secrets.token_urlsafe(24)
    with _lock:
        _purge_locked(now)
        if len(_pending) >= _PENDING_LIMIT:
            oldest = min(_pending, key=lambda key: _pending[key][2])
            _pending.pop(oldest, None)
        _pending[challenge] = (temp_token, email, now + _PENDING_SECONDS)
    return challenge


def peek_2fa(challenge: str) -> tuple[str, str]:
    now = time.monotonic()
    with _lock:
        _purge_locked(now)
        item = _pending.get(challenge)
    if item is None:
        raise RelayLoginError("请先登录，再填写验证码", 400)
    return item[0], item[1]


def drop_2fa(challenge: str) -> None:
    with _lock:
        _pending.pop(challenge, None)
