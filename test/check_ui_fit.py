# -*- coding: utf-8 -*-
"""界面重构体检：几何 + 文字适配检查（离屏，不依赖字体渲染）。

离屏渲染中文字形是豆腐块，肉眼看不出截断，因此这里直接量：
1) 每个工作台的 chrome 行数与占高（正文可用高度）；
2) 应用栏 / 动作行里的文字是否超出控件宽度（会被 Qt 省略或压扁）；
3) 窄窗口（1024 最小宽度）下是否出现横向滚动/重叠。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PAPERWB_DISABLE_PREPARSE", "1")

from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QLabel, QMessageBox, QPushButton, QFrame,
)

for _n in ("warning", "critical", "information", "about", "question"):
    setattr(QMessageBox, _n, staticmethod(lambda *a, **k: QMessageBox.StandardButton.Ok))

app = QApplication([])
import src.app as app_mod  # noqa: E402
app_mod.has_data_root = lambda: True

win = app_mod.MainWindow()
win.resize(1600, 950)
win.show()
for _ in range(25):
    app.processEvents()

FAILS: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def chrome_rows(container) -> list[tuple[str, int]]:
    """列出容器顶部的横向 chrome 条（高 < 60 且宽近似满宽的直接子部件）。"""
    out = []
    for child in container.findChildren(QFrame):
        name = child.objectName()
        if name not in ("appBar", "actionBar"):
            continue
        if child.isVisible() and child.width() > 200:
            out.append((name, child.height()))
    return out


# ---- 1. 各工作台 chrome 占高 ----
for idx, tag in enumerate(("阅读", "检索", "写作", "统计")):
    win._switch_workspace(idx)
    for _ in range(15):
        app.processEvents()
    page = win._main_tabs.widget(idx)
    rows = chrome_rows(page)
    total = sum(h for _n, h in rows) + win._app_header.height()
    # 应用栏 44px 上限 + 工作台自身最多一条动作行 40px
    check(f"{tag}工作台 chrome 不超过 2 行 90px",
          len(rows) <= 1 and total <= 92,
          f"行={[n for n, _ in rows]} 应用栏={win._app_header.height()} " +
          f"动作行={[h for _n, h in rows]} 合计={total}")

# ---- 2. 文字适配：按钮/标签文字不能超出自身宽度 ----
win._switch_workspace(0)
for _ in range(15):
    app.processEvents()


def text_overflow(widget) -> list[str]:
    bad = []
    widgets = list(widget.findChildren(QLabel)) + list(widget.findChildren(QPushButton))
    for w in widgets:
        if not w.isVisible() or w.text() == "":
            continue
        if w.__class__.__name__.startswith("Elided") or \
                w.__class__.__name__ == "_ElidedStatusLabel":
            continue  # 自身就会省略号截断，属预期
        if isinstance(w, QLabel) and w.wordWrap():
            continue  # 折行标签允许多行
        # 用 Qt 自身的最小尺寸提示判定：它已包含 QSS padding/边框/图标宽度，
        # 比手算像素可靠（手算 padding 会差几像素造成误报）。
        need = w.minimumSizeHint().width()
        if need > w.width():
            bad.append(f"{w.__class__.__name__}('{w.text().splitlines()[0][:24]}' "
                       f"需 {need} 有 {w.width()})")
    return bad


for idx, tag in enumerate(("阅读", "检索", "写作", "统计")):
    win._switch_workspace(idx)
    for _ in range(12):
        app.processEvents()
    bad = text_overflow(win._main_tabs.widget(idx))
    check(f"{tag}工作台无文字溢出", not bad, "; ".join(bad[:4]))

# ---- 3. 窄窗口（最小尺寸 1024x680） ----
win.resize(1024, 680)
for _ in range(25):
    app.processEvents()
bad_narrow = []
for idx, tag in enumerate(("阅读", "检索", "写作", "统计")):
    win._switch_workspace(idx)
    for _ in range(12):
        app.processEvents()
    ovf = text_overflow(win._main_tabs.widget(idx))
    if ovf:
        bad_narrow.append(f"{tag}: {ovf[0]}")
check("窄窗口(1024x680)无文字溢出", not bad_narrow, "; ".join(bad_narrow[:3]))

# ---- 4. 阅读工作台正文区占比（关键诉求：正经工作区不能再被压缩） ----
win.resize(1600, 950)
win._switch_workspace(0)
for _ in range(20):
    app.processEvents()
avail_h = win._main_tabs.widget(0).height()
content_h = win.pdf_viewer.scroll_area.height()
ratio = content_h / (win.height() or 1)
check("阅读正文区占窗口高度 ≥ 80%", ratio >= 0.80,
      f"正文 {content_h}px / 窗口 {win.height()}px = {ratio:.0%}")

# ---- 4b. 窄窗口下正文列不能被两侧面板夹成窄条 ----
win.resize(1024, 680)
win._switch_workspace(0)
for _ in range(20):
    app.processEvents()
widths = win._reader_inner_splitter.sizes()
reader_w = widths[0] if widths else 0
check("窄窗口(1024)正文列 ≥ 窗口 55%", reader_w >= 1024 * 0.55,
      f"正文 {reader_w}px / 窗口 1024px = {reader_w / 1024:.0%} "
      f"(左栏 {'显示' if win.pdf_list.isVisible() else '已自动收起'})")

# ---- 5. 隐藏菜单栏后快捷键仍在（Ctrl+, / Ctrl+Q / F11） ----
from PySide6.QtGui import QKeySequence  # noqa: E402
acts = {a.shortcut().toString() for a in win.findChildren(type(win._app_menu.actions()[0]))}
check("菜单快捷方式保留（Ctrl+,）", "Ctrl+," in acts, str(sorted(acts)))

# ---- 6. 图标按钮必须有可访问名称（不能只靠 emoji/tooltip） ----
from PySide6.QtWidgets import QPushButton  # noqa: E402
icon_buttons = [
    b for b in win.findChildren(QPushButton)
    if (b.text() or "").strip()
    and len((b.text() or "").strip()) <= 2
    and not any(ch.isalnum() for ch in (b.text() or "").strip())
]
missing = [b.text() for b in icon_buttons if not b.accessibleName()]
check("图标按钮有可访问名称", not missing, str(missing[:8]))

win.close()
app.processEvents()

print()
print(f"{'-' * 46}")
print("RESULT:", "PASS" if not FAILS else f"FAIL ({len(FAILS)}): {FAILS}")
sys.exit(1 if FAILS else 0)
