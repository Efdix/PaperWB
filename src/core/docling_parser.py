"""Docling 本地解析器 —— Stage 1 布局层。

在 import docling 之前设置环境变量（本模块顶部自动完成）：
- DOCLING_INFERENCE_COMPILE_TORCH_MODELS=0  禁用 torch.compile（避免要求 MSVC cl.exe）
- HF_ENDPOINT / HF_HUB_DISABLE_XET           模型下载走国内镜像（可被 PAPERWB_HF_MIRROR=0 关闭）
- 需要 UTF-8 模式（PYTHONUTF8），由 main.py 启动引导保证

输出格式与 pdf_processor._normalize_page_result 兼容（元素键名为 id/type/text/bbox/caption/
is_meaningful/description/section_name/font_size/is_bold，另附 level 供规则组装判定标题层级）。
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING

os.environ.setdefault("DOCLING_INFERENCE_COMPILE_TORCH_MODELS", "0")
_hf_mirror = os.environ.get(
    "PAPERWB_HF_MIRROR",
    os.environ.get("PDFASKER_HF_MIRROR", "1"),
)
if _hf_mirror == "1":
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

    docling 2.x 的 DocumentConverter 构造签名改为
    ``(allowed_formats, format_options)``，旧版（1.x）仍接受 ``pipeline_options``
    关键字。这里优先用新版 API，TypeError 时回退旧写法以兼容两种版本。
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
                try:
                    # docling >= 2.0：通过 format_options 传入 PDF 管线选项
                    from docling.document_converter import PdfFormatOption
                    from docling.datamodel.base_models import InputFormat
                    conv = DocumentConverter(format_options={
                        InputFormat.PDF: PdfFormatOption(pipeline_options=opts),
                    })
                except TypeError:
                    # docling 1.x：直接传 pipeline_options
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

    - 本页无任何 figure/table 元素：全量补充位图区域（原逻辑）。
    - 本页已检出 figure/table：仍检查未被现有 figure/table bbox 覆盖的
      大位图区域并补充（避免 docling 只检出小图、漏掉大图）。
    被更大图覆盖的嵌套区域去重。
    """
    try:
        import fitz
    except Exception:
        return
    # 已检出的 figure/table 的像素 bbox（150dpi，左上原点）
    existing_pixel_bbox: dict[int, list[list[float]]] = {}
    for r in rows:
        if r["type"] in ("figure", "table") and len(r.get("bbox") or []) == 4:
            existing_pixel_bbox.setdefault(r["page"], []).append(
                [float(v) for v in r["bbox"]]
            )
    try:
        pdf = fitz.open(pdf_path)
    except Exception:
        return
    scale = 150.0 / 72.0  # pt → 150dpi 像素
    try:
        for page_no in range(1, len(pdf) + 1):
            try:
                infos = pdf[page_no - 1].get_image_info()
            except Exception:
                continue
            if not infos:
                continue
            added = 0
            taken: list[list[float]] = []
            for info in infos:
                bbox = info.get("bbox", [0, 0, 0, 0])
                if len(bbox) != 4 or bbox[2] <= bbox[0] or bbox[3] <= bbox[1]:
                    continue
                w = bbox[2] - bbox[0]
                h = bbox[3] - bbox[1]
                area = w * h
                if area < _MIN_IMAGE_AREA_PT2:
                    continue
                # 细长条（页边装饰线/分隔条带）不是内容图：宽高比异常大/小 → 跳过
                if w / h > 8 or h / w > 8:
                    continue
                if any(_rect_intersect_area(bbox, t) >= area * 0.7 for t in taken):
                    continue  # 已被更大的图覆盖
                # 位图 bbox → 150dpi 像素，与 docling 检出的 figure/table bbox 同坐标系
                px_bbox = [v * scale for v in bbox]
                px_area = area * scale * scale
                # 已被 docling 检出的 figure/table bbox 覆盖 70% 以上 → 跳过
                if any(
                    _rect_intersect_area(px_bbox, ex) >= px_area * 0.7
                    for ex in existing_pixel_bbox.get(page_no, [])
                ):
                    continue
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
                print(f"[Docling] 第 {page_no} 页按位图区域补 {added} 个缺失图片")
    finally:
        pdf.close()


def _append_missing_page_texts(pdf_path: str, doc, rows: list[dict],
                               page_counter: dict[int, int]) -> None:
    """Docling 布局模型整页漏检（0 元素）时，用 PyMuPDF 文本块兜底补 body 元素。

    某些 PDF 页 docling 2.x 的 iterate_items 会完全无输出（pages dict 有该页，
    但没有任何 item），导致整页正文丢失。此时退回 PyMuPDF 逐块提取文本，
    按阅读顺序（y 自上而下）补成 body 元素，保证正文不因解析器遗漏而断裂。
    """
    try:
        import fitz
    except Exception:
        return
    try:
        pdf = fitz.open(pdf_path)
    except Exception:
        return
    try:
        pages_with_rows: set[int] = {r["page"] for r in rows}
        for page_no in range(1, len(pdf) + 1):
            if page_no in pages_with_rows:
                continue  # 该页 docling 已有输出，不重复兜底
            try:
                blocks = pdf[page_no - 1].get_text("blocks")
            except Exception:
                continue
            meaningful = [
                b for b in blocks
                if b[4] and len(b[4].strip()) > 1
                and not _looks_like_page_watermark(b[4])
            ]
            if not meaningful:
                continue
            merged_blocks = _merge_text_blocks(meaningful)
            try:
                page_h = float(doc.pages[page_no].size.height)
            except Exception:
                page_h = float(pdf[page_no - 1].rect.height)
            scale = 150.0 / 72.0
            for b in merged_blocks:
                x0, y0, x1, y1 = b["bbox"]
                idx = page_counter.get(page_no, 0) + 1
                page_counter[page_no] = idx
                # PyMuPDF 左上原点 → 应用 150dpi 像素 bbox
                rows.append({
                    "page": page_no,
                    "id": f"p{page_no}_e{idx}",
                    "type": "body",
                    "text": b["text"],
                    "bbox": [
                        round(x0 * scale, 1),
                        round(y0 * scale, 1),
                        round(x1 * scale, 1),
                        round(y1 * scale, 1),
                    ],
                    "caption": "",
                    "is_meaningful": True,
                    "description": "",
                    "section_name": "",
                    "font_size": 0.0,
                    "is_bold": False,
                    "level": 0,
                    "source": "pymupdf_fallback",
                })
            if merged_blocks:
                print(f"[Docling] 第 {page_no} 页 Docling 无任何输出，"
                      f"用 PyMuPDF 文本块兜底补 {len(merged_blocks)} 个正文段落")
    finally:
        pdf.close()


def _looks_like_page_watermark(text: str) -> bool:
    """判断文本块是否仅为铺页水印噪声（如 ARTICLE IN PRESS 旋转水印）。"""
    t = " ".join(text.split()).lower()
    if not t:
        return True
    return t in ("article in press", "article in press1", "article in press ")


def _merge_text_blocks(blocks: list[tuple]) -> list[dict]:
    """将 PyMuPDF 按行返回的文本块合并为自然段。

    文本层异常的 PDF 可能把每一行都暴露为独立 block。相邻行若处于
    同一栏且垂直间距接近行距，则合并；明显更大的间距保留为段落边界。
    """
    ordered = sorted(blocks, key=lambda b: (float(b[1]), float(b[0])))
    merged: list[dict] = []
    for block in ordered:
        text = " ".join(str(block[4]).split())
        if not text:
            continue
        x0, y0, x1, y1 = (float(block[0]), float(block[1]),
                          float(block[2]), float(block[3]))
        current = {
            "bbox": [x0, y0, x1, y1],
            "text": text,
            "_last_y0": y0,
            "_last_y1": y1,
        }
        if merged:
            prev = merged[-1]
            px0, py0, px1, py1 = prev["bbox"]
            prev_w = max(px1 - px0, 1.0)
            curr_w = max(x1 - x0, 1.0)
            overlap = min(px1, x1) - max(px0, x0)
            same_column = abs(x0 - px0) <= 10.0 and overlap >= min(prev_w, curr_w) * 0.55
            last_y0 = prev["_last_y0"]
            last_y1 = prev["_last_y1"]
            line_height = max(last_y1 - last_y0, y1 - y0, 1.0)
            gap = y0 - last_y1
            if same_column and 0.0 <= gap <= max(14.0, line_height * 1.05):
                joiner = "" if prev["text"].endswith("-") else " "
                if joiner == "":
                    prev["text"] = prev["text"][:-1] + text
                else:
                    prev["text"] += joiner + text
                prev["bbox"] = [
                    min(px0, x0), min(py0, y0), max(px1, x1), max(py1, y1),
                ]
                prev["_last_y0"] = y0
                prev["_last_y1"] = y1
                continue
        merged.append(current)
    return [
        {"bbox": item["bbox"], "text": item["text"]}
        for item in merged
    ]


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
    # 兜底 1：Docling 漏检的位图区域补成 figure 元素
    _append_missing_bitmaps(pdf_path, doc, rows, page_counter)
    # 兜底 2：Docling 整页无输出时用 PyMuPDF 文本块补正文
    _append_missing_page_texts(pdf_path, doc, rows, page_counter)
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
