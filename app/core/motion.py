"""底在动时，找只在一帧里出现的字。

小块亮度比较在底不动时够用。手和胶带一直在动时，反光、边缘每帧都在变，
一帧贴字和它们分不开。这里先用光流把前后两帧对齐到当前帧，再逐像素看：

- 当前帧比对齐后的前帧、后帧都亮（或都暗）；
- 对齐后的前帧和后帧彼此接近。

两条都满足，才算这一帧多出来的东西。光流对不准、字幕逐字变色、
镜头切换，前后两帧本身就对不上，第二条会把它们压掉。
剩下的残差只留笔画粗细的部分，实心的大块（遮挡、反光）去掉，
再看一小块窗口里笔画有多密。

依赖 numpy 和 opencv。没装时跳过，只用小块亮度比较。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from pathlib import Path

from app.core.flash import FlashRun, FlashSpan

log = logging.getLogger(__name__)

try:
    import cv2
    import numpy as np
except ImportError:  # pragma: no cover - 打包缺依赖时退回亮度比较
    cv2 = None
    np = None

# 下面的像素量按扫描图短边 360 定，其他尺寸按比例换算。
BASE_SHORT_EDGE = 360
# 扣掉前后帧自己的差以后，至少还要差这么多灰阶。
RESIDUAL = 45
# 比这更粗的实心块不算笔画。
STROKE = 4
# 统计笔画密度的窗口边长。一个价格字在短边 360 上大约这么大。
WINDOW = 16
# 窗口里至少有这么多笔画像素，才送去识别。
MIN_SCORE = 60
# 底在动时最多补这么多帧。
MAX_MOTION_FLASHES = 10
# 密度峰值常落在最清楚的那个字上。短边 360 时向两侧各留这些像素，
# 把旁边更淡的数字一起裁进来，再放大送给模型。
CROP_HALF_W = 80
CROP_HALF_H = 50
CROP_LONG_EDGE = 960


def available() -> bool:
    return cv2 is not None and np is not None


@dataclass(frozen=True)
class MotionHit:
    index: int
    score: int
    x: int
    y: int


class MotionScanner:
    """逐帧推进，只留最近三帧。"""

    def __init__(self, width: int, height: int) -> None:
        if not available():
            raise RuntimeError("缺少 numpy 或 opencv")
        self.width = width
        self.height = height
        scale = max(1e-6, min(width, height) / BASE_SHORT_EDGE)
        self._area = scale * scale
        self._stroke = max(3, int(round(STROKE * scale)))
        self._window = max(8, int(round(WINDOW * scale)))
        self._flow = cv2.DISOpticalFlow_create(cv2.DISOPTICAL_FLOW_PRESET_FAST)
        grid_x, grid_y = np.meshgrid(
            np.arange(width, dtype=np.float32),
            np.arange(height, dtype=np.float32),
        )
        self._grid_x = grid_x
        self._grid_y = grid_y
        self._recent: list[np.ndarray] = []
        self._count = 0
        self._hits: list[MotionHit] = []

    def push(self, frame: bytes) -> None:
        if len(frame) < self.width * self.height:
            return
        image = np.frombuffer(frame, np.uint8, self.width * self.height).reshape(self.height, self.width).copy()
        self._recent.append(image)
        self._count += 1
        if len(self._recent) > 3:
            del self._recent[0]
        if len(self._recent) == 3:
            hit = self._score(self._count - 2)
            if hit is not None:
                self._hits.append(hit)

    def finish(self) -> list[MotionHit]:
        found = sorted(self._hits, key=lambda item: item.score, reverse=True)
        return found[:MAX_MOTION_FLASHES]

    def _aligned(self, cur: np.ndarray, other: np.ndarray) -> np.ndarray:
        flow = self._flow.calc(cur, other, None)
        warped = cv2.remap(
            other,
            self._grid_x + flow[..., 0],
            self._grid_y + flow[..., 1],
            cv2.INTER_LINEAR,
            borderMode=cv2.BORDER_REPLICATE,
        )
        return warped.astype(np.int16)

    def _score(self, index: int) -> MotionHit | None:
        before, middle, after = self._recent
        cur = middle.astype(np.int16)
        prev = self._aligned(middle, before)
        nxt = self._aligned(middle, after)
        stayed = np.abs(prev - nxt)
        brighter = np.minimum(cur - prev, cur - nxt) - stayed
        darker = np.minimum(prev - cur, nxt - cur) - stayed
        mask = (np.maximum(brighter, darker) >= RESIDUAL).astype(np.uint8)
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
        solid = cv2.morphologyEx(mask, cv2.MORPH_OPEN, np.ones((self._stroke, self._stroke), np.uint8))
        strokes = (mask & (1 - solid)).astype(np.float32)
        density = cv2.boxFilter(strokes, -1, (self._window, self._window), normalize=False)
        _, peak, _, (x, y) = cv2.minMaxLoc(density)
        score = int(round(peak / self._area))
        if score < MIN_SCORE:
            return None
        return MotionHit(index=index, score=score, x=int(x), y=int(y))


def motion_runs(hits: list[MotionHit]) -> list[FlashRun]:
    return [FlashRun(item.index, 1, item.score) for item in hits]


def crop_fractions(x: int, y: int, scan_w: int, scan_h: int) -> tuple[float, float, float, float]:
    """峰值周围一块，用相对整帧的左、上、宽、高。贴着边缘时收到画面里。"""
    if scan_w < 2 or scan_h < 2:
        return (0.0, 0.0, 1.0, 1.0)
    scale = min(scan_w, scan_h) / BASE_SHORT_EDGE
    half_w = max(8, int(round(CROP_HALF_W * scale)))
    half_h = max(8, int(round(CROP_HALF_H * scale)))
    x0 = max(0, x - half_w)
    y0 = max(0, y - half_h)
    x1 = min(scan_w, x + half_w)
    y1 = min(scan_h, y + half_h)
    return (x0 / scan_w, y0 / scan_h, (x1 - x0) / scan_w, (y1 - y0) / scan_h)


def stamp_crops(
    spans: list[FlashSpan],
    hits: list[MotionHit],
    scan_w: int,
    scan_h: int,
) -> list[FlashSpan]:
    """按源帧号把裁切框写上。对不上的保持整帧。"""
    located = {hit.index: hit for hit in hits}
    stamped: list[FlashSpan] = []
    for span in spans:
        hit = located.get(span.source_index)
        if hit is None:
            stamped.append(span)
            continue
        stamped.append(replace(span, crop=crop_fractions(hit.x, hit.y, scan_w, scan_h)))
    return stamped


def magnify_flash(path: Path, crop: tuple[float, float, float, float]) -> None:
    """把裁切放大后写回同一张图。失败由调用方改回整帧。"""
    if not available():
        raise RuntimeError("缺少 numpy 或 opencv")
    image = cv2.imread(str(path))
    if image is None or image.size == 0:
        raise RuntimeError("闪帧图片读不出来")
    height, width = image.shape[:2]
    x0 = max(0, min(width - 2, int(round(crop[0] * width))))
    y0 = max(0, min(height - 2, int(round(crop[1] * height))))
    x1 = max(x0 + 2, min(width, int(round((crop[0] + crop[2]) * width))))
    y1 = max(y0 + 2, min(height, int(round((crop[1] + crop[3]) * height))))
    piece = image[y0:y1, x0:x1]
    long_edge = max(piece.shape[0], piece.shape[1])
    if long_edge < 2:
        raise RuntimeError("闪帧裁切是空的")
    scale = CROP_LONG_EDGE / long_edge
    if scale > 1.05:
        piece = cv2.resize(piece, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    if not cv2.imwrite(str(path), piece, [int(cv2.IMWRITE_JPEG_QUALITY), 90]):
        raise RuntimeError("闪帧放大后写不回去")
