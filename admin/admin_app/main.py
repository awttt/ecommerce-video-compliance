from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from admin_app.db import init_db, session
from admin_app.errors import DomainError
from admin_app.image_routes import router as image_router
from admin_app.routes import router
from admin_app.seed import seed

DIST = Path(__file__).resolve().parents[1] / "web" / "dist"


@asynccontextmanager
async def lifespan(_app: FastAPI):
    init_db()
    db = session()
    try:
        seed(db)
    finally:
        db.close()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="御镜视频审核系统", version="0.1.2", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5174", "http://localhost:5174"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.exception_handler(DomainError)
    def _domain(_request, exc: DomainError) -> JSONResponse:
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    app.include_router(router)
    app.include_router(image_router)
    if (DIST / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

        @app.get("/{full_path:path}")
        def spa(full_path: str) -> FileResponse:
            if full_path.startswith("v1"):
                raise HTTPException(status_code=404, detail="没有这个接口")
            candidate = DIST / full_path
            if full_path and candidate.is_file():
                return FileResponse(candidate)
            return FileResponse(DIST / "index.html")

    return app