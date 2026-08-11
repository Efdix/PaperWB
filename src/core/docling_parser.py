"""Docling 本地解析器 —— Stage 1 布局层。

在 import docling 之前设置环境变量（本模块顶部自动完成）：
- DOCLING_INFERENCE_COMPILE_TORCH_MODELS=0  禁用 torch.compile（避免要求 MSVC cl.exe）
- HF_ENDPOINT / HF_HUB_DISABLE_XET           模型下载走国内镜像（可被 PDFASKER_HF_MIRROR=0 关闭）
- 需要 UTF-8 模式（PYTHONUTF8），由 main.py 启动引导保证

输出格式与 pdf_processor._normalize_page_result 兼容（元素键名为 id/type/text/bbox/caption/
is_meaningful/description/section_name/font_size/is_bold，另附 level 供规则组装判定标题层级）。
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
    "diagram": "figure",
    "detailed_picture": "figure",
    "vector_illustration": "figure",
    "screenshot": "figure",
    "sticker": "figure",
    "formula": "equation",
    "reference": "reference",
    "page_header": "header_footer",
    "page_footer": "header_footer",
    "document_index": "unknown",
    "table_of_contents": "unknown",
    "page_index": "unknown",
    "code": "body",
}

_converters: dict[bool, DocumentConverter] = {}
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


def get_converter(do_ocr: bool = False) -> DocumentConverter:
    """惰性创建全局转换器（模型只加载一次，且避免并发重复初始化）。

    do_ocr=False 使用纯版式解析（文本型 PDF，默认、快）；
    do_ocr=True 启用 OCR（扫描版 PDF 回退，慢）。
    两种配置分别缓存，互不干扰。
    """
    key = bool(do_ocr)
    conv = _converters.get(key)
    if conv is None:
        with _converter_lock:
            conv = _converters.get(key)
            if conv is None:
                from docling.document_converter import DocumentConverter
                from docling.datamodel.pipeline_options import PdfPipelineOptions
                opts = PdfPipelineOptions()
                opts.do_ocr = key
                conv = DocumentConverter(pipeline_options=opts)
                _converters[key] = conv
    return conv


def has_text_layer(pdf_path: str, threshold_ratio: float = 0.15) -> bool:
    """用 PyMuPDF 快速探测 PDF 是否有可用文本层。

    全部页面可提取字符数 / 页数 >= 平均阈值即视为文本型 PDF；
    扫描版通常接近 0 字符。探测失败时保守返回 True（走默认快路径）。
    """
    try:
        import fitz
    except Exception:
        return True
    try:
        doc = fitz.open(pdf_path)
        try:
            total_chars = 0
            for page in doc:
                total_chars += len(page.get_text().strip())
            if len(doc) == 0:
                return True
            return total_chars / len(doc) >= threshold_ratio
        finally:
            doc.close()
    except Exception:
        return True


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


def _span_index(pdf_path: str, dpi: int = 150) -> dict[int, list[dict]]:
    """用 PyMuPDF 提取每页文本 span（像素坐标 + 字号），供元素 font_size 匹配。

    返回 {page_no: [{"bbox": [x0,y0,x1,y1], "size": float}]}，bbox 为 150dpi
    左上原点像素坐标，与元素 bbox 同坐标系。
    """
    try:
        import fitz
    except Exception:
        return {}
    try:
        pdf = fitz.open(pdf_path)
    except Exception:
        return {}
    scale = dpi / 72.0
    out: dict[int, list[dict]] = {}
    try:
        for page_no in range(1, len(pdf) + 1):
            page = pdf[page_no - 1]
            spans: list[dict] = []
            try:
                page_h = page.rect.height
                blocks = page.get_text("dict").get("blocks", [])
                for blk in blocks:
                    if blk.get("type") != 0:
                        continue
                    for line in blk.get("lines", []):
                        for span in line.get("spans", []):
                            x0, y0, x1, y1 = span.get("bbox", (0, 0, 0, 0))
                            spans.append({
                                "bbox": [
                                    round(x0 * scale, 1),
                                    round((page_h - y1) * scale, 1),
                                    round(x1 * scale, 1),
                                    round((page_h - y0) * scale, 1),
                                ],
                                "size": round(float(span.get("size", 0.0)), 2),
                            })
            except Exception:
                pass
            out[page_no] = spans
    finally:
        pdf.close()
    return out


def _match_font_size(span_map: dict[int, list[dict]], page_no: int,
                     bbox: list[float]) -> float:
    """元素 bbox 与同页 span 匹配，返回中位字号（无匹配返回 0.0）。"""
    spans = span_map.get(page_no, [])
    if not spans:
        return 0.0
    try:
        eb = bbox
        ex0, ey0, ex1, ey1 = (float(eb[0]), float(eb[1]),
                              float(eb[2]), float(eb[3]))
        ew = ex1 - ex0
        eh = ey1 - ey0
        if ew <= 0 or eh <= 0:
            return 0.0
        sizes: list[float] = []
        for s in spans:
            sb = s["bbox"]
            w = min(eb[2], sb[2]) - max(eb[0], sb[0])
            h = min(eb[3], sb[3]) - max(eb[1], sb[1])
            if w <= 0 or h <= 0:
                continue
            if w * h >= min(ew * eh, max(200.0, ew * eh * 0.15)):
                sizes.append(s["size"])
        if not sizes:
            return 0.0
        sizes.sort()
        return float(sizes[len(sizes) // 2])
    except (TypeError, KeyError, IndexError, ValueError):
        return 0.0


_MIN_IMAGE_AREA_PT2 = 900.0  # 30x30 pt 以下的装饰性小图忽略


def _rect_intersect_area(a: list[float], b: list[float]) -> float:
    """两个矩形（[x0,y0,x1,y1]）的交集面积。"""
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def _append_missing_bitmaps(pdf_path: str, doc, rows: list[dict],
                            page_counter: dict[int, int]) -> None:
    """Docling 布局模型漏检图片时，用 PyMuPDF 位图区域兜底补成 figure 元素。

    只作用于本页没有任何 figure/table 元素的页面；被更大图覆盖的嵌套区域去重。
    """
    try:
        import fitz
    except Exception:
        return
    pages_with_fig = {r["page"] for r in rows if r["type"] in ("figure", "table")}
    try:
        pdf = fitz.open(pdf_path)
    except Exception:
        return
    try:
        for page_no in range(1, len(pdf) + 1):
            if page_no in pages_with_fig:
                continue
            try:
                infos = pdf[page_no - 1].get_image_info()
            except Exception:
                continue
            added = 0
            taken: list[list[float]] = []
            for info in infos:
                bbox = info.get("bbox", [0, 0, 0, 0])
                if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                    continue
                area = (bbox[2] - bbox[0]) * (bbox[3] - bbox[1])
                if area < _MIN_IMAGE_AREA_PT2:
                    continue
                if any(_rect_intersect_area(bbox, t) >= area * 0.7 for t in taken):
                    continue  # 已被更大的图覆盖
                taken.append(bbox)
                idx = page_counter.get(page_no, 0) + 1
                page_counter[page_no] = idx
                rows.append({
                    "page": page_no,
                    "id": f"p{page_no}_e{idx}",
                    "type": "figure",
                    "text": "",
                    "bbox": _to_pixel_bbox(doc, page_no, *bbox),
                    "caption": "",
                    "is_meaningful": True,
                    "description": "",
                    "section_name": "",
                    "font_size": 0.0,
                    "is_bold": False,
                    "level": 0,
                })
                added += 1
            if added:
                print(f"[Docling] 第 {page_no} 页 Docling 未检出图片，按位图区域补 {added} 个")
    finally:
        pdf.close()


def parse_pdf(pdf_path: str, dpi: int = 150) -> list[dict]:
    """用 Docling 解析 PDF，返回与单页解析缓存兼容的结构。

    自动探测文本层：文本型 PDF 用纯版式解析（do_ocr=False，默认快路径）；
    扫描版 PDF 回退到 OCR 解析（do_ocr=True）。Raises:
        Exception: 解析失败（调用方显示错误并允许用户重试）。
    """
    do_ocr = not has_text_layer(pdf_path)
    if do_ocr:
        print("[Docling] 检测到扫描版 PDF（无文本层），回退到 OCR 解析（速度较慢）")
    converter = get_converter(do_ocr)
    with _parse_lock:
        result = converter.convert(pdf_path)
    doc = result.document

    # 收集所有元素（阅读顺序），附上 running section
    rows: list[dict] = []  # {page, elem}
    current_section = ""
    # 记录每页已有元素数，用于 element_id 编号
    page_counter: dict[int, int] = {}
    span_map = _span_index(pdf_path, dpi=dpi)
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
        level = 0
        if etype == "subtitle":
            try:
                level = int(getattr(item, "level", None) or 1)
            except (TypeError, ValueError):
                level = 1
        pixel_bbox = _to_pixel_bbox(doc, page_no, *bbox_raw, dpi=dpi)
        rows.append({
            "page": page_no,
            "id": f"p{page_no}_e{idx}",
            "type": etype,
            "text": text,
            "bbox": pixel_bbox,
            "caption": text if etype in ("figure_caption", "table_caption") else "",
            "is_meaningful": is_meaningful,
            "description": "",
            "section_name": current_section if etype != "subtitle" else "",
            "font_size": _match_font_size(span_map, page_no, pixel_bbox),
            "is_bold": False,
            "level": level,
        })

    # 按页聚合
    # 兜底：Docling 漏检的位图区域补成 figure 元素
    _append_missing_bitmaps(pdf_path, doc, rows, page_counter)
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
