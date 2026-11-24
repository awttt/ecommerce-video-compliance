"""启动本机 FastAPI，并用 pywebview 打开桌面窗口。"""

from __future__ import annotations

import argparse
import asyncio
import logging
import socket
import sys
import threading
import time
import webbrowser

import uvicorn
from fastapi import FastAPI

from app.console import cloud_base_url, console_dist, console_enabled, console_port, create_console_app
from app.core.desktop_save import create_desktop_window
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


def _start(app: FastAPI, host: str, port: int, name: str) -> tuple[uvicorn.Server, threading.Thread]:
    ensure_stdio()
    interactive = sys.stdout is not None and sys.stdout.isatty()
    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="info",
        access_log=False,
        use_colors=interactive,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name=name, daemon=True)
    thread.start()
    _wait_until_up(host, port, server, thread)
    return server, thread


def serve(host: str, port: int, console_url: str = "") -> tuple[uvicorn.Server, threading.Thread]:
    return _start(create_app(console_url), host, port, "evc-api")


def _port_free(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.2)
        return sock.connect_ex((host, port)) != 0


def start_console(host: str, tools_url: str) -> tuple[uvicorn.Server | None, threading.Thread | None, str]:
    """在另一个端口起控制台壳。页面打不开时也不影响审核工具。"""
    if not (console_dist() / "index.html").is_file():
        logging.getLogger("app.console").warning("还没有构建 admin/web/dist，管理控制台入口关闭")
        return None, None, ""
    port = console_port()
    if not _port_free(host, port):
        logging.getLogger("app.console").warning("端口 %s 已被占用，管理控制台入口关闭", port)
        return None, None, ""
    server, thread = _start(create_console_app(tools_url=tools_url), host, port, "evc-console")
    url = f"http://127.0.0.1:{port}/"
    logging.getLogger("app.console").info("管理控制台 %s 转发到 %s", url, cloud_base_url())
    return server, thread, url


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="御镜视频审核系统")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-window", action="store_true", help="只启动本地服务，不打开桌面窗口")
    parser.add_argument("--browser", action="store_true", help="用系统浏览器打开")
    parser.add_argument("--no-console", action="store_true", help="不启动嵌入的管理控制台")
    return parser


def main(argv: list[str] | None = None) -> None:
    ensure_stdio()
    configure_tls()
    load_project_dotenv()
    _configure_loop()
    args = build_parser().parse_args(argv)
    if args.host not in {"127.0.0.1", "localhost"}:
        raise SystemExit("只监听本机地址 127.0.0.1")
    console_server = None
    console_thread = None
    console_url = ""
    if not args.no_console and console_enabled():
        try:
            console_server, console_thread, console_url = start_console(
                args.host, f"http://127.0.0.1:{args.port}/"
            )
        except Exception:
            logging.getLogger("app.console").exception("管理控制台没有启动，审核工具照常使用")
    server, thread = serve(args.host, args.port, console_url)
    url = f"http://127.0.0.1:{args.port}"
    # 启动直接进控制台；工具端通过控制台菜单嵌在页面里。带 sso=1 时用工具端登录换会话。
    start_url = f"{console_url}?sso=1" if console_url else url
    print(f"服务已启动：{url}")
    if console_url:
        print(f"管理控制台：{console_url}")
    try:
        if args.browser:
            webbrowser.open(start_url)
        if args.no_window:
            thread.join()
            return
        try:
            import webview
        except ImportError:
            print("未安装 pywebview，已改为只启动本地服务。用浏览器打开上面的地址即可。")
            thread.join()
            return
        create_desktop_window(webview, start_url)
        webview.start(debug=False)
    finally:
        server.should_exit = True
        thread.join(timeout=8)
        if console_server is not None and console_thread is not None:
            console_server.should_exit = True
            console_thread.join(timeout=8)


if __name__ == "__main__":
    main()
