"""跨平台「在文件管理器中打开文件位置」工具（pdf_list_panel 与 zotero_panel 共用）。"""

from __future__ import annotations

import os
import subprocess
import sys


def open_file_location(path: str, parent=None) -> None:
    """在系统文件管理器中打开文件所在位置并选中该文件。

    parent: 可选 QWidget，仅在两级打开方式都失败时用于弹出警告框。
    """
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", path])
            return
        if sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
            return
        subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except OSError:
        dirname = os.path.dirname(path) or "."
        try:
            if os.name == "nt":
                os.startfile(dirname)  # noqa: S606
            else:
                subprocess.Popen([sys.platform == "darwin" and "open" or "xdg-open", dirname])
        except OSError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(parent, "打开失败", f"无法打开文件位置：{e}")
