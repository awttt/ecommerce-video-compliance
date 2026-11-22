"""把已通过的共享违规词并进检测用的命中词。

读取管理后台时只用内存里的登录令牌，不写进日志或异常文本。
"""

from __future__ import annotations

import os

import httpx

from app.schemas import DetectionRules, PhraseRule

_CATEGORIES = {"价格", "规格", "品牌", "夸大宣传"}


class SharedRulesError(Exception):
    def __init__(self, message: str = "读取共享违规词失败", status: int = 502) -> None:
        self.status = status if 400 <= status <= 599 else 502
        super().__init__(message)


def _safe_detail(response: httpx.Response, token: str) -> str:
    detail = ""
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("detail"), str):
        detail = body["detail"]
    if token and token in detail:
        detail = detail.replace(token, "")
    detail = detail.strip()
    if detail and len(detail) <= 180:
        return detail
    return "管理后台没有完成这次操作"


def phrases_from_shared(rows: list[dict]) -> list[PhraseRule]:
    """只收已通过的词。夸大宣传只进口播，其它类别同时进口播和画面。"""
    found: list[PhraseRule] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("status") or "approved") != "approved":
            continue
        category = str(row.get("category") or "")
        if category not in _CATEGORIES:
            continue
        sources = ("口播",) if category == "夸大宣传" else ("口播", "画面")
        for source in sources:
            try:
                found.append(PhraseRule(text=str(row.get("word") or ""), category=category, source=source))
            except ValueError:
                continue
    return found


def merge_shared_rules(rules: DetectionRules, shared: list[PhraseRule]) -> DetectionRules:
    """已有的词不重复加。总数不超过检测规则允许的 300 条。"""
    merged = list(rules.extra_phrases)
    seen = {(item.source, item.category, item.text) for item in merged}
    for item in shared:
        key = (item.source, item.category, item.text)
        if key in seen or len(merged) >= 300:
            continue
        merged.append(item)
        seen.add(key)
    if merged == list(rules.extra_phrases):
        return rules
    return rules.model_copy(update={"extra_phrases": merged})


def admin_base_url() -> str:
    raw = os.environ.get("EVC_ADMIN_SERVER_URL", "").strip()
    if raw:
        return raw.rstrip("/")
    return "https://admin.token4u.cc"


def admin_shared_request(
    access_token: str,
    method: str,
    path: str,
    body: dict | None = None,
    *,
    base_url: str = "",
    transport: httpx.BaseTransport | None = None,
) -> object:
    """调用管理后台的共享违规词接口。异常文本里不放令牌。"""
    token = (access_token or "").strip()
    if not token:
        raise SharedRulesError("请先登录", 401)
    base = (base_url or admin_base_url()).rstrip("/")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    try:
        with httpx.Client(timeout=8.0, transport=transport, trust_env=transport is None) as client:
            response = client.request(method, f"{base}{path}", headers=headers, json=body)
    except httpx.HTTPError as exc:
        raise SharedRulesError("连不上管理后台") from exc
    if response.status_code >= 400:
        status = response.status_code if 400 <= response.status_code <= 599 else 502
        raise SharedRulesError(_safe_detail(response, token), status)
    try:
        return response.json()
    except ValueError as exc:
        raise SharedRulesError("管理后台没有返回共享违规词") from exc


def fetch_approved_shared_rules(
    access_token: str,
    *,
    base_url: str = "",
    transport: httpx.BaseTransport | None = None,
) -> list[dict]:
    """向管理后台要已通过的共享违规词。没有登录令牌时返回空列表。"""
    if not (access_token or "").strip():
        return []
    body = admin_shared_request(
        access_token,
        "GET",
        "/v1/shared-rules/approved",
        base_url=base_url,
        transport=transport,
    )
    if not isinstance(body, list):
        raise SharedRulesError("管理后台没有返回共享违规词")
    return [item for item in body if isinstance(item, dict)]
