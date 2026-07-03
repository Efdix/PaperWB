"""PDFasker 主窗口 v2 —— 阅读（两阶段视觉 LLM 管线） + 写作（知识库/引文核查/风格分析/文献推荐）。"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, QThread, Signal as QtSignal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMessageBox, QPushButton,
    QSplitter, QStatusBar, QTabWidget, QVBoxLayout, QWidget,
)

from .core.context_manager import ContextManager
from .core.llm_client import LLMClient
from .core.review_checker import ReviewChecker
from .core.zotero_parser import ZoteroLibrary
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


class FirstLaunchDialog(QDialog):
    """首次启动：设置数据根目录。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("PDFasker — 首次设置")
        self.setMinimumWidth(500)
        self.setModal(True)

        layout = QVBoxLayout(self)
        layout.setSpacing(16)

        title = QLabel("欢迎使用 PDFasker")
        title.setStyleSheet("font-size: 18px; font-weight: bold; color: #7aa2f7;")
        layout.addWidget(title)

        desc = QLabel(
            "请选择一个文件夹作为数据根目录。\n"
            "所有 PDF 论文、阅读缓存、写作知识库和草稿都将存储在这个目录下。\n\n"
            "后续可在菜单「设置 → 数据目录...」中更改。"
        )
        desc.setWordWrap(True)
        desc.setStyleSheet("color: #cfd2e3; font-size: 13px; line-height: 1.6;")
        layout.addWidget(desc)

        form = QFormLayout()
        form.setSpacing(8)

        from pathlib import Path
        default_path = str(Path.home() / "Documents" / "PDFasker_Data")

        self._path_edit = QLineEdit(default_path)
        self._path_edit.setStyleSheet(
            "QLineEdit { background-color: #1e2030; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; padding: 8px 12px; font-size: 14px; }"
            "QLineEdit:focus { border-color: #7aa2f7; }"
        )
        browse_btn = QPushButton("浏览...")
        browse_btn.clicked.connect(self._browse)
        row = QWidget()
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.addWidget(self._path_edit)
        row_layout.addWidget(browse_btn)
        form.addRow("数据根目录：", row)
        layout.addLayout(form)

        note = QLabel("该目录将自动创建所需的子目录结构。")
        note.setStyleSheet("color: #636688; font-size: 11px;")
        layout.addWidget(note)

        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("开始使用")
        buttons.accepted.connect(self._accept)
        buttons.setStyleSheet(
            "QPushButton { background: #7aa2f7; color: #1a1b26; font-weight: bold; "
            "border-radius: 6px; padding: 8px 32px; font-size: 14px; }"
            "QPushButton:hover { background: #89b4fa; }"
        )
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
        self._current_pdf_path: str = ""

        self._processors: dict[str, object] = {}
        self._zotero: ZoteroLibrary | None = None
        self._review_checker: ReviewChecker | None = None

        self._setup_ui()
        self._apply_styles()
        self._init_all_clients()
        self._init_write()

    def _setup_ui(self):
        menubar = self.menuBar()
        file_menu = menubar.addMenu("文件(&F)")
        open_action = QAction("打开 PDF...", self)
        open_action.setShortcut("Ctrl+O")
        open_action.triggered.connect(self._on_open_pdf)
        file_menu.addAction(open_action)
        file_menu.addSeparator()
        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)

        settings_menu = menubar.addMenu("设置(&S)")
        api_action = QAction("API 配置...", self)
        api_action.setShortcut("Ctrl+,")
        api_action.triggered.connect(self._on_open_settings)
        settings_menu.addAction(api_action)
        settings_menu.addSeparator()
        data_dir_action = QAction("数据目录...", self)
        data_dir_action.triggered.connect(self._on_change_data_dir)
        settings_menu.addAction(data_dir_action)

        help_menu = menubar.addMenu("帮助(&H)")
        about_action = QAction("关于", self)
        about_action.triggered.connect(self._on_about)
        help_menu.addAction(about_action)

        self._main_tabs = QTabWidget()

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
        outer_splitter.setSizes([200, 1000])
        outer_splitter.setStretchFactor(0, 0)
        outer_splitter.setStretchFactor(1, 1)

        self._main_tabs.addTab(outer_splitter, "📖 阅读")

        # Tab 1: 写作
        self._writing_panel = WritingPanel()
        self._main_tabs.addTab(self._writing_panel, "📝 写作")

        self.setCentralWidget(self._main_tabs)

        self.status_bar = QStatusBar()
        self.setStatusBar(self.status_bar)
        self._status_parse_label = QLabel("📖解析: -")
        self._status_parse_label.setStyleSheet("color: #636688; padding: 2px 6px; font-size: 11px;")
        self.status_bar.addPermanentWidget(self._status_parse_label)
        self._status_translate_label = QLabel("📖翻译: -")
        self._status_translate_label.setStyleSheet("color: #636688; padding: 2px 6px; font-size: 11px;")
        self.status_bar.addPermanentWidget(self._status_translate_label)
        self._status_write_label = QLabel("📝写作: -")
        self._status_write_label.setStyleSheet("color: #636688; padding: 2px 6px; font-size: 11px;")
        self.status_bar.addPermanentWidget(self._status_write_label)

    def _apply_styles(self) -> None:
        self.setStyleSheet(STYLESHEET)

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
            f"PDF 已加载 | 约 {token_est:,} tokens | 历史 {len(history)} 条对话"
        )
        self.chat_panel.set_input_enabled(True)

    def _save_current_chat(self):
        if self._current_pdf_path:
            save_chat_history(self._current_pdf_path, self._context_manager.get_history())

    def _on_pdf_path_changed(self, path: str):
        fname = os.path.basename(path) if path else ""
        self.setWindowTitle(f"PDFasker — {fname}" if fname else "PDFasker — AI 论文解读助手")

    def _on_library_pdf_removed(self, path: str):
        self.pdf_viewer._reset_view()
        self.setWindowTitle("PDFasker — AI 论文解读助手")
        if path in self._processors:
            proc = self._processors.pop(path)
            if hasattr(proc, 'cancel'):
                proc.cancel()

    def _on_pdf_imported(self, path: str):
        if not path:
            return
        self._save_current_chat()
        self._current_pdf_path = ""
        self.pdf_viewer._reset_view()
        self.pdf_viewer.set_parse_client(self._llm_parse)
        self.pdf_viewer.set_translate_client(self._llm_translate)
        self.pdf_viewer.load_pdf(path)

        if hasattr(self.pdf_viewer, '_processor') and self.pdf_viewer._processor:
            proc = self.pdf_viewer._processor
            self._processors[path] = proc
            proc.stage1_progress.connect(self._on_processor_progress)

    def _on_library_pdf_selected(self, path: str):
        if not path:
            return
        if path != self.pdf_viewer.get_current_path():
            self._save_current_chat()
            self.pdf_viewer.set_parse_client(self._llm_parse)
            self.pdf_viewer.set_translate_client(self._llm_translate)
            self.pdf_viewer.load_pdf(path)
            if hasattr(self.pdf_viewer, '_processor') and self.pdf_viewer._processor:
                proc = self.pdf_viewer._processor
                self._processors[path] = proc
                proc.stage1_progress.connect(self._on_processor_progress)

    def _on_library_pdf_reload(self, path: str):
        if path:
            self._save_current_chat()
            self._current_pdf_path = ""
            from .utils.config import delete_page_cache
            delete_page_cache(path)
            self.pdf_viewer._reset_view()
            self.pdf_viewer.set_parse_client(self._llm_parse)
            self.pdf_viewer.set_translate_client(self._llm_translate)
            self.pdf_viewer.load_pdf(path)

    # ---- 右键菜单：分开重跑 ----
    def _on_restage1(self, path: str):
        """重新逐页解析 —— 清除 page_cache，保留整合结果。"""
        from .utils.config import delete_page_cache
        delete_page_cache(path)
        self._current_pdf_path = ""
        self.pdf_viewer._reset_view()
        self.pdf_viewer.set_parse_client(self._llm_parse)
        self.pdf_viewer.set_translate_client(self._llm_translate)
        self.pdf_viewer.load_pdf(path)

    def _on_restage2(self, path: str):
        """重新跨页整合 —— 只清除整合结果。"""
        from .utils.config import save_doc_state
        save_doc_state(path, {})  # 清空
        self._current_pdf_path = ""
        self.pdf_viewer._reset_view()
        self.pdf_viewer.set_parse_client(self._llm_parse)
        self.pdf_viewer.set_translate_client(self._llm_translate)
        self.pdf_viewer.load_pdf(path)

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
            QMessageBox.warning(self, "未配置", "请先配置阅读-解析 API")
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
            QMessageBox.warning(self, "未配置 API", "请先配置阅读-解析 API。\n菜单 → 设置 → API 配置")
            self.chat_panel.set_input_enabled(True)
            return
        if not self._context_manager.has_pdf:
            QMessageBox.warning(self, "未加载 PDF", "请先打开一个 PDF 文件。")
            self.chat_panel.set_input_enabled(True)
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
        if self._current_pdf_path:
            delete_chat_history(self._current_pdf_path)
        self.status_bar.showMessage("对话已清空")

    def _on_open_settings(self):
        dialog = SettingsDialog(self)
        if dialog.exec():
            self._config = load_config()
            self._init_all_clients()
            self._init_write()
            self.status_bar.showMessage("API 配置已更新")

    def _on_change_data_dir(self):
        """更改数据根目录。"""
        from pathlib import Path
        current = self._config.get("data_root", "")
        path = QFileDialog.getExistingDirectory(self, "选择数据根目录", current)
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
                f"数据根目录已更改为：\n{path}\n\n"
                "注意：已有的 PDF 论文和缓存不会自动迁移，\n"
                "如需迁移请手动复制文件到新目录的 library/ 文件夹下。"
            )
            self.pdf_list._refresh()

    def _on_about(self) -> None:
        QMessageBox.about(
            self, "关于 PDFasker",
            "<h3>PDFasker</h3>"
            "<p>AI 论文解读助手 v2.0</p>"
            "<p>支持 DeepSeek V4、Mimo 及所有 OpenAI 兼容接口。</p>"
            "<p>三套 API：阅读-解析（视觉解析+整合+问答）、阅读-翻译、写作（引文核查+风格分析+文献推荐）</p>"
            "<p>🆕 视觉 LLM 两阶段管线 · 结构化阅读视图 · 知识库驱动写作辅助 · Semantic Scholar 集成</p>"
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
        for proc in self._processors.values():
            if hasattr(proc, 'cancel'):
                proc.cancel()
        if self._llm_worker and self._llm_worker.isRunning():
            self._llm_worker.quit()
            if not self._llm_worker.wait(3000):
                self._llm_worker.terminate()
                self._llm_worker.wait()
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
            return f"{prefix}: -"

        self._status_parse_label.setText(_label(self._llm_parse, "📖解析"))
        self._status_parse_label.setStyleSheet(
            f"color: {'#9ece6a' if self._llm_parse else '#636688'}; padding: 2px 6px; font-size: 11px;"
        )
        self._status_translate_label.setText(_label(self._llm_translate, "📖翻译"))
        self._status_translate_label.setStyleSheet(
            f"color: {'#9ece6a' if self._llm_translate else '#636688'}; padding: 2px 6px; font-size: 11px;"
        )
        self._status_write_label.setText(_label(self._llm_write, "📝写作"))
        self._status_write_label.setStyleSheet(
            f"color: {'#9ece6a' if self._llm_write else '#636688'}; padding: 2px 6px; font-size: 11px;"
        )

    def _init_write(self) -> None:
        zotero_dir = self._config.get("zotero_data_dir", "")
        self._zotero = ZoteroLibrary(zotero_dir)
        self._zotero.load()
        if self._llm_write:
            self._review_checker = ReviewChecker(self._llm_write, self._zotero)
            self._writing_panel.set_checker(self._review_checker)
        self._writing_panel.set_write_client(self._llm_write)
        self._writing_panel.set_zotero_library(self._zotero)
