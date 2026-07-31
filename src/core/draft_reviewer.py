"""草稿整体评价器 —— 对完整草稿做结构性诊断，对比知识库基准。"""

from __future__ import annotations

import json as _json
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_client import LLMClient
    from .writing_coach import WritingCoach


DRAFT_REVIEW_PROMPT = """你是学术写作审稿专家。请对以下草稿进行全面结构性评价，对照知识库基准给出具体诊断。

【知识库基准】（从你的历史论文中提取，作为评判参考）
{kb_benchmarks}

【草稿全文】
{draft_text}

## 评价维度

请从以下维度逐一分析，返回严格 JSON（不要 Markdown 标记）：

### 1. 各部分分析 (section_analysis)
识别草稿中的章节结构（Introduction/Methods/Results/Discussion/Conclusion 等，根据草稿实际内容判断边界）。对每个章节：
- section: 章节名
- word_count: 估计字数
- word_count_benchmark: 知识库中该章节的字数基准（若知识库无此章数据填 -1）
- word_count_status: "达标" / "偏少" / "偏多" / "无基准"
- paragraph_count: 段数
- paragraph_benchmark: 知识库中该章节的段数基准（无数据填 -1）
- paragraph_count_status: "达标" / "偏多" / "偏少" / "无基准"
- citation_count: 引用数
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

        prompt_text = DRAFT_REVIEW_PROMPT.replace("{kb_benchmarks}", kb_benchmarks).replace(
            "{draft_text}", draft_text
        )

        messages = [
            {
                "role": "system",
                "content": "你是学术写作审稿专家。只返回 JSON，不要加解释。你的评价应具体、有建设性、可直接指导修改。",
            },
            {"role": "user", "content": prompt_text},
        ]

        try:
            response = write_client.chat_sync(messages, timeout=1800.0)
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
        """解析 LLM 返回的 JSON（五层容错 + 兜底降级）。"""
        if not raw or not raw.strip():
            return {"error": "LLM 返回为空"}

        text = raw.strip()

        # 尝试 1: 直接解析
        try:
            return _json.loads(text)
        except (_json.JSONDecodeError, TypeError):
            pass

        # 尝试 2: 提取 ```json ... ```
        for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    return _json.loads(m.group(1).strip())
                except (_json.JSONDecodeError, TypeError):
                    pass

        # 尝试 3: 提取 { ... }
        first = text.find('{')
        last = text.rfind('}')
        if first >= 0 and last > first:
            json_str = text[first : last + 1]
            try:
                return _json.loads(json_str)
            except (_json.JSONDecodeError, TypeError):
                pass
            # 尝试 3b: 清洗未转义换行符
            try:
                cleaned = re.sub(r'(?<!\\)"\s*\n\s*', r'\\n', json_str)
                cleaned = re.sub(r'(?<!\\)\n\s*"', r'\\n"', cleaned)
                result = _json.loads(cleaned)
                if result is not None:
                    return result
            except Exception:
                pass

        # 尝试 4: 中文全角花括号
        try:
            alt = text.replace('\uff5b', '{').replace('\uff5d', '}')
            first_a = alt.find('{')
            last_a = alt.rfind('}')
            if first_a >= 0 and last_a > first_a:
                return _json.loads(alt[first_a : last_a + 1])
        except Exception:
            pass

        return {"error": f"JSON 解析失败，LLM 原始返回前 200 字符：{text[:200]}"}

    @staticmethod
    def format_review_for_polish(review: dict) -> str:
        """将评价结果格式化为润色用的指导文字。

        提取所有诊断中发现的问题，转化为润色 LLM 可以直接处理的指令。

        Args:
            review: 完整的评价结果 dict（通常来自 load_review）

        Returns:
            格式化的指导文字，或空字符串（无发现问题时）
        """
        lines: list[str] = []

        def _add(line: str) -> None:
            lines.append(f"· {line}")

        # 各部分分析中的具体问题
        for s in review.get("section_analysis", []):
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
        tsg = review.get("transition_summary_gaps", {})
        for g in tsg.get("gaps", []):
            if g.get("severity") in ("缺失", "偏弱"):
                _add(f"【过渡】{g.get('between','?')}：{g.get('suggestion','')}")
        for ms in tsg.get("missing_summaries", []):
            _add(f"【小结】{ms}末尾需要添加小结段落")

        # 冗余
        for r in review.get("redundancy", {}).get("items", []):
            locs_str = "、".join(str(x) for x in r.get("locations", []))
            _add(f"【冗余】{r.get('point','?')}（位置：{locs_str}）")

        # 图表建议
        for f in review.get("figure_suggestions", {}).get("items", []):
            _add(f"【图表】{f.get('location','?')} — {f.get('type','?')}：{f.get('purpose','?')}")

        # 术语一致性
        for t in review.get("terminology_consistency", {}).get("issues", []):
            variants = " / ".join(str(x) for x in t.get("variants", []))
            _add(f"【术语】{t.get('concept','?')}：{variants} → 统一为「{t.get('suggestion','?')}」")

        if not lines:
            return ""

        return "【以下问题来自草稿整体评价，请在润色时一并修正】\n" + "\n".join(lines)
