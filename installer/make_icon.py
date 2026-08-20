# -*- coding: utf-8 -*-
"""生成 PaperWB 应用图标（assets/PaperWB.ico）。

用 Pillow 绘制：暖白纸张 + 靛蓝底的翻开书本 + 琥珀色书签，呼应
「暖白研究工作台」主题。以 1024x1024 超采样绘制后缩放出多尺寸 ICO，
供 PaperWB.exe / 安装向导 / Qt 窗口共用。

用法（任意装有 Pillow 的 Python 环境）:
    python installer/make_icon.py
"""

from __future__ import annotations

import os
import sys

from PIL import Image, ImageDraw

# 主题色（与 src/ui/styles.py 的暖白研究工作台呼应）
_BG = (47, 58, 122, 255)        # 靛蓝底
_PAGE = (250, 246, 235, 255)    # 暖白纸页
_PAGE_SHADE = (232, 224, 205, 255)  # 右页阴影
_BOOKMARK = (232, 160, 66, 255)  # 琥珀书签
_SPINE = (250, 246, 235, 255)

SIZE = 1024


def _draw_icon(size: int) -> Image.Image:
    """在 size x size 画布上绘制图标（先按 1024 绘制再缩放以保证小尺寸平滑）。"""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    # 圆角方形底
    radius = int(SIZE * 0.22)
    d.rounded_rectangle([0, 0, SIZE - 1, SIZE - 1], radius=radius, fill=_BG)

    # 翻开的书：左右两页（梯形），中缝相接
    left_page = [(0.26, 0.32), (0.48, 0.40), (0.48, 0.72), (0.26, 0.64)]
    right_page = [(0.74, 0.32), (0.52, 0.40), (0.52, 0.72), (0.74, 0.64)]
    d.polygon([(x * SIZE, y * SIZE) for x, y in left_page], fill=_PAGE)
    d.polygon([(x * SIZE, y * SIZE) for x, y in right_page], fill=_PAGE_SHADE)

    # 中缝
    d.line([(0.50 * SIZE, 0.40 * SIZE), (0.50 * SIZE, 0.72 * SIZE)],
           fill=_SPINE, width=int(SIZE * 0.012))

    # 左页两条文字线
    for i, y in enumerate((0.46, 0.53, 0.60)):
        x0 = 0.305 if i < 2 else 0.305
        x1 = 0.435 if i < 2 else 0.40
        d.line([(x0 * SIZE, y * SIZE), (x1 * SIZE, y * SIZE)],
               fill=_BG, width=int(SIZE * 0.014))

    # 琥珀书签：从右上斜插进右页
    bookmark = [(0.615, 0.20), (0.685, 0.20), (0.685, 0.335),
                (0.65, 0.30), (0.615, 0.335)]
    d.polygon([(x * SIZE, y * SIZE) for x, y in bookmark], fill=_BOOKMARK)

    if size == SIZE:
        return img
    return img.resize((size, size), Image.LANCZOS)


def main() -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out_dir = os.path.join(repo, "assets")
    os.makedirs(out_dir, exist_ok=True)

    base = _draw_icon(SIZE)
    ico_path = os.path.join(out_dir, "PaperWB.ico")
    base.save(
        ico_path,
        sizes=[(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)],
    )
    # 便于预览/README 引用的 PNG
    base.resize((256, 256), Image.LANCZOS).save(os.path.join(out_dir, "PaperWB.png"))
    print(f"OK: {ico_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
