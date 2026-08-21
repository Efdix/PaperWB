# -*- coding: utf-8 -*-
"""生成 PaperWB 应用图标（assets/PaperWB.ico）。

从 repo 根目录的 PaperWB.jpg 读取源图，裁出图形主体后缩放出多尺寸
ICO，供 PaperWB.exe / 安装向导 / Qt 窗口共用。

用法（任意装有 Pillow 的 Python 环境）:
    python installer/make_icon.py
"""

from __future__ import annotations

import os
import sys

try:
    from PIL import Image, ImageChops
except ImportError:
    sys.exit(
        "错误: 需要安装 Pillow (pip install Pillow) 来生成图标。\n"
        "提示: conda activate PaperWB 后执行。"
    )

ICO_SIZES = [(256, 256), (64, 64), (48, 48), (32, 32), (16, 16)]
PNG_PREVIEW_SIZE = (256, 256)


def _crop_icon_subject(img: Image.Image) -> Image.Image:
    """裁出 PaperWB 图形主体，避免宽幅源图生成过多空白。"""
    rgb = img.convert("RGB")
    white = Image.new("RGB", rgb.size, "white")
    difference = ImageChops.difference(rgb, white)
    # JPEG 的背景噪声很轻，阈值 20 可以保留图形阴影并排除纯白画布。
    mask = difference.point(lambda value: 255 if value > 20 else 0)
    bbox = mask.getbbox()
    if bbox is None:
        w, h = rgb.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        return rgb.crop((left, top, left + side, top + side))

    left, top, right, bottom = bbox
    width = right - left
    height = bottom - top
    padding = max(24, int(max(width, height) * 0.12))
    left -= padding
    top -= padding
    right += padding
    bottom += padding

    side = max(right - left, bottom - top)
    center_x = (left + right) // 2
    center_y = (top + bottom) // 2
    left = center_x - side // 2
    top = center_y - side // 2
    right = left + side
    bottom = top + side

    # 超出边界时平移裁剪框，而不是缩小主体。
    if left < 0:
        right -= left
        left = 0
    if top < 0:
        bottom -= top
        top = 0
    if right > rgb.width:
        left -= right - rgb.width
        right = rgb.width
    if bottom > rgb.height:
        top -= bottom - rgb.height
        bottom = rgb.height
    return rgb.crop((max(0, left), max(0, top), right, bottom))


def main() -> int:
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    src_path = os.path.join(repo, "PaperWB.jpg")
    if not os.path.isfile(src_path):
        sys.exit(f"错误: 源图不存在 {src_path}")

    img = Image.open(src_path)
    if img.mode == "RGBA":
        pass  # 已有 alpha
    elif img.mode == "P":
        img = img.convert("RGBA")
    else:
        img = img.convert("RGB")

    square = _crop_icon_subject(img)

    out_dir = os.path.join(repo, "assets")
    os.makedirs(out_dir, exist_ok=True)

    ico_path = os.path.join(out_dir, "PaperWB.ico")
    square.save(ico_path, sizes=ICO_SIZES)

    png_path = os.path.join(out_dir, "PaperWB.png")
    square.resize(PNG_PREVIEW_SIZE, Image.LANCZOS).save(png_path)

    print(f"OK: {ico_path} (source: {src_path})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
