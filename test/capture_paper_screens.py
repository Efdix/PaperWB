# -*- coding: utf-8 -*-
"""离屏渲染已解析 PDF 并输出整页截图 —— 用于验收接缝整合/图注绑定/开头噪音。

对 page_cache 中已有完整解析缓存的 PDF，复用 PDFViewerPanel 的渲染管线
（无 LLM，跨页接缝走规则合并路径），把滚动内容分块存为 PNG 到 --out 目录。

用法:
    python test/capture_paper_screens.py --count 10 --out D:/System/Desktop/test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication

from src.utils.config import get_page_cache_root_dir, get_states_dir, load_doc_state

CHUNK_H = 2000  # 每张截图高度
MAX_CHUNKS = 8  # 每篇最多截张数（长文只截前段+中段，避免海量文件）


def _safe_name(text: str, limit: int = 40) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", text)
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text).strip(" ._")
    return text[:limit] or "paper"


def _discover_papers() -> list[dict]:
    """扫描 page_cache：返回已完整解析且 state 有效的 pdf_path 列表。"""
    papers: list[dict] = []
    cache_root = get_page_cache_root_dir()
    states_dir = get_states_dir()
    if not cache_root.exists():
        return papers
    for doc_dir in sorted(cache_root.iterdir()):
        doc_id = doc_dir.name
        manifest_path = doc_dir / "_manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pdf_path = manifest.get("pdf_path", "")
        if not pdf_path or not os.path.isfile(pdf_path):
            continue
        # manifest 落盘字段无 is_complete（那是 PageManifest 的 property）
        pages = manifest.get("pages") or {}
        total = int(manifest.get("total_pages", 0) or 0)
        if total <= 0 or sum(1 for s in pages.values() if s in ("done", "error")) < total:
            continue
        state = load_doc_state(pdf_path)
        if state.get("doc_format") != "fast":
            continue
        papers.append({
            "doc_id": doc_id,
            "pdf_path": pdf_path,
            "title": (state.get("structured_document") or {}).get("title", "") or pdf_path,
        })
    return papers


def _wait_loaded(app: QApplication, viewer, timeout: float = 60.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if getattr(viewer, "_structured_doc", None) is not None:
            return True
        if getattr(viewer, "_processor", None) is None \
                and getattr(viewer, "_current_path", ""):
            # 处理器被创建后仍在解析：继续等
            pass
        time.sleep(0.02)
    return getattr(viewer, "_structured_doc", None) is not None


def _force_rebuild(pdf_path: str) -> None:
    """清除整合字段，强制 Page2 走新规则重新组装（截图后恢复原 state）。"""
    from src.utils.config import _doc_id, get_states_dir, load_doc_state, save_doc_state
    state = load_doc_state(pdf_path)
    for key in ("structured_document", "merged_seams", "merged_seams_prelim",
                "seams_final", "doc_format", "fast_version", "pdf_mtime"):
        state.pop(key, None)
    save_doc_state(pdf_path, state)


def _capture(app: QApplication, viewer, pdf_path: str, out_dir: Path,
             tag: str) -> int:
    from src.utils.config import _doc_id, get_states_dir
    state_path = get_states_dir() / f"{_doc_id(pdf_path)}.json"
    backup = state_path.read_bytes() if state_path.exists() else None
    try:
        _force_rebuild(pdf_path)
        viewer.resize(1000, 1400)
        viewer.show()
        viewer.load_pdf(pdf_path)
        if not _wait_loaded(app, viewer):
            print(f"  [WARN] 等待渲染超时: {pdf_path}")
            return 0
        app.processEvents()
        # grab() 走完整 widget 渲染管线（含字体），离屏 render() 会丢字型
        full = viewer.scroll_area.widget().grab()
        total_h = full.height()
        width = full.width()
        n = 0
        for idx, y in enumerate(range(0, total_h, CHUNK_H)):
            if idx >= MAX_CHUNKS:
                break
            seg_h = min(CHUNK_H, total_h - y)
            pix = full.copy(0, y, width, seg_h)
            out = out_dir / f"{tag}_{idx + 1:02d}.png"
            pix.save(str(out))
            n += 1
        print(f"  [{tag}] 内容 {total_h}px → {n} 张截图")
        return n
    finally:
        viewer.clear_pdf()
        if backup is not None:
            state_path.write_bytes(backup)


def _inject_system_fonts() -> None:
    """offscreen 平台字体数据库为空（Windows 上不加载系统字体），
    向 QFontDatabase 显式注入中英文字体，否则所有文本渲染成方框。"""
    candidates = [
        r"C:\Windows\Fonts\segoeui.ttf",
        r"C:\Windows\Fonts\msyh.ttc",
        r"C:\Windows\Fonts\simhei.ttf",
        r"C:\Windows\Fonts\arial.ttf",
    ]
    for path in candidates:
        if os.path.isfile(path):
            QFontDatabase.addApplicationFont(path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--count", type=int, default=10)
    parser.add_argument("--out", default=r"D:/System/Desktop/test")
    args = parser.parse_args()

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)

    app = QApplication([])
    _inject_system_fonts()
    app.setFont(QFont("Segoe UI", 10))
    from src.ui.pdf_viewer import PDFViewerPanel

    papers = _discover_papers()
    if not papers:
        print("[FAIL] 未发现已解析文献")
        sys.exit(1)
    print(f"发现 {len(papers)} 篇已解析文献，选 {min(args.count, len(papers))} 篇")
    viewer = PDFViewerPanel()
    total = 0
    for i, paper in enumerate(papers[:args.count]):
        tag = f"{i + 1:02d}_{paper['doc_id']}_{_safe_name(paper['title'], 24)}"
        print(f"== {i + 1}. {paper['title'][:70]}")
        n = _capture(app, viewer, paper["pdf_path"], out_dir, tag)
        total += n
    print(f"完成：{total} 张 → {out_dir}")


if __name__ == "__main__":
    main()
