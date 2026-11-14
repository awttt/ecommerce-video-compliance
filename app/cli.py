"""命令行跑同一条识别流水线，方便不打开窗口时验证。"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from app.paths import load_project_dotenv
from app.schemas import MAX_CONCURRENCY
from app.services.manager import AuthRequired, get_manager


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="检测一个本地视频里的价格、规格、品牌和口播夸大宣传")
    parser.add_argument("video", type=Path, help="视频文件路径")
    parser.add_argument("--fps", type=float, default=None, help="抽帧 FPS，0.5 到 2")
    parser.add_argument("--concurrency", type=int, default=None, help=f"同时请求模型的数量，1 到 {MAX_CONCURRENCY}")
    parser.add_argument("--mock", action="store_true", help="不调用模型，返回固定样例命中")
    parser.add_argument("--no-asr", action="store_true", help="关闭这一次的口播检测")
    parser.add_argument("--json", dest="json_path", type=Path, default=None, help="把结果写入这个 JSON 文件")
    return parser


async def _scan(args: argparse.Namespace) -> int:
    video = args.video.expanduser().resolve()
    if not video.is_file():
        print(f"找不到视频：{video}", file=sys.stderr)
        return 2
    if args.fps is not None and not 0.5 <= args.fps <= 2:
        print("抽帧 FPS 需要在 0.5 到 2 之间", file=sys.stderr)
        return 2
    if args.concurrency is not None and not 1 <= args.concurrency <= MAX_CONCURRENCY:
        print(f"并发数需要在 1 到 {MAX_CONCURRENCY} 之间", file=sys.stderr)
        return 2
    load_project_dotenv()
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    from app.logging_setup import setup_logging

    setup_logging()
    manager = get_manager()
    await manager.init()
    try:
        try:
            manager.ensure_logged_in()
        except AuthRequired as exc:
            print(str(exc), file=sys.stderr)
            return 1
        state = await manager.create_job(
            video,
            video.name,
            fps=args.fps,
            concurrency=args.concurrency,
            mock=True if args.mock else None,
            asr=False if args.no_asr else None,
        )
        task = manager._tasks.get(state.id)
        while task is not None and not task.done():
            speech = f" | {state.speech_message}" if state.speech_message else ""
            print(f"{state.message}{speech}  {state.progress:.0f}%", flush=True)
            await asyncio.sleep(0.5)
        if task is not None:
            await task
        public = state.to_public()
        payload = [item.export_dict() for item in public.findings]
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        print(text)
        if args.json_path is not None:
            args.json_path.write_text(text, encoding="utf-8")
        if public.status != "done":
            print(public.message, file=sys.stderr)
            return 1
        return 0
    finally:
        await manager.shutdown()


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    raise SystemExit(asyncio.run(_scan(args)))


if __name__ == "__main__":
    main()
