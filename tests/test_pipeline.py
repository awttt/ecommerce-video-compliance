import asyncio
from pathlib import Path

import pytest

from app.core.pipeline import JobCancelled, run_pipeline
from app.schemas import RunConfig
from tests.media import ffmpeg_bin, has_encoder, make_video

pytestmark = pytest.mark.skipif(ffmpeg_bin() is None or not has_encoder("libx264"), reason="需要带 libx264 的 ffmpeg")


def _config(**overrides) -> RunConfig:
    data = dict(fps=1, concurrency=2, max_width=640, jpeg_q=6, mock=True, api_key="", model="deepseek-flash")
    data.update(overrides)
    return RunConfig(**data)


class Boom:
    async def recognize(self, frame):
        raise RuntimeError("boom")

    async def aclose(self):
        return None


class Slow:
    def __init__(self, cancel: asyncio.Event):
        self.cancel = cancel

    async def recognize(self, frame):
        await asyncio.sleep(30)
        return []

    async def aclose(self):
        self.cancel.set()


async def _noop(_progress):
    return None


async def test_pipeline_mock_cleans_frames(tmp_path: Path):
    from app.core.deepseek import MockRecognizer

    video = tmp_path / "素材 库" / "价格 演示.mp4"
    make_video(video, 2)
    work = tmp_path / "frames"
    findings = await run_pipeline(
        video,
        work,
        _config(),
        MockRecognizer(),
        asyncio.Event(),
        _noop,
    )
    assert findings
    assert findings[0].text == "¥199"
    assert findings[0].category == "价格"
    assert not work.exists()


async def test_pipeline_failure_still_cleans_frames(tmp_path: Path):
    video = tmp_path / "a.mp4"
    make_video(video, 1)
    work = tmp_path / "frames"
    with pytest.raises(RuntimeError, match="全部分帧识别失败"):
        await run_pipeline(video, work, _config(concurrency=1), Boom(), asyncio.Event(), _noop)
    assert not work.exists()


async def test_pipeline_cancel_stops_and_cleans(tmp_path: Path):
    video = tmp_path / "a.mp4"
    make_video(video, 2)
    work = tmp_path / "frames"
    cancel = asyncio.Event()

    async def killer():
        await asyncio.sleep(0.4)
        cancel.set()

    task = asyncio.create_task(killer())
    with pytest.raises(JobCancelled):
        await run_pipeline(video, work, _config(concurrency=1), Slow(cancel), cancel, _noop)
    await task
    assert not work.exists()
