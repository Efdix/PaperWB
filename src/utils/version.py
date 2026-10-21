"""版本号单一来源 + 语义化版本比较。

全项目只有本文件出现版本字面量：``main.py`` 用它设置 QApplication 版本，
安装包构建脚本（``installer/build_installer.ps1``）用正则从这里抓版本号给
ISCC 的 ``/DMyAppVersion``，更新检查用它和远端清单比对。改动版本只需改一行。

只依赖标准库（无 PySide6 导入期依赖），因此无头自检、构建脚本、更新检查
线程都能安全导入。
"""

from __future__ import annotations

import re

# ---- 唯一版本字面量（发行前改这里）----

APP_NAME = "PaperWB"

APP_VERSION = "0.2.0"


def current_version() -> str:
    """当前应用版本：优先 QApplication（仅当它确实承载本应用的版本时），否则回退常量。

    QApplication 的版本由 ``main.py`` 用 ``APP_VERSION`` 设置，正常路径两者相同；
    但**不能无条件采信** ``applicationVersion()``：QApplication 已创建却没调过
    ``setApplicationVersion``（测试代码、第三方宿主、嵌入式调用）时，Qt 返回的是
    它自己的版本号（如 ``3.11.16150.1013``），拿它去和远端版本比对会得出错误结论。
    因此要求「应用名匹配 **且** 版本号形如本应用的 x.y.z」才采信——清空版本后
    Qt 同样会回落到自身版本，仅校验应用名不足以挡住。

    构建脚本与无头自检不创建 QApplication，故必须能回退常量。
    """
    try:
        from PySide6.QtWidgets import QApplication

        app = QApplication.instance()
        if app is not None and app.applicationName() == APP_NAME:
            v = str(app.applicationVersion() or "").strip()
            # 必须是本应用的版本形态（x.y.z，可带 v 前缀/预发布后缀），
            # 借此排除 Qt 自身的四段版本号
            if v and parse_version(v) is not None and len(v.lstrip("vV").split("-")[0].split(".")) <= 3:
                return v
    except Exception:  # noqa: BLE001 — 导入/取值失败一律回退常量
        pass
    return APP_VERSION


# ---- 语义化比较 ----

_PARTS_RE = re.compile(r"^[vV]?\s*(\d+(?:\.\d+)*)\s*(?:[-+](.+))?$")


def parse_version(text: str) -> tuple[tuple[int, ...], str] | None:
    """解析版本字符串 → ((主版本号元组), 预发布标记)；无法解析返回 None。

    容忍 ``v`` 前缀、位数不等（``0.2`` / ``0.2.0.0``）、``-beta`` 类预发布后缀
    （预发布一律低于同号正式版）。解析失败返回 None，由调用方决定降级行为——
    更新检查绝不为一个畸形版本号弹窗。
    """
    if not text:
        return None
    m = _PARTS_RE.match(str(text).strip())
    if not m:
        return None
    try:
        nums = tuple(int(p) for p in m.group(1).split("."))
    except ValueError:
        return None
    return nums, (m.group(2) or "").strip().lower()


def compare_versions(a: str, b: str) -> int | None:
    """比较两个版本：a > b 返回 1，相等 0，a < b 返回 -1；任一无法解析返回 None。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    na, pre_a = pa
    nb, pre_b = pb
    width = max(len(na), len(nb))
    na = na + (0,) * (width - len(na))
    nb = nb + (0,) * (width - len(nb))
    if na != nb:
        return 1 if na > nb else -1
    # 主版本相同：无预发布后缀 > 有预发布后缀（0.3.0 > 0.3.0-beta）
    if pre_a == pre_b:
        return 0
    if not pre_a:
        return 1
    if not pre_b:
        return -1
    return 1 if pre_a > pre_b else -1


def is_newer(remote: str, local: str) -> bool:
    """远端版本是否比本地新；任一版本无法解析时返回 False（保守：不误报更新）。"""
    return compare_versions(remote, local) == 1


def format_size(num_bytes: int) -> str:
    """字节数 → 人类可读体积（用于下载包展示）。"""
    n = float(num_bytes or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(n)} {unit}"
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


# ---- 网络自标识 ----

def user_agent(extra: str = "") -> str:
    """请求 UA：自标识 + 版本号（便于服务端排查来源）。"""
    base = f"{APP_NAME}/{APP_VERSION}"
    return f"{base} (+{extra})" if extra else base
