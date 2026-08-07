"""
PDF 文件列表面板 —— 管理导入的 PDF、文件夹分类
"""

import os
import shutil
from datetime import datetime
from pathlib import Path

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QLabel, QFileDialog, QInputDialog, QMenu, QMessageBox,
    QFrame, QTabWidget,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from ..utils.config import (
    load_config, save_config, load_library, save_library,
    add_pdf_to_library, remove_pdf_from_library, get_library_folders,
    delete_chat_history, delete_doc_state, delete_page_cache,
    get_library_dir,
)
from .zotero_panel import ZoteroPanel


class PDFListPanel(QWidget):
    """左侧面板 —— Tab1 Zotero 只读文献库 + Tab2 其它文献"""

    pdf_selected = Signal(str)
    pdf_removed = Signal(str)
    pdf_reload_requested = Signal(str)  # 清除全部缓存后重新加载
    pdf_imported = Signal(str)          # PDF 导入后立即触发分析
    restage1_requested = Signal(str)    # 仅重跑逐页解析
    restage2_requested = Signal(str)    # 仅重跑跨页整合
    zotero_pdf_selected = Signal(str)   # Zotero 文献 PDF 选中

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("libraryPanel")
        self.setMinimumWidth(250)
        self.setMaximumWidth(420)
        self.setAcceptDrops(True)
        self._library: list[dict] = []
        self._setup_ui()
        self._refresh()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._tabs = QTabWidget()
        self._tabs.setDocumentMode(True)
        self._tabs.tabBar().setExpanding(True)
        self._tabs.tabBar().setUsesScrollButtons(False)
        self._tabs.tabBar().setElideMode(Qt.TextElideMode.ElideNone)

        # ---- Tab 2: 其它文献 ----
        library_view = QWidget()
        lib_layout = QVBoxLayout(library_view)
        lib_layout.setContentsMargins(0, 0, 0, 0)
        lib_layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(16, 14, 14, 10)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("其它文献")
        title.setObjectName("titleLabel")
        title_box.addWidget(title)
        subtitle = QLabel("集中管理本地论文与阅读缓存")
        subtitle.setObjectName("subtitleLabel")
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self.import_btn = QPushButton("＋ 导入 PDF")
        self.import_btn.setObjectName("primaryBtn")
        self.import_btn.setToolTip("导入 PDF 论文到论文库")
        self.import_btn.clicked.connect(self._import_pdf)
        header.addWidget(self.import_btn)
        lib_layout.addLayout(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px;")
        lib_layout.addWidget(sep)

        hint = QLabel("支持拖拽 PDF 到这里，导入后会自动开始解析")
        hint.setObjectName("subtitleLabel")
        hint.setContentsMargins(16, 8, 16, 4)
        lib_layout.addWidget(hint)

        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setIndentation(16)
        self.tree.setAnimated(True)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._on_context_menu)
        self.tree.itemClicked.connect(self._on_item_clicked)
        lib_layout.addWidget(self.tree, 1)

        footer = QLabel()
        footer.setObjectName("subtitleLabel")
        footer.setContentsMargins(16, 6, 16, 12)
        self._footer_label = footer
        lib_layout.addWidget(footer)

        # ---- Tab 1: Zotero ----
        self.zotero_panel = ZoteroPanel(parent=self)
        self.zotero_panel.pdf_selected.connect(self.zotero_pdf_selected.emit)

        self._tabs.addTab(self.zotero_panel, "Zotero 文献库")
        self._tabs.addTab(library_view, "其它文献")
        layout.addWidget(self._tabs, 1)

    def _refresh(self):
        """刷新列表 —— 排序：文件夹在前 + 按名称排序"""
        self._library = load_library()

        # 按导入时间排序（新的在前）
        self._library.sort(key=lambda x: x.get("imported_at", ""), reverse=True)

        self.tree.clear()
        folders = sorted(get_library_folders(self._library))

        # 文件夹节点（先添加文件夹，显示为粗体）
        folder_items: dict[str, QTreeWidgetItem] = {}
        for folder in folders:
            item = QTreeWidgetItem([folder])
            item.setData(0, Qt.ItemDataRole.UserRole, {"type": "folder", "name": folder})
            font = item.font(0)
            font.setBold(True)
            font.setPointSize(11)
            item.setFont(0, font)
            item.setForeground(0, Qt.GlobalColor.white)  # 会被 stylesheet 覆盖，但作为兜底
            self.tree.addTopLevelItem(item)
            folder_items[folder] = item

        # 未分类的 PDF
        for pdf in self._library:
            folder = pdf.get("folder", "")
            fname = pdf.get("name", os.path.basename(pdf.get("path", "")))
            file_item = QTreeWidgetItem([fname])
            file_item.setData(0, Qt.ItemDataRole.UserRole, {
                "type": "pdf",
                "path": pdf.get("path"),
                "name": fname,
            })
            file_item.setToolTip(0, pdf.get("path", ""))
            # 根据文件是否存在设置颜色
            if os.path.exists(pdf.get("path", "")):
                file_item.setForeground(0, Qt.GlobalColor.white)
            else:
                file_item.setForeground(0, Qt.GlobalColor.darkGray)
                file_item.setText(0, fname + " (缺失)")

            if folder and folder in folder_items:
                folder_items[folder].addChild(file_item)
            else:
                self.tree.addTopLevelItem(file_item)

        self.tree.expandAll()
        total = len(self._library)
        self._footer_label.setText(f"共 {total} 篇论文")

    def _import_pdf(self):
        lib_dir = str(get_library_dir())
        files, _ = QFileDialog.getOpenFileNames(
            self, "导入 PDF 论文", lib_dir, "PDF 文件 (*.pdf);;所有文件 (*.*)"
        )
        if files:
            self._import_files(files)

    def _import_files(self, files: list[str]):
        lib_dir = str(get_library_dir())
        imported_paths: list[str] = []
        for file_path in files:
            fname = os.path.basename(file_path)
            dest = os.path.join(lib_dir, fname)
            counter = 1
            name, ext = os.path.splitext(fname)
            while os.path.exists(dest):
                dest = os.path.join(lib_dir, f"{name}_{counter}{ext}")
                counter += 1
            try:
                shutil.copy2(file_path, dest)
                add_pdf_to_library({
                    "name": os.path.basename(dest),
                    "path": dest,
                    "folder": "",
                    "imported_at": datetime.now().isoformat(),
                })
                imported_paths.append(dest)
            except OSError as e:
                QMessageBox.warning(self, "导入失败", str(e))
        self._refresh()
        # 导入后自动触发分析（后台进行）
        for path in imported_paths:
            self.pdf_imported.emit(path)

    def _new_folder(self) -> None:
        """新建文件夹分类，并把选中的论文移入。"""
        name, ok = QInputDialog.getText(self, "新建文件夹", "文件夹名称：")
        if ok and name.strip():
            self._move_pdfs_into_folder(name.strip())

    def _move_pdfs_into_folder(self, folder: str) -> None:
        """弹出多选对话框，将勾选的论文移入指定文件夹。"""
        from PySide6.QtWidgets import (
            QDialog, QListWidget, QListWidgetItem, QDialogButtonBox,
            QVBoxLayout, QLabel,
        )
        dialog = QDialog(self)
        dialog.setWindowTitle(f"移入文件夹「{folder}」")
        dialog.setMinimumWidth(420)
        lay = QVBoxLayout(dialog)
        lay.addWidget(QLabel("勾选要移入该文件夹的论文（可多选；直接确定则只创建空文件夹）："))
        lst = QListWidget()
        for pdf in self._library:
            fname = pdf.get("name", os.path.basename(pdf.get("path", "")))
            item = QListWidgetItem(fname)
            item.setData(Qt.ItemDataRole.UserRole, pdf.get("path"))
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Unchecked)
            lst.addItem(item)
        lay.addWidget(lst)
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        btns.accepted.connect(dialog.accept)
        btns.rejected.connect(dialog.reject)
        lay.addWidget(btns)

        if dialog.exec():
            chosen = {
                lst.item(i).data(Qt.ItemDataRole.UserRole)
                for i in range(lst.count())
                if lst.item(i).checkState() == Qt.CheckState.Checked
            }
            lib = load_library()
            for entry in lib:
                if entry.get("path") in chosen:
                    entry["folder"] = folder
            save_library(lib)
            self._refresh()

    def _on_item_clicked(self, item: QTreeWidgetItem, col: int):
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if data and data.get("type") == "pdf":
            path = data.get("path", "")
            if path and os.path.exists(path):
                self.pdf_selected.emit(path)
            else:
                QMessageBox.warning(self, "文件缺失", f"找不到文件：\n{path}")

    def _on_context_menu(self, pos):
        item = self.tree.itemAt(pos)
        menu = QMenu(self)
        if not item:
            # 空白处右键：新建文件夹 / 导入
            a = menu.addAction("  + 新建文件夹")
            a.triggered.connect(self._new_folder)
            a = menu.addAction("  + 导入 PDF")
            a.triggered.connect(self._import_pdf)
            menu.exec(self.tree.viewport().mapToGlobal(pos))
            return

        data = item.data(0, Qt.ItemDataRole.UserRole)

        if data and data.get("type") == "pdf":
            path = data.get("path", "")
            move_menu = menu.addMenu("  移动到")
            folders = sorted(get_library_folders(self._library))
            a = move_menu.addAction("(未分类)")
            a.triggered.connect(lambda: self._move_pdf(path, ""))
            if folders:
                move_menu.addSeparator()
            for f in folders:
                a = move_menu.addAction(f)
                a.triggered.connect(lambda checked, folder=f: self._move_pdf(path, folder))
            menu.addSeparator()
            a = menu.addAction("  🔁 重新逐页解析")
            a.triggered.connect(lambda: self._on_restage1(path))
            a = menu.addAction("  🔄 重新跨页整合")
            a.triggered.connect(lambda: self._on_restage2(path))
            a = menu.addAction("  🗑️ 从库中移除")
            a.triggered.connect(lambda: self._remove_pdf(path))
            menu.addSeparator()
            a = menu.addAction("  📂 重新加载 PDF")
            a.triggered.connect(lambda: self._reload_pdf(path))
        elif data and data.get("type") == "folder":
            name = data.get("name", "")
            menu.addSeparator()
            a = menu.addAction("  重命名")
            a.triggered.connect(lambda: self._rename_folder(name))
            a = menu.addAction("  删除（文件保留）")
            a.triggered.connect(lambda: self._delete_folder(name))

        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def _move_pdf(self, path: str, folder: str):
        lib = load_library()
        for item in lib:
            if item.get("path") == path:
                item["folder"] = folder
                break
        save_library(lib)
        self._refresh()

    def _on_restage1(self, path: str):
        """仅重跑逐页解析。"""
        r = QMessageBox.question(self, "确认",
            "重新逐页解析？\n\n"
            "此操作将清除页面缓存，保留跨页整合结果。\n"
            "将重新运行本地版式解析。")
        if r == QMessageBox.StandardButton.Yes:
            delete_page_cache(path)
            self.restage1_requested.emit(path)

    def _on_restage2(self, path: str):
        """仅重跑跨页整合。"""
        r = QMessageBox.question(self, "确认",
            "重新跨页整合？\n\n"
            "此操作仅清除整合结果，保留逐页解析缓存。\n"
            "仅调用纯文本 LLM（便宜）。")
        if r == QMessageBox.StandardButton.Yes:
            delete_doc_state(path)
            self.restage2_requested.emit(path)

    def _remove_pdf(self, path: str):
        r = QMessageBox.question(self, "确认",
            "从论文库中移除此 PDF 并删除所有关联数据？\n\n"
            "此操作将：\n"
            "• 删除磁盘上的 PDF 文件\n"
            "• 清除逐页解析缓存\n"
            "• 清除跨页整合结果\n"
            "• 清除对话历史\n"
            "• 清除翻译/排版状态")
        if r == QMessageBox.StandardButton.Yes:
            delete_chat_history(path)
            delete_doc_state(path)
            delete_page_cache(path)
            remove_pdf_from_library(path)
            try:
                os.remove(path)
            except OSError as e:
                print(f"[PDFList] 删除文件失败: {e}")
            self.pdf_removed.emit(path)
            self._refresh()

    def _reload_pdf(self, path: str):
        """清除所有缓存并重新加载 PDF。"""
        r = QMessageBox.question(self, "确认",
            f"重新加载此 PDF？\n\n这将清除逐页解析缓存、跨页整合结果和对话历史，\n"
            f"然后重新解析 PDF。下次需要重新运行 LLM。")
        if r == QMessageBox.StandardButton.Yes:
            delete_chat_history(path)
            delete_doc_state(path)
            delete_page_cache(path)
            self.pdf_reload_requested.emit(path)

    def _rename_folder(self, old: str):
        new, ok = QInputDialog.getText(self, "重命名", "新名称：", text=old)
        if ok and new.strip() and new != old:
            lib = load_library()
            for item in lib:
                if item.get("folder") == old:
                    item["folder"] = new.strip()
            save_library(lib)
            self._refresh()

    def _delete_folder(self, name: str):
        r = QMessageBox.question(self, "确认", f"删除文件夹「{name}」？\n文件将变为未分类。")
        if r == QMessageBox.StandardButton.Yes:
            lib = load_library()
            for item in lib:
                if item.get("folder") == name:
                    item["folder"] = ""
            save_library(lib)
            self._refresh()

    # ========== 进度更新 ==========

    def update_pdf_progress(self, pdf_path: str, current: int, total: int):
        """更新指定 PDF 在列表中的解析进度显示。

        Args:
            pdf_path: PDF 文件的绝对路径
            current: 当前已完成页数
            total: 总页数
        """
        def _apply(item: "QTreeWidgetItem"):
            data = item.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("path") != pdf_path:
                return False
            base_name = data.get("name") or item.text(0)
            base_name = base_name.lstrip("✅🔄 ").strip()
            if current >= total:
                item.setText(0, f"✅ {base_name}")
                item.setToolTip(0, f"{pdf_path}\n解析完成: {current}/{total} 页")
            else:
                item.setText(0, f"🔄 {base_name} ({current}/{total})")
                item.setToolTip(0, f"{pdf_path}\n解析中: {current}/{total} 页")
            return True

        for i in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(i)
            if _apply(item):
                return
            for j in range(item.childCount()):
                if _apply(item.child(j)):
                    return

    # ========== 拖拽导入 ==========

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            for url in event.mimeData().urls():
                if url.toLocalFile().lower().endswith(".pdf"):
                    event.acceptProposedAction()
                    return
        event.ignore()

    def dropEvent(self, event: QDropEvent):
        paths = []
        for url in event.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith(".pdf"):
                paths.append(p)
        if paths:
            self._import_files(paths)
