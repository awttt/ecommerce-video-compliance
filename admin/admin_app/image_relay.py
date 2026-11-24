"""管理端生图工作台的专组、专钥、能力矩阵和请求体。

与审核侧完全并行：审核继续用 EVC_RELAY_GROUP_NAME 和「电商视频合规」密钥，
生图用「生图专用-bot创建」分组和「生图专用」密钥。不使用审核视频的「电商视频合规」。
解析不到生图专组就明确失败，绝不改用审核组、普通组或管理密钥。

分组编号只存在于中转站返回的记录里，用来创建这一把专钥。代码不把任何
固定编号当成授权，也没有编号备用机制。

计价：本站展示预估 ¥0.10 / 张，一次 4 张 ¥0.40。真实扣费沿用网关按 ImageCount
的实账，不在这里改 token4u，也不把本站零售价当成上游成本。

尺寸：上游直连只验证过 gpt-image-2.5-flare、n=1、省略 quality 的
2048x1152 与 3840x2160。这两档标「上游已验证 · 本站网关待验」。
其余尺寸、模型、指定 quality、n>1 不得标成已测试。未验证尺寸默认禁用。
1K 基础档可以选，但只标「本站网关待验 · 未经上游实测」。

真实调用默认关闭。必须同时有 EVC_ADMIN_IMAGE_LIVE=1、
EVC_ADMIN_IMAGE_CONTRACT_OK=1、当前用户专组授权，以及已核验组合，才会
同步 POST https://token4u.cc/v1/images/generations。
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import logging
import os
import socket
from datetime import timedelta
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy.orm import Session

import admin_app.auth_relay as auth_relay
from admin_app.config import ROOT, get_settings
from admin_app.errors import DomainError
from admin_app.models import Member
from admin_app.timeutil import utcnow

log = logging.getLogger(__name__)

# 生图专组（bot创建）。可用 EVC_ADMIN_IMAGE_GROUP_NAME 覆盖。只按名称精确解析。
# 不读取 EVC_RELAY_GROUP_NAME，避免和审核组混用。
DEFAULT_IMAGE_GROUP_NAME = "生图专用-bot创建"
# 生图密钥名（bot创建）。与审核密钥「电商视频合规」是两把不同的 Key。
IMAGE_KEY_NAME = "生图专用"
REVIEW_KEY_NAME = "电商视频合规"
IMAGE_PRICE_CENTS = 10
IMAGE_GENERATIONS_URL = "https://token4u.cc/v1/images/generations"
IMAGE_GENERATIONS_PATH = "/images/generations"

DEFAULT_IMAGE_MODELS = (
    "gpt-image-2.5-flare",
    "gpt-image-2.5-sunburst",
    "gpt-image-2.6-flare",
    "gpt-image-2.6-sunburst",
)
# 上游直连实测能出图的模型。不代表本站网关已就绪，也不代表专组清单里一定有它。
IMAGE_UPSTREAM_VERIFIED_MODELS = frozenset({"gpt-image-2.5-flare"})
IMAGE_MODEL_VERIFIED_SIZES = {
    "gpt-image-2.5-flare": frozenset({"2048x1152", "3840x2160"}),
}

IMAGE_GROUP_NOT_READY = (
    "生图专用分组未就绪：分组「{name}」不在当前账号的可用分组中。"
    "当前账号未开通时不可用，不会改用审核分组或其他分组。"
)
IMAGE_CAPABILITY_NOTE = (
    "2K/4K 仅模型 gpt-image-2.5-flare 的 2048x1152、3840x2160 为上游已验证尺寸"
    "（仅 n=1、省略 quality）；本站网关待验，真实生图默认关闭。"
    "其他尺寸、模型、指定 quality、n>1 不得视为已测试；未验证尺寸默认禁用。"
    "参考图与异步生成首版禁用。"
)

# 1K 基础档可以选，但没有上游实测，不能标成已验证。
_GATEWAY_PENDING_SIZES = frozenset({"auto", "1024x1024", "1536x1024", "1024x1536"})
_UNVERIFIED_SIZES = frozenset(
    {
        "1536x864",
        "864x1536",
        "2048x2048",
        "2560x1440",
        "1440x2560",
        "2160x3840",
        "2880x2880",
    }
)
_MODEL_VERIFIED_SIZE_ALL: frozenset[str] = frozenset().union(*IMAGE_MODEL_VERIFIED_SIZES.values())
_ALLOWED_SIZES = _GATEWAY_PENDING_SIZES | _MODEL_VERIFIED_SIZE_ALL | _UNVERIFIED_SIZES
_SIZE_MENU: tuple[tuple[str, str], ...] = (
    ("auto", ""),
    ("1024x1024", ""),
    ("1536x1024", ""),
    ("1024x1536", ""),
    ("2048x1152", "2K 横版"),
    ("3840x2160", "4K 横版"),
    ("1536x864", "未验证"),
    ("864x1536", "未验证"),
    ("2048x2048", "2K 方版 · 未验证"),
    ("2560x1440", "未验证"),
    ("1440x2560", "未验证"),
    ("2160x3840", "4K 竖版 · 未验证"),
    ("2880x2880", "未验证"),
)
_QUALITIES = ("auto", "low", "medium", "high")
_MAX_IMAGE_BYTES = 20 * 1024 * 1024

# 测试注入。正式运行为 None，不会因此去读任何密钥。
_transport: httpx.BaseTransport | None = None


def set_transport(transport: httpx.BaseTransport | None) -> None:
    global _transport
    _transport = transport


def _flag(name: str) -> bool:
    return os.environ.get(name, "").strip() == "1"


def _unverified_sizes_allowed() -> bool:
    return _flag("EVC_ADMIN_IMAGE_ALLOW_UNVERIFIED_SIZES")


def _unverified_quality_allowed() -> bool:
    return _flag("EVC_ADMIN_IMAGE_ALLOW_UNVERIFIED_QUALITY")


def candidate_models() -> tuple[str, ...]:
    """候选模型可配置。空配置沿用四名已确认候选，不把它们写成已就绪。"""
    raw = os.environ.get("EVC_ADMIN_IMAGE_MODELS", "").strip()
    if not raw:
        return DEFAULT_IMAGE_MODELS
    items = tuple(dict.fromkeys(part.strip() for part in raw.split(",") if part.strip()))
    return items or DEFAULT_IMAGE_MODELS


def image_group_name() -> str:
    text = os.environ.get("EVC_ADMIN_IMAGE_GROUP_NAME", "").strip()
    return text or DEFAULT_IMAGE_GROUP_NAME


def image_pricing_metadata() -> dict:
    return {
        "currency": "CNY",
        "unit": "张",
        "price_per_image": IMAGE_PRICE_CENTS / 100,
        "price_per_image_cents": IMAGE_PRICE_CENTS,
        "display": "¥0.10 / 张",
        "note": (
            "界面预估为张数 × ¥0.10（一次 4 张 ¥0.40）。"
            "这是本站展示用零售预估，真实扣费以网关按 ImageCount 的实账为准，不是上游成本。"
            "不同参数的成本并未承诺相同。"
        ),
    }


def estimate_cents(n: int) -> int:
    count = max(1, min(4, int(n)))
    return count * IMAGE_PRICE_CENTS


def matched_image_groups(groups: list[dict], *, name: str) -> list[dict]:
    wanted = name.strip()
    found: list[dict] = []
    seen: set[int] = set()
    for item in groups:
        if not isinstance(item, dict):
            continue
        raw = item.get("id")
        if isinstance(raw, bool) or isinstance(raw, float) or not isinstance(raw, int) or raw <= 0:
            continue
        status = str(item.get("status") or "active").strip().lower()
        if status not in {"", "active"}:
            continue
        if str(item.get("name") or "").strip() != wanted:
            continue
        if raw in seen:
            continue
        seen.add(raw)
        found.append(item)
    return found


def resolve_image_group(groups: list[dict], *, name: str) -> dict:
    """按名称精确解析生图专组。找不到或多个同名都失败，不回落到其他组。"""
    found = matched_image_groups(groups, name=name)
    wanted = name.strip()
    if len(found) == 1:
        return found[0]
    if len(found) > 1:
        raise DomainError(502, f"可用分组里有多个名为「{wanted}」的分组，无法确定绑定哪一个")
    raise DomainError(403, IMAGE_GROUP_NOT_READY.format(name=wanted))


def group_model_names(group: dict) -> list[str] | None:
    """专组如果带回模型清单，就用这份清单判断能力。没有字段则返回 None，不假装已就绪。"""
    for key in ("models", "available_models", "model_list"):
        if key not in group:
            continue
        value = group.get(key)
        if not isinstance(value, list):
            return []
        names: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                names.append(item.strip())
            elif isinstance(item, dict):
                label = str(item.get("id") or item.get("name") or "").strip()
                if label:
                    names.append(label)
        return list(dict.fromkeys(names))
    return None


def image_model_metadata(group_models: list[str] | None) -> list[dict]:
    known = set(group_models or [])
    confirmed = group_models is not None
    items: list[dict] = []
    for model_id in candidate_models():
        upstream = model_id in IMAGE_UPSTREAM_VERIFIED_MODELS
        ready = confirmed and model_id in known
        if confirmed:
            selectable = ready
        else:
            selectable = upstream
        if ready and upstream:
            note = "专组已列出 · 上游已验证 · 本站网关待验"
        elif ready:
            note = "专组已列出 · 本站网关待验 · 未经上游实测"
        elif upstream:
            note = "上游已验证 · 本站网关待验"
        else:
            note = "未按专组能力确认 · 未就绪"
        items.append(
            {
                "id": model_id,
                "label": model_id,
                "ready": ready,
                "upstream_verified": upstream,
                "selectable": selectable,
                "note": note,
            }
        )
    return items


def image_size_metadata() -> list[dict]:
    allow_all = _unverified_sizes_allowed()
    candidates = set(candidate_models())
    items: list[dict] = []
    for value, base_note in _SIZE_MENU:
        verified_for = [
            model
            for model in candidate_models()
            if model in candidates and value in IMAGE_MODEL_VERIFIED_SIZES.get(model, frozenset())
        ]
        gateway_pending = value in _GATEWAY_PENDING_SIZES
        upstream = bool(verified_for)
        if gateway_pending:
            note = "本站网关待验 · 未经上游实测"
            enabled = True
            verified = False
        elif upstream:
            note = (
                f"{base_note} · 上游已验证尺寸（仅 {verified_for[0]}，n=1、省略 quality）"
                " · 本站网关待验"
            )
            enabled = True
            verified = True
        else:
            note = base_note or "未验证"
            enabled = allow_all
            verified = False
        items.append(
            {
                "value": value,
                "label": "Auto" if value == "auto" else value,
                "note": note,
                "verified": verified,
                "gateway_pending": gateway_pending or upstream,
                "verified_for": verified_for,
                "enabled": enabled,
            }
        )
    return items


def image_quality_metadata() -> list[dict]:
    allow = _unverified_quality_allowed()
    items = []
    for value in _QUALITIES:
        enabled = value == "auto" or allow
        if value == "auto":
            note = "省略 quality，与已验证请求一致"
        elif allow:
            note = "未实测 · 已由服务端开关临时放开"
        else:
            note = "未实测 · 禁用"
        items.append(
            {
                "value": value,
                "label": "Auto" if value == "auto" else value[:1].upper() + value[1:],
                "enabled": enabled,
                "note": note,
            }
        )
    return items


def normalize_size(value: object) -> str:
    text = str(value or "auto").strip().lower()
    if text not in _ALLOWED_SIZES:
        raise DomainError(400, f"不支持的生图尺寸：{text}")
    return text


def normalize_quality(value: object) -> str:
    text = str(value or "auto").strip().lower()
    allowed = {item["value"] for item in image_quality_metadata() if item["enabled"]}
    if text not in _QUALITIES:
        raise DomainError(400, f"不支持的生图质量：{text}")
    if text not in allowed:
        raise DomainError(400, "指定质量尚未实测，暂不可用。请使用 Auto（请求中省略 quality）。")
    return text


def ensure_model(model: str) -> str:
    text = str(model or "").strip()
    if text not in candidate_models():
        raise DomainError(400, "请选择一个候选生图模型")
    return text


def ensure_verified_size(value: object, *, model: str = "") -> str:
    size = normalize_size(value)
    if size in _GATEWAY_PENDING_SIZES or _unverified_sizes_allowed():
        return size
    model_id = str(model or "").strip()
    if model_id and size in IMAGE_MODEL_VERIFIED_SIZES.get(model_id, frozenset()):
        return size
    if size in _MODEL_VERIFIED_SIZE_ALL:
        owners = "、".join(sorted(IMAGE_UPSTREAM_VERIFIED_MODELS))
        if not model_id:
            raise DomainError(400, f"尺寸 {size} 为上游已验证尺寸，但仅限模型 {owners}；请先选择该模型。")
        raise DomainError(400, f"尺寸 {size} 仅对模型 {owners} 上游已验证，当前模型 {model_id} 未验证该尺寸，暂不可用。")
    raise DomainError(
        400,
        f"尺寸 {size} 尚未验证（2K/4K 仅 gpt-image-2.5-flare 的 2048x1152、3840x2160 有上游实测），暂不可用。",
    )


def ensure_verified_size_combination(*, size: object, model: str, n: int, quality: object) -> str:
    quality_text = normalize_quality(quality)
    verified = ensure_verified_size(size, model=model)
    model_id = str(model or "").strip()
    if verified in IMAGE_MODEL_VERIFIED_SIZES.get(model_id, frozenset()) and (
        int(n) != 1 or quality_text != "auto"
    ):
        raise DomainError(
            400,
            f"尺寸 {verified} 的上游实测范围仅限 n=1、省略 quality（Auto）。"
            "n>1 或指定 quality 的组合尚未实测，暂不可用。",
        )
    return verified


def combination_ready_for_live(*, model: str, size: str, n: int, quality: str) -> bool:
    return (
        str(model) in IMAGE_MODEL_VERIFIED_SIZES
        and str(size) in IMAGE_MODEL_VERIFIED_SIZES.get(str(model), frozenset())
        and int(n) == 1
        and str(quality) == "auto"
    )


def orientation_of(size: str) -> str:
    text = str(size or "auto").strip().lower()
    if text == "auto" or "x" not in text:
        return "auto"
    parts = text.split("x")
    if len(parts) != 2:
        return "auto"
    try:
        width, height = int(parts[0]), int(parts[1])
    except ValueError:
        return "auto"
    if width == height:
        return "square"
    return "landscape" if width > height else "portrait"


def build_generation_payload(*, model: str, prompt: str, n: int, size: str = "auto", quality: str = "auto") -> dict:
    payload: dict = {"model": model, "prompt": prompt, "n": max(1, min(4, int(n)))}
    if size and size != "auto":
        payload["size"] = normalize_size(size)
    if quality and quality != "auto":
        payload["quality"] = normalize_quality(quality)
    return payload


def live_generation_enabled() -> bool:
    return _flag("EVC_ADMIN_IMAGE_LIVE") and _flag("EVC_ADMIN_IMAGE_CONTRACT_OK")


def image_generations_url() -> str:
    """只允许同步调用 token4u 网关。其他主机（包括直连渠道站）直接拒绝。"""
    base = get_settings().relay_gateway_url.rstrip("/")
    parsed = urlparse(base)
    host = (parsed.hostname or "").lower()
    path = parsed.path.rstrip("/") or "/"
    if parsed.scheme != "https" or host != "token4u.cc" or parsed.port not in (None, 443) or path != "/v1":
        raise DomainError(400, "生图只允许同步调用 https://token4u.cc/v1/images/generations")
    return IMAGE_GENERATIONS_URL


def _http_client(timeout: httpx.Timeout) -> httpx.Client:
    kwargs: dict = {"timeout": timeout, "trust_env": _transport is None}
    if _transport is not None:
        kwargs["transport"] = _transport
    return httpx.Client(**kwargs)


def _unwrap(response: httpx.Response) -> object:
    try:
        body = response.json()
    except ValueError:
        return None
    if isinstance(body, dict) and "data" in body:
        return body["data"]
    return body


def _extract_key(data: object) -> str:
    if not isinstance(data, dict):
        return ""
    for name in ("key", "api_key", "secret"):
        value = str(data.get(name) or "").strip()
        if value:
            return value
    return ""


def create_or_find_image_key(base_url: str, access_token: str, group_id: int, email: str) -> str:
    """用当前用户令牌幂等创建生图专钥。列回时必须同时匹配名称和分组，避免拿到审核钥。"""
    if group_id <= 0:
        raise DomainError(400, "生图分组无效")
    idem = hashlib.sha256(f"evc-admin-image-key:{email}:{group_id}".encode()).hexdigest()[:32]
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Idempotency-Key": idem,
        "User-Agent": "evc-admin/0.1.2",
        "Accept": "application/json",
    }
    list_headers = {
        "Authorization": f"Bearer {access_token}",
        "User-Agent": "evc-admin/0.1.2",
        "Accept": "application/json",
    }
    with _http_client(httpx.Timeout(20.0, connect=10.0)) as client:
        listing = client.get(
            f"{base_url.rstrip('/')}/keys",
            headers=list_headers,
            params={"page": 1, "page_size": 100},
        )
        if listing.status_code in (401, 403):
            raise DomainError(401, "登录已失效，请重新登录后再试")
        key = _key_from_list(listing, group_id)
        if not key:
            response = client.post(
                f"{base_url.rstrip('/')}/keys",
                headers=headers,
                json={"name": IMAGE_KEY_NAME, "group_id": group_id},
            )
            if response.status_code in (401, 403):
                raise DomainError(401, "登录已失效，请重新登录后再试")
            if response.status_code < 400:
                key = _extract_key(_unwrap(response))
        if key:
            return key
    raise DomainError(502, "没有拿到生图密钥。请重新登录后再试。不会改用审核密钥。")


def _key_items(response: httpx.Response) -> list[dict]:
    if response.status_code >= 400:
        return []
    body = _unwrap(response)
    items: object = body
    if isinstance(body, dict):
        items = None
        for key in ("items", "list", "keys"):
            if isinstance(body.get(key), list):
                items = body[key]
                break
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _key_from_list(response: httpx.Response, group_id: int) -> str:
    for item in _key_items(response):
        name = str(item.get("name") or "").strip()
        # 审核钥即使挂在同一个分组编号上也不取，避免把「电商视频合规」当成生图钥。
        if name == REVIEW_KEY_NAME or name != IMAGE_KEY_NAME:
            continue
        try:
            if int(item.get("group_id") or 0) != group_id:
                continue
        except (TypeError, ValueError):
            continue
        found = _extract_key(item)
        if found:
            return found
    return ""


def _access_token(db: Session, member: Member) -> str:
    from admin_app.relay import _refresh_member_token, base_url_of

    base = base_url_of(db)
    token = str(member.relay_access_token or "").strip()
    expires_at = member.relay_token_expires_at
    if token and expires_at is not None and expires_at <= utcnow() + timedelta(minutes=5):
        token = _refresh_member_token(db, member, base) or ""
    return token


def describe_image_group(db: Session, member: Member) -> tuple[dict | None, str]:
    """只查询一次。不创建密钥，也不把查不到解释成其他分组可用。"""
    token = _access_token(db, member)
    if not token:
        return None, "当前账号没有中转站登录态，无法确认生图分组。请重新登录后再试。不会改用审核密钥或管理密钥。"
    try:
        groups = auth_relay.available_groups(token)
        return resolve_image_group(groups, name=image_group_name()), ""
    except auth_relay.RelayLoginError:
        return None, "无法确认当前账号的生图分组，请重新登录后再试。不会改用审核分组。"
    except DomainError as exc:
        return None, exc.detail


def ensure_member_image_key(db: Session, member: Member) -> tuple[dict, str]:
    """确认专组并取得当前用户自己的生图钥。不读写审核钥 relay_api_key。"""
    from admin_app.relay import base_url_of

    token = _access_token(db, member)
    if not token:
        _group, reason = describe_image_group(db, member)
        raise DomainError(403, reason)
    try:
        groups = auth_relay.available_groups(token)
    except auth_relay.RelayLoginError as exc:
        raise DomainError(exc.status if 400 <= exc.status <= 599 else 502, "无法确认当前账号的生图分组，请重新登录后再试。") from exc
    group = resolve_image_group(groups, name=image_group_name())
    group_id = int(group["id"])
    existing = str(member.relay_image_api_key or "").strip()
    if existing:
        return group, existing
    key = create_or_find_image_key(base_url_of(db), token, group_id, member.email)
    member.relay_image_api_key = key
    db.commit()
    return group, key


def ensure_model_selectable(group: dict, model: str) -> None:
    chosen = next((item for item in image_model_metadata(group_model_names(group)) if item["id"] == model), None)
    if chosen is None or not chosen["selectable"]:
        raise DomainError(400, "该模型尚未按生图专用分组的可用能力确认，暂不可用")


def request_image_generation(*, api_key: str, payload: dict, live: bool = False) -> list[dict]:
    if not live and not live_generation_enabled():
        raise DomainError(501, "真实生图默认关闭。需要同时打开两个服务端开关，并完成能力与授权校验。")
    if not str(api_key or "").strip():
        raise DomainError(403, "当前账号还没有生图密钥。")
    url = image_generations_url()
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
        "User-Agent": "evc-admin/0.1.2",
    }
    try:
        with _http_client(httpx.Timeout(120.0, connect=15.0)) as client:
            response = client.post(url, json=payload, headers=headers)
    except httpx.HTTPError as exc:
        log.warning("image generation request failed: %s", type(exc).__name__)
        raise DomainError(502, "生图请求失败，请稍后再试") from exc
    try:
        body = response.json()
    except ValueError as exc:
        raise DomainError(502, "生图服务返回的数据格式不对") from exc
    if response.status_code >= 400:
        raise DomainError(502, f"生图请求被拒绝（{response.status_code}）")
    data = body.get("data") if isinstance(body, dict) else None
    if isinstance(data, dict) and isinstance(data.get("data"), list):
        data = data["data"]
    if not isinstance(data, list):
        raise DomainError(502, "生图服务没有返回结果列表")
    return [item for item in data if isinstance(item, dict)]


def _host_is_blocked(host: str) -> bool:
    lowered = host.strip().lower().rstrip(".")
    if not lowered or "chuanapi" in lowered or lowered in {"localhost", "metadata.google.internal"} or lowered.endswith(".local"):
        return True
    try:
        ip = ipaddress.ip_address(lowered)
    except ValueError:
        return False
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_reserved,
            ip.is_multicast,
            ip.is_unspecified,
        )
    )


def _assert_public_image_url(url: str) -> None:
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme != "https" or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise DomainError(502, "生图结果地址无效")
    if _host_is_blocked(host):
        raise DomainError(502, "生图结果地址无效")
    try:
        infos = socket.getaddrinfo(host, 443)
    except socket.gaierror as exc:
        raise DomainError(502, "生图结果地址无法访问") from exc
    for info in infos:
        address = info[4][0]
        if _host_is_blocked(address):
            raise DomainError(502, "生图结果地址无效")


def _download_image(url: str) -> bytes:
    _assert_public_image_url(url)
    try:
        with _http_client(httpx.Timeout(30.0, connect=10.0)) as client:
            response = client.get(url, follow_redirects=False)
    except httpx.HTTPError as exc:
        raise DomainError(502, "生图结果下载失败") from exc
    if response.status_code >= 300:
        raise DomainError(502, "生图结果下载失败")
    if len(response.content) > _MAX_IMAGE_BYTES:
        raise DomainError(502, "生图结果过大")
    return response.content


def image_bytes_from_item(item: dict) -> bytes:
    encoded = item.get("b64_json") or item.get("b64")
    if isinstance(encoded, str) and encoded.strip():
        try:
            data = base64.b64decode(encoded, validate=True)
        except Exception as exc:
            raise DomainError(502, "生图结果无法解析") from exc
        if not data or len(data) > _MAX_IMAGE_BYTES:
            raise DomainError(502, "生图结果无法解析")
        return data
    url = item.get("url")
    if isinstance(url, str) and url.strip():
        return _download_image(url.strip())
    raise DomainError(502, "生图结果缺少图片数据")


def sniff_image_suffix(payload: bytes) -> str:
    if payload.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if payload.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if payload.startswith(b"RIFF") and payload[8:12] == b"WEBP":
        return ".webp"
    raise DomainError(502, "生图结果不是受支持的图片")


def images_root() -> Path:
    override = os.environ.get("EVC_ADMIN_IMAGE_DIR", "").strip()
    if override:
        return Path(override)
    url = get_settings().database_url
    if url.startswith("sqlite:///"):
        return Path(url.removeprefix("sqlite:///")).parent / "image_jobs"
    return ROOT / "data" / "image_jobs"


def job_directory(member_id: int, job_id: str) -> Path:
    return images_root() / str(int(member_id)) / job_id


def parse_pixel_size(size: str) -> tuple[int, int]:
    if size == "auto" or "x" not in size:
        return (1024, 1024)
    parts = size.lower().split("x")
    try:
        return int(parts[0]), int(parts[1])
    except (ValueError, IndexError):
        return (1024, 1024)


def mock_svg(index: int, width: int, height: int, prompt: str, *, size: str) -> str:
    """占位图只声明请求尺寸，不把小图放大冒充原生分辨率。画面上必须能看出 MOCK。"""
    safe_prompt = (
        prompt[:40]
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
    caption = f"占位 {width}x{height}" if size == "auto" else f"{width}x{height}"
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">'
        f'<rect width="100%" height="100%" fill="#f7f9fc"/>'
        f'<rect x="8" y="8" width="{max(width - 16, 1)}" height="{max(height - 16, 1)}" fill="none" '
        'stroke="#d9dee5" stroke-dasharray="8 6"/>'
        f'<text x="50%" y="46%" font-family="sans-serif" font-size="{max(width // 16, 28)}" font-weight="bold" '
        'fill="#1677ff" text-anchor="middle">MOCK</text>'
        f'<text x="50%" y="58%" font-family="sans-serif" font-size="{max(width // 48, 16)}" fill="#5c6b7a" '
        'text-anchor="middle">预览占位图 · 未请求真实生图接口</text>'
        f'<text x="50%" y="68%" font-family="sans-serif" font-size="{max(width // 56, 14)}" fill="#8b97a6" '
        f'text-anchor="middle">{safe_prompt} · {caption}</text>'
        "</svg>"
    )


def store_mock_images(directory: Path, count: int, size: str, prompt: str) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    width, height = parse_pixel_size(size)
    names: list[str] = []
    for index in range(1, count + 1):
        name = f"mock-{index}.svg"
        (directory / name).write_text(
            mock_svg(index, width, height, prompt, size=size),
            encoding="utf-8",
        )
        names.append(name)
    return names


def store_live_images(directory: Path, items: list[dict]) -> list[str]:
    directory.mkdir(parents=True, exist_ok=True)
    names: list[str] = []
    for index, item in enumerate(items, start=1):
        payload = image_bytes_from_item(item)
        suffix = sniff_image_suffix(payload)
        name = f"image-{index}{suffix}"
        (directory / name).write_bytes(payload)
        names.append(name)
    if not names:
        raise DomainError(502, "生图服务没有返回可用的图片")
    return names
