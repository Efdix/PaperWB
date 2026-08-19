"""草稿整体评价器 —— 对完整草稿做结构性诊断，对比知识库基准。"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_client import LLMClient
    from .writing_coach import WritingCoach


DRAFT_REVIEW_PROMPT = """你是学术写作审稿专家。请对以下草稿进行全面结构性评价，对照知识库基准给出具体诊断。

【知识库基准】（从你的历史论文中提取，作为评判参考）
{kb_benchmarks}

【草稿全文】
{draft_text}

{local_stats}

## 评价维度

请从以下维度逐一分析，返回严格 JSON（不要 Markdown 标记）：

### 1. 各部分分析 (section_analysis)
识别草稿中的章节结构（Introduction/Methods/Results/Discussion/Conclusion 等，根据草稿实际内容判断边界）。对每个章节：
- section: 章节名
- word_count: 章节字数 —— 优先采用上方【本地实测章节统计】中对应章节的数值，不要重新估算；若该章节不在上表中再自行估算
- word_count_benchmark: 知识库中该章节的字数基准（若知识库无此章数据填 -1）
- word_count_status: "达标" / "偏少" / "偏多" / "无基准"
- paragraph_count: 段数 —— 同样优先采用本地实测值
- paragraph_benchmark: 知识库中该章节的段数基准（无数据填 -1）
- paragraph_count_status: "达标" / "偏多" / "偏少" / "无基准"
- citation_count: 引用数 —— 同样优先采用本地实测值
- citation_benchmark: 知识库基准引用数（若知识库无此章数据填 -1）
- citation_status: "达标" / "偏少" / "偏多" / "无基准"
- citation_detail_issue: 是否有某条引用描述过详或过略（null 或"引用[3]描述过详，超出基准范围"）
- has_summary: 章节末尾是否有小结
- paragraph_size_issue: 是否有孤句段落或超长段落（null 或具体描述）
- other_issues: [其他问题列表]

### 2. 过渡与小结 (transition_summary_gaps)
- gaps: [{between: "章节A → 章节B", severity: "缺失"/"偏弱"/"无", suggestion: "建议..."}]
- missing_summaries: [缺少小结的章节名列表]

### 3. 覆盖分析 (coverage_analysis)
- covered_domains: [{domain: "子方向名", coverage: "充分"/"一般"/"薄弱"}]
- overrepresented: 占比过大的方向及原因
- missing_or_thin: 遗漏或薄弱的子方向
- suggestion: 补充建议

### 4. 文献时效性 (timeliness)
- total_citations: 引用总数
- recent_3yr: 近3年内文献数
- classic_before_3yr: 3年前文献数
- assessment: "优秀"/"合理"/"偏旧"/"缺乏近3年文献"
- suggestion: 具体建议

### 5. 批判性深度 (critical_depth)
- has_comparison: 是否对不同研究进行了横向对比（true/false）
- has_contradiction_discussion: 是否讨论了研究间的矛盾或不同结论（true/false）
- has_gap_analysis: 是否指出了 research gap（true/false）
- has_future_directions: 是否提出了未来研究方向（true/false）
- assessment: 整体评价
- suggestion: 改进建议

### 6. 冗余检查 (redundancy)
- items: [{point: "重复内容描述", locations: ["章节A第N段", "章节B第M段"]}]
（无冗余时为空数组 []）

### 7. 图表建议 (figure_suggestions)
- items: [{location: "位置描述", type: "对比表"/"流程图"/"示意图", purpose: "用途"}]
（无建议时为空数组 []）

### 8. 术语一致性 (terminology_consistency)
- issues: [{concept: "概念名", variants: ["术语A", "术语B"], suggestion: "建议统一为..."}]
（无问题时为空数组 []）

### 9. 总体评价
- overall_grade: A+ / A / B+ / B / C / D
- overall_summary: 一段话总结，先夸优点，再列最关键的改进方向

## 输出格式

{{
  "section_analysis": [
    {{
      "section": "Introduction",
      "word_count": 350,
      "word_count_benchmark": 500,
      "word_count_status": "偏少",
      "paragraph_count": 3,
      "paragraph_benchmark": 3,
      "paragraph_count_status": "达标",
      "citation_count": 3,
      "citation_benchmark": 5,
      "citation_status": "偏少",
      "citation_detail_issue": null,
      "has_summary": false,
      "paragraph_size_issue": null,
      "other_issues": []
    }}
  ],
  "transition_summary_gaps": {{
    "gaps": [{{"between": "Introduction → Results", "severity": "缺失", "suggestion": "建议添加..."}}],
    "missing_summaries": []
  }},
  "coverage_analysis": {{
    "covered_domains": [],
    "overrepresented": "",
    "missing_or_thin": "",
    "suggestion": ""
  }},
  "timeliness": {{
    "total_citations": 0,
    "recent_3yr": 0,
    "classic_before_3yr": 0,
    "assessment": "",
    "suggestion": ""
  }},
  "critical_depth": {{
    "has_comparison": false,
    "has_contradiction_discussion": false,
    "has_gap_analysis": false,
    "has_future_directions": false,
    "assessment": "",
    "suggestion": ""
  }},
  "redundancy": {{
    "items": []
  }},
  "figure_suggestions": {{
    "items": []
  }},
  "terminology_consistency": {{
    "issues": []
  }},
  "overall_grade": "B",
  "overall_summary": ""
}}"""


class DraftReviewer:
    """草稿整体评价器 —— 发送全文 + 知识库基准给 LLM，获取结构性诊断报告。"""

    def __init__(self) -> None:
        pass

    def review(
        self,
        write_client: "LLMClient",
        draft_text: str,
        coach: "WritingCoach",
    ) -> dict:
        """执行草稿整体评价。

        Args:
            write_client: 写作 LLM 客户端。
            draft_text: 编辑器全文。
            coach: 写作教练（提供知识库基准）。

        Returns:
            完整的诊断结果 dict，或 {"error": "..."}
        """
        if not draft_text.strip():
            return {"error": "草稿为空"}

        kb_benchmarks = coach.build_review_benchmarks()

        # 本地实测章节统计（供 LLM 直接使用，避免估算偏差）
        local_block = ""
        local_stats = self._compute_local_section_stats(draft_text)
        if local_stats:
            lines = [
                "以下是本地程序按标题切分实测的章节统计。请直接采用这些数值作为 "
                "section_analysis 中对应章节的 word_count / paragraph_count / citation_count，不要重新估算。"
            ]
            for name, st in local_stats.items():
                lines.append(
                    f"- {name}: 约{st['word_count']}字, {st['paragraph_count']}段, "
                    f"{st['citation_count']}处引用标记"
                )
            local_block = "【本地实测章节统计】\n" + "\n".join(lines)

        prompt_text = (
            DRAFT_REVIEW_PROMPT.replace("{kb_benchmarks}", kb_benchmarks)
            .replace("{local_stats}", local_block)
            .replace("{draft_text}", draft_text)
        )

        messages = [
            {
                "role": "system",
                "content": "你是学术写作审稿专家。只返回 JSON，不要加解释。你的评价应具体、有建设性、可直接指导修改。",
            },
            {"role": "user", "content": prompt_text},
        ]

        try:
            response = write_client.chat_sync(messages, timeout=1800.0, json_mode=True)
            if not response or not response.strip():
                return {"error": "LLM 返回了空响应"}
            result = self._parse_response(response)
            if "error" in result:
                return result
            return result
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _parse_response(raw: str) -> dict:
        """解析 LLM 返回的 JSON（多层容错 + 兜底降级）。"""
        from .json_utils import parse_json_response
        result = parse_json_response(raw)
        if result is not None:
            return result
        return {"error": f"JSON 解析失败，LLM 原始返回前 200 字符：{raw[:200]}"}

    @staticmethod
    def _compute_local_section_stats(draft_text: str) -> dict:
        """本地按标题切分草稿，实测各章节字数/段数/引用数（纯统计，不依赖 LLM）。

        Returns:
            {章节名: {"word_count": int, "paragraph_count": int, "citation_count": int}}
        """
        import re as _re

        lines = draft_text.splitlines()
        sections: list[tuple[str, int]] = []  # (heading, start_line_idx)
        heading_re = _re.compile(
            r'^(\d+(\.\d+)*[\.\s、．]?'
            r'|[一二三四五六七八九十]+[、.．]'
            r'|\b(Abstract|Introduction|Background|Materials|Methods|Results|Discussion|Conclusion|References|Acknowledg(e)?ments)\b'
            r'|摘要|引言|前言|材料与方法|方法|结果|讨论|结论|参考文献|致谢)',
            _re.IGNORECASE,
        )
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not stripped:
                continue
            # 标题启发式：长度适中 + 数字编号或常见章节词开头，
            # 且不以句末标点结尾（避免把正文句子误判为标题）
            if (2 <= len(stripped) <= 60
                    and not stripped.endswith(("。", "！", "？", ".", "!", "?"))
                    and heading_re.match(stripped)):
                sections.append((stripped, i))

        if not sections:
            return {}

        stats: dict[str, dict] = {}
        for idx, (name, start) in enumerate(sections):
            end = sections[idx + 1][1] if idx + 1 < len(sections) else len(lines)
            body = "\n".join(lines[start:end])
            paras = [p for p in _re.split(r'\n\s*\n', body) if p.strip()]
            word_count = len(_re.sub(r'\s', '', body))
            cites = 0
            for pat in (r'\[(\d+(?:[,\-]\d+)*)\]',
                        r'\([^)]+?,\s*(?:19|20)\d{2}[a-z]?\)',
                        r'（[^）]+?，\s*(?:19|20)\d{2}[a-z]?）'):
                cites += len(_re.findall(pat, body))
            stats[name] = {
                "word_count": word_count,
                "paragraph_count": len(paras),
                "citation_count": cites,
            }
        return stats

    @staticmethod
    def format_review_for_polish(review: dict) -> str:
        """将评价结果格式化为润色用的指导文字。

        优先使用用户在 ReviewDialog 中采纳/编辑过的发现项（_accepted_items），
        未采纳项（_rejected_items）不会进入润色指令。旧格式评价（无
        _accepted_items）时回退到原始字段重建。

        Args:
            review: 完整的评价结果 dict（通常来自 load_review）

        Returns:
            格式化的指导文字，或空字符串（无发现问题时）
        """
        # ---- 新格式：用户采纳/编辑过的发现项 ----
        # 键存在但为空列表 = 用户在对话框中明确全部拒绝，不应注入任何
        # 问题；键不存在才是旧格式（回退到原始字段重建）。
        if "_accepted_items" in review:
            accepted = review.get("_accepted_items")
            if not (isinstance(accepted, list) and accepted):
                return ""
            lines: list[str] = []
            for item in accepted:
                if not isinstance(item, dict):
                    continue
                cat = str(item.get("category", "") or "")
                title = str(item.get("title", "") or "").strip()
                sug = str(item.get("suggestion", "") or "").strip()
                prefix = f"【{cat}】" if cat else ""
                if title and sug:
                    lines.append(f"· {prefix}{title} → 处理：{sug}")
                elif sug:
                    lines.append(f"· {prefix}处理：{sug}")
                elif title:
                    lines.append(f"· {prefix}{title}")
            if lines:
                return "【以下问题来自草稿整体评价（已按你的采纳选择过滤），请在润色时一并修正】\n" + "\n".join(lines)
            return ""

        # ---- 旧格式回退：原始字段重建 ----
        lines = []

        def _add(line: str) -> None:
            lines.append(f"· {line}")

        # 各部分分析中的具体问题
        for s in review.get("section_analysis") or []:
            if not isinstance(s, dict):
                continue
            section = s.get("section", "?")
            issues: list[str] = []
            if s.get("citation_status") and s["citation_status"] not in ("达标", "无基准"):
                issues.append(
                    f"引用数量{s['citation_status']}"
                    f"（当前{s.get('citation_count','?')}篇，基准{s.get('citation_benchmark','?')}篇）"
                )
            if s.get("word_count_status") and s["word_count_status"] not in ("达标", "无基准"):
                issues.append(
                    f"字数{s['word_count_status']}"
                    f"（当前约{s.get('word_count','?')}字，基准{s.get('word_count_benchmark','?')}字）"
                )
            if s.get("paragraph_count_status") and s["paragraph_count_status"] not in ("达标", "无基准"):
                issues.append(
                    f"段数{s['paragraph_count_status']}"
                    f"（当前{s.get('paragraph_count','?')}段，基准{s.get('paragraph_benchmark','?')}段）"
                )
            if not s.get("has_summary"):
                issues.append("缺少章节小结")
            if s.get("paragraph_size_issue"):
                issues.append(s["paragraph_size_issue"])
            if s.get("citation_detail_issue"):
                issues.append(s["citation_detail_issue"])
            for oi in s.get("other_issues", []):
                issues.append(str(oi))
            if issues:
                _add(f"【{section}】" + "；".join(issues))

        # 过渡缺失
        tsg = review.get("transition_summary_gaps") or {}
        for g in tsg.get("gaps") or []:
            if not isinstance(g, dict):
                continue
            if g.get("severity") in ("缺失", "偏弱"):
                _add(f"【过渡】{g.get('between','?')}：{g.get('suggestion','')}")
        for ms in tsg.get("missing_summaries") or []:
            _add(f"【小结】{ms}末尾需要添加小结段落")

        # 冗余
        for r in (review.get("redundancy") or {}).get("items") or []:
            if not isinstance(r, dict):
                continue
            locs_str = "、".join(str(x) for x in r.get("locations", []))
            _add(f"【冗余】{r.get('point','?')}（位置：{locs_str}）")

        # 图表建议
        for f in (review.get("figure_suggestions") or {}).get("items") or []:
            if not isinstance(f, dict):
                continue
            _add(f"【图表】{f.get('location','?')} — {f.get('type','?')}：{f.get('purpose','?')}")

        # 术语一致性
        for t in (review.get("terminology_consistency") or {}).get("issues") or []:
            if not isinstance(t, dict):
                continue
            variants = " / ".join(str(x) for x in t.get("variants", []))
            _add(f"【术语】{t.get('concept','?')}：{variants} → 统一为「{t.get('suggestion','?')}」")

        if not lines:
            return ""

        return "【以下问题来自草稿整体评价，请在润色时一并修正】\n" + "\n".join(lines)
