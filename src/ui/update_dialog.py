"""更新推送对话框 —— 展示新版本信息、下载安装包、确认后覆盖安装。

非模态（``show()``），三态：检查中 / 已有新版本 / 下载中。
安装前明确告知数据不受影响，并显示实际 ``data_root`` 路径（消除"升级会不会
弄丢我的文献和 API Key"的疑虑）。

下载完成后点「立即更新」→ 写启动器脚本 → 脱离启动 → ``QApplication.quit()``
→ 安装器静默覆盖安装 → 脚本把应用重新拉起。
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QProgressBar, QPushButton, QScrollArea,
    QTextBrowser, QVBoxLayout, QWidget,
)

from ..core.updater import (
    KIND_DEV,
    UpdateDownloadWorker,
    UpdateInfo,
    launch_installer_after_exit,
    open_url,
)
from ..utils.config import get_updates_dir, load_config, set_update_skipped_version
from ..utils.threads import track
from ..utils.version import current_version, format_size


class UpdateDialog(QDialog):
    """新版本信息 + 下载 + 安装确认。

    信号：
        skipped(str): 用户选择「跳过此版本」，携带版本号（主窗口据此停止提醒）。
        installed(): 用户确认安装、应用即将退出（主窗口可做收尾）。
    """

    skipped = Signal(str)
    installed = Signal()

    def __init__(self, info: UpdateInfo, parent=None, manual: bool = False):
        super().__init__(parent)
        self._info = info
        self._manual = manual
        self._worker: UpdateDownloadWorker | None = None
        self._setup_path = ""
        self._speed_text = ""
        self.setWindowTitle(f"发现新版本 v{info.version}" if info.version else "检查更新")
        self.resize(660, 620)
        self.setMinimumSize(520, 460)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.Window
        )
        self._setup_ui()

    # ---- UI ----

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 14)
        layout.setSpacing(10)

        info = self._info
        head = QLabel(f"PaperWB {info.version} 已发布")
        head.setObjectName("dialogHead")
        layout.addWidget(head)

        meta = [
            f"当前版本：v{current_version()}",
            f"最新版本：v{info.version}",
            f"安装包：{info.kind_text} · {info.size_text}",
        ]
        if info.source == "github":
            meta.append("来源：GitHub Releases")
        if info.published_at:
            meta.append(f"发布时间：{info.published_at[:10]}")
        meta_label = QLabel("　·　".join(meta))
        meta_label.setObjectName("subtitleLabel")
        meta_label.setWordWrap(True)
        layout.addWidget(meta_label)

        notes_title = QLabel("更新说明")
        notes_title.setObjectName("sectionLabel")
        layout.addWidget(notes_title)

        notes = QTextBrowser()
        notes.setOpenExternalLinks(True)
        notes.setText(info.notes or "（该版本未提供更新说明，详见发布页）")
        layout.addWidget(notes, 1)

        # 数据安全声明：升级前把"不会丢数据"讲清楚，并显示实际数据目录
        data_root = str(load_config().get("data_root", "") or "")
        safe = QLabel(
            "升级为覆盖安装，不会改动你的数据目录"
            + (f"（{data_root}）" if data_root else "")
            + "：文献库、Zotero 路径、API Key、写作知识库均保留。"
            "安装期间应用会退出，完成后自动重新打开。"
        )
        safe.setObjectName("subtitleLabel")
        safe.setWordWrap(True)
        layout.addWidget(safe)

        # 下载进度区（默认隐藏）
        self._progress_box = QWidget()
        pbox = QVBoxLayout(self._progress_box)
        pbox.setContentsMargins(0, 4, 0, 0)
        pbox.setSpacing(4)
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setTextVisible(True)
        pbox.addWidget(self.progress)
        self._progress_label = QLabel("")
        self._progress_label.setObjectName("subtitleLabel")
        pbox.addWidget(self._progress_label)
        self._progress_box.setVisible(False)
        layout.addWidget(self._progress_box)

        self.status = QLabel("")
        self.status.setObjectName("subtitleLabel")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        row = QHBoxLayout()
        row.setSpacing(8)
        self.page_btn = QPushButton("打开发布页")
        self.page_btn.setObjectName("secondaryBtn")
        self.page_btn.setToolTip("在浏览器中打开发布页（可手动下载安装包）")
        self.page_btn.clicked.connect(self._open_page)
        row.addWidget(self.page_btn)

        self.skip_btn = QPushButton("跳过此版本")
        self.skip_btn.setObjectName("secondaryBtn")
        self.skip_btn.setToolTip("不再提示这个版本（下个版本仍会提示）")
        self.skip_btn.clicked.connect(self._skip)
        row.addWidget(self.skip_btn)

        row.addStretch()

        self.later_btn = QPushButton("以后再说")
        self.later_btn.setObjectName("secondaryBtn")
        self.later_btn.clicked.connect(self.close)
        row.addWidget(self.later_btn)

        self.action_btn = QPushButton("下载并更新")
        self.action_btn.setObjectName("primaryBtn")
        self.action_btn.clicked.connect(self._on_action)
        row.addWidget(self.action_btn)
        layout.addLayout(row)

        # 开发模式（源码运行）不能覆盖安装，降级为"打开发布页"
        if not info.has_asset:
            self.action_btn.setEnabled(False)
            self.status.setText("该更新未提供安装包下载地址，请点「打开发布页」手动下载。")
        elif self._install_unsupported():
            self.action_btn.setText("打开发布页下载")
            self.action_btn.clicked.disconnect()
            self.action_btn.clicked.connect(self._open_page)
            self.status.setText(
                "当前为源码运行模式，无法在应用内自动安装；"
                "请用发布页的安装包升级（或 git pull 更新源码）。")
        if self._manual:
            self.later_btn.setText("关闭")

    @staticmethod
    def _install_unsupported() -> bool:
        from ..core.updater import install_kind

        return install_kind() == KIND_DEV

    # ---- 交互 ----

    def _open_page(self) -> None:
        if not open_url(self._info.page_url):
            self.status.setText(f"打开发布页失败，请手动访问：{self._info.page_url}")

    def _skip(self) -> None:
        set_update_skipped_version(self._info.version)
        self.skipped.emit(self._info.version)
        self.close()

    def _on_action(self) -> None:
        if self._setup_path:
            self._install_now()
            return
        self._start_download()

    def _start_download(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        if not self._info.sha256:
            self._on_failed(
                "发布方未提供 sha256 校验值，出于安全考虑已拒绝自动安装。\n"
                "请点击「打开发布页」手动下载安装包。")
            return
        self._progress_box.setVisible(True)
        self.progress.setValue(0)
        self._progress_label.setText("正在连接下载源…")
        self.status.setText("")
        self.action_btn.setEnabled(False)
        self.skip_btn.setEnabled(False)
        self.page_btn.setEnabled(False)
        self.later_btn.setEnabled(False)
        self.action_btn.setText("下载中…")

        worker = UpdateDownloadWorker(self._info)
        self._worker = worker
        track(worker)
        worker.progress.connect(self._on_progress)
        worker.finished_ok.connect(self._on_downloaded)
        worker.failed.connect(self._on_failed)
        worker.start()

    def _on_progress(self, done: int, total: int) -> None:
        if total > 0:
            pct = int(done * 100 / total)
            self.progress.setRange(0, 100)
            self.progress.setValue(min(100, pct))
            self._progress_label.setText(
                f"已下载 {format_size(done)} / {format_size(total)}（{pct}%）")
        else:
            self.progress.setRange(0, 0)  # 未知总大小 → 忙碌条
            self._progress_label.setText(f"已下载 {format_size(done)}")

    def _on_downloaded(self, path: str) -> None:
        self._setup_path = path
        self.progress.setRange(0, 100)
        self.progress.setValue(100)
        verify = "（已通过 sha256 校验）" if self._info.sha256 else "（发布方未提供哈希，仅校验了体积）"
        self._progress_label.setText(f"下载完成{verify}：{os.path.basename(path)}")
        self.action_btn.setEnabled(True)
        self.action_btn.setText("立即安装并重启")
        self.action_btn.setToolTip("退出 PaperWB → 静默覆盖安装 → 自动重新打开")
        self.skip_btn.setEnabled(True)
        self.page_btn.setEnabled(True)
        self.later_btn.setEnabled(True)
        self.status.setText(
            "点「立即安装并重启」后：应用退出 → 安装程序覆盖升级（进度见安装窗口）"
            "→ 自动重新打开。数据目录不受影响。")

    def _on_failed(self, message: str) -> None:
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self._progress_box.setVisible(False)
        self.action_btn.setEnabled(True)
        self.action_btn.setText("重试下载")
        self.skip_btn.setEnabled(True)
        self.page_btn.setEnabled(True)
        self.later_btn.setEnabled(True)
        self.status.setText(
            f"{message}\n可点「重试下载」，或「打开发布页」手动下载安装包。")

    def _install_now(self) -> None:
        self.action_btn.setEnabled(False)
        try:
            launch_installer_after_exit(self._setup_path)
        except Exception as e:  # noqa: BLE001 — 启动失败要能回退到手动路径
            self.action_btn.setEnabled(True)
            self.status.setText(
                f"启动安装程序失败：{e}\n"
                f"安装包已下载到：{self._setup_path}\n请手动双击安装。")
            return
        self.status.setText("正在退出并安装…应用会在安装完成后自动重新打开。")
        self.installed.emit()

    # ---- 生命周期 ----

    def download_dir(self) -> str:
        """安装包所在目录（供状态提示）。"""
        return str(get_updates_dir())

    def shutdown(self) -> None:
        """取消在途下载（对话框关闭/应用退出时调用）。"""
        worker = self._worker
        if worker is not None and worker.isRunning():
            worker.requestInterruption()
            worker.wait(3000)

    def closeEvent(self, event) -> None:  # noqa: N802
        self.shutdown()
        super().closeEvent(event)
