"""PaperWB 主窗口 —— 本地版式阅读 + 图表问答 + 写作辅助。"""

from __future__ import annotations

import os
import time
import weakref

from PySide6.QtCore import Qt, QThread, QTimer, Signal as QtSignal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget, QFrame,
)

from .core.context_manager import ContextManager
from .core.library_preparser import LibraryPreparser
from .core.llm_client import LLMClient
from .core.stats_tracker import StatsTracker
from .core.zotero_parser import ZoteroLibrary
from .core.zotero_watcher import ZoteroWatcher
from .ui.chat_panel import ChatPanel
from .ui.library_qa_panel import LibraryQAPanel
from .ui.pdf_list_panel import PDFListPanel
from .ui.pdf_viewer import PDFViewerPanel
from .ui.stats_panel import StatsPanel
from .ui.workbench_panel import WorkbenchPanel
from .ui.writing_panel import WritingPanel
from .ui.settings_dialog import DirectorySettingDialog, SettingsDialog
from .ui.styles import STYLESHEET
from .utils.config import (
    delete_chat_history, get_text_api, get_vision_api,
    has_data_root, load_chat_history, load_config, save_chat_history,
    save_config,
)
from .utils.threads import track

# 单窗口常驻后台处理器的数量上限：超量时淘汰最旧的闲置处理器（正在解析/整合的除外）
_MAX_PROCESSORS = 8


class LLMWorker(QThread):
    chunk_received = QtSignal(str)
    finished = QtSignal()
    error = QtSignal(str)
    done = QtSignal()  # 线程必然退出信号（run 的 finally 触发）

    def __init__(self, client: LLMClient, messages: list[dict]) -> None:
        super().__init__()
        self._client = client
        self._messages = messages

    def run(self) -> None:
        try:
            for chunk in self._client.chat_stream(self._messages):
                if self.isInterruptionRequested():
                    return  # 已取消：不再投递旧问答的后续内容
                self.chunk_received.emit(chunk)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))
        finally:
            self.done.emit()


class DoclingWarmupWorker(QThread):
    """后台预热 Docling 导入，避免第一次点击论文时阻塞在模块加载。"""

    finished_signal = QtSignal(bool, str)

    def run(self) -> None:
        try:
            from .core.docling_parser import warm_up_import
            warm_up_import()
            self.finished_signal.emit(True, "")
        except Exception as e:
            self.finished_signal.emit(False, str(e))


class FirstLaunchDialog(QDialog):
    """首次启动：设置数据根目录。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PaperWB · 首次设置")
        self.setMinimumSize(520, 430)
        self.setModal(True)
        self.setStyleSheet(STYLESHEET)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 28, 30, 24)
        layout.setSpacing(14)

        eyebrow = QLabel("欢迎来到你的研究工作台")
        eyebrow.setObjectName("eyebrowLabel")
        layout.addWidget(eyebrow)

        title = QLabel("让每一篇论文，都变成可读的知识")
        title.setObjectName("titleLabel")
        title.setStyleSheet("font-size: 22px;")
        layout.addWidget(title)

        desc = QLabel(
            "请选择一个文件夹作为数据根目录。\n"
            "论文、阅读缓存、写作知识库和草稿都会集中保存在这里，"
            "后续也可以在设置中随时更改。"
        )
        desc.setWordWrap(True)
        desc.setObjectName("subtitleLabel")
        desc.setStyleSheet("font-size: 13px; line-height: 1.7;")
        layout.addWidget(desc)

        form = QFormLayout()
        form.setSpacing(8)

        from pathlib import Path
        default_path = str(Path.home() / "Documents" / "PaperWB_Data")

        self._path_edit = QLineEdit(default_path)
        browse_btn = QPushButton("浏览...")
        browse_btn.setObjectName("secondaryBtn")
        browse_btn.clicked.connect(self._browse)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self._path_edit)
        row_layout.addWidget(browse_btn)
        form.addRow("数据根目录：", row)
        layout.addLayout(form)

        note = QLabel("目录不存在时会自动创建所需的子目录。")
        note.setObjectName("subtitleLabel")
        layout.addWidget(note)

        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始使用")
        buttons.accepted.connect(self._accept)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryBtn")
        layout.addWidget(buttons)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "选择数据根目录", self._path_edit.text())
        if path:
            self._path_edit.setText(path)

    def _accept(self):
        path = self._path_edit.text().strip()
        if not path:
            QMessageBox.warning(self, "提示", "请选择或输入数据根目录。")
            return
        from pathlib import Path
        try:
            Path(path).mkdir(parents=True, exist_ok=True)
        except OSError as e:
            QMessageBox.critical(self, "错误", f"无法创建目录：{e}")
            return
        self._selected_path = path
        self.accept()

    @property
    def selected_path(self) -> str:
        return getattr(self, '_selected_path', self._path_edit.text().strip())


class MainWindow(QMainWindow):
    """PaperWB 主窗口 v2 —— 阅读 + 写作。"""

    def __init__(self) -> None:
        super().__init__()

        # 首次启动检测：如果未设置数据根目录，弹窗让用户选择
        if not has_data_root():
            dialog = FirstLaunchDialog()
            if dialog.exec():
                config = load_config()
                config["data_root"] = dialog.selected_path
                save_config(config)
            else:
                pass  # 用户关闭了对话框，使用默认路径

        self._config = load_config()
        self._llm_vision: LLMClient | None = None
        self._llm_text: LLMClient | None = None
        self._context_manager = ContextManager(
            max_tokens=self._config.get("max_tokens", 1_000_000)
        )
        self._llm_worker: LLMWorker | None = None
        self._docling_warmup: DoclingWarmupWorker | None = None
        self._current_pdf_path: str = ""

        self._processors: dict[str, object] = {}
        self._app_progress_connected: weakref.WeakSet = weakref.WeakSet()
        self._zotero: ZoteroLibrary | None = None
        self._zotero_watcher: ZoteroWatcher | None = None
        self._feed_bridge_connected = False
        self._preparser: LibraryPreparser | None = None
        self._preparse_flush_count = 0

        self._setup_ui()
        self._apply_styles()
        self._init_all_clients()
        self._init_write()
        self._init_stats()
        self._validate_data_root()
        QTimer.singleShot(800, self._start_docling_warmup)
        self._init_preparser()

    def _init_preparser(self) -> None:
        """后台全库预解析：与阅读共用缓存，索引就绪/启动延时后自动开跑。"""
        self._preparser = LibraryPreparser(
            should_yield=self._preparse_should_yield, parent=self)
        self._preparser.progress.connect(self._on_preparse_progress)
        self._preparser.item_done.connect(self._on_preparse_item_done)
        self._preparser.state_changed.connect(self._on_preparse_state_changed)
        self.library_qa_panel.index_built.connect(self._maybe_start_preparse)
        self.library_qa_panel.preparse_toggled.connect(self._on_preparse_toggled)
        QTimer.singleShot(60_000, self._maybe_start_preparse)

    def _init_stats(self) -> None:
        """统计工作台：活动埋点接线（阅读/问答/检索/巡视/写作）。"""
        self._stats_tracker = self._stats_panel._tracker
        # 面板信号 → 统计
        self._workbench_panel.search_completed.connect(
            lambda _n: self._stats_tracker.record_search())
        self._workbench_panel.scout_completed.connect(
            lambda _name, _n: self._stats_tracker.record_scout())
        self._writing_panel.draft_saved.connect(self._stats_tracker.record_draft)
        self._writing_panel.polish_accepted.connect(self._stats_tracker.record_polish)
        self._writing_panel.lit_search_completed.connect(
            lambda _n: self._stats_tracker.record_search())
        self.library_qa_panel.qa_asked.connect(
            lambda: self._stats_tracker.record_qa())

    def _setup_ui(self):
        self.setWindowTitle("PaperWB · AI 论文研究工作台")
        self.setMinimumSize(1024, 680)
        self.resize(1440, 900)

        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件")
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        settings_menu = menubar.addMenu("设置")
        api_action = QAction("API 接口设置...", self)
        api_action.setShortcut("Ctrl+,")
        api_action.triggered.connect(self._on_open_settings)
        settings_menu.addAction(api_action)
        settings_menu.addSeparator()
        zotero_dir_action = QAction("Zotero 文献库路径设置...", self)
        zotero_dir_action.triggered.connect(self._on_change_zotero_dir)
        settings_menu.addAction(zotero_dir_action)
        data_dir_action = QAction("缓存文件存储路径设置...", self)
        data_dir_action.triggered.connect(self._on_change_data_dir)
        settings_menu.addAction(data_dir_action)

        help_menu = menubar.addMenu("帮助")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

        self._main_tabs = QTabWidget()
        self._main_tabs.setDocumentMode(True)
        self._main_tabs.tabBar().setVisible(False)

        # Tab 0: 阅读
        outer_splitter = QSplitter(Qt.Orientation.Horizontal)
        outer_splitter.setHandleWidth(3)
        outer_splitter.setOpaqueResize(False)

        self.pdf_list = PDFListPanel()
        self.pdf_list.pdf_selected.connect(self._on_library_pdf_selected)
        self.pdf_list.pdf_removed.connect(self._on_library_pdf_removed)
        self.pdf_list.pdf_reload_requested.connect(self._on_library_pdf_reload)
        self.pdf_list.pdf_imported.connect(self._on_pdf_imported)
        self.pdf_list.restage1_requested.connect(self._on_restage1)
        self.pdf_list.restage2_requested.connect(self._on_restage2)
        self.pdf_list.reparse_requested.connect(self._on_reparse)
        self.pdf_list.zotero_pdf_selected.connect(self._on_zotero_pdf_selected)
        self.pdf_list.zotero_reparse_requested.connect(self._on_reparse)

        inner_splitter = QSplitter(Qt.Orientation.Horizontal)
        inner_splitter.setHandleWidth(3)
        inner_splitter.setOpaqueResize(False)

        self.pdf_viewer = PDFViewerPanel()
        self.pdf_viewer.setMinimumWidth(300)
        self.pdf_viewer.pdf_loaded.connect(self._on_pdf_loaded)
        self.pdf_viewer.pdf_path_changed.connect(self._on_pdf_path_changed)
        self.pdf_viewer.structured_document_updated.connect(
            self._on_structured_document_updated)
        self.pdf_viewer.follow_up_question.connect(self._on_follow_up_from_reader)

        self.chat_panel = ChatPanel()
        self.chat_panel.send_message.connect(self._on_user_message)
        self.chat_panel.clear_requested.connect(self._on_clear_chat)
        self.library_qa_panel = LibraryQAPanel()
        self.library_qa_panel.open_pdf_requested.connect(self._on_zotero_pdf_selected)

        self.chat_tabs = QTabWidget()
        self.chat_tabs.setDocumentMode(True)
        self.chat_tabs.setMinimumWidth(280)
        self.chat_tabs.addTab(self.chat_panel, "本篇论文")
        self.chat_tabs.addTab(self.library_qa_panel, "全文献库")

        inner_splitter.addWidget(self.pdf_viewer)
        inner_splitter.addWidget(self.chat_tabs)
        inner_splitter.setSizes([550, 450])
        inner_splitter.setStretchFactor(0, 2)
        inner_splitter.setStretchFactor(1, 1)
        inner_splitter.setCollapsible(0, False)
        inner_splitter.setCollapsible(1, True)
        self._reader_inner_splitter = inner_splitter

        outer_splitter.addWidget(self.pdf_list)
        outer_splitter.addWidget(inner_splitter)
        outer_splitter.setSizes([285, 1000])
        outer_splitter.setStretchFactor(0, 0)
        outer_splitter.setStretchFactor(1, 1)
        outer_splitter.setCollapsible(0, True)
        outer_splitter.setCollapsible(1, False)
        self._reader_outer_splitter = outer_splitter

        reader_surface = QWidget()
        reader_surface.setObjectName("workspaceSurface")
        reader_layout = QVBoxLayout(reader_surface)
        reader_layout.setContentsMargins(0, 0, 0, 0)
        reader_layout.setSpacing(10)
        reader_header = QFrame()
        reader_header.setObjectName("workspaceHeader")
        reader_header_layout = QHBoxLayout(reader_header)
        reader_header_layout.setContentsMargins(16, 10, 16, 10)
        reader_header_layout.setSpacing(8)
        reader_title_box = QVBoxLayout()
        reader_title_box.setSpacing(1)
        reader_eyebrow = QLabel("阅读与理解")
        reader_eyebrow.setObjectName("eyebrowLabel")
        reader_title_box.addWidget(reader_eyebrow)
        reader_title = QLabel("阅读工作台")
        reader_title.setObjectName("titleLabel")
        reader_title_box.addWidget(reader_title)
        reader_header_layout.addLayout(reader_title_box)
        reader_subtitle = QLabel("结构化阅读、段落翻译与论文问答（本篇 / 全库）")
        reader_subtitle.setObjectName("subtitleLabel")
        reader_header_layout.addWidget(reader_subtitle)
        reader_header_layout.addStretch()

        self._reader_library_toggle_btn = QPushButton("文献列表")
        self._reader_library_toggle_btn.setObjectName("paneToggle")
        self._reader_library_toggle_btn.setCheckable(True)
        self._reader_library_toggle_btn.setChecked(True)
        self._reader_library_toggle_btn.setToolTip("显示或隐藏左侧文献列表")
        reader_header_layout.addWidget(self._reader_library_toggle_btn)

        self._reader_chat_toggle_btn = QPushButton("论文问答")
        self._reader_chat_toggle_btn.setObjectName("paneToggle")
        self._reader_chat_toggle_btn.setCheckable(True)
        self._reader_chat_toggle_btn.setChecked(True)
        self._reader_chat_toggle_btn.setToolTip(
            "显示或隐藏右侧论文问答（本篇论文 / 全文献库 两个页签）")
        reader_header_layout.addWidget(self._reader_chat_toggle_btn)

        self._reader_library_toggle_btn.toggled.connect(self._set_reader_library_visible)
        self._reader_chat_toggle_btn.toggled.connect(self._set_reader_chat_visible)
        reader_layout.addWidget(reader_header)
        reader_layout.addWidget(outer_splitter, 1)
        self._main_tabs.addTab(reader_surface, "阅读工作台")

        # Tab 1: 检索工作台（AI 检索 + 按库推荐 + 定时文献巡视）
        self._workbench_panel = WorkbenchPanel()
        self._main_tabs.addTab(self._workbench_panel, "检索工作台")

        # Tab 2: 写作
        self._writing_panel = WritingPanel()
        self._main_tabs.addTab(self._writing_panel, "写作工作台")

        # Tab 3: 统计（面板在 _init_stats 中创建并注入 tracker）
        self._stats_panel = StatsPanel(StatsTracker(parent=self))
        self._main_tabs.addTab(self._stats_panel, "统计工作台")

        # 顶部应用栏：把工作区切换和高频操作从传统标签页中提出来。
        shell = QWidget()
        shell.setObjectName("appShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(16, 12, 16, 8)
        shell_layout.setSpacing(10)

        app_header = QFrame()
        app_header.setObjectName("appHeader")
        header_layout = QHBoxLayout(app_header)
        header_layout.setContentsMargins(14, 10, 14, 10)
        header_layout.setSpacing(8)

        brand_mark = QLabel("研")
        brand_mark.setObjectName("brandMark")
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        app_icon = QApplication.windowIcon()
        if not app_icon.isNull():
            brand_mark.setText("")
            brand_mark.setPixmap(app_icon.pixmap(32, 32))
            brand_mark.setScaledContents(True)
            brand_mark.setFixedSize(38, 38)
        header_layout.addWidget(brand_mark)

        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_title = QLabel("PaperWB")
        brand_title.setObjectName("brandTitle")
        brand_text.addWidget(brand_title)
        brand_subtitle = QLabel("AI 论文研究工作台")
        brand_subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(brand_subtitle)
        header_layout.addLayout(brand_text)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("background-color: #e5e5ea; max-width: 1px;")
        header_layout.addSpacing(10)
        header_layout.addWidget(divider)
        header_layout.addSpacing(4)

        self._read_nav = QPushButton("阅读工作台")
        self._read_nav.setObjectName("workspaceNav")
        self._read_nav.setCheckable(True)
        self._read_nav.setChecked(True)
        self._read_nav.clicked.connect(lambda _checked=False: self._switch_workspace(0))
        header_layout.addWidget(self._read_nav)

        self._scout_nav = QPushButton("检索工作台")
        self._scout_nav.setObjectName("workspaceNav")
        self._scout_nav.setCheckable(True)
        self._scout_nav.clicked.connect(lambda _checked=False: self._switch_workspace(1))
        header_layout.addWidget(self._scout_nav)

        self._write_nav = QPushButton("写作工作台")
        self._write_nav.setObjectName("workspaceNav")
        self._write_nav.setCheckable(True)
        self._write_nav.clicked.connect(lambda _checked=False: self._switch_workspace(2))
        header_layout.addWidget(self._write_nav)

        self._stats_nav = QPushButton("统计工作台")
        self._stats_nav.setObjectName("workspaceNav")
        self._stats_nav.setCheckable(True)
        self._stats_nav.clicked.connect(lambda _checked=False: self._switch_workspace(3))
        header_layout.addWidget(self._stats_nav)

        header_layout.addStretch()

        settings_header_btn = QPushButton("设置")
        settings_header_btn.setObjectName("headerAction")
        settings_header_btn.clicked.connect(self._on_open_settings)
        header_layout.addWidget(settings_header_btn)

        shell_layout.addWidget(app_header)
        shell_layout.addWidget(self._main_tabs, 1)
        self.setCentralWidget(shell)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_vision_label = QLabel("多模态：未配置")
        self._status_vision_label.setObjectName("statusChip")
        self.status_bar.addPermanentWidget(self._status_vision_label)
        self._status_text_label = QLabel("纯文本：未配置")
        self._status_text_label.setObjectName("statusChip")
        self.status_bar.addPermanentWidget(self._status_text_label)

    def _switch_workspace(self, index: int) -> None:
        """切换阅读/检索/写作/统计工作区，并同步顶部导航状态。"""
        names = ("阅读工作台", "检索工作台", "写作工作台", "统计工作台")
        self._main_tabs.setCurrentIndex(index)
        self._read_nav.setChecked(index == 0)
        self._scout_nav.setChecked(index == 1)
        self._write_nav.setChecked(index == 2)
        self._stats_nav.setChecked(index == 3)
        # 阅读时长统计：离开阅读台结算，回到阅读台且当前有文献时续计
        if index != 0:
            self._stats_tracker.stop_reading()
        elif self._current_pdf_path:
            self._stats_tracker.start_reading(
                self._current_pdf_path, self._current_reading_title())
        if 0 <= index < len(names):
            self.status_bar.showMessage(f"已切换到{names[index]}")

    def _set_reader_library_visible(self, visible: bool) -> None:
        """阅读工作台左侧文献列表可收起，给正文腾出空间。"""
        self.pdf_list.setVisible(bool(visible))
        if visible:
            self._reader_outer_splitter.setSizes([285, max(500, self.width() - 330)])
        else:
            self._reader_outer_splitter.setSizes([0, max(500, self.width() - 30)])

    def _set_reader_chat_visible(self, visible: bool) -> None:
        """阅读工作台问答栏可收起，避免三栏同时挤压正文。"""
        self.chat_tabs.setVisible(bool(visible))
        if visible:
            self._reader_inner_splitter.setSizes([max(500, self.width() - 470), 420])
        else:
            self._reader_inner_splitter.setSizes([max(500, self.width() - 80), 0])

    def _apply_styles(self) -> None:
        self.setStyleSheet(STYLESHEET)

    def _start_docling_warmup(self) -> None:
        """界面显示后再后台预热，避免拖慢窗口创建过程。"""
        if self._docling_warmup is not None and self._docling_warmup.isRunning():
            return
        # 无 parent：窗口销毁不连带销毁运行中的线程；由 track() 注册表保活到导入完成
        warmup = DoclingWarmupWorker()
        track(warmup)
        self._docling_warmup = warmup
        warmup.start()

    def _on_pdf_loaded(self, text: str):
        if self._current_pdf_path:
            self._save_current_chat()
        self._current_pdf_path = self.pdf_viewer.get_current_path()
        self._context_manager.load_pdf_text(text)
        self.pdf_list.refresh_state_badge(self._current_pdf_path)
        # 统计：开始/续计当前文献阅读时长
        self._stats_tracker.start_reading(
            self._current_pdf_path, self._current_reading_title())

        # 加载结构化文档到上下文
        doc = self.pdf_viewer.structured_document
        if doc:
            self._context_manager.load_structured_doc(doc)

        history = load_chat_history(self._current_pdf_path)
        self._context_manager.load_history(history)
        self.chat_panel.clear_messages()
        for msg in history:
            if msg["role"] == "user":
                self.chat_panel.add_user_message(msg["content"])
            elif msg["role"] == "assistant":
                self.chat_panel._insert_bubble_from_history("assistant", msg["content"])

        token_est = self._context_manager.estimate_tokens(text)
        self.status_bar.showMessage(
            f"文献已加载 | 约 {token_est:,} 个令牌 | 历史 {len(history)} 条对话"
        )
        self.chat_panel.set_input_enabled(
            self._llm_text is not None and self._context_manager.has_pdf
        )

    def _current_reading_title(self) -> str:
        """当前文献的展示标题（结构化文档标题优先，否则用文件名）。"""
        doc = self.pdf_viewer.structured_document
        if doc is not None and getattr(doc, "title", ""):
            return doc.title
        return os.path.basename(self._current_pdf_path) if self._current_pdf_path else ""

    def _on_structured_document_updated(self, path: str) -> None:
        """跨页合并后只更新检索文档，不重置当前对话。"""
        if path != self._current_pdf_path:
            return
        doc = self.pdf_viewer.structured_document
        if doc is None:
            return
        text = "\n\n".join(e.text for e in doc.display_elements if e.text)
        self._context_manager.update_document(text, doc)
        # 阅读侧接缝定稿（LLM 精修完成）：库问答索引跟进该篇最终版段落
        key = self.library_qa_panel.item_key_for_pdf(path)
        if key and self.library_qa_panel.refresh_engine_item(key):
            self.library_qa_panel.flush_engine()

    def _save_current_chat(self):
        if self._current_pdf_path:
            save_chat_history(self._current_pdf_path, self._context_manager.get_history())

    def _begin_pdf_switch(self, path: str) -> None:
        """切换文献前立即清空当前会话，避免上一篇论文的问答残留。"""
        if self._current_pdf_path:
            self._save_current_chat()
        self._stats_tracker.stop_reading()
        self._cancel_llm_worker()
        self._current_pdf_path = ""
        self._context_manager.load_pdf_text("")
        self.chat_panel.clear_messages()
        self.chat_panel.set_token_count(0)
        self.chat_panel.set_input_enabled(False)
        self.status_bar.showMessage(f"正在加载文献：{os.path.basename(path)}")

    def _on_pdf_path_changed(self, path: str):
        fname = os.path.basename(path) if path else ""
        self.setWindowTitle(f"PaperWB · {fname}" if fname else "PaperWB · AI 论文研究工作台")

    def _on_library_pdf_removed(self, path: str):
        """从库中移除文献：先停后台线程，再删缓存与文件（顺序不可颠倒）。"""
        # 先丢弃当前会话引用：对话历史即将被删除，不能在切换/关窗时写回复活
        if self._current_pdf_path == path:
            self._current_pdf_path = ""
            self._context_manager.load_pdf_text("")
            self.chat_panel.clear_messages()
            self.chat_panel.set_token_count(0)
            self.chat_panel.set_input_enabled(False)
            self.pdf_viewer.clear_pdf()
            self.setWindowTitle("PaperWB · AI 论文研究工作台")
        elif self.pdf_viewer.get_current_path() == path:
            self.pdf_viewer.clear_pdf()

        self._cancel_processor(path)

        from .utils.config import (
            delete_chat_history, delete_doc_state, delete_page_cache,
            remove_pdf_from_library,
        )
        delete_chat_history(path)
        delete_doc_state(path)
        delete_page_cache(path)
        remove_pdf_from_library(path)
        try:
            os.remove(path)
        except OSError as e:
            print(f"[Library] 删除文件失败: {e}")
        self.pdf_list._refresh()

    def _on_pdf_imported(self, path: str):
        if not path:
            return
        self._stats_tracker.record_import()
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _on_library_pdf_selected(self, path: str):
        if not path:
            return
        if path != self.pdf_viewer.get_current_path():
            self._begin_pdf_switch(path)
            self._load_pdf_into_viewer(path)

    def _on_library_pdf_reload(self, path: str):
        """重新加载文献：必须先取消后台处理器再清缓存，否则旧线程会向
        已删除的缓存目录回写数据、且复用的 manifest 仍认为缓存完整。"""
        from .utils.config import (
            delete_chat_history, delete_doc_state, delete_page_cache,
        )
        self._cancel_processor(path)
        delete_chat_history(path)
        delete_doc_state(path)
        delete_page_cache(path)
        # 对话历史已删除：切换时不能把内存里的旧会话重新写盘
        if self._current_pdf_path == path:
            self._current_pdf_path = ""
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _load_pdf_into_viewer(self, path: str) -> None:
        """统一入口：复用/创建处理器 → 注入客户端 → 加载 PDF → 登记进度回调。

        切换文献不取消其它论文的后台处理器（后台解析/整合继续运行并自动落盘），
        仅在超过 _MAX_PROCESSORS 时淘汰最旧的闲置处理器。
        """
        existing = self._processors.get(path)
        if existing is None and self._preparser is not None:
            # 后台建库正在解析同一文献：直接接管在途处理器，不重复解析
            existing = self._preparser.active_processor(path)
        if existing is not None:
            try:
                manifest = existing.manifest
                current_mtime = os.path.getmtime(path)
                if manifest is None or abs(manifest.pdf_mtime - current_mtime) > 1.0:
                    self._cancel_processor(path)
                    existing = None
            except (OSError, AttributeError, TypeError, ValueError):
                self._cancel_processor(path)
                existing = None
        old_proc = getattr(self.pdf_viewer, '_processor', None)
        self.pdf_viewer.set_text_client(self._llm_text)
        self.pdf_viewer.load_pdf(path, existing_processor=existing)
        proc = getattr(self.pdf_viewer, '_processor', None)
        if proc is not None:
            proc.set_llm_client(self._llm_text)  # 后台处理器也同步最新纯文本接口
            self._processors[path] = proc
            # viewer.load_pdf 每次都先 detach（断开全部连接）再 attach，
            # 故旧处理器（含同路径重载）的连接已被清除，这里按对象幂等重连
            if old_proc is not None:
                self._app_progress_connected.discard(old_proc)
            if proc not in self._app_progress_connected:
                proc.stage1_progress.connect(self._on_processor_progress)
                self._app_progress_connected.add(proc)
            self._evict_idle_processors(keep_path=path)

    def _evict_idle_processors(self, keep_path: str) -> None:
        """超过上限时淘汰最旧的闲置处理器，避免长会话内存无界增长。

        dict 按插入序遍历（最旧在前）；当前正在阅读的文献与忙碌（Stage1/2
        在途）的处理器跳过，本轮挑不出受害者则等下次打开再试。
        """
        while len(self._processors) > _MAX_PROCESSORS:
            victim = None
            for path, proc in self._processors.items():
                if path in (keep_path, self._current_pdf_path):
                    continue
                if getattr(proc, "is_busy", lambda: False)():
                    continue
                victim = path
                break
            if victim is None:
                return
            self._cancel_processor(victim)

    # ---- 后台全库预解析 ----

    def _maybe_start_preparse(self) -> None:
        """库问答索引就绪（或启动 60 秒兜底）后启动后台建库解析。"""
        if self._preparser is None:
            return
        if os.environ.get("PAPERWB_DISABLE_PREPARSE"):
            return  # 自测/冒烟脚本用环境变量禁用真实解析
        if not load_config().get("preparse_enabled", True):
            return
        if self._preparser.state == "running":
            return
        self._refresh_preparse_queue()
        if self._preparser.state == "paused":
            self._preparser.resume()
        else:
            self._preparser.start()

    def _refresh_preparse_queue(self) -> None:
        """以当前 Zotero 库重建预解析队列（已解析篇目自动跳过）。"""
        if self._preparser is None:
            return
        items: list = []
        if self._zotero is not None and self._zotero.is_available:
            try:
                items = self._zotero.get_all_items()
            except Exception:  # noqa: BLE001
                items = []
        self._preparser.set_queue(
            [it.pdf_path for it in items if getattr(it, "pdf_path", "")])

    def _preparse_should_yield(self) -> bool:
        """用户侧有解析/整合在途时后台建库让路（预解析自己的处理器除外）。"""
        if self._preparser is None:
            return False
        mine = self._preparser.current_processor
        viewer_proc = getattr(self.pdf_viewer, "_processor", None)
        if viewer_proc is not None and viewer_proc is not mine \
                and getattr(viewer_proc, "is_busy", lambda: False)():
            return True
        for proc in self._processors.values():
            if proc is not mine and getattr(proc, "is_busy", lambda: False)():
                return True
        return False

    def _on_preparse_progress(self, done: int, total: int, name: str) -> None:
        text = f"后台建库解析：{done}/{total}"
        if name:
            text += f" · {name}"
        self.library_qa_panel.set_preparse_status(text)

    def _on_preparse_state_changed(self, state: str) -> None:
        if state == "idle":
            self._flush_preparse()
            self.library_qa_panel.set_preparse_status("")
        elif state == "paused":
            self._flush_preparse()
            self.library_qa_panel.set_preparse_status("后台建库解析：已暂停")

    def _flush_preparse(self) -> None:
        """收口单篇索引刷新：落盘并重建全文检索器。"""
        self._preparse_flush_count = 0
        self.library_qa_panel.flush_engine(reindex=True)

    def _on_preparse_item_done(self, path: str) -> None:
        """预解析完成一篇：升级库问答全文索引（每 10 篇节流落盘一次）。"""
        key = self.library_qa_panel.item_key_for_pdf(path)
        if not key:
            return
        self.library_qa_panel.refresh_engine_item(key)
        self._preparse_flush_count += 1
        if self._preparse_flush_count >= 10:
            self.library_qa_panel.flush_engine(reindex=False)
            self._preparse_flush_count = 0

    def _on_preparse_toggled(self, enabled: bool) -> None:
        config = load_config()
        config["preparse_enabled"] = enabled
        save_config(config)
        self._config = config
        if self._preparser is None:
            return
        if enabled:
            self._maybe_start_preparse()
        else:
            self._preparser.stop()
            self._flush_preparse()
            self.library_qa_panel.set_preparse_status("")

    def _cancel_processor(self, path: str) -> None:
        """取消并移除指定文献的后台处理器（显式重跑/删除/超量淘汰时使用）。"""
        proc = self._processors.pop(path, None)
        if self._preparser is not None:
            # 该文献可能正被后台建库预解析：一并取消，避免旧线程回写已删缓存
            self._preparser.cancel_if_active(path)
        if proc is not None:
            self._app_progress_connected.discard(proc)
            if hasattr(proc, 'cancel'):
                proc.cancel()

    # ---- 右键菜单：分开重跑 ----
    def _on_restage1(self, path: str):
        """重新逐页解析 —— 清除页缓存和旧整合结果后重新进入 Stage 1。"""
        from .utils.config import delete_page_cache, load_doc_state, save_doc_state
        self._cancel_processor(path)
        delete_page_cache(path)
        state = load_doc_state(path)
        state.pop("structured_document", None)
        state.pop("merged_seams", None)
        state.pop("merged_seams_prelim", None)
        state.pop("seams_final", None)
        state.pop("pdf_mtime", None)
        save_doc_state(path, state)
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _on_restage2(self, path: str):
        """重新跨页整合 —— 只清除整合结果。"""
        from .utils.config import load_doc_state, save_doc_state
        self._cancel_processor(path)
        state = load_doc_state(path)
        state.pop("structured_document", None)
        state.pop("merged_seams", None)
        state.pop("merged_seams_prelim", None)
        state.pop("seams_final", None)
        save_doc_state(path, state)
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _on_reparse(self, path: str):
        """重新解析+整合 —— 先停后台处理器，再清页缓存与整合结果，全流程自动重跑。

        顺序很重要：必须先 cancel 再删除缓存，否则正在运行的 Docling 线程
        会在删除后通过 makedirs 重新创建缓存目录并写回 manifest/页面，
        导致新处理器读到不一致的零完成页状态。
        """
        from .utils.config import delete_doc_state, delete_page_cache
        self._cancel_processor(path)
        delete_page_cache(path)
        delete_doc_state(path)
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _on_processor_progress(self, pdf_path: str, current: int, total: int):
        if self.sender() is not self._processors.get(pdf_path):
            return
        self.pdf_list.update_pdf_progress(pdf_path, current, total)

    def _cancel_llm_worker(self) -> None:
        if self._llm_worker and self._llm_worker.isRunning():
            # 优雅中断：不再 terminate（原生 HTTP 调用中强杀线程会随机崩溃），
            # 线程由注册表保活，流式循环检测到中断后自然退出。
            self._llm_worker.requestInterruption()
        self._llm_worker = None

    def _on_follow_up_from_reader(self, question: str, image_path: str = ""):
        client = self._llm_vision if image_path else self._llm_text
        if not client:
            if image_path and self._llm_text:
                # 多模态接口未配置：降级为纯文本提问（不带图），仅提示一次
                client = self._llm_text
                self.status_bar.showMessage("多模态接口未配置，图表问答已降级为纯文本提问")
            else:
                QMessageBox.warning(self, "未配置接口", "请先在设置中配置接口。")
                return
        if self._llm_worker and self._llm_worker.isRunning():
            self.status_bar.showMessage("已有问题正在回答，请等待当前回答完成")
            return
        self._stats_tracker.record_qa()
        self.chat_panel.set_input_enabled(True)
        self.chat_panel.add_user_message(question)
        self._context_manager.add_to_history("user", question)
        messages = self._context_manager.build_messages(question)
        if image_path and client is self._llm_vision:
            messages = self._attach_vision_message(messages, question, image_path)
        self.chat_panel.start_ai_response()
        self._cancel_llm_worker()
        self._llm_worker = LLMWorker(client, messages)
        track(self._llm_worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        self._llm_worker.chunk_received.connect(self._on_ai_chunk)
        self._llm_worker.finished.connect(self._on_ai_finished)
        self._llm_worker.error.connect(self._on_ai_error)
        self._llm_worker.start()

    @staticmethod
    def _attach_vision_message(messages: list[dict], question: str,
                               image_path: str) -> list[dict]:
        """把最后一条 user 消息替换为多模态内容（图片 base64 data URI + 问题）。

        对话历史仍保存纯文本问题，视觉内容只随本次请求发送。
        """
        import base64
        if not image_path or not os.path.exists(image_path):
            return messages
        try:
            with open(image_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("ascii")
        except OSError:
            return messages
        ext = os.path.splitext(image_path)[1].lower()
        mime = {"jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(
            ext.lstrip("."), "image/png")
        msgs = [dict(m) for m in messages]
        msgs[-1] = {
            "role": "user",
            "content": [
                {"type": "text", "text": question},
                {"type": "image_url",
                 "image_url": {"url": f"data:{mime};base64,{b64}"}},
            ],
        }
        return msgs

    def _on_user_message(self, text: str):
        if not self._llm_text:
            QMessageBox.warning(self, "未配置纯文本接口", "请先在设置中配置纯文本接口。")
            self.chat_panel.set_input_enabled(False)
            return
        if not self._context_manager.has_pdf:
            QMessageBox.warning(self, "未加载 PDF", "请先打开一个 PDF 文件。")
            self.chat_panel.set_input_enabled(False)
            return
        if self._llm_worker and self._llm_worker.isRunning():
            self.status_bar.showMessage("已有问题正在回答，请等待当前回答完成")
            return

        self._stats_tracker.record_qa()
        self.chat_panel.add_user_message(text)
        self._context_manager.add_to_history("user", text)
        messages = self._context_manager.build_messages(text)
        self.chat_panel.start_ai_response()
        self.status_bar.showMessage("AI 正在思考...")
        self._cancel_llm_worker()
        self._llm_worker = LLMWorker(self._llm_text, messages)
        track(self._llm_worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        self._llm_worker.chunk_received.connect(self._on_ai_chunk)
        self._llm_worker.finished.connect(self._on_ai_finished)
        self._llm_worker.error.connect(self._on_ai_error)
        self._llm_worker.start()

    def _on_ai_chunk(self, chunk: str):
        if self.sender() is not self._llm_worker:
            return  # 旧 worker 的残余流直接丢弃，不污染当前答复
        self.chat_panel.append_ai_text(chunk)

    def _on_ai_finished(self):
        if self.sender() is not self._llm_worker:
            return
        ai_text = ""
        if self.chat_panel._current_ai_bubble:
            ai_text = self.chat_panel._current_ai_bubble.get_content()
        self._context_manager.add_to_history("assistant", ai_text)
        self.chat_panel.finish_ai_response()
        self.chat_panel.set_token_count(self._context_manager.estimate_tokens(
            self._context_manager.get_full_context_for_estimation()
        ))
        if self._current_pdf_path:
            save_chat_history(self._current_pdf_path, self._context_manager.get_history())
        self.status_bar.showMessage("就绪")
        self._llm_worker = None

    def _on_ai_error(self, error_msg: str):
        if self.sender() is not self._llm_worker:
            return
        self.chat_panel.append_ai_text(f"\n\n❌ 错误：{error_msg}")
        self.chat_panel.finish_ai_response()
        self.status_bar.showMessage(f"错误：{error_msg}")
        self._llm_worker = None

    def _on_clear_chat(self):
        self._cancel_llm_worker()
        self._context_manager.clear_history()
        self.chat_panel.clear_messages()
        self.chat_panel.set_token_count(0)
        if self._current_pdf_path:
            delete_chat_history(self._current_pdf_path)
        self.status_bar.showMessage("对话已清空")

    def _on_open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec():
            self._config = load_config()
            self._init_all_clients()
            self._init_write()
            self.status_bar.showMessage("API 接口设置已更新")

    def _open_directory_setting(self, config_key: str) -> bool:
        if config_key == "data_root" and not self._writing_panel.prepare_storage_switch():
            return False
        dialog = DirectorySettingDialog(config_key, self)
        if not dialog.exec():
            return False
        self._config = load_config()
        if config_key == "zotero_data_dir":
            self._init_write()
            self.status_bar.showMessage("Zotero 文献库路径已更新")
        else:
            self._cancel_llm_worker()
            if self._preparser is not None:
                # 数据根目录已换：停掉旧缓存上的预解析，索引随新引擎重建
                self._preparser.stop()
            for path in list(self._processors):
                self._cancel_processor(path)
            self._app_progress_connected.clear()
            self.pdf_viewer.clear_pdf()
            self._current_pdf_path = ""
            self._context_manager.load_pdf_text("")
            self.chat_panel.clear_messages()
            self.chat_panel.set_token_count(0)
            self.chat_panel.set_input_enabled(False)
            self.pdf_list._refresh()
            self._stats_tracker.flush()  # 旧目录状态先落盘
            self._stats_panel.reload_storage()
            self._writing_panel.reload_storage()
            workbench_reloaded = self._workbench_panel.reload_storage()
            qa_reloaded = self.library_qa_panel.reload_storage()
            self.setWindowTitle("PaperWB · AI 论文研究工作台")
            if workbench_reloaded and qa_reloaded:
                self.status_bar.showMessage("缓存文件存储路径已更新")
            else:
                self.status_bar.showMessage("缓存路径已更新，后台任务退出后再刷新工作台")
        return True

    def _on_change_zotero_dir(self):
        """独立设置阅读与写作共用的 Zotero 数据目录。"""
        self._open_directory_setting("zotero_data_dir")

    def _on_change_data_dir(self):
        """独立设置论文、缓存和写作数据的根目录。"""
        self._open_directory_setting("data_root")

    def _on_about(self) -> None:
        QMessageBox.about(
            self, "关于 PaperWB",
            "<h3>PaperWB</h3>"
            "<p>AI 论文解读助手 v1.0.0</p>"
            "<p>支持 DeepSeek、Mimo、OpenCode 及所有 OpenAI 兼容接口。</p>"
             "<p>两套接口：多模态（图表问答）+ 纯文本（论文问答/翻译/写作/文献检索）</p>"
             "<p>本地版式解析 · 结构化阅读视图 · 知识库驱动写作辅助 · Zotero 引文核查 · PubMed 文献检索</p>"
             "<p>论文问答双页签：本篇论文问答 + 全文献库跨文献问答（BM25 全库索引）；"
             "检索工作台：自然语言 AI 检索（OpenAlex / PubMed / arXiv 三源两轮）+ 按库推荐 + 定向文献巡视（自动过滤库内已有）</p>"
        )

    def closeEvent(self, event) -> None:
        if not self._writing_panel.shutdown():
            event.ignore()
            return
        self._save_current_chat()
        self._stats_tracker.stop_reading()
        self._stats_panel.shutdown()
        self._workbench_panel.shutdown()
        self.library_qa_panel.shutdown()
        if self._preparser is not None:
            self._preparser.stop()
            self.library_qa_panel.flush_engine()
        if self._zotero_watcher is not None:
            self._zotero_watcher.stop()
        for proc in self._processors.values():
            if hasattr(proc, 'cancel'):
                proc.cancel()
        if self._llm_worker and self._llm_worker.isRunning():
            self._llm_worker.requestInterruption()
        if self._docling_warmup and self._docling_warmup.isRunning():
            # 导入阶段无法安全中断：有界等待，超时后放行关窗。
            # 线程由 track() 注册表保活到自然退出，卡死的模块导入不该冻住窗口。
            self._docling_warmup.wait(10_000)
        # 有界等待剩余后台线程自然退出（LLM 调用中的线程由注册表保活，
        # 最多等待 15 秒；空闲时立即返回，不拖慢正常关窗）。
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            pending = any(
                p.is_stage1_running or p.is_stage2_running
                for p in self._processors.values()
            )
            llm_busy = self._llm_worker is not None and self._llm_worker.isRunning()
            wb_busy = self._workbench_panel.has_busy_workers()
            qa_busy = self.library_qa_panel.has_busy_workers()
            if not pending and not llm_busy and not wb_busy and not qa_busy:
                break
            QApplication.processEvents()
            time.sleep(0.1)
        super().closeEvent(event)

    def _init_all_clients(self) -> None:
        def _make_client(cfg: dict) -> LLMClient | None:
            if cfg.get("api_key") and cfg.get("base_url") and cfg.get("model"):
                return LLMClient(cfg["api_key"], cfg["base_url"], cfg["model"])
            return None

        vision_cfg = get_vision_api(self._config)
        text_cfg = get_text_api(self._config)

        self._llm_vision = _make_client(vision_cfg)
        self._llm_text = _make_client(text_cfg)

        self.pdf_viewer.set_text_client(self._llm_text)
        self.chat_panel.set_input_enabled(self._llm_text is not None)
        self.library_qa_panel.set_text_client(self._llm_text)
        self._workbench_panel.set_text_client(self._llm_text)

        def _label(client, prefix):
            if client:
                return f"{prefix}: {client.model}"
            return f"{prefix}: 未配置"

        def _set_chip(label: QLabel, client: LLMClient | None, text: str) -> None:
            label.setText(text)
            label.setProperty("status", "ready" if client else "warning")
            label.style().unpolish(label)
            label.style().polish(label)

        _set_chip(self._status_vision_label, self._llm_vision, _label(self._llm_vision, "多模态"))
        _set_chip(self._status_text_label, self._llm_text, _label(self._llm_text, "纯文本"))

    def _init_write(self, zotero_path: str = "") -> None:
        # 优先使用信号传来的路径，其次重新从磁盘读 config
        if zotero_path:
            zotero_dir = zotero_path
        else:
            self._config = load_config()
            zotero_dir = self._config.get("zotero_data_dir", "")

        # 停掉旧 watcher（重建 ZoteroLibrary）
        if self._zotero_watcher is not None:
            self._zotero_watcher.stop()
            self._zotero_watcher = None

        self._zotero = ZoteroLibrary(zotero_dir)
        try:
            count = self._zotero.load()
        except Exception as e:  # noqa: BLE001
            # 探测/复制数据库的意外错误不应阻断主窗口启动
            print(f"[Zotero] 初始加载异常: {e}")
            count = 0
        if count == 0 and zotero_dir:
            QMessageBox.warning(
                self, "Zotero 加载失败",
                f"未能从指定目录加载到文献：\n{zotero_dir}\n\n请检查该目录是否包含 zotero.sqlite 和 storage/ 子目录。"
            )
        self._writing_panel.set_text_client(self._llm_text)
        self._writing_panel.set_zotero_library(self._zotero)
        # 写作「文献补充」的检索结果统一推送到检索工作台推荐流
        if not self._feed_bridge_connected:
            self._writing_panel.feed_requested.connect(
                lambda papers, label: self._workbench_panel.add_to_feed(papers, label))
            self._feed_bridge_connected = True

        # 周期同步 watcher
        self._zotero_watcher = ZoteroWatcher(self._zotero, parent=self)
        self._zotero_watcher.changed.connect(self._on_zotero_changed)
        if self._zotero.is_available:
            self._zotero_watcher.start()
        self.pdf_list.zotero_panel.set_library(self._zotero)
        self.pdf_list.zotero_panel.set_watcher(self._zotero_watcher)
        self.library_qa_panel.set_zotero_library(self._zotero)
        self._workbench_panel.set_zotero_library(self._zotero)

    def _on_zotero_pdf_selected(self, path: str) -> None:
        """点击 Zotero 文献 PDF —— 直接进入两阶段阅读管线（只读，不导入本地库）。"""
        if not path:
            return
        if not os.path.exists(path):
            QMessageBox.warning(self, "文件缺失", f"找不到 PDF 文件：\n{path}")
            return
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _on_zotero_changed(self, diff: dict) -> None:
        """Zotero 侧增删改 → 刷新写作面板状态 + 全库问答索引 + 巡视比对池。"""
        self._writing_panel.refresh_zotero_status()
        self.library_qa_panel.on_zotero_changed()
        self._workbench_panel.on_zotero_changed()
        # 新入库的 PDF 纳入后台预解析（已解析的自动跳过）
        self._refresh_preparse_queue()
        if self._preparser is not None and self._preparser.state == "idle":
            self._maybe_start_preparse()

    def _validate_data_root(self) -> None:
        """启动时校验缓存文件存储路径是否可访问。"""
        dr = self._config.get("data_root", "")
        if not dr:
            return
        from pathlib import Path
        p = Path(dr)
        if not p.exists():
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                QMessageBox.warning(
                    self, "缓存文件存储路径不可用",
                    f"存储路径无法创建：\n{dr}\n\n请在菜单「设置 → 缓存文件存储路径设置...」中重新设置。"
                )
                return
        if not os.access(str(p), os.W_OK):
            QMessageBox.warning(
                self, "缓存文件存储路径无写入权限",
                    f"存储路径无写入权限：\n{dr}\n\n请检查权限或在菜单「设置 → 缓存文件存储路径设置...」中重新设置。"
            )
