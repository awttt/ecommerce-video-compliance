"""token4u.cc 账号接口。浏览器不直接访问，只由本机后端转发。"""

from __future__ import annotations

import re
import uuid
from typing import Any

import httpx

from app.logging_setup import redact

AUTH_ROOT = "https://token4u.cc/api/v1"
CHAT_BASE = "https://token4u.cc/v1"
KEY_NAME = "电商视频合规"
# 用户可见分组「国产模型-bot创建」。Key 不绑这个分组时，中转站不会给它分配渠道。
KEY_GROUP_ID = 14
# 画面模型只有这两个。DeepSeek 4.1 Flash 的接口名是 deepseek-flash。
PICTURE_MODEL = "glm-5.3-flash"
DEEPSEEK_FLASH_MODEL = "deepseek-flash"
PICTURE_MODELS = (PICTURE_MODEL, DEEPSEEK_FLASH_MODEL)


def configured_model(value: object) -> str:
    """空值和其他名字用 GLM。DeepSeek 的旧名字都归到 4.1 Flash。"""
    text = str(value or "").strip().lower()
    if text.startswith("deepseek"):
        return DEEPSEEK_FLASH_MODEL
    if text == PICTURE_MODEL:
        return PICTURE_MODEL
    return PICTURE_MODEL


_REASON = {
    "INVALID_CREDENTIALS": (400, "邮箱或密码不正确"),
    "EMAIL_EXISTS": (409, "这个邮箱已经注册"),
    "EMAIL_RESERVED": (400, "这个邮箱不能注册"),
    "INVALID_EMAIL": (400, "邮箱格式不正确"),
    "EMAIL_VERIFY_REQUIRED": (400, "请填写邮箱验证码"),
    "INVALID_VERIFY_CODE": (400, "验证码不正确或已过期"),
    "VERIFY_CODE_REQUIRED": (400, "请填写邮箱验证码"),
    "VERIFY_CODE_TOO_FREQUENT": (429, "发送太频繁，请稍后再试"),
    "VERIFY_CODE_MAX_ATTEMPTS": (429, "验证码错误次数过多，请重新获取"),
    "REGISTRATION_DISABLED": (403, "站点暂时关闭了注册"),
    "EMAIL_SUFFIX_NOT_ALLOWED": (400, "这个邮箱后缀不能注册"),
    "INVITATION_CODE_REQUIRED": (400, "注册需要邀请码，请先在网站完成注册后再回来登录"),
    "USER_NOT_ACTIVE": (403, "这个账号已停用"),
    "BACKEND_MODE_ADMIN_ONLY": (403, "中转站处于后台模式，普通用户不能登录"),
    "TURNSTILE_VERIFICATION_FAILED": (400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    "TENCENT_CAPTCHA_VERIFICATION_FAILED": (400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    "ALIYUN_CAPTCHA_VERIFICATION_FAILED": (400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    "TURNSTILE_NOT_CONFIGURED": (502, "中转站的人机验证还没配好"),
    "TENCENT_CAPTCHA_NOT_CONFIGURED": (502, "中转站的人机验证还没配好"),
    "ALIYUN_CAPTCHA_NOT_CONFIGURED": (502, "中转站的人机验证还没配好"),
    "CAPTCHA_PROVIDER_CONFLICT": (502, "中转站同时开了多个人机验证"),
    "TOKEN_EXPIRED": (401, "登录已过期，请重新登录"),
    "ACCESS_TOKEN_EXPIRED": (401, "登录已过期，请重新登录"),
    "INVALID_TOKEN": (401, "登录已过期，请重新登录"),
    "TOKEN_REVOKED": (401, "登录已过期，请重新登录"),
    "REFRESH_TOKEN_INVALID": (401, "登录已过期，请重新登录"),
    "REFRESH_TOKEN_EXPIRED": (401, "登录已过期，请重新登录"),
    "REFRESH_TOKEN_REUSED": (401, "登录已过期，请重新登录"),
    "IDEMPOTENCY_STORE_UNAVAILABLE": (503, "中转站暂时不可用，请稍后再试"),
    "IDEMPOTENCY_KEY_REQUIRED": (502, "中转站没有接受创建密钥的请求"),
    "PAYMENT_DISABLED": (403, "站点暂时关闭了充值"),
    "BALANCE_PAYMENT_DISABLED": (403, "余额充值已关闭"),
    "INVALID_AMOUNT": (400, "充值金额不在允许范围内"),
    "INVALID_ORDER_TYPE": (400, "不支持这种充值方式"),
    "PAYMENT_GATEWAY_ERROR": (502, "支付通道暂时不可用，请稍后再试"),
    "PAYMENT_PROVIDER_MISCONFIGURED": (502, "支付通道还没配好"),
    "NO_AVAILABLE_INSTANCE": (429, "这个支付方式暂时不可用，请换一种"),
    "DAILY_LIMIT_EXCEEDED": (429, "今天的充值额度已用完"),
    "WECHAT_PAYMENT_MP_NOT_CONFIGURED": (502, "微信支付还没配好"),
    "USER_INACTIVE": (403, "这个账号已停用"),
}

_TEXT = (
    ("invalid email or password", 400, "邮箱或密码不正确"),
    ("email already exists", 409, "这个邮箱已经注册"),
    ("registration is currently disabled", 403, "站点暂时关闭了注册"),
    ("email verification is required", 400, "请填写邮箱验证码"),
    ("invalid or expired verification code", 400, "验证码不正确或已过期"),
    ("invalid or expired 2fa", 400, "两步验证已过期，请重新登录"),
    ("backend mode", 403, "中转站处于后台模式，普通用户不能登录"),
    ("user self-service is disabled", 403, "中转站处于后台模式，普通用户不能登录"),
    ("turnstile", 400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    ("captcha", 400, "站点开启了人机验证，请先在中转站关闭验证码后再试"),
    ("too many", 429, "操作太频繁，请稍后再试"),
    ("payment system is disabled", 403, "站点暂时关闭了充值"),
    ("balance recharge has been disabled", 403, "余额充值已关闭"),
    ("amount out of range", 400, "充值金额不在允许范围内"),
    ("amount must be a positive number", 400, "请填写大于 0 的充值金额"),
    ("order not found", 404, "订单不存在"),
)

_CJK = re.compile(r"[\u4e00-\u9fff]")


class RelayError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status if 400 <= status <= 599 else 502


def usable_api_key(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value.strip()
    if len(text) < 16 or "*" in text or any(char.isspace() for char in text):
        return False
    return True


def explain_error(status: int, body: object) -> RelayError:
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
        return RelayError(mapped[1], mapped[0])
    if status == 429:
        return RelayError("操作太频繁，请稍后再试", 429)
    if status in {401, 403} and not _CJK.search(message):
        if status == 403:
            return RelayError("中转站拒绝了这次登录", 403)
        return RelayError("登录已过期，请重新登录", 401)
    if status >= 500 or status == 0:
        return RelayError("中转站暂时不可用，请稍后再试", 502 if status == 0 else status)
    cleaned = redact(message)
    if _CJK.search(cleaned) and "***" not in cleaned and len(cleaned) <= 180:
        return RelayError(cleaned, status if status >= 400 else 400)
    return RelayError("中转站拒绝了这次请求", status if status >= 400 else 400)


class RelayClient:
    def __init__(self, transport: httpx.AsyncBaseTransport | None = None, auth_root: str = AUTH_ROOT):
        self._transport = transport
        self.auth_root = auth_root.rstrip("/")

    def _open(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "timeout": httpx.Timeout(20.0, connect=10.0),
            "trust_env": self._transport is None,
        }
        if self._transport is not None:
            kwargs["transport"] = self._transport
        return httpx.AsyncClient(**kwargs)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str = "",
        json: dict | None = None,
        params: dict | None = None,
        headers: dict | None = None,
    ) -> Any:
        header_map = {"Accept": "application/json", "Accept-Language": "zh-CN"}
        if token:
            header_map["Authorization"] = f"Bearer {token}"
        if headers:
            header_map.update(headers)
        try:
            async with self._open() as client:
                response = await client.request(
                    method,
                    f"{self.auth_root}{path}",
                    json=json,
                    params=params,
                    headers=header_map,
                )
        except httpx.TimeoutException as exc:
            raise RelayError("连接 token4u.cc 超时，请稍后再试", 504) from exc
        except httpx.HTTPError as exc:
            raise RelayError("连不上 token4u.cc，请检查网络", 502) from exc
        try:
            body = response.json()
        except ValueError:
            body = {}
        code = body.get("code") if isinstance(body, dict) else None
        if response.status_code >= 400 or (code not in (None, 0)):
            raise explain_error(response.status_code, body)
        if isinstance(body, dict) and "data" in body:
            return body.get("data")
        return body

    async def public_settings(self) -> dict:
        data = await self._request("GET", "/settings/public")
        return data if isinstance(data, dict) else {}

    async def send_verify_code(self, email: str) -> dict:
        data = await self._request("POST", "/auth/send-verify-code", json={"email": email})
        return data if isinstance(data, dict) else {}

    async def register(self, email: str, password: str, verify_code: str = "") -> dict:
        payload: dict[str, str] = {"email": email, "password": password}
        if verify_code:
            payload["verify_code"] = verify_code
        data = await self._request("POST", "/auth/register", json=payload)
        return data if isinstance(data, dict) else {}

    async def login(self, email: str, password: str) -> dict:
        data = await self._request("POST", "/auth/login", json={"email": email, "password": password})
        return data if isinstance(data, dict) else {}

    async def login_2fa(self, temp_token: str, totp_code: str) -> dict:
        data = await self._request(
            "POST",
            "/auth/login/2fa",
            json={"temp_token": temp_token, "totp_code": totp_code},
        )
        return data if isinstance(data, dict) else {}

    async def refresh(self, refresh_token: str) -> dict:
        data = await self._request("POST", "/auth/refresh", json={"refresh_token": refresh_token})
        return data if isinstance(data, dict) else {}

    async def logout(self, refresh_token: str) -> None:
        payload = {"refresh_token": refresh_token} if refresh_token else {}
        await self._request("POST", "/auth/logout", json=payload)

    async def profile(self, token: str) -> dict:
        data = await self._request("GET", "/user/profile", token=token)
        return data if isinstance(data, dict) else {}

    async def usage(self, token: str) -> dict:
        data = await self._request("GET", "/usage/dashboard/stats", token=token)
        return data if isinstance(data, dict) else {}

    async def update_key_group(self, token: str, key_id: int) -> dict:
        data = await self._request(
            "PUT",
            f"/keys/{key_id}",
            token=token,
            json={"group_id": KEY_GROUP_ID},
        )
        return data if isinstance(data, dict) else {}

    async def list_keys(self, token: str) -> list[dict]:
        data = await self._request("GET", "/keys", token=token, params={"page": 1, "page_size": 100})
        raw = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    async def create_key(self, token: str) -> dict:
        data = await self._request(
            "POST",
            "/keys",
            token=token,
            json={"name": KEY_NAME, "group_id": KEY_GROUP_ID},
            headers={"Idempotency-Key": uuid.uuid4().hex},
        )
        return data if isinstance(data, dict) else {}

    async def checkout_info(self, token: str) -> dict:
        data = await self._request("GET", "/payment/checkout-info", token=token)
        return data if isinstance(data, dict) else {}

    async def create_balance_order(self, token: str, amount: float, payment_type: str) -> dict:
        data = await self._request(
            "POST",
            "/payment/orders",
            token=token,
            json={
                "amount": amount,
                "payment_type": payment_type,
                "order_type": "balance",
                "is_mobile": False,
            },
        )
        return data if isinstance(data, dict) else {}

    async def list_balance_orders(self, token: str) -> list[dict]:
        data = await self._request(
            "GET",
            "/payment/orders/my",
            token=token,
            params={"page": 1, "page_size": 10, "order_type": "balance"},
        )
        raw = data.get("items") if isinstance(data, dict) else None
        if not isinstance(raw, list):
            return []
        return [item for item in raw if isinstance(item, dict)]

    async def get_payment_order(self, token: str, order_id: int) -> dict:
        data = await self._request("GET", f"/payment/orders/{order_id}", token=token)
        return data if isinstance(data, dict) else {}

    async def cancel_payment_order(self, token: str, order_id: int) -> None:
        await self._request("POST", f"/payment/orders/{order_id}/cancel", token=token, json={})
