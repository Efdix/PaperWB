"""统一润色与引文核查 —— 单一 LLM 调用完成三项任务。"""

from __future__ import annotations

import json as _json
import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_client import LLMClient
    from .zotero_parser import ZoteroLibrary
    from .writing_coach import WritingCoach


UNIFIED_PROMPT = """你是学术写作助手。请对以下文本进行润色和引文核查。

{style_context}

【待处理文本】
{selected_text}

【可用引文原文】（如某引文的原文为空或无法匹配，对该引文的核查标注为 unchecked）
{citation_sources}

## 重要原则
- 风格指南描述的是写作习惯和格式规范，不限制文本的学术主题。
- 输出语言必须与输入文字的语言和主题保持一致。

## 任务

### 1. 识别并处理批注/修改意见（最高优先级）

文本中可能混入了他人（导师、审稿人、合作者）的修改意见。你需要自行判断哪些文字是"批注"而非正文。判断依据：
- 与正文的语气、人称、措辞风格明显不同
- 带有指令性或建议性口吻（如"需要补充"、"建议改成"、"这里有问题"、"把xxx改为"）
- 使用括号、中括号、特殊标记与正文隔开
- 以人名/角色名开头（如"导师："、"老师："、"Reviewer:"）
- 出现位置在段落首尾、单独成行，或紧跟在某句话之后用"——"连接

如果你判断存在批注：
a) 以批注意见为首要参考进行修改
b) 在 supervisor_notes 中记录每条批注的处理情况
c) 如果批注意见与引文原文明显矛盾，标注 flagged 但仍按批注修改，说明疑虑

注意：
- 不要机械地依赖特定标记符号——有些批注没有任何符号包裹
- 有些文字可能既是正文又包含修改意见（例如标红、加粗的修改），合并不进去就保留原样
- polished_text 中不要保留批注原文本身

### 2. 润色
提升学术表达的清晰度和流畅度，修正语法错误和不当用词。
保持引用标记不变。

### 3. 引文核查
对每处引文标记，比对上方提供的对应原文，验证表述是否准确反映原文发现。如有偏差请在润色中直接修正。

## 禁止事项（严格遵守）
1. 不要添加原文中没有的新论证、新数据或新结论
2. 不要新增、删除或合并引用标记
3. 不要改变学术术语（除非原始术语本身是错误的）
4. 不要改变段落数量和段落间的逻辑顺序
5. 尽量不使文本显著变长或变短（不要进行不必要的扩写或删减）

## 输出格式

{
  "polished_text": "润色后的完整文字（不含批注原文）",
  "citation_notes": [
    {
      "marker": "Author et al., Year",
      "status": "accurate/corrected/partial/unchecked",
      "note": "简短说明"
    }
  ],
  "supervisor_notes": [
    {
      "suggestion": "批注的关键内容摘要",
      "action": "applied/modified/flagged",
      "note": "1句话说明如何处理"
    }
  ]
}

## 字段说明
- polished_text: 不含批注原文的润色后完整文字
- citation_notes: 引文核查结果（无引文时为空数组 []）
  - status: accurate | corrected | partial | unchecked
- supervisor_notes: 每条批注一条记录（无批注时为空数组 []）
  - suggestion: 用你自己的话概括批注的关键内容
  - action: applied(已采纳) | modified(调整后采纳) | flagged(有疑虑但已按意思修改)
   - note: 1句话说明如何处理"""


VERIFY_ONLY_PROMPT = """你是学术写作核查专家。只核查引文，不修改原文。

{style_context}

【待核查文本】
{selected_text}

【可用引文原文】（如某引文的原文为空或无法匹配，对该引文的核查标注为 unchecked）
{citation_sources}

## 任务

对每处引文标记，比对上方提供的对应原文，验证表述是否准确反映原文发现。
- 如果表述准确 → status: "accurate"
- 如果有偏差 → status: "corrected"，在 note 中说明差异
- 如果原文中有但引文中表述不完整 → status: "partial"
- 如果无原文可核查 → status: "unchecked"

## 禁止事项
1. 不要修改原文任何字词
2. polished_text 必须和输入文本完全一致

## 输出格式

{
  "polished_text": "（必须与输入文本完全一致）",
  "citation_notes": [...],
  "supervisor_notes": []
}"""


class UnifiedWriter:
    """统一的润色+引文核查处理器。

    使用方式:
        uw = UnifiedWriter()
        result = uw.process(client, selected_text, coach, zotero_lib)
        # result["polished_text"] + result["citation_notes"]
    """

    def __init__(self) -> None:
        pass

    def process(
        self,
        write_client: "LLMClient",
        selected_text: str,
        coach: "WritingCoach",
        zotero_lib: "ZoteroLibrary | None" = None,
        writing_type: str = "综述",
        verify_only: bool = False,
    ) -> dict:
        """执行统一润色+核查。

        Args:
            write_client: 写作 LLM 客户端。
            selected_text: 用户选中的文字（含引用标记）。
            coach: 写作教练（提供风格指南）。
            zotero_lib: Zotero 文献库（提供原文匹配）。
            writing_type: 写作类型 key。

        Returns:
            {
                "polished_text": str,
                "citation_notes": [{"marker": str, "status": str, "note": str}],
                "error": str | None
            }
        """
        if not selected_text.strip():
            return {"polished_text": "", "citation_notes": [], "error": "文本为空"}

        # 构建风格指南（精简版：仅含润色和核查需要的字段）
        system_prompt = coach.build_polish_system_prompt(writing_type) if coach else ""
        style_context = f"【风格约束】\n{system_prompt}" if system_prompt else ""

        # 构建引文原文上下文
        citation_sources = self._build_citation_sources(selected_text, zotero_lib)

        if verify_only:
            prompt = (VERIFY_ONLY_PROMPT
                .replace("{style_context}", "")
                .replace("{selected_text}", selected_text)
                .replace("{citation_sources}", citation_sources))
            system_prompt = "你是学术写作核查专家。只返回 JSON，不要修改原文。"
        else:
            prompt = (UNIFIED_PROMPT
                .replace("{style_context}", style_context)
                .replace("{selected_text}", selected_text)
                .replace("{citation_sources}", citation_sources))

        messages = [
            {"role": "system", "content": system_prompt or "你是学术写作助手。"},
            {"role": "user", "content": prompt},
        ]

        try:
            response = write_client.chat_sync(messages, timeout=180.0)
            if not response or not response.strip():
                return {"polished_text": selected_text, "citation_notes": [],
                        "supervisor_notes": [], "citation_sources_text": citation_sources,
                        "error": "LLM 返回了空响应。可能原因：消息过长超出模型限制、API 配额用尽、或模型不支持该请求。"}
            result = self._parse_response(response)
            polished = result.get("polished_text", "")
            if not polished.strip():
                polished = selected_text
            return {"polished_text": polished,
                    "citation_notes": result.get("citation_notes", []),
                    "supervisor_notes": result.get("supervisor_notes", []),
                    "citation_sources_text": citation_sources,
                    "error": None}
        except Exception as e:
            return {"polished_text": selected_text, "citation_notes": [],
                    "supervisor_notes": [], "citation_sources_text": "",
                    "error": str(e)}

    def _build_citation_sources(
        self, selected_text: str, zotero_lib,
    ) -> str:
        """从 Zotero 库中匹配选中文字中的引文，返回原文摘要。

        支持引文格式: (Author et al., Year), (Author & Author, Year), (Author, Year)
        """
        if not zotero_lib or not hasattr(zotero_lib, 'get_all_items'):
            return "（Zotero 未连接，无法提供引文原文）"

        # 从选中文字中提取 Author-Year 引文对
        cite_pattern = re.compile(r'\(([^)]+?,\s*\d{4}[a-z]?)\)')
        markers = cite_pattern.findall(selected_text)

        if not markers:
            # 尝试 [1] 编号格式
            num_pattern = re.compile(r'\[(\d+(?:[,\-]\d+)*)\]')
            num_matches = num_pattern.findall(selected_text)
            if num_matches:
                return self._build_numbered_sources(selected_text, zotero_lib, num_pattern)

            return "（未检测到引文标记）"

        sources: list[str] = []
        matched_authors: set[str] = set()

        for marker_text in markers:
            # 解析作者和年份
            parts = marker_text.split(",")
            if len(parts) < 2:
                continue
            author_part = parts[0].strip()
            year_part = parts[-1].strip()

            # 提取第一作者姓氏
            first_author = author_part.split("&")[0].split("and")[0].strip()
            first_author = re.sub(r'\s+et\s+al\.?\s*', '', first_author).strip()

            if first_author.lower() in matched_authors:
                continue
            matched_authors.add(first_author.lower())

            # 在 Zotero 库中搜索
            candidates = zotero_lib.find_by_citation(first_author, year_part)
            if not candidates:
                sources.append(f"--- {marker_text}: 未在 Zotero 库中匹配到 ---\n(无原文可对照)")
                continue

            for item in candidates[:2]:
                source_text = self._extract_pdf_text(item)
                title = item.title[:100] if item.title else "?"
                authors = ", ".join(item.authors[:3]) if item.authors else "?"
                sources.append(
                    f"--- 引文 {marker_text} → {authors} ({item.year}) {title} ---\n"
                    f"{source_text}"
                )

        if not sources:
            return "（未在 Zotero 库中匹配到任何引文）"

        return "\n\n".join(sources)

    def _build_numbered_sources(
        self, selected_text: str, zotero_lib, num_pattern,
    ) -> str:
        """处理 [1] / [2,3] 编号格式的引文匹配。"""
        cited_nums: set[int] = set()
        for m in num_pattern.finditer(selected_text):
            for num_str in re.split(r'[,，\-]+', m.group(1)):
                num_str = num_str.strip()
                if num_str.isdigit():
                    cited_nums.add(int(num_str))

        if not cited_nums:
            return "（未检测到有效编号引用）"

        sources: list[str] = []
        items = getattr(zotero_lib, 'get_all_items', lambda: [])()
        for num in sorted(cited_nums):
            if 1 <= num <= len(items):
                item = items[num - 1]
                source_text = self._extract_pdf_text(item)
                title = item.title[:100] if item.title else "?"
                authors = ", ".join(item.authors[:3]) if item.authors else "?"
                sources.append(
                    f"--- 引文 [{num}] → {authors} ({item.year}) {title} ---\n"
                    f"{source_text}"
                )

        if not sources:
            return "（Zotero 库中无对应编号的文献）"
        return "\n\n".join(sources)

    @staticmethod
    def _extract_pdf_text(item) -> str:
        """从 ZoteroItem 提取 PDF 全文。"""
        if not item.pdf_path or not os.path.isfile(item.pdf_path):
            return "(PDF 文件缺失)"

        try:
            import fitz
            doc = fitz.open(item.pdf_path)
            parts = []
            for page in doc:
                t = page.get_text()
                if t.strip():
                    parts.append(t.strip())
            doc.close()
            return "\n\n".join(parts)
        except Exception:
            return "(PDF 读取失败)"

    @staticmethod
    def _try_parse_json(text: str) -> dict | None:
        try:
            return _json.loads(text)
        except (_json.JSONDecodeError, TypeError):
            return None

    @staticmethod
    def _parse_response(raw: str) -> dict:
        """解析 LLM 返回的 JSON（五层容错 + 兜底降级）。"""
        if not raw or not raw.strip():
            return {"polished_text": "", "citation_notes": [], "supervisor_notes": []}

        text = raw.strip()

        parse = UnifiedWriter._try_parse_json

        # 尝试 1: 直接解析
        obj = parse(text)
        if obj is not None:
            return obj

        # 尝试 2: 提取 ```json ... ```
        for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                obj = parse(m.group(1).strip())
                if obj is not None:
                    return obj

        # 尝试 3: 提取 { ... }
        first = text.find('{')
        last = text.rfind('}')
        if first >= 0 and last > first:
            json_str = text[first:last + 1]
            obj = parse(json_str)
            if obj is not None:
                return obj
            # 尝试 3b: 清洗文本中未转义的换行符（LLM 常见错误）
            try:
                cleaned = re.sub(r'(?<!\\)"\s*\n\s*', r'\\n', json_str)
                cleaned = re.sub(r'(?<!\\)\n\s*"', r'\\n"', cleaned)
                obj = parse(cleaned)
                if obj is not None:
                    return obj
            except Exception:
                pass

        # 尝试 4: 用中文全角花括号替换后重试
        try:
            alt = text.replace('\uff5b', '{').replace('\uff5d', '}')
            first_a = alt.find('{')
            last_a = alt.rfind('}')
            if first_a >= 0 and last_a > first_a:
                obj = parse(alt[first_a:last_a + 1])
                if obj is not None:
                    return obj
        except Exception:
            pass

        # 兜底降级: 所有 JSON 解析都失败，将整个原始返回作为 polished_text
        return {"polished_text": text, "citation_notes": [], "supervisor_notes": []}
