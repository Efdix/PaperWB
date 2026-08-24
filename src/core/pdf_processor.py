"""PDF 智能处理器 —— 两阶段管线：本地版式解析 + 本地规则跨页整合。

Stage 1（自动触发）: PDF导入 → Docling 本地版式解析 → 结构化 JSON → 缓存到磁盘
Stage 2（点击论文）: 读缓存 → 本地规则组装（章节/引文/图表绑定 + 跨页接缝合并）→ StructuredDocument → UI渲染
"""

from __future__ import annotations

import glob
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

from ..utils.threads import track

if TYPE_CHECKING:
    from .llm_client import LLMClient


FAST_DOCUMENT_VERSION = 9
DOCLING_PARSER_VERSION = "docling_v3"


def _warm_up_converter(get_converter) -> None:
    """在普通 Python 线程里创建 docling converter（两套：OCR/非 OCR）。

    docling/torch 的转换器若在 QThread 内首次创建会与 Qt 事件循环死锁
    （权重加载后线程卡死，Stage 1 永不结束）。docling 的全局转换器缓存
    ``_converters`` 是跨线程共享的：这里先在普通线程建好并缓存，之后
    QThread 里的 ``parse_pdf`` 只会命中已就绪的转换器，从而避开死锁。
    """
    def _create() -> None:
        try:
            get_converter(False)
        except Exception:
            pass
        try:
            get_converter(True)
        except Exception:
            pass

    t = threading.Thread(target=_create, daemon=True)
    t.start()
    t.join()  # 阻塞等待：converter 就绪后 QThread 才继续，保证不触发线程内首次创建


# ============================================================
# 数据结构
# ============================================================

@dataclass
class StructuredElement:
    """单个结构化元素 —— 可来自单页解析或跨页整合后的结果。

    注意：bbox 在 JSON 中存为 list[float]，加载后由工厂方法转为 tuple。
    """

    element_type: str           # title | subtitle | authors | affiliations | abstract_heading
                                # abstract_body | body | keywords | figure | table
                                # figure_caption | table_caption | reference | metadata
                                # header_footer | publisher_logo | equation | acknowledgment
                                # appendix | unknown
    text: str = ""
    page: int = 0               # 所在页码（1-based）
    heading_level: int = 0      # 1=一级标题, 2=二级, 0=非标题
    font_size: float = 0.0
    is_bold: bool = False
    bbox: tuple[float, float, float, float] = (0, 0, 0, 0)
    image_path: str = ""        # 图片/表格截图路径（相对或绝对）
    image_caption: str = ""     # 图表标题文字
    image_description: str = "" # LLM 对图表内容的描述
    is_meaningful: bool = True  # 是否有学术意义（false=出版商logo等）
    display_priority: str = "normal"  # high | normal | low | collapsed
    section_name: str = ""      # 所属章节名（如 "Introduction"）
    element_id: str = ""        # 唯一标识，如 "p1_e3"

    @staticmethod
    def from_dict(d: dict) -> "StructuredElement":
        """从字典（JSON加载）构造 StructuredElement，处理 bbox 转换。

        容错：LLM 偶尔会把元素列表返回成字符串等非 dict 结构，此时降级为未知元素。
        """
        if not isinstance(d, dict):
            return StructuredElement(
                element_type="unknown",
                text=str(d or ""),
            )
        bbox_raw = d.get("bbox", [0, 0, 0, 0])
        if isinstance(bbox_raw, list) and len(bbox_raw) == 4:
            bbox = (float(bbox_raw[0]), float(bbox_raw[1]),
                    float(bbox_raw[2]), float(bbox_raw[3]))
        else:
            bbox = (0, 0, 0, 0)

        return StructuredElement(
            element_type=d.get("type", d.get("element_type", "unknown")),
            text=d.get("text", ""),
            page=d.get("page", 0),
            heading_level=d.get("heading_level", 0),
            font_size=float(d.get("font_size", 0)),
            is_bold=bool(d.get("is_bold", False)),
            bbox=bbox,
            image_path=d.get("image_path", ""),
            image_caption=d.get("caption", d.get("image_caption", "")),
            image_description=d.get("description", d.get("image_description", "")),
            is_meaningful=bool(d.get("is_meaningful", True)),
            display_priority=d.get("display_priority", "normal"),
            section_name=d.get("section_name", ""),
            element_id=d.get("id", d.get("element_id", "")),
        )

    def to_dict(self) -> dict:
        """序列化为字典（含 bbox 转 list）。"""
        return {
            "element_type": self.element_type,
            "text": self.text,
            "page": self.page,
            "heading_level": self.heading_level,
            "font_size": self.font_size,
            "is_bold": self.is_bold,
            "bbox": list(self.bbox),
            "image_path": self.image_path,
            "image_caption": self.image_caption,
            "image_description": self.image_description,
            "is_meaningful": self.is_meaningful,
            "display_priority": self.display_priority,
            "section_name": self.section_name,
            "element_id": self.element_id,
        }


@dataclass
class StructuredDocument:
    """跨页整合后的完整结构化文档。

    包含两类视图：
    - display_elements: 用于 UI 展示的排序后元素（弱化了元信息）
    - metadata_pool: 完整的元信息、参考文献等，可检索但不优先展示
    """

    title: str = ""
    authors: str = ""
    display_elements: list[StructuredElement] = field(default_factory=list)
    metadata_pool: list[StructuredElement] = field(default_factory=list)
    toc: list[dict] = field(default_factory=list)  # [{level, title, element_index}]
    figures: list[StructuredElement] = field(default_factory=list)
    tables: list[StructuredElement] = field(default_factory=list)
    references: list[StructuredElement] = field(default_factory=list)
    raw_page_count: int = 0

    @staticmethod
    def from_dict(d: dict) -> "StructuredDocument":
        """从字典恢复 StructuredDocument。

        容错：LLM 返回的元素列表可能混入字符串等非 dict 项，一律跳过。
        """
        if not isinstance(d, dict):
            d = {}

        def _elements(items) -> list[StructuredElement]:
            return [
                StructuredElement.from_dict(e)
                for e in items
                if isinstance(e, dict)
            ]

        return StructuredDocument(
            title=d.get("title", ""),
            authors=d.get("authors", ""),
            display_elements=_elements(d.get("display_elements", [])),
            metadata_pool=_elements(d.get("metadata_pool", [])),
            toc=[t for t in d.get("toc", []) if isinstance(t, dict)],
            figures=_elements(d.get("figures", [])),
            tables=_elements(d.get("tables", [])),
            references=_elements(d.get("references", [])),
            raw_page_count=d.get("raw_page_count", 0),
        )

    def to_dict(self) -> dict:
        """序列化为字典。"""
        return {
            "title": self.title,
            "authors": self.authors,
            "display_elements": [e.to_dict() for e in self.display_elements],
            "metadata_pool": [e.to_dict() for e in self.metadata_pool],
            "toc": self.toc,
            "figures": [e.to_dict() for e in self.figures],
            "tables": [e.to_dict() for e in self.tables],
            "references": [e.to_dict() for e in self.references],
            "raw_page_count": self.raw_page_count,
        }


@dataclass
class PageResult:
    """单页解析结果（Stage 1 输出，缓存单位）。"""

    page: int
    status: str = "pending"              # pending | processing | done | error
    elements: list[dict] = field(default_factory=list)
    page_role: str = "unknown"           # title_page | content_page | reference_page | ...
    raw_text: str = ""                   # PyMuPDF 原始提取文本（备用）
    error_message: str = ""
    processed_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "page": self.page,
            "status": self.status,
            "elements": self.elements,
            "page_role": self.page_role,
            "raw_text": self.raw_text,
            "error_message": self.error_message,
            "processed_at": self.processed_at,
        }

    @staticmethod
    def from_dict(d: dict) -> "PageResult":
        return PageResult(
            page=d.get("page", 0),
            status=d.get("status", "pending"),
            elements=d.get("elements", []),
            page_role=d.get("page_role", "unknown"),
            raw_text=d.get("raw_text", ""),
            error_message=d.get("error_message", ""),
            processed_at=d.get("processed_at", 0.0),
        )


@dataclass
class PageManifest:
    """页面缓存清单 —— 记录一篇 PDF 所有页的解析状态。"""

    pdf_path: str = ""
    pdf_md5: str = ""
    total_pages: int = 0
    pdf_mtime: float = 0.0
    pages: dict[int, str] = field(default_factory=dict)  # {page_num: status}
    created_at: float = 0.0
    updated_at: float = 0.0
    integration_version: int = 0  # 跨页整合版本号（变更prompt时可递增使缓存失效）
    parser: str = DOCLING_PARSER_VERSION  # 固定使用本地版式解析

    @property
    def done_count(self) -> int:
        return sum(1 for s in self.pages.values() if s == "done")

    @property
    def error_count(self) -> int:
        return sum(1 for s in self.pages.values() if s == "error")

    @property
    def is_complete(self) -> bool:
        return self.done_count + self.error_count >= self.total_pages

    @property
    def progress_ratio(self) -> float:
        if self.total_pages <= 0:
            return 0.0
        return (self.done_count + self.error_count) / self.total_pages

    def to_dict(self) -> dict:
        return {
            "pdf_path": self.pdf_path,
            "pdf_md5": self.pdf_md5,
            "total_pages": self.total_pages,
            "pdf_mtime": self.pdf_mtime,
            "pages": self.pages,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "integration_version": self.integration_version,
            "parser": self.parser,
        }

    @staticmethod
    def from_dict(d: dict) -> "PageManifest":
        pages = d.get("pages", {})
        # JSON keys 是字符串，转为 int
        return PageManifest(
            pdf_path=d.get("pdf_path", ""),
            pdf_md5=d.get("pdf_md5", ""),
            total_pages=d.get("total_pages", 0),
            pdf_mtime=d.get("pdf_mtime", 0.0),
            pages={int(k): v for k, v in pages.items()},
            created_at=d.get("created_at", 0.0),
            updated_at=d.get("updated_at", 0.0),
            integration_version=d.get("integration_version", 0),
            parser=d.get("parser", DOCLING_PARSER_VERSION),
        )


# ============================================================
# 页面解析结果校验
# ============================================================

VALID_ELEMENT_TYPES = frozenset({
    "title", "subtitle", "authors", "affiliations",
    "abstract_heading", "abstract_body", "body",
    "keywords", "figure", "table", "figure_caption", "table_caption",
    "reference", "metadata", "header_footer", "publisher_logo",
    "equation", "acknowledgment", "appendix", "unknown",
})


def _validate_page_result(raw: str, page_num: int) -> dict:
    """解析并校验 LLM 返回的单页 JSON。

    解析容错（代码围栏提取/括号切片/换行清洗/全角括号等 5 层）统一由
    json_utils.parse_json_response 处理，此处只做空值检查与结构规范化。

    Returns:
        {"page": int, "page_role": str, "elements": list[dict], "parse_error": str|None}
    """
    if not raw or not raw.strip():
        return _fallback_page_result(page_num, "LLM 返回为空")

    obj = _try_parse_json(raw)
    if obj is not None:
        return _normalize_page_result(obj, page_num)

    return _fallback_page_result(page_num, f"无法解析 LLM 返回的 JSON（前100字符: {raw[:100]}）")


def _try_parse_json(text: str) -> dict | None:
    from .json_utils import parse_json_response
    return parse_json_response(text)


def _normalize_page_result(obj: dict, page_num: int) -> dict:
    """校验并规范化单页解析结果。"""
    elements = obj.get("elements", [])
    if not isinstance(elements, list):
        elements = []

    normalized_elements = []
    for i, elem in enumerate(elements):
        if not isinstance(elem, dict):
            continue
        etype = elem.get("type", "unknown")
        if etype not in VALID_ELEMENT_TYPES:
            etype = "unknown"

        bbox = elem.get("bbox", [0, 0, 0, 0])
        if not isinstance(bbox, list) or len(bbox) != 4:
            bbox = [0, 0, 0, 0]

        normalized = {
            "id": elem.get("id", f"p{page_num}_e{i}"),
            "type": etype,
            "text": str(elem.get("text") or elem.get("content") or ""),
            "bbox": [float(v) for v in bbox],
            "font_size": float(elem.get("font_size", 0)),
            "is_bold": bool(elem.get("is_bold", False)),
            "caption": str(elem.get("caption") or elem.get("image_caption") or ""),
            "is_meaningful": bool(elem.get("is_meaningful", True)),
            "description": str(elem.get("description", "")),
            "section_name": str(elem.get("section_name", "")),  # Stage 1 可选建议
            "image_path": str(elem.get("image_path", "")),
        }
        normalized_elements.append(normalized)

    return {
        "page": int(obj.get("page", page_num)),
        "page_role": str(obj.get("page_role", "unknown")),
        "elements": normalized_elements,
        "parse_error": None,
    }


def _fallback_page_result(page_num: int, error: str) -> dict:
    """JSON 解析失败时的降级结果。"""
    return {
        "page": page_num,
        "page_role": "unknown",
        "elements": [],
        "parse_error": error,
    }


# ============================================================
# 整合结果校验
# ============================================================

def _validate_integration_result(raw: str) -> dict:
    """解析并校验 LLM 返回的整合 JSON（容错统一由 json_utils 处理）。"""
    if not raw or not raw.strip():
        return {"error": "LLM 返回为空"}

    obj = _try_parse_json(raw)
    if obj is not None:
        return obj

    return {"error": f"无法解析整合结果 JSON（前100字符: {raw[:100]}）"}


# ============================================================
# 快速规则组装（Stage 2 主路径，不调用全量 LLM 整合）
# ============================================================

_FAST_SKIP_TYPES = frozenset({"header_footer", "publisher_logo", "unknown"})

_FAST_PRIORITY: dict[str, str] = {
    "title": "high",
    "subtitle": "high",
    "abstract_heading": "high",
    "abstract_body": "high",
    "body": "normal",
    "keywords": "low",
    "authors": "low",
    "affiliations": "low",
    "metadata": "low",
    "figure": "high",
    "table": "high",
    "figure_caption": "normal",
    "table_caption": "normal",
    "equation": "normal",
    "reference": "collapsed",
    "acknowledgment": "collapsed",
    "appendix": "collapsed",
}

# 参与跨页接缝检测的文本元素类型
_SEAM_TEXT_TYPES = frozenset({"body", "abstract_body"})

# 铺页水印（ARTICLE IN PRESS 等），docling 可能把水印并进正文段落开头或中间
_WATERMARK_RE = re.compile(
    r"\bARTICLE\s+IN\s+PRESS\d*\b|\bARTICLE\s+IN\s+PRESS\b", re.IGNORECASE
)
_LIGATURE_STOP_PREFIXES = frozenset({
    "a", "an", "and", "as", "at", "by", "for", "from", "in", "of",
    "on", "or", "the", "this", "to", "with",
})


def _repair_ligature_spacing(match: re.Match[str]) -> str:
    """修复 fi/fl 连字拆分，同时保留正常的 ``to fi gure`` 词间空格。"""
    prefix = match.group("prefix") or ""
    fragment = match.group("fragment")
    suffix = match.group("suffix")
    if prefix and prefix.lower() in _LIGATURE_STOP_PREFIXES:
        return f"{prefix} {fragment}{suffix}"
    return f"{prefix}{fragment}{suffix}"


def _strip_watermarks(text: str) -> str:
    """从元素文本中剥离铺页水印（如 ARTICLE IN PRESS），压缩多余空白。

    水印可能出现在段落开头（摘要/正文被整体误判）或段落中间
    （docling 把旋转水印识别进正文），剥离后保留真实正文。
    """
    if not text:
        return ""
    t = _WATERMARK_RE.sub(" ", text)
    # 软连字符（U+00AD）是断行连字残留，直接拼回单词（``inter\xad twined``）；
    # nbsp/窄 nbsp 等 Unicode 空白归一成普通空格。
    t = re.sub(r"\xad\s?", "", t)
    t = re.sub("[\u00a0\u202f\u2007\u2009\u2002\u2003]", " ", t)
    # 部分 PDF 字体把 ff/fi/fl/ffi/ffl 连字拆成独立文本片段，Docling 会输出
    # ``pro fi ling``、``di ff erent`` 等形式；同时修复标题中 ``AStudy``
    # 连写。只处理高置信模式，不做激进合并：单个大写字母 + 空格 +
    # 小写词在正常学术英语中大量存在（T cell / B cells / X axis /
    # G protein / N terminal），无条件合并会永久损坏正文文本。
    t = re.sub(r"\bA(?=[A-Z][a-z])", "A ", t)
    t = re.sub(
        r"\b(?:(?P<prefix>[A-Za-z]+)\s+)?(?P<fragment>ffi|ffl|ff|fi|fl)\s+"
        r"(?P<suffix>[a-z]+)\b",
        _repair_ligature_spacing,
        t,
        flags=re.IGNORECASE,
    )
    # 字母间的空格连字符是断行残留（``Hong -Hu``、``broad -leaved``、
    # ``E -mail``），拼回原词；数字两侧不动（保护 ``pages 3 - 4`` 类区间）。
    t = re.sub(r"(?<=[A-Za-z])\s+-\s*(?=[A-Za-z])", "-", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t


# 期刊页眉/版头黑名单（按前缀/精确匹配剔除，避免当标题或小节显示）
_RUNNING_HEAD_BLACKLIST = (
    "article in press", "article in press1", "article in press ",
    "received:", "accepted:", "published online:", "cite this article",
    "doi:", "open access", "copyright", "© ", "issn", "volume ",
    "www.", "http://", "https://doi",
    "email:", "e-mail:", "e mail:", "contact email",
    "we are providing", "unedited version", "if this paper is publishing",
    "transparent peer", "the author(s)", "this article is a",
    "this article was submitted",
    # 出版流程/声明行（PNAS/eLife/预印本等首页模板，任何页面出现都属噪声）
    "author contributions", "author affiliations",
    "competing interest", "declaration of interest",
    "the authors declare", "competing financial interest",
    "creative commons", "license, which permits",
    "funding:", "reviewed preprint", "edited by", "academic editor",
    "reviewing editor", "to whom correspondence",
    "this article contains supporting", "pnas direct submission",
    "these authors contributed", "corresponding author",
)


def _is_running_head(text: str) -> bool:
    """判断文本是否为期刊运行页眉/版头噪声（如 ARTICLE IN PRESS）。

    先剥离铺页水印再判断：
    - 原文本为空（如 figure/table 元素只有截图无文字）→ 不是 running head，保留
    - 原文非空但剥离水印后为空（如封面大图区被识别成纯水印 text）→ 纯噪声，剔除
    - 水印+正文（如摘要被并入水印）→ 剥离后保留正文，避免整段误杀
    """
    raw = (text or "").strip()
    if not raw:
        return False  # 空元素（图/表）不是噪声
    t = _strip_watermarks(raw)
    if not t:
        return True  # 剥离水印后为空 → 纯噪声
    low = t.lower()
    return any(low.startswith(p) for p in _RUNNING_HEAD_BLACKLIST)


def _guess_title_fallback(page_data: list[dict]) -> str:
    """Docling 未标出 title 元素时，从第 1 页上半区挑最可能的标题候选。

    排序依据：元素类型优先级（title > subtitle > body）+ 文本长度；
    期刊版头/未编辑声明等模板噪声由黑名单排除。
    """
    first_page = None
    for page in page_data:
        if int(page.get("page", 0)) == 1:
            first_page = page
            break
    if first_page is None:
        return ""
    try:
        page_bottom = max(
            float(e.get("bbox", [0, 0, 0, 0])[3])
            for e in first_page.get("elements", [])
            if isinstance(e, dict) and e.get("bbox")
        )
        top_limit = page_bottom * 0.55
    except (TypeError, KeyError, IndexError, ValueError):
        top_limit = 400.0
    type_rank = {"title": 0, "subtitle": 1, "body": 2}
    candidates: list[tuple[int, float, str]] = []
    for e in (first_page.get("elements") or []):
        if not isinstance(e, dict):
            continue
        etype = e.get("type")
        if etype not in type_rank:
            continue
        text = (e.get("text") or "").strip()
        if not (12 <= len(text) <= 300):
            continue
        if _is_running_head(text):
            continue
        try:
            b = e.get("bbox") or ()
            y_center = (float(b[1]) + float(b[3])) / 2.0
        except (TypeError, KeyError, IndexError, ValueError):
            continue
        if y_center > top_limit:
            continue
        candidates.append((type_rank[etype], len(text), text))
    if not candidates:
        return ""
    candidates.sort(key=lambda c: (c[0], -c[1]), reverse=False)
    return candidates[0][2]


def _figure_internal_text(e: dict, figure_bboxes: list[list[float]]) -> bool:
    """元素大部分落在同页某 figure/table 区域内 → 视为图内文字，不单独展示。"""
    try:
        eb = e.get("bbox") or ()
        ex0, ey0 = float(eb[0]), float(eb[1])
        ew = float(eb[2]) - ex0
        eh = float(eb[3]) - ey0
        if ew * eh <= 0:
            return False
        for fb in figure_bboxes:
            w = min(float(eb[2]), float(fb[2])) - max(ex0, float(fb[0]))
            h = min(float(eb[3]), float(fb[3])) - max(ey0, float(fb[1]))
            if w > 0 and h > 0 and w * h >= 0.8 * ew * eh:
                return True
    except (TypeError, KeyError, IndexError, ValueError):
        pass
    return False


_FIGURE_CAPTION_RE = re.compile(
    r"^(?:fig(?:ure)?|table|extended\s+data|supplementary\s+fig(?:ure)?)\s*"
    r"[.:]?\s*\d+",
    re.IGNORECASE,
)


def _looks_like_figure_caption(text: str) -> bool:
    """识别图版页上仍应保留的明确图注。"""
    return bool(_FIGURE_CAPTION_RE.match(_strip_watermarks(text)))


def _is_decorative_figure(element: dict, page_elements: list[dict],
                           page_no: int) -> bool:
    """过滤出版社 Logo/更新图标等无图注装饰图，不影响正文大图。

    - 无 caption/area<2500 的小图 → 装饰图（出版社徽标）；
    - 首页无图注 + 占页面小部分 / 大幅占位 → 封面图/Graphical Abstract；
    - 任意页：无图注 + 位于页面顶部页眉带（bbox y 距页顶 < 10%）且
      高度小于正文行的 figure → 页眉 logo（如 Cell/CellPress 重复水印）。
    """
    if element.get("type") != "figure":
        return False
    if not element.get("is_meaningful", True):
        return True
    if (element.get("text") or "").strip() or (element.get("caption") or "").strip():
        return False
    bbox = element.get("bbox") or []
    if len(bbox) != 4:
        return False
    area = max(0.0, float(bbox[2]) - float(bbox[0])) * max(
        0.0, float(bbox[3]) - float(bbox[1])
    )
    if area < 2500.0:
        return True
    boxes = [
        e.get("bbox") for e in page_elements
        if len(e.get("bbox") or []) == 4
    ]
    if not boxes:
        return False
    page_w = max(float(b[2]) for b in boxes)
    page_h = max(float(b[3]) for b in boxes)
    page_area = max(page_w * page_h, 1.0)
    # 首页无图注 + 占页面小部分 → 出版社 Logo/更新徽标
    if page_no == 1 and area / page_area < 0.25:
        return True
    # 首页无图注 + 大幅占位 → 封面图/Graphical Abstract（UI 单独展示）
    if page_no == 1 and area / page_area >= 0.30:
        return True
    # 任意页页眉水印：y 接近页顶（< 10%）、高度 ≤ 200px（远小于正文大图）、
    # 宽度 ≤ 25% 页面宽 → Cell/CellPress 等重复页眉 logo
    ex0, ey0, ex1, ey1 = (float(v) for v in bbox)
    if ey0 < page_h * 0.10 and (ey1 - ey0) <= 200 \
            and (ex1 - ex0) <= page_w * 0.25:
        return True
    return False


def _is_front_matter_noise(text: str, page_no: int, element_type: str) -> bool:
    """过滤首页出版社模板碎片，不误删正文中的同名术语（仅在首页生效）。

    覆盖常见期刊版头：刊名卷期行（Annu. Rev. ... 2007. 38:179-201）、
    在线访问声明、doi 行、ISSN/报价行（1543-592X/07/1201-0179$20.00）、
    栏目类型标签（Review / Research Article / Brief Communication）、
    通讯作者与收稿日期行、出版流程信息（Edited by / Published / 学术编辑）。
    """
    if page_no != 1 or element_type not in ("body", "subtitle", "metadata"):
        return False
    t = _strip_watermarks(text).strip()
    if not t:
        return True
    low = t.lower().rstrip(" .:!；;")
    # 栏目类型标签（整词精确匹配）：标题上方/下方的文章类型标注
    if low in {
        "article", "articles", "research article", "research articles",
        "review", "review article", "mini review", "minireview",
        "brief communication", "short communication", "microarticle",
        "case report", "perspective", "commentary", "editorial",
        "corrigendum", "erratum", "original article", "original research",
        "technical advance", "resource", "opinion", "news", "forum",
        "check for updates", "article type",
    }:
        return True
    if "doi.org/" in low or low.startswith("https://doi") \
            or re.match(r"^\(?https?://doi", low):
        return True
    if low.startswith(("annu. rev.", "the annual review of", "this article's doi",
                       "article's doi:", "doi:")):
        return True
    if "is online at http" in low or low.startswith("www."):
        return True
    # ISSN/卷期页报价行：1543-592X/07/1201-0179$20.00
    if re.match(r"^\d{4}-?\d{3}[xX]/", t) and "$" in t[20:40]:
        return True
    # 收稿/录用/出版日期行（Received 23 September 2024; Accepted …；
    # Published: 29 March 2024 / Published online: xx xx xxxx）
    if re.match(r"^\*?\s*(?:received|accepted|published|revised)\b", low) \
            and (re.search(r"(?:19|20)\d{2}", low) or low.endswith("online")
                 or low == "published") \
            and len(t) <= 160:
        return True
    # 学术编辑/责任编辑行（PeerJ/PNAS 等流程信息）
    if re.match(r"^\*?\s*(?:academic\s+editor|reviewing\s+editor|edited\s+by"
                r"|handling\s+editor)\b", low):
        return True
    # 通讯作者行（单复数/中英文括号/星号前缀：
    # *Author(s) for correspondence: / †Corresponding author(s). E-mail(s):）
    if re.match(r"^\*?\s*(?:authors?\s+for\s+correspondence"
                r"|corresponding\s+authors?|correspondence)\s*[:.]", low) \
            or re.match(r"^[+*†‡§]?\s*correspondence\s*:?", low) \
            or re.match(r"^\d?\s*to\s+whom\s+correspondence", low):
        return True
    # 通讯作者行变体（*Correspondence author(s). E-mail(s): xxx@yyy）：
    # 以 correspondence 开头且带邮箱 → 版头信息行
    if re.match(r"^\*?\s*correspond", low) and re.search(r"@\w+\.\w{2,}", t):
        return True
    # 贡献声明（These authors contributed equally to this work.）
    if re.match(r"^[+*†‡§]?\s*(?:these\s+)?authors?\s+contributed\s+equally", low):
        return True
    # 邮箱行（单个或多个空格分隔，容忍句尾标点）：
    # jiajepeng@nwpu.edu.cn. / liushk@ouc.edu.cn qili66@ouc.edu.cn
    stripped = t.rstrip(".,;。，；")
    emails = re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", stripped)
    if emails and re.sub(r"\s+", "", stripped) == re.sub(
            r"\s+", "", "".join(emails)):
        return True
    # ORCID / 作者信息索引行
    if re.search(r"orcid\.org", low):
        return True
    # 版权/许可声明（© The Author(s) … / Creative Commons Attribution …）
    if re.match(r"^copyright\b|^©", low) or "creative commons" in low \
            or "rights reserved" in low or "which permits unrestricted" in low:
        return True
    # 纯 DOI 值行
    if re.match(r"^10\.\d{4,9}/", t):
        return True
    return len(t) <= 60 and not re.search(r"[a-z\u4e00-\u9fff]", t, re.IGNORECASE)


# 常见期刊名（页眉/版头标签，Docling 常标成 subtitle）：不得当作文章标题，
# 也不关闭 front matter 状态机
_JOURNAL_NAME_RE = re.compile(
    r"^(?:nature\s+[a-z&]+|cell(?:[:\s]|$)|molecular\s+cell|developmental\s+cell|"
    r"cancer\s+cell|cell\s+(?:reports?|systems?|stem\s+cell|metabolism|"
    r"host\s+&?\s*microbe|chemical\s+biology)|current\s+biology|neuron|"
    r"science(?:\s+advances?|\s+immunology|\s+robotics|\s+signaling|"
    r"\s+translational\s+medicine)?|pnas|proceedings\s+of\s+the\s+national\s+"
    r"academy\s+of\s+sciences|elife|embo\s+journal|plos\s+(?:biology|"
    r"computational\s+biology|genetics|pathogens|one)|bmc\s+(?:biology|"
    r"bioinformatics|genomics|medicine|plant\s+biology)|genome\s+biology|"
    r"genome\s+research|nucleic\s+acids\s+research|bioinformatics|"
    r"briefings\s+in\s+bioinformatics|molecular\s+systems\s+biology|"
    r"molecular\s+biology\s+and\s+evolution|the\s+american\s+naturalist|"
    r"journal\s+of\s+molecular\s+biology|febs\s+letters|protein\s+&?\s*cell|"
    r"cell\s+research|science\s+china\s+life\s+sciences|"
    r"national\s+science\s+review)\s*$",
    re.IGNORECASE,
)

# 摘要/小结小节名（映射为摘要标题卡；其下正文映射为摘要正文卡）
_ABSTRACT_HEADING_RE = re.compile(
    r"^(?:abstract|summary|significance|graphical\s+abstract|highlights?)\s*$"
    r"|^摘要$",
    re.IGNORECASE,
)

# 常规章节名（状态机里短行小节标题不得跳过：``Methods`` 等是真实小节）
_SECTION_TITLE_RE = re.compile(
    r"^(?:\d+(?:[.)]\s*|\s+))?(?:introduction|methods?|"
    r"materials?\s+(?:and\s+methods?)?|results?(?:\s+and\s+discussion)?|"
    r"discussion|conclusions?|background|data\s+availability|"
    r"acknowledg(e)?ments?|funding|references?|supplementary\s+materials?)\s*$",
    re.IGNORECASE,
)

# 首页出版社模板小节名：其下条目（Highlights 列表/作者名单/邮箱等）
# 是版面元信息而非正文，整块剔除（仅首页生效，见 _front_matter_block_ids）
_FRONT_MATTER_SECTION_RE = re.compile(
    r"^(?:graphical?\s+(?:abstract|abstract\s*text)|highlights?|"
    r"authors?(?:\s+list)?|authors?\s+and\s+affiliations?|"
    r"for\s+correspondence|\*?\s*correspondence|"
    r"(?:reviewing\s+)?editor(?:ial\s+board)?|edited\s+by|reviewed\s+by|"
    r"specialty\s+section|citation|in\s+brief|lead\s+contact|"
    r"funding|competing\s+(?:interests?|statements?)|"
    r"e[- ]?mail(?:\s+addresses?)?)\s*:?\s*$",
    re.IGNORECASE,
)

# 单位块特征：机构词 + 编号段（``1 Department of …``），配合邮箱/邮编识别
_INSTITUTION_WORD_RE = re.compile(
    r"\b(?:universit|institut|college|academ|department|laborator|"
    r"school\s+of|center\s+for|centre\s+for|museum|hospital|faculty|"
    r"key\s+laborator)\w*", re.IGNORECASE,
)
_NUMBERED_AFFIL_START_RE = re.compile(r"^[(\[{]?\d{1,2}[*)\]}]?\s+[A-Za-z]")
_NUMBERED_AFFIL_SEG_RE = re.compile(r"[.;]\s*\d{1,2}\s+[A-Z]")
# 作者行特征：姓名（``Wei-hang Geng`` / ``Allen W. Zhang`` / ``Ciara O'Flanagan``）
_INITIAL_RE = re.compile(
    r"\b[A-Z][A-Za-z'\-]*(?:[ .][A-Z]\.?)?\s+[A-Z][A-Za-z'\-]+"
)
# 摘要开头散文信号词（区分摘要与正文）
_ABSTRACT_PROSE_RE = re.compile(
    r"\b(?:background|aims?|objectives?|results?|conclusions?|methods?)\b\s*"
    r"[:.]|the\s+(?:aim|objective|purpose)\s+of|here\s+we\b|in\s+this\s+study\b|"
    r"\bwe\b|\bour\b|"
    r"\bwe\s+(?:show|report|present|demonstrate|describe|investigate|provide)\b|"
    r"\babstract\b",
    re.IGNORECASE,
)
_PROSE_MARKER_RE = re.compile(
    r"\b(?:furthermore|moreover|however|although|therefore|thus|meanwhile)\b,"
    r"?|\bwe\b|\bour\b|\bthis\s+(?:study|paper|work)\b|\bthese\s+results\b",
    re.IGNORECASE,
)
_POSTAL_CODE_RE = re.compile(
    r"\b\d{5,6}(?:-\d{4})?\b"          # 美/中/欧大陆邮编
    r"|[A-Z]{1,2}\d{1,2}[A-Z]?\s+\d[A-Z]{2}\b"  # 英式邮编 CF10 3AT
)
_CONTACT_INFO_RE = re.compile(
    r"\be[- ]?mail\b|@\w+\.\w{2,}|\b(?:tel|fax|phone)\b", re.IGNORECASE
)


def _is_affiliation_block(text: str, page_no: int) -> bool:
    """判断是否为作者单位列表块（仅前两页生效）。

    特征组合：机构词密度 + 编号段/编号开头 + 邮编或联系方式，
    且不含正文散文标记（furthermore/we/this study 等）——混排了
    正文散文的长块一律保留，避免误删真实内容。
    """
    if page_no > 2:
        return False
    t = (text or "").strip()
    if not t or len(t) > 1500:
        return False
    if any("\u4e00" <= ch <= "\u9fff" for ch in t):
        return False
    inst_words = len(_INSTITUTION_WORD_RE.findall(t))
    if not inst_words:
        return False
    numbered_start = bool(_NUMBERED_AFFIL_START_RE.match(t))
    numbered_segs = len(_NUMBERED_AFFIL_SEG_RE.findall(t))
    has_postal = bool(_POSTAL_CODE_RE.search(t))
    has_contact = bool(_CONTACT_INFO_RE.search(t))
    # 剥离声明句（``* These authors contributed equally to this work.`` /
    # ``Correspondence and requests for materials...``）后再查散文信号，
    # 否则单位块尾巴上的声明会误判为正文散文而拒绝识别
    strip_notes = re.sub(
        r"(?:[+*†‡§]?\s*these\s+authors\s+contributed\s+equally[^.]*\.|"
        r"correspondence\s+and\s+requests[^.]*\.)",
        "", t, flags=re.IGNORECASE)
    if _PROSE_MARKER_RE.search(strip_notes):
        return False  # 含正文散文 → 混排块，宁可不删
    short = len(t) <= 300
    if numbered_segs >= 2 and (inst_words >= 2 or has_contact or has_postal):
        return True
    if numbered_start and inst_words >= 1 and short:
        return True  # 编号开头的单位行（整块或单机构短行均适用）
    if numbered_start and inst_words >= 2 and short:
        return True
    # 无编号的孤立单位行/单位碎片（Texas 77555, and Florida Medical …）
    if inst_words >= 1 and has_postal and short \
            and not t.endswith(("。", "！")):
        return True
    # 机构词开头且无句终标点的多机构块（AnnRev/MDPI 等无编号单位列表）
    if inst_words >= 2 and len(t) > 80 \
            and not _ends_sentence(t) and not t.endswith(("。", "！")):
        return True
    return False


def _is_affiliation_fragment(text: str, page_no: int) -> bool:
    """首页正文开始前的单行单位碎片（无编号段的机构行，如 ``1 Chinese
    Academy of Sciences, China``）。仅在 front matter 状态机内调用——
    正文开始后不再参与，避免误删真实内容。"""
    if page_no != 1:
        return False
    t = (text or "").strip()
    if not t or len(t) > 80:
        return False
    if re.search(r"[.!?。！？](?:\s|$)", t):
        return False
    if _PROSE_MARKER_RE.search(t):
        return False
    if _NUMBERED_AFFIL_START_RE.match(t):
        return True
    return bool(_INSTITUTION_WORD_RE.search(t[:20]))


def _is_author_line(text: str, page_no: int) -> bool:
    """作者行识别（仅首页）：≥2 个「姓名+编号/†* 上标」模式。

    Docling 常把作者行标成 body；单位块/摘要不以「姓名+编号」开头，
    可借此区分（作者行与单位混排时也归入作者行）。
    """
    if page_no > 2:
        return False
    t = (text or "").strip()
    if not t or len(t) > 1200:
        return False
    # 以句终标点结尾 → 不是作者行（容忍缩写点如 ``David M. Irwin``）
    if re.search(r"[.!?。！？]\s*$", t):
        return False
    # 以编号开头（``1 Collaborative Innovation Center…``）→ 单位行而非作者行
    if _NUMBERED_AFFIL_START_RE.match(t):
        return False
    # 前 80 字符内出现机构词 → 是作者行+单位混排行或纯单位块，不是作者行
    if re.search(r"\b(?:universit|institut|college|academ|department|"
                 r"laborator|school\s+of|center\s+for|centre\s+for|"
                 r"key\s+laboratory)\w*", t[:80], re.IGNORECASE):
        return False
    names = len(_INITIAL_RE.findall(t))
    if names < 2:
        return False
    # 上标标记：数字编号（``Geng 1``）或字母上标（PNAS 版式 ``Duvall a``）
    markers = len(re.findall(
        r"\b[A-Z][a-zA-Z'\-]+\s*[,(]?\s*(?:\d+|[a-z])(?=\s*[,;]|$)", t))
    return markers >= 2


def _is_keywords_line(text: str, page_no: int) -> bool:
    """关键词行识别（首页/次页）：Keywords / Key words / 关键词 前缀。"""
    if page_no > 2:
        return False
    t = (text or "").strip()
    return bool(re.match(r"^(?:key\s*words?|keywords?|关键词)\s*:?", t.lower()))


def _looks_like_abstract(text: str, ordered: list[dict], i: int,
                         page_no: int) -> bool:
    """首页正文开始前的长 body 是否摘要：有散文信号词，或其后紧跟
    关键词行/小节标题（如 Frontiers 无 Abstract 小节的版式）。"""
    if page_no != 1 or len(text) < 120:
        return False
    if _NUMBERED_AFFIL_START_RE.match(text):
        return False  # 编号开头的单位行不是摘要
    if _ABSTRACT_PROSE_RE.search(text):
        return True
    for nxt in ordered[i + 1:i + 6]:
        if not isinstance(nxt, dict):
            continue
        nt = (nxt.get("text") or "").strip()
        if not nt:
            continue
        if _is_keywords_line(nt, 1):
            return True
        if nxt.get("type") == "subtitle" \
                and not _FRONT_MATTER_SECTION_RE.match(nt):
            return True
    return False


def _is_article_title(text: str, e: dict, page1_h: float | None) -> bool:
    """页首 subtitle 是否文章标题（用于把 Docling 标成 subtitle 的标题
    渲染为标题卡并写入 doc.title）。"""
    t = (text or "").strip()
    if not (20 <= len(t) <= 300):
        return False  # 过短（期刊名/栏目标签等）或过长（正文段）都不是标题
    if _FRONT_MATTER_SECTION_RE.match(t) or _is_running_head(t):
        return False
    if _JOURNAL_NAME_RE.match(t):
        return False  # 期刊名（Nature Communications / Cell 等）不是文章标题
    # 排除编号小节（1 Introduction / 2.1 Methods）与常见章节名——正文
    # 小节标题不是文章标题
    if re.match(r"^\d+(?:[.)]\s*|\s+)", t) or re.match(
            r"^(?:introduction|methods?|materials?\s+(?:and\s+methods?)?|"
            r"results?(?:\s+and\s+discussion)?|discussion|conclusions?|"
            r"background|abstract|summary|data\s+availability|"
            r"acknowledg(e)?ments?|funding|references?)\s*$", t, re.IGNORECASE):
        return False
    if page1_h:
        b = e.get("bbox") or []
        try:
            if float(b[3]) > page1_h * 0.6:
                return False  # 页面下半区的 subtitle 不是标题
        except (TypeError, ValueError):
            pass
    return True


def _is_bare_number_text(text: str) -> bool:
    """上标引用编号被拆成的裸数字元素（如 ``31``）。"""
    return bool(re.fullmatch(r"\d{1,3}", (text or "").strip()))


def _front_matter_block_ids(page_data: list[dict]) -> set[str]:
    """首页模板区块（Graphical Abstract/Highlights/Authors 等）元素 id。

    从模板小节标题起收集后续短条目，直到遇到下一个非模板小节标题；
    超过 400 字符的段落视为已进入正文，停止收集防误删。
    """
    block: set[str] = set()
    for page in page_data:
        if int(page.get("page", 0)) != 1:
            continue
        collecting = False
        for e in page.get("elements", []):
            if not isinstance(e, dict) or not e.get("id"):
                continue
            etype = e.get("type")
            text = _strip_watermarks(e.get("text") or "").strip()
            if etype == "subtitle":
                collecting = bool(text) and bool(
                    _FRONT_MATTER_SECTION_RE.match(text)
                )
                if collecting:
                    block.add(str(e["id"]))
                continue
            if not collecting or etype not in ("body", "metadata", "authors"):
                continue
            if len(text) > 400:
                collecting = False  # 长段落 → 已是正文，停止收集
                continue
            block.add(str(e["id"]))
    return block


def _figure_plate_pages(page_data: list[dict]) -> set[int]:
    """识别以整页图版为主的页面，避免把坐标轴标签当正文卡片。

    论文正文中的单个插图不触发该规则；只有页面上有多幅图且文字大多是
    短标签，或一幅图覆盖页面主要区域时才认定为图版页。
    """
    plate_pages: set[int] = set()
    for page in page_data:
        try:
            page_no = int(page.get("page", 0))
        except (TypeError, ValueError):
            continue
        elements = [e for e in page.get("elements", []) if isinstance(e, dict)]
        figures = [
            e for e in elements
            if e.get("type") in ("figure", "table")
            and len(e.get("bbox") or []) == 4
        ]
        if not figures:
            continue
        boxes = [e["bbox"] for e in elements if len(e.get("bbox") or []) == 4]
        if not boxes:
            continue
        page_w = max(float(b[2]) for b in boxes)
        page_h = max(float(b[3]) for b in boxes)
        page_area = max(page_w * page_h, 1.0)
        areas = [
            max(0.0, float(e["bbox"][2]) - float(e["bbox"][0]))
            * max(0.0, float(e["bbox"][3]) - float(e["bbox"][1]))
            for e in figures
        ]
        largest_ratio = max(areas, default=0.0) / page_area
        text_items = [
            _strip_watermarks(e.get("text") or "")
            for e in elements
            if e.get("type") not in (
                "figure", "table", "figure_caption", "table_caption",
            )
        ]
        text_items = [t for t in text_items if t]
        short_ratio = (
            sum(len(t) <= 80 for t in text_items) / len(text_items)
            if text_items else 1.0
        )
        if (
            len(figures) >= 3 and short_ratio >= 0.75
        ) or (
            largest_ratio >= 0.55 and short_ratio >= 0.75
        ):
            plate_pages.add(page_no)
    return plate_pages


def _sorted_elements(elements: list[dict]) -> list[dict]:
    """按 Docling 版面阅读顺序返回元素（保持缓存原生顺序）。

    Docling 的 iterate_items 已按版面阅读顺序输出（含双栏：先左栏再右栏，
    栏内自上而下）。早期曾用 bbox 的 (y, x) 重排，但那会把双栏版式打乱
    （如参考文献列表右栏整体插入左栏之间、正文段落混入文献区），
    因此这里不再重排，直接信任 Docling 顺序。
    """
    return list(elements)


# 句尾缩写点不算句子终止（et al. / Fig. / e.g. / vs. 等）
_ABBREV_TAIL_RE = re.compile(
    r"\b(?:et\s+al|e\.g|i\.e|vs|cf|ca|Fig|Figs|Eq|Eqs|Ref|Refs|Sec|Secs"
    r"|No|Nos|Vol|Dr|Prof|Mr|Ms|St|approx|ed|eds)\.\s*$",
    re.IGNORECASE,
)
_CJK_TERMINAL_ENDS = ("。", "！", "？", "；", "：", "…", "——")

# 参考文献条目开头（作者姓 + 名首字母缩写 + 年份），跨页配对时不得并入正文
_REFERENCE_ENTRY_RE = re.compile(
    r"^\s*[A-Z][A-Za-z'\-]+(?:\s+(?:[A-Z]\.){1,3}){1,3}[^a-z]{0,4}"
    r"|[A-Za-z\-]{2,}\.(?:19|20)\d{2}"
    r"|\b(?:19|20)\d{2}[;.,]?\s*\d{1,4}\s*[:.:]\s*\d{1,5}",
)


def _ends_sentence(text_a: str) -> bool:
    """判断段落是否以真正的句终标点收尾（容忍缩写点与收尾引号）。"""
    t = re.sub(r"[\]\[”’\"')\s]+$", "", text_a or "")
    if not t:
        return True
    if not re.search(r"[.!?:;…]$|[.!?][\"'”’]$", t):
        return False
    return not _ABBREV_TAIL_RE.search(t)


def _looks_like_reference_entry(text_b: str) -> bool:
    """b 段是否像一条参考文献的开头（防止把文献列表当跨页续文合并）。"""
    head = (text_b or "").strip()[:60]
    if not head:
        return False
    return bool(_REFERENCE_ENTRY_RE.search(head))


def _looks_like_continuation(text_a: str, text_b: str) -> bool:
    """规则判断 text_b 是否可能是 text_a 的段落跨页延续。

    - 上一页末段以连字符结尾 → 强信号（断词）
    - 英文：a 未以真句终标点结尾，且 b 以小写字母/引号/括号/数字开头
      （大写开头的续文如 RNA-seq、专有名词同样常见）
    - a 无任何终止标点时接受大写开头的 b（句子明显未写完）
    - a 无任何终止标点时接受大写开头的 b（句子明显未写完）
    - 中文：a 不以句号/问号/感叹号/分号结尾（中文无大小写，20 字以上即接）
    - b 是参考文献条目开头 → 不合并
    - b 是图注占位/分段标签（(legend on next page) / (A) Blood feeding…）→ 不合并
    """
    a = text_a.rstrip()
    b = text_b.lstrip()
    if not a or not b:
        return False
    if len(b) < 12:
        return False
    # 图注占位行与图注分段标签（Figure N. 引出的 (A)/(B)… 面板说明）是版式
    # 元素而非续文，规则层直接拒接，避免无 LLM 路径把图注并进正文
    b_head = b[:80]
    if b_head.lower().startswith("(legend on next page)") \
            or b_head.lower().startswith("(legend continues"):
        return False
    if re.match(r"^\([A-Za-z](?:-[A-Za-z])?\)", b_head):
        return False
    # 连字符结尾是断词强信号（prolif- + erating），豁免短段长度门槛
    if a.endswith("-"):
        return True
    cjk_a = any("\u4e00" <= ch <= "\u9fff" for ch in a[:20])
    cjk_b = any("\u4e00" <= ch <= "\u9fff" for ch in b[:20])
    if cjk_a or cjk_b:
        # 中文无大小写：上段未以句终标点收尾且长度足够（>20 字）即接上
        return len(a) > 20 \
            and not a.endswith(_CJK_TERMINAL_ENDS) and not a.endswith(".")
    if len(a) < 30:
        return False
    if _looks_like_reference_entry(b):
        return False
    if b[0].islower() or b[0] in "([\"'“‘" or b[0].isdigit():
        # 小写/括号/数字开头：只要上一段不是真正句终就接上
        return not _ends_sentence(a)
    # 大写开头（RNA-seq / We / 专有名词）：仅当上一段没有真正句终
    # （_ends_sentence 容忍句尾缩写点：e.g. / et al. / Fig. 不算句终）
    return not _ends_sentence(a)


def _join_cross_page_text(text_a: str, text_b: str) -> str:
    """按换页规则拼接明显连续的两段文本。"""
    a = text_a.rstrip()
    b = text_b.lstrip()
    if a.endswith("-") and len(a) > 1 and b and b[0].isalnum():
        return a[:-1] + b
    return f"{a} {b}".strip()


def _figure_internal_short_text(e: dict, figure_bboxes: list[list[float]]) -> bool:
    """图内短标注文字（坐标轴标签/物种名等）：大部分面积落在图区内且文本不长。

    此类元素既不是正文段落，也不是图注（图注通常在图区之外），
    接缝配对与正文流都应跳过。
    """
    if len((e.get("text") or "").strip()) > 60:
        return False
    return _figure_internal_text(e, figure_bboxes)


# Docling 漏识别成 table 的表格碎片特征：单/双行高度（bbox 高 < 45px）
_TABLE_FRAGMENT_MAX_H = 45.0


def _bbox_xywh(e: dict) -> tuple[float, float, float, float] | None:
    b = e.get("bbox") or []
    if len(b) != 4:
        return None
    try:
        return tuple(float(v) for v in b)
    except (TypeError, ValueError):
        return None


def _horizontal_fragment_pair(e: dict, other: dict) -> bool:
    """两个矮 body 是否构成横向排布碎片：同 y 带且 x 基本不重叠。"""
    r1 = _bbox_xywh(e)
    r2 = _bbox_xywh(other)
    if r1 is None or r2 is None:
        return False
    ex0, ey0, ex1, ey1 = r1
    ox0, oy0, ox1, oy1 = r2
    if ey1 - ey0 >= _TABLE_FRAGMENT_MAX_H or oy1 - oy0 >= _TABLE_FRAGMENT_MAX_H:
        return False
    inter_y = min(ey1, oy1) - max(ey0, oy0)
    if inter_y <= 0.6 * min(ey1 - ey0, oy1 - oy0):
        return False
    inter_x = min(ex1, ox1) - max(ex0, ox0)
    if inter_x > 0.5 * min(ex1 - ex0, ox1 - ox0):
        return False
    return True


def _table_fragment_ids(page_elems: list[dict]) -> set[str]:
    """Docling 漏识别成 table 的表格碎片 id 集合。

    横向碎片：同 y 带且 x 不重叠的矮 body 成对出现（如表格跨列断行）。
    拖尾碎片：与碎片行 y 相邻的矮 body（表格尾部断行，如
    "3. Results nant pathogen in the phylum" 这类本行无横排对的残行）。
    """
    frag: set[str] = set()
    bodies = [
        e for e in page_elems
        if isinstance(e, dict) and e.get("id") and e.get("type") == "body"
        and (e.get("text") or "").strip()
        and _bbox_xywh(e) is not None
    ]
    for e in bodies:
        if any(_horizontal_fragment_pair(e, o) for o in bodies):
            frag.add(str(e["id"]))
    # 传播：与碎片行 y 相邻（重叠或间隙小于相邻行高）的矮 body 也是碎片
    changed = True
    while changed:
        changed = False
        for e in bodies:
            if str(e["id"]) in frag:
                continue
            r = _bbox_xywh(e)
            if r is None or r[3] - r[1] >= _TABLE_FRAGMENT_MAX_H:
                continue
            ex0, ey0, ex1, ey1 = r
            for o in bodies:
                if str(o["id"]) not in frag:
                    continue
                ob = _bbox_xywh(o)
                if ob is None:
                    continue
                oy0, oy1 = ob[1], ob[3]
                inter_y = min(ey1, oy1) - max(ey0, oy0)
                max_h = max(ey1 - ey0, oy1 - oy0)
                if inter_y >= 0.6 * min(ey1 - ey0, oy1 - oy0) \
                        or -inter_y < max_h * 0.9:
                    frag.add(str(e["id"]))
                    changed = True
                    break
    return frag


def _is_table_fragment(e: dict, page_elems: list[dict]) -> bool:
    """单个 body 是否属于表格碎片（供快速判定的单元素查询）。"""
    return str(e.get("id", "")) in _table_fragment_ids(page_elems)


def _is_figure_caption_body(e: dict, elements: list[dict]) -> bool:
    """紧跟「图题样式 subtitle」（Figure N / Table N）之后的 body 段 → 图注正文。

    Docling 常把图题标成 subtitle、图注正文标成 body（如 Annual Reviews 版式）。
    该 body 段不进入正文流（图注由 _bind_captions 绑定到图块），
    跨页接缝配对时也不作为「页首正文」。
    """
    if not isinstance(e, dict):
        return False
    if e.get("type") not in _SEAM_TEXT_TYPES:
        return False
    if not (e.get("text") or "").strip():
        return False
    try:
        idx = elements.index(e)
    except ValueError:
        return False
    for prev in reversed(elements[:idx]):
        if not isinstance(prev, dict):
            continue
        ptext = (prev.get("text") or "").strip()
        if prev.get("type") == "subtitle" and _FIGURE_CAPTION_RE.match(ptext):
            return True
        if prev.get("type") in _SEAM_TEXT_TYPES and (prev.get("text") or "").strip():
            return False  # 前面是正文段落：不是图注
    return False


def find_cross_page_seams(page_data: list[dict]) -> list[dict]:
    """检测相邻页之间疑似被分页断裂的正文段落（接缝候选）。

    只取上一页最后一个正文元素与下一页第一个正文元素做配对；
    配对前过滤会挤占「页末/页首正文」位置的噪音：图内短标注、
    表格碎片、图注正文、页脚许可/版权行、单位块、首页模板区块、
    裸数字引用编号——否则真实续文永远进不了接缝候选。
    判定采用规则启发式（_looks_like_continuation），少量候选
    交给跨页 LLM 合并线程最终确认。
    """
    seams: list[dict] = []
    by_page: dict[int, list[dict]] = {}
    front_block = _front_matter_block_ids(page_data)
    for page in page_data:
        try:
            pn = int(page.get("page", 0))
        except (TypeError, ValueError):
            continue
        elems = []
        figure_bboxes: list[list[float]] = []
        for e in page.get("elements", []):
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if e.get("type") in ("figure", "table") and e.get("bbox"):
                try:
                    figure_bboxes.append([float(v) for v in e["bbox"]])
                except (TypeError, KeyError, ValueError):
                    pass
        for e in page.get("elements", []):
            if isinstance(e, dict) and e.get("id") \
                    and e.get("type") in _SEAM_TEXT_TYPES:
                text = (e.get("text") or "").strip()
                if not text:
                    continue
                if str(e["id"]) in front_block:
                    continue  # 首页模板区块（Highlights/Authors 等）
                if _figure_internal_short_text(e, figure_bboxes):
                    continue  # 图内短标注（坐标轴/物种名等）
                if _is_table_fragment(e, page.get("elements", [])):
                    continue  # Docling 漏识别成 table 的表格碎片
                if _is_figure_caption_body(e, page.get("elements", [])):
                    continue  # 图注正文
                if _is_running_head(text):
                    continue  # 页眉/页脚许可与版权行
                if _is_front_matter_noise(text, pn, e.get("type", "")):
                    continue  # 首页出版社模板碎片
                if _is_affiliation_block(text, pn):
                    continue  # 作者单位列表块
                if _is_bare_number_text(text):
                    continue  # 上标引用编号碎片
                elems.append(e)
        by_page[pn] = _sorted_elements(elems)
    pages = sorted(by_page)
    for i, pn in enumerate(pages[:-1]):
        qn = pages[i + 1]
        if qn - pn > 2:
            continue  # 中间隔了不止一页（无正文元素的页不算），不做判断；
            # 恰好隔一页（如整页图版页）时正文仍可能是连续段落，继续检测
        prev_elems = by_page[pn]
        next_elems = by_page[qn]
        if not prev_elems or not next_elems:
            continue
        a, b = prev_elems[-1], next_elems[0]
        text_a = (a.get("text") or "").strip()
        text_b = (b.get("text") or "").strip()
        if not _looks_like_continuation(text_a, text_b):
            continue
        seams.append({
            "key": f"{a.get('id')}|{b.get('id')}",
            "page_a": pn,
            "page_b": qn,
            "element_id_a": a.get("id"),
            "element_id_b": b.get("id"),
            "text_a": text_a,
            "text_b": text_b,
        })
    return seams


def _nearest_figure_id(caption: dict, page_no: int,
                       figure_map: dict[int, list[dict]],
                       taken: set[str]) -> str | None:
    """图注 → 最近未被占用的 figure/table 元素（含上一页兜底）。"""
    try:
        ab = caption.get("bbox") or ()
        ax = (float(ab[0]) + float(ab[2])) / 2.0
        ay = (float(ab[1]) + float(ab[3])) / 2.0
    except (TypeError, KeyError, IndexError, ValueError):
        return None
    best_id: str | None = None
    best_d: float | None = None
    for pp in (page_no, page_no - 1):
        for e in figure_map.get(pp, []):
            fid = str(e.get("id") or "")
            if not fid or fid in taken:
                continue
            try:
                b = e["bbox"]
                cx = (float(b[0]) + float(b[2])) / 2.0
                cy = (float(b[1]) + float(b[3])) / 2.0
            except (TypeError, KeyError, IndexError, ValueError):
                continue
            d = (cx - ax) ** 2 + (cy - ay) ** 2
            if best_d is None or d < best_d:
                best_d, best_id = d, fid
    return best_id


def _bind_captions(page_data: list[dict]) -> tuple[dict[str, str], set[str]]:
    """figure_caption/table_caption → figure/table 的 bbox 就近配对。

    额外识别 Docling 常见的「图题 subtitle + body 图注」结构：图题
    （Figure N / Table N 样式）后紧跟的正文段从版面关系看是图注内容，
    一并并入图块；否则图题会进目录、图注会被当普通正文渲染（如
    Annual Reviews 版式中 Figure 1 整页图 + 图注版式）。

    Returns:
        (binding, used_caption_ids)
        binding: {figure_id: caption_text}
        used_caption_ids: 已被并入图表块的图注元素 id（不再单独展示）
    """
    figure_map: dict[int, list[dict]] = {}
    captions: list[dict] = []
    page_of: dict[str, int] = {}
    for page in page_data:
        pn = int(page.get("page", 0))
        for e in page.get("elements", []):
            if not isinstance(e, dict) or not e.get("id"):
                continue
            etype = e.get("type")
            if etype in ("figure", "table"):
                figure_map.setdefault(pn, []).append(e)
            elif etype in ("figure_caption", "table_caption"):
                if _strip_watermarks(e.get("text") or ""):
                    captions.append(e)
                    page_of[str(e["id"])] = pn
    binding: dict[str, str] = {}
    used: set[str] = set()
    taken: set[str] = set()
    for cap in captions:
        cid = str(cap["id"])
        fid = _nearest_figure_id(cap, page_of.get(cid, 0), figure_map, taken)
        if not fid:
            continue
        taken.add(fid)
        used.add(cid)
        cap_txt = _strip_watermarks(cap.get("text") or "")
        old = binding.get(fid)
        if old:
            cap_txt = old + " " + cap_txt
        binding[fid] = cap_txt

    # 图题 subtitle（Figure N / Table N）+ 紧跟的 body 图注
    for page in page_data:
        pn = int(page.get("page", 0))
        elems = page.get("elements", [])
        for i, e in enumerate(elems):
            if not isinstance(e, dict) or not e.get("id"):
                continue
            if e.get("type") != "subtitle":
                continue
            head_txt = _strip_watermarks(e.get("text") or "")
            if not _FIGURE_CAPTION_RE.match(head_txt):
                continue
            # 向后找第一个非空文本元素：正文段 → 视为图注正文；其它类型则无
            body = None
            for nxt in elems[i + 1:]:
                if not isinstance(nxt, dict):
                    continue
                nt = (nxt.get("text") or "").strip()
                if not nt:
                    continue
                if nxt.get("type") in _SEAM_TEXT_TYPES:
                    body = nxt
                break
            if body is None:
                continue
            fid = _nearest_figure_id(body, pn, figure_map, taken)
            if not fid:
                continue
            taken.add(fid)
            used.add(str(e.get("id")))
            used.add(str(body.get("id")))
            cap_txt = f"{head_txt} {_strip_watermarks(body.get('text') or '')}".strip()
            old = binding.get(fid)
            if old:
                cap_txt = old + " " + cap_txt
            binding[fid] = cap_txt
    return binding, used


def _index_merged_seams(merged_seams: dict) -> dict[str, dict]:
    """按接缝起始元素索引合并缓存。

    缓存键使用 ``element_a|element_b``，而组装循环逐个访问当前元素 ID。
    统一在入口建立索引，兼容旧版按单元素 ID 保存的缓存格式。
    """
    indexed: dict[str, dict] = {}
    for raw_key, value in merged_seams.items():
        if not isinstance(value, dict):
            continue
        key = str(raw_key)
        source_id = key.split("|", 1)[0].strip() if "|" in key else key.strip()
        if source_id:
            indexed[source_id] = value
    return indexed


def build_document_fast(page_data: list[dict],
                        merged_seams: dict | None = None) -> StructuredDocument:
    """规则组装 —— 不用 LLM，直接按版面规则构建 StructuredDocument。

    规则要点：
    - 页内按 bbox 阅读顺序，页间按页码顺序
    - subtitle 的 heading_level 来自 Docling 的 level 字段
    - 图注按 bbox 就近绑定到 figure/table 块，绑定的图注不再单独展示
    - 跨页断裂段落用 merged_seams 缓存合并（由 SeamMergeWorker 填充）
    - collapsed 元素（参考文献/致谢/附录等）保留展示，由 UI 折叠
    """
    if not isinstance(merged_seams, dict):
        merged_seams = {}
    merged_by_source = _index_merged_seams(merged_seams)
    # 已被并入上一页合并正文的续文元素 id：仅跳过它本身，不得跳过
    # 两者之间的图/图题/图注等元素（否则跨页合并会吞掉整页版式）
    consumed_seam_targets: set[str] = set()
    binding, used_captions = _bind_captions(page_data)
    figure_plate_pages = _figure_plate_pages(page_data)
    # 首页出版社模板区块（Graphical Abstract/Highlights/Authors/In Brief 等）
    front_block_ids = _front_matter_block_ids(page_data)
    page_elements_by_no = {
        int(page.get("page", 0)): [
            e for e in page.get("elements", []) if isinstance(e, dict)
        ]
        for page in page_data
    }
    plate_primary_ids = {
        int(page.get("page", 0)): next(
            (
                str(e.get("id"))
                for e in page.get("elements", [])
                if e.get("type") in ("figure", "table") and e.get("id")
            ),
            "",
        )
        for page in page_data
        if int(page.get("page", 0)) in figure_plate_pages
    }

    by_id: dict[str, dict] = {}
    page_by_id: dict[str, int] = {}
    # 每页 figure/table bbox，用于剔除图内文字
    figure_bboxes_by_page: dict[int, list[list[float]]] = {}
    for page in page_data:
        pn = int(page.get("page", 0))
        for e in page.get("elements", []):
            if isinstance(e, dict) and e.get("id"):
                by_id[str(e["id"])] = e
                page_by_id[str(e["id"])] = pn
                if e.get("type") in ("figure", "table") and e.get("bbox"):
                    figure_bboxes_by_page.setdefault(pn, []).append(
                        [float(v) for v in e["bbox"]]
                    )

    # 展平阅读顺序：相邻正文段若被 Docling 切碎（如上标引用把句子打断），
    # 顺序扫描时按续写判定合并，避免上游一切块都被独立渲染为断卡片。
    raw_ordered: list[dict] = []
    for page in sorted(page_data, key=lambda p: int(p.get("page", 0))):
        for e in _sorted_elements(page.get("elements", [])):
            raw_ordered.append(e)
    ordered: list[dict] = []
    for e in raw_ordered:
        if (ordered and e.get("type") in _SEAM_TEXT_TYPES
                and ordered[-1].get("type") in _SEAM_TEXT_TYPES
                and ordered[-1].get("page") == e.get("page")):
            ta = (ordered[-1].get("text") or "").strip()
            tb = (e.get("text") or "").strip()
            # 首页 front matter 元素不参与同页续写合并：作者行/单位块/单位
            # 碎片/关键词行一旦与前后文粘合，会超过单位块长度上限或变成
            # 混排大卡（如 Frontiers 作者+单位+摘要、JSE 单位块+单位块）。
            # 前一个元素与当前元素任一命中即不合并（单位块可能紧跟摘要）。
            fm_page = int(ordered[-1].get("page") or 0)
            fm_prev = (_is_author_line(ta, fm_page)
                       or _is_affiliation_block(ta, fm_page)
                       or _is_affiliation_fragment(ta, fm_page)
                       or _is_keywords_line(ta, fm_page)
                       or _is_running_head(ta)
                       or _is_front_matter_noise(ta, fm_page, "body"))
            fm_cur = (_is_author_line(tb, fm_page)
                      or _is_affiliation_block(tb, fm_page)
                      or _is_affiliation_fragment(tb, fm_page)
                      or _is_keywords_line(tb, fm_page)
                      or _is_running_head(tb)
                      or _is_front_matter_noise(tb, fm_page, "body"))
            if ta and tb and not fm_prev and not fm_cur \
                    and _looks_like_continuation(ta, tb):
                merged_text = _join_cross_page_text(ta, tb)
                new_e = dict(ordered[-1])
                new_e["text"] = merged_text
                ordered[-1] = new_e
                continue
        ordered.append(e)

    doc = StructuredDocument()
    display: list[StructuredElement] = []
    toc: list[dict] = []
    current_section = ""
    current_level = 0
    # 首页 front matter 状态机（仅第 1 页，进入正文后关闭）：
    # 作者行/单位/摘要/关键词在 Docling 里常被标成 body，这里按版面
    # 位置与文本特征归类为专门的卡片类型，而不是混入正文流。
    page1_h = 0.0
    for page in page_data:
        if int(page.get("page", 0)) == 1:
            for pe in page.get("elements", []):
                if isinstance(pe, dict) and pe.get("bbox"):
                    try:
                        page1_h = max(page1_h, float(pe["bbox"][3]))
                    except (TypeError, ValueError):
                        pass
    front_active = True
    fm_author_seen = False  # 作者行已出现（其后长 body 更可能是摘要）
    fm_title_seen = False   # 标题已捕获（只取页首第一个）
    in_abstract_body = False  # 摘要小节（Abstract/Summary/Significance）后：body → abstract_body
    i = 0
    n = len(ordered)
    while i < n:
        e = ordered[i]
        eid = str(e.get("id", ""))
        if eid in consumed_seam_targets:
            i += 1
            continue  # 已并入上一页合并正文（只消费该元素本身）
        etype = e.get("type", "unknown")
        text = _strip_watermarks(e.get("text", "") or "")
        page_no = page_by_id.get(eid, int(e.get("page", 0) or 0))

        # ---- 首页 front matter 状态机 ----
        if front_active and page_no == 1 and etype in ("body", "subtitle"):
            if eid in front_block_ids:
                i += 1
                continue  # 首页模板区块（编辑信息碎片等）跳过，不关闭状态机
            if etype == "subtitle":
                if not fm_title_seen and _is_article_title(text, e, page1_h):
                    # Docling 常把标题标成 subtitle：捕获为标题卡（不关闭状态机，
                    # 标题之后仍是作者/单位/摘要的 front matter 区域）
                    fm_title_seen = True
                    if not doc.title:
                        doc.title = text
                    display.append(StructuredElement(
                        element_type="title",
                        text=text,
                        page=page_no,
                        heading_level=0,
                        section_name="",
                        display_priority="high",
                        element_id=eid,
                        bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                    ))
                    i += 1
                    continue
                if _ABSTRACT_HEADING_RE.match(text):
                    # 摘要小节（Abstract/Summary/Significance）：渲染摘要标题卡，
                    # 不关闭状态机（其后 body 由摘要判定接管）
                    in_abstract_body = True
                    display.append(StructuredElement(
                        element_type="abstract_heading",
                        text=text,
                        page=page_no,
                        heading_level=0,
                        section_name="",
                        display_priority="high",
                        element_id=eid,
                        bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                    ))
                    i += 1
                    continue
                if _FRONT_MATTER_SECTION_RE.match(text):
                    i += 1
                    continue  # 模板小节名（Highlights/Reviewed by 等）整块剔除
                if _is_front_matter_noise(text, 1, "subtitle"):
                    i += 1
                    continue  # 文章类型标签（Review/Article 等）：跳过不关闭状态机
                # 短行 subtitle（期刊名/装饰性标签如 ``nature methods``、
                # ``BMC Bioinformatics``）：跳过不关闭状态机，也不当标题；
                # 已知章节名（Methods 等）除外——那是真实小节，走正文流
                if len(text) < 20 and not _SECTION_TITLE_RE.match(text):
                    i += 1
                    continue
                # 期刊名（即使超过 20 字符，如 ``Nature Communications``）：
                # 跳过不关闭状态机，也不当标题
                if _JOURNAL_NAME_RE.match(text):
                    i += 1
                    continue
                front_active = False  # 真正的章节标题 → 正文开始
            else:  # body
                # 版头噪声（通讯行/日期行/投稿声明/运行页眉）跳过，不关闭状态机
                if _is_running_head(text) or _is_front_matter_noise(text, 1, "body"):
                    i += 1
                    continue
                if _is_keywords_line(text, 1):
                    display.append(StructuredElement(
                        element_type="keywords",
                        text=text,
                        page=page_no,
                        heading_level=0,
                        section_name="",
                        display_priority="normal",
                        element_id=eid,
                        bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                    ))
                    i += 1
                    continue
                if _is_author_line(text, 1):
                    if not doc.authors:
                        doc.authors = text
                    display.append(StructuredElement(
                        element_type="authors",
                        text=text,
                        page=page_no,
                        heading_level=0,
                        section_name="",
                        display_priority="normal",
                        element_id=eid,
                        bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                    ))
                    fm_author_seen = True
                    i += 1
                    continue
                if _is_affiliation_block(text, 1):
                    if not doc.metadata_pool:
                        doc.metadata_pool.append(StructuredElement(
                            element_type="affiliations",
                            text=text,
                            page=page_no,
                            heading_level=0,
                            section_name="",
                            display_priority="collapsed",
                            element_id=eid,
                            bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                        ))
                    display.append(StructuredElement(
                        element_type="affiliations",
                        text=text,
                        page=page_no,
                        heading_level=0,
                        section_name="",
                        display_priority="normal",
                        element_id=eid,
                        bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                    ))
                    i += 1
                    continue
                if _is_affiliation_fragment(text, 1):
                    i += 1
                    continue  # 单位碎片（编号/机构词开头的短行）
                # 摘要：独立 Abstract 小节后的正文，或 ``Abstract ...`` 词头
                # 长段（MDPI/JSE 版式把摘要并入同一元素）
                if len(text) > 120 and (
                        (fm_author_seen and _looks_like_abstract(text, ordered, i, 1))
                        or re.match(r"^(?:abstract|summary)\b", text,
                                    re.IGNORECASE)):
                    display.append(StructuredElement(
                        element_type="abstract_body",
                        text=text,
                        page=page_no,
                        heading_level=0,
                        section_name="",
                        display_priority="normal",
                        element_id=eid,
                        bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                    ))
                    i += 1
                    continue
                # 短碎片（名字行/日期行/无标点行）：跳过，继续 front matter 扫描
                if len(text) < 40 and not re.search(r"[.!?。！？]", text):
                    i += 1
                    continue
                # 其余首页 body：视为正文开始（编辑信息碎片由
                # _is_front_matter_noise / front_block_ids 另行剔除）
                front_active = False

        # 跨页接缝：命中缓存 → 合并为一个正文元素
        seam = merged_by_source.get(eid)
        if seam and etype in _SEAM_TEXT_TYPES:
            next_id = str(seam.get("with_id", ""))
            merged_text = _strip_watermarks(seam.get("merged_text", "") or "")
            if next_id and merged_text:
                elem = StructuredElement(
                    element_type="body",
                    text=merged_text,
                    page=page_no,
                    heading_level=0,
                    section_name=current_section,
                    display_priority="normal",
                    element_id=eid,
                    bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                )
                display.append(elem)
                consumed_seam_targets.add(next_id)
                i += 1
                continue

        i += 1
        if etype in _FAST_SKIP_TYPES:
            continue
        if eid in used_captions:
            continue  # 已并入图表块
        if eid in front_block_ids:
            continue  # 首页模板区块（Highlights / Authors 等整块剔除）

        is_media = etype in ("figure", "table", "figure_caption", "table_caption")
        if _is_front_matter_noise(text, page_no, etype):
            continue
        if _is_affiliation_block(text, page_no):
            # 作者单位块不进正文流；若是首页首张单位卡则记为作者单位元数据
            if (etype in ("body", "metadata", "affiliations", "authors")
                    and page_no <= 2 and not doc.metadata_pool
                    and _NUMBERED_AFFIL_START_RE.match(text)):
                doc.metadata_pool.append(StructuredElement(
                    element_type="affiliations",
                    text=text,
                    page=page_no,
                    heading_level=0,
                    section_name="",
                    display_priority="collapsed",
                    element_id=eid,
                    bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                ))
            continue
        if etype == "body" and _is_bare_number_text(text):
            continue  # 上标引用编号碎片（不污染正文流与接缝配对池）
        # 首页作者行兜底：Docling 双栏/封面版式可能把作者行排在版头小节
        # 之后（状态机已关闭）或封面页之后的次页（Cell 版式 p2 作者行），
        # 此时仍归类为作者卡而非正文；doc.authors 已捕获则丢弃（去重）
        if page_no <= 2 and etype == "body" and _is_author_line(text, page_no):
            if not doc.authors:
                doc.authors = text
                display.append(StructuredElement(
                    element_type="authors",
                    text=text,
                    page=page_no,
                    heading_level=0,
                    section_name="",
                    display_priority="normal",
                    element_id=eid,
                    bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                ))
            continue
        if etype == "figure" and _is_decorative_figure(
            e, page_elements_by_no.get(page_no, []), page_no,
        ):
            continue
        if (
            page_no in figure_plate_pages
            and etype in ("figure", "table")
            and eid != plate_primary_ids.get(page_no)
        ):
            continue  # 多面板图版改为一张完整页面图
        if not text and not is_media:
            continue
        if page_no in figure_plate_pages and not is_media:
            if not _looks_like_figure_caption(text):
                continue

        # 运行页眉噪声（ARTICLE IN PRESS 等）与图内标签文字不进入正文流
        if _is_running_head(text):
            continue
        if etype == "body" and _is_table_fragment(
            e, page_elements_by_no.get(page_no, []),
        ):
            continue
        if etype not in ("figure", "table"):
            fb = figure_bboxes_by_page.get(page_no, [])
            if fb and _figure_internal_text(e, fb):
                if len(text) <= 60 or not text:
                    continue

        priority = _FAST_PRIORITY.get(etype, "normal")
        if etype == "subtitle":
            # 模板小节名（Authors and Affiliations / Reviewed by 等，常出现在
            # 次页版头）：不进正文流、不进目录
            if _FRONT_MATTER_SECTION_RE.match(text):
                continue
            # 与已捕获标题完全相同的重复标题（封面页+正文页重复排印）去重
            if doc.title and text.strip().lower() == doc.title.lower():
                continue
            current_section = text
            try:
                current_level = int(e.get("level", 1) or 1)
            except (TypeError, ValueError):
                current_level = 1
            # 摘要小节名（Abstract / Summary / Significance 等）映射为摘要
            # 标题卡，其后的正文段落映射为摘要正文卡
            if _ABSTRACT_HEADING_RE.match(text):
                in_abstract_body = True
                elem = StructuredElement(
                    element_type="abstract_heading",
                    text=text,
                    page=page_no,
                    heading_level=current_level,
                    section_name="",
                    display_priority=priority,
                    element_id=eid,
                    bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
                )
                display.append(elem)
                continue
            in_abstract_body = False
            elem = StructuredElement(
                element_type="subtitle",
                text=text,
                page=page_no,
                heading_level=current_level,
                section_name="",
                display_priority=priority,
                element_id=eid,
                bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
            )
            display.append(elem)
            toc.append({
                "level": current_level,
                "title": text,
                "element_index": len(display) - 1,
            })
            continue

        if etype == "title":
            if not doc.title:
                doc.title = text
            # 保留 title 卡片（旧缓存/兜底路径下渲染标题卡）
            display.append(StructuredElement(
                element_type="title",
                text=text,
                page=page_no,
                heading_level=0,
                section_name="",
                display_priority="high",
                element_id=eid,
                bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
            ))
            continue
        elif etype == "authors":
            if not doc.authors:
                doc.authors = text
            display.append(StructuredElement(
                element_type="authors",
                text=text,
                page=page_no,
                heading_level=0,
                section_name="",
                display_priority="normal",
                element_id=eid,
                bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
            ))
            continue
        elif etype == "abstract_heading":
            current_section = text
        elif etype == "body":
            # 无章节名的正文沿用当前小节
            pass

        image_caption = ""
        if etype in ("figure", "table"):
            image_caption = binding.get(eid, "")
            if not image_caption:
                image_caption = _strip_watermarks(e.get("caption", "") or "")
        if etype in ("figure", "table") and not text and not image_caption:
            # 空图 + 无图注：仍保留（截图由 image_path 回填后展示）
            pass

        elem = StructuredElement(
            element_type="abstract_body" if (
                in_abstract_body and etype == "body"
            ) else etype,
            text=text,
            page=page_no,
            heading_level=0,
            section_name=current_section if etype in ("body", "abstract_body") else "",
            display_priority=priority,
            element_id=eid,
            bbox=tuple(float(v) for v in (e.get("bbox") or [0, 0, 0, 0])),
            image_path=str(e.get("image_path") or ""),
            image_caption=image_caption,
        )
        display.append(elem)

    if not doc.title:
        doc.title = _guess_title_fallback(page_data)

    doc.display_elements = display
    doc.toc = toc
    for elem in display:
        if elem.element_type == "figure":
            doc.figures.append(elem)
        elif elem.element_type == "table":
            doc.tables.append(elem)
        elif elem.element_type == "reference":
            doc.references.append(elem)
    # 不再用 display 覆盖 metadata_pool：单位块在组装时已直接入池
    # （见 _is_affiliation_block 分支），这里保留组装期间追加的元信息
    doc.raw_page_count = len(page_data)
    return doc


def rebuild_document_fast(pdf_path: str) -> "StructuredDocument | None":
    """从磁盘页缓存重建结构化文档（旧版整合结果迁移入口）。

    旧版 state 的 structured_document 由全量 LLM 生成，打开时改用
    规则组装重建（引用 merged_seams 缓存），保证图片与结构完整。
    """
    from ..utils.config import get_page_cache_dir, load_doc_state

    cache_dir = get_page_cache_dir(pdf_path)
    page_data: list[dict] = []
    for f in sorted(glob.glob(os.path.join(str(cache_dir), "page_*.json"))):
        try:
            with open(f, "r", encoding="utf-8") as fh:
                page_data.append(json.load(fh))
        except (json.JSONDecodeError, OSError):
            continue
    if not page_data:
        return None
    ensure_figure_page_snapshots(pdf_path, str(cache_dir), page_data)
    state = load_doc_state(pdf_path)
    merged = state.get("merged_seams") or {}
    if not isinstance(merged, dict):
        merged = {}
    prelim = state.get("merged_seams_prelim") or {}
    if isinstance(prelim, dict):
        merged = {**prelim, **merged}
    doc = build_document_fast(page_data, merged)
    if not doc.display_elements:
        return None
    backfill_image_paths(doc, str(cache_dir))
    return doc


def backfill_image_paths(doc: StructuredDocument, cache_dir: str) -> None:
    """按 element_id 从页面缓存回填图表截图路径（确定性，不依赖 LLM 透传）。

    裁剪产生的 PNG 文件名格式为 page_{page:03d}_{element_id}.png。
    """
    if not cache_dir or not doc:
        return
    targets: list[StructuredElement] = []
    for e in doc.display_elements:
        if e.element_type in ("figure", "table"):
            targets.append(e)
    for e in (doc.figures or []) + (doc.tables or []):
        if e.element_type in ("figure", "table"):
            targets.append(e)
    seen: set[str] = set()
    for elem in targets:
        if not elem.element_id or elem.element_id in seen:
            continue
        seen.add(elem.element_id)
        full_snapshot = os.path.join(cache_dir, f"page_{elem.page:03d}_full.png")
        if elem.element_type == "figure" and os.path.exists(full_snapshot):
            elem.image_path = full_snapshot
            continue
        filepath = os.path.join(cache_dir, f"page_{elem.page:03d}.json")
        if not os.path.exists(filepath):
            continue
        try:
            with open(filepath, "r", encoding="utf-8") as fh:
                cache = json.load(fh)
        except (json.JSONDecodeError, OSError):
            continue
        for e in cache.get("elements", []):
            if not isinstance(e, dict):
                continue
            if e.get("id") == elem.element_id:
                img = e.get("image_path", "")
                if img:
                    elem.image_path = img
                if not elem.image_caption:
                    elem.image_caption = e.get("caption", "")
                break


SEAM_MERGE_PROMPT = """你是一位学术论文编辑。以下是同一篇论文相邻两页之间疑似被分页断裂的段落对（上一页末尾 + 下一页开头）。

请判断每一对是否属于**同一个自然段落**被分页截断：
- 属于同一段：输出合并后的完整段落文本 merged_text（去掉两段之间因换页产生的多余空格/连字符）。
- 不属于同一段（是独立的两个段落/小节标题/图注等）：输出 "merge": false。

## 输入 JSON
{"seams": [{"key": "p3_e5|p4_e1", "text_a": "...", "text_b": "..."}]}

## 输出 JSON（只输出 JSON，不加解释）
{"results": [{"key": "p3_e5|p4_e1", "merge": true, "merged_text": "..."}]}
"""


class SeamMergeWorker(QThread):
    """跨页接缝合并线程 —— 只处理少量候选接缝，小任务、快返回。"""

    finished_signal = Signal(object)  # {seam_key: {"with_id": str, "merged_text": str}}
    error = Signal(str)
    done = Signal()  # 线程必然退出信号（run 的 finally 触发）

    def __init__(self, client: "LLMClient", seams: list[dict], parent=None):
        super().__init__(parent)
        self._client = client
        self._seams = seams

    def run(self) -> None:
        try:
            payload = [
                {"key": s["key"], "text_a": s["text_a"], "text_b": s["text_b"]}
                for s in self._seams
            ]
            messages = [
                {"role": "system", "content": SEAM_MERGE_PROMPT},
                {"role": "user", "content": json.dumps(
                    {"seams": payload}, ensure_ascii=False,
                )},
            ]
            response = self._client.chat_sync(messages, timeout=120.0, json_mode=True)
            if self.isInterruptionRequested():
                return
            plan = _validate_integration_result(response)
            if "error" in plan:
                self.error.emit(plan["error"])
                return
            results: dict[str, dict] = {}
            by_key = {s["key"]: s for s in self._seams}
            for item in (plan.get("results") or []):
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key", ""))
                if key not in by_key:
                    continue
                merged = str(item.get("merged_text", "") or "").strip()
                if item.get("merge") and merged:
                    results[key] = {
                        "with_id": by_key[key]["element_id_b"],
                        "merged_text": merged,
                    }
            self.finished_signal.emit(results)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))
        finally:
            self.done.emit()


# ============================================================
# 通用工具
# ============================================================

def crop_meaningful_images(pdf_path: str, cache_dir: str,
                           page_num: int, elements: list[dict]) -> None:
    """裁剪 figure/table 元素区域为 PNG，写回 image_path/image_caption。

    bbox 坐标为页面渲染像素坐标（150dpi，左上原点），裁剪时映射回 PDF 点坐标。
    """
    import fitz

    meaningful = [
        e for e in elements
        if e.get("type") in ("figure", "table") and e.get("is_meaningful", True)
    ]
    if not meaningful:
        return

    try:
        doc = fitz.open(pdf_path)
        if page_num > len(doc):
            doc.close()
            return
        page = doc[page_num - 1]
        page_rect = page.rect  # 单位：点
        scale = 72.0 / 150.0  # 像素 → 点

        for elem in meaningful:
            bbox = elem.get("bbox", [0, 0, 0, 0])
            if len(bbox) != 4:
                continue
            x0, y0, x1, y1 = float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])
            if x1 <= x0 or y1 <= y0:
                continue
            # 像素→PDF 点坐标
            px0, py0, px1, py1 = x0 * scale, y0 * scale, x1 * scale, y1 * scale
            # 越界裁剪
            px0 = max(0.0, px0); py0 = max(0.0, py0)
            px1 = min(page_rect.width, px1); py1 = min(page_rect.height, py1)
            if px1 <= px0 or py1 <= py0:
                continue

            elem_id = elem.get("id", f"p{page_num}_e_img")
            filename = f"page_{page_num:03d}_{elem_id}.png"
            output_path = os.path.join(cache_dir, filename)

            clip = fitz.Rect(px0 - 2, py0 - 2, px1 + 2, py1 + 2)
            mat = fitz.Matrix(200 / 72, 200 / 72)
            pix = page.get_pixmap(matrix=mat, clip=clip)
            if pix.width < 8 or pix.height < 8:
                # 裁剪区域过小（越界/坐标异常）→ 跳过并留痕，避免显示空白图
                print(f"[Docling] 第 {page_num} 页元素 {elem_id} 裁剪区域过小 "
                      f"({pix.width}x{pix.height})，跳过截图")
                continue
            pix.save(output_path)

            elem["image_path"] = filename
            elem["image_caption"] = elem.get("caption", "")

        doc.close()
    except Exception:
        pass  # 裁剪失败不阻塞流程


def render_figure_page_snapshot(pdf_path: str, cache_dir: str,
                                page_num: int, dpi: int = 150) -> str:
    """将图版页完整渲染为一张 PNG，保留多面板图的原始排版。"""
    import fitz

    filename = f"page_{page_num:03d}_full.png"
    output_path = os.path.join(cache_dir, filename)
    try:
        doc = fitz.open(pdf_path)
        if page_num < 1 or page_num > len(doc):
            doc.close()
            return ""
        page = doc[page_num - 1]
        pix = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
        os.makedirs(cache_dir, exist_ok=True)
        pix.save(output_path)
        doc.close()
        return filename
    except Exception:
        return ""


def ensure_figure_page_snapshots(pdf_path: str, cache_dir: str,
                                 page_data: list[dict]) -> None:
    """为图版页生成完整页面图，并挂到该页第一个媒体元素。"""
    plate_pages = _figure_plate_pages(page_data)
    for page in page_data:
        page_no = int(page.get("page", 0) or 0)
        if page_no not in plate_pages:
            continue
        media = [
            e for e in page.get("elements", [])
            if e.get("type") in ("figure", "table")
        ]
        if not media:
            continue
        filename = f"page_{page_no:03d}_full.png"
        output_path = os.path.join(cache_dir, filename)
        if not os.path.exists(output_path):
            filename = render_figure_page_snapshot(pdf_path, cache_dir, page_no)
        if filename:
            media[0]["image_path"] = filename
            media[0]["image_caption"] = media[0].get("caption", "")


class DoclingParseWorker(QThread):
    """Docling 本地解析整本 PDF → 逐页缓存（Stage 1 的本地引擎）。"""

    page_done = Signal(int)            # page_num（复用 PDFProcessor._on_stage1_page_done）
    progress = Signal(int, int)        # current, total
    status = Signal(str)               # 初始化/解析阶段提示
    completed = Signal()               # 解析成功且全部页面落盘后才触发
    error = Signal(str)
    done = Signal()                    # 线程必然退出信号（run 的 finally 触发）

    def __init__(self, pdf_path: str, cache_dir: str, manifest: "PageManifest",
                 parent=None):
        super().__init__(parent)
        self._pdf_path = pdf_path
        self._cache_dir = cache_dir
        self._manifest = manifest

    def run(self) -> None:
        try:
            self._run()
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"Stage 1 缓存写入失败：{e}")
        finally:
            self.done.emit()  # 无论成功/失败/中断，线程退出时必然触发

    def _run(self) -> None:
        try:
            self.status.emit("正在加载本地版式模型，首次使用可能需要较长时间...")
            from .docling_parser import parse_pdf, get_converter
            if self.isInterruptionRequested():
                return
            self.status.emit("正在用本地版式解析器读取 PDF...")
            # 关键：docling/torch 的 converter 不能在 QThread 内首次创建
            # （会与 Qt 事件循环死锁）。先在普通 Python 线程里创建并缓存，
            # 之后 parse_pdf 只会命中已就绪的转换器。
            _warm_up_converter(get_converter)
            if self.isInterruptionRequested():
                return
            pages = parse_pdf(self._pdf_path)
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"Docling 解析失败：{e}")
            return

        self._manifest.total_pages = len(pages)
        figure_plate_pages = _figure_plate_pages(pages)
        ensure_figure_page_snapshots(self._pdf_path, self._cache_dir, pages)
        for i, page in enumerate(pages):
            if self.isInterruptionRequested():
                return
            page_num = int(page.get("page", i + 1))
            elements = page.get("elements", [])

            if page_num in figure_plate_pages:
                # 图版页保留原始多面板排版，不把每个坐标轴/小位图拆成几十张卡片。
                full_path = os.path.join(
                    self._cache_dir, f"page_{page_num:03d}_full.png",
                )
                if not os.path.exists(full_path):
                    crop_meaningful_images(
                        self._pdf_path, self._cache_dir, page_num, elements,
                    )
            else:
                # 正文页仍按 figure/table bbox 裁剪，支持单图问答。
                crop_meaningful_images(self._pdf_path, self._cache_dir, page_num, elements)

            result = {
                "page": page_num,
                "status": "done",
                "page_role": page.get("page_role", "content_page"),
                "elements": elements,
                "raw_text": "",
                "error_message": "",
                "processed_at": time.time(),
                "parser": DOCLING_PARSER_VERSION,
            }
            self._save_page_cache(page_num, result)
            self._manifest.pages[page_num] = "done"
            self._manifest.updated_at = time.time()
            self._save_manifest()

            self.page_done.emit(page_num)
            self.progress.emit(i + 1, len(pages))

        try:
            png_count = len([
                f for f in os.listdir(self._cache_dir)
                if f.lower().endswith(".png")
            ])
            print(f"[Docling] Stage 1 完成：{len(pages)} 页，"
                  f"共生成 {png_count} 张图表截图（含位图兜底）")
        except OSError:
            pass
        self.completed.emit()

    def _save_page_cache(self, page_num: int, data: dict) -> None:
        os.makedirs(self._cache_dir, exist_ok=True)
        filepath = os.path.join(self._cache_dir, f"page_{page_num:03d}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_manifest(self) -> None:
        os.makedirs(self._cache_dir, exist_ok=True)
        filepath = os.path.join(self._cache_dir, "_manifest.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self._manifest.to_dict(), f, ensure_ascii=False, indent=2)


# ============================================================
# PDFProcessor —— 协调者：管理两阶段流程
# ============================================================

class PDFProcessor(QObject):
    """PDF 处理协调器 —— 管理 Stage 1 逐页解析 + Stage 2 规则组装。

    Stage 2 不再是"整篇发给 LLM 精整"：先用本地规则秒级组装
    （build_document_fast），随后在后台对少量跨页接缝候选做 LLM
    合并确认（SeamMergeWorker），结果缓存到 state（merged_seams）。

    使用方式：
        processor = PDFProcessor(pdf_path, llm_client)
        processor.stage1_progress.connect(on_progress)
        processor.start_stage1()    # 自动后台开始
        # ... 用户点击论文时 ...
        processor.start_stage2()    # 读缓存 → 规则组装 → 渲染
    """

    # 信号
    stage1_progress = Signal(str, int, int)  # pdf_path, current, total
    stage1_status = Signal(str, str)          # pdf_path, status message
    stage1_page_done = Signal(str, int)       # pdf_path, page_num
    stage1_complete = Signal(str)             # pdf_path
    stage1_error = Signal(str, int, str)      # pdf_path, page_num, error
    stage2_finished = Signal(str, object)     # pdf_path, StructuredDocument
    stage2_error = Signal(str, str)           # pdf_path, error
    stage2_merged = Signal(str, object)       # pdf_path, StructuredDocument（接缝合并后刷新）

    _STAGE1_MAX_RETRIES = 2  # done_count==0 时回退 Stage 1 的最大重试次数

    def __init__(self, pdf_path: str, llm_client: "LLMClient | None") -> None:
        super().__init__()  # QObject init
        self._pdf_path = pdf_path
        self._client = llm_client
        self._docling_worker: DoclingParseWorker | None = None
        self._seam_worker: SeamMergeWorker | None = None
        self._manifest: PageManifest | None = None
        self._cache_dir: str = ""
        self._integrated_doc: StructuredDocument | None = None
        self._seams_mode = ""  # 接缝合并级别："" 未处理 / "prelim" 规则初步 / "final" 定稿
        self._seam_candidates: list[dict] = []
        self._stage1_retries = 0  # done_count==0 时回退 Stage 1 的重试次数
        self._generation = 0  # cancel/重跑代际：迟到的后台结果据此丢弃

        self._init_cache()

    def set_llm_client(self, client: "LLMClient | None") -> None:
        """更新解析接口（配置变更后同步到后台处理器）。"""
        self._client = client

    def _init_cache(self) -> None:
        """初始化缓存目录和 manifest。"""
        from ..utils.config import _doc_id, get_page_cache_dir

        pdf_md5 = _doc_id(self._pdf_path)
        self._cache_dir = str(get_page_cache_dir(self._pdf_path))

        # 加载或创建 manifest
        manifest_path = os.path.join(self._cache_dir, "_manifest.json")
        if os.path.exists(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    saved = json.load(f)
                self._manifest = PageManifest.from_dict(saved)

                # 检查 PDF 是否被修改（mtime 变化 → 缓存失效）
                current_mtime = os.path.getmtime(self._pdf_path)
                parser_now = self._current_parser()
                if abs(self._manifest.pdf_mtime - current_mtime) > 1.0 \
                        or self._manifest.parser != parser_now:
                    # PDF 已修改或解析引擎切换 → 重置 manifest
                    self._manifest = self._create_fresh_manifest()
            except (json.JSONDecodeError, OSError):
                self._manifest = self._create_fresh_manifest()
        else:
            self._manifest = self._create_fresh_manifest()

    @staticmethod
    def _current_parser() -> str:
        return DOCLING_PARSER_VERSION

    def _create_fresh_manifest(self) -> PageManifest:
        """创建全新的 manifest（通过 PyMuPDF 获取总页数）。"""
        import fitz
        from ..utils.config import _doc_id

        try:
            doc = fitz.open(self._pdf_path)
            total = len(doc)
            doc.close()
        except Exception:
            total = 0

        return PageManifest(
            pdf_path=self._pdf_path,
            pdf_md5=_doc_id(self._pdf_path),
            total_pages=total,
            pdf_mtime=os.path.getmtime(self._pdf_path),
            pages={p: "pending" for p in range(1, total + 1)},
            created_at=time.time(),
            updated_at=time.time(),
            parser=DOCLING_PARSER_VERSION,
        )

    # ---- 公共 API ----

    @property
    def manifest(self) -> PageManifest | None:
        return self._manifest

    @property
    def cached_document(self) -> StructuredDocument | None:
        return self._integrated_doc

    @property
    def is_stage1_complete(self) -> bool:
        return self._manifest is not None and self._manifest.is_complete

    @property
    def is_stage1_running(self) -> bool:
        return self._docling_worker is not None and self._docling_worker.isRunning()

    @property
    def is_stage2_running(self) -> bool:
        return self._seam_worker is not None and self._seam_worker.isRunning()

    @property
    def is_busy(self) -> bool:
        return self.is_stage1_running or self.is_stage2_running

    @property
    def seams_mode(self) -> str:
        """接缝合并级别："" 未处理 / "prelim" 规则初步（待阅读精修） / "final" 定稿。"""
        return self._seams_mode

    @property
    def stage1_progress_ratio(self) -> float:
        if self._manifest is None:
            return 0.0
        return self._manifest.progress_ratio

    def get_page_cache(self, page_num: int) -> dict | None:
        """读取指定页的缓存数据。"""
        filepath = os.path.join(self._cache_dir, f"page_{page_num:03d}.json")
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None

    def start_stage1(self) -> None:
        """启动 Stage 1 本地版式解析。"""
        if self._manifest is None:
            self.stage1_error.emit(self._pdf_path, 0, "Manifest 初始化失败")
            return

        # 如果全部已完成，直接发信号
        if self._manifest.is_complete:
            self.stage1_progress.emit(
                self._pdf_path,
                self._manifest.done_count + self._manifest.error_count,
                self._manifest.total_pages,
            )
            self.stage1_complete.emit(self._pdf_path)
            return

        self._start_stage1_docling()

    def _start_stage1_docling(self) -> None:
        """用 Docling 本地解析 Stage 1，不再回退到视觉模型。"""
        self._docling_worker = DoclingParseWorker(
            self._pdf_path, self._cache_dir, self._manifest
        )
        track(self._docling_worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        self._docling_worker.page_done.connect(
            lambda pn: self._on_stage1_page_done(pn, {})
        )
        self._docling_worker.progress.connect(
            lambda cur, tot: self.stage1_progress.emit(self._pdf_path, cur, tot)
        )
        self._docling_worker.status.connect(
            lambda message: self.stage1_status.emit(self._pdf_path, message)
        )
        self._docling_worker.completed.connect(lambda: self._on_stage1_all_done(self._manifest))
        self._docling_worker.error.connect(self._on_stage1_docling_error)
        self._docling_worker.start()

    def _on_stage1_docling_error(self, error_msg: str) -> None:
        """Docling 解析失败 → 只提示错误，不再调用视觉模型。"""
        self.stage1_error.emit(self._pdf_path, 0, error_msg)

    def start_stage2(self, preliminary: bool = False) -> None:
        """启动 Stage 2：规则组装（同步、秒级）+ 跨页接缝合并（后台异步）。

        规则组装不依赖 LLM，立即产出结构化文档；随后若存在疑似跨页
        断裂接缝且没有定稿缓存，才用解析接口做小规模合并确认。

        preliminary=True 为后台建库模式（library_preparser）：全程零 LLM，
        规则合并结果写入 merged_seams_prelim 并把 states 标记
        seams_final=False，留给用户点开阅读时再精修定稿。
        """
        if self._manifest is None:
            self.stage2_error.emit(self._pdf_path, "没有页面缓存数据")
            return

        done_count = self._manifest.done_count
        if done_count == 0:
            if (not self._manifest.is_complete and not self.is_stage1_running
                    and self._stage1_retries < self._STAGE1_MAX_RETRIES):
                # 尚未有任何页面解析完成：回退到 Stage 1 自我修复，
                # 而不是直接报"没有已完成的页面"（可能由缓存被清/竞态引起）。
                # 但最多重试有限次数，避免解析持续失败时无限循环"解析/构建"。
                self._stage1_retries += 1
                self.start_stage1()
                return
            self.stage2_error.emit(self._pdf_path, "没有已完成的页面，请等待 Stage 1 完成")
            return

        page_data = self._load_all_page_data()
        if not page_data:
            self.stage2_error.emit(self._pdf_path, "没有可用的页面缓存数据，请等待 Stage 1 完成")
            return

        merged_final, merged_prelim = self._load_seam_caches()
        doc = build_document_fast(page_data, {**merged_prelim, **merged_final})
        if not doc.display_elements:
            self.stage2_error.emit(self._pdf_path, "规则组装结果为空（页面缓存可能异常），请重新解析")
            return
        # 检查跨页接缝：只处理到本次请求的级别（final 定稿后幂等；
        # prelim 之后的 final 请求是阅读精修，仍要重新送 LLM 复核）。
        # 级别在 emit stage2_finished 之前更新，UI 侧回调可据此触发阅读精修。
        skip_seams = (self._seams_mode == "final"
                      or (preliminary and self._seams_mode))
        if not skip_seams:
            self._seams_mode = "prelim" if preliminary else "final"
        self._on_stage2_finished(doc)
        if skip_seams:
            return
        seams = find_cross_page_seams(page_data)
        if merged_final:
            # 只按定稿缓存过滤：初步版接缝在阅读精修时仍要重新评估
            cached_keys = {str(key) for key in merged_final}
            cached_sources = {
                key.split("|", 1)[0].strip()
                for key in cached_keys
                if "|" in key
            }
            seams = [
                seam for seam in seams
                if seam["key"] not in cached_keys
                and seam["element_id_a"] not in cached_sources
            ]
        if seams:
            if self._client is not None and not preliminary:
                self._start_seam_merge(seams)
            else:
                # 没有解析 API（或后台建库）时仍保证明显的跨页正文连续，
                # 不让本地整合卡住。
                fallback = {
                    seam["key"]: {
                        "with_id": seam["element_id_b"],
                        "merged_text": _join_cross_page_text(
                            seam["text_a"], seam["text_b"]
                        ),
                    }
                    for seam in seams
                }
                self._on_seam_merge_done(fallback, preliminary=preliminary)
        elif not preliminary:
            # 没有待处理接缝：阅读模式下直接定稿（清掉后台建库的初步标记）
            self._mark_seams_final()

    def _load_all_page_data(self) -> list[dict]:
        """加载所有已完成页的完整缓存（含 bbox），供规则组装使用。"""
        results = []
        for page_num in range(1, self._manifest.total_pages + 1):
            status = self._manifest.pages.get(page_num, "pending")
            if status == "done":
                cache = self.get_page_cache(page_num)
                if cache:
                    results.append(cache)
        return results

    def _load_seam_caches(self) -> tuple[dict, dict]:
        """读取两级接缝缓存：(定稿 merged_seams, 初步 merged_seams_prelim)。"""
        from ..utils.config import load_doc_state
        try:
            state = load_doc_state(self._pdf_path)
        except Exception:
            return {}, {}
        final = state.get("merged_seams") or {}
        prelim = state.get("merged_seams_prelim") or {}
        if not isinstance(final, dict):
            final = {}
        if not isinstance(prelim, dict):
            prelim = {}
        return final, prelim

    def _start_seam_merge(self, seams: list[dict]) -> None:
        """启动跨页接缝合并（后台线程），完成后回填并刷新文档。"""
        if self._client is None:
            return
        self._seam_candidates = list(seams)
        self._seam_worker = SeamMergeWorker(self._client, seams)
        track(self._seam_worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        gen = self._generation
        self._seam_worker.finished_signal.connect(
            lambda merged: self._on_seam_merge_done_if_current(gen, merged)
        )
        self._seam_worker.error.connect(
            lambda err: self._on_seam_merge_error_if_current(gen, err)
        )
        self._seam_worker.start()

    def _on_seam_merge_done_if_current(self, gen: int, merged: dict) -> None:
        """取消/重跑后迟到的合并结果直接丢弃，防止旧接缝写回已清空的缓存。"""
        if gen != self._generation:
            return
        self._on_seam_merge_done(merged)

    def _on_seam_merge_error_if_current(self, gen: int, err: str) -> None:
        if gen != self._generation:
            return
        self._on_seam_merge_error(err)

    def _on_seam_merge_done(self, merged: dict, preliminary: bool = False) -> None:
        """接缝合并完成 → 缓存 + 重建文档 + 通知 UI 刷新。

        preliminary=True：规则合并写入 merged_seams_prelim（阅读时再精修）；
        否则为定稿：接受者入 merged_seams，被否决的初步接缝随之失效，
        states 标记 seams_final=True。
        """
        doc_changed = bool(merged)
        if preliminary:
            if merged:
                self._save_prelim_seams(merged)
        else:
            if merged:
                self._save_merged_seams(merged)
            if self._drop_rejected_prelim_seams(merged):
                doc_changed = True
            self._mark_seams_final()
        if not doc_changed or not self._integrated_doc:
            return
        try:
            page_data = self._load_all_page_data()
            final, prelim = self._load_seam_caches()
            doc = build_document_fast(page_data, {**prelim, **final})
            if not doc.display_elements:
                return
            backfill_image_paths(doc, self._cache_dir)
            self._integrated_doc = doc
            self._save_integrated_doc(doc)
            self.stage2_merged.emit(self._pdf_path, doc)
        except Exception:  # noqa: BLE001
            pass  # 合并刷新失败不影响已渲染的文档

    def _on_seam_merge_error(self, error_msg: str) -> None:
        print(f"[Docling] 跨页接缝合并失败（忽略，不影响阅读）：{error_msg}")
        if not self._seam_candidates:
            return
        # 接口限流、网络失败等情况下，启发式已确认的正文接缝仍可安全拼接，
        # 避免整合结果退回为两个被分页截断的卡片。
        fallback = {
            seam["key"]: {
                "with_id": seam["element_id_b"],
                "merged_text": _join_cross_page_text(
                    seam["text_a"], seam["text_b"]
                ),
            }
            for seam in self._seam_candidates
        }
        self._on_seam_merge_done(fallback)

    def _save_merged_seams(self, merged: dict) -> None:
        try:
            from ..utils.config import load_doc_state, save_doc_state
            state = load_doc_state(self._pdf_path)
            cache = state.get("merged_seams") or {}
            if not isinstance(cache, dict):
                cache = {}
            cache.update(merged)
            state["merged_seams"] = cache
            # 已定稿的接缝从初步缓存中移除，保持两级缓存互斥
            prelim = state.get("merged_seams_prelim") or {}
            if isinstance(prelim, dict):
                for key in merged:
                    prelim.pop(key, None)
                state["merged_seams_prelim"] = prelim
            state["seams_final"] = True
            save_doc_state(self._pdf_path, state)
        except Exception:
            pass  # 缓存失败可下次重跑接缝合并

    def _save_prelim_seams(self, merged: dict) -> None:
        """后台建库的规则合并：单独立缓存并标记待阅读精修。"""
        try:
            from ..utils.config import load_doc_state, save_doc_state
            state = load_doc_state(self._pdf_path)
            cache = state.get("merged_seams_prelim") or {}
            if not isinstance(cache, dict):
                cache = {}
            cache.update(merged)
            state["merged_seams_prelim"] = cache
            state["seams_final"] = False
            save_doc_state(self._pdf_path, state)
        except Exception:
            pass

    def _drop_rejected_prelim_seams(self, accepted: dict) -> bool:
        """LLM 定稿后剔除被否决的初步接缝，避免规则版压制定稿结果。

        Returns:
            是否有剔除（有则调用方需要重建文档）。
        """
        if not self._seam_candidates:
            return False
        try:
            from ..utils.config import load_doc_state, save_doc_state
            state = load_doc_state(self._pdf_path)
            prelim = state.get("merged_seams_prelim") or {}
            if not isinstance(prelim, dict) or not prelim:
                return False
            changed = False
            for seam in self._seam_candidates:
                key = str(seam.get("key", ""))
                if key and key not in accepted and key in prelim:
                    prelim.pop(key, None)
                    changed = True
            if not changed:
                return False
            state["merged_seams_prelim"] = prelim
            save_doc_state(self._pdf_path, state)
            return True
        except Exception:
            return False

    def _mark_seams_final(self) -> None:
        """阅读模式定稿：清掉后台建库留下的初步整合标记。"""
        try:
            from ..utils.config import load_doc_state, save_doc_state
            state = load_doc_state(self._pdf_path)
            if state.get("seams_final") is False:
                state["seams_final"] = True
                save_doc_state(self._pdf_path, state)
        except Exception:
            pass

    def cancel(self) -> None:
        """取消所有进行中的操作。

        Docling/torch 可能在执行原生代码，必须等线程自然退出；但等待有界
        （GUI 线程调用时无界等待会冻结界面），超时后线程由 track() 注册表
        保活自行退出。
        """
        self._generation += 1  # 使在途后台结果的回调全部失效
        if self._seam_worker and self._seam_worker.isRunning():
            self._seam_worker.requestInterruption()
        if self._docling_worker and self._docling_worker.isRunning():
            self._docling_worker.requestInterruption()
            # 逐页循环会尽快响应中断；有界等待避免主线程长时间冻结
            self._docling_worker.wait(10_000)

    # ---- 内部回调 ----

    def _on_stage1_page_done(self, page_num: int, result: dict) -> None:
        """单页完成 → 发射进度信号。"""
        done = self._manifest.done_count + self._manifest.error_count
        self.stage1_progress.emit(self._pdf_path, done, self._manifest.total_pages)
        self.stage1_page_done.emit(self._pdf_path, page_num)

    def _on_stage1_all_done(self, manifest: PageManifest) -> None:
        """Stage 1 全部完成。

        只有确实解析出页面（done_count > 0）才广播完成事件；否则解析必然已
        通过 error 信号报告失败，这里直接忽略，避免失败也被当成「完成」。
        """
        if manifest is None or manifest.done_count == 0:
            return
        done = manifest.done_count + manifest.error_count
        self.stage1_progress.emit(self._pdf_path, done, manifest.total_pages)
        self.stage1_complete.emit(self._pdf_path)

    def _on_stage1_page_error(self, page_num: int, error_msg: str) -> None:
        """单页错误 → 发射错误信号。"""
        self.stage1_error.emit(self._pdf_path, page_num, error_msg)

    def _on_stage2_finished(self, doc: StructuredDocument) -> None:
        """Stage 2 组装完成 → 回填截图 → 落盘 → 通知 UI。"""
        backfill_image_paths(doc, self._cache_dir)
        self._integrated_doc = doc
        self._save_integrated_doc(doc)
        self.stage2_finished.emit(self._pdf_path, doc)

    def _save_integrated_doc(self, doc: StructuredDocument) -> None:
        """整合结果持久化到 states/，后台任务不依赖前台 UI 也能保存。"""
        try:
            from ..utils.config import load_doc_state, save_doc_state
            state = load_doc_state(self._pdf_path)
            state["structured_document"] = doc.to_dict()
            state["doc_format"] = "fast"  # 标记规则组装格式，旧版整合结果据此重建
            state["fast_version"] = FAST_DOCUMENT_VERSION
            try:
                state["pdf_mtime"] = os.path.getmtime(self._pdf_path)
            except OSError:
                pass
            save_doc_state(self._pdf_path, state)
        except Exception:
            pass  # 保存失败不阻塞流程，可随时重新整合

    def _on_stage2_error(self, error_msg: str) -> None:
        """Stage 2 组装失败。"""
        self.stage2_error.emit(self._pdf_path, error_msg)
