"""Docling 本地解析器 —— Stage 1 布局层。

在 import docling 之前设置环境变量（本模块顶部自动完成）：
- DOCLING_INFERENCE_COMPILE_TORCH_MODELS=0  禁用 torch.compile（避免要求 MSVC cl.exe）
- HF_ENDPOINT / HF_HUB_DISABLE_XET           模型下载走国内镜像（可被 PDFASKER_HF_MIRROR=0 关闭）
- 需要 UTF-8 模式（PYTHONUTF8），由 main.py 启动引导保证

输出格式与 pdf_processor._normalize_page_result 兼容（元素键名为 id/type/text/bbox/caption/
is_meaningful/description/section_name/font_size/is_bold）。
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

os.environ.setdefault("DOCLING_INFERENCE_COMPILE_TORCH_MODELS", "0")
if os.environ.get("PDFASKER_HF_MIRROR", "1") == "1":
    os.environ.setdefault("HF_ENDPOINT", "https://hf-mirror.com")
    os.environ.setdefault("HF_HUB_DISABLE_XET", "1")

if TYPE_CHECKING:
    from docling.document_converter import DocumentConverter

# Docling label → 应用 element_type
_DOCLING_LABEL_MAP: dict[str, str] = {
    "title": "title",
    "section_header": "subtitle",
    "text": "body",
    "paragraph": "body",
    "list_item": "body",
    "footnote": "body",
    "caption": "figure_caption",
    "table": "table",
    "picture": "figure",
    "chart": "figure",
    "formula": "equation",
    "reference": "reference",
    "page_header": "header_footer",
    "page_footer": "header_footer",
    "document_index": "unknown",
    "code": "body",
}

_converter: DocumentConverter | None = None
_converter_lock = threading.Lock()
_parse_lock = threading.Lock()  # 全局转换器非线程安全：多篇 PDF 解析自动串行排队


def warm_up_import() -> None:
    """后台预热 Docling Python 模块，不创建转换器或触发 PDF 解析。"""
    from docling.document_converter import DocumentConverter  # noqa: F401


def is_available() -> bool:
    """是否可用的探针（首次调用会触发模型下载）。"""
    try:
        get_converter()
        return True
    except Exception:
        return False


def get_converter() -> DocumentConverter:
    """惰性创建全局转换器（模型只加载一次，且避免并发重复初始化）。"""
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                from docling.document_converter import DocumentConverter
                _converter = DocumentConverter()
    return _converter


def _to_pixel_bbox(doc, page_no: int, l: float, t: float, r: float, b: float,
                   dpi: int = 150) -> list[float]:
    """把 Docling 的 PDF 坐标（点，原点左下、y 向上）转成像素坐标（原点左上、y 向下）。"""
    scale = dpi / 72.0
    page_h = 792.0
    try:
        page_h = float(doc.pages[page_no].size.height)
    except Exception:
        pass
    x0, x1 = l * scale, r * scale
    if t >= b:  # 底部原点
        y0 = (page_h - t) * scale
        y1 = (page_h - b) * scale
    else:  # 已经是左上原点（容错）
        y0, y1 = t * scale, b * scale
    return [round(x0, 1), round(y0, 1), round(x1, 1), round(y1, 1)]


def _element_text(item) -> str:
    text = getattr(item, "text", "") or ""
    if text.strip():
        return text
    # 表格：导出为 markdown 文本
    exporter = getattr(item, "export_to_dataframe", None)
    if exporter is not None:
        try:
            df = exporter()
            if df is not None and not df.empty:
                return df.to_markdown(index=False)
        except Exception:
            pass
    return ""


def parse_pdf(pdf_path: str, dpi: int = 150) -> list[dict]:
    """用 Docling 解析 PDF，返回与单页解析缓存兼容的结构。

    Raises:
        Exception: 解析失败（调用方显示错误并允许用户重试）。
    """
    converter = get_converter()
    with _parse_lock:
        result = converter.convert(pdf_path)
    doc = result.document

    # 收集所有元素（阅读顺序），附上 running section
    rows: list[dict] = []  # {page, elem}
    current_section = ""
    # 记录每页已有元素数，用于 element_id 编号
    page_counter: dict[int, int] = {}
    for item, _depth in doc.iterate_items(traverse_pictures=True):
        label = getattr(item, "label", None)
        label_str = label.value if hasattr(label, "value") else str(label or "")
        etype = _DOCLING_LABEL_MAP.get(label_str, "unknown")

        prov = getattr(item, "prov", None)
        if not prov:
            continue
        p = prov[0]
        page_no = int(p.page_no)
        bbox_raw = (p.bbox.l, p.bbox.t, p.bbox.r, p.bbox.b)
        if not (bbox_raw[2] > bbox_raw[0] and bbox_raw[3] != bbox_raw[1]):
            continue

        if etype == "subtitle":
            current_section = (_element_text(item) or "").strip()

        idx = page_counter.get(page_no, 0) + 1
        page_counter[page_no] = idx

        text = _element_text(item)
        is_meaningful = etype not in ("header_footer", "publisher_logo", "unknown")
        rows.append({
            "page": page_no,
            "id": f"p{page_no}_e{idx}",
            "type": etype,
            "text": text,
            "bbox": _to_pixel_bbox(doc, page_no, *bbox_raw, dpi=dpi),
            "caption": text if etype in ("figure_caption", "table_caption") else "",
            "is_meaningful": is_meaningful,
            "description": "",
            "section_name": current_section if etype != "subtitle" else "",
            "font_size": 0.0,
            "is_bold": False,
        })

    # 按页聚合
    pages: dict[int, list[dict]] = {}
    for r in rows:
        pages.setdefault(r["page"], []).append(r)

    total = max(doc.pages.keys()) if doc.pages else (max(pages) if pages else 0)
    out: list[dict] = []
    for page_no in range(1, total + 1):
        elems = pages.get(page_no, [])
        out.append({
            "page": page_no,
            "page_role": "content_page",
            "elements": [e for e in elems if e["type"] not in ("header_footer", "unknown")],
            "parse_error": None,
        })
    return out
