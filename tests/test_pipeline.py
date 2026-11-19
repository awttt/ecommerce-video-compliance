import asyncio
import hashlib
import subprocess
from pathlib import Path

import pytest

from app.core.ffmpeg import extract_frames, extract_one_frame, probe_video
from app.core.pipeline import JobCancelled, run_pipeline, run_selected_frames
from app.schemas import RawHit, RunConfig
from tests.media import ffmpeg_bin, has_encoder, make_video

pytestmark = pytest.mark.skipif(ffmpeg_bin() is None or not has_encoder("libx264"), reason="需要带 libx264 的 ffmpeg")


def _config(**overrides) -> RunConfig:
    data = dict(fps=1, concurrency=2, max_width=640, jpeg_q=6, mock=True, api_key="", model="glm-5.3-flash")
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


def _ticks(path: Path, seconds: float = 2.2) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg_bin(),
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x240:rate=30:duration={seconds}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


async def _collect(video: Path, directory: Path, fps: float, duration: float):
    frames = []
    async for frame in extract_frames(
        video,
        directory,
        fps=fps,
        max_edge=320,
        jpeg_q=2,
        duration=duration,
        cancel=asyncio.Event(),
    ):
        frames.append((frame.start_sec, hashlib.md5(Path(frame.path).read_bytes()).hexdigest()))
    return frames


async def test_two_fps_includes_the_one_fps_frames(tmp_path: Path):
    video = tmp_path / "ticks.mp4"
    _ticks(video)
    duration = (await probe_video(video)).duration
    one = await _collect(video, tmp_path / "one", 1, duration)
    two = await _collect(video, tmp_path / "two", 2, duration)
    assert [start for start, _ in one] == [0.0, 1.0, 2.0]
    assert [start for start, _ in two] == [0.0, 0.5, 1.0, 1.5, 2.0]
    assert len({digest for _, digest in one}) == len(one)
    by_time = {start: digest for start, digest in two}
    for start, digest in one:
        assert by_time[start] == digest
    again = await extract_one_frame(
        video,
        tmp_path / "retry.jpg",
        index=3,
        fps=2,
        duration=duration,
        max_edge=320,
        jpeg_q=2,
        cancel=asyncio.Event(),
    )
    assert again.start_sec == 1.0
    assert hashlib.md5(Path(again.path).read_bytes()).hexdigest() == by_time[1.0]


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


def _price(frame) -> RawHit:
    return RawHit(
        start_sec=frame.start_sec,
        end_sec=frame.end_sec,
        category="价格",
        text="¥199",
        position="画面右下角价格贴纸",
        confidence="确定",
        reason="这一帧写着价格",
    )


class OneFail:
    async def recognize(self, frame):
        if frame.index == 1:
            raise RuntimeError("模型返回 HTTP 503：Service temporarily unavailable")
        return [_price(frame)]

    async def aclose(self):
        return None


class WatchIndex:
    def __init__(self):
        self.indexes: list[int] = []

    async def recognize(self, frame):
        self.indexes.append(frame.index)
        return [_price(frame)]

    async def aclose(self):
        return None


async def test_partial_failure_records_the_frame_index(tmp_path: Path):
    video = tmp_path / "a.mp4"
    make_video(video, 2)
    work = tmp_path / "frames"
    seen = []

    async def on_progress(progress):
        seen.append(progress)

    findings = await run_pipeline(video, work, _config(fps=1, concurrency=1), OneFail(), asyncio.Event(), on_progress)
    assert [item.category for item in findings] == ["解析失败", "价格"]
    assert findings[0].note == "解析失败，需要人工审核"
    assert findings[0].start_sec == 0
    assert findings[0].end_sec == 1
    assert "503" in findings[0].reason
    assert findings[1].text == "¥199"
    assert seen[-1].failed_frames == 1
    assert seen[-1].failed_indexes == [1]
    assert "503" in seen[-1].last_error
    assert not work.exists()


async def test_selected_retry_only_touches_the_listed_frame(tmp_path: Path):
    video = tmp_path / "a.mp4"
    make_video(video, 2)
    work = tmp_path / "frames"
    watch = WatchIndex()
    findings, failed, error = await run_selected_frames(
        video,
        work,
        _config(fps=1, concurrency=1),
        watch,
        asyncio.Event(),
        _noop,
        None,
        [2],
        recognized_base=1,
        frames_estimated=2,
        frames_extracted=2,
    )
    assert watch.indexes == [2]
    assert findings and findings[0].text == "¥199"
    assert failed == []
    assert error == ""
    assert not work.exists()


async def test_selected_retry_reports_a_frame_that_fails_again(tmp_path: Path):
    video = tmp_path / "a.mp4"
    make_video(video, 2)
    work = tmp_path / "frames"
    findings, failed, error = await run_selected_frames(
        video,
        work,
        _config(fps=1, concurrency=1),
        Boom(),
        asyncio.Event(),
        _noop,
        None,
        [1],
    )
    assert len(findings) == 1
    assert findings[0].category == "解析失败"
    assert findings[0].note == "解析失败，需要人工审核"
    assert findings[0].start_sec == 0
    assert "boom" in findings[0].reason
    assert failed == [1]
    assert "boom" in error
    assert not work.exists()


async def test_pipeline_failure_still_cleans_frames(tmp_path: Path):
    video = tmp_path / "a.mp4"
    make_video(video, 1)
    work = tmp_path / "frames"
    with pytest.raises(RuntimeError, match="全部分帧识别失败"):
        await run_pipeline(video, work, _config(concurrency=1), Boom(), asyncio.Event(), _noop)
    assert not work.exists()


class Hold:
    def __init__(self, resume: asyncio.Event):
        self.resume = resume
        self.calls = 0

    async def recognize(self, frame):
        self.calls += 1
        if self.calls == 1:
            self.resume.clear()
        return []

    async def aclose(self):
        return None


async def test_pipeline_pause_holds_the_next_frame(tmp_path: Path):
    video = tmp_path / "a.mp4"
    make_video(video, 3)
    work = tmp_path / "frames"
    resume = asyncio.Event()
    resume.set()
    hold = Hold(resume)

    async def release():
        while hold.calls < 1:
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.4)
        assert hold.calls == 1
        resume.set()

    releaser = asyncio.create_task(release())
    try:
        await run_pipeline(
            video,
            work,
            _config(fps=1, concurrency=1),
            hold,
            asyncio.Event(),
            _noop,
            resume=resume,
        )
        await releaser
    finally:
        if not releaser.done():
            releaser.cancel()
    assert hold.calls >= 2
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
