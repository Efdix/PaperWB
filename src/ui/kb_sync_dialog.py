"""知识库导出/导入对话框 —— 把写作知识库打包搬去另一台电脑，免于重建。

双页签：导出（勾选知识库 → 存 zip）/ 导入（选包 → 预览 → 冲突策略 → 还原）。
非模态（show() + 信号），导入成功后发 ``kb_imported`` 让写作面板刷新下拉。
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
    QProgressBar, QPushButton, QScrollArea, QTabWidget, QVBoxLayout, QWidget,
)

from ..core.kb_sync import (
    CONFLICT_LABELS, CONFLICT_OVERWRITE, CONFLICT_SKIP, KbExportWorker,
    KbImportWorker, collect_sources, human_size, list_packages,
)
from ..utils.config import get_sync_dir, set_sync_dir
from ..utils.threads import track


def _primary_btn(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("primaryBtn")
    return b


def _soft_btn(text: str) -> QPushButton:
    b = QPushButton(text)
    b.setObjectName("secondaryBtn")
    return b


class KbSyncDialog(QDialog):
    """写作知识库导出/导入（跨机同步）。"""

    kb_imported = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("知识库导出 / 导入")
        self.resize(620, 620)
        self.setMinimumSize(520, 520)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.Window
        )
        self._export_worker: KbExportWorker | None = None
        self._import_worker: KbImportWorker | None = None
        self._src_rows: list[dict] = []
        self._pkg_rows: list[dict] = []
        self._setup_ui()

    # ---- UI ----

    def _setup_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        tabs.addTab(self._build_export_tab(), "📦 导出")
        tabs.addTab(self._build_import_tab(), "📥 导入")
        layout.addWidget(tabs, 1)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setContentsMargins(16, 6, 16, 6)
        self._status.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self._status)

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setContentsMargins(16, 0, 16, 0)
        layout.addWidget(self._progress)

        btn_row = QWidget()
        btn_lo = QHBoxLayout(btn_row)
        btn_lo.setContentsMargins(16, 8, 16, 14)
        btn_lo.addStretch()
        close_btn = _soft_btn("关闭")
        close_btn.clicked.connect(self.accept)
        btn_lo.addWidget(close_btn)
        layout.addWidget(btn_row)

    def _build_export_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(16, 14, 16, 10)
        v.setSpacing(8)

        tip = QLabel(
            "知识库自包含（范文全文与分析结果都在库内），导出后在另一台电脑"
            "导入即可直接使用，无需重新做风格分析（省 LLM 费用）。")
        tip.setWordWrap(True)
        tip.setObjectName("subtitleLabel")
        v.addWidget(tip)

        head = QHBoxLayout()
        self._exp_all_cb = QCheckBox("全选")
        self._exp_all_cb.setChecked(True)
        self._exp_all_cb.toggled.connect(self._on_export_all_toggled)
        head.addWidget(self._exp_all_cb)
        refresh = _soft_btn("刷新列表")
        refresh.clicked.connect(self._reload_sources)
        head.addWidget(refresh)
        head.addStretch()
        self._exp_summary = QLabel("")
        self._exp_summary.setObjectName("subtitleLabel")
        head.addWidget(self._exp_summary)
        v.addLayout(head)

        self._exp_scroll = QScrollArea()
        self._exp_scroll.setWidgetResizable(True)
        v.addWidget(self._exp_scroll, 1)

        opts = QFrame()
        olo = QVBoxLayout(opts)
        olo.setContentsMargins(0, 0, 0, 0)
        olo.setSpacing(2)
        opt_tip = QLabel("附加数据（按库名关联，可选）：")
        opt_tip.setObjectName("subtitleLabel")
        olo.addWidget(opt_tip)
        self._exp_drafts_cb = QCheckBox("包含编辑器草稿（drafts）")
        self._exp_history_cb = QCheckBox("包含润色历史（polish_history）")
        self._exp_reviews_cb = QCheckBox("包含已保存的草稿评价（reviews）")
        for cb in (self._exp_drafts_cb, self._exp_history_cb, self._exp_reviews_cb):
            olo.addWidget(cb)
        v.addWidget(opts)

        out_row = QHBoxLayout()
        out_row.addWidget(QLabel("保存为："))
        self._exp_path = QLabel("（未选择）")
        self._exp_path.setObjectName("subtitleLabel")
        self._exp_path.setWordWrap(True)
        out_row.addWidget(self._exp_path, 1)
        pick = _soft_btn("选择位置...")
        pick.clicked.connect(self._pick_export_path)
        out_row.addWidget(pick)
        v.addLayout(out_row)

        self._exp_btn = _primary_btn("📦 导出所选知识库")
        self._exp_btn.clicked.connect(self._on_export)
        v.addWidget(self._exp_btn)

        self._reload_sources()
        return page

    def _build_import_tab(self) -> QWidget:
        page = QWidget()
        v = QVBoxLayout(page)
        v.setContentsMargins(16, 14, 16, 10)
        v.setSpacing(8)

        tip = QLabel(
            "选择另一台电脑导出的 .zip 包，勾选要还原的知识库。"
            "同名库默认不勾选，需你主动选择处理方式。")
        tip.setWordWrap(True)
        tip.setObjectName("subtitleLabel")
        v.addWidget(tip)

        pick_row = QHBoxLayout()
        pick_row.addWidget(QLabel("知识库包："))
        self._imp_path = QLabel("（未选择）")
        self._imp_path.setObjectName("subtitleLabel")
        self._imp_path.setWordWrap(True)
        pick_row.addWidget(self._imp_path, 1)
        browse = _soft_btn("浏览...")
        browse.clicked.connect(self._pick_import_path)
        pick_row.addWidget(browse)
        v.addLayout(pick_row)

        ctrl = QHBoxLayout()
        self._imp_all_cb = QCheckBox("全选")
        self._imp_all_cb.setChecked(True)
        self._imp_all_cb.toggled.connect(self._on_import_all_toggled)
        ctrl.addWidget(self._imp_all_cb)
        ctrl.addStretch()
        ctrl.addWidget(QLabel("同名知识库："))
        self._conflict_combo = QComboBox()
        for key, label in CONFLICT_LABELS.items():
            self._conflict_combo.addItem(label, key)
        self._conflict_combo.setCurrentIndex(1)  # 默认覆盖（会先自动备份）
        self._conflict_combo.setToolTip(
            "覆盖前会把本地同名库及其草稿/润色历史/评价备份到\n"
            "writing_kb/.backup/<时间戳>/，可随时回退")
        ctrl.addWidget(self._conflict_combo)
        v.addLayout(ctrl)

        self._imp_scroll = QScrollArea()
        self._imp_scroll.setWidgetResizable(True)
        v.addWidget(self._imp_scroll, 1)

        self._imp_btn = _primary_btn("📥 导入所选知识库")
        self._imp_btn.setEnabled(False)
        self._imp_btn.clicked.connect(self._on_import)
        v.addWidget(self._imp_btn)
        return page

    # ---- 导出页列表 ----

    def _reload_sources(self) -> None:
        sources = collect_sources()
        self._src_rows = []
        container = QWidget()
        lo = QVBoxLayout(container)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(4)

        if not sources:
            empty = QLabel("还没有知识库。在「知识库」工具里新建并添加范文后再来导出。")
            empty.setWordWrap(True)
            empty.setObjectName("subtitleLabel")
            lo.addWidget(empty)
        for src in sources:
            card = QFrame()
            card.setStyleSheet(
                "QFrame { background: #fffdfa; border: 1px solid #e5e1d9; "
                "border-radius: 8px; }"
            )
            clo = QHBoxLayout(card)
            clo.setContentsMargins(10, 6, 10, 6)
            cb = QCheckBox(src.name)
            cb.setChecked(True)
            cb.toggled.connect(self._update_export_summary)
            clo.addWidget(cb)
            info = QLabel(f"{src.summary} · {human_size(src.size)}")
            info.setObjectName("subtitleLabel")
            info.setWordWrap(True)
            clo.addWidget(info, 1)
            lo.addWidget(card)
            self._src_rows.append({"check": cb, "source": src})

        lo.addStretch()
        self._exp_scroll.setWidget(container)
        self._update_export_summary()

    def _on_export_all_toggled(self, checked: bool) -> None:
        for row in self._src_rows:
            row["check"].setChecked(checked)

    def _selected_sources(self) -> list:
        out = []
        for row in self._src_rows:
            if not row["check"].isChecked():
                continue
            src = row["source"]
            src.include_drafts = self._exp_drafts_cb.isChecked()
            src.include_history = self._exp_history_cb.isChecked()
            src.include_reviews = self._exp_reviews_cb.isChecked()
            out.append(src)
        return out

    def _update_export_summary(self) -> None:
        chosen = [r for r in self._src_rows if r["check"].isChecked()]
        total = sum(r["source"].size for r in chosen)
        self._exp_summary.setText(
            f"已选 {len(chosen)}/{len(self._src_rows)} 个 · 约 {human_size(total)}")

    def _pick_export_path(self) -> None:
        start = get_sync_dir()
        default = str(Path(start) / "PaperWB-知识库.zip") if start else "PaperWB-知识库.zip"
        path, _ = QFileDialog.getSaveFileName(
            self, "导出知识库", default, "知识库包 (*.zip)")
        if not path:
            return
        if not path.lower().endswith(".zip"):
            path += ".zip"
        self._exp_path.setText(path)
        set_sync_dir(str(Path(path).parent))

    # ---- 导出执行 ----

    def _on_export(self) -> None:
        if self._export_worker is not None and self._export_worker.isRunning():
            return
        sources = self._selected_sources()
        if not sources:
            self._status.setText("请至少勾选一个知识库")
            return
        dest = self._exp_path.text()
        if dest in ("", "（未选择）"):
            self._status.setText("请先选择保存位置")
            return

        self._set_busy(True)
        self._status.setText("正在打包...")
        worker = KbExportWorker(sources, dest, parent=self)
        track(worker)
        self._export_worker = worker
        worker.progress.connect(self._on_progress)
        worker.error.connect(self._on_export_error)
        worker.done.connect(self._on_export_done)
        worker.start()

    def _on_export_done(self, result: dict) -> None:
        if self.sender() is not self._export_worker:
            return
        self._export_worker = None
        self._set_busy(False)
        path = result.get("path", "")
        self._status.setText(
            f"✅ 已导出 {result.get('profiles', 0)} 个知识库、"
            f"{result.get('files', 0)} 个文件（{human_size(result.get('bytes', 0))}）\n{path}\n"
            "把该文件拷到另一台电脑（或放进网盘目录），在「导入」页还原。")

    def _on_export_error(self, err: str) -> None:
        if self.sender() is not self._export_worker:
            return
        self._export_worker = None
        self._set_busy(False)
        self._status.setText(f"导出失败：{err}")

    # ---- 导入页 ----

    def _pick_import_path(self) -> None:
        start = get_sync_dir()
        path, _ = QFileDialog.getOpenFileName(
            self, "选择知识库包", start or "", "知识库包 (*.zip)")
        if not path:
            return
        self._imp_path.setText(path)
        set_sync_dir(str(Path(path).parent))
        self._reload_packages(path)

    def _reload_packages(self, zip_path: str) -> None:
        self._pkg_rows = []
        try:
            packages = list_packages(zip_path)
        except ValueError as e:
            self._imp_btn.setEnabled(False)
            self._status.setText(f"无法读取该包：{e}")
            self._imp_scroll.setWidget(QWidget())
            return

        container = QWidget()
        lo = QVBoxLayout(container)
        lo.setContentsMargins(0, 0, 0, 0)
        lo.setSpacing(4)
        if not packages:
            empty = QLabel("包内没有知识库。")
            empty.setObjectName("subtitleLabel")
            lo.addWidget(empty)
        for pkg in packages:
            card = QFrame()
            card.setStyleSheet(
                "QFrame { background: #fffdfa; border: 1px solid #e5e1d9; "
                "border-radius: 8px; }"
            )
            clo = QHBoxLayout(card)
            clo.setContentsMargins(10, 6, 10, 6)
            cb = QCheckBox(pkg.name)
            # 同名库默认不勾选：需用户主动决定是否覆盖/另存
            cb.setChecked(not pkg.exists_locally)
            clo.addWidget(cb)
            info = QLabel(f"{pkg.summary} · {human_size(pkg.size)}")
            info.setObjectName("subtitleLabel")
            info.setWordWrap(True)
            clo.addWidget(info, 1)
            lo.addWidget(card)
            self._pkg_rows.append({"check": cb, "package": pkg})

        lo.addStretch()
        self._imp_scroll.setWidget(container)
        conflict_n = sum(1 for p in packages if p.exists_locally)
        head = f"包内 {len(packages)} 个知识库"
        if conflict_n:
            head += f"，其中 {conflict_n} 个与本地同名（默认未勾选）"
        else:
            head += "，无同名冲突"
        self._status.setText(f"{head}（导出于 {packages[0].exported_at or '未知时间'}）")
        self._imp_btn.setEnabled(bool(packages))

    def _on_import_all_toggled(self, checked: bool) -> None:
        for row in self._pkg_rows:
            row["check"].setChecked(checked)

    def _on_import(self) -> None:
        if self._import_worker is not None and self._import_worker.isRunning():
            return
        selected = [r["package"].name for r in self._pkg_rows
                    if r["check"].isChecked()]
        if not selected:
            self._status.setText("请至少勾选一个知识库")
            return
        policy = self._conflict_combo.currentData() or CONFLICT_SKIP
        zip_path = self._imp_path.text()
        self._set_busy(True)
        self._status.setText("正在导入...")
        worker = KbImportWorker(zip_path, selected, policy, parent=self)
        track(worker)
        self._import_worker = worker
        worker.progress.connect(self._on_progress)
        worker.error.connect(self._on_import_error)
        worker.done.connect(self._on_import_done)
        worker.start()

    def _on_import_done(self, report) -> None:
        if self.sender() is not self._import_worker:
            return
        self._import_worker = None
        self._set_busy(False)
        lines: list[str] = []
        if report.imported:
            lines.append("✅ 已导入：" + "、".join(report.imported))
        if report.overwritten:
            lines.append("♻ 已覆盖：" + "、".join(report.overwritten)
                         + f"（原库已备份）")
        if report.renamed:
            lines.append("📄 存为副本：" + "、".join(
                f"{o} → {n}" for o, n in report.renamed))
        if report.skipped:
            lines.append("⏭ 已跳过（本地已有）：" + "、".join(report.skipped))
        if report.failed:
            lines.append("⚠ 失败：" + "；".join(
                f"{n}（{r}）" for n, r in report.failed))
        if report.backup_dir:
            lines.append(f"备份目录：{report.backup_dir}")
        self._status.setText("\n".join(lines) or "没有可导入的内容")
        if report.changed:
            self.kb_imported.emit()
            # 刷新导入页的冲突标记
            self._reload_packages(self._imp_path.text())

    def _on_import_error(self, err: str) -> None:
        if self.sender() is not self._import_worker:
            return
        self._import_worker = None
        self._set_busy(False)
        self._status.setText(f"导入失败：{err}")

    # ---- 公共 ----

    def _on_progress(self, done: int, total: int, label: str) -> None:
        self._progress.setVisible(True)
        self._progress.setRange(0, max(total, 1))
        self._progress.setValue(done)
        self._status.setText(f"正在处理 {done}/{total} · {label}")

    def _set_busy(self, busy: bool) -> None:
        self._exp_btn.setEnabled(not busy)
        self._imp_btn.setEnabled(not busy)
        self._exp_all_cb.setEnabled(not busy)
        self._imp_all_cb.setEnabled(not busy)
        self._conflict_combo.setEnabled(not busy)
        self._progress.setVisible(busy)
        if busy:
            self._progress.setRange(0, 0)

    def closeEvent(self, event) -> None:
        for worker in (self._export_worker, self._import_worker):
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                worker.wait(3000)
        super().closeEvent(event)
