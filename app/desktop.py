"""启动本机 FastAPI，并用 pywebview 打开桌面窗口。"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
import threading
import time
import webbrowser

import uvicorn

from app.core.relay import configure_tls
from app.factory import create_app
from app.logging_setup import ensure_stdio
from app.paths import load_project_dotenv


def _configure_loop() -> None:
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())


def _wait_until_up(host: str, port: int, server: uvicorn.Server, thread: threading.Thread) -> None:
    deadline = time.time() + 20
    while time.time() < deadline:
        if not thread.is_alive():
            raise RuntimeError("本地服务启动失败，请查看 data/logs/app.log")
        with socket.socket() as sock:
            sock.settimeout(0.2)
            if sock.connect_ex((host, port)) == 0:
                return
        time.sleep(0.05)
    server.should_exit = True
    raise RuntimeError("本地服务启动超时")


def serve(host: str, port: int) -> tuple[uvicorn.Server, threading.Thread]:
    ensure_stdio()
    interactive = sys.stdout is not None and sys.stdout.isatty()
    config = uvicorn.Config(
        create_app(),
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        use_colors=interactive,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="evc-api", daemon=True)
    thread.start()
    _wait_until_up(host, port, server, thread)
    return server, thread


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="电商视频素材合规检测")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-window", action="store_true", help="只启动本地服务，不打开桌面窗口")
    parser.add_argument("--browser", action="store_true", help="用系统浏览器打开")
    return parser


def main(argv: list[str] | None = None) -> None:
    ensure_stdio()
    configure_tls()
    load_project_dotenv()
    _configure_loop()
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("只监听本机地址 127.0.0.1")
    server, thread = serve(args.host, args.port)
    url = f"http://127.0.0.1:{args.port}"
    print(f"服务已启动：{url}")
    try:
        if args.browser:
            webbrowser.open(url)
        if args.no_window:
            thread.join()
            return
        try:
            import webview
        except ImportError:
            print("未安装 pywebview，已改为只启动本地服务。用浏览器打开上面的地址即可。")
            thread.join()
            return
        webview.create_window(
            "电商视频素材合规检测",
            url,
            width=1360,
            height=860,
            min_size=(1100, 720),
            text_select=True,
        )
        webview.start(debug=False)
    finally:
        server.should_exit = True
        thread.join(timeout=8)


if __name__ == "__main__":
    main()
