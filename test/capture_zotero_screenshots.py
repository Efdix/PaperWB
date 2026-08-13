"""PaperWB Zotero UI 验收截图脚本。"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.app import MainWindow
from src.core.zotero_parser import ZoteroItem, ZoteroLibrary
from src.ui.pdf_viewer import ImageCard, ParagraphCard
from src.utils.config import load_config


def _select_items(items: list[ZoteroItem], count: int) -> list[ZoteroItem]:
    valid: dict[str, ZoteroItem] = {}
    for item in items:
        if item.key and item.pdf_path and os.path.isfile(item.pdf_path):
            valid.setdefault(item.key, item)
    ordered = sorted(valid.values(), key=lambda item: (item.title.lower(), item.key))
    target = next(
        (
            item
            for item in ordered
            if "single-cell profiling decodes patagium development" in item.title.lower()
        ),
        None,
    )
    if target is not None:
        ordered.remove(target)
        ordered.insert(0, target)
    return ordered[:count]


def _safe_name(text: str, limit: int = 60) -> str:
    text = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", text)
    text = re.sub(r"[\\/:*?\"<>|]+", "_", text).strip(" ._")
    return text[:limit] or "paper"


def _pump(app: QApplication, seconds: float = 0.15) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.01)


def _load_and_wait(window: MainWindow, app: QApplication, item: ZoteroItem) -> None:
    window._on_zotero_pdf_selected(item.pdf_path)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        app.processEvents()
        doc = window.pdf_viewer.structured_document
        processor = getattr(window.pdf_viewer, "_processor", None)
        if doc is not None and not (processor and processor.is_stage2_running):
            break
        time.sleep(0.03)
    _pump(app, 0.4)


def _capture(window: MainWindow, app: QApplication, path: Path) -> None:
    _pump(app, 0.25)
    if not window.grab().save(str(path)):
        raise RuntimeError(f"截图保存失败：{path}")


def _scroll_to_card(window: MainWindow, app: QApplication, predicate) -> bool:
    viewer = window.pdf_viewer
    for card in viewer._cards:
        if predicate(card):
            viewer.card_layout.activate()
            viewer.scroll_area.verticalScrollBar().setValue(max(0, card.y() - 70))
            _pump(app, 0.35)
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Capture PaperWB Zotero UI screenshots.")
    parser.add_argument("--count", type=int, default=20, help="Number of different papers to capture.")
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")

    output_dir = Path(r"D:\System\Desktop\test_fig")
    output_dir.mkdir(parents=True, exist_ok=True)
    config = load_config()
    library = ZoteroLibrary(config.get("zotero_data_dir", ""))
    library.load()
    selected = _select_items(library.get_all_items(), args.count)
    if len(selected) < args.count:
        print(f"有效 PDF 少于 {args.count} 篇：{len(selected)}")
        return 2

    app = QApplication.instance() or QApplication(sys.argv)
    window = MainWindow()
    window.resize(1600, 1000)
    window.show()
    _pump(app, 1.5)

    for index, item in enumerate(selected, 1):
        print(f"[{index}/{args.count}] {item.title}", flush=True)
        _load_and_wait(window, app, item)
        viewer = window.pdf_viewer
        viewer.scroll_area.verticalScrollBar().setValue(0)
        _capture(
            window,
            app,
            output_dir / f"{index:02d}_{item.key}_{_safe_name(item.title)}_overview.png",
        )

        if index == 1:
            window.chat_panel.add_user_message("请概括这篇论文的主要发现。")
            window.chat_panel.start_ai_response()
            _capture(window, app, output_dir / "01_patagium_thinking.png")
            window.chat_panel.finish_ai_response()
            window.chat_panel.clear_messages()

            if _scroll_to_card(
                window,
                app,
                lambda card: isinstance(card, ParagraphCard)
                and getattr(card._elem, "element_id", "") == "p3_e1",
            ):
                _capture(window, app, output_dir / "01_patagium_abstract.png")
            if _scroll_to_card(
                window,
                app,
                lambda card: isinstance(card, ParagraphCard)
                and getattr(card._elem, "element_id", "") == "p6_e4"
                and "distribution in the patagium" in card._text,
            ):
                _capture(window, app, output_dir / "01_patagium_cross_page.png")
            if _scroll_to_card(
                window,
                app,
                lambda card: isinstance(card, ImageCard)
                and getattr(card, "_page", 0) == 47,
            ):
                _capture(window, app, output_dir / "01_patagium_figure_page47.png")

    window.close()
    _pump(app, 1.0)
    print(f"截图目录：{output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
