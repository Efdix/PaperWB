"""PaperWB 主窗口 —— 本地版式阅读 + 图表问答 + 写作辅助。"""

from __future__ import annotations

import os
import threading
import time
import uuid
import weakref
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal as QtSignal
from PySide6.QtGui import QAction, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox, QPushButton,
    QSplitter, QStatusBar, QTabWidget, QToolButton, QVBoxLayout, QWidget, QFrame,
)

from .core.context_manager import ContextManager
from .core.library_preparser import LibraryPreparser
from .core.llm_client import LLMClient
from .core.stats_tracker import StatsTracker
from .core.updater import (
    UpdateCheckWorker, now_iso, resolve_source, should_auto_check,
)
from .core.zotero_parser import ZoteroLibrary
from .core.zotero_watcher import ZoteroWatcher
from .ui.chat_panel import ChatPanel
from .ui.library_qa_panel import LibraryQAPanel
from .ui.pdf_list_panel import PDFListPanel
from .ui.pdf_viewer import PDFViewerPanel
from .ui.stats_panel import StatsPanel
from .ui.update_dialog import UpdateDialog
from .ui.workbench_panel import WorkbenchPanel
from .ui.writing_panel import WritingPanel
from .ui.settings_dialog import DirectorySettingDialog, SettingsDialog
from .ui.styles import STYLESHEET
from .utils.config import (
    _default_data_root, _get_data_root,
    delete_chat_history, get_text_api, get_update_config, get_vision_api,
    has_data_root, load_chat_history, load_config, save_chat_history,
    save_config, set_update_last_check,
)
from .utils.threads import track
from .utils.version import current_version

# 单窗口常驻后台处理器的数量上限：超量时淘汰最旧的闲置处理器（正在解析/整合的除外）
_MAX_PROCESSORS = 8


class UpdateManager(QObject):
    """更新推送调度：启动后静默检查 + 周期检查 + 手动检查。

    - 自动检查受配置节流（``update_check.enabled`` / ``interval_hours`` /
      ``last_check``），失败静默（状态栏一句提示，绝不弹窗打断）；
    - 手动检查不受「跳过此版本」限制，失败时明确告知原因；
    - 检查线程经 ``track()`` 保活，``stop()`` 时中断并等待。
    """

    update_found = QtSignal(object, bool)   # (UpdateInfo, manual)
    status_message = QtSignal(str)

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._worker: UpdateCheckWorker | None = None
        self._timer: QTimer | None = None
        self._manual = False

    def start(self) -> None:
        """启动调度：立即（按节流）检查一次，并按间隔起周期定时器。"""
        cfg = get_update_config()
        if not cfg.get("enabled"):
            return
        self.check_now(manual=False)
        self._ensure_timer()

    def _ensure_timer(self) -> None:
        if self._timer is None:
            self._timer = QTimer(self)
            self._timer.timeout.connect(lambda: self.check_now(manual=False))
        try:
            hours = max(1, int(get_update_config().get("interval_hours") or 24))
        except (TypeError, ValueError):
            hours = 24
        # 定时器精度不敏感（小时级），上限用 int 毫秒
        self._timer.start(min(hours, 24) * 3600 * 1000)

    def stop(self) -> None:
        if self._timer is not None:
            self._timer.stop()
        self._cancel()

    def _cancel(self) -> None:
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait(3_000)
        self._worker = None

    def check_now(self, manual: bool = False) -> None:
        """发起一次检查（自动检查按节流与跳过版本过滤，手动不受限）。"""
        cfg = get_update_config()
        if manual:
            pass  # 手动检查不看开关与节流
        elif not should_auto_check(cfg):
            return
        if self._worker is not None and self._worker.isRunning():
            return  # 已有检查在途，不重复请求
        self._manual = manual
        worker = UpdateCheckWorker(
            current=current_version(),
            source=cfg.get("source", ""),
            url=cfg.get("url", ""),
            skipped_version=cfg.get("skipped_version", ""),
            respect_skipped=not manual,
        )
        self._worker = worker
        track(worker)
        worker.found.connect(self._on_found)
        worker.up_to_date.connect(self._on_up_to_date)
        worker.no_release.connect(self._on_no_release)
        worker.failed.connect(self._on_failed)
        worker.start()
        # 记录检查时间：自动检查的节流依据，也避免频繁请求被 GitHub 限流
        set_update_last_check(now_iso())

    def _on_found(self, info) -> None:
        if self.sender() is not self._worker:
            return
        self._worker = None
        self.update_found.emit(info, self._manual)

    def _on_up_to_date(self) -> None:
        if self.sender() is not self._worker:
            return
        self._worker = None
        if self._manual:
            self.status_message.emit(f"已是最新版本（v{current_version()}）")

    def _on_no_release(self) -> None:
        """更新源尚无已发布版本：自动检查静默，手动检查说明原因。"""
        if self.sender() is not self._worker:
            return
        self._worker = None
        if self._manual:
            QMessageBox.information(
                None, "检查更新",
                "更新源尚未发布任何版本（GitHub 仓库还没有 Release）。\n"
                "发布者运行 installer\\publish_release.ps1 后即可被检查到。")

    def _on_failed(self, message: str) -> None:
        if self.sender() is not self._worker:
            return
        self._worker = None
        if self._manual:
            QMessageBox.warning(None, "检查更新失败",
                                f"{message}\n\n可在「设置 → 更新」中改用自定义更新源，"
                                "或点「打开发布页」手动下载。")
        else:
            # 自动检查失败静默：一句话进状态栏，不打断任何操作
            self.status_message.emit(f"检查更新失败：{message}")


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

        default_path = str(_default_data_root())

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


class ZoteroLoadWorker(QThread):
    """后台加载 Zotero 库：复制 sqlite + 逐条查询，避免启动期阻塞主线程。"""

    loaded = QtSignal(int)
    failed = QtSignal(str)

    def __init__(self, library: ZoteroLibrary, parent=None):
        super().__init__(parent)
        self._library = library

    def run(self) -> None:
        try:
            self.loaded.emit(int(self._library.load()))
        except Exception as e:  # noqa: BLE001 — 加载失败不能拖垮主窗口
            self.failed.emit(str(e))


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
        # 必须在 _setup_ui 之前校验：WritingPanel/WorkbenchPanel 构造时就会
        # mkdir 数据目录，路径不可用会直接抛 OSError，用户连设置界面都进不去。
        self._ensure_data_root()
        self._llm_vision: LLMClient | None = None
        self._llm_text: LLMClient | None = None
        self._context_manager = ContextManager(
            max_tokens=self._config.get("max_tokens", 128_000)
        )
        self._llm_worker: LLMWorker | None = None
        self._current_pdf_path: str = ""

        self._processors: dict[str, object] = {}
        self._app_progress_connected: weakref.WeakSet = weakref.WeakSet()
        self._zotero: ZoteroLibrary | None = None
        self._zotero_watcher: ZoteroWatcher | None = None
        self._zotero_load_worker: ZoteroLoadWorker | None = None
        self._feed_bridge_connected = False
        self._closing = False
        self._preparser: LibraryPreparser | None = None
        self._preparse_flush_count = 0
        self._update_manager: UpdateManager | None = None
        self._update_dialog: UpdateDialog | None = None
        self._latest_update = None  # UpdateInfo | None，供胶囊点击复用

        self._setup_ui()
        self._apply_styles()
        self._apply_accessible_names()
        self._init_all_clients()
        self._init_stats()
        # Zotero 启动加载会复制整个 sqlite 并逐条查询，放在构造期会让窗口
        # 迟迟不出现。先让事件循环跑起来，再执行（仍是主线程，但至少先见窗）。
        QTimer.singleShot(0, self._init_write)
        QTimer.singleShot(800, self._start_docling_warmup)
        self._init_preparser()
        self._init_updater()

    def _ensure_data_root(self) -> None:
        """启动前确认 data_root 可写；不可写时让用户改回默认路径。

        不能等到面板构造时再抛 OSError——那时还没有主窗口，用户无法进入
        「缓存文件存储路径设置」自救。
        """
        dr = str(self._config.get("data_root", "") or "").strip()
        # 未显式配置时也要校验默认路径（仓库/安装目录不可写、U 盘拔出等）。
        effective = dr or str(_get_data_root(self._config) or "").strip()
        if not effective:
            return
        p = Path(effective)
        try:
            p.mkdir(parents=True, exist_ok=True)
            probe = p / f".paperwb_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}"
            probe.touch()
            probe.unlink()
            return
        except OSError as e:
            box = QMessageBox(
                QMessageBox.Icon.Critical, "数据目录不可用",
                f"当前数据目录无法写入：\n{effective}\n\n{e}\n\n"
                "可以改用默认数据目录继续启动，或退出后在设置中修复路径。\n"
                "（改用默认目录不会移动旧数据，旧文件仍留在原位置。）",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            box.setDefaultButton(QMessageBox.StandardButton.No)
            box.setWindowTitle("PaperWB")
            if box.exec() != QMessageBox.StandardButton.Yes:
                raise SystemExit(1)
            # 默认目录也可能不可写（安装进 Program Files 等）；再退到
            # %LOCALAPPDATA%/PaperWB/data，绝不带着坏路径继续构造面板。
            appdata = os.environ.get(
                "LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
            candidates = [str(_default_data_root()),
                          str(Path(appdata) / "PaperWB" / "data")]
            chosen = ""
            for cand in candidates:
                try:
                    cp = Path(cand)
                    cp.mkdir(parents=True, exist_ok=True)
                    probe = cp / f".paperwb_probe_{os.getpid()}_{uuid.uuid4().hex[:8]}"
                    probe.touch()
                    probe.unlink()
                    chosen = cand
                    break
                except OSError:
                    continue
            if not chosen:
                QMessageBox.critical(
                    self, "无法保存配置",
                    "默认数据目录与 %LOCALAPPDATA% 都不可写，无法启动。\n"
                    "请检查安装目录权限或手动修改 config.json 的 data_root。")
                raise SystemExit(1)
            self._config["data_root"] = chosen
            try:
                save_config(self._config)
            except OSError as save_err:
                QMessageBox.critical(
                    self, "无法保存配置",
                    f"数据目录可用但配置无法保存：\n{save_err}\n"
                    "请检查安装目录权限。")
                raise SystemExit(1)
            QMessageBox.information(
                self, "已切换数据目录",
                f"本次启动使用数据目录：\n{chosen}\n"
                "旧目录中的数据没有被移动或删除。")

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

    def _init_updater(self) -> None:
        """更新推送：启动后静默检查新版本（可关闭，失败不打扰）。"""
        self._update_manager = UpdateManager(self)
        self._update_manager.update_found.connect(self._on_update_found)
        self._update_manager.status_message.connect(
            lambda msg: self.status_bar.showMessage(msg))
        # 启动 15 秒后再查（避开启动高峰：预加载/预热/建库都在这时候）
        QTimer.singleShot(15_000, self._start_update_manager)

    def _start_update_manager(self) -> None:
        if self._closing or self._update_manager is None:
            return
        self._update_manager.start()

    def _on_update_found(self, info, manual: bool) -> None:
        """发现新版本：应用栏出现提示胶囊；手动检查时直接开窗。"""
        self._latest_update = info
        self._update_chip.setText(f"↑ 新版本 v{info.version}")
        self._update_chip.setToolTip(
            f"发现新版本 v{info.version}（当前 v{current_version()}）——点击查看更新说明并升级")
        self._update_chip.setVisible(True)
        self.status_bar.showMessage(f"发现新版本 v{info.version}，点应用栏提示可查看")
        if manual:
            self._open_update_dialog(manual=True)

    def _on_update_chip_clicked(self) -> None:
        self._open_update_dialog(manual=False)

    def _open_update_dialog(self, manual: bool = False) -> None:
        """打开更新窗口（复用已发现的版本信息，无信息时先查一次）。"""
        info = self._latest_update
        if info is None:
            self._check_update_manual()
            return
        if self._update_dialog is not None and self._update_dialog.isVisible():
            self._update_dialog.raise_()
            self._update_dialog.activateWindow()
            return
        dialog = UpdateDialog(info, self, manual=manual)
        self._update_dialog = dialog
        dialog.skipped.connect(self._on_update_skipped)
        dialog.installed.connect(self._on_update_installing)
        dialog.show()

    def _on_update_skipped(self, version: str) -> None:
        self._update_chip.setVisible(False)
        self._latest_update = None
        self.status_bar.showMessage(f"已跳过 v{version}（下个版本仍会提示）")

    def _on_update_installing(self) -> None:
        """安装器已启动：应用即将退出，交给启动器脚本在安装后重新拉起。"""
        self.status_bar.showMessage("正在退出以完成更新…")
        QTimer.singleShot(600, QApplication.quit)

    def _check_update_manual(self) -> None:
        """手动检查更新（☰ 菜单 / 设置页「立即检查更新」）。"""
        if self._update_manager is None:
            return
        self.status_bar.showMessage("正在检查更新…")
        self._update_manager.check_now(manual=True)

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

    def _build_app_menu(self) -> QMenu:
        """应用栏 ☰ 菜单：承接原菜单栏的全部入口（快捷键不变）。"""
        menu = QMenu(self)

        api_action = QAction("API 接口设置...", self)
        api_action.setShortcut("Ctrl+,")
        api_action.triggered.connect(self._on_open_settings)
        menu.addAction(api_action)

        zotero_dir_action = QAction("Zotero 文献库路径设置...", self)
        zotero_dir_action.triggered.connect(self._on_change_zotero_dir)
        menu.addAction(zotero_dir_action)

        data_dir_action = QAction("缓存文件存储路径设置...", self)
        data_dir_action.triggered.connect(self._on_change_data_dir)
        menu.addAction(data_dir_action)

        menu.addSeparator()
        update_action = QAction("检查更新...", self)
        update_action.triggered.connect(self._check_update_manual)
        menu.addAction(update_action)

        about_action = QAction("关于", self)
        about_action.triggered.connect(self._on_about)
        menu.addAction(about_action)

        exit_action = QAction("退出", self)
        exit_action.setShortcut("Ctrl+Q")
        exit_action.triggered.connect(self.close)
        menu.addAction(exit_action)
        return menu

    def _setup_ui(self):
        self.setWindowTitle("PaperWB · AI 论文研究工作台")
        self.setMinimumSize(1024, 680)
        self.resize(1440, 900)

        # 传统菜单栏整体撤掉（省掉一整行高度与一条分隔线），
        # 全部入口收进应用栏最左的 ☰ 菜单，快捷键保持不变。
        self.menuBar().setVisible(False)
        self._app_menu = self._build_app_menu()

        # 全局一键全屏（F11）：任何工作台可用，Esc 退出
        fs_shortcut = QShortcut(QKeySequence("F11"), self)
        fs_shortcut.activated.connect(self._toggle_fullscreen)

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
        self.pdf_viewer.setMinimumWidth(260)
        self.pdf_viewer.pdf_loaded.connect(self._on_pdf_loaded)
        self.pdf_viewer.pdf_path_changed.connect(self._on_pdf_path_changed)
        self.pdf_viewer.structured_document_updated.connect(
            self._on_structured_document_updated)
        self.pdf_viewer.follow_up_question.connect(self._on_follow_up_from_reader)
        self.pdf_viewer.fullscreen_toggled.connect(self._on_fullscreen_toggled)

        # 阅读面板开关注入阅读工具栏右端（原先是工作台标题栏里的两个按钮，
        # 标题栏已撤销，开关就近放到阅读控件旁边更符合使用路径）
        # 图标式开关（文字在 tooltip）：阅读工具栏本就控件密集，
        # 1024px 最小窗口下再多写两个字就会把控件挤出可视区。
        self._reader_library_toggle_btn = QPushButton("☰")
        self._reader_library_toggle_btn.setObjectName("paneToggle")
        self._reader_library_toggle_btn.setCheckable(True)
        self._reader_library_toggle_btn.setChecked(True)
        self._reader_library_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reader_library_toggle_btn.setToolTip("显示或隐藏左侧文献列表")
        self.pdf_viewer._toolbar_slot.addWidget(self._reader_library_toggle_btn)

        self._reader_chat_toggle_btn = QPushButton("💬")
        self._reader_chat_toggle_btn.setObjectName("paneToggle")
        self._reader_chat_toggle_btn.setCheckable(True)
        self._reader_chat_toggle_btn.setChecked(True)
        self._reader_chat_toggle_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._reader_chat_toggle_btn.setToolTip(
            "显示或隐藏右侧论文问答（本篇论文 / 全文献库 两个页签）")
        self.pdf_viewer._toolbar_slot.addWidget(self._reader_chat_toggle_btn)

        self._reader_library_toggle_btn.toggled.connect(self._on_library_toggle_clicked)
        self._reader_chat_toggle_btn.toggled.connect(self._on_chat_toggle_clicked)
        # 窄窗口下自动收起左栏，避免正文被两侧面板夹成窄条；
        # 用户一旦手动开关过该面板、或拖动过中间分隔条，就交还控制权。
        self._reader_auto_library = True
        self._reader_auto_applying = False
        self._reader_inner_custom = False

        self.chat_panel = ChatPanel()
        self.chat_panel.send_message.connect(self._on_user_message)
        self.chat_panel.clear_requested.connect(self._on_clear_chat)
        self.library_qa_panel = LibraryQAPanel()
        self.library_qa_panel.open_pdf_requested.connect(self._on_zotero_pdf_selected)

        self.chat_tabs = QTabWidget()
        self.chat_tabs.setDocumentMode(True)
        self.chat_tabs.setMinimumWidth(240)
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
        inner_splitter.splitterMoved.connect(self._on_reader_splitter_moved)

        outer_splitter.addWidget(self.pdf_list)
        outer_splitter.addWidget(inner_splitter)
        outer_splitter.setSizes([285, 1000])
        outer_splitter.setStretchFactor(0, 0)
        outer_splitter.setStretchFactor(1, 1)
        outer_splitter.setCollapsible(0, True)
        outer_splitter.setCollapsible(1, False)
        self._reader_outer_splitter = outer_splitter

        # 阅读工作台不再自带标题横幅：它占掉一整行却只写「阅读工作台」四个字，
        # 而应用栏已经标明了当前工作台。面板开关下沉到阅读区自己的动作行。
        reader_surface = QWidget()
        reader_surface.setObjectName("workspaceSurface")
        reader_layout = QVBoxLayout(reader_surface)
        reader_layout.setContentsMargins(0, 0, 0, 0)
        reader_layout.setSpacing(0)
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

        # 顶部应用栏：全窗口唯一一条 chrome。品牌、工作台切换、接口状态全在这里，
        # 高度压到 44px；正文区域直接贴到窗口边缘，不再有外层白边。
        shell = QWidget()
        shell.setObjectName("appShell")
        shell_layout = QVBoxLayout(shell)
        shell_layout.setContentsMargins(0, 0, 0, 0)
        shell_layout.setSpacing(0)

        app_header = QFrame()
        app_header.setObjectName("appBar")
        header_layout = QHBoxLayout(app_header)
        header_layout.setContentsMargins(10, 4, 10, 4)
        header_layout.setSpacing(8)

        menu_btn = QToolButton()
        menu_btn.setObjectName("appMenuBtn")
        menu_btn.setText("☰")
        menu_btn.setToolTip("菜单（API 接口 / Zotero 路径 / 缓存路径 / 关于 / 退出）")
        menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu_btn.setMenu(self._app_menu)
        header_layout.addWidget(menu_btn)

        brand_mark = QLabel("研")
        brand_mark.setObjectName("brandMark")
        brand_mark.setAlignment(Qt.AlignmentFlag.AlignCenter)
        brand_mark.setFixedSize(26, 26)
        app_icon = QApplication.windowIcon()
        if not app_icon.isNull():
            brand_mark.setText("")
            brand_mark.setPixmap(app_icon.pixmap(22, 22))
            brand_mark.setScaledContents(True)
        header_layout.addWidget(brand_mark)

        brand_title = QLabel("PaperWB")
        brand_title.setObjectName("brandTitle")
        header_layout.addWidget(brand_title)

        header_layout.addSpacing(6)

        # 整段式工作台切换：四个按钮共享一个底槽，视觉上是一个控件
        nav_seg = QFrame()
        nav_seg.setObjectName("navSegmented")
        nav_layout = QHBoxLayout(nav_seg)
        nav_layout.setContentsMargins(0, 0, 0, 0)
        nav_layout.setSpacing(0)
        self._workspace_names = ("阅读", "检索", "写作", "统计")
        self._nav_buttons: list[QPushButton] = []
        for idx, name in enumerate(self._workspace_names):
            btn = QPushButton(name)
            btn.setObjectName("workspaceNav")
            btn.setCheckable(True)
            btn.setChecked(idx == 0)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(
                lambda _checked=False, i=idx: self._switch_workspace(i))
            nav_layout.addWidget(btn)
            self._nav_buttons.append(btn)
        header_layout.addWidget(nav_seg)
        # 兼容旧引用（自检脚本按名字取导航按钮检查选中态）
        self._read_nav, self._scout_nav, self._write_nav, self._stats_nav = \
            self._nav_buttons

        header_layout.addStretch()

        # 接口状态：只留两个圆点式小胶囊，鼠标悬停看完整模型名
        self._status_vision_label = QLabel("多模态：未配置")
        self._status_vision_label.setObjectName("modelChip")
        header_layout.addWidget(self._status_vision_label)
        self._status_text_label = QLabel("纯文本：未配置")
        self._status_text_label.setObjectName("modelChip")
        header_layout.addWidget(self._status_text_label)

        # 更新提示胶囊：平时隐藏，发现新版本才出现（点击打开更新窗口）
        self._update_chip = QPushButton("")
        self._update_chip.setObjectName("updateChip")
        self._update_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._update_chip.clicked.connect(self._on_update_chip_clicked)
        self._update_chip.setVisible(False)
        header_layout.addWidget(self._update_chip)

        settings_header_btn = QPushButton("设置")
        settings_header_btn.setObjectName("headerAction")
        settings_header_btn.setToolTip("API 接口设置（Ctrl+,）")
        settings_header_btn.clicked.connect(self._on_open_settings)
        header_layout.addWidget(settings_header_btn)

        shell_layout.addWidget(app_header)
        shell_layout.addWidget(self._main_tabs, 1)
        self.setCentralWidget(shell)
        self._app_header = app_header
        self._app_menu_btn = menu_btn

        self.status_bar = QStatusBar()
        self.status_bar.setSizeGripEnabled(False)
        self.setStatusBar(self.status_bar)

    def _toggle_fullscreen(self) -> None:
        """F11 全屏切换：与阅读工具栏「⛶ 全屏阅读」按钮共用同一条路径。"""
        if self._main_tabs.currentIndex() != 0:
            self.status_bar.showMessage("F11 沉浸阅读仅用于阅读工作台")
            return
        self.pdf_viewer.toggle_fullscreen()

    def _on_fullscreen_toggled(self, entering: bool) -> None:
        """沉浸阅读：全屏时隐藏应用栏/状态栏/左右面板，整个窗口只剩正文；
        退出时按各面板开关的原状态还原。"""
        self._app_header.setVisible(not entering)
        self.status_bar.setVisible(not entering)
        if entering:
            self._set_reader_library_visible(False)
            self._set_reader_chat_visible(False)
        else:
            self._set_reader_library_visible(
                self._reader_library_toggle_btn.isChecked())
            self._set_reader_chat_visible(
                self._reader_chat_toggle_btn.isChecked())
        self.status_bar.showMessage("已进入沉浸阅读（Esc 退出）" if entering
                                    else "已退出沉浸阅读")

    def keyPressEvent(self, event) -> None:
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.pdf_viewer.toggle_fullscreen()
            return
        super().keyPressEvent(event)

    def _switch_workspace(self, index: int) -> None:
        """切换阅读/检索/写作/统计工作区，并同步顶部导航状态。"""
        names = ("阅读工作台", "检索工作台", "写作工作台", "统计工作台")
        self._main_tabs.setCurrentIndex(index)
        for i, btn in enumerate(getattr(self, "_nav_buttons", [])):
            btn.setChecked(i == index)
        # 阅读时长统计：离开阅读台结算，回到阅读台且当前有文献时续计
        if index != 0:
            self._stats_tracker.stop_reading()
        elif self._current_pdf_path:
            self._stats_tracker.start_reading(
                self._current_pdf_path, self._current_reading_title())
        if index == 0:
            # 切回阅读台时按当前窗口宽度重排一次：本工作台的侧栏比例
            # 只在 resizeEvent 里算，而从别的台切回来不会触发 resizeEvent。
            self._apply_narrow_reader_layout()
            self._apply_reader_pane_ratios()
        if 0 <= index < len(names):
            self.status_bar.showMessage(f"已切换到{names[index]}")

    # 侧栏宽度按窗口比例分配（而非固定像素）：1024px 窗口下固定 285+420
    # 会把正文压到 55%，比例分配后侧栏自动收窄，正文始终是画面主体。
    _LIBRARY_W_MIN, _LIBRARY_W_MAX = 250, 300
    _CHAT_W_MIN, _CHAT_W_MAX = 300, 430

    def _set_reader_library_visible(self, visible: bool) -> None:
        """阅读工作台左侧文献列表可收起，给正文腾出空间。"""
        self.pdf_list.setVisible(bool(visible))
        if visible:
            lib_w = max(self._LIBRARY_W_MIN,
                        min(self._LIBRARY_W_MAX, int(self.width() * 0.19)))
            self._reader_outer_splitter.setSizes(
                [lib_w, max(500, self.width() - lib_w - 45)])
        else:
            self._reader_outer_splitter.setSizes([0, max(500, self.width() - 30)])

    def _set_reader_chat_visible(self, visible: bool) -> None:
        """阅读工作台问答栏可收起，避免三栏同时挤压正文。"""
        self.chat_tabs.setVisible(bool(visible))
        if visible:
            chat_w = max(self._CHAT_W_MIN,
                         min(self._CHAT_W_MAX, int(self.width() * 0.30)))
            # 正文下限 420px：1024px 窗口下若强行保底 500px，会把问答栏
            # 压到不可用，而比例分配本身就保证正文占约 2/3。
            self._reader_inner_splitter.setSizes(
                [max(420, self.width() - chat_w - 50), chat_w])
        else:
            self._reader_inner_splitter.setSizes([max(420, self.width() - 80), 0])

    # 正文区低于此宽度时自动收起左侧文献列表（纯阅读场景下它最可让）
    _NARROW_READER_WIDTH = 1250

    def _on_library_toggle_clicked(self, checked: bool) -> None:
        """用户手动点过左栏开关：此后不再自动收起（尊重用户选择）。"""
        if not self._reader_auto_applying:
            self._reader_auto_library = False
        self._set_reader_library_visible(bool(checked))

    def _on_chat_toggle_clicked(self, checked: bool) -> None:
        if not self._reader_auto_applying:
            self._reader_auto_library = False
        self._set_reader_chat_visible(bool(checked))

    def _on_reader_splitter_moved(self, _pos: int, _index: int) -> None:
        """用户拖动过正文/问答分隔条：保留其比例，不再按窗口宽度重排。"""
        if not self._reader_auto_applying:
            self._reader_inner_custom = True

    def _apply_reader_pane_ratios(self) -> None:
        """按窗口宽度重排阅读区：正文始终占约 2/3，侧栏按比例收缩。

        只在用户未手动调整过分隔条、且问答栏可见时执行；否则尊重用户选择。
        """
        if self._reader_inner_custom:
            return
        if self._reader_auto_applying:
            return
        if not self.chat_tabs.isVisible():
            return
        total = self._reader_inner_splitter.width()
        if total <= 0:
            return
        chat_w = max(self._CHAT_W_MIN,
                     min(self._CHAT_W_MAX, int(self.width() * 0.30)))
        chat_w = min(chat_w, max(self._CHAT_W_MIN, total - 420))
        self._reader_auto_applying = True
        try:
            self._reader_inner_splitter.setSizes([total - chat_w, chat_w])
        finally:
            self._reader_auto_applying = False

    def _apply_narrow_reader_layout(self) -> None:
        """窗口偏窄时自动收起文献列表：三栏并排会把正文压到 40% 宽。

        只动左栏（阅读时最可让的一栏），且用程序化 setChecked 同步按钮，
        避免被误判成用户操作而永久关闭自动布局。
        """
        if not self._reader_auto_library:
            return
        wide_enough = self.width() >= self._NARROW_READER_WIDTH
        if wide_enough == self._reader_library_toggle_btn.isChecked():
            return
        self._reader_auto_applying = True
        try:
            self._reader_library_toggle_btn.setChecked(wide_enough)
            self._set_reader_library_visible(wide_enough)
        finally:
            self._reader_auto_applying = False

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        if self._main_tabs.currentIndex() == 0 and not self.isFullScreen():
            self._apply_narrow_reader_layout()
            self._apply_reader_pane_ratios()

    def _apply_styles(self) -> None:
        self.setStyleSheet(STYLESHEET)

    def _apply_accessible_names(self) -> None:
        """图标/emoji 按钮补可读名称：屏幕阅读器不能只读出“垃圾桶”。

        以 tooltip 作为首选描述；没有 tooltip 时至少给“操作”，避免空名称。
        """
        for btn in self.findChildren(QPushButton):
            if btn.accessibleName():
                continue
            text = (btn.text() or "").strip()
            tip = (btn.toolTip() or "").strip()
            if not text:
                btn.setAccessibleName(tip or "操作")
            elif len(text) <= 2 and not any(ch.isalnum() for ch in text):
                btn.setAccessibleName(tip or text)
            if tip:
                btn.setAccessibleDescription(tip)

    def _start_docling_warmup(self) -> None:
        """界面显示后再后台预热，避免拖慢窗口创建过程。

        用 daemon 普通线程而不是 QThread：模块导入无法安全中断，QThread
        在关窗时仍在运行会触发 Qt abort（“Destroyed while thread is still
        running”）。daemon 线程随解释器退出，不参与 Qt 对象生命周期。
        """
        if self._closing:
            return
        def _warm() -> None:
            try:
                from .core.docling_parser import warm_up_import
                warm_up_import()
            except Exception:  # noqa: BLE001 — 预热失败不影响正常使用
                pass
        threading.Thread(target=_warm, name="docling-warmup", daemon=True).start()

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
        if self._closing or self._preparser is None:
            return
        if os.environ.get("PAPERWB_DISABLE_PREPARSE"):
            return  # 自测/冒烟脚本用环境变量禁用真实解析
        if not load_config().get("preparse_enabled", False):
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
        config["preparse_choice_made"] = True  # 用户显式选择，此后不再被新默认值覆盖
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
        dialog.check_update_requested.connect(self._check_update_manual)
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
        from .utils.version import current_version

        QMessageBox.about(
            self, "关于 PaperWB",
            "<h3>PaperWB</h3>"
            f"<p>AI 论文解读助手 v{current_version()}</p>"
            "<p>支持 DeepSeek、Mimo、OpenCode 及所有 OpenAI 兼容接口。</p>"
             "<p>两套接口：多模态（图表问答）+ 纯文本（论文问答/翻译/写作/文献检索）</p>"
             "<p>本地版式解析 · 结构化阅读视图 · 知识库驱动写作辅助 · Zotero 引文核查 · PubMed 文献检索</p>"
             "<p>论文问答双页签：本篇论文问答 + 全文献库跨文献问答（BM25 全库索引）；"
             "检索工作台：自然语言 AI 检索（OpenAlex / PubMed / arXiv 三源两轮）+ 按库推荐 + 定向文献巡视（自动过滤库内已有）</p>"
        )

    def closeEvent(self, event) -> None:
        self._closing = True
        if not self._writing_panel.shutdown():
            event.ignore()
            return
        self._save_current_chat()
        self._stats_tracker.stop_reading()
        self._stats_panel.shutdown()
        self._workbench_panel.shutdown()
        self.library_qa_panel.shutdown()
        if self._update_manager is not None:
            self._update_manager.stop()
        if self._update_dialog is not None:
            self._update_dialog.shutdown()
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
        if self._zotero_load_worker is not None and self._zotero_load_worker.isRunning():
            self._zotero_load_worker.wait(5_000)
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
        if self._closing:
            return
        # 优先使用信号传来的路径，其次重新从磁盘读 config
        if zotero_path:
            zotero_dir = zotero_path
        else:
            self._config = load_config()
            zotero_dir = self._config.get("zotero_data_dir", "")

        # 停掉旧 watcher / 旧加载线程（重建 ZoteroLibrary）
        if self._zotero_watcher is not None:
            self._zotero_watcher.stop()
            self._zotero_watcher = None
        old_worker = self._zotero_load_worker
        if old_worker is not None and old_worker.isRunning():
            old_worker.requestInterruption()
            old_worker.wait(2_000)
        self._zotero_load_worker = None

        self._zotero = ZoteroLibrary(zotero_dir)
        self._writing_panel.set_text_client(self._llm_text)
        # 写作「文献补充」的检索结果统一推送到检索工作台推荐流
        if not self._feed_bridge_connected:
            self._writing_panel.feed_requested.connect(
                lambda papers, label: self._workbench_panel.add_to_feed(papers, label))
            self._feed_bridge_connected = True

        # 后台加载：复制 sqlite + 逐条查询可能耗时数秒，不能在构造期阻塞。
        worker = ZoteroLoadWorker(self._zotero)
        track(worker)
        self._zotero_load_worker = worker
        worker.loaded.connect(
            lambda count, path=zotero_dir: self._on_zotero_loaded(path, count))
        worker.failed.connect(self._on_zotero_load_failed)
        worker.start()

    def _on_zotero_loaded(self, zotero_dir: str, count: int) -> None:
        """Zotero 加载完成：接线各面板 + 启动周期同步。"""
        if self._closing or self._zotero is None:
            return
        if count == 0 and zotero_dir:
            QMessageBox.warning(
                self, "Zotero 加载失败",
                f"未能从指定目录加载到文献：\n{zotero_dir}\n\n"
                "请检查该目录是否包含 zotero.sqlite 和 storage/ 子目录。")
        self._writing_panel.set_zotero_library(self._zotero)
        self._zotero_watcher = ZoteroWatcher(self._zotero, parent=self)
        self._zotero_watcher.changed.connect(self._on_zotero_changed)
        if self._zotero.is_available:
            self._zotero_watcher.start()
        self.pdf_list.zotero_panel.set_library(self._zotero)
        self.pdf_list.zotero_panel.set_watcher(self._zotero_watcher)
        self.library_qa_panel.set_zotero_library(self._zotero)
        self._workbench_panel.set_zotero_library(self._zotero)

    def _on_zotero_load_failed(self, message: str) -> None:
        print(f"[Zotero] 后台加载失败: {message}")
        if self._zotero is not None:
            self._writing_panel.set_zotero_library(self._zotero)
            self.pdf_list.zotero_panel.set_library(self._zotero)
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
