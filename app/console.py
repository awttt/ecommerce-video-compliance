"""管理控制台的本机壳：托管打进安装包的页面，把 /v1 原样转发到云端后台。

控制台自己有云端服务和数据库，本机不复制它的业务逻辑，也不保存它的数据。
这里只做两件事：

- 把 `admin/web/dist` 的静态文件发给窗口里的 iframe；
- 把 `/v1/*` 的请求方法、查询串、请求头和请求体转发到 `EVC_ADMIN_SERVER_URL`，
  再把状态码、响应头和响应体带回来。

两个应用用不同的本机端口，浏览器的 localStorage、sessionStorage 和 Cookie
因此天然分开，桌面工具这边的数据不会被控制台页面读到。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response

from app.paths import bundle_root, is_inside, project_root

DEFAULT_CLOUD_URL = "https://admin.token4u.cc"
DEFAULT_PORT = 8780
_PROXY_TIMEOUT = httpx.Timeout(30.0, read=120.0, write=120.0, pool=10.0)

# 逐跳头由本机这一跳重新生成，不能直接透传。
# accept-encoding 交给 httpx 自己声明，避免云端用 br/zstd 压缩而本机解不开。
_DROP_REQUEST = {
    "host",
    "content-length",
    "accept-encoding",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}
_DROP_RESPONSE = {
    "content-length",
    "content-encoding",
    # date 和 server 由本机这一跳重新生成，透传会出现两个同名头。
    "date",
    "server",
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
}

_RECHARGE_PATH = re.compile(r"orders(?:/\d+(?:/cancel)?)?")

_FALLBACK = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>御镜管理控制台</title>
  <style>
    body { font-family: sans-serif; margin: 48px auto; max-width: 720px; line-height: 1.6; color: #1f2933; }
    code { background: #f3f5f7; padding: 2px 6px; }
  </style>
</head>
<body>
  <h1>御镜管理控制台</h1>
  <p>控制台页面还没有构建。请在 <code>admin/web</code> 目录执行 <code>npm install</code> 和 <code>npm run build</code>，然后重新打开本程序。</p>
</body>
</html>
"""


def console_enabled() -> bool:
    """环境变量 EVC_CONSOLE=0 时整个控制台入口关闭。"""
    return os.environ.get("EVC_CONSOLE", "").strip() not in {"0", "false", "off", "no"}


def cloud_base_url() -> str:
    raw = os.environ.get("EVC_ADMIN_SERVER_URL", "").strip() or DEFAULT_CLOUD_URL
    return raw.rstrip("/")


def console_port() -> int:
    raw = os.environ.get("EVC_CONSOLE_PORT", "").strip()
    if not raw:
        return DEFAULT_PORT
    try:
        port = int(raw)
    except ValueError:
        logging.getLogger("app.console").warning("EVC_CONSOLE_PORT 不是数字，改用 %s", DEFAULT_PORT)
        return DEFAULT_PORT
    if not 1 <= port <= 65535:
        return DEFAULT_PORT
    return port


def console_dist() -> Path:
    """打包后在只读资源目录，开发时用 admin/web/dist。"""
    bundled = bundle_root() / "console" / "dist"
    if (bundled / "index.html").is_file():
        return bundled
    return project_root() / "admin" / "web" / "dist"


def _tools_credentials() -> tuple[str, str]:
    """读工具端已经登录的中转站令牌。只在本进程里用，不经过 HTTP。"""
    try:
        from app.services.manager import get_manager

        return get_manager().relay_credentials()
    except Exception:
        logging.getLogger("app.console").warning("读取工具端登录状态失败", exc_info=True)
        return "", ""


async def adopt_tools_session(kind: str, payload: dict) -> None:
    """控制台登录成功后，把同一套账号也登进工具端，避免再输一遍密码。"""
    logger = logging.getLogger("app.console")
    try:
        from app.services.manager import get_manager

        manager = get_manager()
        if kind == "login":
            email = str(payload.get("email") or "").strip()
            password = str(payload.get("password") or "")
            if not email or not password:
                return
            await manager.account_login(email, password)
        else:
            code = str(payload.get("totp_code") or "").strip()
            if not code:
                return
            await manager.account_login_2fa(code)
    except Exception:
        logger.warning("控制台登录后同步工具端会话失败，工具端仍可用自己的登录页", exc_info=True)


def create_console_app(
    cloud: str | None = None,
    dist: Path | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    tools_url: str = "",
) -> FastAPI:
    base = (cloud or cloud_base_url()).rstrip("/")
    directory = dist or console_dist()
    logger = logging.getLogger("app.console")
    index = directory / "index.html"
    index_html = ""
    if index.is_file():
        index_html = index.read_text(encoding="utf-8")
        if tools_url:
            # 从桌面工具整页跳过来时，控制台据此显示「视频审核工具」并跳回去。
            snippet = f"<script>window.__EVC_TOOLS_URL__={json.dumps(tools_url)};</script>"
            if "</head>" in index_html:
                index_html = index_html.replace("</head>", snippet + "</head>", 1)
            else:
                index_html = snippet + index_html
    app = FastAPI(
        title="御镜管理控制台",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict:
        return {"ok": True, "console": True, "cloud": base}

    @app.get("/api/account/balance", include_in_schema=False)
    async def get_balance() -> Response:
        """获取充值账号的余额信息（需要工具端登录）。"""
        if not tools_url:
            return JSONResponse({"detail": "充值只能在桌面端使用"}, status_code=404)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_PROXY_TIMEOUT) as client:
                upstream = await client.get(f"{tools_url.rstrip('/')}/api/settings")
        except httpx.HTTPError as exc:
            logger.warning("获取工具端余额失败：%s", exc)
            return JSONResponse({"detail": "连不上本机服务，请重启程序后再试"}, status_code=502)
        try:
            data = upstream.json()
            return JSONResponse({
                "logged_in": bool(data.get("account_logged_in")),
                "email": str(data.get("account_email") or ""),
                "balance": data.get("account_balance"),
            })
        except Exception:
            return JSONResponse({"detail": "本机服务响应格式错误"}, status_code=502)

    @app.api_route("/api/account/recharge", methods=["GET"], include_in_schema=False)
    @app.api_route(
        "/api/account/recharge/{path:path}",
        methods=["GET", "POST"],
        include_in_schema=False,
    )
    async def proxy_recharge(request: Request, path: str = "") -> Response:
        """转发充值相关请求到工具端。只放行充值接口，其他路径一律 404。"""
        if not tools_url:
            return JSONResponse({"detail": "充值只能在桌面端使用"}, status_code=404)
        if path and not _RECHARGE_PATH.fullmatch(path):
            return JSONResponse({"detail": "没有这个接口"}, status_code=404)
        url = f"{tools_url.rstrip('/')}/api/account/recharge" + (f"/{path}" if path else "")
        # 只带内容类型，后台的登录令牌和 Cookie 不转给工具端。
        headers = {}
        if request.headers.get("content-type"):
            headers["content-type"] = request.headers["content-type"]
        body = await request.body()
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_PROXY_TIMEOUT) as client:
                upstream = await client.request(
                    request.method,
                    url,
                    params=request.query_params,
                    headers=headers,
                    content=body,
                )
        except httpx.HTTPError as exc:
            logger.warning("转发到充值接口 %s 失败：%s", url, exc)
            return JSONResponse({"detail": "连不上本机服务，请重启程序后再试"}, status_code=502)
        outgoing = {key: value for key, value in upstream.headers.items() if key.lower() not in _DROP_RESPONSE}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=outgoing)

    @app.api_route(
        "/v1/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        include_in_schema=False,
    )
    async def proxy(path: str, request: Request) -> Response:
        if not base:
            return JSONResponse({"detail": "还没有配置管理服务器地址"}, status_code=503)
        url = f"{base}/v1/{path}"
        headers = {key: value for key, value in request.headers.items() if key.lower() not in _DROP_REQUEST}
        body = await request.body()
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_PROXY_TIMEOUT) as client:
                upstream = await client.request(
                    request.method,
                    url,
                    params=request.query_params,
                    headers=headers,
                    content=body,
                )
        except httpx.HTTPError as exc:
            logger.warning("转发到 %s 失败：%s", url, exc)
            return JSONResponse({"detail": "连不上管理服务器，请检查网络后重试"}, status_code=502)
        outgoing = {key: value for key, value in upstream.headers.items() if key.lower() not in _DROP_RESPONSE}
        if request.method == "POST" and upstream.status_code == 200 and path in {"auth/login", "auth/login/2fa"}:
            try:
                payload = json.loads(body) if body else {}
            except ValueError:
                payload = {}
            if isinstance(payload, dict) and payload:
                # 不阻塞这次登录响应，工具端在后台登录同一套账号。
                asyncio.create_task(
                    adopt_tools_session("login" if path == "auth/login" else "2fa", payload)
                )
        return Response(content=upstream.content, status_code=upstream.status_code, headers=outgoing)

    @app.post("/sso", include_in_schema=False)
    async def sso() -> Response:
        """把工具端的登录会话换成控制台会话。"""
        if not base:
            return JSONResponse({"detail": "还没有配置管理服务器地址"}, status_code=503)
        access, refresh = _tools_credentials()
        if not access:
            return JSONResponse({"detail": "工具端还没有登录，请先在工具端登录"}, status_code=401)
        try:
            async with httpx.AsyncClient(transport=transport, timeout=_PROXY_TIMEOUT) as client:
                upstream = await client.post(
                    f"{base}/v1/auth/sso",
                    json={"access_token": access, "refresh_token": refresh},
                )
        except httpx.HTTPError as exc:
            logger.warning("控制台单点登录失败：%s", exc)
            return JSONResponse({"detail": "连不上管理服务器，请检查网络后重试"}, status_code=502)
        outgoing = {key: value for key, value in upstream.headers.items() if key.lower() not in _DROP_RESPONSE}
        return Response(content=upstream.content, status_code=upstream.status_code, headers=outgoing)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def spa(full_path: str) -> Response:
        if full_path in {"v1", "api"} or full_path.startswith(("v1/", "api/")):
            return JSONResponse({"detail": "没有这个接口"}, status_code=404)
        index = directory / "index.html"
        if not index_html:
            return HTMLResponse(_FALLBACK, status_code=503)
        if full_path:
            candidate = directory / full_path
            try:
                candidate = candidate.resolve()
            except OSError:
                candidate = directory
            if candidate.is_file() and is_inside(candidate, directory):
                return FileResponse(candidate)
        return HTMLResponse(index_html)

    return app
