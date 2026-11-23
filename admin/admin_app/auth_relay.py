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

from admin_app.config import get_settings

SUPER_ADMIN_EMAIL = "350944364@qq.com"
USER_AGENT = "ecommerce-video-compliance/0.1.2"
_EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_PENDING_SECONDS = 300
_PENDING_LIMIT = 32

# 测试可以换成 httpx.MockTransport。正式运行保持 None，走真实网络。
_transport: httpx.BaseTransport | None = None
_pending: dict[str, tuple[str, str, float]] = {}
_lock = threading.Lock()

_REASON = {
    "INVALID_CREDENTIALS": (400, "邮箱或密码不正确"),
    "PASSWORD_INCORRECT": (400, "当前密码不正确"),
    "INVALID_EMAIL": (400, "邮箱格式不正确"),
    "USER_NOT_ACTIVE": (403, "这个账号已停用"),
    "USER_INACTIVE": (403, "这个账号已停用"),
    "BACKEND_MODE_ADMIN_ONLY": (403, "暂不支持普通账号登录，请联系管理员"),
    "TURNSTILE_VERIFICATION_FAILED": (400, "登录需要人机验证，请联系管理员处理"),
    "TENCENT_CAPTCHA_VERIFICATION_FAILED": (400, "登录需要人机验证，请联系管理员处理"),
    "ALIYUN_CAPTCHA_VERIFICATION_FAILED": (400, "登录需要人机验证，请联系管理员处理"),
    "TOKEN_EXPIRED": (401, "登录已过期，请重新登录"),
    "ACCESS_TOKEN_EXPIRED": (401, "登录已过期，请重新登录"),
    "INVALID_TOKEN": (401, "登录已过期，请重新登录"),
    "TOKEN_REVOKED": (401, "登录已过期，请重新登录"),
}

_TEXT = (
    ("invalid email or password", 400, "邮箱或密码不正确"),
    ("current password is incorrect", 400, "当前密码不正确"),
    ("password is incorrect", 400, "当前密码不正确"),
    ("invalid or expired 2fa", 400, "两步验证已过期，请重新登录"),
    ("backend mode", 403, "暂不支持普通账号登录，请联系管理员"),
    ("user self-service is disabled", 403, "暂不支持普通账号登录，请联系管理员"),
    ("turnstile", 400, "登录需要人机验证，请联系管理员处理"),
    ("captcha", 400, "登录需要人机验证，请联系管理员处理"),
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
            return RelayLoginError("登录被拒绝，请联系管理员", 403)
        return RelayLoginError("邮箱或密码不正确", 401)
    if status >= 500 or status == 0:
        return RelayLoginError("服务暂时不可用，请稍后再试", 502 if status == 0 else status)
    if _CJK.search(message) and len(message) <= 180 and "sk-" not in message.lower():
        return RelayLoginError(message, status if status >= 400 else 400)
    return RelayLoginError("请求被拒绝，请稍后再试", status if status >= 400 else 400)


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


def _authed(method: str, path: str, access_token: str, payload: dict | None = None) -> dict:
    """带用户令牌访问中转站账号接口。payload 可能含密码，异常里不要带上请求体。"""
    token = access_token.strip()
    if not token:
        raise RelayLoginError("登录已失效，请重新登录", 401)
    url = f"{get_settings().relay_base_url.rstrip('/')}{path}"
    kwargs: dict = {
        "timeout": httpx.Timeout(20.0, connect=10.0),
        "trust_env": _transport is None,
        "headers": {"User-Agent": USER_AGENT},
    }
    if _transport is not None:
        kwargs["transport"] = _transport
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Accept-Language": "zh-CN",
        "User-Agent": USER_AGENT,
    }
    try:
        with httpx.Client(**kwargs) as client:
            response = client.request(method, url, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise RelayLoginError("连接大语言模型超时，请稍后再试", 504)
    except httpx.HTTPError:
        raise RelayLoginError("连不上大语言模型，请检查网络", 502)
    try:
        body = response.json()
    except ValueError:
        body = {}
    code = body.get("code") if isinstance(body, dict) else None
    if response.status_code >= 400 or code not in (None, 0):
        if response.status_code in {401, 403}:
            # 这里校验的是令牌，不是密码，别把失败说成密码错误。
            raise RelayLoginError("登录已失效，请重新登录", response.status_code)
        raise explain_login_error(response.status_code, body)
    data = body.get("data") if isinstance(body, dict) and "data" in body else body
    return data if isinstance(data, dict) else {}


def current_account(access_token: str) -> dict:
    """用中转站的用户令牌读取当前账号，走单点登录时校验令牌是否还有效。"""
    return _authed("GET", "/auth/me", access_token)


def change_password(access_token: str, old_password: str, new_password: str) -> dict:
    """改中转站登录密码。调用方负责在成功后用新密码重新登录，旧令牌会失效。"""
    return _authed(
        "PUT",
        "/user/password",
        access_token,
        {"old_password": old_password, "new_password": new_password},
    )


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
