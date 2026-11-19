"""底在动时，光流对齐后仍能留下只出现一帧的笔画。"""

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("cv2")
pytest.importorskip("numpy")

import numpy as np

from app.core.ffmpeg import probe_video, scan_flashes
from app.core.flash import FlashSpan
from app.core.motion import MotionHit, MotionScanner, crop_fractions, magnify_flash, stamp_crops
from tests.media import ffmpeg_bin, has_encoder

_HEIGHT = 180
_WIDTH = 180


def _texture(shift: int, *, marks: bool = False, cut: bool = False) -> bytes:
    yy, xx = np.mgrid[0:_HEIGHT, 0:_WIDTH]
    if cut:
        image = np.full((_HEIGHT, _WIDTH), 20, np.uint8)
    else:
        image = ((np.sin((xx + shift) / 5.0) * np.sin(yy / 7.0) + 1) * 90 + 30).astype(np.uint8)
        if marks:
            image = image.copy()
            image[70:72, 40:110] = 250
            image[80:82, 40:110] = 250
            image[90:92, 55:85] = 250
            image[70:100, 60:62] = 250
            image[70:100, 88:90] = 250
    return image.tobytes()


def _scan(marked_at: int | None = None, cut_at: int | None = None) -> list[tuple[int, int]]:
    scanner = MotionScanner(_WIDTH, _HEIGHT)
    for index in range(20):
        scanner.push(
            _texture(
                index * 12,
                marks=marked_at == index,
                cut=cut_at == index,
            )
        )
    return [(item.index, item.score) for item in scanner.finish()]


def test_one_frame_strokes_on_a_moving_texture_are_found():
    found = _scan(marked_at=10)
    assert [index for index, _score in found] == [10]


def test_a_moving_texture_without_strokes_stays_under_the_threshold():
    assert _scan() == []


def test_a_scene_cut_is_not_a_glyph():
    assert _scan(cut_at=10) == []


def test_crop_around_the_peak_keeps_the_faint_side_and_stays_inside():
    left, top, width, height = crop_fractions(222, 317, 360, 640)
    assert 0 <= left and left + width <= 1
    assert 0 <= top and top + height <= 1
    # 峰值在「元」上。窗口要向左多留一截，淡的「9.9」才在里面。
    assert left * 720 <= 444 - 140
    assert (left + width) * 720 >= 444 + 40
    assert top * 1280 <= 634 - 40
    assert (top + height) * 1280 >= 634 + 40


def test_crop_at_the_corner_stays_inside():
    left, top, width, height = crop_fractions(0, 0, 360, 640)
    assert left == 0 and top == 0
    assert width > 0 and height > 0
    assert left + width <= 1 and top + height <= 1


def test_stamp_crops_only_marks_the_matching_frame():
    spans = [
        FlashSpan(0.633, 0.667, 19, 104),
        FlashSpan(3.5, 3.533, 105, 90),
    ]
    stamped = stamp_crops(spans, [MotionHit(19, 104, 222, 317)], 360, 640)
    assert stamped[0].crop is not None
    assert stamped[1].crop is None
    assert stamped[1].source_index == 105


def test_magnify_replaces_the_frame_with_the_crop(tmp_path: Path):
    import cv2

    image = np.zeros((1280, 720, 3), np.uint8)
    image[534:734, 284:604] = (255, 255, 255)
    path = tmp_path / "flash.jpg"
    cv2.imwrite(str(path), image)
    magnify_flash(path, crop_fractions(222, 317, 360, 640))
    out = cv2.imread(str(path))
    assert out is not None
    assert max(out.shape[:2]) >= 900
    assert float(out.mean()) > 200


def _moving_video(path: Path) -> None:
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
            "nullsrc=s=720x360:r=30:d=1",
            "-vf",
            "geq=lum='128+100*sin(X/8)*sin(Y/11)',"
            "crop=360:180:x='min(12*n,300)':y=40,"
            "scale=360:180,"
            "drawbox=enable='eq(n,15)':x=40:y=70:w=70:h=2:color=white:t=fill,"
            "drawbox=enable='eq(n,15)':x=40:y=82:w=70:h=2:color=white:t=fill,"
            "drawbox=enable='eq(n,15)':x=60:y=70:w=2:h=30:color=white:t=fill,"
            "drawbox=enable='eq(n,15)':x=88:y=70:w=2:h=30:color=white:t=fill",
            "-an",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


@pytest.mark.skipif(ffmpeg_bin() is None or not has_encoder("libx264"), reason="需要带 libx264 的 ffmpeg")
async def test_encoded_moving_strokes_are_returned_by_scan(tmp_path: Path):
    video = tmp_path / "move.mp4"
    _moving_video(video)
    probe = await probe_video(video)
    spans = await scan_flashes(
        video,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1,
        cancel=asyncio.Event(),
        motion_enabled=True,
    )
    matched = [item for item in spans if item.source_index == 15]
    assert matched and matched[0].crop is not None
    plain = await scan_flashes(
        video,
        width=probe.width,
        height=probe.height,
        fps=probe.fps,
        duration=probe.duration,
        sample_fps=1,
        cancel=asyncio.Event(),
    )
    assert all(item.crop is None for item in plain)
