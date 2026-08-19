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
    QSizePolicy, QListWidget, QListWidgetItem, QApplication, QMenu,
    QLineEdit, QDialog, QDialogButtonBox,
)
from PySide6.QtCore import Qt, Signal, QThread, QSize, QTimer
from PySide6.QtGui import QFont, QTextCursor, QColor, QTextCharFormat

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.zotero_parser import ZoteroLibrary
    from ..core.writing_coach import WritingCoach, WritingProfile


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
    status_message = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._write_client: LLMClient | None = None
        self._zotero: ZoteroLibrary | None = None
        self._coach = self._create_coach()
        self._current_writing_type = "综述"
        self._verify_only = False
        self._style_worker: StyleGuideWorker | None = None
        self._unified_worker: UnifiedWorker | None = None
        self._review_worker: DraftReviewWorker | None = None
        self._citation_worker: CitationExtractWorker | None = None
        self._active_dialogs: list = []  # 保持非模态对话框引用防止被GC
        self._draft_dirty = False  # 仅用户真实改动后才允许自动保存落盘

        self._setup_ui()
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
        for b in (self._polish_btn, self._verify_btn, self._review_btn,
                  self._cn2en_btn):
            if b is not None:
                b.setEnabled(not busy)
        if not busy:
            self._polish_btn.setText("AI 润色与核查")
            self._review_btn.setText("草稿整体评价")

    # ---- 注入依赖 ----

    def set_write_client(self, client: "LLMClient | None"):
        self._write_client = client

    def set_zotero_library(self, zotero: "ZoteroLibrary | None"):
        self._zotero = zotero
        self._update_zotero_status()

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

        self.editor = QTextEdit()
        self.editor.setObjectName("draftEditor")
        self.editor.setPlaceholderText(
            "从这里开始写作...\n\n"
            "建议先选择写作类型和知识库，再把需要润色或核查的段落选中。"
        )
        self.editor.textChanged.connect(self._on_editor_text_changed)
        self.editor.textChanged.connect(self._on_ai_highlight_schedule)
        editor_layout.addWidget(self.editor)

        splitter.addWidget(editor_frame)

        # -- 右侧栏 --
        right_scroll = QScrollArea()
        right_scroll.setObjectName("writingSideScroll")
        right_scroll.setWidgetResizable(True)
        right_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        right_scroll.setFrameShape(QFrame.Shape.NoFrame)

        right_frame = QFrame()
        right_frame.setObjectName("writingSidePanel")
        right_frame.setMinimumWidth(220)
        right_frame.setMaximumWidth(350)
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(10)

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

        right_layout.addWidget(kb_group)

        # Zotero 状态
        zotero_group = QGroupBox("Zotero 状态")
        zotero_layout = QVBoxLayout(zotero_group)
        zotero_layout.setSpacing(4)

        self._zotero_status_label = QLabel("未连接")
        self._zotero_status_label.setWordWrap(True)
        self._zotero_status_label.setObjectName("statusChip")
        self._zotero_status_label.setProperty("status", "warning")
        zotero_layout.addWidget(self._zotero_status_label)
        right_layout.addWidget(zotero_group)

        # AI 辅助
        ai_group = QGroupBox("AI 辅助工具")
        ai_layout = QVBoxLayout(ai_group)
        ai_layout.setSpacing(6)

        self._review_btn = QPushButton("草稿整体评价")
        self._review_btn.setObjectName("secondaryBtn")
        self._review_btn.setToolTip(
            "AI 对全文进行结构性诊断：引用密度、过渡/小结、覆盖广度、\n"
            "文献时效性、批判性深度、冗余、图表建议、术语一致性。\n"
            "诊断后可逐项采纳/忽略/编辑，保存后将在润色时一并处理。"
        )
        self._review_btn.clicked.connect(self._on_review_draft)
        ai_layout.addWidget(self._review_btn)

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
        self._verify_btn.setToolTip(
            "不修改原文措辞，仅验证引文是否准确反映原文发现（需 Zotero 连接）。"
        )
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

        right_layout.addWidget(ai_group)
        right_layout.addStretch()

        right_scroll.setWidget(right_frame)
        splitter.addWidget(right_scroll)
        splitter.setSizes([760, 290])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

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
        self._cancel_btn.clicked.connect(self._cancel_all_workers)
        self._cancel_btn.setVisible(False)
        status_bar.addWidget(self._cancel_btn)
        main_layout.addLayout(status_bar)

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
        self._kb_combo.blockSignals(False)
        self._update_kb_status()

    def _on_kb_changed(self, idx: int):
        data = self._kb_combo.itemData(idx) or ""
        if data == "__new__":
            # 触发新建
            self._kb_combo.blockSignals(True)
            self._kb_combo.setCurrentIndex(0)
            self._kb_combo.blockSignals(False)
            self._on_new_kb()
            return
        if data and data in self._coach.profile_names:
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
            # 加载新知识库的草稿：必须整体替换，否则旧库内容会被
            # 30 秒自动保存写进新库的草稿文件（两库互相污染）
            self._load_draft(replace=True)
        else:
            self._coach._current_profile = None
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
            cursor = QTextCursor(self.editor.document())
            cursor.setPosition(start)
            cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
            fmt = QTextCharFormat()
            fmt.setBackground(QColor("#fff0d6"))
            fmt.setForeground(QColor("#a76d2b"))
            sel = QTextEdit.ExtraSelection(cursor=cursor, format=fmt)
            selections.append(sel)
        self.editor.setExtraSelections(selections)
        if matches:
            self._word_count_label.setText(f"字数: {len(text)} | 疑似AI味: {len(matches)}")

    def _on_new_kb(self):
        name, ok = QInputDialog.getText(
            self, "新建知识库", "知识库名称：",
        )
        if ok and name.strip():
            try:
                profile = self._coach.create_profile(
                    name.strip(), self._current_writing_type
                )
                self._refresh_kb_dropdown()
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
            self._coach.delete_profile(name)
            self._refresh_kb_dropdown()
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
        if not self._write_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return

        self._style_btn.setEnabled(False)
        self._style_btn.setText("正在分析...")
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)  # 不确定进度条
        self._status_label.setText("正在分析写作风格（可能需要 30-60 秒）...")
        QApplication.processEvents()

        self._style_worker = StyleGuideWorker(self._coach, self._write_client)
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
        """仅核查引文 —— 不修改原文措辞。"""
        self._verify_only = True
        self._run_polish_flow("polish")

    def _on_cn2en(self):
        """中译英 —— 翻译并润色为英文学术片段。"""
        self._verify_only = False
        self._run_polish_flow("cn2en")

    def _run_polish_flow(self, mode: str = "polish"):
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            QMessageBox.warning(self, "提示", "请先在编辑器中选中要处理的文字")
            return
        if not self._write_client:
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
            self._write_client, text, citation_count
        )
        self._citation_worker.finished_signal.connect(
            lambda citations: self._on_citations_extracted(citations, text, review_findings)
        )
        self._citation_worker.error_signal.connect(
            lambda err: self._on_citation_extract_error(err, text, review_findings)
        )
        self._citation_worker.start()

    def _on_citation_extract_error(self, error: str, text: str, review_findings: str):
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
                self._write_client, text, self._count_citation_markers(text)
            )
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
        """LLM 引文识别完成 → 在 Zotero 中搜索 → 启动润色。"""
        if self.sender() is not self._citation_worker:
            return  # 已取消的旧线程迟到结果，不再重启润色流程
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

            pre_citation_sources = ""
            from ..core.unified_writer import UnifiedWriter
            uw_helper = UnifiedWriter()
            if self._zotero:
                sources_list = []
                for c in citations:
                    author = c["author_hint"]
                    year = c["year_hint"]
                    marker = c["original_marker"]
                    if not author or not year or author == "unknown" or year == "unknown":
                        sources_list.append(f"--- 引文 {marker}: 未能识别到作者/年份 ---\n(无原文可对照)")
                        continue
                    candidates = self._zotero.find_by_citation(author, year)
                    if not candidates:
                        sources_list.append(f"--- 引文 {marker} ({author}, {year}): 未在 Zotero 库中匹配到 ---\n(无原文可对照)")
                        continue
                    query = uw_helper._sentence_around(text, marker)
                    for item in candidates[:2]:
                        text_pdf = uw_helper._extract_relevant_context(item.pdf_path, query)
                        title = (item.title or "?")[:100]
                        authors_list = ", ".join(item.authors[:3]) if item.authors else "?"
                        sources_list.append(
                            f"--- 引文 {marker} → {authors_list} ({item.year}) {title} ---\n{text_pdf}"
                        )
                pre_citation_sources = "\n\n".join(sources_list) if sources_list else ""
            self._status_label.setText(f"已识别 {len(citations)} 处引文标记")

            self._start_polish_worker(text, pre_citation_sources, review_findings)
        except Exception as e:  # noqa: BLE001
            self._on_unified_error(f"引文上下文构建失败：{e}")

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
            self._write_client, text, self._coach, self._zotero,
            self._current_writing_type, pre_citation_sources,
            review_findings=review_findings,
            verify_only=self._verify_only,
            mode=mode,
        )
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
            write_client=self._write_client,
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
        if not self._write_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return

        from .lit_search_dialog import LitSearchDialog
        dialog = LitSearchDialog(self._write_client, self._coach, parent=None)
        dialog.set_draft_text(draft)
        dialog.insert_requested.connect(self._on_lit_insert)
        self._track_dialog(dialog)
        dialog.show()

    def _on_lit_insert(self, marker: str):
        cursor = self.editor.textCursor()
        cursor.insertText(marker)
        self._status_label.setText("已插入文献引用标记")

    # ---- 按钮 3: 草稿整体评价 ----

    def _on_review_draft(self):
        """对全文进行结构性诊断评价。"""
        draft = self.editor.toPlainText().strip()
        if not draft:
            QMessageBox.warning(self, "提示", "请先编写草稿")
            return
        if not self._write_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return
        if not self._coach or not self._coach.current_profile:
            QMessageBox.warning(self, "提示", "请先选择知识库")
            return

        profile = self._coach.current_profile
        if not profile.has_writing_habits and not profile.has_journal_style:
            QMessageBox.warning(
                self, "提示",
                "知识库尚未生成风格指南，缺少评价基准。\n请先添加范文后点击「生成风格指南」。"
            )
            return

        self._review_btn.setEnabled(False)
        self._review_btn.setText("正在评价...")
        self._set_ai_buttons_busy(True)
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._cancel_btn.setVisible(True)
        self._status_label.setText("AI 正在诊断草稿结构（可能需要 30-60 秒）...")
        QApplication.processEvents()

        self._review_worker = DraftReviewWorker(
            self._write_client, draft, self._coach
        )
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
        self._status_label.setText("评价完成")

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
        self._status_label.setText(f"评价失败: {err[:60]}")
        QMessageBox.warning(self, "评价失败", err)

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

    def _cancel_all_workers(self):
        """协作式取消所有后台 AI 处理线程（请求中断 → 等待 → 超时才强杀）。"""
        workers = [self._citation_worker, self._unified_worker,
                   self._review_worker, self._style_worker]
        for w in workers:
            if w and w.isRunning():
                w.requestInterruption()
                w.quit()
                if not w.wait(3000):
                    w.terminate()
                    w.wait()
        self._citation_worker = None
        self._unified_worker = None
        self._review_worker = None
        self._style_worker = None
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._cancel_btn.setVisible(False)
        self._set_ai_buttons_busy(False)
        self._style_btn.setEnabled(True)
        self._style_btn.setText("重新生成风格指南" if self._coach.current_profile and self._coach.current_profile.total_papers > 0 else "生成风格指南")
        self._status_label.setText("已取消")

    def shutdown(self):
        """清理后台线程并保存草稿。"""
        self._auto_save_draft()
        self._auto_save_timer.stop()
        self._cancel_all_workers()
        for d in list(self._active_dialogs):
            try:
                d.close()
                d.deleteLater()
            except Exception:
                pass
        self._active_dialogs.clear()

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
        except Exception:
            pass

    def _swap_editor_text(self, text: str) -> None:
        """程序性替换编辑器全文：不记为用户修改，不触发高亮防抖。"""
        self.editor.blockSignals(True)
        self.editor.setPlainText(text)
        self.editor.blockSignals(False)
        self._draft_dirty = False
        self._on_editor_text_changed()
        self._draft_dirty = False
        self._refresh_ai_highlight()

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
        except Exception:
            pass

    def get_editor_text(self) -> str:
        return self.editor.toPlainText()

    def set_editor_text(self, text: str):
        self.editor.setPlainText(text)
