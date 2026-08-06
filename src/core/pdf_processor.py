"""PDF 智能处理器 —— 两阶段管线：逐页视觉解析 + 跨页整合。

Stage 1（自动触发）: PDF导入 → 逐页(图片+文本) → 视觉LLM → 结构化JSON → 缓存到磁盘
Stage 2（用户点击）: 读缓存 → LLM跨页整合 → StructuredDocument → UI渲染
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from PySide6.QtCore import QObject, QThread, Signal

if TYPE_CHECKING:
    from .llm_client import LLMClient


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
        """从字典（JSON加载）构造 StructuredElement，处理 bbox 转换。"""
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
        """从字典恢复 StructuredDocument。"""
        return StructuredDocument(
            title=d.get("title", ""),
            authors=d.get("authors", ""),
            display_elements=[StructuredElement.from_dict(e) for e in d.get("display_elements", [])],
            metadata_pool=[StructuredElement.from_dict(e) for e in d.get("metadata_pool", [])],
            toc=d.get("toc", []),
            figures=[StructuredElement.from_dict(e) for e in d.get("figures", [])],
            tables=[StructuredElement.from_dict(e) for e in d.get("tables", [])],
            references=[StructuredElement.from_dict(e) for e in d.get("references", [])],
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
    parser: str = "vision"        # 逐页解析引擎: vision | docling

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
            parser=d.get("parser", "vision"),
        )


# ============================================================
# LLM Prompt 模板
# ============================================================

PAGE_ANALYSIS_SYSTEM_PROMPT = """你是一位学术论文结构分析专家。你会收到一页论文的渲染图片和对应的提取文本。

请仔细分析该页面，识别其中所有有意义的元素，并以 JSON 格式返回。

## 元素类型（type 字段）

- "title": 论文标题
- "subtitle": 章节/小节标题
- "authors": 作者列表
- "affiliations": 作者单位
- "abstract_heading": "Abstract"/"摘要" 这个词本身
- "abstract_body": 摘要正文
- "body": 普通正文段落
- "keywords": 关键词列表
- "figure": 学术插图/图表
- "table": 学术表格
- "figure_caption": 图表标题文字（与 figure/table 分开标注）
- "table_caption": 表格标题文字
- "reference": 参考文献条目
- "metadata": 出版信息（DOI、期刊名、卷期、页码、日期、版权声明）
- "header_footer": 页眉/页脚/页码
- "publisher_logo": 出版商 logo 或装饰性图片
- "equation": 数学公式
- "acknowledgment": 致谢
- "appendix": 附录
- "unknown": 无法确定

## 判断 is_meaningful

- 学术插图/表格 → true
- 出版商 logo、装饰图、空白 → false

## bbox 坐标

- bbox 使用页面渲染图的像素坐标 [x0, y0, x1, y1]
- 坐标原点在页面左上角
- 尽量精确，特别是 figure/table 类型

## 要求

1. 遍历页面上每个有意义的文字块和图形，不要遗漏
2. 正文段落按自然段划分，不要过碎
3. 图表要标注 caption 和简短的自然语言描述（description 字段）
4. 公式保留 LaTeX 或原文格式
5. 页眉页脚/页码识别后标注 type=header_footer
6. 只返回 JSON，不要加任何解释或 Markdown 标记
7. JSON 格式：{"page": 页码, "page_role": "页面角色", "elements": [...]}
8. 每个元素必须包含 "text" 字段（不是 content），值为该元素的文本内容
9. 每个元素必须包含 "id" 字段，格式如 "p1_e1", "p1_e2" 等（p页码_e序号）
10. bbox 坐标是像素坐标 [x0, y0, x1, y1]，原点在页面左上角
11. 可选字段 "section_name": 如果你能确定该元素所属的章节（如 "Abstract", "Introduction", "Results", "Discussion", "Methods", "Conclusion"），请填写；不确定则留空字符串 ""

## 输出示例（仅作格式参考，实际元素按页面内容增减）

{"page": 1, "page_role": "title_page", "elements": [
  {"id": "p1_e1", "type": "title", "text": "Deep Learning for Medical Image Analysis", "bbox": [120, 80, 780, 130], "is_bold": true, "section_name": ""},
  {"id": "p1_e2", "type": "authors", "text": "Y. Wang, X. Li, Z. Chen", "bbox": [120, 150, 500, 170], "section_name": ""},
  {"id": "p1_e3", "type": "affiliations", "text": "School of Medicine, Peking University", "bbox": [120, 175, 600, 200], "section_name": ""},
  {"id": "p1_e4", "type": "abstract_heading", "text": "Abstract", "bbox": [120, 240, 220, 260], "section_name": "Abstract"},
  {"id": "p1_e5", "type": "abstract_body", "text": "This paper presents a novel framework ...", "bbox": [120, 265, 780, 380], "section_name": "Abstract"},
  {"id": "p1_e6", "type": "body", "text": "Recent advances in deep learning have ...", "bbox": [120, 400, 780, 520], "section_name": "Introduction"}
]}
"""


INTEGRATION_SYSTEM_PROMPT = """你是一位学术论文编辑专家。你会收到一篇论文所有页面的结构化解析结果。

你的任务是将这些逐页的元素整合为一份完整的、可阅读的结构化文档。

## 任务

1. **构建层级目录树**: 识别所有章节标题及其层级关系（如 "1. Introduction" → "1.1 Background"）
2. **合并断裂段落**: 跨页的正文段落合并为完整段落
3. **排序元素**: 按阅读顺序排列所有元素
4. **标注展示优先级**: 
   - "high": 标题、摘要、图表（用户最关心）
   - "normal": 正文段落
   - "low": 作者、单位、关键词
   - "collapsed": 出版信息、DOI、版权、致谢、附录、参考文献
5. **图表编号**: 统一编号所有图片和表格
6. **提取元信息**: 标题、作者等汇总到顶层字段
7. **标注章节归属**: 给每个 body/subtitle/abstract_body 元素填写 section_name（如 "Abstract", "Introduction", "Methods", "Results", "Discussion", "Conclusion" 等）。如果 Stage 1 已经填了但不准确，请根据全文上下文纠正
8. **确定标题层级**: 给每个 subtitle 元素填写 heading_level（1=一级章节如 Introduction/Results/Discussion, 2=二级小节, 3=三级小节, 0=非标题）

## 输出 JSON 格式

{
  "title": "论文标题",
  "authors": "作者列表（逗号分隔）",
  "toc": [{"level": 1, "title": "Introduction", "element_index": 3}, ...],
  "display_elements": [
    {
      "element_type": "title",
      "text": "...",
      "page": 1,
      "heading_level": 0,
      "display_priority": "high",
      "section_name": "",
      "element_id": "p1_e2"
    },
    ...
  ],
  "metadata_pool": [...],
  "figures": [...],
  "tables": [...],
  "references": [...],
  "raw_page_count": 30
}

## 重要原则

1. display_elements 按阅读顺序排列，标题在前、正文在后
2. 元信息（作者/单位/出版信息）放入 metadata_pool，display_elements 中可弱化（display_priority=low/collapsed）
3. 正文段落合理分段（每段 200-800 字为宜），在语义边界处断开
4. 图表元素保留原始 element_id，以便后续定位截图
5. 参考文献集中放在 references 数组中
6. 只返回 JSON，不要加任何解释或 Markdown 标记"""


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

    容错策略：
    1. 直接 JSON 解析
    2. 提取 ```json ... ``` 代码块
    3. 提取第一个 { 到最后一个 }
    4. 全部失败则返回降级结果

    Returns:
        {"page": int, "page_role": str, "elements": list[dict], "parse_error": str|None}
    """
    import re

    if not raw or not raw.strip():
        return _fallback_page_result(page_num, "LLM 返回为空")

    text = raw.strip()

    # 尝试 1: 直接解析
    obj = _try_parse_json(text)
    if obj is not None:
        return _normalize_page_result(obj, page_num)

    # 尝试 2: ```json ... ``` 或 ``` ... ```
    for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            obj = _try_parse_json(m.group(1).strip())
            if obj is not None:
                return _normalize_page_result(obj, page_num)

    # 尝试 3: 第一个 { 到最后一个 }
    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace >= 0 and last_brace > first_brace:
        obj = _try_parse_json(text[first_brace:last_brace + 1])
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
    """解析并校验 LLM 返回的整合 JSON。"""
    import re

    if not raw or not raw.strip():
        return {"error": "LLM 返回为空"}

    text = raw.strip()

    obj = _try_parse_json(text)
    if obj is not None:
        return obj

    for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            obj = _try_parse_json(m.group(1).strip())
            if obj is not None:
                return obj

    first_brace = text.find('{')
    last_brace = text.rfind('}')
    if first_brace >= 0 and last_brace > first_brace:
        obj = _try_parse_json(text[first_brace:last_brace + 1])
        if obj is not None:
            return obj

    return {"error": f"无法解析整合结果 JSON（前100字符: {raw[:100]}）"}


# ============================================================
# 后台工作线程
# ============================================================

class PageAnalysisWorker(QThread):
    """后台单页分析线程 —— 发送一页给视觉 LLM 并解析结果。"""

    finished = Signal(int, dict)   # (page_num, result_dict)
    error = Signal(int, str)      # (page_num, error_message)

    def __init__(self, client: LLMClient, page_num: int,
                 page_image_b64: str, page_text: str):
        super().__init__()
        self._client = client
        self._page_num = page_num
        self._image_b64 = page_image_b64
        self._text = page_text

    def run(self) -> None:
        try:
            messages = [
                {"role": "system", "content": PAGE_ANALYSIS_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                f"请分析第 {self._page_num} 页论文。\n\n"
                                f"【提取文本供参考】\n{self._text}"
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {"url": self._image_b64},
                        },
                    ],
                },
            ]
            response = self._client.chat_sync(messages, timeout=180.0, json_mode=True)
            result = _validate_page_result(response, self._page_num)
            self.finished.emit(self._page_num, result)
        except Exception as e:
            self.error.emit(self._page_num, str(e))


class IntegrationWorker(QThread):
    """后台跨页整合线程 —— 读缓存、发 LLM、返回 StructuredDocument。"""

    finished = Signal(object)    # StructuredDocument
    error = Signal(str)

    def __init__(self, client: LLMClient, all_page_data: list[dict],
                 pdf_title: str = ""):
        super().__init__()
        self._client = client
        self._all_page_data = all_page_data
        self._pdf_title = pdf_title

    def run(self) -> None:
        try:
            pages_json = json.dumps(
                self._all_page_data, ensure_ascii=False, indent=2
            )
            user_prompt = (
                f"请整合以下 {len(self._all_page_data)} 页论文的结构化数据。\n\n"
                f"【逐页数据】\n{pages_json}"
            )
            messages = [
                {"role": "system", "content": INTEGRATION_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ]
            response = self._client.chat_sync(messages, timeout=300.0, json_mode=True)
            result = _validate_integration_result(response)

            if "error" in result:
                self.error.emit(result["error"])
                return

            doc = StructuredDocument.from_dict(result)
            doc.raw_page_count = len(self._all_page_data)
            self.finished.emit(doc)
        except Exception as e:
            self.error.emit(str(e))


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
            pix.save(output_path)

            elem["image_path"] = filename
            elem["image_caption"] = elem.get("caption", "")

        doc.close()
    except Exception:
        pass  # 裁剪失败不阻塞流程


class DoclingParseWorker(QThread):
    """Docling 本地解析整本 PDF → 逐页缓存（Stage 1 的本地引擎）。"""

    page_done = Signal(int)            # page_num（复用 PDFProcessor._on_stage1_page_done）
    progress = Signal(int, int)        # current, total
    finished = Signal()
    error = Signal(str)

    def __init__(self, pdf_path: str, cache_dir: str, manifest: "PageManifest",
                 parent=None):
        super().__init__(parent)
        self._pdf_path = pdf_path
        self._cache_dir = cache_dir
        self._manifest = manifest

    def run(self) -> None:
        try:
            from .docling_parser import parse_pdf
            pages = parse_pdf(self._pdf_path)
        except Exception as e:  # noqa: BLE001
            self.error.emit(f"Docling 解析失败：{e}")
            return

        self._manifest.total_pages = len(pages)
        for i, page in enumerate(pages):
            page_num = int(page.get("page", i + 1))
            elements = page.get("elements", [])

            # 裁剪图表区域
            crop_meaningful_images(self._pdf_path, self._cache_dir, page_num, elements)

            result = {
                "page": page_num,
                "status": "done",
                "page_role": page.get("page_role", "content_page"),
                "elements": elements,
                "raw_text": "",
                "error_message": "",
                "processed_at": time.time(),
                "parser": "docling",
            }
            self._save_page_cache(page_num, result)
            self._manifest.pages[page_num] = "done"
            self._manifest.updated_at = time.time()
            self._save_manifest()

            self.page_done.emit(page_num)
            self.progress.emit(i + 1, len(pages))

        self.finished.emit()

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
# PageAnalyzer —— Stage 1: 逐页视觉解析
# ============================================================

class PageAnalyzer:
    """逐页分析器 —— 将 PDF 每页（图片+文本）发送给视觉 LLM 解析。

    特性：
    - 并发控制（最多 MAX_CONCURRENT 页同时在飞）
    - 每页结果立即缓存 + 更新 manifest
    - 单页失败不阻断整体流程
    - 自动跳过已完成页（断点续传）
    - max_concurrent=1 时为同步顺序模式
    """

    def __init__(self, pdf_path: str, llm_client: LLMClient,
                 cache_dir: str, manifest: PageManifest,
                 max_concurrent: int = 3) -> None:
        self._pdf_path = pdf_path
        self._client = llm_client
        self._cache_dir = cache_dir
        self._manifest = manifest
        self._max_concurrent = max(1, max_concurrent)
        self._page_texts: dict[int, str] = {}
        self._page_images: dict[int, str] = {}
        self._pending_pages: list[int] = []
        self._active_workers: dict[int, PageAnalysisWorker] = {}
        self._on_page_done: callable | None = None
        self._on_all_done: callable | None = None
        self._on_error: callable | None = None

    # ---- 公共 API ----

    def set_callbacks(self, on_page_done=None, on_all_done=None, on_error=None):
        """设置回调函数。"""
        self._on_page_done = on_page_done
        self._on_all_done = on_all_done
        self._on_error = on_error

    def start(self) -> None:
        """启动逐页分析流程。"""
        # 提取所有页文本和图片
        self._extract_raw_data()

        # 确定待处理页（跳过已完成的）
        self._pending_pages = [
            p for p in range(1, self._manifest.total_pages + 1)
            if self._manifest.pages.get(p, "pending") != "done"
        ]

        if not self._pending_pages:
            if self._on_all_done:
                self._on_all_done(self._manifest)
            return

        # 启动初始并发
        for _ in range(min(self._max_concurrent, len(self._pending_pages))):
            self._start_next_page()

    def cancel(self) -> None:
        """取消所有进行中的分析。"""
        for worker in list(self._active_workers.values()):
            if worker.isRunning():
                worker.quit()
                worker.wait(1000)
        self._active_workers.clear()
        self._pending_pages.clear()

    # ---- 内部方法 ----

    def _extract_raw_data(self) -> None:
        """使用 PyMuPDF 提取每页文本和渲染图片。"""
        import fitz

        doc = fitz.open(self._pdf_path)
        self._manifest.total_pages = len(doc)

        for i in range(len(doc)):
            page_num = i + 1
            page = doc[i]
            self._page_texts[page_num] = page.get_text().strip()

            mat = fitz.Matrix(150 / 72, 150 / 72)
            pix = page.get_pixmap(matrix=mat)
            import base64
            img_bytes = pix.tobytes("png")
            b64 = base64.b64encode(img_bytes).decode("ascii")
            self._page_images[page_num] = f"data:image/png;base64,{b64}"

        doc.close()

    def _start_next_page(self) -> None:
        """从待处理队列取下一页并启动分析。"""
        if not self._pending_pages:
            # 检查是否全部完成
            if not self._active_workers and self._on_all_done:
                self._on_all_done(self._manifest)
            return

        page_num = self._pending_pages.pop(0)
        self._manifest.pages[page_num] = "processing"
        self._save_manifest()

        worker = PageAnalysisWorker(
            self._client, page_num,
            self._page_images.get(page_num, ""),
            self._page_texts.get(page_num, ""),
        )
        worker.finished.connect(self._on_worker_finished)
        worker.error.connect(self._on_worker_error)
        self._active_workers[page_num] = worker
        worker.start()

    def _on_worker_finished(self, page_num: int, result: dict) -> None:
        """单页分析完成回调。"""
        self._active_workers.pop(page_num, None)

        if result.get("parse_error"):
            # 解析失败，标记为 error
            self._manifest.pages[page_num] = "error"
            result["status"] = "error"
            result["error_message"] = result["parse_error"]
        else:
            self._manifest.pages[page_num] = "done"
            result["status"] = "done"

        # 裁剪有意义的图片/表格（先裁剪后保存，image_path 才能进入页面缓存）
        self._crop_meaningful_images(page_num, result.get("elements", []))

        # 保存缓存页
        result["raw_text"] = self._page_texts.get(page_num, "")
        result["processed_at"] = time.time()
        self._save_page_cache(page_num, result)

        # 更新 manifest
        self._manifest.updated_at = time.time()
        self._save_manifest()

        if self._on_page_done:
            self._on_page_done(page_num, result)

        # 启动下一页
        self._start_next_page()

    def _on_worker_error(self, page_num: int, error_msg: str) -> None:
        """单页分析失败回调。"""
        self._active_workers.pop(page_num, None)

        self._manifest.pages[page_num] = "error"
        self._manifest.updated_at = time.time()
        self._save_manifest()

        error_result = {
            "page": page_num,
            "status": "error",
            "page_role": "unknown",
            "elements": [],
            "raw_text": self._page_texts.get(page_num, ""),
            "error_message": error_msg,
            "processed_at": time.time(),
        }
        self._save_page_cache(page_num, error_result)

        if self._on_error:
            self._on_error(page_num, error_msg)

        # 继续下一页
        self._start_next_page()

    def _crop_meaningful_images(self, page_num: int, elements: list[dict]) -> None:
        """裁剪 LLM 标注的有意义图片/表格区域，保存为 PNG。"""
        crop_meaningful_images(self._pdf_path, self._cache_dir, page_num, elements)

    # ---- 缓存读写 ----

    def _save_page_cache(self, page_num: int, data: dict) -> None:
        """保存单页解析结果到 JSON 文件。"""
        os.makedirs(self._cache_dir, exist_ok=True)
        filepath = os.path.join(self._cache_dir, f"page_{page_num:03d}.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

    def _save_manifest(self) -> None:
        """保存 manifest 到缓存目录。"""
        os.makedirs(self._cache_dir, exist_ok=True)
        filepath = os.path.join(self._cache_dir, "_manifest.json")
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(self._manifest.to_dict(), f, ensure_ascii=False, indent=2)


# ============================================================
# DocumentIntegrator —— Stage 2: 跨页整合
# ============================================================

class DocumentIntegrator:
    """跨页整合器 —— 读取所有页缓存，发送给 LLM 整合为 StructuredDocument。

    特性：
    - 只读已完成页（status=done），跳过 error 页
    - 可重试（用户不满意整合效果可重新触发）
    - 整合结果缓存到 states/{pdf_md5}.json 中
    """

    def __init__(self, pdf_path: str, llm_client: LLMClient,
                 cache_dir: str, manifest: PageManifest) -> None:
        self._pdf_path = pdf_path
        self._client = llm_client
        self._cache_dir = cache_dir
        self._manifest = manifest
        self._worker: IntegrationWorker | None = None

    def integrate_async(self, on_finished: callable, on_error: callable) -> None:
        """异步执行跨页整合。

        Args:
            on_finished: (StructuredDocument) -> None
            on_error: (str) -> None
        """
        all_page_data = self._load_all_page_caches()
        if not all_page_data:
            on_error("没有可用的页面缓存数据，请等待 Stage 1 完成")
            return

        self._worker = IntegrationWorker(
            self._client, all_page_data, os.path.basename(self._pdf_path)
        )
        self._worker.finished.connect(on_finished)
        self._worker.error.connect(on_error)
        self._worker.start()

    def cancel(self) -> None:
        """取消整合。"""
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(1000)

    def _load_all_page_caches(self) -> list[dict]:
        """加载所有已完成页的缓存数据。"""
        results = []
        for page_num in range(1, self._manifest.total_pages + 1):
            status = self._manifest.pages.get(page_num, "pending")
            if status == "done":
                cache = self._load_page_cache(page_num)
                if cache:
                    # 精简发给 LLM 的数据（去掉 raw_text 减少 token）
                    results.append({
                        "page": page_num,
                        "page_role": cache.get("page_role", "unknown"),
                        "elements": cache.get("elements", []),
                    })
        return results

    def _load_page_cache(self, page_num: int) -> dict | None:
        """加载单页缓存。"""
        filepath = os.path.join(self._cache_dir, f"page_{page_num:03d}.json")
        if not os.path.exists(filepath):
            return None
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            return None


# ============================================================
# PDFProcessor —— 协调者：管理两阶段流程
# ============================================================

class PDFProcessor(QObject):
    """PDF 处理协调器 —— 管理 Stage 1 逐页解析 + Stage 2 跨页整合。

    使用方式：
        processor = PDFProcessor(pdf_path, llm_client)
        processor.stage1_progress.connect(on_progress)
        processor.start_stage1()    # 自动后台开始
        # ... 用户点击论文时 ...
        processor.start_stage2()    # 读缓存 → 整合 → 渲染
    """

    # 信号
    stage1_progress = Signal(str, int, int)  # pdf_path, current, total
    stage1_page_done = Signal(str, int)       # pdf_path, page_num
    stage1_complete = Signal(str)             # pdf_path
    stage1_error = Signal(str, int, str)      # pdf_path, page_num, error
    stage2_finished = Signal(str, object)     # pdf_path, StructuredDocument
    stage2_error = Signal(str, str)           # pdf_path, error

    def __init__(self, pdf_path: str, llm_client: LLMClient) -> None:
        super().__init__()  # QObject init
        self._pdf_path = pdf_path
        self._client = llm_client
        self._analyzer: PageAnalyzer | None = None
        self._integrator: DocumentIntegrator | None = None
        self._docling_worker: DoclingParseWorker | None = None
        self._manifest: PageManifest | None = None
        self._cache_dir: str = ""
        self._integrated_doc: StructuredDocument | None = None

        self._init_cache()

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
        from ..utils.config import load_config
        return load_config().get("stage1_parser", "vision")

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
            parser=self._current_parser(),
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
        """启动 Stage 1 逐页分析（根据配置选择引擎与同步/异步）。"""
        if self._manifest is None:
            self.stage1_error.emit(self._pdf_path, 0, "Manifest 初始化失败")
            return

        # 读取用户配置
        from ..utils.config import load_config
        config = load_config()
        parser = config.get("stage1_parser", "vision")

        # 如果全部已完成，直接发信号
        if self._manifest.is_complete:
            self.stage1_progress.emit(
                self._pdf_path,
                self._manifest.done_count + self._manifest.error_count,
                self._manifest.total_pages,
            )
            self.stage1_complete.emit(self._pdf_path)
            return

        if parser == "docling":
            self._start_stage1_docling()
            return

        # 视觉 LLM 管线需要客户端
        if self._client is None:
            self.stage1_error.emit(self._pdf_path, 0, "未配置 API 客户端")
            return

        mode = config.get("stage1_mode", "async")
        concurrency = config.get("stage1_concurrency", 3)

        max_concurrent = 1 if mode == "sync" else max(1, min(10, concurrency))
        self._analyzer = PageAnalyzer(
            self._pdf_path, self._client, self._cache_dir, self._manifest,
            max_concurrent=max_concurrent,
        )
        self._analyzer.set_callbacks(
            on_page_done=self._on_stage1_page_done,
            on_all_done=self._on_stage1_all_done,
            on_error=self._on_stage1_page_error,
        )
        self._analyzer.start()

    def _start_stage1_docling(self) -> None:
        """用 Docling 本地解析 Stage 1（快、省；失败自动回退视觉 LLM）。"""
        self._docling_worker = DoclingParseWorker(
            self._pdf_path, self._cache_dir, self._manifest
        )
        self._docling_worker.page_done.connect(
            lambda pn: self._on_stage1_page_done(pn, {})
        )
        self._docling_worker.progress.connect(
            lambda cur, tot: self.stage1_progress.emit(self._pdf_path, cur, tot)
        )
        self._docling_worker.finished.connect(lambda: self._on_stage1_all_done(self._manifest))
        self._docling_worker.error.connect(self._on_stage1_docling_error)
        self._docling_worker.start()

    def _on_stage1_docling_error(self, error_msg: str) -> None:
        """Docling 解析失败 → 提示并回退视觉 LLM 管线。"""
        self.stage1_error.emit(self._pdf_path, 0, error_msg)
        self._manifest = self._create_fresh_manifest()
        self._analyzer = PageAnalyzer(
            self._pdf_path, self._client, self._cache_dir, self._manifest,
            max_concurrent=1,
        )
        self._analyzer.set_callbacks(
            on_page_done=self._on_stage1_page_done,
            on_all_done=self._on_stage1_all_done,
            on_error=self._on_stage1_page_error,
        )
        self._analyzer.start()

    def start_stage2(self) -> None:
        """启动 Stage 2 跨页整合（异步）。"""
        if self._client is None:
            self.stage2_error.emit(self._pdf_path, "未配置 API 客户端")
            return

        if self._manifest is None:
            self.stage2_error.emit(self._pdf_path, "没有页面缓存数据")
            return

        done_count = self._manifest.done_count
        if done_count == 0:
            self.stage2_error.emit(self._pdf_path, "没有已完成的页面，请等待 Stage 1 完成")
            return

        self._integrator = DocumentIntegrator(
            self._pdf_path, self._client, self._cache_dir, self._manifest
        )
        self._integrator.integrate_async(
            on_finished=self._on_stage2_finished,
            on_error=self._on_stage2_error,
        )

    def cancel(self) -> None:
        """取消所有进行中的操作。"""
        if self._analyzer:
            self._analyzer.cancel()
        if self._integrator:
            self._integrator.cancel()
        if self._docling_worker and self._docling_worker.isRunning():
            self._docling_worker.quit()
            if not self._docling_worker.wait(1500):
                self._docling_worker.terminate()
                self._docling_worker.wait()

    # ---- 内部回调 ----

    def _on_stage1_page_done(self, page_num: int, result: dict) -> None:
        """单页完成 → 发射进度信号。"""
        done = self._manifest.done_count + self._manifest.error_count
        self.stage1_progress.emit(self._pdf_path, done, self._manifest.total_pages)
        self.stage1_page_done.emit(self._pdf_path, page_num)

    def _on_stage1_all_done(self, manifest: PageManifest) -> None:
        """Stage 1 全部完成。"""
        done = manifest.done_count + manifest.error_count
        self.stage1_progress.emit(self._pdf_path, done, manifest.total_pages)
        self.stage1_complete.emit(self._pdf_path)

    def _on_stage1_page_error(self, page_num: int, error_msg: str) -> None:
        """单页错误 → 发射错误信号。"""
        self.stage1_error.emit(self._pdf_path, page_num, error_msg)

    def _on_stage2_finished(self, doc: StructuredDocument) -> None:
        """Stage 2 整合完成。"""
        self._backfill_image_paths(doc)
        self._integrated_doc = doc
        self.stage2_finished.emit(self._pdf_path, doc)

    def _backfill_image_paths(self, doc: StructuredDocument) -> None:
        """整合后按 element_id 从页面缓存回填图表截图路径，不依赖 LLM 透传。

        裁剪产生的 PNG 文件名格式为 page_{page:03d}_{element_id}.png，
        与页面缓存中元素一一对应，可直接确定性回填。
        """
        if not self._cache_dir or not doc:
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
            filepath = os.path.join(self._cache_dir, f"page_{elem.page:03d}.json")
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "r", encoding="utf-8") as f:
                    cache = json.load(f)
            except (json.JSONDecodeError, OSError):
                continue
            for e in cache.get("elements", []):
                if e.get("id") == elem.element_id:
                    img = e.get("image_path", "")
                    if img:
                        elem.image_path = img
                    if not elem.image_caption:
                        elem.image_caption = e.get("caption", "")
                    break

    def _on_stage2_error(self, error_msg: str) -> None:
        """Stage 2 整合失败。"""
        self.stage2_error.emit(self._pdf_path, error_msg)
