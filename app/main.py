"""给 uvicorn 用的应用对象：`uvicorn app.main:app`。"""

from app.factory import create_app

app = create_app()
