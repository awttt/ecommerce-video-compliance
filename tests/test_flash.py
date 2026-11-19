"""一帧贴字：小块亮度变了又变回去，时间用这一帧自己的起止。"""

import asyncio
from pathlib import Path

import pytest

from app.core.aggregate import aggregate
from app.core.ffmpeg import extract_source_frames, probe_video, scan_flashes
from app.core.flash import (
    FLASH_INDEX_BASE,
    FlashRun,
    FlashScanner,
    block_means,
    covered_by_samples,
    flash_frame_index,
    flash_time,
    spans_from_runs,
)
from app.core.pipeline import drop_retried_reviews, manual_review_hit, run_pipeline
from app.schemas import MANUAL_REVIEW_NOTE, Finding, RawHit, RunConfig
from tests.media import ffmpeg_bin, has_encoder, make_video

pytestmark_ffmpeg = pytest.mark.skipif(
    ffmpeg_bin() is None or not has_encoder("libx264"),
    reason="需要带 libx264 的 ffmpeg",
)


def _means(value: int, width: int = 32, height: int = 32) -> list[int]:
    return block_means(bytes([value]) * (width * height), width, height)


def _paint(
    width: int,
    height: int,
    x: int,
    y: int,
    box_w: int,
    box_h: int,
    value: int,
    bg: int = 20,
) -> list[int]:
    frame = bytearray(bytes([bg]) * (width * height))
    for row in range(y, min(height, y + box_h)):
        frame[row * width + x : row * width + x + box_w] = bytes([value]) * min(box_w, width - x)
    return block_means(bytes(frame), width, height)


def _finish(frames: list[list[int]]) -> list[FlashRun]:
    scanner = FlashScanner()
    for means in frames:
        scanner.push(means)
    return scanner.finish()


def test_motion_spans_are_kept_ahead_of_brightness_spans():
    from app.core.flash import FlashSpan, merge_flash_spans

    motion = [FlashSpan(0.6, 0.633, 18, 90), FlashSpan(1.0, 1.033, 30, 80)]
    bright = [FlashSpan(0.6, 0.633, 18, 200), FlashSpan(3.5, 3.533, 106, 70)]
    merged = merge_flash_spans(motion, bright, limit=3)
    assert [item.source_index for item in merged] == [18, 30, 106]


def test_one_frame_overlay_is_found_when_the_rest_of_the_picture_stays():
    frames = [_means(30)] * 10
    frames[5] = _means(220)
    found = _finish(frames)
    assert [(item.start_index, item.length) for item in found] == [(5, 1)]
    assert found[0].score >= 24


def test_the_frame_after_a_hit_is_still_checked():
    """相邻两帧各有一块不同的字时，两帧都要留下。"""
    still = [30, 30] * 4
    left = still.copy()
    left[0] = left[1] = 220
    right = still.copy()
    right[2] = right[3] = 220
    frames = [still] * 10
    frames[5] = left
    frames[6] = right
    found = _finish(frames)
    assert sorted((item.start_index, item.length) for item in found) == [(5, 1), (6, 1)]


def test_two_frame_overlay_stays_one_run():
    frames = [_means(30)] * 10
    frames[5] = _means(220)
    frames[6] = _means(210)
    found = _finish(frames)
    assert [(item.start_index, item.length) for item in found] == [(5, 2)]


def test_a_cut_and_a_change_that_stays_are_not_flashes():
    cut = [_means(20)] * 5 + [_means(200)] * 5
    assert _finish(cut) == []
    staying = [_means(20)] * 4 + [_means(220)] * 6
    assert _finish(staying) == []


def test_a_few_bright_or_dark_pixels_count_when_the_block_average_barely_moves():
    """几个字盖不住整块。整块平均几乎不动，最亮或最暗的一段仍要能被看见。"""
    bright_still = _paint(64, 64, 0, 0, 0, 0, 40, bg=40)
    bright = _paint(64, 64, 6, 6, 8, 4, 255, bg=40)
    frames = [bright_still] * 8
    frames[4] = bright
    found = _finish(frames)
    assert [(item.start_index, item.length) for item in found] == [(4, 1)]
    assert found[0].score >= 24

    dark_still = _paint(64, 64, 0, 0, 0, 0, 180, bg=180)
    dark = _paint(64, 64, 4, 4, 8, 5, 0, bg=180)
    frames = [dark_still] * 8
    frames[4] = dark
    found = _finish(frames)
    assert [(item.start_index, item.length) for item in found] == [(4, 1)]
    assert found[0].score >= 24


def test_corner_block_counts_even_when_it_is_smaller_than_the_grid():
    still = _paint(40, 40, 0, 0, 0, 0, 20)
    flashed = _paint(40, 40, 32, 32, 8, 8, 250)
    frames = [still] * 8
    frames[3] = flashed
    found = _finish(frames)
    assert [(item.start_index, item.length) for item in found] == [(3, 1)]


def test_sample_grid_skips_a_flash_the_normal_pass_already_sees():
    assert covered_by_samples(0.0, 0.033, 1) is True
    assert covered_by_samples(0.5, 0.533, 1) is False
    assert covered_by_samples(0.5, 0.533, 2) is True
    times = [index / 30 for index in range(30)]
    runs = [FlashRun(0, 1, 80), FlashRun(15, 1, 180)]
    spans = spans_from_runs(runs, times, fps=30, duration=1, sample_fps=1)
    assert len(spans) == 1
    assert spans[0].source_index == 15
    assert spans[0].start_sec == 0.5
    assert spans[0].end_sec == pytest.approx(0.533, abs=0.01)
    assert flash_frame_index(spans[0].start_sec) == FLASH_INDEX_BASE + 500
    assert flash_time(FLASH_INDEX_BASE + 500) == 0.5
    assert flash_time(2) is None


def test_retry_drops_a_flash_review_by_its_start():
    flashed = aggregate(
        [
            manual_review_hit(0.5, 0.533, "模型请求超时"),
            manual_review_hit(1, 2, "模型请求超时"),
        ]
    )
    assert flashed[0].time_range == "00:00.50-00:00.53"
    kept = drop_retried_reviews(flashed, [FLASH_INDEX_BASE + 500], fps=1, duration=10)
    assert [(item.start_sec, item.end_sec) for item in kept] == [(1.0, 2.0)]
    untouched = drop_retried_reviews(
        [Finding(
            time_range="00:01-00:02",
            category="价格",
            text="9.9元",
            position="画面中",
            confidence="确定",
            reason="看得见",
            start_sec=1,
            end_sec=2,
        )],
        [FLASH_INDEX_BASE + 500],
        fps=1,
        duration=10,
    )
    assert len(untouched) == 1
    assert kept[0].note == MANUAL_REVIEW_NOTE


def _one_frame_box(
    path: Path,
    *,
    size: str,
    color: str,
    box: str,
    at: int,
    seconds: float = 2,
    rate: int = 30,
) -> None:
    binary = ffmpeg_bin()
    if binary is None:
        raise RuntimeError("没有 ffmpeg")
    import subprocess

    subprocess.run(
        [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"color=c={color}:s={size}:r={rate}:d={seconds}",
            "-vf",
            f"drawbox=enable='eq(n,{at})':{box}:t=fill",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def _flash_video(path: Path) -> None:
    binary = ffmpeg_bin()
    if binary is None:
        raise RuntimeError("没有 ffmpeg")
    import subprocess

    subprocess.run(
        [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x2244aa:s=640x360:r=30:d=1",
            "-vf",
            "drawbox=enable='eq(n,15)':x=80:y=80:w=180:h=90:color=white:t=fill",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


@pytestmark_ffmpeg
async def test_encoded_one_frame_box_is_found_and_a_still_video_is_not(tmp_path: Path):
    still = tmp_path / "still.mp4"
    make_video(still, 1)
    probed = await probe_video(still)
    none_found = await scan_flashes(
        still,
        width=probed.width,
        height=probed.height,
        fps=probed.fps,
        duration=probed.duration,
        sample_fps=1,
        cancel=asyncio.Event(),
    )
    assert none_found == []

    flashed = tmp_path / "flash.mp4"
    _flash_video(flashed)
    probe = await probe_video(flashed)
    spans = await scan_flashes(
        flashed,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1,
        cancel=asyncio.Event(),
    )
    assert len(spans) == 1
    assert spans[0].start_sec == pytest.approx(0.5, abs=0.04)
    assert spans[0].end_sec - spans[0].start_sec < 0.12
    frames = await extract_source_frames(
        flashed,
        tmp_path / "frames",
        spans,
        max_edge=640,
        jpeg_q=6,
        cancel=asyncio.Event(),
    )
    assert len(frames) == 1
    assert frames[0].index >= FLASH_INDEX_BASE
    assert Path(frames[0].path).stat().st_size > 0


@pytestmark_ffmpeg
async def test_encoded_small_sticker_between_samples_is_found(tmp_path: Path):
    """价格只有几十像素、只占一帧时，整块平均亮度不够，仍要抽到这一帧。"""
    cases = (
        ("yellow", "0x2244aa", "yellow"),
        ("black", "0xdddddd", "black"),
    )
    for name, color, box_color in cases:
        path = tmp_path / f"{name}.mp4"
        _one_frame_box(
            path,
            size="1280x720",
            color=color,
            box=f"x=400:y=300:w=48:h=18:color={box_color}",
            at=40,
        )
        probe = await probe_video(path)
        spans = await scan_flashes(
            path,
            width=probe.width,
            height=probe.height,
            fps=probe.fps,
            duration=probe.duration,
            sample_fps=1,
            cancel=asyncio.Event(),
        )
        assert len(spans) == 1, name
        assert spans[0].source_index == 40
        assert spans[0].start_sec == pytest.approx(40 / 30, abs=0.04)
        assert spans[0].end_sec - spans[0].start_sec < 0.08


@pytestmark_ffmpeg
async def test_encoded_static_detail_is_not_a_flash(tmp_path: Path):
    path = tmp_path / "detail.mp4"
    binary = ffmpeg_bin()
    assert binary is not None
    import subprocess

    subprocess.run(
        [
            binary,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "nullsrc=s=640x360:r=30:d=2",
            "-vf",
            "geq=lum='128+110*sin(X/2.5)*sin(Y/3.5)':cb=128:cr=128",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )
    probe = await probe_video(path)
    spans = await scan_flashes(
        path,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1,
        cancel=asyncio.Event(),
    )
    assert spans == []


class _Record:
    def __init__(self) -> None:
        self.frames: list[tuple[int, float, float]] = []

    async def recognize(self, frame):
        self.frames.append((frame.index, frame.start_sec, frame.end_sec))
        if frame.index < FLASH_INDEX_BASE:
            return []
        return [
            RawHit(
                start_sec=frame.start_sec,
                end_sec=frame.end_sec,
                category="价格",
                text="1.11元",
                position="画面中的白块",
                confidence="确定",
                reason="这一帧写着价格",
                source="画面",
            )
        ]

    async def aclose(self):
        return None


@pytestmark_ffmpeg
async def test_pipeline_sends_the_flash_frame_with_its_own_time(tmp_path: Path):
    video = tmp_path / "flash.mp4"
    _flash_video(video)
    work = tmp_path / "frames"
    recognizer = _Record()
    skipped = _Record()
    await run_pipeline(
        video,
        work,
        RunConfig(fps=1, concurrency=1, max_width=640, jpeg_q=6, mock=True, api_key="", model="glm-5.3-flash"),
        skipped,
        asyncio.Event(),
        lambda _progress: _noop(),
    )
    assert not any(item[0] >= FLASH_INDEX_BASE for item in skipped.frames)
    findings = await run_pipeline(
        video,
        work,
        RunConfig(
            fps=1,
            concurrency=1,
            max_width=640,
            jpeg_q=6,
            mock=True,
            api_key="",
            model="glm-5.3-flash",
            motion_enabled=True,
        ),
        recognizer,
        asyncio.Event(),
        lambda _progress: _noop(),
    )
    flashed = [item for item in recognizer.frames if item[0] >= FLASH_INDEX_BASE]
    assert len(flashed) == 1
    assert flashed[0][1] == pytest.approx(0.5, abs=0.04)
    assert flashed[0][2] - flashed[0][1] < 0.12
    assert len(findings) == 1
    assert findings[0].text == "1.11元"
    assert findings[0].start_sec == pytest.approx(0.5, abs=0.02)
    assert findings[0].time_range == "00:00.50-00:00.53"
    assert findings[0].end_sec - findings[0].start_sec < 0.12
    assert not work.exists()


async def _noop():
    return None
