"""写作面板 —— 综述/论文/专利/软著 全流程写作辅助。

布局: 顶部工具栏(类型/知识库/Zotero) | 左侧编辑器 | 右侧知识库状态 + AI辅助

AI 辅助按钮:
  - "AI 润色与核查": 统一润色+引文核查 → Diff 对话框对比
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
    QLineEdit,
)
from PySide6.QtCore import Qt, Signal, QThread, QSize
from PySide6.QtGui import QFont, QTextCursor

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.zotero_parser import ZoteroLibrary
    from ..core.review_checker import ReviewChecker
    from ..core.writing_coach import WritingCoach, WritingProfile


# ============================================================
# 后台工作线程
# ============================================================

class StyleGuideWorker(QThread):
    """后台生成风格指南。"""
    finished_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, coach: "WritingCoach", client: "LLMClient"):
        super().__init__()
        self._coach = coach
        self._client = client

    def run(self):
        try:
            guide = self._coach.generate_style_guide(self._client)
            if guide:
                self.finished_signal.emit(guide)
            else:
                self.error_signal.emit("风格分析失败：LLM 返回为空或格式异常")
        except Exception as e:
            self.error_signal.emit(str(e))


class UnifiedWorker(QThread):
    """后台统一润色与引文核查。"""
    finished_signal = Signal(dict)
    error_signal = Signal(str)

    def __init__(self, client: "LLMClient", selected_text: str,
                 coach: "WritingCoach", zotero_lib: "ZoteroLibrary | None",
                 writing_type: str):
        super().__init__()
        self._client = client
        self._text = selected_text
        self._coach = coach
        self._zotero = zotero_lib
        self._writing_type = writing_type

    def run(self):
        try:
            from ..core.unified_writer import UnifiedWriter
            uw = UnifiedWriter()
            result = uw.process(self._client, self._text, self._coach,
                                self._zotero, self._writing_type)
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
        self._review_checker: ReviewChecker | None = None
        self._coach = self._create_coach()
        self._current_writing_type = "综述"
        self._style_worker: StyleGuideWorker | None = None
        self._unified_worker: UnifiedWorker | None = None

        self._setup_ui()
        self._refresh_kb_dropdown()

    @staticmethod
    def _create_coach():
        from ..core.writing_coach import WritingCoach
        return WritingCoach()

    # ---- 注入依赖 ----

    def set_write_client(self, client: "LLMClient | None"):
        self._write_client = client

    def set_zotero_library(self, zotero: "ZoteroLibrary | None"):
        self._zotero = zotero
        self._update_zotero_status()

    def set_checker(self, checker: "ReviewChecker | None"):
        self._review_checker = checker

    # ---- UI 构建 ----

    def _setup_ui(self):
        main_layout = QVBoxLayout(self)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        # ===== 顶部工具栏 =====
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 8, 12, 8)
        toolbar.setSpacing(10)

        title = QLabel("📝 写作")
        title.setObjectName("titleLabel")
        toolbar.addWidget(title)

        # 写作类型
        type_label = QLabel("类型:")
        type_label.setStyleSheet("color: #8a8ea6; font-size: 13px;")
        toolbar.addWidget(type_label)

        self._type_combo = QComboBox()
        self._type_combo.setEditable(True)
        self._type_combo.setMinimumWidth(160)
        from ..core.writing_prompts import get_all_writing_types
        for key, label in get_all_writing_types():
            self._type_combo.addItem(label, key)
        self._type_combo.currentIndexChanged.connect(self._on_writing_type_changed)
        toolbar.addWidget(self._type_combo)

        toolbar.addSpacing(10)

        # 知识库
        kb_label = QLabel("知识库:")
        kb_label.setStyleSheet("color: #8a8ea6; font-size: 13px;")
        toolbar.addWidget(kb_label)

        self._kb_combo = QComboBox()
        self._kb_combo.setEditable(True)
        self._kb_combo.setMinimumWidth(180)
        self._kb_combo.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._kb_combo.customContextMenuRequested.connect(self._on_kb_context_menu)
        self._kb_combo.currentIndexChanged.connect(self._on_kb_changed)
        toolbar.addWidget(self._kb_combo)

        toolbar.addSpacing(8)

        # Zotero 路径
        zotero_label = QLabel("Zotero:")
        zotero_label.setStyleSheet("color: #8a8ea6; font-size: 13px;")
        toolbar.addWidget(zotero_label)

        self._zotero_path_edit = QLineEdit()
        self._zotero_path_edit.setPlaceholderText("自动检测...")
        self._zotero_path_edit.setMaximumWidth(200)
        self._zotero_path_edit.setStyleSheet(
            "QLineEdit { background-color: #24253a; color: #a9b1d6; "
            "border: 1px solid #3b3d54; border-radius: 4px; padding: 2px 6px; "
            "font-size: 12px; }"
        )
        toolbar.addWidget(self._zotero_path_edit)

        self._zotero_browse_btn = QPushButton("📂")
        self._zotero_browse_btn.setFixedWidth(45)
        self._zotero_browse_btn.setToolTip("浏览选择 Zotero 数据目录")
        self._zotero_browse_btn.clicked.connect(self._on_zotero_browse)
        toolbar.addWidget(self._zotero_browse_btn)

        toolbar.addStretch()
        main_layout.addLayout(toolbar)

        # ===== 分隔线 =====
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #2a2c3d; max-height: 1px;")
        main_layout.addWidget(sep)

        # ===== 主区域: 编辑器 | 右侧栏 =====
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.setOpaqueResize(False)

        # -- 编辑器 --
        editor_frame = QFrame()
        editor_frame.setStyleSheet("background-color: #1a1b26;")
        editor_layout = QVBoxLayout(editor_frame)
        editor_layout.setContentsMargins(8, 8, 8, 8)

        self.editor = QTextEdit()
        self.editor.setPlaceholderText(
            "在此编写你的综述/论文...\n\n"
            "提示：选中文字后可在右侧使用 AI 辅助功能"
        )
        self.editor.setStyleSheet(
            "QTextEdit { background-color: #1e2030; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 8px; "
            "padding: 16px; font-size: 14px; line-height: 1.8; }"
            "QTextEdit:focus { border-color: #7aa2f7; }"
        )
        editor_layout.addWidget(self.editor)

        splitter.addWidget(editor_frame)

        # -- 右侧栏 --
        right_frame = QFrame()
        right_frame.setMinimumWidth(220)
        right_frame.setMaximumWidth(320)
        right_frame.setStyleSheet("background-color: #1a1b26;")
        right_layout = QVBoxLayout(right_frame)
        right_layout.setContentsMargins(8, 8, 8, 8)
        right_layout.setSpacing(10)

        # 知识库状态
        kb_group = QGroupBox("📚 知识库状态")
        kb_group.setStyleSheet(
            "QGroupBox { color: #a9b1d6; font-weight: bold; border: 1px solid #2a2c3d; "
            "border-radius: 8px; margin-top: 8px; padding: 12px 8px 8px 8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
        )
        kb_layout = QVBoxLayout(kb_group)
        kb_layout.setSpacing(6)

        self._kb_status_label = QLabel("未选择知识库")
        self._kb_status_label.setWordWrap(True)
        self._kb_status_label.setStyleSheet("color: #8a8ea6; font-size: 12px;")
        kb_layout.addWidget(self._kb_status_label)

        self._personal_btn = QPushButton("📄 添加参考论文")
        self._personal_btn.setToolTip("上传你自己的论文 PDF 供风格分析")
        self._personal_btn.clicked.connect(self._on_add_personal_paper)
        kb_layout.addWidget(self._personal_btn)

        self._journal_btn = QPushButton("📰 添加期刊范文")
        self._journal_btn.setToolTip("上传目标期刊的综述 PDF 供风格分析")
        self._journal_btn.clicked.connect(self._on_add_journal_paper)
        kb_layout.addWidget(self._journal_btn)

        self._style_btn = QPushButton("📐 生成风格指南")
        self._style_btn.setToolTip("基于已添加的论文和范文，让 AI 分析写作风格并生成指南")
        self._style_btn.clicked.connect(self._on_generate_style_guide)
        self._style_btn.setEnabled(False)
        kb_layout.addWidget(self._style_btn)

        right_layout.addWidget(kb_group)

        # Zotero 状态
        zotero_group = QGroupBox("📖 Zotero")
        zotero_group.setStyleSheet(
            "QGroupBox { color: #a9b1d6; font-weight: bold; border: 1px solid #2a2c3d; "
            "border-radius: 8px; margin-top: 8px; padding: 12px 8px 8px 8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
        )
        zotero_layout = QVBoxLayout(zotero_group)
        zotero_layout.setSpacing(4)

        self._zotero_status_label = QLabel("未连接")
        self._zotero_status_label.setWordWrap(True)
        self._zotero_status_label.setStyleSheet("color: #636688; font-size: 12px;")
        zotero_layout.addWidget(self._zotero_status_label)
        right_layout.addWidget(zotero_group)

        # AI 辅助
        ai_group = QGroupBox("🤖 AI 辅助")
        ai_group.setStyleSheet(
            "QGroupBox { color: #a9b1d6; font-weight: bold; border: 1px solid #2a2c3d; "
            "border-radius: 8px; margin-top: 8px; padding: 12px 8px 8px 8px; }"
            "QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }"
        )
        ai_layout = QVBoxLayout(ai_group)
        ai_layout.setSpacing(6)

        self._polish_btn = QPushButton("✨ AI 润色与核查")
        self._polish_btn.setToolTip(
            "选中含引文的文字后，AI 同时完成：润色语言 + 核查引文准确性（需 Zotero 连接）\n"
            "结果以左右并排对比的方式展示，方便逐条审查"
        )
        self._polish_btn.clicked.connect(self._on_unified_polish)
        ai_layout.addWidget(self._polish_btn)

        self._lit_search_btn = QPushButton("🔍 文献补充")
        self._lit_search_btn.setToolTip(
            "AI 分析草稿的遗漏方向 → 你审阅并反馈 → 确认后 PubMed 检索"
        )
        self._lit_search_btn.clicked.connect(self._on_lit_search)
        ai_layout.addWidget(self._lit_search_btn)

        right_layout.addWidget(ai_group)
        right_layout.addStretch()

        splitter.addWidget(right_frame)
        splitter.setSizes([650, 250])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)

        main_layout.addWidget(splitter, 1)

        # ===== 底部状态栏 =====
        status_sep = QFrame()
        status_sep.setFrameShape(QFrame.Shape.HLine)
        status_sep.setStyleSheet("background-color: #2a2c3d; max-height: 1px;")
        main_layout.addWidget(status_sep)

        status_bar = QHBoxLayout()
        status_bar.setContentsMargins(12, 4, 12, 4)
        self._status_label = QLabel("就绪")
        self._status_label.setStyleSheet("color: #8a8ea6; font-size: 12px;")
        status_bar.addWidget(self._status_label)
        status_bar.addStretch()

        self._progress_bar = QProgressBar()
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setVisible(False)
        self._progress_bar.setMaximumWidth(180)
        self._progress_bar.setMaximumHeight(14)
        self._progress_bar.setStyleSheet(
            "QProgressBar { background-color: #24253a; border: 1px solid #3b3d54; "
            "border-radius: 7px; }"
            "QProgressBar::chunk { background-color: #7aa2f7; border-radius: 6px; }"
        )
        status_bar.addWidget(self._progress_bar)
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
                h = "H" if profile.has_writing_habits else "-"
                j = "J" if profile.has_journal_style else "-"
                extra = f" ({profile.personal_count}篇 [{h}], {profile.journal_count}篇 [{j}])"
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
            self._coach.switch_profile(data)
            profile = self._coach.current_profile
            if profile and profile.writing_type:
                type_idx = self._type_combo.findData(profile.writing_type)
                if type_idx >= 0:
                    self._type_combo.blockSignals(True)
                    self._type_combo.setCurrentIndex(type_idx)
                    self._type_combo.blockSignals(False)
                    self._current_writing_type = profile.writing_type
        else:
            self._coach._current_profile = None
        self._update_kb_status()

    def _on_kb_context_menu(self, pos):
        """知识库下拉右键菜单：删除。"""
        data = self._kb_combo.currentData() or ""
        if not data or data == "__new__" or data not in self._coach.profile_names:
            return
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #24253a; color: #cfd2e3; border: 1px solid #3b3d54; }"
            "QMenu::item:selected { background: #3b3d54; }"
        )
        a = menu.addAction("🗑 删除此知识库")
        a.triggered.connect(lambda: self._on_delete_kb())
        menu.exec(self._kb_combo.mapToGlobal(pos))

    def _on_zotero_browse(self):
        """浏览选择 Zotero 数据目录。"""
        current = self._zotero_path_edit.text().strip()
        path = QFileDialog.getExistingDirectory(self, "选择 Zotero 数据目录", current or "")
        if path:
            self._zotero_path_edit.setText(path)
            # 同时更新 config
            from ..utils.config import load_config, save_config
            cfg = load_config()
            cfg["zotero_data_dir"] = path
            save_config(cfg)
            self._status_label.setText(f"Zotero 路径已更新: {path}")

    def _on_writing_type_changed(self, idx: int):
        self._current_writing_type = self._type_combo.itemData(idx) or "综述"

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
            except ValueError as e:
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

    def _on_add_personal_paper(self):
        if not self._coach.current_profile:
            QMessageBox.warning(self, "提示", "请先选择或创建一个知识库")
            return
        paths, _ = QFileDialog.getOpenFileNames(
            self, "选择参考论文 PDF", "", "PDF 文件 (*.pdf);;所有文件 (*.*)"
        )
        for path in paths:
            result = self._coach.add_personal_paper(path)
            if result:
                self._status_label.setText(f"已添加个人论文: {result['filename']}")
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
            QMessageBox.warning(self, "提示", "请先添加参考论文或期刊范文")
            return
        if not self._write_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return

        self._style_btn.setEnabled(False)
        self._style_btn.setText("⏳ 分析中...")
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)  # 不确定进度条
        self._status_label.setText("正在分析写作风格（可能需要 30-60 秒）...")
        QApplication.processEvents()

        self._style_worker = StyleGuideWorker(self._coach, self._write_client)
        self._style_worker.finished_signal.connect(self._on_style_guide_ready)
        self._style_worker.error_signal.connect(self._on_style_guide_error)
        self._style_worker.start()

    def _on_style_guide_ready(self, guide: dict):
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._style_btn.setEnabled(True)
        self._style_btn.setText("📐 重新生成")
        self._update_kb_status()
        self._status_label.setText("风格分析完成")

        profile = self._coach.current_profile
        lines = []
        if profile:
            if profile.has_writing_habits:
                h = profile.writing_habits
                if h.get("citation_detail_level"):
                    cd = h["citation_detail_level"]
                    lines.append(f"引用详略度: 平均 {cd['avg_sentences_per_citation']} 句 / {cd['avg_chars_per_citation']} 字 (共 {cd['sample_count']} 个样本)")
                if h.get("argumentation_style"):
                    lines.append(f"论述逻辑: {h['argumentation_style'][:80]}")
                if h.get("paragraph_patterns"):
                    lines.append(f"段落: {h['paragraph_patterns'][:80]}")
            if profile.has_journal_style:
                j = profile.journal_style
                if j.get("citation_format"):
                    lines.append(f"引用格式: {j['citation_format'][:80]}")
                if j.get("section_structure"):
                    lines.append(f"结构: {j['section_structure'][:80]}")

        QMessageBox.information(
            self, "风格分析完成",
            "AI 已完成知识库分析。\n\n"
            + "\n".join(lines) +
            "\n\n后续写作时将自动遵循这些规范。"
        )

    def _on_style_guide_error(self, err: str):
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._style_btn.setEnabled(True)
        self._style_btn.setText("📐 生成风格指南")
        self._status_label.setText(f"风格分析失败: {err[:60]}")
        QMessageBox.warning(self, "风格分析失败", err)

    # ---- 按钮 1: AI 润色与核查 ----

    def _on_unified_polish(self):
        """统一润色 + 引文核查。"""
        cursor = self.editor.textCursor()
        if not cursor.hasSelection():
            QMessageBox.warning(self, "提示", "请先在编辑器中选中要处理的文字")
            return
        if not self._write_client:
            QMessageBox.warning(self, "提示", "请先配置写作 API")
            return

        text = cursor.selectedText().strip()
        self._polish_btn.setEnabled(False)
        self._polish_btn.setText("⏳ 处理中...")
        self._progress_bar.setVisible(True)
        self._progress_bar.setRange(0, 0)
        self._status_label.setText("AI 正在润色并核查引文...")
        QApplication.processEvents()

        self._unified_worker = UnifiedWorker(
            self._write_client, text, self._coach, self._zotero,
            self._current_writing_type
        )
        self._unified_worker.finished_signal.connect(self._on_unified_done)
        self._unified_worker.error_signal.connect(self._on_unified_error)
        self._unified_worker.start()

    def _on_unified_done(self, result: dict):
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._polish_btn.setEnabled(True)
        self._polish_btn.setText("✨ AI 润色与核查")
        self._status_label.setText("处理完成")

        if result.get("error"):
            QMessageBox.warning(self, "处理失败", result["error"])
            return

        from .diff_dialog import DiffDialog
        dialog = DiffDialog(
            original=self.editor.textCursor().selectedText(),
            polished=result.get("polished_text", ""),
            citation_notes=result.get("citation_notes", []),
            supervisor_notes=result.get("supervisor_notes", []),
            parent=self,
        )
        if dialog.exec():
            # 用户点击了"替换原文"
            cursor = self.editor.textCursor()
            cursor.insertText(dialog.get_polished_text())
            self._status_label.setText("润色文本已替换")

    def _on_unified_error(self, err: str):
        self._progress_bar.setVisible(False)
        self._progress_bar.setRange(0, 100)
        self._polish_btn.setEnabled(True)
        self._polish_btn.setText("✨ AI 润色与核查")
        self._status_label.setText(f"处理失败: {err[:60]}")
        QMessageBox.warning(self, "处理失败", err)

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
        dialog = LitSearchDialog(self._write_client, self._coach, parent=self)
        dialog.set_draft_text(draft)
        dialog.exec()

    # ---- Zotero ----

    def _update_zotero_status(self):
        if self._zotero and self._zotero.is_available:
            count = self._zotero.item_count if hasattr(self._zotero, 'item_count') else 0
            self._zotero_status_label.setText(f"✅ 已连接 ({count} 篇文献)")
            self._zotero_status_label.setStyleSheet("color: #9ece6a; font-size: 12px;")
            # 更新路径显示
            from ..utils.config import load_config
            cfg = load_config()
            zdir = cfg.get("zotero_data_dir", "") or self._zotero._data_dir
            if zdir and not self._zotero_path_edit.text():
                self._zotero_path_edit.setText(zdir)
        else:
            self._zotero_status_label.setText("⚠️ 未连接")
            self._zotero_status_label.setStyleSheet("color: #e0af68; font-size: 12px;")

    # ---- 知识库状态 ----

    def _update_kb_status(self):
        profile = self._coach.current_profile
        if profile:
            habits_ok = "已生成" if profile.has_writing_habits else "未生成"
            journal_ok = "已生成" if profile.has_journal_style else "未生成"
            lines = [
                f"名称: {profile.name}",
                f"类型: {profile.writing_type}",
                f"个人论文: {profile.personal_count} 篇",
                f"期刊范文: {profile.journal_count} 篇",
                f"写作习惯: {habits_ok}",
                f"期刊格式: {journal_ok}",
            ]
            self._kb_status_label.setText("\n".join(lines))
            self._style_btn.setEnabled(profile.total_papers > 0)
        else:
            self._kb_status_label.setText("未选择知识库\n请在下拉菜单中选择或新建")
            self._style_btn.setEnabled(False)

    # ---- 生命周期 ----

    def shutdown(self):
        """清理后台线程。"""
        if self._style_worker and self._style_worker.isRunning():
            self._style_worker.quit()
            self._style_worker.wait(2000)
        if self._unified_worker and self._unified_worker.isRunning():
            self._unified_worker.quit()
            self._unified_worker.wait(2000)

    def get_editor_text(self) -> str:
        return self.editor.toPlainText()

    def set_editor_text(self, text: str):
        self.editor.setPlainText(text)
