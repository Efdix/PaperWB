"""写作教练 —— 写作辅助的主引擎，管理知识库、分析风格、驱动润色/改写/文献推荐。

Phase 1: 知识库 CRUD — 创建 profile，添加写作范文（提取写作习惯）和期刊范文（提取期刊格式）
Phase 2: 风格分析（双轨并行）
  - writing_habits ← 写作范文: 用词偏好 / 句式模板 / 段落大小 / 引用详略度 / 论述逻辑
  - journal_style  ← 期刊范文: 引用格式 / 章节结构 / 图表惯例 / 摘要格式
Phase 3: AI 辅助写作 — 润色选中文字、基于引文改写、Semantic Scholar 遗漏文献检测
"""

from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .llm_client import LLMClient


# ============================================================
# 数据结构
# ============================================================

@dataclass
class WritingProfile:
    """写作知识库配置。

    写作范文 → 分析 writing_habits（用词/句式/段落/引用详略度）
    期刊范文 → 分析 journal_style（引用格式/章节结构/图表惯例）
    """

    name: str = ""                         # 知识库名称
    writing_type: str = "综述"              # 写作类型 key
    created_at: str = ""
    updated_at: str = ""
    personal_papers: list[dict] = field(default_factory=list)    # [{filename, original_path, text}]
    journal_papers: list[dict] = field(default_factory=list)     # 同上
    writing_habits: dict | None = None     # 个人论文分析结果（用词/句式/段落/引用详略度/论述逻辑）
    journal_style: dict | None = None      # 期刊范文分析结果（引用格式/章节结构/图表惯例）
    # 兼容旧格式（v1 的 style_guide 不再写入，读取时自动迁移）
    _legacy_style_guide: dict | None = field(default=None, repr=False)

    @property
    def personal_count(self) -> int:
        return len(self.personal_papers)

    @property
    def journal_count(self) -> int:
        return len(self.journal_papers)

    @property
    def total_papers(self) -> int:
        return self.personal_count + self.journal_count

    @property
    def has_style_guide(self) -> bool:
        """是否有任何风格指南（兼容旧 code）。"""
        return (self.writing_habits is not None and bool(self.writing_habits)) or \
               (self.journal_style is not None and bool(self.journal_style)) or \
               (self._legacy_style_guide is not None and bool(self._legacy_style_guide))

    @property
    def has_writing_habits(self) -> bool:
        return self.writing_habits is not None and bool(self.writing_habits)

    @property
    def has_journal_style(self) -> bool:
        return self.journal_style is not None and bool(self.journal_style)

    def to_dict(self) -> dict:
        d: dict = {
            "name": self.name,
            "writing_type": self.writing_type,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "personal_papers": [
                {"filename": p["filename"], "original_path": p.get("original_path", ""), "text": p["text"]}
                for p in self.personal_papers
            ],
            "journal_papers": [
                {"filename": p["filename"], "original_path": p.get("original_path", ""), "text": p["text"]}
                for p in self.journal_papers
            ],
            "writing_habits": self.writing_habits,
            "journal_style": self.journal_style,
        }
        # 兼容旧版本读取：如果存在 legacy 数据，也序列化（但不鼓励）
        if self._legacy_style_guide:
            d["style_guide"] = self._legacy_style_guide
        return d

    @staticmethod
    def from_dict(d: dict) -> "WritingProfile":
        writing_habits = d.get("writing_habits")
        journal_style = d.get("journal_style")
        legacy_guide = d.get("style_guide")  # 旧格式

        # 兼容迁移：旧 style_guide → 拆分为 journal_style（格式部分）+ 清空 writing_habits
        if legacy_guide and not journal_style:
            journal_style = legacy_guide
        if legacy_guide and not writing_habits:
            # 旧 style_guide 可能是混合数据，不自动迁移到 writing_habits
            writing_habits = None

        return WritingProfile(
            name=d.get("name", ""),
            writing_type=d.get("writing_type", "综述"),
            created_at=d.get("created_at", ""),
            updated_at=d.get("updated_at", ""),
            personal_papers=d.get("personal_papers", []),
            journal_papers=d.get("journal_papers", []),
            writing_habits=writing_habits,
            journal_style=journal_style,
            _legacy_style_guide=legacy_guide if not journal_style and legacy_guide else None,
        )


# ============================================================
# 写作教练
# ============================================================

class WritingCoach:
    """写作教练 —— 管理知识库、生成风格指南、辅助写作。"""

    def __init__(self) -> None:
        self._kb_dir = self._resolve_kb_dir()
        self._current_profile: WritingProfile | None = None
        self._profiles: dict[str, WritingProfile] = {}
        self._load_profiles()
        self._restore_last_profile()

    # ---- 路径 ----

    @staticmethod
    def _resolve_kb_dir() -> Path:
        from ..utils.config import get_writing_kb_dir
        return get_writing_kb_dir()

    def _profile_dir(self, name: str) -> Path:
        return self._kb_dir / name

    def _profile_config_path(self, name: str) -> Path:
        return self._profile_dir(name) / "config.json"

    def _papers_dir(self, name: str, paper_type: str) -> Path:
        """paper_type: 'personal' | 'journal'"""
        d = self._profile_dir(name) / f"{paper_type}_papers"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _last_profile_path(self) -> Path:
        return self._kb_dir / "_last_profile.txt"

    def _restore_last_profile(self) -> None:
        """恢复上次关闭时使用的知识库。"""
        lp = self._last_profile_path()
        if lp.exists():
            name = lp.read_text(encoding="utf-8").strip()
            if name and name in self._profiles:
                self._current_profile = self._profiles[name]

    def _save_last_profile(self) -> None:
        """保存当前知识库名，供下次启动恢复。"""
        lp = self._last_profile_path()
        name = self._current_profile.name if self._current_profile else ""
        lp.write_text(name, encoding="utf-8")

    # ---- 加载 ----

    def _load_profiles(self) -> None:
        """扫描 knowledge base 目录，加载所有 profile。"""
        self._profiles.clear()
        if not self._kb_dir.exists():
            return
        for entry in self._kb_dir.iterdir():
            if entry.is_dir():
                cfg_path = entry / "config.json"
                if cfg_path.exists():
                    try:
                        data = json.loads(cfg_path.read_text(encoding="utf-8"))
                        profile = WritingProfile.from_dict(data)
                        self._profiles[profile.name] = profile
                    except (json.JSONDecodeError, OSError):
                        pass

    def _save_profile(self, profile: WritingProfile) -> None:
        """保存 profile 配置到磁盘。"""
        d = self._profile_dir(profile.name)
        d.mkdir(parents=True, exist_ok=True)
        cfg_path = d / "config.json"
        cfg_path.write_text(
            json.dumps(profile.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    # ---- 公共 API: 知识库管理 ----

    @property
    def profile_names(self) -> list[str]:
        return sorted(self._profiles.keys())

    @property
    def current_profile(self) -> WritingProfile | None:
        return self._current_profile

    def create_profile(self, name: str, writing_type: str = "综述") -> WritingProfile:
        """创建新的写作知识库。"""
        from datetime import datetime

        if name in self._profiles:
            raise ValueError(f"知识库 '{name}' 已存在")

        now = datetime.now().isoformat()
        profile = WritingProfile(
            name=name,
            writing_type=writing_type,
            created_at=now,
            updated_at=now,
        )
        self._profiles[name] = profile
        self._save_profile(profile)
        self._current_profile = profile
        self._save_last_profile()
        return profile

    def switch_profile(self, name: str) -> WritingProfile:
        """切换到指定知识库。"""
        if name not in self._profiles:
            raise ValueError(f"知识库 '{name}' 不存在")
        self._current_profile = self._profiles[name]
        self._save_last_profile()
        return self._current_profile

    def delete_profile(self, name: str) -> None:
        """删除知识库及其所有数据。"""
        if name not in self._profiles:
            return
        d = self._profile_dir(name)
        if d.exists():
            shutil.rmtree(str(d))
        self._profiles.pop(name, None)
        if self._current_profile and self._current_profile.name == name:
            self._current_profile = None
        self._save_last_profile()

    # ---- 公共 API: 论文管理 ----

    def add_sample_paper(self, pdf_path: str) -> dict | None:
        """添加一篇写作范文到当前知识库。

        Returns:
            {"filename": str, "text": str} 或 None（失败时）
        """
        return self._add_paper(pdf_path, "personal")

    def add_journal_paper(self, pdf_path: str) -> dict | None:
        """添加一篇目标期刊范文到当前知识库。"""
        return self._add_paper(pdf_path, "journal")

    def remove_sample_paper(self, filename: str) -> None:
        """移除一篇写作范文。"""
        self._remove_paper(filename, "personal")

    def remove_journal_paper(self, filename: str) -> None:
        """移除一篇期刊范文。"""
        self._remove_paper(filename, "journal")

    def clear_sample_papers(self) -> None:
        """清空所有写作范文。"""
        if self._current_profile:
            self._current_profile.personal_papers.clear()
            self._save_profile(self._current_profile)

    def clear_journal_papers(self) -> None:
        """清空所有期刊范文。"""
        if self._current_profile:
            self._current_profile.journal_papers.clear()
            self._save_profile(self._current_profile)

    # ---- 内部: 论文添加/移除 ----

    def _add_paper(self, pdf_path: str, paper_type: str) -> dict | None:
        """添加论文到知识库（通用）。"""
        if not self._current_profile:
            return None
        if not os.path.isfile(pdf_path):
            return None

        import fitz

        try:
            doc = fitz.open(pdf_path)
            text_parts = []
            for page in doc:
                t = page.get_text()
                if t.strip():
                    text_parts.append(t.strip())
            doc.close()
            full_text = "\n\n".join(text_parts)
        except Exception:
            return None

        if not full_text.strip():
            return None

        filename = os.path.basename(pdf_path)
        # 存文本到 papers 目录（供检索）
        papers_dir = self._papers_dir(self._current_profile.name, paper_type)
        txt_path = papers_dir / (filename + ".txt")
        txt_path.write_text(full_text, encoding="utf-8")

        paper_entry = {
            "filename": filename,
            "original_path": pdf_path,
            "text": full_text,
        }

        target_list = (
            self._current_profile.personal_papers if paper_type == "personal"
            else self._current_profile.journal_papers
        )
        # 去重（同文件名替换）
        target_list[:] = [p for p in target_list if p["filename"] != filename]
        target_list.append(paper_entry)

        from datetime import datetime
        self._current_profile.updated_at = datetime.now().isoformat()
        self._save_profile(self._current_profile)

        return paper_entry

    def _remove_paper(self, filename: str, paper_type: str) -> None:
        if not self._current_profile:
            return
        target_list = (
            self._current_profile.personal_papers if paper_type == "personal"
            else self._current_profile.journal_papers
        )
        target_list[:] = [p for p in target_list if p["filename"] != filename]

        # 删除文本文件
        papers_dir = self._papers_dir(self._current_profile.name, paper_type)
        txt_path = papers_dir / (filename + ".txt")
        if txt_path.exists():
            txt_path.unlink()

        from datetime import datetime
        self._current_profile.updated_at = datetime.now().isoformat()
        self._save_profile(self._current_profile)

    # ---- 公共 API: 获取拼接文本 ----

    def get_sample_paper_texts(self) -> str:
        """获取当前知识库中写作范文的拼接文本（供写作习惯分析用）。"""
        if not self._current_profile:
            return ""
        parts = []
        for p in self._current_profile.personal_papers:
            parts.append(f"--- {p['filename']} ---\n{p.get('text', '')}")
        return "\n\n".join(parts)

    def get_journal_paper_texts(self) -> str:
        """获取当前知识库中期刊范文的拼接文本（供期刊格式分析用）。"""
        if not self._current_profile:
            return ""
        parts = []
        for p in self._current_profile.journal_papers:
            parts.append(f"--- {p['filename']} ---\n{p.get('text', '')}")
        return "\n\n".join(parts)

    def get_all_paper_texts(self) -> str:
        """获取当前知识库中所有论文的拼接文本（兼容旧代码）。"""
        if not self._current_profile:
            return ""
        parts = []
        for p in self._current_profile.personal_papers:
            parts.append(f"--- 写作范文: {p['filename']} ---\n{p.get('text', '')}")
        for p in self._current_profile.journal_papers:
            parts.append(f"--- 期刊范文: {p['filename']} ---\n{p.get('text', '')}")
        return "\n\n".join(parts)

    def build_review_benchmarks(self) -> str:
        """构建草稿评价用的知识库基准字符串（供 DraftReviewer 使用）。

        提取当前 profile 中所有可用的基准数据，格式化为结构化文字，
        让 LLM 能够对照草稿进行量化/定性评判。

        Returns:
            格式化的基准字符串，或 "（未配置知识库或知识库无基准数据）"
        """
        if not self._current_profile:
            return "（未配置知识库）"

        profile = self._current_profile
        if not profile.has_writing_habits and not profile.has_journal_style:
            return "（知识库尚未生成风格指南，无基准数据）"

        sections: list[str] = []

        # ---- 写作习惯基准 ----
        if profile.has_writing_habits:
            habits = profile.writing_habits

            # 引用密度（各章节引用数分布）
            cit_den = habits.get("citation_density", {})
            if cit_den:
                lines = [f"整体描述：{cit_den.get('summary', '无')}"]
                for s in cit_den.get("sections", []):
                    lines.append(f"  {s.get('name', '?')}：{s.get('citation_count', '?')} 篇")
                sections.append("【引用密度基准（各章节应引用文献数）】\n" + "\n".join(lines))

            # 引用详略度
            cd = habits.get("citation_detail_level", {})
            if cd and cd.get("sample_count", 0) > 0:
                sections.append(
                    f"【引用详略度基准】\n"
                    f"每引用平均 {cd.get('avg_sentences_per_citation', '?')} 句话、"
                    f"{cd.get('avg_chars_per_citation', '?')} 字。"
                    f"四分位范围：{cd.get('q25_chars', '?')}-{cd.get('q75_chars', '?')} 字。"
                    f"分布：{cd.get('distribution_description', '?')}"
                )

            # 段落组织
            if habits.get("paragraph_patterns"):
                sections.append(f"【段落组织习惯】\n{habits['paragraph_patterns']}")

            # 过渡方式
            if habits.get("transition_phrases"):
                sections.append(f"【过渡方式习惯】\n{habits['transition_phrases']}")

            # 论述逻辑
            if habits.get("argumentation_style"):
                sections.append(f"【论述逻辑习惯】\n{habits['argumentation_style']}")

            # 术语偏好
            if habits.get("terminology_preferences"):
                sections.append(f"【术语偏好】\n{habits['terminology_preferences']}")

            # 句式模板（精简）
            st = habits.get("sentence_templates")
            if st:
                if isinstance(st, list):
                    sections.append("【常用句式模板】\n" + "\n".join(f"· {s}" for s in st[:6]))
                else:
                    sections.append(f"【常用句式模板】\n{st}")

            # 语气风格
            if habits.get("tone_voice"):
                sections.append(f"【语气风格】\n{habits['tone_voice']}")

            # 章节段落组织（新增）
            sp = habits.get("section_paragraphs")
            if sp and isinstance(sp, list):
                lines = []
                for s in sp:
                    lines.append(
                        f"  {s.get('section', '?')}: {s.get('paragraph_count', '?')} 段, "
                        f"每段平均 {s.get('avg_words_per_paragraph', '?')} 字"
                    )
                    if s.get("notes"):
                        lines[-1] += f" ({s['notes']})"
                sections.append("【各章节段落组织基准】\n" + "\n".join(lines))

            # 章节过渡模式（新增）
            st_habits = habits.get("section_transitions")
            if st_habits:
                lines = [f"密度: {st_habits.get('density', '无')}"]
                pats = st_habits.get("patterns", [])
                if pats:
                    lines.append("典型模式: " + "; ".join(str(p) for p in pats))
                wb = st_habits.get("weak_boundaries", [])
                if wb:
                    lines.append("薄弱边界: " + "; ".join(str(w) for w in wb))
                sections.append("【章节过渡模式基准】\n" + "\n".join(lines))

            # 各部分字数分布（新增）
            sw = habits.get("section_word_counts")
            if sw and isinstance(sw, list):
                lines = []
                for s in sw:
                    lines.append(
                        f"  {s.get('section', '?')}: 约 {s.get('word_count', '?')} 字 "
                        f"({s.get('percentage', '?')})"
                    )
                sections.append("【各部分字数基准】\n" + "\n".join(lines))

        # ---- 期刊格式基准 ----
        if profile.has_journal_style:
            js = profile.journal_style

            if js.get("section_structure"):
                sections.append(
                    f"【期刊要求的章节结构（草稿应包含这些部分）】\n{js['section_structure']}"
                )

            if js.get("figure_conventions"):
                sections.append(
                    f"【期刊图表惯例（可据此建议图表位置和格式）】\n{js['figure_conventions']}"
                )

            if js.get("citation_format"):
                sections.append(f"【期刊引用格式要求】\n{js['citation_format']}")

        if not sections:
            return "（知识库已生成但无可用的基准数据字段）"

        return "\n\n".join(sections)

    def build_polish_system_prompt(self, writing_type: str = "综述") -> str:
        """构建精简版润色 system prompt —— 仅含润色和核查需要的字段，不含期刊格式。

        用于 UnifiedWriter 润色+核查流程，避免无关的期刊格式信息干扰 LLM。
        """
        from .writing_prompts import get_writing_type_config

        cfg = get_writing_type_config(writing_type)
        prompt = cfg["system_prompt"]

        if not self._current_profile:
            return prompt

        parts = []

        if self._current_profile.has_writing_habits:
            habits = self._current_profile.writing_habits
            if habits.get("sentence_templates"):
                st = habits["sentence_templates"]
                if isinstance(st, list):
                    parts.append("【句式模板】\n" + "\n".join(f"· {s}" for s in st))
                else:
                    parts.append(f"【句式模板】\n{st}")
            if habits.get("terminology_preferences"):
                parts.append(f"【术语偏好】\n{habits['terminology_preferences']}")
            if habits.get("transition_phrases"):
                parts.append(f"【过渡方式】\n{habits['transition_phrases']}")
            if habits.get("tone_voice"):
                parts.append(f"【语气风格】\n{habits['tone_voice']}")
            cd = habits.get("citation_detail_level", {})
            if cd and cd.get("sample_count", 0) > 0:
                parts.append(
                    f"【引用详略度约束（极其重要）】\n"
                    f"你的参考综述在描述每篇引用文献时，平均使用 {cd.get('avg_sentences_per_citation', '?')} 句话、"
                    f"约 {cd.get('avg_chars_per_citation', '?')} 字。"
                    f"分布范围：{cd.get('distribution_description', '?')}。\n"
                    f"请严格按照这个详略尺度来写，不要对某篇文献进行过度详细的描述。"
                )
            # 引用密度（每个章节的引用数分布）
            cit_den = habits.get("citation_density", {})
            if cit_den:
                parts.append(
                    f"【引用密度参考】\n"
                    f"{cit_den.get('summary', '')}\n"
                    + "\n".join(
                        f"  {s.get('name', '?')}: {s.get('citation_count', '?')} 篇"
                        for s in cit_den.get("sections", [])
                    )
                    + "\n请在润色时参考此引用密度，如果某部分引用过少或过多，给出建议。"
                )
            # 章节段落组织（新增）
            sp = habits.get("section_paragraphs")
            if sp and isinstance(sp, list):
                lines = []
                for s in sp:
                    lines.append(
                        f"  {s.get('section', '?')}: {s.get('paragraph_count', '?')} 段, "
                        f"每段平均 {s.get('avg_words_per_paragraph', '?')} 字"
                    )
                parts.append("【各章节段落组织参考】\n" + "\n".join(lines))
            # 各部分字数分布（新增）
            sw = habits.get("section_word_counts")
            if sw and isinstance(sw, list):
                lines = []
                for s in sw:
                    lines.append(
                        f"  {s.get('section', '?')}: 约 {s.get('word_count', '?')} 字 "
                        f"({s.get('percentage', '?')})"
                    )
                parts.append("【各部分字数参考】\n" + "\n".join(lines) + "\n请在润色时参考此字数分布，保持各部分的篇幅比例一致。")

        if parts:
            prompt += "\n\n---\n以下是根据你的历史论文分析出的写作习惯（仅描述风格，不限制学术主题），请在写作中保持一致的风格：\n\n" + "\n\n".join(parts)

        # 小结与过渡指导
        prompt += (
            "\n\n---\n以下是小结与过渡策略指导：\n"
            "请根据上述段落组织习惯和过渡方式，在写作中主动判断并添加：\n"
            "1. 如果段落间的逻辑跨度较大，添加过渡句连接前后内容\n"
            "2. 如果章节或大段落末尾缺少总结，添加1-2句小结段落来概括要点\n"
            "3. 新增的过渡和小结应与原文风格一致，使用上述句式模板中的表达方式\n"
        )

        return prompt

    def build_writing_system_prompt(self, writing_type: str = "综述") -> str:
        """构建完整的写作 system prompt（原则 + 写作习惯 + 引用详略度 + 期刊格式）。"""
        from .writing_prompts import get_writing_type_config

        cfg = get_writing_type_config(writing_type)
        prompt = cfg["system_prompt"]
        has_extra = False

        if not self._current_profile:
            return prompt

        # 写作习惯（来自写作范文分析）
        if self._current_profile.has_writing_habits:
            habits = self._current_profile.writing_habits
            parts = []
            if habits.get("terminology_preferences"):
                parts.append(f"【术语偏好】\n{habits['terminology_preferences']}")
            if habits.get("sentence_templates"):
                st = habits["sentence_templates"]
                if isinstance(st, list):
                    parts.append("【句式模板】\n" + "\n".join(f"· {s}" for s in st))
                else:
                    parts.append(f"【句式模板】\n{st}")
            if habits.get("paragraph_patterns"):
                parts.append(f"【段落组织】\n{habits['paragraph_patterns']}")
            if habits.get("transition_phrases"):
                parts.append(f"【过渡方式】\n{habits['transition_phrases']}")
            if habits.get("argumentation_style"):
                parts.append(f"【论述逻辑】\n{habits['argumentation_style']}")
            if habits.get("tone_voice"):
                parts.append(f"【语气风格】\n{habits['tone_voice']}")
            # 引用详略度约束（计算性指标，非 LLM 生成）
            cd = habits.get("citation_detail_level", {})
            if cd and cd.get("sample_count", 0) > 0:
                parts.append(
                    f"【引用详略度约束（极其重要）】\n"
                    f"你的参考综述在描述每篇引用文献时，平均使用 {cd.get('avg_sentences_per_citation', '?')} 句话、"
                    f"约 {cd.get('avg_chars_per_citation', '?')} 字。"
                    f"分布范围：{cd.get('distribution_description', '?')}。\n"
                    f"请严格按照这个详略尺度来写，不要对某篇文献进行过度详细的描述。"
                    f"综述是概括性的，应在有限的字数内向读者传达关键发现即可。"
                )
            if parts:
                prompt += "\n\n---\n以下是根据你的历史论文分析出的写作习惯（仅描述风格，不限制学术主题），请在写作中保持一致的风格：\n\n" + "\n\n".join(parts)
                has_extra = True

        # 期刊格式（来自期刊范文分析）
        if self._current_profile.has_journal_style:
            js = self._current_profile.journal_style
            parts = []
            if js.get("citation_format"):
                parts.append(f"【引用格式】\n{js['citation_format']}")
            if js.get("reference_list_format"):
                parts.append(f"【参考文献列表格式】\n{js['reference_list_format']}")
            if js.get("section_structure"):
                parts.append(f"【章节结构】\n{js['section_structure']}")
            if js.get("figure_conventions"):
                parts.append(f"【图表惯例】\n{js['figure_conventions']}")
            if js.get("abstract_format"):
                parts.append(f"【摘要格式】\n{js['abstract_format']}")
            if js.get("general_formatting"):
                parts.append(f"【其他格式】\n{js['general_formatting']}")
            if parts:
                section_title = "" if has_extra else "---\n以下是你需要遵循的期刊格式规范：\n\n"
                prompt += f"\n\n{section_title}" + "\n\n".join(parts)

        # 兼容旧格式 style_guide（fallback）
        if not has_extra and self._current_profile._legacy_style_guide:
            guide = self._current_profile._legacy_style_guide
            guide_text = self._format_style_guide(guide)
            if guide_text:
                prompt += "\n\n---\n以下是你需要遵循的具体格式和风格规范（基于真实论文分析）：\n\n" + guide_text

        return prompt

    @staticmethod
    def _format_style_guide(guide: dict) -> str:
        """兼容旧格式：将风格指南 dict 格式化为 prompt 可用的文字。"""
        parts = []
        if guide.get("citation_style"):
            parts.append(f"【引用格式】\n{guide['citation_style']}")
        if guide.get("structure_template"):
            parts.append(f"【结构模板】\n{guide['structure_template']}")
        if guide.get("terminology_preferences"):
            parts.append(f"【术语偏好】\n{guide['terminology_preferences']}")
        if guide.get("sentence_templates"):
            if isinstance(guide["sentence_templates"], list):
                parts.append("【句式模板】\n" + "\n".join(f"· {s}" for s in guide["sentence_templates"]))
            else:
                parts.append(f"【句式模板】\n{guide['sentence_templates']}")
        if guide.get("general_notes"):
            parts.append(f"【其他注意事项】\n{guide['general_notes']}")
        return "\n\n".join(parts)

    # ---- Phase 2: 引用详略度计算（纯统计，不用 LLM） ----

    @staticmethod
    def _analyze_citation_detail(profile: "WritingProfile") -> dict | None:
        """从写作范文中计算平均每引用字数/句数/分布（纯统计指标）。

        提取所有引文标记 [n] 或 [n,m]，取标记前后各 2 句作为引用上下文，
        统计字符数和句子数，输出均值、中位数、四分位数、分布描述。

        Returns:
            {
                "avg_chars_per_citation": int, "med_chars_per_citation": int,
                "avg_sentences_per_citation": float,
                "q25_chars": int, "q75_chars": int,
                "distribution_description": str, "sample_count": int
            }
        """
        import re

        all_texts = []
        for paper in profile.personal_papers:
            all_texts.append(paper.get("text", ""))
        if not all_texts:
            return None

        joined = "\n\n".join(all_texts)

        # 提取所有引文标记及其位置（支持 [1]、[2,3]、[4-7]、[4,5,6]）
        cite_pattern = re.compile(r'\[(\d+(?:[,\-]\d+)*)\]')
        markers = list(cite_pattern.finditer(joined))
        if not markers:
            return None

        # 句子切分：以句号、问号、感叹号后跟空白或行尾为界
        sentences = re.split(r'(?<=[。！？.!?])\s*', joined)
        # 重新定位每个句子的起止位置
        sent_spans: list[tuple[int, int]] = []
        pos = 0
        for s in sentences:
            if s:
                start = joined.index(s, pos) if s in joined[pos:] else pos
                end = start + len(s)
                sent_spans.append((start, end))
                pos = end

        # 对每个引文标记，找到包含它的句子，然后取前后各 2 句作为上下文
        char_counts: list[int] = []
        sent_counts: list[int] = []

        for m in markers:
            m_start = m.start()
            # 找到包含此标记的句子索引
            sent_idx = -1
            for si, (ss, se) in enumerate(sent_spans):
                if ss <= m_start < se:
                    sent_idx = si
                    break
            if sent_idx < 0:
                continue

            # 取前后各 2 句（但不超过前后引文标记边界或文章边界）
            ctx_start_idx = max(0, sent_idx - 2)
            ctx_end_idx = min(len(sent_spans), sent_idx + 3)  # +3 = 当前句 + 后面2句

            # 检查上下文是否被其他引文标记污染：如果前/后2句范围内有另一个引文标记，则缩小到那个标记
            for si in range(sent_idx - 1, max(0, sent_idx - 3), -1):
                if si < 0:
                    break
                ss, se = sent_spans[si]
                other = cite_pattern.search(joined, ss, se)
                if other:
                    ctx_start_idx = max(ctx_start_idx, si + 1)
                    break

            for si in range(sent_idx + 1, min(len(sent_spans), sent_idx + 3)):
                if si >= len(sent_spans):
                    break
                ss, se = sent_spans[si]
                other = cite_pattern.search(joined, ss, se)
                if other:
                    ctx_end_idx = min(ctx_end_idx, si)
                    break

            ctx_text = "".join(
                joined[ss:se]
                for ss, se in sent_spans[ctx_start_idx:ctx_end_idx]
            )
            if ctx_text.strip():
                char_counts.append(len(ctx_text))
                ctx_sents = [s for s in sent_spans[ctx_start_idx:ctx_end_idx]]
                sent_counts.append(len(ctx_sents))

        if not char_counts:
            return None

        sorted_chars = sorted(char_counts)
        n = len(sorted_chars)
        avg_chars = round(sum(char_counts) / n)
        med_chars = sorted_chars[n // 2]
        q25_chars = sorted_chars[n // 4]
        q75_chars = sorted_chars[3 * n // 4]
        avg_sents = round(sum(sent_counts) / n, 1)

        # 生成分布描述
        if avg_sents <= 2:
            dist_desc = f"典型引用用 1-2 句话、{q25_chars}-{q75_chars} 字概括，平均 {avg_sents} 句 {avg_chars} 字"
        elif avg_sents <= 4:
            dist_desc = f"典型引用用 {max(1, avg_sents - 1).__round__()}-{avg_sents.__round__() + 1} 句话、{q25_chars}-{q75_chars} 字，平均 {avg_sents} 句 {avg_chars} 字"
        else:
            dist_desc = f"典型引用用 {avg_sents.__round__() - 1}-{avg_sents.__round__() + 1} 句话、{q25_chars}-{q75_chars} 字，平均 {avg_sents} 句 {avg_chars} 字"

        return {
            "avg_chars_per_citation": avg_chars,
            "med_chars_per_citation": med_chars,
            "avg_sentences_per_citation": avg_sents,
            "q25_chars": q25_chars,
            "q75_chars": q75_chars,
            "distribution_description": dist_desc,
            "sample_count": n,
        }

    # ---- Phase 2a: 写作习惯分析（基于写作范文，逐篇分析+合成） ----

    SINGLE_PAPER_HABITS_PROMPT = """你是一位学术写作风格分析专家。请分析以下这篇论文的写作习惯。

这是你自己撰写的论文，请从以下角度提取写作风格：

1. **术语偏好**: 高频术语、固定搭配、学科特有表达
2. **句式模板**: 摘录 5-8 个你常用的中文句式（如开头句、过渡句、总结句、引用句）
3. **段落大小**: 平均每段几句话？每段约多少字？段落是围绕单一主题还是多主题？
4. **过渡方式**: 段落间如何过渡？（如"此外…""相比之下…""更重要的是…""例如…"）
5. **论述逻辑**: 是归纳式（先罗列研究后总结）还是演绎式（先给观点再引用证据）？还是两者混合？
6. **语气风格**: 主动/被动语态偏好？第一人称使用频率？评价性语言的强弱？
7. **引用密度**: 分析各主要部分（根据论文自身内容判断章节边界）分别引用了多少文献。统计每部分的引用总数，并给出整体分布描述
8. **章节段落组织**: 每个主要章节各有几段？每段平均多少字？各章是否有孤句段或超长段？
9. **章节过渡模式**: 各章节之间是否有过渡段或过渡句？过渡密度如何？哪些章节边界缺少过渡？
10. **各部分字数分布**: 每个主要章节约多少字？占全文的大致百分比？

## 输出格式

请严格返回 JSON（不要加 Markdown 标记）：

{
  "terminology_preferences": "术语偏好描述",
  "sentence_templates": ["句式1", "句式2", "..."],
  "paragraph_patterns": "段落大小和组织方式描述",
  "transition_phrases": "段落间过渡方式描述",
  "argumentation_style": "论述逻辑描述（归纳/演绎/混合）",
  "tone_voice": "语气和语态描述",
  "citation_density": {"summary": "整体引用分布描述", "sections": [{"name": "部分名", "citation_count": 8}]},
  "section_paragraphs": [{"section": "Introduction", "paragraph_count": 3, "avg_words_per_paragraph": 150, "notes": ""}],
  "section_transitions": {"density": "过渡密度描述", "patterns": ["典型过渡方式"], "weak_boundaries": ["薄弱边界"]},
  "section_word_counts": [{"section": "Introduction", "word_count": 500, "percentage": "10%"}]
}

以下是论文文本：
{paper_text}"""

    SYNTHESIS_HABITS_PROMPT = """你是一位学术写作风格分析专家。以下是 {count} 篇论文的独立写作习惯分析结果。

请综合这 {count} 份分析，提炼出该作者的总体写作习惯。综合原则：
- 统计量（引用密度、段落数、字数分布）取各篇的平均值和范围
- 定性描述（句式模板、过渡方式、论述逻辑）取各篇的共性，标注个别差异
- 如果某篇数据明显异常（与其他论文差异过大），在输出中标注但不纳入均值

## 输出格式

请严格返回 JSON（不要加 Markdown 标记）：

{
  "terminology_preferences": "综合术语偏好描述",
  "sentence_templates": ["句式1", "..."],
  "paragraph_patterns": "综合段落大小和组织方式描述",
  "transition_phrases": "综合过渡方式描述",
  "argumentation_style": "综合论述逻辑描述",
  "tone_voice": "综合语气和语态描述",
  "citation_density": {"summary": "综合引用分布描述", "sections": [{"name": "部分名", "citation_count": 8}]},
  "section_paragraphs": [{"section": "Introduction", "paragraph_count": 3, "avg_words_per_paragraph": 150, "notes": ""}],
  "section_transitions": {"density": "综合过渡密度", "patterns": ["模式1"], "weak_boundaries": []},
  "section_word_counts": [{"section": "Introduction", "word_count": 500, "percentage": "10%"}]
}

以下为各篇论文的独立分析：
{analyses}"""

    def generate_writing_habits(self, parse_client: "LLMClient",
                                on_progress=None) -> dict | None:
        """逐篇分析写作范文 → 综合写作习惯（批量+合成模式）。

        每篇论文独立发给 LLM 分析，避免多篇全文挤在同一 prompt
        中导致的注意力稀释问题。最后用一次合成调用合并所有结果。
        """
        from collections.abc import Callable

        if not self._current_profile:
            return None
        papers = self._current_profile.personal_papers
        if not papers:
            return None

        def _emit(msg: str) -> None:
            if on_progress:
                on_progress(msg)

        # ---- 阶段 1: 逐篇分析 ----
        single_results: list[dict] = []
        for i, paper in enumerate(papers):
            filename = paper.get("filename", f"论文{i + 1}")
            text = paper.get("text", "")
            if not text.strip():
                _emit(f"跳过空文件: {filename}")
                continue

            _emit(f"正在分析第 {i + 1}/{len(papers)} 篇写作范文（{filename}）...")

            prompt = self.SINGLE_PAPER_HABITS_PROMPT.replace("{paper_text}", text)
            messages = [
                {"role": "system", "content": "你是学术写作风格分析专家。只返回 JSON，不要加解释。"},
                {"role": "user", "content": prompt},
            ]

            try:
                response = parse_client.chat_sync(messages, timeout=600.0)
                analysis = self._parse_style_guide_response(response)
                if analysis:
                    analysis["_source"] = filename
                    single_results.append(analysis)
                else:
                    _emit(f"  ⚠ {filename} 分析失败（LLM 返回为空或格式异常）")
            except Exception as e:
                _emit(f"  ⚠ {filename} 分析失败: {e}")

        if not single_results:
            # 全部失败，尝试纯统计
            detail = self._analyze_citation_detail(self._current_profile)
            if detail:
                minimal_habits = {"citation_detail_level": detail}
                self._current_profile.writing_habits = minimal_habits
                self._save_profile(self._current_profile)
                return minimal_habits
            return None

        # ---- 阶段 2: 综合所有分析结果 ----
        _emit(f"正在综合 {len(single_results)} 篇论文的写作习惯...")

        analyses_text = "\n\n---\n\n".join(
            f"【论文 {r.get('_source', '?')}】\n"
            + json.dumps({k: v for k, v in r.items() if k != "_source"}, ensure_ascii=False, indent=2)
            for r in single_results
        )

        import json as _json_local
        synth_prompt = self.SYNTHESIS_HABITS_PROMPT.replace(
            "{count}", str(len(single_results))
        ).replace("{analyses}", analyses_text)

        synth_messages = [
            {"role": "system", "content": "你是学术写作风格分析专家。只返回 JSON，不要加解释。"},
            {"role": "user", "content": synth_prompt},
        ]

        try:
            response = parse_client.chat_sync(synth_messages, timeout=300.0)
            habits = self._parse_style_guide_response(response)
            if habits:
                # 附加计算性引用详略度指标（基于所有论文全文的纯统计）
                habits["citation_detail_level"] = self._analyze_citation_detail(self._current_profile)
                self._current_profile.writing_habits = habits
                from datetime import datetime
                self._current_profile.updated_at = datetime.now().isoformat()
                self._save_profile(self._current_profile)
                return habits
        except Exception:
            pass

        # 合成失败：用第一篇的结果兜底
        detail = self._analyze_citation_detail(self._current_profile)
        fallback = dict(single_results[0])
        fallback.pop("_source", None)
        if detail:
            fallback["citation_detail_level"] = detail
        self._current_profile.writing_habits = fallback
        self._save_profile(self._current_profile)
        return fallback

    # ---- Phase 2b: 期刊格式分析（基于期刊范文） ----

    JOURNAL_STYLE_PROMPT = """你是一位学术期刊格式分析专家。请分析以下目标期刊的范文，提炼其格式规范。

这些范文全部来自同一本目标期刊，请提取：

1. **引用格式**: 文中引用是 [1] 编号制还是 (Author, Year)？参考文献列表的具体格式？
2. **章节结构**: 综述/论文的典型章节结构是什么？每个章节的名称和排列顺序？
3. **参考文献列表格式**: 作者、年份、标题、期刊、卷、页码的排序和标点规范（给出具体示例）
4. **图表惯例**: 图表标题格式（Figure 1. vs Fig. 1.）？正文中如何引用？
5. **摘要格式**: 摘要是否有结构化标签（Background/Methods/Results）？长度限制？
6. **其他格式**: 标题格式、作者署名、关键词数量等

## 输出格式

请严格返回 JSON（不要加 Markdown 标记）：

{
  "citation_format": "引用格式描述（文中引用 + 参考文献列表格式，给出具体示例）",
  "section_structure": "章节结构描述",
  "reference_list_format": "参考文献列表具体格式和示例",
  "figure_conventions": "图表引用惯例描述",
  "abstract_format": "摘要格式描述",
  "general_formatting": "其他格式注意事项"
}

以下是目标期刊的范文文本：
{paper_texts}"""

    def generate_journal_style(self, parse_client: "LLMClient") -> dict | None:
        """分析期刊范文的格式规范 —— 仅基于 journal_papers。

        Returns:
            journal_style dict。
        """
        if not self._current_profile:
            return None
        if self._current_profile.journal_count == 0:
            return None

        all_text = self.get_journal_paper_texts()
        if not all_text.strip():
            return None

        prompt = self.JOURNAL_STYLE_PROMPT.replace("{paper_texts}", all_text)
        messages = [
            {"role": "system", "content": "你是学术期刊格式分析专家。只返回 JSON，不要加解释。"},
            {"role": "user", "content": prompt},
        ]

        try:
            response = parse_client.chat_sync(messages, timeout=180.0)
            style = self._parse_style_guide_response(response)
            if style:
                self._current_profile.journal_style = style
                from datetime import datetime
                self._current_profile.updated_at = datetime.now().isoformat()
                self._save_profile(self._current_profile)
                return style
        except Exception:
            pass

        return None

    # ---- 兼容旧 API：generate_style_guide（会调用两个新方法） ----

    def generate_style_guide(self, parse_client: "LLMClient",
                            on_progress=None) -> dict | None:
        """生成完整的风格指南 —— 同时运行写作习惯和期刊格式分析。

        兼容旧版调用者。新版建议直接调用 generate_writing_habits() 和 generate_journal_style()。

        Returns:
            journal_style dict（用于向后兼容，完整指南存在 profile 的 writing_habits + journal_style 中）。
        """
        if not self._current_profile:
            return None

        def _emit(msg: str) -> None:
            if on_progress:
                on_progress(msg)

        habits = None
        style = None

        if self._current_profile.personal_count > 0:
            habits = self.generate_writing_habits(parse_client, on_progress=on_progress)
        if self._current_profile.journal_count > 0:
            _emit("正在分析期刊格式...")
            style = self.generate_journal_style(parse_client)

        return style or habits

    @staticmethod
    def _parse_style_guide_response(raw: str) -> dict | None:
        """解析 LLM 返回的风格指南 JSON。"""
        import json as _json
        import re

        if not raw or not raw.strip():
            return None

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
            try:
                return _json.loads(text[first:last + 1])
            except (_json.JSONDecodeError, TypeError):
                pass

        return None

    # ================================================================
    # Phase 3: 引用感知改写 + 遗漏文献检测
    # ================================================================

    POLISH_PROMPT = """你是学术写作润色专家。请润色以下文字，保持原意不变。

{style_context}
要求：
1. 提升学术表达的清晰度和流畅度
2. 修正语法错误和不当用词
3. 保持原有的引用标记不变
4. 输出语言必须与输入文字的语言保持一致（输入中文则输出中文，输入英文则输出英文）
5. 输出润色后的完整文字，不要加解释"""

    CITE_REWRITE_PROMPT = """你是学术综述写作专家。请根据提供的文献内容，改写以下文字。

{style_context}

【待改写的文字】
{selected_text}

【所有可用文献的摘要】（请根据待改写文字中的引用标记自动匹配对应的文献）
{citation_texts}

要求：
1. 先判断待改写文字中引用了哪些文献（不管引用格式是 [1]、(Author,Year) 还是其他），在可用文献中找到对应条目
2. 确保改写后的表述准确反映引文原文的发现
3. 如果原文不支持当前表述，请指出并提供更准确的版本
4. 保持综述应有的概括性风格，不要逐字照抄原文
5. 保持原有的引用标记不变
6. 输出语言必须与输入文字的语言保持一致（输入中文则输出中文，输入英文则输出英文）
7. 输出改写后的文字"""

    MISSING_LIT_PROMPT = """你是学术文献检索专家。分析以下综述草稿和已引用文献，找出可能遗漏的重要文献。

【综述草稿】
{draft_text}

【已引用文献】
{cited_papers}

请分析：
1. 这篇综述覆盖了哪些研究子方向？
2. 有哪些明显遗漏的研究方向或领域？（横向遗漏）
3. 在已覆盖的方向中，有没有近年（2023-2024）可能遗漏的新文献？（纵向遗漏）
4. 对每个遗漏方向，给出 3-5 个英文搜索关键词

请严格返回 JSON：
{{
  "covered_domains": [{{"domain": "方向名", "paper_count": 数字, "latest_year": 年份}}],
  "horizontal_gaps": [{{"domain": "遗漏方向", "reason": "原因", "search_queries": ["关键词1", "关键词2"]}}],
  "vertical_gaps": [{{"domain": "已有方向", "reason": "原因", "search_queries": ["关键词1"]}}]
}}"""

    def polish_text(self, write_client: "LLMClient", selected_text: str,
                    writing_type: str = "综述") -> str:
        """润色选中文字。"""
        if not selected_text.strip():
            return ""
        system_prompt = self.build_writing_system_prompt(writing_type)
        style_context = f"风格规范：\n{system_prompt}" if system_prompt else ""
        prompt = self.POLISH_PROMPT.replace("{style_context}", style_context)
        messages = [
            {"role": "system", "content": system_prompt or "你是学术写作润色专家。"},
            {"role": "user", "content": prompt + "\n\n待润色文字：\n" + selected_text},
        ]
        try:
            return write_client.chat_sync(messages, timeout=120.0)
        except Exception as e:
            return f"润色失败：{e}"

    def rewrite_with_citations(
        self, write_client: "LLMClient", selected_text: str,
        zotero_lib, writing_type: str = "综述",
    ) -> str:
        """基于 Zotero 引文原文改写选中文字。

        不管用户用什么引用格式，把 Zotero 库中所有文献信息发给 LLM，由 LLM 自动匹配。
        """
        if not selected_text.strip():
            return ""

        # 收集所有 Zotero 文献的摘要信息（不分引用格式）
        import fitz
        all_citation_texts = []
        if zotero_lib and hasattr(zotero_lib, 'get_all_items'):
            for i, item in enumerate(zotero_lib.get_all_items()):
                if not item.title:
                    continue
                title = item.title
                authors = ", ".join(item.authors[:3]) if item.authors else "?"
                year = item.year or "?"
                text = ""
                if item.pdf_path and os.path.isfile(item.pdf_path):
                    try:
                        doc = fitz.open(item.pdf_path)
                        parts = []
                        for page in doc:
                            t = page.get_text()
                            if t.strip():
                                parts.append(t.strip())
                        text = "\n\n".join(parts)
                        doc.close()
                    except Exception:
                        text = ""
                if not text:
                    text = getattr(item, 'abstract', '') or ""
                if text:
                    all_citation_texts.append(
                        f"--- 文献 [{i+1}] {authors} ({year}) {title} ---\n{text[:2000]}"
                    )

        if not all_citation_texts:
            return "Zotero 库中没有可用的文献全文或摘要，无法进行基于引文的改写"

        system_prompt = self.build_writing_system_prompt(writing_type)
        style_context = f"风格规范：\n{system_prompt}" if system_prompt else ""

        prompt = (self.CITE_REWRITE_PROMPT
            .replace("{style_context}", style_context)
            .replace("{selected_text}", selected_text)
            .replace("{citation_texts}", "\n\n".join(all_citation_texts)))
        messages = [
            {"role": "system", "content": system_prompt or "你是学术综述写作专家。请根据提供的文献内容，判断选中的文字引用了哪些文献，并据此改写。"},
            {"role": "user", "content": prompt},
        ]
        try:
            return write_client.chat_sync(messages, timeout=180.0)
        except Exception as e:
            return f"改写失败：{e}"

    def detect_missing_literature(
        self, write_client: "LLMClient", draft_text: str,
        zotero_lib,
    ) -> dict | None:
        """Step 1 of 遗漏文献检测：LLM 分析草稿 → 输出遗漏方向和搜索关键词。

        Returns:
            {"covered_domains": [...], "horizontal_gaps": [...], "vertical_gaps": [...]}
        """
        if not draft_text.strip():
            return None

        # 构建已引用文献列表
        cited_parts = []
        if zotero_lib and hasattr(zotero_lib, 'get_all_items'):
            items = zotero_lib.get_all_items()
            import re
            cite_pattern = re.compile(r'\[(\d+(?:[,，\s]*\d+)*)\]')
            cited_nums = set()
            for m in cite_pattern.finditer(draft_text):
                for num in re.split(r'[,，\s]+', m.group(1)):
                    if num.strip().isdigit():
                        cited_nums.add(int(num.strip()))

            for i, item in enumerate(items):
                if (i + 1) in cited_nums:
                    authors = ", ".join(item.authors[:2]) if item.authors else "?"
                    year = item.year or "?"
                    title = item.title or "?"
                    cited_parts.append(f"[{i+1}] {authors} ({year}) - {title[:120]}")

        cited_text = "\n".join(cited_parts) if cited_parts else "（未检测到引用或 Zotero 未连接）"

        prompt = (self.MISSING_LIT_PROMPT
            .replace("{draft_text}", draft_text)
            .replace("{cited_papers}", cited_text))
        messages = [
            {"role": "system", "content": "你是学术文献检索专家。只返回 JSON，不要加解释。"},
            {"role": "user", "content": prompt},
        ]
        try:
            response = write_client.chat_sync(messages, timeout=120.0)
            # 使用多级容错解析（复用 _parse_style_guide_response 的容错策略）
            import json as _json
            import re
            text = response.strip()
            # 尝试1: 直接解析
            try:
                return _json.loads(text)
            except (_json.JSONDecodeError, TypeError):
                pass
            # 尝试2: 提取 ```json ... ```
            for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
                m = re.search(pattern, text, re.DOTALL)
                if m:
                    try:
                        return _json.loads(m.group(1).strip())
                    except (_json.JSONDecodeError, TypeError):
                        pass
            # 尝试3: 提取 { ... }
            first = text.find('{')
            last = text.rfind('}')
            if first >= 0 and last > first:
                return _json.loads(text[first:last + 1])
            return None
        except Exception:
            return None

    def search_semantic_scholar(self, queries: list[str], limit: int = 10) -> list[dict]:
        """Step 2 of 遗漏文献检测：用搜索关键词调用 S2 API。

        返回论文列表 [{title, authors, year, citationCount, doi, url}, ...]。
        """
        import urllib.request
        import urllib.parse
        import urllib.error
        import json as _json
        import time as _time

        all_papers = []
        seen_titles = set()

        for qi, query in enumerate(queries[:8]):  # 最多 8 个查询
            if qi > 0:
                _time.sleep(1.0)  # 避免触发 S2 限流（无 API key 时约 1 req/s）
            for attempt in range(3):
                try:
                    params = urllib.parse.urlencode({"query": query, "limit": limit})
                    url = f"https://api.semanticscholar.org/graph/v1/paper/search?{params}&fields=title,authors,year,citationCount,externalIds,url"
                    req = urllib.request.Request(url, headers={"User-Agent": "PDFasker/2.0"})
                    with urllib.request.urlopen(req, timeout=15) as resp:
                        data = _json.loads(resp.read().decode())
                        for paper in data.get("data", []):
                            title = paper.get("title", "")
                            if title.lower() in seen_titles:
                                continue
                            seen_titles.add(title.lower())
                            authors_list = paper.get("authors", [])
                            author_names = ", ".join(
                                a.get("name", "") for a in authors_list[:3]
                            )
                            doi = ""
                            ext_ids = paper.get("externalIds", {}) or {}
                            doi = ext_ids.get("DOI", "")
                            all_papers.append({
                                "title": title,
                                "authors": author_names,
                                "year": paper.get("year", ""),
                                "citationCount": paper.get("citationCount", 0),
                                "doi": doi,
                                "url": paper.get("url", ""),
                                "source": f"S2搜索: {query[:40]}",
                            })
                        break  # 请求成功，跳出重试循环
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        wait = 2 ** attempt  # 指数退避: 1, 2, 4 秒
                        _time.sleep(wait)
                        continue
                    break
                except Exception:
                    break

        # 按引用数降序
        all_papers.sort(key=lambda p: p.get("citationCount", 0), reverse=True)
        return all_papers

    def search_s2_recommendations(
        self, positive_dois: list[str], limit: int = 20,
    ) -> list[dict]:
        """Step 2b of 遗漏文献检测：用 S2 推荐 API 基于已知文献推荐。

        positive_dois: 已引用文献的 DOI 列表。
        """
        import urllib.request
        import urllib.error
        import json as _json
        import time as _time

        # 先用 DOI 获取 paperId
        paper_ids = []
        for doi in positive_dois[:20]:
            for attempt in range(3):
                try:
                    url = f"https://api.semanticscholar.org/graph/v1/paper/DOI:{doi}?fields=paperId"
                    req = urllib.request.Request(url, headers={"User-Agent": "PDFasker/2.0"})
                    with urllib.request.urlopen(req, timeout=10) as resp:
                        data = _json.loads(resp.read().decode())
                        pid = data.get("paperId", "")
                        if pid:
                            paper_ids.append(pid)
                    break
                except urllib.error.HTTPError as e:
                    if e.code == 429:
                        _time.sleep(2 ** attempt)
                        continue
                    break
                except Exception:
                    break

            if len(paper_ids) >= 20:
                break

        if not paper_ids:
            return []

        # 调用推荐 API
        for attempt in range(3):
            try:
                body = _json.dumps({
                    "positivePaperIds": paper_ids[:20],
                    "limit": limit,
                }).encode("utf-8")
                req = urllib.request.Request(
                    "https://api.semanticscholar.org/recommendations/v1/papers",
                    data=body,
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "PDFasker/2.0",
                    },
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=20) as resp:
                    data = _json.loads(resp.read().decode())
                    papers = []
                    seen = set()
                    for paper in data.get("recommendedPapers", [])[:limit]:
                        title = paper.get("title", "")
                        if title.lower() in seen:
                            continue
                        seen.add(title.lower())
                        authors = ", ".join(
                            a.get("name", "") for a in (paper.get("authors") or [])[:3]
                        )
                        ext_ids = paper.get("externalIds", {}) or {}
                        papers.append({
                            "title": title,
                            "authors": authors,
                            "year": paper.get("year", ""),
                            "citationCount": paper.get("citationCount", 0),
                            "doi": ext_ids.get("DOI", ""),
                            "url": paper.get("url", ""),
                            "source": "S2推荐",
                        })
                    papers.sort(key=lambda p: p.get("citationCount", 0), reverse=True)
                    return papers
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    _time.sleep(2 ** attempt)
                    continue
                break
            except Exception:
                break

        return []
