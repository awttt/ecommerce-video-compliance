"""FastAPI 应用。界面构建产物存在时由这里托管。"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.image_routes import router as image_router
from app.api.routes import router
from app.logging_setup import setup_logging
from app.paths import bundle_root, load_project_dotenv
from app.services.manager import get_manager

_FALLBACK = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>御镜视频审核系统</title>
  <style>
    body { font-family: sans-serif; margin: 48px auto; max-width: 720px; line-height: 1.6; color: #1f2933; }
    code { background: #f3f5f7; padding: 2px 6px; }
  </style>
</head>
<body>
  <h1>御镜视频审核系统</h1>
  <p>接口已经启动，但桌面界面还没有构建。</p>
  <p>在项目的 <code>frontend</code> 目录执行 <code>npm install</code> 和 <code>npm run build</code>，然后重新打开本程序。</p>
  <p>接口文档：<a href="/docs">/docs</a></p>
</body>
</html>
"""


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_project_dotenv()
    setup_logging()
    manager = get_manager()
    await manager.init()
    mcp_session = getattr(app.state, "mcp_session", None)
    mcp_started = False
    if mcp_session is not None:
        try:
            await mcp_session.__aenter__()
            mcp_started = True
        except Exception:
            logging.getLogger("app.mcp").exception("MCP 会话没有启动，界面和现有接口不受影响")
    app.state.mcp_ready = mcp_started
    try:
        yield
    finally:
        if mcp_started:
            try:
                await mcp_session.__aexit__(None, None, None)
            except Exception:
                logging.getLogger("app.mcp").exception("MCP 会话关闭时出错")
        await manager.shutdown()


def create_app(console_url: str | None = None) -> FastAPI:
    app = FastAPI(title="御镜视频审核系统", version=__version__, lifespan=lifespan)
    app.state.mcp_session = None
    app.state.mcp_ready = False
    # 管理控制台的地址。为空表示这次没有启动控制台，界面不显示入口。
    app.state.console_url = console_url or ""
    app.include_router(router)
    app.include_router(image_router)
    try:
        from app.mcp_server import attach_mcp, mcp_enabled

        if mcp_enabled():
            attach_mcp(app)
    except Exception:
        logging.getLogger("app.mcp").exception("MCP 未加载，界面和现有接口不受影响")
        app.state.mcp_session = None
    dist = bundle_root() / "frontend" / "dist"
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    else:

        @app.get("/", include_in_schema=False)
        async def frontend_missing() -> HTMLResponse:
            return HTMLResponse(_FALLBACK)

    return app
