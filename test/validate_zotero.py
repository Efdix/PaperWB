"""PaperWB Zotero 文献整合验收脚本。

逐篇等待 Stage 1 和 Stage 2，避免同时启动多个 Docling 处理器。
脚本只通过 ZoteroLibrary 的临时数据库副本读取 Zotero，不会写入 Zotero 数据目录。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from src.core.pdf_processor import (
    PDFProcessor,
    StructuredDocument,
    _strip_watermarks,
    find_cross_page_seams,
)
from src.core.zotero_parser import ZoteroItem, ZoteroLibrary
from src.utils.config import get_text_api, load_config, load_doc_state


def _make_parse_client(config: dict):
    api = get_text_api(config)
    if not all(api.get(key) for key in ("api_key", "base_url", "model")):
        return None
    from src.core.llm_client import LLMClient

    return LLMClient(api["api_key"], api["base_url"], api["model"])


def _page_data(processor: PDFProcessor) -> list[dict]:
    manifest = processor.manifest
    if manifest is None:
        return []
    pages = []
    for page_num in range(1, manifest.total_pages + 1):
        page = processor.get_page_cache(page_num)
        if page:
            pages.append(page)
    return pages


def _seams_applied(doc: StructuredDocument, state: dict) -> tuple[int, int]:
    merged = state.get("merged_seams") or {}
    if not isinstance(merged, dict):
        return 0, 0
    by_id = {e.element_id: e for e in doc.display_elements if e.element_id}
    applied = 0
    for raw_key, value in merged.items():
        if not isinstance(value, dict):
            continue
        source_id = str(raw_key).split("|", 1)[0]
        target_id = str(value.get("with_id", ""))
        merged_text = _strip_watermarks(str(value.get("merged_text", "")))
        source = by_id.get(source_id)
        if source and source.text == merged_text and target_id not in by_id:
            applied += 1
    return applied, len(merged)


def _run_one(
    app: QCoreApplication,
    item: ZoteroItem,
    client,
    timeout_seconds: int,
) -> dict:
    path = item.pdf_path
    processor = PDFProcessor(path, client)
    loop = QEventLoop()
    started = time.monotonic()
    result: dict = {
        "key": item.key,
        "title": item.title,
        "path": path,
        "ok": False,
        "stage1_errors": [],
        "error": "",
    }
    latest_doc: StructuredDocument | None = None
    merged_signal_seen = False
    finished = False

    def stop_loop() -> None:
        nonlocal finished
        if not finished:
            finished = True
            loop.quit()

    def on_stage1_complete(_path: str) -> None:
        processor.start_stage2()

    def on_stage1_error(_path: str, page: int, message: str) -> None:
        result["stage1_errors"].append({"page": page, "message": message})

    def wait_for_stage2_idle() -> None:
        if processor.is_stage2_running:
            QTimer.singleShot(200, wait_for_stage2_idle)
        else:
            stop_loop()

    def on_stage2_finished(_path: str, doc: StructuredDocument) -> None:
        nonlocal latest_doc
        latest_doc = doc
        # 等待可选的跨页 LLM 线程结束；没有候选接缝时立即收尾。
        QTimer.singleShot(50, wait_for_stage2_idle)

    def on_stage2_merged(_path: str, doc: StructuredDocument) -> None:
        nonlocal latest_doc, merged_signal_seen
        latest_doc = doc
        merged_signal_seen = True
        stop_loop()

    def on_stage2_error(_path: str, message: str) -> None:
        result["error"] = message
        stop_loop()

    processor.stage1_complete.connect(on_stage1_complete)
    processor.stage1_error.connect(on_stage1_error)
    processor.stage2_finished.connect(on_stage2_finished)
    processor.stage2_merged.connect(on_stage2_merged)
    processor.stage2_error.connect(on_stage2_error)
    QTimer.singleShot(timeout_seconds * 1000, lambda: (
        result.__setitem__("error", f"超时（>{timeout_seconds} 秒）")
        or stop_loop()
    ))

    processor.start_stage1()
    loop.exec()
    if processor.is_busy:
        processor.cancel()
    app.processEvents()

    manifest = processor.manifest
    result["elapsed_seconds"] = round(time.monotonic() - started, 1)
    result["stage1"] = {
        "total_pages": manifest.total_pages if manifest else 0,
        "done_pages": manifest.done_count if manifest else 0,
        "error_pages": manifest.error_count if manifest else 0,
    }
    if latest_doc is None:
        result["error"] = result["error"] or "未收到结构化文档"
        return result

    state = load_doc_state(path)
    pages = _page_data(processor)
    seams = find_cross_page_seams(pages)
    applied, cached = _seams_applied(latest_doc, state)
    missing_images = [
        {
            "element_id": element.element_id,
            "page": element.page,
            "type": element.element_type,
        }
        for element in latest_doc.display_elements
        if element.element_type in ("figure", "table")
        and not (
            element.image_path
            and os.path.exists(
                element.image_path
                if os.path.isabs(element.image_path)
                else os.path.join(processor._cache_dir, element.image_path)
            )
        )
    ]
    result["document"] = {
        "title": latest_doc.title,
        "display_elements": len(latest_doc.display_elements),
        "figures": len(latest_doc.figures),
        "tables": len(latest_doc.tables),
        "references": len(latest_doc.references),
        "cached_seams": cached,
        "candidate_seams": len(seams),
        "applied_seams": applied,
        "unresolved_seams": max(0, len(seams) - applied),
        "merged_signal": merged_signal_seen,
        "missing_images": missing_images,
        "doc_format": state.get("doc_format"),
        "fast_version": state.get("fast_version"),
    }
    result["ok"] = bool(
        not result["stage1_errors"]
        and result["stage1"]["total_pages"] > 0
        and result["stage1"]["done_pages"] == result["stage1"]["total_pages"]
        and latest_doc.display_elements
        and not missing_images
        and state.get("doc_format") == "fast"
        and applied >= len(seams)
    )
    return result


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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--zotero-dir", default="", help="Zotero 数据目录")
    parser.add_argument("--count", type=int, default=20, help="验收文献数量")
    parser.add_argument("--timeout", type=int, default=1800, help="单篇超时秒数")
    parser.add_argument(
        "--report",
        default=r"D:\System\Desktop\test_fig\zotero_validation.json",
        help="JSON 报告路径",
    )
    args = parser.parse_args()
    if args.count < 20:
        parser.error("--count 不能小于 20")

    config = load_config()
    zotero_dir = args.zotero_dir or config.get("zotero_data_dir", "")
    library = ZoteroLibrary(zotero_dir)
    library.load()
    selected = _select_items(library.get_all_items(), args.count)
    if len(selected) < args.count:
        print(f"有效本地 PDF 只有 {len(selected)} 篇，无法满足 {args.count} 篇验收。")
        return 2

    client = _make_parse_client(config)
    app = QCoreApplication.instance() or QCoreApplication(sys.argv)
    results = []
    for index, item in enumerate(selected, 1):
        print(f"[{index}/{len(selected)}] {item.title}", flush=True)
        result = _run_one(app, item, client, args.timeout)
        results.append(result)
        status = "PASS" if result["ok"] else "FAIL"
        print(
            f"  {status} pages={result.get('stage1', {}).get('done_pages', 0)}"
            f"/{result.get('stage1', {}).get('total_pages', 0)}"
            f" elements={result.get('document', {}).get('display_elements', 0)}"
            f" seams={result.get('document', {}).get('applied_seams', 0)}"
            f"/{result.get('document', {}).get('candidate_seams', 0)}"
            f" time={result.get('elapsed_seconds', 0)}s",
            flush=True,
        )

    report = {
        "zotero_dir": zotero_dir,
        "item_count": library.item_count,
        "collection_count": library.collection_count,
        "selected_count": len(selected),
        "valid_pdf_count": sum(
            bool(item.pdf_path and os.path.isfile(item.pdf_path))
            for item in library.get_all_items()
        ),
        "api_seam_merge": client is not None,
        "passed": sum(bool(result["ok"]) for result in results),
        "failed": sum(not result["ok"] for result in results),
        "results": results,
    }
    report_path = Path(args.report)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"报告：{report_path}")
    return 0 if report["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
