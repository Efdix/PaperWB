"""统一润色与引文核查 —— 单一 LLM 调用完成三项任务。"""

from __future__ import annotations

import os
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_client import LLMClient
    from .zotero_parser import ZoteroLibrary
    from .writing_coach import WritingCoach


UNIFIED_PROMPT = """你是学术写作助手。请对以下文本进行润色和引文核查。

{style_context}

{review_findings}

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

### 2. 润色（含去 AI 化）
提升学术表达的清晰度和流畅度，修正语法错误和不当用词。
保持引用标记不变。

同时执行「去 AI 化」处理（默认启用）：
- 词汇规范化：优先使用朴实、精准的学术词汇，避免被过度滥用的复杂词汇（如 leverage、delve into、tapestry、彰显、赋能、范式转移 等），改用直白的日常学术表达（use、investigate、context 等）
- 结构自然化：删除生硬的机械连接词（如 First and foremost、It is worth noting that、综上所述、由此可见 等），通过句子间的逻辑递进自然衔接；尽量减少破折号（—）
- 排版规范：正文中不使用加粗、斜体或强调标记
- 宁缺毋滥：如果文本已经非常自然地道，保留原文，不要为了修改而修改

### 3. 引文核查
对每处引文标记，比对上方提供的对应原文，验证表述是否准确反映原文发现。如有偏差请在润色中直接修正。

## 新增文献规则
- 你可以根据原文的逻辑需要，新增引用标记和对应的文献
- 优先使用【可用引文原文】中已载入的文献（这些已核验过）
- 如果你引用的是记忆中真实存在的文献（不在【可用引文原文】中），必须在 citation_notes 中标注 status="unchecked"，note 中写明该文献的完整信息（标题、第一作者、年份和 DOI 如能提供），方便用户核实
- 如果你不确定某文献是否真实存在，绝对不要引用。宁可少引一篇，不要编造一篇
- 引用格式必须遵循风格指南中指定的目标期刊格式。忽略原文的引用格式，所有新增和已有引用统一为期刊要求的格式（如 [1] 编号制 或 (Author, Year) 制）

## 段落结构规则
- 根据上下文的逻辑关系，在需要时主动添加过渡句和小结段落，使文章结构更清晰、衔接更自然
- 参考风格指南中的段落组织习惯和过渡方式来编写新增段落
- 如果段落间的逻辑跨度较大，用过渡句连接；如果章节末尾缺少总结，添加小结段

## 红线检查（高容忍度终检，只报致命问题）
对润色后的文本做终检，只检查三类问题：
1. 致命逻辑：是否存在前后完全矛盾的陈述
2. 术语一致性：核心概念是否在没有说明的情况下换了名字
3. 严重语病：是否存在导致句意不清的中式英语或语法结构错误

审查阈值：
- 默认假设：文本质量较高，已过多轮修改
- 仅报错原则：只有在阻碍读者理解的逻辑断层、引起歧义的术语混乱、或严重语法错误时才提出意见
- 严禁优化：可改可不改的风格问题直接忽略，不要挑刺
- 未发现问题时 logic_issues 返回空数组 []

## 禁止事项
1. 不要改变学术术语（除非原始术语本身是错误的）
2. 尽量不使文本显著变长或变短（不要进行不必要的扩写或删减）

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
  ],
  "logic_issues": [
    {
      "type": "逻辑矛盾/术语不一致/语法错误",
      "quote": "原文引用",
      "explanation": "问题说明",
      "suggestion": "修改建议"
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
  - note: 1句话说明如何处理
- logic_issues: 红线检查发现的致命问题（无问题时空数组 []）
  - type: 逻辑矛盾 | 术语不一致 | 语法错误
  - quote: 出问题的原文片段
  - explanation: 问题说明
  - suggestion: 修改建议"""


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


CN2EN_PROMPT = """你是兼具顶尖科研写作专家与资深会议审稿人双重身份的学术翻译助手。请将以下【中文草稿】翻译并润色为符合顶级会议/期刊标准的【英文学术论文片段】。

{style_context}

【中文草稿】
{selected_text}

## 要求
1. 视觉与排版：不使用加粗、斜体或引号；不使用列表，用连贯段落表达。
2. 风格与逻辑：逻辑严谨、用词准确、表达凝练连贯，尽量使用常见单词，避免生僻词；尽量避免破折号（—），用从句或同位语替代。
3. 时态规范：描述方法、架构和实验结论统一用一般现在时。
4. 术语：专业术语保留英文惯例表达，避免中式英语；首次出现可保留原文术语。

## 禁止事项
1. 不要改变引用标记与数字
2. 不要增减信息，忠实原意
3. 不要输出中文解释或多余说明

## 输出格式（严格 JSON，不要加 Markdown 标记）

{
  "polished_text": "翻译润色后的英文学术文本",
  "modification_log": ["翻译/润色要点说明（中文）"]
}
"""


class UnifiedWriter:
    """统一的润色+引文核查处理器。

    使用方式:
        uw = UnifiedWriter()
        result = uw.process(client, selected_text, coach, zotero_lib)
        # result["polished_text"] + result["citation_notes"]
    """

    def __init__(self) -> None:
        # PDF 段落索引缓存（按 pdf_path），避免同一文献多次重复建索引
        self._pdf_indexes: dict[str, object] = {}

    @staticmethod
    def _sentence_around(text: str, marker: str, max_prefix: int = 200) -> str:
        """提取包含引文标记的上下文句子，作为检索查询。"""
        idx = text.find(marker)
        if idx < 0:
            return text[:max_prefix]
        start = max(text.rfind("。", 0, idx), text.rfind(".", 0, idx),
                    text.rfind("；", 0, idx), text.rfind(";", 0, idx),
                    text.rfind("\n", 0, idx))
        start = start + 1 if start >= 0 else max(0, idx - max_prefix)
        end = idx + len(marker)
        for sep in ("。", ".", "；", ";"):
            j = text.find(sep, end)
            if j >= 0:
                end = j + 1
                break
        return text[start:end].strip() or text[:max_prefix]

    def _extract_relevant_context(self, pdf_path: str, query: str, top_k: int = 3) -> str:
        """从文献 PDF 中检索与声明最相关的段落作为证据（不再整篇塞全文）。"""
        if not pdf_path or not os.path.isfile(pdf_path):
            return "(PDF 文件缺失)"
        try:
            import fitz
            from .retriever import Bm25Retriever

            index = self._pdf_indexes.get(pdf_path)
            if index is None:
                doc = fitz.open(pdf_path)
                paras = []
                for page in doc:
                    for chunk in page.get_text().split("\n\n"):
                        c = chunk.strip()
                        if len(c) >= 40:
                            paras.append(c)
                doc.close()
                if not paras:
                    return "(PDF 无可检索文本)"
                index = Bm25Retriever()
                index.index([{"text": p, "section": "", "page": 0} for p in paras])
                self._pdf_indexes[pdf_path] = index

            hits = index.search(query or "", top_k=top_k)
            if not hits:
                return "(未检索到与声明相关的原文段落)"
            return "\n\n".join(f"…{h['text'][:600]}…" for h in hits)
        except Exception:
            return "(PDF 读取失败)"

    def process(
        self,
        write_client: "LLMClient",
        selected_text: str,
        coach: "WritingCoach",
        zotero_lib: "ZoteroLibrary | None" = None,
        writing_type: str = "综述",
        verify_only: bool = False,
        pre_citation_sources: str = "",
        review_findings: str = "",
        mode: str = "polish",
    ) -> dict:
        """执行统一润色+核查。

        Args:
            write_client: 写作 LLM 客户端。
            selected_text: 用户选中的文字（含引用标记）。
            coach: 写作教练（提供风格指南）。
            zotero_lib: Zotero 文献库（提供原文匹配）。
            writing_type: 写作类型 key。
            verify_only: 仅核查引文，不修改原文。
            pre_citation_sources: 预构建的引文原文上下文。
            review_findings: 草稿整体评价的诊断结论（可选，作为润色指导）。
            mode: "polish"（润色+去AI化+红线检查+引文核查）| "cn2en"（中译英）。

        Returns:
            {
                "polished_text": str,
                "citation_notes": [...],
                "supervisor_notes": [...],
                "logic_issues": [...],
                "modification_log": [...],
                "error": str | None
            }
        """
        if not selected_text.strip():
            return {"polished_text": "", "citation_notes": [], "error": "文本为空"}

        # 构建风格指南（精简版：仅含润色和核查需要的字段）
        system_prompt = coach.build_polish_system_prompt(writing_type) if coach else ""
        style_context = f"【风格约束】\n{system_prompt}" if system_prompt else ""

        # 中译英：不核查引文，直接按任务 prompt 处理
        if mode == "cn2en":
            template = CN2EN_PROMPT
            prompt = (template
                      .replace("{style_context}", style_context)
                      .replace("{selected_text}", selected_text))
            system_prompt = "你是学术写作编辑。只返回 JSON，不要加解释。"
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt},
            ]
            try:
                response = write_client.chat_sync(messages, timeout=180.0, json_mode=True)
                if not response or not response.strip():
                    return {"polished_text": selected_text, "citation_notes": [],
                            "supervisor_notes": [], "logic_issues": [],
                            "modification_log": [],
                            "error": "LLM 返回了空响应。"}
                result = self._parse_response(response)
                polished = result.get("polished_text", "")
                if not polished.strip():
                    polished = selected_text
                return {"polished_text": polished,
                        "citation_notes": [],
                        "supervisor_notes": [],
                        "logic_issues": [],
                        "modification_log": result.get("modification_log", []),
                        "citation_sources_text": "",
                        "error": None}
            except Exception as e:
                return {"polished_text": selected_text, "citation_notes": [],
                        "supervisor_notes": [], "logic_issues": [],
                        "modification_log": [],
                        "citation_sources_text": "", "error": str(e)}

        # 构建引文原文上下文（优先使用预构建的，否则本地匹配）
        citation_sources = pre_citation_sources or self._build_citation_sources(selected_text, zotero_lib)

        if verify_only:
            # 用户文本最后替换：其中若含字面 {citation_sources} 等占位符也不会被二次展开
            prompt = (VERIFY_ONLY_PROMPT
                .replace("{style_context}", "")
                .replace("{citation_sources}", citation_sources)
                .replace("{selected_text}", selected_text))
            system_prompt = "你是学术写作核查专家。只返回 JSON，不要修改原文。"
        else:
            review_section = f"【评价诊断】（以下问题来自草稿整体评价，请在润色时一并修正）\n{review_findings}" if review_findings else ""
            prompt = (UNIFIED_PROMPT
                .replace("{style_context}", style_context)
                .replace("{review_findings}", review_section)
                .replace("{citation_sources}", citation_sources)
                .replace("{selected_text}", selected_text))

        messages = [
            {"role": "system", "content": system_prompt or "你是学术写作助手。"},
            {"role": "user", "content": prompt},
        ]

        try:
            response = write_client.chat_sync(messages, timeout=180.0, json_mode=True)
            if not response or not response.strip():
                return {"polished_text": selected_text, "citation_notes": [],
                        "supervisor_notes": [], "logic_issues": [],
                        "citation_sources_text": citation_sources,
                        "error": "LLM 返回了空响应。可能原因：消息过长超出模型限制、API 配额用尽、或模型不支持该请求。"}
            result = self._parse_response(response)
            polished = result.get("polished_text", "")
            if not polished.strip():
                polished = selected_text
            return {"polished_text": polished,
                    "citation_notes": result.get("citation_notes", []),
                    "supervisor_notes": result.get("supervisor_notes", []),
                    "logic_issues": result.get("logic_issues", []),
                    "modification_log": result.get("modification_log", []),
                    "citation_sources_text": citation_sources,
                    "error": None}
        except Exception as e:
            return {"polished_text": selected_text, "citation_notes": [],
                    "supervisor_notes": [], "logic_issues": [],
                    "citation_sources_text": "",
                    "error": str(e)}

    def _build_citation_sources(
        self, selected_text: str, zotero_lib,
    ) -> str:
        """从 Zotero 库中匹配选中文字中的引文，返回原文摘要。

        支持引文格式: (Author et al., Year), (Author & Author, Year),
        (Author, Year)、（作者等，年份）、（作者，年份）
        """
        if not zotero_lib or not hasattr(zotero_lib, 'get_all_items'):
            return "（Zotero 未连接，无法提供引文原文）"

        # 从选中文字中提取 Author-Year 引文对（ASCII 括号 + 全角中文括号）
        markers: list[str] = []
        markers.extend(re.findall(r'\(([^)]+?,\s*\d{4}[a-z]?)\)', selected_text))
        markers.extend(re.findall(r'（([^）]+?，\s*\d{4}[a-z]?)）', selected_text))

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
            # 解析作者和年份（兼容中英文逗号）
            separator = "，" if "，" in marker_text else ","
            parts = marker_text.split(separator)
            if len(parts) < 2:
                continue
            author_part = parts[0].strip()
            year_part = parts[-1].strip()
            year_clean = year_part.rstrip('abcdefghijklmnopqrstuvwxyz')  # 2025a → 2025

            first_author = author_part.split("&")[0].split("and")[0].strip()
            first_author = re.sub(r'\s+et\s+al\.?\s*', '', first_author).strip()
            first_author = re.sub(r'等$', '', first_author).strip()  # 中文作者去掉"等"

            dedup_key = f"{first_author.lower()}|{year_clean}"
            if dedup_key in matched_authors:
                continue
            matched_authors.add(dedup_key)

            candidates = zotero_lib.find_by_citation(first_author, year_clean)
            if not candidates:
                sources.append(f"--- {marker_text}: 未在 Zotero 库中匹配到 ---\n(无原文可对照)")
                continue

            # 优先选择有 PDF 的条目，按标题去重
            pdf_items = [c for c in candidates if c.pdf_path and os.path.isfile(c.pdf_path)]
            no_pdf_items = [c for c in candidates if not c.pdf_path or not os.path.isfile(c.pdf_path)]
            best = pdf_items + no_pdf_items
            seen_titles = set()
            full_marker = f"({marker_text})"
            query = self._sentence_around(selected_text, full_marker)
            for item in best:
                key = item.title.lower()
                if key in seen_titles:
                    continue
                seen_titles.add(key)
                source_text = self._extract_relevant_context(item.pdf_path, query)
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
        """处理 [1] / [2,3] 编号格式的引文匹配。

        优先从选中文本末尾的参考文献列表解析 编号 → 作者/年份，再走
        find_by_citation 作者/年份匹配（不再依赖库顺序）。无参考文献列表
        时退回位置映射兜底（并提示用户人工核对）。
        """
        cited_nums: set[int] = set()
        for m in num_pattern.finditer(selected_text):
            for num_str in re.split(r'[,，\-]+', m.group(1)):
                num_str = num_str.strip()
                if num_str.isdigit():
                    cited_nums.add(int(num_str))

        if not cited_nums:
            return "（未检测到有效编号引用）"

        ref_map = self._parse_numbered_reference_list(selected_text)

        sources: list[str] = []
        matched_nums: set[int] = set()

        # 参考列表驱动：编号 → 作者/年份 → Zotero 匹配
        for num in sorted(cited_nums):
            entry = ref_map.get(num)
            if not entry or not entry.get("year"):
                continue
            candidates = zotero_lib.find_by_citation(entry["author"], entry["year"])
            if not candidates:
                continue
            matched_nums.add(num)
            query = self._sentence_around(selected_text, f"[{num}]")
            for item in candidates[:2]:
                source_text = self._extract_relevant_context(item.pdf_path, query)
                title = item.title[:100] if item.title else "?"
                authors = ", ".join(item.authors[:3]) if item.authors else "?"
                sources.append(
                    f"--- 引文 [{num}]（参考列表: {entry['author']}, {entry['year']}）"
                    f"→ {authors} ({item.year}) {title} ---\n{source_text}"
                )

        # 兜底：未能通过参考列表匹配的编号，位置映射（按库顺序，提示人工核对）
        items = getattr(zotero_lib, 'get_all_items', lambda: [])()
        for num in sorted(cited_nums):
            if num in matched_nums or not (1 <= num <= len(items)):
                continue
            item = items[num - 1]
            query = self._sentence_around(selected_text, f"[{num}]")
            source_text = self._extract_relevant_context(item.pdf_path, query)
            title = item.title[:100] if item.title else "?"
            authors = ", ".join(item.authors[:3]) if item.authors else "?"
            sources.append(
                f"--- 引文 [{num}] → {authors} ({item.year}) {title}（按库顺序匹配，请人工核对）---\n"
                f"{source_text}"
            )

        if not sources:
            return "（Zotero 库中无对应编号的文献）"
        return "\n\n".join(sources)

    @staticmethod
    def _parse_numbered_reference_list(text: str) -> dict[int, dict]:
        """从文本末尾的参考文献列表解析 编号 → {author, year}。

        支持 [1] ... 2024. 与 1. ... 2024. 两种常见编号格式。
        """
        result: dict[int, dict] = {}
        lines = text.splitlines()
        start = max(0, len(lines) - 40)
        for line in lines[start:]:
            m = re.match(
                r'^\s*\[?(\d+)\]?[\.\s、]\s*(.*?)\b((?:19|20)\d{2})\b.*$', line
            )
            if not m:
                continue
            num = int(m.group(1))
            year = m.group(3)
            # 第一作者姓氏 = 条目中的第一个词（兼容 Smith J, ... / Zhang W. ... / Zhang等）
            author = (m.group(2).strip().split() or [""])[0]
            author = re.sub(r'[,，;；、]+$', '', author).strip()
            if author:
                result[num] = {"author": author, "year": year}
        return result

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
    def extract_citations_via_llm(draft_text: str, citation_count: int, llm_client: "LLMClient") -> list[dict]:
        """让 LLM 识别草稿中的引文标记，返回 author_hint + year_hint 列表。"""
        prompt = CITATION_EXTRACT_PROMPT.replace("{draft_text}", draft_text[:10000]).replace("{citation_count}", str(citation_count))
        messages = [
            {"role": "system", "content": "你是学术文献识别专家。只返回 JSON，不要加解释。"},
            {"role": "user", "content": prompt},
        ]
        try:
            response = llm_client.chat_sync(messages, timeout=60.0, json_mode=True)
            if not response or not response.strip():
                return []
            from .json_utils import parse_json_response
            obj = parse_json_response(response)
            if not obj:
                return []
            raw = obj.get("citations")
            if not isinstance(raw, list):
                return []
            # 出口规范化：LLM 可能把字段返回为 null/数字，
            # UI 线程直接 .strip()/str.find() 会抛异常
            citations: list[dict] = []
            for c in raw:
                if not isinstance(c, dict):
                    continue
                citations.append({
                    "original_marker": str(c.get("original_marker") or "?"),
                    "author_hint": str(c.get("author_hint") or "").strip(),
                    "year_hint": str(c.get("year_hint") or "").strip(),
                })
            return citations
        except Exception:
            pass
        return []

    @staticmethod
    def _try_parse_json(text: str) -> dict | None:
        from .json_utils import parse_json_response
        return parse_json_response(text)

    @staticmethod
    def _parse_response(raw: str) -> dict:
        """解析 LLM 返回的 JSON（多层容错 + 兜底降级）。"""
        from .json_utils import parse_json_response
        if not raw or not raw.strip():
            return {"polished_text": "", "citation_notes": [], "supervisor_notes": [],
                    "logic_issues": []}

        obj = parse_json_response(raw)
        if obj is not None:
            return obj

        # 兜底降级: 所有 JSON 解析都失败，将整个原始返回作为 polished_text
        return {"polished_text": raw.strip(), "citation_notes": [], "supervisor_notes": [],
                "logic_issues": []}


CITATION_EXTRACT_PROMPT = """你是一位学术文献识别专家。请分析以下草稿，找出文中所有的引文标记并推断出对应的文献信息。

草稿中应有 **{citation_count} 篇** 不同的引文文献。请以此数量为参考，努力找出所有文献。

【草稿】
{draft_text}

## 任务

1. 找出文中所有引文标记（不管是什么格式：Author-Year、(Author, Year)、[1] 编号、纯数字角标如 19,20、Unicode 上标等）
2. 对每篇独立的引文，推断第一作者姓氏和出版年份
3. 如果同一篇文献被多次引用，只列一次
4. 总共应找出约 {citation_count} 篇不同文献

## 输出格式

请严格返回 JSON（不要加 Markdown 标记）：

{
  "citations": [
    {"original_marker": "19", "author_hint": "第一作者姓", "year_hint": "2024"},
    {"original_marker": "20", "author_hint": "另一作者姓", "year_hint": "2023"}
  ]
}

## 说明
- author_hint: 你推断的第一作者姓氏（不确定填 "unknown"）
- year_hint: 你推断的出版年份（不确定填 "unknown"）
- 如果草稿末尾有参考文献列表，可参考其中的信息
- 只返回 JSON，不要加解释"""

