import pytest


@pytest.fixture(autouse=True)
def isolate_env(tmp_path, monkeypatch):
    monkeypatch.setenv("EVC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_ID", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_SECRET_KEY", raising=False)
    monkeypatch.delenv("TENCENTCLOUD_REGION", raising=False)
    monkeypatch.delenv("EVC_MOCK", raising=False)
    monkeypatch.delenv("EVC_INLINE_JOBS", raising=False)
    monkeypatch.delenv("EVC_KEEP_FRAMES", raising=False)
    import app.factory as factory
    from app.logging_setup import reset_logging
    from app.paths import reset_paths_cache
    from app.services.manager import reset_manager

    monkeypatch.setattr(factory, "load_project_dotenv", lambda: None)
    monkeypatch.setattr("app.paths.load_project_dotenv", lambda: None)
    reset_logging()
    reset_paths_cache()
    reset_manager()
    yield
    reset_logging()
    reset_paths_cache()
    reset_manager()
