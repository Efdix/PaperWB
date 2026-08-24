"""写作面板 —— 综述/论文/专利/软著 全流程写作辅助。

布局: 顶部工具栏(类型/知识库/Zotero) | 左侧编辑器 | 右侧知识库状态 + AI辅助

AI 辅助按钮:
  - "草稿整体评价": 对全文结构性诊断 → 用户确认/编辑 → 保存 → 注入润色
  - "AI 润色与核查": 润色语言 + 引文核查（有评价时同时处理评价发现的问题）
  - "文献补充": LLM分析→用户反馈→PubMed检索
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QScrollArea, QLabel, QFrame, QSplitter, QProgressBar,
    QMessageBox, QFileDialog, QComboBox, QGroupBox, QInputDialog,
    QListWidget, QListWidgetItem, QApplication, QMenu,
    QLineEdit, QDialog, QDialogButtonBox, QCheckBox, QTabWidget,
)
from PySide6.QtCore import Qt, Signal, QThread, QTimer
from PySide6.QtGui import QFont, QTextCursor, QColor, QTextCharFormat, QTextDocument

from ..utils.threads import track

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.zotero_parser import ZoteroLibrary
    from ..core.writing_coach import WritingCoach, WritingProfile


# ============================================================
# 后台线程：选中即改（内联润色）与批注批量修改
# ============================================================

class InlinePolishWorker(QThread):
    """选中即改：LLM 润色选中文字（复用 UnifiedWriter 管线）。

    携带 review_findings（草稿整体评价采纳结论）+ custom_instruction
    （含段内批注）；引文证据由 UnifiedWriter 按选区文本只读段内引文。
    """

    finished_signal = Signal(dict)  # {"polished_text", "original_text"}
    error_signal = Signal(str)

    def __init__(self, client, coach, text: str, instruction: str,
                 writing_type: str, zotero=None, review_findings: str = "",
                 parent=None):
        super().__init__(parent)
        self._client = client
        self._coach = coach
        self._text = text
        self._instruction = instruction
        self._writing_type = writing_type
        self._zotero = zotero
        self._review_findings = review_findings

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            from ..core.unified_writer import UnifiedWriter
            uw = UnifiedWriter()
            result = uw.process(
                self._client, self._text, self._coach, self._zotero,
                writing_type=self._writing_type,
                custom_instruction=self._instruction,
                review_findings=self._review_findings,
            )
            if self.isInterruptionRequested():
                return
            self.finished_signal.emit({
                "polished_text": result.get("polished_text", ""),
                "original_text": self._text,
            })
        except Exception as e:  # noqa: BLE001
            if not self.isInterruptionRequested():
                self.error_signal.emit(str(e))


class CommentFixWorker(QThread):
    """AI 按批注修改：批注列表 + 锚定段落 → LLM 返回修改后段落。

    接入知识库风格指南（coach）与段内引文的 Zotero 证据（zotero），
    与 UnifiedWriter 润色管线同口径。
    """

    finished_signal = Signal(dict)  # {"changes": [{"paragraph_index", "new_text"}]}
    error_signal = Signal(str)

    PROMPT = """你是学术论文修改助手。以下是导师在论文上留下的批注及对应的段落原文。

{style_context}

{evidence}

【批注与段落】
{items}

## 任务
逐条处理批注：根据批注要求修改对应段落。如果批注只是表扬或无需修改（如"很好"），保持段落不变。

## 输出（严格 JSON，不要加解释）
{{"changes": [{{"paragraph_index": 段落编号, "new_text": "修改后的完整段落文本"}}]}}

要求：
- paragraph_index 必须与输入一致
- new_text 是修改后的完整段落（不是片段）
- 无需修改的段落不要出现在 changes 里
- 保持学术风格，遵循风格约束；引文内容须与引文原文一致，不要编造"""

    def __init__(self, client, items: list[dict], coach=None, zotero=None,
                 writing_type: str = "综述", parent=None):
        super().__init__(parent)
        self._client = client
        self._items = items
        self._coach = coach
        self._zotero = zotero
        self._writing_type = writing_type

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            from ..core.json_utils import parse_json_response
            lines = []
            for i, it in enumerate(self._items):
                lines.append(
                    f"[{i}] 批注（{it.get('author', '')}）：{it.get('text', '')}\n"
                    f"段落：{it.get('paragraph', '')}")
            # 风格指南（与 UnifiedWriter 润色同一约束）
            style_context = ""
            if self._coach is not None:
                try:
                    sp = self._coach.build_polish_system_prompt(self._writing_type)
                    if sp:
                        style_context = f"【风格约束】\n{sp}"
                except Exception:  # noqa: BLE001
                    pass
            # 引文证据：段落中的引文标记 → Zotero 匹配（同 UnifiedWriter 口径）
            citation_evidence = ""
            if self._zotero is not None and hasattr(self._zotero, "get_all_items"):
                try:
                    from ..core.unified_writer import UnifiedWriter
                    uw = UnifiedWriter()
                    paras = " ".join(it.get("paragraph", "") for it in self._items)
                    evidence = uw._build_citation_sources(paras, self._zotero)
                    if evidence and "未检测到引文标记" not in evidence \
                            and "Zotero 未连接" not in evidence:
                        citation_evidence = f"【引文原文证据】\n{evidence}"
                except Exception:  # noqa: BLE001
                    pass
            prompt = (self.PROMPT
                      .replace("{style_context}", style_context)
                      .replace("{evidence}", citation_evidence)
                      .replace("{items}", "\n\n".join(lines)))
            resp = self._client.chat_sync(
                [{"role": "system", "content": "只返回 JSON，不要解释。"},
                 {"role": "user", "content": prompt}],
                 timeout=180.0, json_mode=True)
            if self.isInterruptionRequested():
                return
            data = parse_json_response(resp) or {}
            changes = []
            for ch in data.get("changes") or []:
                if not isinstance(ch, dict):
                    continue
                try:
                    pi = int(ch.get("paragraph_index", -1))
                except (TypeError, ValueError):
                    continue
                new_text = str(ch.get("new_text", "") or "").strip()
                if new_text:
                    changes.append({"paragraph_index": pi, "new_text": new_text})
            self.finished_signal.emit({"changes": changes})
        except Exception as e:  # noqa: BLE001
            if not self.isInterruptionRequested():
                self.error_signal.emit(str(e))


# ============================================================
# 风格指南展示对话框
# ============================================================

class StyleGuideDialog(QDialog):
    """风格指南滚动展示对话框 —— 可滚动的完整分析结果。"""

    def __init__(self, profile, parent=None):
        super().__init__(parent)
        self.setWindowTitle("风格分析完成")
        self.resize(560, 500)
        self.setMinimumSize(420, 350)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowMinimizeButtonHint | Qt.WindowType.Window)
        self._setup_ui(profile)

    def _setup_ui(self, profile):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: #f4f1eb; }")

        container = QWidget()
        container.setStyleSheet("background: #f4f1eb;")
        cl = QVBoxLayout(container)
        cl.setContentsMargins(24, 20, 24, 20)
        cl.setSpacing(12)

        def _section(title, content, color="#147c7c"):
            if not content:
                return
            header = QLabel(title)
            header.setStyleSheet(
                f"color: {color}; font-size: 15px; font-weight: bold; "
                f"border-left: 3px solid {color}; padding-left: 10px; margin-top: 8px;"
            )
            cl.addWidget(header)
            body = QLabel(content)
            body.setWordWrap(True)
            body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            body.setStyleSheet(
                "color: #29434a; font-size: 13px; line-height: 1.8; "
                "padding: 10px 12px; background: #fffdfa; border: 1px solid #e5e1d9; border-radius: 8px;"
            )
            cl.addWidget(body)

        # 写作习惯部分
        if profile and profile.has_writing_habits:
            h = profile.writing_habits
            if h.get("citation_detail_level"):
                cd = h["citation_detail_level"] or {}
                _section("引用详略度",
                    f"平均 {cd.get('avg_sentences_per_citation', '?')} 句话 / {cd.get('avg_chars_per_citation', '?')} 字  "
                    f"(共 {cd.get('sample_count', '?')} 个样本)\n"
                    f"中位数: {cd.get('med_chars_per_citation', '?')} 字  "
                    f"四分位: {cd.get('q25_chars', '?')}-{cd.get('q75_chars', '?')} 字\n"
                    f"分布: {cd.get('distribution_description', '')}",
                    "#147c7c")
            if h.get("argumentation_style"):
                _section("论述逻辑", h["argumentation_style"], "#147c7c")
            if h.get("paragraph_patterns"):
                _section("段落组织", h["paragraph_patterns"], "#147c7c")
            if h.get("terminology_preferences"):
                _section("术语偏好", h["terminology_preferences"], "#147c7c")
            st = h.get("sentence_templates")
            if st:
                if isinstance(st, list):
                    _section("句式模板", "\n".join(f"· {s}" for s in st), "#147c7c")
                else:
                    _section("句式模板", st, "#147c7c")
            if h.get("transition_phrases"):
                _section("过渡方式", h["transition_phrases"], "#147c7c")
            if h.get("tone_voice"):
                _section("语气风格", h["tone_voice"], "#147c7c")
            cit_den = h.get("citation_density", {})
            if cit_den:
                lines = []
                summary = cit_den.get("summary", "")
                if summary:
                    lines.append(summary)
                sections = cit_den.get("sections", [])
                if sections:
                    lines.append("")
                    for s in sections:
                        lines.append(f"  · {s.get('name', '?')}: {s.get('citation_count', '?')} 篇")
                _section("引用密度（各章节引用分布）", "\n".join(lines), "#147c7c")

            sp = h.get("section_paragraphs")
            if sp and isinstance(sp, list):
                lines = []
                for s in sp:
                    lines.append(
                        f"· {s.get('section', '?')}: {s.get('paragraph_count', '?')} 段, "
                        f"每段平均 {s.get('avg_words_per_paragraph', '?')} 字"
                    )
                    if s.get("notes"):
                        lines.append(f"  ({s['notes']})")
                _section("各章节段落组织", "\n".join(lines), "#147c7c")

            st_habits = h.get("section_transitions")
            if st_habits:
                lines = [f"密度: {st_habits.get('density', '无')}"]
                pats = st_habits.get("patterns", [])
                if pats:
                    lines.append("典型模式: " + "; ".join(str(p) for p in pats))
                wb = st_habits.get("weak_boundaries", [])
                if wb:
                    lines.append("薄弱边界: " + "; ".join(str(w) for w in wb))
                _section("章节过渡模式", "\n".join(lines), "#147c7c")

            sw = h.get("section_word_counts")
            if sw and isinstance(sw, list):
                lines = []
                for s in sw:
                    lines.append(
                        f"· {s.get('section', '?')}: 约 {s.get('word_count', '?')} 字 "
                        f"({s.get('percentage', '?')})"
                    )
                _section("各部分字数分布", "\n".join(lines), "#147c7c")
        if profile and profile.has_journal_style:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px;")
            cl.addWidget(sep)

            j = profile.journal_style
            if j.get("citation_format"):
                _section("引用格式", j["citation_format"], "#b87835")
            if j.get("section_structure"):
                _section("章节结构", j["section_structure"], "#b87835")
            if j.get("reference_list_format"):
                _section("参考文献格式", j["reference_list_format"], "#b87835")
            if j.get("figure_conventions"):
                _section("图表惯例", j["figure_conventions"], "#b87835")
            if j.get("abstract_format"):
                _section("摘要格式", j["abstract_format"], "#b87835")
            if j.get("general_formatting"):
                _section("其他格式", j["general_formatting"], "#b87835")

        cl.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)

        btn_row = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        btn_row.accepted.connect(self.accept)
        btn_row.setStyleSheet(
            "QPushButton { background: #147c7c; color: #ffffff; font-weight: bold; "
            "border-radius: 7px; padding: 7px 24px; font-size: 13px; }"
            "QPushButton:hover { background: #0e696a; }"
        )
        btn_container = QWidget()
        btn_container.setStyleSheet("background: #f4f1eb;")
        btn_lo = QHBoxLayout(btn_container)
        btn_lo.setContentsMargins(24, 10, 24, 16)
        btn_lo.addStretch()
        btn_lo.addWidget(btn_row)
        layout.addWidget(btn_container)


# ============================================================
# 后台工作线程
# ============================================================

class StyleGuideWorker(QThread):
    """后台生成风格指南。"""
    finished_signal = Signal(dict)
    error_signal = Signal(str)
    progress_signal = Signal(str)

    def __init__(self, coach: "WritingCoach", client: "LLMClient"):
        super().__init__()
        self._coach = coach
        self._client = client

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            guide = self._coach.generate_style_guide(
                self._client, on_progress=lambda msg: self.progress_signal.emit(msg)
            )
            if self.isInterruptionRequested():
                return
            if guide:
                self.finished_signal.emit(guide)
            else:
                self.error_signal.emit("风格分析失败：LLM 返回为空或格式异常")
        except Exception as e:
            self.error_signal.emit(str(e))


class CitationExtractWorker(QThread):
    """后台 LLM 引文识别。"""
    finished_signal = Signal(list)
    error_signal = Signal(str)

    def __init__(self, client: "LLMClient", draft_text: str, citation_count: int):
        super().__init__()
        self._client = client
        self._draft = draft_text
        self._count = citation_count

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            from ..core.unified_writer import UnifiedWriter
            citations = UnifiedWriter.extract_citations_via_llm(
                self._draft, self._count, self._client
            )
            if self.isInterruptionRequested():
                return
            self.finished_signal.emit(citations)
        except Exception as e:
            self.error_signal.emit(str(e))


class CitationEvidenceWorker(QThread):
    """后台读取引文对应的 Zotero PDF 证据，避免阻塞编辑器。"""

    finished_signal = Signal(str)
    error_signal = Signal(str)

    def __init__(self, text: str, citations: list[dict], zotero):
        super().__init__()
        self._text = text
        self._citations = citations
        self._zotero = zotero

    def run(self) -> None:
        try:
            from ..core.unified_writer import UnifiedWriter

            helper = UnifiedWriter()
            sources: list[str] = []
            for citation in self._citations:
                if self.isInterruptionRequested():
                    return
                author = citation["author_hint"]
                year = citation["year_hint"]
                marker = citation["original_marker"]
                if not author or not year or author == "unknown" or year == "unknown":
                    sources.append(
                        f"--- 引文 {marker}: 未能识别到作者/年份 ---\n(无原文可对照)"
                    )
                    continue
                candidates = self._zotero.find_by_citation(author, year)
                if not candidates:
                    sources.append(
                        f"--- 引文 {marker} ({author}, {year}): 未在 Zotero 库中匹配到 ---\n"
                        "(无原文可对照)"
                    )
                    continue
                query = helper._sentence_around(self._text, marker)
                for item in candidates[:2]:
                    if self.isInterruptionRequested():
                        return
                    evidence = helper._extract_relevant_context(item.pdf_path, query)
                    title = (item.title or "?")[:100]
                    authors = ", ".join(item.authors[:3]) if item.authors else "?"
                    sources.append(
                        f"--- 引文 {marker} → {authors} ({item.year}) {title} ---\n{evidence}"
                    )
            if not self.isInterruptionRequested():
                self.finished_signal.emit("\n\n".join(sources))
        except Exception as e:  # noqa: BLE001
            if not self.isInterruptionRequested():
                self.error_signal.emit(str(e))


class UnifiedWorker(QThread):
    """后台统一润色与引文核查。"""
    finished_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, client: "LLMClient", selected_text: str,
                 coach: "WritingCoach", zotero_lib: "ZoteroLibrary | None",
                 writing_type: str, pre_citation_sources: str = "",
                 review_findings: str = "", verify_only: bool = False,
                 mode: str = "polish"):
        super().__init__()
        self._client = client
        self._text = selected_text
        self._coach = coach
        self._zotero = zotero_lib
        self._writing_type = writing_type
        self._pre_citation_sources = pre_citation_sources
        self._review_findings = review_findings
        self._verify_only = verify_only
        self._mode = mode

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            from ..core.unified_writer import UnifiedWriter
            uw = UnifiedWriter()
            result = uw.process(self._client, self._text, self._coach,
                                self._zotero, self._writing_type,
                                pre_citation_sources=self._pre_citation_sources,
                                review_findings=self._review_findings,
                                verify_only=self._verify_only,
                                mode=self._mode)
            if self.isInterruptionRequested():
                return
            self.finished_signal.emit(result)
        except Exception as e:
            self.error_signal.emit(str(e))


class DraftReviewWorker(QThread):
    """后台草稿整体评价。"""
    finished_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, client: "LLMClient", draft_text: str,
                 coach: "WritingCoach"):
        super().__init__()
        self._client = client
        self._draft = draft_text
        self._coach = coach

    def run(self):
        if self.isInterruptionRequested():
            return
        try:
            from ..core.draft_reviewer import DraftReviewer
            reviewer = DraftReviewer()
            result = reviewer.review(self._client, self._draft, self._coach)
            if self.isInterruptionRequested():
                return
            if result.get("error"):
                self.error_signal.emit(result["error"])
            else:
                self.finished_signal.emit(result)
        except Exception as e:
            self.error_signal.emit(str(e))


# ============================================================
# 写作面板
# ============================================================

class WritingPanel(QWidget):
    """写作面板 —— 综述/论文/专利/软著 写作辅助。"""

    # 信号
    feed_requested = Signal(list, str)  # 文献补充结果推送到推荐流 (papers, label)
    # 统计埋点：草稿落盘（字数）、润色采纳（原文/润色后字数）、文献补充检索完成（结果数）
    draft_saved = Signal(int)
    polish_accepted = Signal(int, int)
    lit_search_completed = Signal(int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._text_client: LLMClient | None = None
        self._zotero: ZoteroLibrary | None = None
        self._coach = self._create_coach()
        self._current_writing_type = "综述"
        self._last_kb_index = 0
        self._verify_only = False
        self._style_worker: StyleGuideWorker | None = None
        self._unified_worker: UnifiedWorker | None = None
        self._review_worker: DraftReviewWorker | None = None
        self._citation_worker: CitationExtractWorker | None = None
        self._evidence_worker: CitationEvidenceWorker | None = None
        self._active_dialogs: list = []  # 保持非模态对话框引用防止被GC
        self._draft_dirty = False  # 仅用户真实改动后才允许自动保存落盘

        # ---- Word 文档绑定与 AI 修订（WorkBuddy 式人机双写） ----
        self._word_path: str = ""            # 绑定的 .docx 路径（空 = 未绑定）
        self._word_styles: list[str] = []    # 打开时的段落样式名
        self._word_comments: list = []       # 打开时的批注（DocxComment 列表）
        self._word_has_revisions = False
        self._word_dirty = False             # 编辑器内容与磁盘不一致
        self._rev_controller = None          # DocDiffController（_setup_ui 后创建）
        self._rev_worker: "InlinePolishWorker | None" = None
        self._comment_worker: "CommentFixWorker | None" = None
        self._ai_bar: QFrame | None = None   # 浮动 AI 操作条
        self._ai_bar_anchor: tuple[int, int] = (-1, -1)  # 操作条对应的选区
        self._comment_highlights: list = []  # 批注定位高亮（ExtraSelection）
        self._comment_marks: list = []       # 批注常驻标记（ExtraSelection）
        self._ai_word_selections: list = []  # AI 味标黄（ExtraSelection）
        self._inspector_visible = True

        self._setup_ui()
        from ..core.doc_diff import DocDiffController
        self._rev_controller = DocDiffController(self.editor)
        self._rev_controller.set_on_changed(self._on_rev_changed)
        self.editor.textChanged.connect(self._rev_controller.on_text_changed)
        self._refresh_kb_dropdown()
        self._load_draft()  # 恢复上次知识库的编辑器草稿（防止空文本覆盖磁盘）

        # 自动保存定时器：每 30 秒保存一次编辑器草稿
        self._auto_save_timer = QTimer(self)
        self._auto_save_timer.timeout.connect(self._auto_save_draft)
        self._auto_save_timer.start(30_000)

        # AI 味禁词标黄（防抖刷新）
        self._ai_highlight_timer = QTimer(self)
        self._ai_highlight_timer.setSingleShot(True)
        self._ai_highlight_timer.timeout.connect(self._refresh_ai_highlight)

    @staticmethod
    def _create_coach():
        from ..core.writing_coach import WritingCoach
        return WritingCoach()

    def _track_dialog(self, dialog) -> None:
        """登记非模态对话框并防止 GC；关闭后自动从列表移除，避免泄漏。"""
        self._active_dialogs.append(dialog)
        dialog.finished.connect(
            lambda _result, d=dialog: self._release_dialog(d)
        )

    def _release_dialog(self, dialog) -> None:
        if dialog in self._active_dialogs:
            self._active_dialogs.remove(dialog)

    def _set_ai_buttons_busy(self, busy: bool) -> None:
        """批量禁用/启用 AI 辅助按钮，避免并发操作。"""
        for b in (self._polish_btn, self._cn2en_btn,
                  getattr(self, "_verify_btn", None),
                  getattr(self, "_lit_search_btn", None),
                  getattr(self, "_comment_ai_btn", None)):
            if b is not None:
                b.setEnabled(not busy)
        if not busy and getattr(self, "_comment_ai_btn", None) is not None:
            self._comment_ai_btn.setEnabled(bool(self._word_comments))
        if not busy:
            self._polish_btn.setText("AI 润色与核查")

    # ---- 注入依赖 ----

    def set_text_client(self, client: "LLMClient | None"):
        self._text_client = client

    def set_zotero_library(self, zotero: "ZoteroLibrary | None"):
        self._zotero = zotero
        self._update_zotero_status()

    def prepare_storage_switch(self) -> bool:
        """在数据根目录改变前保存旧目录草稿并停止写作任务。"""
        if not self._confirm_save_if_dirty():
            return False
        self._auto_save_draft()
        if not self._cancel_all_workers():
            QMessageBox.warning(self, "请稍候", "当前写作任务尚未退出，请稍后再切换数据目录。")
            return False
        return True

    def reload_storage(self) -> None:
        """数据根目录切换后重新加载写作知识库。"""
        self._clear_word_binding()
        self._coach.reload_storage()
        self._refresh_kb_dropdown()
        self._load_draft(replace=True)

    # ---- UI 构建 ----

    def _setup_ui(self):
        self.setObjectName("writingSurface")
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(12)

        # ===== 页面标题 =====
        writing_header = QFrame()
        writing_header.setObjectName("writingHeader")
        header_layout = QHBoxLayout(writing_header)
        header_layout.setContentsMargins(18, 14, 18, 14)
        header_text = QVBoxLayout()
        header_text.setSpacing(1)
        eyebrow = QLabel("写作与引用")
        eyebrow.setObjectName("eyebrowLabel")
        header_text.addWidget(eyebrow)
        title = QLabel("写作工作台")
        title.setObjectName("titleLabel")
        header_text.addWidget(title)
        intro = QLabel("把观点写清楚，把每一处引用都落到证据上")
        intro.setObjectName("subtitleLabel")
        header_text.addWidget(intro)
        header_layout.addLayout(header_text)
        header_layout.addStretch()
        self._history_btn = QPushButton("查看润色历史")
        self._history_btn.setObjectName("secondaryBtn")
        self._history_btn.setToolTip("查看当前知识库的润色历史，可复制或回插到编辑器")
        self._history_btn.clicked.connect(self._on_polish_history)
        header_layout.addWidget(self._history_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        self._inspector_btn = QPushButton("工具栏")
        self._inspector_btn.setObjectName("secondaryBtn")
        self._inspector_btn.setCheckable(True)
        self._inspector_btn.setChecked(True)
        self._inspector_btn.setToolTip("显示或隐藏写作工具检查器")
        self._inspector_btn.toggled.connect(self._set_inspector_visible)
        header_layout.addWidget(self._inspector_btn, alignment=Qt.AlignmentFlag.AlignBottom)
        main_layout.addWidget(writing_header)

        # ===== 工作上下文 =====
        context_bar = QFrame()
        context_bar.setObjectName("controlBar")
        context_layout = QHBoxLayout(context_bar)
        context_layout.setContentsMargins(14, 10, 14, 10)
        context_layout.setSpacing(8)

        type_label = QLabel("写作类型")
        type_label.setObjectName("sectionLabel")
        context_layout.addWidget(type_label)

        self._type_combo = QComboBox()
        self._type_combo.setEditable(True)
        self._type_combo.setMinimumWidth(150)
        self._type_combo.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._type_combo.customContextMenuRequested.connect(self._on_type_context_menu)
        from ..core.writing_prompts import get_all_writing_types
        for key, label in get_all_writing_types():
            self._type_combo.addItem(label, key)
        self._type_combo.insertSeparator(self._type_combo.count())
        self._type_combo.addItem("＋ 自定义类型...", "__custom__")
        self._type_combo.currentIndexChanged.connect(self._on_writing_type_changed)
        context_layout.addWidget(self._type_combo)

        kb_label = QLabel("当前知识库")
        kb_label.setObjectName("sectionLabel")
        context_layout.addWidget(kb_label)

        self._kb_combo = QComboBox()
        self._kb_combo.setEditable(True)
        self._kb_combo.setMinimumWidth(190)
        self._kb_combo.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._kb_combo.customContextMenuRequested.connect(self._on_kb_context_menu)
        self._kb_combo.currentIndexChanged.connect(self._on_kb_changed)
        context_layout.addWidget(self._kb_combo)

        context_layout.addStretch()
        main_layout.addWidget(context_bar)

        # ===== 主区域: 编辑器 | 右侧栏 =====
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.setOpaqueResize(False)

        # -- 编辑器 --
        editor_frame = QFrame()
        editor_frame.setObjectName("editorCard")
        editor_layout = QVBoxLayout(editor_frame)
        editor_layout.setContentsMargins(16, 14, 16, 16)
        editor_layout.setSpacing(10)

        editor_head = QHBoxLayout()
        editor_title = QLabel("正文编辑器")
        editor_title.setObjectName("sectionLabel")
        editor_head.addWidget(editor_title)
        editor_hint = QLabel("选中文字后，可使用右侧 AI 工具")
        editor_hint.setObjectName("subtitleLabel")
        editor_head.addWidget(editor_hint)
        editor_head.addStretch()

        editor_layout.addLayout(editor_head)

        # Word 文件操作单独占一行，避免窄窗口下与标题互相挤压。
        word_head = QHBoxLayout()
        self._open_word_btn = QPushButton("打开 Word")
        self._open_word_btn.setObjectName("secondaryBtn")
        self._open_word_btn.setToolTip("打开 .docx 文档直接编辑（段落级格式，批注只读展示）")
        self._open_word_btn.clicked.connect(self._on_open_word)
        word_head.addWidget(self._open_word_btn)
        self._save_word_btn = QPushButton("保存到 Word")
        self._save_word_btn.setObjectName("primaryBtn")
        self._save_word_btn.setToolTip("把编辑器内容写回当前 Word 文档（保留批注）")
        self._save_word_btn.clicked.connect(self._on_save_word)
        self._save_word_btn.setEnabled(False)
        word_head.addWidget(self._save_word_btn)
        self._word_file_label = QLabel("未绑定文件")
        self._word_file_label.setObjectName("subtitleLabel")
        self._word_file_label.setToolTip("当前绑定的 Word 文档路径")
        word_head.addWidget(self._word_file_label, 1)
        word_head.addStretch()
        editor_layout.addLayout(word_head)

        # 修订审阅工具栏（有 AI 修订时显示）
        review_bar = QFrame()
        review_bar.setObjectName("reviewBar")
        review_bar.setStyleSheet(
            "QFrame#reviewBar { background-color: #f0f7f4; border: 1px solid #cfe5dd; "
            "border-radius: 8px; }"
        )
        review_layout = QHBoxLayout(review_bar)
        review_layout.setContentsMargins(10, 4, 10, 4)
        review_layout.setSpacing(6)
        review_label = QLabel("✏️ AI 修订")
        review_label.setObjectName("sectionLabel")
        review_layout.addWidget(review_label)
        self._rev_prev_btn = QPushButton("◀ 上一处")
        self._rev_prev_btn.setObjectName("secondaryBtn")
        self._rev_prev_btn.setToolTip("跳转到上一处 AI 修改")
        self._rev_prev_btn.clicked.connect(lambda: self._rev_controller.navigate(-1))
        review_layout.addWidget(self._rev_prev_btn)
        self._rev_next_btn = QPushButton("下一处 ▶")
        self._rev_next_btn.setObjectName("secondaryBtn")
        self._rev_next_btn.setToolTip("跳转到下一处 AI 修改")
        self._rev_next_btn.clicked.connect(lambda: self._rev_controller.navigate(1))
        review_layout.addWidget(self._rev_next_btn)
        self._rev_accept_btn = QPushButton("✅ 接受")
        self._rev_accept_btn.setObjectName("successBtn")
        self._rev_accept_btn.setToolTip("接受当前修改")
        self._rev_accept_btn.clicked.connect(lambda: self._rev_controller.apply_change(True))
        review_layout.addWidget(self._rev_accept_btn)
        self._rev_reject_btn = QPushButton("❌ 拒绝")
        self._rev_reject_btn.setObjectName("dangerBtn")
        self._rev_reject_btn.setToolTip("拒绝当前修改")
        self._rev_reject_btn.clicked.connect(lambda: self._rev_controller.apply_change(False))
        review_layout.addWidget(self._rev_reject_btn)
        self._rev_accept_all_btn = QPushButton("全部接受")
        self._rev_accept_all_btn.setObjectName("secondaryBtn")
        self._rev_accept_all_btn.setToolTip("接受全部 AI 修改")
        self._rev_accept_all_btn.clicked.connect(self._on_rev_accept_all)
        review_layout.addWidget(self._rev_accept_all_btn)
        self._rev_reject_all_btn = QPushButton("全部拒绝")
        self._rev_reject_all_btn.setObjectName("softBtn")
        self._rev_reject_all_btn.setToolTip("拒绝全部 AI 修改")
        self._rev_reject_all_btn.clicked.connect(self._on_rev_reject_all)
        review_layout.addWidget(self._rev_reject_all_btn)
        self._rev_count_label = QLabel("修改: 0 处")
        self._rev_count_label.setObjectName("subtitleLabel")
        review_layout.addWidget(self._rev_count_label)
        review_layout.addStretch()
        review_bar.setVisible(False)
        self._review_bar = review_bar
        editor_layout.addWidget(review_bar)

        self.editor = QTextEdit()
        self.editor.setObjectName("draftEditor")
        self.editor.setPlaceholderText(
            "从这里开始写作...\n\n"
            "建议先选择写作类型和知识库，再把需要润色或核查的段落选中。\n"
            "也可以点「打开 Word」直接编辑 .docx 文档。"
        )
        self.editor.textChanged.connect(self._on_editor_text_changed)
        self.editor.textChanged.connect(self._on_ai_highlight_schedule)
        self.editor.textChanged.connect(self._on_editor_manual_edit)
        self.editor.selectionChanged.connect(self._on_editor_selection_changed)
        editor_layout.addWidget(self.editor)

        splitter.addWidget(editor_frame)

        # -- 右侧工具检查器：按功能分栏，避免所有操作纵向堆在一条窄侧栏 --
        right_scroll = QScrollArea()
        self._inspector_scroll = right_scroll
        right_scroll.setObjectName("writingSideScroll")
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)

        right_frame = QFrame()
        right_frame.setObjectName("writingSidePanel")
        right_frame.setMinimumWidth(260)
        right_frame.setMaximumWidth(380)
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(8)

        inspector_title = QLabel("工具检查器")
        inspector_title.setObjectName("sectionLabel")
        right_layout.addWidget(inspector_title)

        inspector_hint = QLabel("按功能分栏，正文区域保持专注")
        inspector_hint.setObjectName("subtitleLabel")
        right_layout.addWidget(inspector_hint)

        inspector_tabs = QTabWidget()
        inspector_tabs.setObjectName("writingInspectorTabs")
        inspector_tabs.setDocumentMode(True)
        inspector_tabs.setUsesScrollButtons(True)

        # 知识库状态
        kb_group = QGroupBox("📚 知识库状态")
        kb_layout = QVBoxLayout(kb_group)
        kb_layout.setSpacing(6)

        self._kb_status_label = QLabel("未选择知识库")
        self._kb_status_label.setWordWrap(True)
        self._kb_status_label.setObjectName("subtitleLabel")
        kb_layout.addWidget(self._kb_status_label)

        self._sample_btn = QPushButton("添加写作范文")
        self._sample_btn.setObjectName("secondaryBtn")
        self._sample_btn.setToolTip("上传你自己的论文 PDF 供风格分析")
        self._sample_btn.clicked.connect(self._on_add_sample_paper)
        kb_layout.addWidget(self._sample_btn)

        self._journal_btn = QPushButton("添加期刊范文")
        self._journal_btn.setObjectName("secondaryBtn")
        self._journal_btn.setToolTip("上传目标期刊的综述 PDF 供风格分析")
        self._journal_btn.clicked.connect(self._on_add_journal_paper)
        kb_layout.addWidget(self._journal_btn)

        self._style_btn = QPushButton("生成风格指南")
        self._style_btn.setObjectName("primaryBtn")
        self._style_btn.setToolTip("基于已添加的论文和范文，让 AI 分析写作风格并生成指南")
        self._style_btn.clicked.connect(self._on_generate_style_guide)
        self._style_btn.setEnabled(False)
        kb_layout.addWidget(self._style_btn)

        self._view_style_btn = QPushButton("查看风格指南")
        self._view_style_btn.setObjectName("secondaryBtn")
        self._view_style_btn.setToolTip("再次查看已生成的风格分析结果")
        self._view_style_btn.clicked.connect(self._on_view_style_guide)
        self._view_style_btn.setEnabled(False)
        kb_layout.addWidget(self._view_style_btn)

        inspector_tabs.addTab(kb_group, "知识库")

        # Zotero 状态
        zotero_group = QGroupBox("Zotero 状态")
        zotero_layout = QVBoxLayout(zotero_group)
        zotero_layout.setSpacing(4)

        self._zotero_status_label = QLabel("未连接")
        self._zotero_status_label.setWordWrap(True)
        self._zotero_status_label.setObjectName("statusChip")
        self._zotero_status_label.setProperty("status", "warning")
        zotero_layout.addWidget(self._zotero_status_label)
        inspector_tabs.addTab(zotero_group, "Zotero")

        # 审阅批注（Word 文档批注只读展示）
        comment_group = QGroupBox("📝 审阅批注")
        comment_layout = QVBoxLayout(comment_group)
        comment_layout.setSpacing(6)

        self._comment_status = QLabel("未打开 Word 文档")
        self._comment_status.setWordWrap(True)
        self._comment_status.setObjectName("subtitleLabel")
        comment_layout.addWidget(self._comment_status)

        self._comment_list = QListWidget()
        self._comment_list.setMaximumHeight(160)
        self._comment_list.setStyleSheet(
            "QListWidget { background-color: #fffdfa; border: 1px solid #e5e1d9; "
            "border-radius: 8px; font-size: 12px; color: #29434a; }"
            "QListWidget::item { padding: 6px; border-bottom: 1px solid #eef0ec; }"
        )
        self._comment_list.itemClicked.connect(self._on_comment_clicked)
        comment_layout.addWidget(self._comment_list)

        self._comment_ai_btn = QPushButton("AI 按批注修改")
        self._comment_ai_btn.setObjectName("primaryBtn")
        self._comment_ai_btn.setToolTip(
            "让 AI 读取全部批注并修改对应段落，修改以修订形式展示，可逐处接受/拒绝")
        self._comment_ai_btn.clicked.connect(self._on_ai_fix_comments)
        self._comment_ai_btn.setEnabled(False)
        comment_layout.addWidget(self._comment_ai_btn)

        inspector_tabs.addTab(comment_group, "批注")

        # AI 辅助
        ai_group = QGroupBox("AI 辅助工具")
        ai_layout = QVBoxLayout(ai_group)
        ai_layout.setSpacing(6)

        self._polish_btn = QPushButton("AI 润色与核查")
        self._polish_btn.setObjectName("primaryBtn")
        self._polish_btn.setToolTip(
            "选中含引文的文字后，AI 同时完成：润色语言（含去除机械化表达）+ 红线检查（只报致命逻辑/术语/语法问题）"
            " + 核查引文准确性（需 Zotero 连接）。\n"
            "如有已保存的草稿评价，将同时处理评价发现的问题。"
        )
        self._polish_btn.clicked.connect(self._on_unified_polish)
        ai_layout.addWidget(self._polish_btn)

        self._verify_btn = QPushButton("仅核查引文")
        self._verify_btn.setObjectName("secondaryBtn")
        self._verify_btn.setToolTip("只核查选中文字中的引文，不修改正文")
        self._verify_btn.clicked.connect(self._on_verify_only)
        ai_layout.addWidget(self._verify_btn)

        self._cn2en_btn = QPushButton("中文翻译为英文")
        self._cn2en_btn.setObjectName("secondaryBtn")
        self._cn2en_btn.setToolTip(
            "将选中的中文草稿翻译并润色为符合顶级会议/期刊标准的英文学术片段。"
        )
        self._cn2en_btn.clicked.connect(self._on_cn2en)
        ai_layout.addWidget(self._cn2en_btn)

        self._lit_search_btn = QPushButton("补充参考文献")
        self._lit_search_btn.setObjectName("secondaryBtn")
        self._lit_search_btn.setToolTip(
            "AI 分析草稿的遗漏方向 → 你审阅并反馈 → 确认后 PubMed 检索"
        )
        self._lit_search_btn.clicked.connect(self._on_lit_search)
        ai_layout.addWidget(self._lit_search_btn)

        inspector_tabs.addTab(ai_group, "AI 辅助")
        right_layout.addWidget(inspector_tabs, 1)

        right_scroll.setWidget(right_frame)
        splitter.addWidget(right_scroll)
        splitter.setSizes([900, 320])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, True)

        main_layout.addWidget(splitter, 1)

        # ===== 底部状态栏 =====
        status_sep = QFrame()
        status_sep.setFrameShape(QFrame.Shape.HLine)
        status_sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px;")
        main_layout.addWidget(status_sep)

        status_bar = QHBoxLayout()
        status_bar.setContentsMargins(12, 4, 12, 4)
        self._status_label = QLabel("就绪")
        self._status_label.setObjectName("subtitleLabel")
        status_bar.addWidget(self._status_label)
        status_bar.addStretch()

        self._word_count_label = QLabel("字数: 0")
        self._word_count_label.setObjectName("subtitleLabel")
        status_bar.addWidget(self._word_count_label)

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(False)
        self._progress_bar.setMaximumWidth(180)
        self._progress_bar.setMaximumHeight(14)
        status_bar.addWidget(self._progress_bar)

        self._cancel_btn = QPushButton("停止处理")
        self._cancel_btn.setObjectName("dangerBtn")
        self._cancel_btn.setToolTip("\u7ec8\u6b62\u5f53\u524d\u7684 AI \u5904\u7406")
        self._cancel_btn.clicked.connect(
            lambda _checked=False: self._cancel_all_workers())
        self._cancel_btn.setVisible(False)
        status_bar.addWidget(self._cancel_btn)
        main_layout.addLayout(status_bar)

    def _set_inspector_visible(self, visible: bool) -> None:
        """切换工具检查器，给编辑器留出完整的专注空间。"""
        self._inspector_visible = bool(visible)
        if hasattr(self, "_inspector_scroll"):
            self._inspector_scroll.setVisible(self._inspector_visible)
        if hasattr(self, "_inspector_btn"):
            self._inspector_btn.setText("隐藏工具栏" if self._inspector_visible else "显示工具栏")

    # ---- 知识库操作 ----

    def _refresh_kb_dropdown(self):
        """刷新知识库下拉列表，最后一项为'＋ 新建知识库...'。"""
        self._kb_combo.blockSignals(True)
        self._kb_combo.clear()
        self._kb_combo.addItem("(未选择)", "")
        for name in self._coach.profile_names:
            profile = self._coach._profiles.get(name)
            extra = ""
            if profile:
                h = "习惯✓" if profile.has_writing_habits else "习惯—"
                j = "期刊✓" if profile.has_journal_style else "期刊—"
                extra = f"（范文 {profile.personal_count} 篇 · {h} · 期刊 {profile.journal_count} 篇 · {j}）"
            self._kb_combo.addItem(f"{name}{extra}", name)

        # 分隔 + 新建项
        self._kb_combo.insertSeparator(self._kb_combo.count())
        self._kb_combo.addItem("＋ 新建知识库...", "__new__")

        # 恢复当前选择
        if self._coach.current_profile:
            idx = self._kb_combo.findData(self._coach.current_profile.name)
            if idx >= 0:
                self._kb_combo.setCurrentIndex(idx)
            profile_type = self._coach.current_profile.writing_type or "综述"
            type_idx = self._type_combo.findData(profile_type)
            if type_idx >= 0:
                self._type_combo.blockSignals(True)
                self._type_combo.setCurrentIndex(type_idx)
                self._type_combo.blockSignals(False)
            self._current_writing_type = profile_type
        self._last_kb_index = self._kb_combo.currentIndex()
        self._kb_combo.blockSignals(False)
        self._update_kb_status()

    def _on_kb_changed(self, idx: int):
        data = self._kb_combo.itemData(idx) or ""
        if data == "__new__":
            # 触发新建
            self._kb_combo.blockSignals(True)
            self._kb_combo.setCurrentIndex(self._last_kb_index)
            self._kb_combo.blockSignals(False)
            self._on_new_kb()
            return
        if data and data in self._coach.profile_names:
            # 切换知识库前询问未保存的 Word 修改
            if not self._confirm_save_if_dirty():
                # 用户取消：回退下拉选择
                self._kb_combo.blockSignals(True)
                self._kb_combo.setCurrentIndex(self._last_kb_index)
                self._kb_combo.blockSignals(False)
                return
            if not self._cancel_all_workers():
                self._kb_combo.blockSignals(True)
                self._kb_combo.setCurrentIndex(self._last_kb_index)
                self._kb_combo.blockSignals(False)
                QMessageBox.warning(self, "请稍候", "当前 AI 任务尚未退出，请稍后再切换知识库。")
                return
            # 保存当前草稿（到旧知识库）
            self._auto_save_draft()
            self._coach.switch_profile(data)
            profile = self._coach.current_profile
            if profile and profile.writing_type:
                type_idx = self._type_combo.findData(profile.writing_type)
                if type_idx >= 0:
                    self._type_combo.blockSignals(True)
                    self._type_combo.setCurrentIndex(type_idx)
                    self._type_combo.blockSignals(False)
                    self._current_writing_type = profile.writing_type
            self._clear_word_binding()
            # 加载新知识库的草稿：必须整体替换，否则旧库内容会被
            # 30 秒自动保存写进新库的草稿文件（两库互相污染）
            self._load_draft(replace=True)
            self._last_kb_index = self._kb_combo.currentIndex()
        else:
            self._coach._current_profile = None
            self._clear_word_binding()
            self._last_kb_index = self._kb_combo.currentIndex()
        self._update_kb_status()

    def _on_kb_context_menu(self, pos):
        """知识库下拉右键菜单：删除。"""
        data = self._kb_combo.currentData() or ""
        if not data or data == "__new__" or data not in self._coach.profile_names:
            return
        menu = QMenu(self)
        a = menu.addAction("🗑 删除此知识库")
        a.triggered.connect(lambda: self._on_delete_kb())
        menu.exec(self._kb_combo.mapToGlobal(pos))

    def _on_writing_type_changed(self, idx: int):
        data = self._type_combo.itemData(idx) or ""
        if data == "__custom__":
            self._type_combo.blockSignals(True)
            cur = self._current_writing_type
            ci = self._type_combo.findData(cur)
            self._type_combo.setCurrentIndex(ci if ci >= 0 else 0)
            self._type_combo.blockSignals(False)
            self._on_new_custom_type()
            return
        if data:
            self._current_writing_type = data
            if self._coach.current_profile is not None:
                self._coach.current_profile.writing_type = data
                try:
                    self._coach._save_profile(self._coach.current_profile)
                except Exception:  # noqa: BLE001
                    pass

    # ---- 自定义写作类型 ----

    def _on_type_context_menu(self, pos):
        """类型下拉右键菜单：删除自定义类型（内置类型不可删）。"""
        data = self._type_combo.currentData() or ""
        from ..core.writing_prompts import WRITING_TYPES
        if not data or data == "__custom__" or data in WRITING_TYPES:
            return
        menu = QMenu(self)
        a = menu.addAction("🗑 删除此自定义类型")
        a.triggered.connect(lambda: self._on_delete_custom_type(data))
        menu.exec(self._type_combo.mapToGlobal(pos))

    def _on_new_custom_type(self):
        key, ok = QInputDialog.getText(self, "自定义写作类型", "类型标识（英文 key，如 lab_report）：")
        if not ok or not key.strip():
            return
        key = key.strip().lower().replace(" ", "_")
        label, ok = QInputDialog.getText(self, "自定义写作类型", "显示名称（如 📄 实验报告）：")
        if not ok or not label.strip():
            return
        prompt, ok = QInputDialog.getMultiLineText(
            self, "自定义写作类型",
            "系统提示词（说明该类型写作原则）：",
            "你是该写作类型的专家。请遵循以下原则：\n1. \n2. ",
        )
        if not ok or not prompt.strip():
            return
        from ..utils.config import add_custom_writing_type
        add_custom_writing_type(key, label.strip(), prompt.strip())
        self._refresh_type_combo(select_key=key)
        self._status_label.setText(f"已创建自定义类型: {label.strip()}")

    def _on_delete_custom_type(self, key: str):
        from ..utils.config import remove_custom_writing_type
        remove_custom_writing_type(key)
        if self._current_writing_type == key:
            self._current_writing_type = "综述"
        self._refresh_type_combo()
        self._status_label.setText("已删除自定义类型")

    def _refresh_type_combo(self, select_key: str = ""):
        self._type_combo.blockSignals(True)
        self._type_combo.clear()
        from ..core.writing_prompts import get_all_writing_types
        for k, lb in get_all_writing_types():
            self._type_combo.addItem(lb, k)
        self._type_combo.insertSeparator(self._type_combo.count())
        self._type_combo.addItem("＋ 自定义类型...", "__custom__")
        if select_key:
            ci = self._type_combo.findData(select_key)
            if ci >= 0:
                self._type_combo.setCurrentIndex(ci)
                self._current_writing_type = select_key
        self._type_combo.blockSignals(False)

    def _on_editor_text_changed(self):
        self._draft_dirty = True
        text = self.editor.toPlainText()
        chars = len(text)
        self._word_count_label.setText(f"字数: {chars}")
        # 绑定 Word 文件后，内容变化即标记未保存
        if self._word_path:
            self._word_dirty = True
            self._update_word_file_label()

    def _on_editor_manual_edit(self):
        """用户手动编辑：若光标落在修订区域内，自动接受该处修订避免冲突。"""
        if self._rev_controller is None or not self._rev_controller.has_changes:
            return
        # 渲染期间（_skip_recompute）不处理
        if getattr(self._rev_controller, "_skip_recompute", False):
            return
        cursor = self.editor.textCursor()
        pos = cursor.position()
        for i, (start, end, kind) in enumerate(self._rev_controller.change_anchors):
            if start <= pos <= end:
                self._rev_controller._current_anchor_idx = i
                self._rev_controller.apply_change(accept=True)
                break

    # ================= Word 文档打开/保存 =================

    def _clear_word_binding(self) -> None:
        """切换知识库时解除旧 Word 绑定，避免跨项目覆盖文件。"""
        self._word_path = ""
        self._word_styles = []
        self._word_comments = []
        self._word_has_revisions = False
        self._word_dirty = False
        self._save_word_btn.setEnabled(False)
        self._comment_list.clear()
        self._render_comments()
        self._update_word_file_label()

    def _on_open_word(self):
        """打开 .docx：读入编辑器 + 绑定文件 + 刷新批注。"""
        if self._word_dirty:
            r = QMessageBox.question(
                self, "未保存的修改",
                "当前文档有未保存的修改，打开新文档将丢失这些修改。\n继续打开？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
        path, _ = QFileDialog.getOpenFileName(
            self, "打开 Word 文档", "", "Word 文档 (*.docx)")
        if not path:
            return
        try:
            from ..core.docx_io import read_docx
            content = read_docx(path)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "打开失败", f"无法读取 Word 文档：\n{e}")
            return
        if not self._cancel_all_workers():
            QMessageBox.warning(self, "请稍候", "当前 AI 任务尚未退出，请稍后再打开 Word 文档。")
            return

        self._word_path = path
        self._word_styles = list(content.styles)
        self._word_comments = content.comments
        self._word_has_revisions = content.has_revisions
        self._word_paragraphs_snapshot = list(content.paragraphs)  # 批注偏移校验基准
        self._word_dirty = False
        self._swap_editor_text(content.to_plain_text())
        self._update_word_file_label()
        self._save_word_btn.setEnabled(True)
        self._render_comments()
        if content.has_revisions:
            self._status_label.setText(
                "文档含修订标记（track changes），已合并显示；保存时按当前文本写回")
        else:
            self._status_label.setText(f"已打开 {os.path.basename(path)}")
        # 自动触发草稿整体评价（只读全文+风格指南，不读引用文献）
        QTimer.singleShot(200, self._start_auto_review)

    def _on_save_word(self):
        """把编辑器内容写回绑定的 Word 文档（段落级，保留批注）。"""
        if not self._word_path:
            QMessageBox.information(self, "提示", "请先打开一个 Word 文档。")
            return
        if self._rev_controller is not None and self._rev_controller.has_changes:
            r = QMessageBox.question(
                self, "存在未处理的 AI 修订",
                "编辑器中有未处理的 AI 修订。\n"
                "「是」= 全部接受后保存；「否」= 取消保存。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if r != QMessageBox.StandardButton.Yes:
                return
            self._rev_controller.accept_all()
        try:
            from ..core.docx_io import write_docx
            text = self.editor.toPlainText().replace("\u2029", "\n")
            paragraphs = text.split("\n")
            write_docx(self._word_path, paragraphs,
                       styles=self._word_styles, comments=self._word_comments)
        except Exception as e:  # noqa: BLE001
            QMessageBox.critical(self, "保存失败", f"无法写入 Word 文档：\n{e}")
            return
        self._word_dirty = False
        self._update_word_file_label()
        self._status_label.setText(f"已保存到 {os.path.basename(self._word_path)}")

    def _update_word_file_label(self):
        if not self._word_path:
            self._word_file_label.setText("未绑定文件")
            return
        name = os.path.basename(self._word_path)
        self._word_file_label.setText(
            f"● {name} 已修改未保存" if self._word_dirty else name)

    def _confirm_save_if_dirty(self) -> bool:
        """关闭/切换前询问是否保存 Word 修改。返回 True=可继续。"""
        if not self._word_path or not self._word_dirty:
            return True
        r = QMessageBox.question(
            self, "未保存的修改",
            f"「{os.path.basename(self._word_path)}」有未保存的修改，是否保存？",
            QMessageBox.StandardButton.Save | QMessageBox.StandardButton.Discard
            | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Save,
        )
        if r == QMessageBox.StandardButton.Save:
            self._on_save_word()
            return not self._word_dirty
        return r == QMessageBox.StandardButton.Discard

    # ================= AI 修订（人机双写） =================

    def _on_rev_changed(self):
        """修订锚点变化：刷新审阅工具栏状态。"""
        n = self._rev_controller.anchor_count if self._rev_controller else 0
        self._review_bar.setVisible(n > 0)
        self._rev_count_label.setText(f"修改: {n} 处")

    def _on_rev_accept_all(self):
        if self._rev_controller is not None:
            self._rev_controller.accept_all()

    def _on_rev_reject_all(self):
        if self._rev_controller is not None:
            self._rev_controller.reject_all()

    def _start_inline_polish(self, text: str, instruction: str):
        """选中即改：后台 LLM 润色 → 修订形式渲染进编辑器。

        携带草稿整体评价结论（review_findings）+ 选区段内批注，
        引文证据由 UnifiedWriter 按选区文本只读段内引文的 PDF。
        """
        if not self._text_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return
        if self._rev_worker is not None and self._rev_worker.isRunning():
            QMessageBox.information(self, "提示", "已有 AI 修改正在处理，请稍候。")
            return
        from ..core.unified_writer import UnifiedWriter

        # 加载已保存的整体评价结论（采纳过滤后）
        review_findings = ""
        if self._coach and self._coach.current_profile:
            from ..utils.config import load_review
            from ..core.draft_reviewer import DraftReviewer
            saved = load_review(self._coach.current_profile.name)
            if saved:
                review_findings = DraftReviewer.format_review_for_polish(saved)

        # 选区所在段落含批注 → 附加到修改要求（参考批注一并处理）
        comment_hints = self._get_comment_hints_for_span(*self._ai_bar_anchor)
        if comment_hints:
            instruction = instruction + "\n\n【该段落有导师批注，请一并参考处理】\n" + comment_hints

        worker = InlinePolishWorker(
            self._text_client, self._coach, text, instruction,
            self._current_writing_type, self._zotero,
            review_findings=review_findings)
        track(worker)
        self._rev_worker = worker
        worker.finished_signal.connect(self._on_inline_polish_done)
        worker.error_signal.connect(self._on_inline_polish_error)
        worker.start()
        self._status_label.setText("AI 正在修改选中文字（将读取该段引用的文献证据）...")

    def _get_comment_hints_for_span(self, start: int, end: int) -> str:
        """取覆盖指定字符范围的批注文本（用于选中即改时参考）。"""
        if not self._word_comments:
            return ""
        hints = []
        for c in self._word_comments:
            span = self._comment_span(c)
            if span is None:
                continue
            if not (span[1] <= start or end <= span[0]):  # 有重叠
                hints.append(f"· {c.author or '批注'}：{c.text}")
        return "\n".join(hints)

    def _on_inline_polish_done(self, result: dict):
        if self.sender() is not self._rev_worker:
            return
        self._rev_worker = None
        polished = result.get("polished_text", "")
        original = result.get("original_text", "")
        if not polished or polished == original:
            self._status_label.setText("AI 未产生修改")
            return
        # 用修订形式渲染：替换选区内容为 diff 渲染文本
        self._render_revision(original, polished)
        self._status_label.setText("AI 修改已就绪，可在编辑器上方审阅（接受/拒绝）")

    def _on_inline_polish_error(self, err: str):
        if self.sender() is not self._rev_worker:
            return
        self._rev_worker = None
        self._status_label.setText(f"AI 修改失败：{err}")

    def _render_revision(self, original: str, polished: str):
        """把 original→polished 的 diff 渲染进编辑器（替换选区）。"""
        start = getattr(self, "_pending_cursor_pos", -1)
        end = getattr(self, "_pending_cursor_end", -1)
        plain = self.editor.toPlainText().replace("\u2029", "\n")
        if not (0 <= start <= end <= len(plain)) or plain[start:end] != original:
            start = plain.find(original)
            if start < 0:
                start = len(plain)
            end = start + len(original)
        self._rev_controller._skip_recompute = True
        self.editor.blockSignals(True)
        try:
            # 先删除原选区，再在光标处插入 diff 渲染文本。
            cur = self.editor.textCursor()
            cur.setPosition(start)
            cur.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            self.editor.setTextCursor(cur)
            cur.removeSelectedText()
            from ..core.doc_diff import DocDiffController
            tmp_edit = QTextEdit()
            tmp_ctrl = DocDiffController(tmp_edit)
            tmp_ctrl.render(original, polished, highlight_citations=False)
            insert_cursor = self.editor.textCursor()
            insert_cursor.setPosition(start)
            self._copy_doc_into(tmp_edit.document(), insert_cursor, start)
        finally:
            self.editor.blockSignals(False)
            self._rev_controller._skip_recompute = False
        self._on_editor_text_changed()
        self._on_ai_highlight_schedule()
        self._rev_controller.recompute_anchors()

    def _copy_doc_into(self, src_doc: QTextDocument, cursor: QTextCursor, base: int):
        """把 src_doc 的带格式内容插入到 cursor 位置。"""
        block = src_doc.firstBlock()
        while block.isValid():
            it = block.begin()
            while not it.atEnd():
                frag = it.fragment()
                if frag is not None:
                    fmt = frag.charFormat()
                    text = frag.text()
                    if text:
                        cursor.insertText(text, fmt)
                it += 1
            if block.next().isValid():
                cursor.insertText("\n")
            block = block.next()

    # ================= 浮动 AI 操作条 =================

    def _on_editor_selection_changed(self) -> None:
        """选中文字后显示操作条；清空选区时立即收起。"""
        if not self.editor.textCursor().hasSelection():
            self._hide_ai_bar()
            return
        QTimer.singleShot(0, self._show_ai_bar)

    def _show_ai_bar(self):
        """选中文字后显示浮动 AI 操作条。"""
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._hide_ai_bar()
            return
        self._ai_bar_anchor = (cursor.selectionStart(), cursor.selectionEnd())
        if self._ai_bar is None:
            self._ai_bar = QFrame(self.editor)
            self._ai_bar.setStyleSheet(
                "QFrame { background-color: #ffffff; border: 1px solid #cfe5dd; "
                "border-radius: 8px; }"
            )
            lay = QHBoxLayout(self._ai_bar)
            lay.setContentsMargins(6, 4, 6, 4)
            lay.setSpacing(4)
            for label, tip in (("润色", "润色语言并核查引文"),
                               ("扩写", "扩写这段内容"),
                               ("改写", "按不同风格改写"),
                               ("翻译", "翻译为英文")):
                b = QPushButton(label)
                b.setObjectName("secondaryBtn")
                b.setToolTip(tip)
                b.clicked.connect(lambda _c=False, l=label: self._on_ai_bar_action(l))
                lay.addWidget(b)
            self._ai_input = QLineEdit()
            self._ai_input.setPlaceholderText("或输入自定义要求...")
            self._ai_input.setMaximumWidth(220)
            self._ai_input.returnPressed.connect(
                lambda: self._on_ai_bar_action(self._ai_input.text().strip()))
            lay.addWidget(self._ai_input)
            self._ai_bar.adjustSize()
        # 定位到选区上方
        rect = self.editor.cursorRect(cursor)
        max_width = max(240, self.editor.viewport().width() - 12)
        if self._ai_bar.width() > max_width:
            self._ai_bar.setFixedWidth(max_width)
        x = min(max(6, rect.x()), max(6, self.editor.viewport().width() - self._ai_bar.width() - 6))
        y = rect.y() - self._ai_bar.height() - 6
        if y < 0:
            y = rect.y() + self.editor.fontMetrics().height() + 6
        self._ai_bar.move(x, y)
        self._ai_bar.show()
        self._ai_bar.raise_()

    def _hide_ai_bar(self):
        if self._ai_bar is not None:
            self._ai_bar.hide()

    def _on_ai_bar_action(self, action: str):
        """浮动操作条动作：读取选区 → 后台润色 → 修订渲染。"""
        if not action:
            return
        cursor = self.editor.textCursor()
        start, end = self._ai_bar_anchor
        plain = self.editor.toPlainText().replace("\u2029", "\n")
        if not (0 <= start <= end <= len(plain)):
            self._hide_ai_bar()
            return
        text = plain[start:end]
        if not text.strip():
            self._hide_ai_bar()
            return
        self._pending_cursor_pos = start
        self._pending_cursor_end = end
        instruction = {
            "润色": "润色语言，去除口语化和机械化表达，保持学术风格",
            "扩写": "扩写这段内容，补充细节和论证",
            "改写": "改写这段内容，保持原意但换一种表达方式",
            "翻译": "翻译为英文学术表达",
        }.get(action, action)
        self._hide_ai_bar()
        self._start_inline_polish(text, instruction)

    # ================= 批注 =================

    def _comment_span(self, c) -> tuple[int, int] | None:
        """计算批注在编辑器全文中的字符范围（含段内偏移；偏移失效降级段落级）。

        精确偏移仅在「当前段落与打开时一致」时有效——用户改动过段落
        文本后偏移失去意义，自动降级为整段高亮。
        返回 (start, end)；无法定位返回 None。
        """
        if c.paragraph_index < 0:
            return None
        plain = self.editor.toPlainText().replace("\u2029", "\n")
        paragraphs = plain.split("\n")
        if c.paragraph_index >= len(paragraphs):
            return None
        # 段落起点
        para_start = sum(len(p) + 1 for p in paragraphs[:c.paragraph_index])
        para_text = paragraphs[c.paragraph_index]
        snapshot = getattr(self, "_word_paragraphs_snapshot", None)
        para_unchanged = (
            snapshot is not None
            and 0 <= c.paragraph_index < len(snapshot)
            and snapshot[c.paragraph_index] == para_text
        )
        if (para_unchanged and 0 <= c.char_start < c.char_end <= len(para_text)):
            return para_start + c.char_start, para_start + c.char_end
        # 段落被改动或偏移未解析 → 降级为整段
        return para_start, para_start + len(para_text)

    def _make_selection(self, start: int, end: int, bg_color: str) -> QTextEdit.ExtraSelection:
        """构造 ExtraSelection（PySide6 构造函数会丢失 cursor 选区，须属性赋值）。"""
        cursor = QTextCursor(self.editor.document())
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        fmt = QTextCharFormat()
        fmt.setBackground(QColor(bg_color))
        sel = QTextEdit.ExtraSelection()
        sel.cursor = cursor
        sel.format = fmt
        return sel

    def _render_comment_marks(self):
        """在编辑器中常驻显示批注标记（淡黄底，与修订绿/红区分）。

        与 AI 味禁词高亮共用 ExtraSelection 机制（setExtraSelections）。
        """
        if not self._word_comments:
            return
        selections: list = []
        for c in self._word_comments:
            span = self._comment_span(c)
            if span is None:
                continue
            selections.append(self._make_selection(span[0], span[1], "#fff3d6"))
        self._comment_marks = selections
        self._refresh_extra_selections()

    def _refresh_extra_selections(self):
        """合并批注常驻标记与批注定位高亮。"""
        marks = getattr(self, "_comment_marks", []) or []
        self.editor.setExtraSelections(marks + self._comment_highlights)

    def _render_comments(self):
        """渲染批注列表。"""
        self._comment_list.clear()
        if not self._word_comments:
            self._comment_status.setText("此文档没有批注")
            self._comment_ai_btn.setEnabled(False)
            self._comment_marks = []
            self._refresh_extra_selections()
            return
        self._comment_status.setText(f"共 {len(self._word_comments)} 条批注，点击可定位到批注处")
        self._comment_ai_btn.setEnabled(True)
        for i, c in enumerate(self._word_comments):
            author = c.author or "批注"
            text = (c.text or "").strip()
            shown = text[:40] + ("…" if len(text) > 40 else "")
            item = QListWidgetItem(f"{author}：{shown}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            item.setToolTip(f"{author}\n{text}")
            self._comment_list.addItem(item)
        self._render_comment_marks()

    def _on_comment_clicked(self, item):
        """点击批注 → 精确定位并高亮对应句子（偏移失效时降级段落）。"""
        idx = item.data(Qt.ItemDataRole.UserRole)
        if idx is None or not (0 <= idx < len(self._word_comments)):
            return
        c = self._word_comments[idx]
        span = self._comment_span(c)
        if span is None:
            return
        cursor = self.editor.textCursor()
        cursor.setPosition(span[0])
        self.editor.setTextCursor(cursor)
        self.editor.setFocus()
        self._highlight_range(span[0], span[1])

    def _highlight_range(self, start: int, end: int):
        """临时深色高亮（批注定位用），与常驻淡黄标记叠加。"""
        self._comment_highlights = [self._make_selection(start, end, "#ffe9a8")]
        self._refresh_extra_selections()
        QTimer.singleShot(3000, self._clear_comment_highlight)

    def _clear_comment_highlight(self):
        self._comment_highlights = []
        self._refresh_extra_selections()

    def _on_ai_fix_comments(self):
        """AI 按批注修改：把批注+锚定段落发给 LLM → 修订渲染。"""
        if not self._word_comments:
            return
        if not self._text_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return
        if self._comment_worker is not None and self._comment_worker.isRunning():
            QMessageBox.information(self, "提示", "AI 正在处理批注，请稍候。")
            return
        plain = self.editor.toPlainText().replace("\u2029", "\n")
        paragraphs = plain.split("\n")
        # 收集有锚定段落的批注
        items = []
        for c in self._word_comments:
            if 0 <= c.paragraph_index < len(paragraphs):
                items.append({
                    "author": c.author or "",
                    "text": c.text or "",
                    "paragraph": paragraphs[c.paragraph_index],
                    "paragraph_index": c.paragraph_index,
                })
        if not items:
            QMessageBox.information(self, "提示", "批注未锚定到具体段落，无法自动修改。")
            return
        worker = CommentFixWorker(self._text_client, items,
                                  coach=self._coach, zotero=self._zotero,
                                  writing_type=self._current_writing_type)
        track(worker)
        self._comment_worker = worker
        worker.finished_signal.connect(self._on_comment_fix_done)
        worker.error_signal.connect(self._on_comment_fix_error)
        worker.start()
        self._status_label.setText("AI 正在按批注修改...")

    def _on_comment_fix_done(self, result: dict):
        if self.sender() is not self._comment_worker:
            return
        self._comment_worker = None
        changes = result.get("changes", [])
        if not changes:
            self._status_label.setText("AI 未对批注产生修改")
            return
        # 逐段渲染修订
        plain = self.editor.toPlainText().replace("\u2029", "\n")
        paragraphs = plain.split("\n")
        for ch in changes:
            pi = ch.get("paragraph_index", -1)
            new_text = ch.get("new_text", "")
            if not (0 <= pi < len(paragraphs)) or not new_text:
                continue
            original = paragraphs[pi]
            if new_text == original:
                continue
            # 定位段落起点
            pos = 0
            for _ in range(pi):
                nxt = plain.find("\n", pos)
                if nxt < 0:
                    break
                pos = nxt + 1
            end = plain.find("\n", pos)
            if end < 0:
                end = len(plain)
            self._pending_cursor_pos = pos
            self._pending_cursor_end = end
            self._render_revision(original, new_text)
        self._status_label.setText("批注修改已就绪，可在编辑器上方审阅")

    def _on_comment_fix_error(self, err: str):
        if self.sender() is not self._comment_worker:
            return
        self._comment_worker = None
        self._status_label.setText(f"批注处理失败：{err}")

    def _on_ai_highlight_schedule(self):
        """输入后防抖调度 AI 味禁词标黄刷新。"""
        self._ai_highlight_timer.start(300)

    def _refresh_ai_highlight(self):
        """本地检测并标黄疑似 AI 味词（零 LLM 调用）。"""
        from ..core.ai_words import match_ai_words
        text = self.editor.toPlainText()
        matches = match_ai_words(text)
        selections: list = []
        for start, end in matches:
            sel = self._make_selection(start, end, "#fff0d6")
            sel.format.setForeground(QColor("#a76d2b"))
            selections.append(sel)
        self._ai_word_selections = selections
        self._refresh_extra_selections()
        if matches:
            self._word_count_label.setText(f"字数: {len(text)} | 疑似AI味: {len(matches)}")

    def _refresh_extra_selections(self):
        """合并 AI 味标黄 + 批注常驻标记 + 批注定位高亮。"""
        ai_sel = getattr(self, "_ai_word_selections", []) or []
        marks = getattr(self, "_comment_marks", []) or []
        self.editor.setExtraSelections(ai_sel + marks + self._comment_highlights)

    def _on_new_kb(self):
        name, ok = QInputDialog.getText(
            self, "新建知识库", "知识库名称：",
        )
        if ok and name.strip():
            try:
                if not self._confirm_save_if_dirty():
                    return
                self._auto_save_draft()
                if not self._cancel_all_workers():
                    QMessageBox.warning(self, "请稍候", "当前 AI 任务尚未退出，请稍后再新建知识库。")
                    return
                profile = self._coach.create_profile(
                    name.strip(), self._current_writing_type
                )
                self._clear_word_binding()
                self._refresh_kb_dropdown()
                self._load_draft(replace=True)
                self._status_label.setText(f"已创建知识库: {name.strip()}")
            except (ValueError, OSError) as e:
                QMessageBox.warning(self, "创建失败", str(e))

    def _on_delete_kb(self):
        if not self._coach.current_profile:
            return
        name = self._coach.current_profile.name
        r = QMessageBox.question(
            self, "确认删除",
            f"删除知识库「{name}」及其所有关联论文数据？\n此操作不可恢复。"
        )
        if r == QMessageBox.StandardButton.Yes:
            if not self._confirm_save_if_dirty() or not self._cancel_all_workers():
                return
            self._coach.delete_profile(name)
            self._clear_word_binding()
            self._refresh_kb_dropdown()
            self._load_draft(replace=True)
            self._status_label.setText(f"已删除知识库: {name}")

    def _on_add_sample_paper(self):
        if not self._coach.current_profile:
            QMessageBox.warning(self, "提示", "请先选择或创建一个知识库")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择写作范文 PDF", "", "PDF 文件 (*.pdf);;所有文件 (*.*)"
        )
        for path in paths:
            result = self._coach.add_sample_paper(path)
            if result:
                self._status_label.setText(f"已添加写作范文: {result['filename']}")
            else:
                self._status_label.setText(f"添加失败: {os.path.basename(path)}")
        self._refresh_kb_dropdown()

    def _on_add_journal_paper(self):
        if not self._coach.current_profile:
            QMessageBox.warning(self, "提示", "请先选择或创建一个知识库")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择期刊范文 PDF", "", "PDF 文件 (*.pdf);;所有文件 (*.*)"
        )
        for path in paths:
            result = self._coach.add_journal_paper(path)
            if result:
                self._status_label.setText(f"已添加期刊范文: {result['filename']}")
            else:
                self._status_label.setText(f"添加失败: {os.path.basename(path)}")
        self._refresh_kb_dropdown()

    def _on_generate_style_guide(self):
        """生成风格指南（Phase 2）。"""
        if not self._coach.current_profile:
            return
        if self._coach.current_profile.total_papers == 0:
            QMessageBox.warning(self, "提示", "请先添加写作范文或期刊范文")
            return
        if not self._text_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return

        self._style_btn.setEnabled(False)
        self._style_btn.setText("正在分析...")
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)  # 不确定进度条
        self._status_label.setText("正在分析写作风格（可能需要 30-60 秒）...")
        QApplication.processEvents()

        self._style_worker = StyleGuideWorker(self._coach, self._text_client)
        track(self._style_worker)
        self._style_worker.finished_signal.connect(self._on_style_guide_ready)
        self._style_worker.error_signal.connect(self._on_style_guide_error)
        self._style_worker.progress_signal.connect(
            lambda msg: self._status_label.setText(msg)
        )
        self._style_worker.start()

    def _on_view_style_guide(self):
        """再次查看已生成的风格分析结果。"""
        if not self._coach or not self._coach.current_profile:
            return
        profile = self._coach.current_profile
        if not profile.has_writing_habits and not profile.has_journal_style:
            QMessageBox.information(self, "提示",
                "尚未生成风格指南，请先添加范文后点击\u201c生成风格指南\u201d。")
            return
        dialog = StyleGuideDialog(profile, parent=self)
        dialog.exec()

    def _on_style_guide_ready(self, guide: dict):
        if self.sender() is not self._style_worker:
            return
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._style_btn.setEnabled(True)
        self._style_btn.setText("重新生成风格指南")
        self._update_kb_status()
        self._status_label.setText("风格分析完成")

        profile = self._coach.current_profile
        dialog = StyleGuideDialog(profile, parent=self)
        dialog.exec()

    def _on_style_guide_error(self, err: str):
        if self.sender() is not self._style_worker:
            return
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._style_btn.setEnabled(True)
        self._style_btn.setText("生成风格指南")
        self._status_label.setText(f"风格分析失败: {err[:60]}")
        QMessageBox.warning(self, "风格分析失败", err)

    # ---- 按钮 1: AI 润色与核查 ----

    @staticmethod
    def _count_citation_markers(text: str) -> int:
        """本地统计引文标记数量（作为 LLM 引文提取的参考数量，替代手输）。"""
        import re
        seen: set[str] = set()
        for m in re.finditer(r'\([^)]+?,\s*\d{4}[a-z]?\)', text):
            seen.add("a:" + m.group(0).lower())
        for m in re.finditer(r'（[^）]+?，\s*\d{4}[a-z]?）', text):
            seen.add("cn:" + m.group(0))
        for m in re.finditer(r'\[(\d+(?:[,\-]\d+)*)\]', text):
            for part in re.split(r'[,，\-]', m.group(1)):
                if part.strip().isdigit():
                    seen.add(f"n:[{part.strip()}]")
        for m in re.finditer(r'[A-Z][a-z]+等（\d{4}）', text):
            seen.add("cn:" + m.group(0))
        for m in re.finditer(r'[A-Z]\w+(?:\s+(?:et al\.|& [A-Z]\w+))?,\s*\d{4}[a-z]?', text):
            seen.add("a:" + m.group(0).lower())
        return len(seen) or 8

    def _on_unified_polish(self):
        """统一润色 + 引文核查（含草稿评价诊断）。"""
        self._verify_only = False
        self._run_polish_flow("polish")

    def _on_verify_only(self):
        """仅核查引文：展示核查结果，不改动编辑器正文。"""
        self._verify_only = True
        self._run_polish_flow("verify")

    def _on_cn2en(self):
        """中译英 —— 翻译并润色为英文学术片段。"""
        self._verify_only = False
        self._run_polish_flow("cn2en")

    def _run_polish_flow(self, mode: str = "polish"):
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            self._verify_only = False
            QMessageBox.warning(self, "提示", "请先在编辑器中选中要处理的文字")
            return
        if not self._text_client:
            self._verify_only = False
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return

        text = cursor.selectedText().strip()
        # 归一化 Qt 富文本换行符（QTextEdit 内部使用 \u2029 而非 \n）
        text = text.replace('\u2029', '\n')
        self._pending_original = text
        self._pending_cursor_pos = cursor.selectionStart()
        self._pending_cursor_end = cursor.selectionEnd()

        # 中译英：不走引文核查流程，直接后台处理
        if mode == "cn2en":
            self._set_ai_buttons_busy(True)
            self._progress_bar.setVisible(True)
            self._progress_bar.setRange(0, 0)
            self._cancel_btn.setVisible(True)
            self._progress_bar.setFormat("正在中译英...")
            self._status_label.setText("AI 正在翻译润色为英文...")
            QApplication.processEvents()
            self._start_polish_worker(text, "", "", mode)
            return

        # 加载已保存的草稿评价
        review_findings = ""
        if self._coach and self._coach.current_profile:
            from ..utils.config import load_review
            saved_review = load_review(self._coach.current_profile.name)
            if saved_review:
                from ..core.draft_reviewer import DraftReviewer
                review_findings = DraftReviewer.format_review_for_polish(saved_review)
                if review_findings:
                    self._status_label.setText("已加载草稿评价，将一并处理评价发现的问题")

        # Zotero 可用性检查
        zotero_ok = self._zotero and hasattr(self._zotero, 'get_all_items') and len(self._zotero.get_all_items()) > 0
        if not zotero_ok:
            answer = QMessageBox.question(
                self, "Zotero 未连接",
                "当前 Zotero 文献库未加载到有效文献，引文核查将跳过。\n\n"
                "如需引文核查，请先在工具栏中设置 Zotero 数据目录（保证目录下有 zotero.sqlite 和 storage/）。\n\n"
                "是否继续（仅润色，不核查引文）？",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
            )
            if answer != QMessageBox.StandardButton.Yes:
                self._verify_only = False
                return

        # 本地统计引文标记数量 → 后台 LLM 识别引文（无需手输）
        citation_count = self._count_citation_markers(text)
        self._status_label.setText(
            f"检测到约 {citation_count} 处引文标记，AI 正在解析..."
        )

        self._set_ai_buttons_busy(True)
        self._progress_bar.setVisible(True)
        self._cancel_btn.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._progress_bar.setFormat("AI 正在解析草稿中的引文...")
        self._status_label.setText("AI 正在解析草稿中的引文...")
        QApplication.processEvents()

        self._citation_worker = CitationExtractWorker(
            self._text_client, text, citation_count
        )
        track(self._citation_worker)
        self._citation_worker.finished_signal.connect(
            lambda citations: self._on_citations_extracted(citations, text, review_findings)
        )
        self._citation_worker.error_signal.connect(
            lambda err: self._on_citation_extract_error(err, text, review_findings)
        )
        self._citation_worker.start()

    def _on_citation_extract_error(self, error: str, text: str, review_findings: str):
        if self.sender() is not self._citation_worker:
            return
        self._progress_bar.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._set_ai_buttons_busy(False)
        self._status_label.setText("引文解析失败")

        answer = QMessageBox.question(
            self, "引文解析失败",
            f"AI 未能完成引文解析：{error[:200]}\n\n"
            "可能原因：\n"
            "  1. API 连接异常或 Key 无效\n"
            "  2. 草稿文本过长\n"
            "  3. 服务暂不可用\n\n"
            "重试 / 跳过引文核查直接润色 / 取消？",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel
        )

        if answer == QMessageBox.StandardButton.Yes:
            # 重试引文解析：保持当前模式与 verify_only 语义不变
            # （调 _on_unified_polish 会把仅核查静默变成完整润色）
            self._set_ai_buttons_busy(True)
            self._progress_bar.setVisible(True)
            self._progress_bar.setRange(0, 0)
            self._cancel_btn.setVisible(True)
            self._progress_bar.setFormat("AI 正在解析草稿中的引文...")
            self._status_label.setText("重试解析草稿中的引文...")
            QApplication.processEvents()
            self._citation_worker = CitationExtractWorker(
                self._text_client, text, self._count_citation_markers(text)
            )
            track(self._citation_worker)
            self._citation_worker.finished_signal.connect(
                lambda citations: self._on_citations_extracted(citations, text, review_findings)
            )
            self._citation_worker.error_signal.connect(
                lambda err: self._on_citation_extract_error(err, text, review_findings)
            )
            self._citation_worker.start()
        elif answer == QMessageBox.StandardButton.No:
            self._progress_bar.setVisible(True)
            self._progress_bar.setRange(0, 0)
            self._progress_bar.setFormat("正在润色修改...")
            self._status_label.setText("跳过引文核查，直接润色...")
            QApplication.processEvents()
            self._start_polish_worker(text, "", review_findings)

    def _on_citations_extracted(self, citations: list, text: str, review_findings: str):
        """LLM 引文识别完成 → 后台读取 Zotero 证据 → 启动润色。"""
        if self.sender() is not self._citation_worker:
            return  # 已取消的旧线程迟到结果，不再重启润色流程
        self._citation_worker = None
        try:
            # LLM 返回的字段类型不可信：null/数字都会让 UI 线程抛异常
            # 并把 AI 按钮永久卡在 busy 态，这里统一规范化
            clean: list[dict] = []
            for c in citations or []:
                if not isinstance(c, dict):
                    continue
                clean.append({
                    "author_hint": str(c.get("author_hint") or "").strip(),
                    "year_hint": str(c.get("year_hint") or "").strip(),
                    "original_marker": str(c.get("original_marker") or "?"),
                })
            citations = clean

            self._status_label.setText(f"已识别 {len(citations)} 处引文标记")
            if not self._zotero or not citations:
                self._start_polish_worker(text, "", review_findings)
                return

            self._evidence_worker = CitationEvidenceWorker(text, citations, self._zotero)
            track(self._evidence_worker)
            self._evidence_worker.finished_signal.connect(
                lambda sources: self._on_citation_evidence_done(
                    sources, text, review_findings))
            self._evidence_worker.error_signal.connect(
                lambda err: self._on_citation_evidence_error(
                    err, text, review_findings))
            self._status_label.setText("正在后台读取引文对应的 PDF 证据...")
            self._evidence_worker.start()
        except Exception as e:  # noqa: BLE001
            self._progress_bar.setVisible(False)
            self._progress_bar.setRange(0, 100)
            self._cancel_btn.setVisible(False)
            self._set_ai_buttons_busy(False)
            self._status_label.setText(f"引文上下文构建失败：{e}")
            QMessageBox.warning(self, "引文上下文构建失败", str(e))

    def _on_citation_evidence_done(
        self, sources: str, text: str, review_findings: str,
    ) -> None:
        if self.sender() is not self._evidence_worker:
            return
        self._evidence_worker = None
        self._start_polish_worker(text, sources, review_findings)

    def _on_citation_evidence_error(
        self, error: str, text: str, review_findings: str,
    ) -> None:
        if self.sender() is not self._evidence_worker:
            return
        self._evidence_worker = None
        self._status_label.setText(f"引文证据读取失败，将继续处理：{error[:80]}")
        self._start_polish_worker(text, "", review_findings)

    def _start_polish_worker(self, text: str, pre_citation_sources: str,
                             review_findings: str = "", mode: str = "polish"):
        """启动润色 worker（统一入口）。"""
        if mode == "cn2en":
            fmt = "正在中译英..."
        else:
            fmt = "正在核查引文..." if self._verify_only else "正在润色修改..."
        self._progress_bar.setFormat(fmt)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._set_ai_buttons_busy(True)
        QApplication.processEvents()

        self._unified_worker = UnifiedWorker(
            self._text_client, text, self._coach, self._zotero,
            self._current_writing_type, pre_citation_sources,
            review_findings=review_findings,
            verify_only=self._verify_only,
            mode=mode,
        )
        track(self._unified_worker)
        self._unified_worker.finished_signal.connect(self._on_unified_done)
        self._unified_worker.error_signal.connect(self._on_unified_error)
        self._unified_worker.start()

    def _on_unified_done(self, result: dict):
        if self.sender() is not self._unified_worker:
            return  # 已取消的旧线程迟到结果，丢弃
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._cancel_btn.setVisible(False)
        self._set_ai_buttons_busy(False)
        self._status_label.setText("\u5904\u7406\u5b8c\u6210")

        if result.get("error"):
            QMessageBox.warning(self, "\u5904\u7406\u5931\u8d25", result["error"])
            return

        original = getattr(self, '_pending_original', '')
        polished = result.get("polished_text", "")
        if self._verify_only:
            from .diff_dialog import DiffDialog
            dialog = DiffDialog(
                original=original,
                polished=original,
                citation_notes=result.get("citation_notes", []),
                supervisor_notes=result.get("supervisor_notes", []),
                modification_log=[],
                logic_issues=result.get("logic_issues", []),
                citation_sources_text=result.get("citation_sources_text", ""),
                write_client=self._text_client,
                coach=self._coach,
                zotero=self._zotero,
                writing_type=self._current_writing_type,
                parent=None,
            )
            dialog.setWindowTitle("引文核查结果")
            self._track_dialog(dialog)
            dialog.show()
            self._verify_only = False
            return
        if not polished.strip() and not result.get("error"):
            QMessageBox.warning(
                self, "\u6da6\u8272\u7ed3\u679c\u4e3a\u7a7a",
                "LLM \u8fd4\u56de\u4e86\u7a7a\u7684\u6da6\u8272\u7ed3\u679c\u3002\n\n"
                "\u53ef\u80fd\u539f\u56e0\uff1a\n"
                "1. \u9009\u4e2d\u7684\u6587\u5b57\u8fc7\u957f\uff0c\u8d85\u51fa\u6a21\u578b\u5904\u7406\u4e0a\u9650\n"
                "2. API \u914d\u989d\u7528\u5c3d\n"
                "3. \u6a21\u578b\u4e0d\u652f\u6301\u8be5\u8bf7\u6c42\u683c\u5f0f\n\n"
                "\u5efa\u8bae\uff1a\u7f29\u77ed\u9009\u4e2d\u6587\u5b57\u540e\u91cd\u8bd5\uff0c\u6216\u68c0\u67e5 API \u914d\u7f6e\u3002"
            )
            return

        from .diff_dialog import DiffDialog
        dialog = DiffDialog(
            original=original,
            polished=polished,
            citation_notes=result.get("citation_notes", []),
            supervisor_notes=result.get("supervisor_notes", []),
            modification_log=result.get("modification_log", []),
            logic_issues=result.get("logic_issues", []),
            citation_sources_text=result.get("citation_sources_text", ""),
            write_client=self._text_client,
            coach=self._coach,
            zotero=self._zotero,
            writing_type=self._current_writing_type,
            parent=None,
        )
        dialog.accepted_signal.connect(lambda text: self._on_diff_accepted(text, result, original))
        self._track_dialog(dialog)
        dialog.show()

    def _on_diff_accepted(self, text: str, result: dict, original: str):
        # 润色等待期间编辑器可能已被改动：原选区位置失效时不能盲目替换，
        # 先校验区间文本，不一致则按原文重定位，彻底找不到就追加到文末。
        plain = self.editor.toPlainText().replace('\u2029', '\n')
        start = getattr(self, '_pending_cursor_pos', -1)
        end = getattr(self, '_pending_cursor_end', -1)
        if not (0 <= start <= end <= len(plain)) or plain[start:end] != original:
            start = plain.find(original)
            if start < 0:
                cursor = self.editor.textCursor()
                cursor.movePosition(QTextCursor.MoveOperation.End)
                self.editor.setTextCursor(cursor)
                cursor.insertText(("\n\n" if plain.strip() else "") + text)
                self._status_label.setText("原选区已被修改，润色结果已追加到文末")
                self._save_polish_history(result, original, final_text=text)
                return
            end = start + len(original)
            self._status_label.setText("原选区位置已变动，已按原文重新定位替换")
        cursor = self.editor.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self.editor.setTextCursor(cursor)
        cursor.insertText(text)
        self._status_label.setText("润色文本已替换")
        self._save_polish_history(result, original, final_text=text)

    def _on_unified_error(self, err: str):
        if self.sender() is not self._unified_worker:
            return
        self._progress_bar.setVisible(False)
        self._cancel_btn.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._set_ai_buttons_busy(False)
        self._status_label.setText(f"\u5904\u7406\u5931\u8d25: {err[:60]}")
        QMessageBox.warning(self, "\u5904\u7406\u5931\u8d25", err)

    # ---- 按钮 2: 文献补充 ----

    def _on_lit_search(self):
        """文献补充：LLM 分析 → 用户反馈 → PubMed 检索。"""
        draft = self.editor.toPlainText().strip()
        if not draft:
            QMessageBox.warning(self, "提示", "请先编写草稿")
            return
        if not self._text_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return

        from .lit_search_dialog import LitSearchDialog
        dialog = LitSearchDialog(self._text_client, self._coach,
                                 pool=self._zotero_pool(), parent=None)
        dialog.set_draft_text(draft)
        dialog.insert_requested.connect(self._on_lit_insert)
        dialog.feed_requested.connect(self.feed_requested.emit)
        dialog.search_done.connect(self.lit_search_completed.emit)
        self._track_dialog(dialog)
        dialog.show()

    def _zotero_pool(self) -> list[dict]:
        """Zotero 条目快照（检索结果滤除库内已有），与检索工作台同口径。"""
        lib = self._zotero
        if lib is None or not lib.is_available:
            return []
        try:
            items = lib.get_all_items()
        except Exception:  # noqa: BLE001
            return []
        pool = []
        for it in items:
            first_last = it.first_author_last if getattr(it, 'authors', None) else ""
            pool.append({
                "key": it.key, "title": it.title, "doi": it.doi,
                "authors": first_last, "year": it.year,
                "collections": [],
            })
        return pool

    def _on_lit_insert(self, marker: str):
        cursor = self.editor.textCursor()
        cursor.insertText(marker)
        self._status_label.setText("已插入文献引用标记")

    # ---- 按钮 3: 草稿整体评价 ----

    def _start_auto_review(self):
        """打开 Word 后自动触发草稿整体评价（不读引用文献，只用风格指南）。

        评价不依赖知识库：无知识库/无风格指南时照常执行（仅缺基准参照），
        只在状态栏提示，不阻断。
        """
        draft = self.editor.toPlainText().strip()
        if not draft:
            self._status_label.setText("文档为空，跳过整体评价")
            return
        if not self._text_client:
            self._status_label.setText("未配置写作 API，跳过整体评价")
            return
        if self._review_worker is not None and self._review_worker.isRunning():
            return

        hint = ""
        if not self._coach or not self._coach.current_profile:
            hint = "（知识库未配置，评价仅供参考）"
        else:
            profile = self._coach.current_profile
            if not profile.has_writing_habits and not profile.has_journal_style:
                hint = "（知识库无风格基准，评价仅供参考）"

        self._set_ai_buttons_busy(True)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._cancel_btn.setVisible(True)
        self._status_label.setText(
            "AI 正在整体评价草稿（可能需要 30-60 秒）..." + hint)
        QApplication.processEvents()

        self._review_worker = DraftReviewWorker(
            self._text_client, draft, self._coach
        )
        track(self._review_worker)
        self._review_worker.finished_signal.connect(self._on_review_done)
        self._review_worker.error_signal.connect(self._on_review_error)
        self._review_worker.start()

    def _on_review_done(self, result: dict):
        if self.sender() is not self._review_worker:
            return
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._cancel_btn.setVisible(False)
        self._set_ai_buttons_busy(False)
        self._status_label.setText("整体评价完成，可采纳/编辑评价结论（将影响后续润色）")

        profile_name = ""
        if self._coach and self._coach.current_profile:
            profile_name = self._coach.current_profile.name

        from .review_dialog import ReviewDialog
        dialog = ReviewDialog(result, profile_name=profile_name, parent=None)
        self._track_dialog(dialog)
        dialog.show()

    def _on_review_error(self, err: str):
        if self.sender() is not self._review_worker:
            return
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._cancel_btn.setVisible(False)
        self._set_ai_buttons_busy(False)
        self._status_label.setText(f"整体评价失败: {err[:60]}")
        QMessageBox.warning(self, "整体评价失败", err)

    # ---- 润色历史 ----

    def _on_polish_history(self):
        """查看当前知识库的润色历史。"""
        if not self._coach or not self._coach.current_profile:
            QMessageBox.warning(self, "提示", "请先选择知识库")
            return
        from ..utils.config import load_polish_history
        from .polish_history_dialog import PolishHistoryDialog
        history = load_polish_history(self._coach.current_profile.name)
        dialog = PolishHistoryDialog(history, parent=None)
        dialog.insert_requested.connect(self._on_history_insert)
        self._track_dialog(dialog)
        dialog.show()

    def _on_history_insert(self, text: str):
        cursor = self.editor.textCursor()
        cursor.insertText(text)
        self._status_label.setText("已插入润色历史文本")

    # ---- Zotero ----

    def refresh_zotero_status(self):
        """公开入口：Zotero 数据变更后刷新状态显示。"""
        self._update_zotero_status()

    def _update_zotero_status(self):
        if self._zotero and self._zotero.is_available:
            count = self._zotero.item_count if hasattr(self._zotero, 'item_count') else 0
            text = f"已连接 · {count} 篇文献"
            data_dir = getattr(self._zotero, "data_dir", "") or ""
            if data_dir:
                text += f"\n{data_dir}"
            self._zotero_status_label.setText(text)
            self._zotero_status_label.setProperty("status", "ready")
        else:
            self._zotero_status_label.setText("未连接 · 引文核查不可用")
            self._zotero_status_label.setProperty("status", "warning")
        self._zotero_status_label.style().unpolish(self._zotero_status_label)
        self._zotero_status_label.style().polish(self._zotero_status_label)

    # ---- 知识库状态 ----

    def _update_kb_status(self):
        profile = self._coach.current_profile
        if profile:
            habits_ok = "已生成" if profile.has_writing_habits else "未生成"
            journal_ok = "已生成" if profile.has_journal_style else "未生成"
            lines = [
                f"名称: {profile.name}",
                f"类型: {profile.writing_type}",
                f"写作范文: {profile.personal_count} 篇",
                f"期刊范文: {profile.journal_count} 篇",
                f"写作习惯: {habits_ok}",
                f"期刊格式: {journal_ok}",
            ]
            self._kb_status_label.setText("\n".join(lines))
            self._style_btn.setEnabled(profile.total_papers > 0)
            has_guide = profile.has_writing_habits or profile.has_journal_style
            self._view_style_btn.setEnabled(has_guide)
        else:
            self._kb_status_label.setText("未选择知识库\n请在下拉菜单中选择或新建")
            self._style_btn.setEnabled(False)
            self._view_style_btn.setEnabled(False)

    # ---- 生命周期 ----

    def _cancel_all_workers(self, wait_ms: int = 3000) -> bool:
        """协作式取消所有后台 AI 处理线程，不强杀正在执行网络请求的线程。"""
        attrs = ("_citation_worker", "_evidence_worker", "_unified_worker",
                 "_review_worker", "_style_worker", "_rev_worker",
                 "_comment_worker")
        all_stopped = True
        for attr in attrs:
            w = getattr(self, attr)
            if w is None:
                continue
            if w.isRunning():
                w.requestInterruption()
                if not w.wait(wait_ms):
                    all_stopped = False
                    continue
            setattr(self, attr, None)
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._cancel_btn.setVisible(False)
        self._set_ai_buttons_busy(False)
        has_style_source = bool(
            self._coach.current_profile
            and self._coach.current_profile.total_papers > 0
        )
        self._style_btn.setEnabled(has_style_source)
        self._style_btn.setText("重新生成风格指南" if has_style_source else "生成风格指南")
        self._status_label.setText("已取消")
        return all_stopped

    def shutdown(self) -> bool:
        """清理后台线程并保存草稿。"""
        if not self._confirm_save_if_dirty():
            return False
        self._auto_save_draft()
        self._auto_save_timer.stop()
        if not self._cancel_all_workers():
            self._auto_save_timer.start(30_000)
            self._status_label.setText("仍有后台任务未退出，请稍后再关闭窗口")
            return False
        for d in list(self._active_dialogs):
            try:
                d.close()
                d.deleteLater()
            except Exception:
                pass
        self._active_dialogs.clear()
        return True

    def _auto_save_draft(self):
        """自动保存编辑器草稿到磁盘。

        只有用户真实修改过（_draft_dirty）才落盘：启动后尚未加载草稿、
        或程序性 setPlainText 都不触发保存，避免空文本/旧库内容覆盖。
        """
        try:
            if self._coach and self._coach.current_profile and self._draft_dirty:
                text = self.editor.toPlainText()
                from ..utils.config import save_draft
                save_draft(self._coach.current_profile.name, text)
                self._draft_dirty = False
                self.draft_saved.emit(len(text))
        except Exception:
            pass

    def _swap_editor_text(self, text: str) -> None:
        """程序性替换编辑器全文：不记为用户修改，不触发高亮防抖。"""
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self._draft_dirty = False
        self._word_count_label.setText(f"字数: {len(text)}")
        if self._word_path:
            self._update_word_file_label()
        self._draft_dirty = False
        self._refresh_ai_highlight()
        # 程序性替换后清空修订状态（只重置锚点，不触碰已写入的内容）
        if self._rev_controller is not None:
            self._rev_controller._change_anchors = []
            self._rev_controller._current_anchor_idx = -1
            self._on_rev_changed()

    def _load_draft(self, replace: bool = False):
        """加载当前知识库的编辑器草稿。

        Args:
            replace: True 时无条件替换编辑器内容（切换知识库场景，
                     旧库内容不能残留进新库）；False 时仅编辑器为空才恢复。
        """
        try:
            if self._coach and self._coach.current_profile:
                from ..utils.config import load_draft
                text = load_draft(self._coach.current_profile.name)
                if not text:
                    if replace:
                        self._swap_editor_text("")
                    return
                if replace or not self.editor.toPlainText().strip():
                    self._swap_editor_text(text)
                    self._status_label.setText("已恢复上次草稿")
        except Exception:
            pass

    def _save_polish_history(self, result: dict, original: str = "",
                             final_text: str = ""):
        """保存润色结果到历史记录（优先保存用户在 diff 中最终采纳的文本）。"""
        try:
            if self._coach and self._coach.current_profile and not result.get("error"):
                from datetime import datetime
                from ..utils.config import save_polish_entry
                entry = {
                    "timestamp": datetime.now().isoformat(),
                    "original": original,
                    "polished_text": final_text or result.get("polished_text", ""),
                    "citation_notes": result.get("citation_notes", []),
                    "supervisor_notes": result.get("supervisor_notes", []),
                    "logic_issues": result.get("logic_issues", []),
                }
                save_polish_entry(self._coach.current_profile.name, entry)
                self.polish_accepted.emit(
                    len(original),
                    len(final_text or result.get("polished_text", "")))
        except Exception:
            pass

    def get_editor_text(self) -> str:
        return self.editor.toPlainText()

    def set_editor_text(self, text: str):
        self.editor.setPlainText(text)
