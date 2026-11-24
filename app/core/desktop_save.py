"""桌面窗口暴露给页面的保存接口。不依赖审核服务，测试可以直接调用。"""

from __future__ import annotations

# 与 pywebview.SAVE_DIALOG 相同。测试不导入 webview，所以把数值写在这里。
SAVE_DIALOG = 30


class DesktopApi:
    """页面只能通过这个接口把已经生成的图片存到用户选择的位置。"""

    def save_generated_image(self, url: str, filename: str) -> dict:
        from app.core.image_save import file_filter_for, save_generated_image

        def choose(suggested: str) -> object:
            window = _save_window()
            return window.create_file_dialog(
                SAVE_DIALOG,
                directory="",
                allow_multiple=False,
                save_filename=suggested,
                file_types=(file_filter_for(suggested),),
            )

        return save_generated_image(url, filename, choose_path=choose)


def _save_window():
    import webview

    if not webview.windows:
        raise RuntimeError("窗口还没准备好")
    return webview.windows[0]


def create_desktop_window(webview_module, start_url: str):
    return webview_module.create_window(
        "御镜视频审核系统",
        start_url,
        width=1360,
        height=860,
        min_size=(1100, 720),
        text_select=True,
        js_api=DesktopApi(),
    )
