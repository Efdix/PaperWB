"""PDFasker 主窗口 —— 本地版式阅读 + 识图问答 + 写作辅助。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, QTimer, Signal as QtSignal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget, QFrame,
)

from .core.context_manager import ContextManager
from .core.llm_client import LLMClient
from .core.zotero_parser import ZoteroLibrary
from .core.zotero_watcher import ZoteroWatcher
from .ui.chat_panel import ChatPanel
from .ui.pdf_list_panel import PDFListPanel
from .ui.pdf_viewer import PDFViewerPanel
from .ui.writing_panel import WritingPanel
from .ui.settings_dialog import SettingsDialog
from .ui.styles import STYLESHEET
from .utils.config import (
    delete_chat_history, get_parse_api, get_translate_api, get_write_api,
    has_data_root, load_chat_history, load_config, save_chat_history,
    save_config, save_draft,
)


class LLMWorker(QThread):
    chunk_received = QtSignal(str)
    finished = QtSignal()
    error = QtSignal(str)

    def __init__(self, client: LLMClient, messages: list[dict]) -> None:
        super().__init__()
        self._client = client
        self._messages = messages

    def run(self) -> None:
        try:
            for chunk in self._client.chat_stream(self._messages):
                self.chunk_received.emit(chunk)
            self.finished.emit()
        except Exception as e:
            self.error.emit(str(e))


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
        self.setWindowTitle("PDFasker · 首次设置")
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
        default_path = str(Path.home() / "Documents" / "PDFasker_Data")

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
    """PDFasker 主窗口 v2 —— 阅读 + 写作。"""

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
        self._llm_parse: LLMClient | None = None
        self._llm_translate: LLMClient | None = None
        self._llm_write: LLMClient | None = None
        self._context_manager = ContextManager(
            max_tokens=self._config.get("max_tokens", 1_000_000)
        )
        self._llm_worker: LLMWorker | None = None
        self._docling_warmup: DoclingWarmupWorker | None = None
        self._current_pdf_path: str = ""

        self._processors: dict[str, object] = {}
        self._app_progress_connected: set[int] = set()
        self._zotero: ZoteroLibrary | None = None
        self._zotero_watcher: ZoteroWatcher | None = None

        self._setup_ui()
        self._apply_styles()
        self._init_all_clients()
        self._init_write()
        self._validate_data_root()
        QTimer.singleShot(800, self._start_docling_warmup)

    def _setup_ui(self):
        self.setWindowTitle("PDFasker · AI 论文研究工作台")
        self.setMinimumSize(1180, 760)
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
        data_dir_action = QAction("缓存文件存储路径...", self)
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
        self.pdf_list.zotero_pdf_selected.connect(self._on_zotero_pdf_selected)

        inner_splitter = QSplitter(Qt.Orientation.Horizontal)
        inner_splitter.setHandleWidth(3)
        inner_splitter.setOpaqueResize(False)

        self.pdf_viewer = PDFViewerPanel()
        self.pdf_viewer.setMinimumWidth(300)
        self.pdf_viewer.pdf_loaded.connect(self._on_pdf_loaded)
        self.pdf_viewer.pdf_path_changed.connect(self._on_pdf_path_changed)
        self.pdf_viewer.follow_up_question.connect(self._on_follow_up_from_reader)

        self.chat_panel = ChatPanel()
        self.chat_panel.setMinimumWidth(250)
        self.chat_panel.send_message.connect(self._on_user_message)
        self.chat_panel.clear_requested.connect(self._on_clear_chat)

        inner_splitter.addWidget(self.pdf_viewer)
        inner_splitter.addWidget(self.chat_panel)
        inner_splitter.setSizes([550, 450])
        inner_splitter.setStretchFactor(0, 2)
        inner_splitter.setStretchFactor(1, 1)

        outer_splitter.addWidget(self.pdf_list)
        outer_splitter.addWidget(inner_splitter)
        outer_splitter.setSizes([285, 1000])
        outer_splitter.setStretchFactor(0, 0)
        outer_splitter.setStretchFactor(1, 1)

        self._main_tabs.addTab(outer_splitter, "阅读工作台")

        # Tab 1: 写作
        self._writing_panel = WritingPanel()
        self._main_tabs.addTab(self._writing_panel, "写作工作台")

        # 顶部应用栏：把工作区切换和高频操作从传统标签页中提出来。
        shell = QWidget()
        shell.setObjectName("appShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(18, 14, 18, 8)
        shell_layout.setSpacing(12)

        app_header = QFrame()
        app_header.setObjectName("appHeader")
        header_layout = QHBoxLayout(app_header)
        header_layout.setContentsMargins(16, 12, 16, 12)
        header_layout.setSpacing(10)

        brand_mark = QLabel("研")
        brand_mark.setObjectName("brandMark")
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        header_layout.addWidget(brand_mark)

        brand_text = QVBoxLayout()
        brand_text.setSpacing(0)
        brand_title = QLabel("PDFasker")
        brand_title.setObjectName("brandTitle")
        brand_text.addWidget(brand_title)
        brand_subtitle = QLabel("AI 论文研究工作台")
        brand_subtitle.setObjectName("brandSubtitle")
        brand_text.addWidget(brand_subtitle)
        header_layout.addLayout(brand_text)

        divider = QFrame()
        divider.setFrameShape(QFrame.Shape.VLine)
        divider.setStyleSheet("background-color: #3e686d; max-width: 1px;")
        header_layout.addSpacing(10)
        header_layout.addWidget(divider)
        header_layout.addSpacing(4)

        self._read_nav = QPushButton("阅读工作台")
        self._read_nav.setObjectName("workspaceNav")
        self._read_nav.setCheckable(True)
        self._read_nav.setChecked(True)
        self._read_nav.clicked.connect(lambda _checked=False: self._switch_workspace(0))
        header_layout.addWidget(self._read_nav)

        self._write_nav = QPushButton("写作工作台")
        self._write_nav.setObjectName("workspaceNav")
        self._write_nav.setCheckable(True)
        self._write_nav.clicked.connect(lambda _checked=False: self._switch_workspace(1))
        header_layout.addWidget(self._write_nav)

        header_layout.addStretch()

        self._header_status_label = QLabel("接口未配置")
        self._header_status_label.setObjectName("statusChip")
        header_layout.addWidget(self._header_status_label)

        settings_header_btn = QPushButton("设置")
        settings_header_btn.setObjectName("headerAction")
        settings_header_btn.clicked.connect(self._on_open_settings)
        header_layout.addWidget(settings_header_btn)

        shell_layout.addWidget(app_header)
        shell_layout.addWidget(self._main_tabs, 1)
        self.setCentralWidget(shell)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_parse_label = QLabel("识图：未配置")
        self._status_parse_label.setObjectName("statusChip")
        self.status_bar.addPermanentWidget(self._status_parse_label)
        self._status_translate_label = QLabel("翻译：未配置")
        self._status_translate_label.setObjectName("statusChip")
        self.status_bar.addPermanentWidget(self._status_translate_label)
        self._status_write_label = QLabel("写作与引用：未配置")
        self._status_write_label.setObjectName("statusChip")
        self.status_bar.addPermanentWidget(self._status_write_label)

    def _switch_workspace(self, index: int) -> None:
        """切换阅读/写作工作区，并同步顶部导航状态。"""
        self._main_tabs.setCurrentIndex(index)
        self._read_nav.setChecked(index == 0)
        self._write_nav.setChecked(index == 1)
        self.status_bar.showMessage("已切换到阅读工作台" if index == 0 else "已切换到写作工作台")

    def _apply_styles(self) -> None:
        self.setStyleSheet(STYLESHEET)

    def _start_docling_warmup(self) -> None:
        """界面显示后再后台预热，避免拖慢窗口创建过程。"""
        if self._docling_warmup is not None and self._docling_warmup.isRunning():
            return
        self._docling_warmup = DoclingWarmupWorker(self)
        self._docling_warmup.start()

    def _on_open_pdf(self) -> None:
        self.pdf_viewer._open_pdf()

    def _on_pdf_loaded(self, text: str):
        if self._current_pdf_path:
            self._save_current_chat()
        self._current_pdf_path = self.pdf_viewer.get_current_path()
        self._context_manager.load_pdf_text(text)

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
            self._llm_parse is not None and self._context_manager.has_pdf
        )

    def _save_current_chat(self):
        if self._current_pdf_path:
            save_chat_history(self._current_pdf_path, self._context_manager.get_history())

    def _begin_pdf_switch(self, path: str) -> None:
        """切换文献前立即清空当前会话，避免上一篇论文的问答残留。"""
        if self._current_pdf_path:
            self._save_current_chat()
        self._current_pdf_path = ""
        self._context_manager.load_pdf_text("")
        self.chat_panel.clear_messages()
        self.chat_panel.set_token_count(0)
        self.chat_panel.set_input_enabled(False)
        self.status_bar.showMessage(f"正在加载文献：{os.path.basename(path)}")

    def _on_pdf_path_changed(self, path: str):
        fname = os.path.basename(path) if path else ""
        self.setWindowTitle(f"PDFasker · {fname}" if fname else "PDFasker · AI 论文研究工作台")

    def _on_library_pdf_removed(self, path: str):
        self.pdf_viewer._reset_view()
        self.setWindowTitle("PDFasker · AI 论文研究工作台")
        if path in self._processors:
            proc = self._processors.pop(path)
            self._app_progress_connected.discard(id(proc))
            if hasattr(proc, 'cancel'):
                proc.cancel()

    def _on_pdf_imported(self, path: str):
        if not path:
            return
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _on_library_pdf_selected(self, path: str):
        if not path:
            return
        if path != self.pdf_viewer.get_current_path():
            self._begin_pdf_switch(path)
            self._load_pdf_into_viewer(path)

    def _on_library_pdf_reload(self, path: str):
        if path:
            self._begin_pdf_switch(path)
            self._load_pdf_into_viewer(path)

    def _load_pdf_into_viewer(self, path: str) -> None:
        """统一入口：复用/创建处理器 → 注入客户端 → 加载 PDF → 登记进度回调。

        不再取消其它论文的后台处理器：切换文献后，后台解析/整合继续运行并自动落盘。
        """
        existing = self._processors.get(path)
        old_proc = getattr(self.pdf_viewer, '_processor', None)
        self.pdf_viewer.set_parse_client(self._llm_parse)
        self.pdf_viewer.set_translate_client(self._llm_translate)
        self.pdf_viewer.load_pdf(path, existing_processor=existing)
        proc = getattr(self.pdf_viewer, '_processor', None)
        if proc is not None:
            proc.set_llm_client(self._llm_parse)  # 后台处理器也同步最新识图接口
            self._processors[path] = proc
            # viewer.load_pdf 每次都先 detach（断开全部连接）再 attach，
            # 故旧处理器（含同路径重载）的连接已被清除，这里按 id 幂等重连
            if old_proc is not None:
                self._app_progress_connected.discard(id(old_proc))
            if id(proc) not in self._app_progress_connected:
                proc.stage1_progress.connect(self._on_processor_progress)
                self._app_progress_connected.add(id(proc))

    def _cancel_processor(self, path: str) -> None:
        """取消并移除指定文献的后台处理器（显式重跑/删除时使用）。"""
        proc = self._processors.pop(path, None)
        if proc is not None:
            self._app_progress_connected.discard(id(proc))
            if hasattr(proc, 'cancel'):
                proc.cancel()

    # ---- 右键菜单：分开重跑 ----
    def _on_restage1(self, path: str):
        """重新逐页解析 —— 清除 page_cache 与旧处理器，保留整合结果。"""
        from .utils.config import delete_page_cache
        self._cancel_processor(path)
        delete_page_cache(path)
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _on_restage2(self, path: str):
        """重新跨页整合 —— 只清除整合结果。"""
        from .utils.config import load_doc_state, save_doc_state
        self._cancel_processor(path)
        state = load_doc_state(path)
        state.pop("structured_document", None)
        save_doc_state(path, state)
        self._begin_pdf_switch(path)
        self._load_pdf_into_viewer(path)

    def _on_processor_progress(self, pdf_path: str, current: int, total: int):
        self.pdf_list.update_pdf_progress(pdf_path, current, total)

    def _cancel_llm_worker(self) -> None:
        if self._llm_worker and self._llm_worker.isRunning():
            self._llm_worker.quit()
            if not self._llm_worker.wait(3000):
                self._llm_worker.terminate()
                self._llm_worker.wait()
        self._llm_worker = None

    def _on_follow_up_from_reader(self, context: str):
        if not self._llm_parse:
            QMessageBox.warning(self, "未配置识图接口", "请先在设置中配置识图接口。")
            return
        self.chat_panel.set_input_enabled(True)
        self.chat_panel.add_user_message(f"[追问] {context[:100]}...")
        self._context_manager.add_to_history("user", context)
        messages = self._context_manager.build_messages(context)
        self.chat_panel.start_ai_response()
        self._cancel_llm_worker()
        self._llm_worker = LLMWorker(self._llm_parse, messages)
        self._llm_worker.chunk_received.connect(self._on_ai_chunk)
        self._llm_worker.finished.connect(self._on_ai_finished)
        self._llm_worker.error.connect(self._on_ai_error)
        self._llm_worker.start()

    def _on_user_message(self, text: str):
        if not self._llm_parse:
            QMessageBox.warning(self, "未配置识图接口", "请先在设置中配置识图接口。")
            self.chat_panel.set_input_enabled(False)
            return
        if not self._context_manager.has_pdf:
            QMessageBox.warning(self, "未加载 PDF", "请先打开一个 PDF 文件。")
            self.chat_panel.set_input_enabled(False)
            return

        self.chat_panel.add_user_message(text)
        self._context_manager.add_to_history("user", text)
        messages = self._context_manager.build_messages(text)
        self.chat_panel.start_ai_response()
        self.status_bar.showMessage("AI 正在思考...")
        self._cancel_llm_worker()
        self._llm_worker = LLMWorker(self._llm_parse, messages)
        self._llm_worker.chunk_received.connect(self._on_ai_chunk)
        self._llm_worker.finished.connect(self._on_ai_finished)
        self._llm_worker.error.connect(self._on_ai_error)
        self._llm_worker.start()

    def _on_ai_chunk(self, chunk: str):
        self.chat_panel.append_ai_text(chunk)

    def _on_ai_finished(self):
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
        self.chat_panel.append_ai_text(f"\n\n❌ 错误：{error_msg}")
        self.chat_panel.finish_ai_response()
        self.status_bar.showMessage(f"错误：{error_msg}")
        self._llm_worker = None

    def _on_clear_chat(self):
        self._context_manager.clear_history()
        self.chat_panel.clear_messages()
        self.chat_panel.set_token_count(0)
        if self._current_pdf_path:
            delete_chat_history(self._current_pdf_path)
        self.status_bar.showMessage("对话已清空")

    def _on_open_settings(self):
        old_root = self._config.get("data_root", "")
        dialog = SettingsDialog(self)
        if dialog.exec():
            self._config = load_config()
            self._init_all_clients()
            self._init_write()
            new_root = self._config.get("data_root", "")
            if new_root and new_root != old_root:
                self.pdf_list._refresh()
                self.status_bar.showMessage("缓存文件存储路径已更新")
            else:
                self.status_bar.showMessage("接口设置已更新")

    def _on_change_data_dir(self):
        """更改缓存文件存储路径。"""
        from pathlib import Path
        current = self._config.get("data_root", "")
        path = QFileDialog.getExistingDirectory(self, "选择缓存文件存储路径", current)
        if path:
            try:
                Path(path).mkdir(parents=True, exist_ok=True)
            except OSError as e:
                QMessageBox.critical(self, "错误", f"无法使用该目录：{e}")
                return
            self._config["data_root"] = path
            save_config(self._config)
            QMessageBox.information(
                self, "已更新",
                f"缓存文件存储路径已更改为：\n{path}\n\n"
                "注意：已有的 PDF 论文和缓存不会自动迁移，\n"
                "如需迁移请手动复制文件到新目录的 library/ 文件夹下。"
            )
            self.pdf_list._refresh()

    def _on_about(self) -> None:
        QMessageBox.about(
            self, "关于 PDFasker",
            "<h3>PDFasker</h3>"
            "<p>AI 论文解读助手 v1.0.0</p>"
            "<p>支持 DeepSeek、Mimo、OpenCode 及所有 OpenAI 兼容接口。</p>"
             "<p>三套接口：识图、翻译、写作（引文核查+风格分析+文献推荐）</p>"
             "<p>本地版式解析 · 结构化阅读视图 · 知识库驱动写作辅助 · Zotero 引文核查 · PubMed 文献检索</p>"
        )

    def closeEvent(self, event) -> None:
        self._save_current_chat()
        # 保存写作编辑器草稿
        try:
            coach = self._writing_panel._coach
            if coach and coach.current_profile:
                text = self._writing_panel.get_editor_text()
                if text.strip():
                    save_draft(coach.current_profile.name, text)
        except Exception:
            pass
        self._writing_panel.shutdown()
        if self._zotero_watcher is not None:
            self._zotero_watcher.stop()
        for proc in self._processors.values():
            if hasattr(proc, 'cancel'):
                proc.cancel()
        if self._llm_worker and self._llm_worker.isRunning():
            self._llm_worker.quit()
            if not self._llm_worker.wait(3000):
                self._llm_worker.terminate()
                self._llm_worker.wait()
        if self._docling_warmup and self._docling_warmup.isRunning():
            # 导入阶段无法安全中断，等待其自然完成，避免 QThread 被销毁时崩溃。
            self._docling_warmup.wait()
        super().closeEvent(event)

    def _init_all_clients(self) -> None:
        def _make_client(cfg: dict) -> LLMClient | None:
            if cfg.get("api_key") and cfg.get("base_url") and cfg.get("model"):
                return LLMClient(cfg["api_key"], cfg["base_url"], cfg["model"])
            return None

        parse_cfg = get_parse_api(self._config)
        translate_cfg = get_translate_api(self._config)
        write_cfg = get_write_api(self._config)

        self._llm_parse = _make_client(parse_cfg)
        self._llm_translate = _make_client(translate_cfg)
        self._llm_write = _make_client(write_cfg)

        self.pdf_viewer.set_parse_client(self._llm_parse)
        self.pdf_viewer.set_translate_client(self._llm_translate)
        self.chat_panel.set_input_enabled(self._llm_parse is not None)

        def _label(client, prefix):
            if client:
                return f"{prefix}: {client.model}"
            return f"{prefix}: 未配置"

        def _set_chip(label: QLabel, client: LLMClient | None, text: str) -> None:
            label.setText(text)
            label.setProperty("status", "ready" if client else "warning")
            label.style().unpolish(label)
            label.style().polish(label)

        _set_chip(self._status_parse_label, self._llm_parse, _label(self._llm_parse, "识图"))
        _set_chip(self._status_translate_label, self._llm_translate, _label(self._llm_translate, "翻译"))
        _set_chip(self._status_write_label, self._llm_write, _label(self._llm_write, "写作与引用"))
        ready_count = sum(client is not None for client in (
            self._llm_parse, self._llm_translate, self._llm_write
        ))
        self._header_status_label.setText(f"{ready_count}/3 项接口已就绪")
        self._header_status_label.setProperty("status", "ready" if ready_count else "warning")
        self._header_status_label.style().unpolish(self._header_status_label)
        self._header_status_label.style().polish(self._header_status_label)

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
        count = self._zotero.load()
        if count == 0 and zotero_dir:
            QMessageBox.warning(
                self, "Zotero 加载失败",
                f"未能从指定目录加载到文献：\n{zotero_dir}\n\n请检查该目录是否包含 zotero.sqlite 和 storage/ 子目录。"
            )
        self._writing_panel.set_write_client(self._llm_write)
        self._writing_panel.set_zotero_library(self._zotero)

        # 实时同步 watcher
        self._zotero_watcher = ZoteroWatcher(self._zotero, parent=self)
        self._zotero_watcher.changed.connect(self._on_zotero_changed)
        if self._zotero.is_available:
            self._zotero_watcher.start()
        self.pdf_list.zotero_panel.set_library(self._zotero)
        self.pdf_list.zotero_panel.set_watcher(self._zotero_watcher)

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
        """Zotero 侧增删改 → 刷新写作面板状态。"""
        self._writing_panel.refresh_zotero_status()

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
                    f"存储路径无法创建：\n{dr}\n\n请在菜单「设置 → 缓存文件存储路径...」中重新设置。"
                )
                return
        if not os.access(str(p), os.W_OK):
            QMessageBox.warning(
                self, "缓存文件存储路径无写入权限",
                f"存储路径无写入权限：\n{dr}\n\n请检查权限或在菜单「设置 → 缓存文件存储路径...」中重新设置。"
            )
