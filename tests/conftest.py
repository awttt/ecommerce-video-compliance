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
    monkeypatch.delenv("EVC_MCP", raising=False)
    monkeypatch.delenv("EVC_RELAY_GROUP_NAME", raising=False)
    monkeypatch.delenv("EVC_RELAY_GROUP_ID", raising=False)
    monkeypatch.delenv("EVC_IMAGE_PROVIDER", raising=False)
    monkeypatch.delenv("EVC_IMAGE_RELAY_GROUP_NAME", raising=False)
    monkeypatch.delenv("EVC_IMAGE_RELAY_GROUP_ID", raising=False)
    monkeypatch.delenv("EVC_IMAGE_GATEWAY_URL", raising=False)
    monkeypatch.delenv("EVC_IMAGE_PREVIEW_LOGIN", raising=False)
    monkeypatch.setenv("EVC_IMAGE_MOCK_DELAY", "0")
    from app.core.image_contract import reset_image_contract
    from app.services.image_jobs import reset_image_jobs

    reset_image_contract()
    reset_image_jobs()
    import app.factory as factory
    from app.logging_setup import reset_logging
    from app.paths import reset_paths_cache
    from app.mcp_server import reset_batches
    from app.services.manager import reset_manager

    reset_batches()
    monkeypatch.setattr(factory, "load_project_dotenv", lambda: None)
    monkeypatch.setattr("app.paths.load_project_dotenv", lambda: None)
    reset_logging()
    reset_paths_cache()
    reset_manager()
    yield
    reset_batches()
    reset_image_contract()
    reset_image_jobs()
    reset_logging()
    reset_paths_cache()
    reset_manager()
