"""FastAPI 应用。界面构建产物存在时由这里托管。"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import router
from app.logging_setup import setup_logging
from app.paths import bundle_root, load_project_dotenv
from app.services.manager import get_manager

_FALLBACK = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <title>电商视频素材合规检测</title>
  <style>
    body { font-family: sans-serif; margin: 48px auto; max-width: 720px; line-height: 1.6; color: #1f2933; }
    code { background: #f3f5f7; padding: 2px 6px; }
  </style>
</head>
<body>
  <h1>电商视频素材合规检测</h1>
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
    try:
        yield
    finally:
        await manager.shutdown()


def create_app() -> FastAPI:
    app = FastAPI(title="电商视频素材合规检测", version=__version__, lifespan=lifespan)
    app.include_router(router)
    dist = bundle_root() / "frontend" / "dist"
    if (dist / "index.html").is_file():
        app.mount("/", StaticFiles(directory=dist, html=True), name="ui")
    else:

        @app.get("/", include_in_schema=False)
        async def frontend_missing() -> HTMLResponse:
            return HTMLResponse(_FALLBACK)

    return app
