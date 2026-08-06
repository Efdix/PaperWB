"""Zotero 文献库面板 —— 只读镜像 Zotero 集合树，实时同步增删改。"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QLabel, QPushButton, QFrame, QMenu, QMessageBox,
)
from PySide6.QtCore import Qt, Signal

if TYPE_CHECKING:
    from ..core.zotero_parser import ZoteroLibrary
    from ..core.zotero_watcher import ZoteroWatcher


class ZoteroPanel(QWidget):
    """Zotero 只读文献库树形视图。

    信号:
        pdf_selected(str): 用户点击了带 PDF 附件的条目，传出 PDF 绝对路径。
    """

    pdf_selected = Signal(str)

    def __init__(self, library: "ZoteroLibrary | None" = None,
                 watcher: "ZoteroWatcher | None" = None, parent=None):
        super().__init__(parent)
        self._library = library
        self._watcher = watcher
        self._setup_ui()
        self.refresh()

    # ---- 依赖注入 ----

    def set_library(self, library: "ZoteroLibrary | None"):
        self._library = library
        self.refresh()

    def set_watcher(self, watcher: "ZoteroWatcher | None"):
        if self._watcher is watcher:
            return
        if self._watcher is not None:
            self._watcher.changed.disconnect(self._on_watcher_changed)
            self._watcher.status.disconnect(self._on_watcher_status)
            self._watcher.error.disconnect(self._on_watcher_error)
        self._watcher = watcher
        if watcher is not None:
            watcher.changed.connect(self._on_watcher_changed)
            watcher.status.connect(self._on_watcher_status)
            watcher.error.connect(self._on_watcher_error)

    # ---- UI ----

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(12, 8, 12, 8)
        title = QLabel("📚 Zotero")
        title.setObjectName("titleLabel")
        header.addWidget(title)
        header.addStretch()
        self._refresh_btn = QPushButton("🔄 刷新")
        self._refresh_btn.setToolTip("手动刷新（通常会自动同步）")
        self._refresh_btn.clicked.connect(self._on_manual_refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #2a2c3d; max-height: 1px;")
        layout.addWidget(sep)

        self._status_label = QLabel("未连接 Zotero")
        self._status_label.setObjectName("subtitleLabel")
        self._status_label.setContentsMargins(12, 4, 12, 4)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(16)
        self._tree.setAnimated(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemClicked.connect(self._on_item_clicked)
        self._tree.setStyleSheet(
            "QTreeWidget { background-color: #1a1b26; border: none; outline: none; }"
            "QTreeWidget::item { padding: 5px 8px; color: #cfd2e3; border-radius: 4px; }"
            "QTreeWidget::item:hover { background-color: #2a2c3d; }"
            "QTreeWidget::item:selected { background-color: #3b3d54; }"
        )
        layout.addWidget(self._tree, 1)

    # ---- 数据渲染 ----

    def refresh(self):
        """重建树（保留集合展开状态）。"""
        expanded = self._collect_expanded()
        self._tree.clear()
        lib = self._library
        if lib is None or not lib.is_available or lib.item_count == 0:
            self._tree.addTopLevelItem(QTreeWidgetItem(["（未检测到 Zotero 文献库）"]))
            self._update_status()
            return

        unclassified = QTreeWidgetItem(["🗂️ （未分类）"])
        unclassified.setData(0, Qt.ItemDataRole.UserRole, {"kind": "unclassified"})
        classified_ids: set[int] = set()

        def build_collection(coll):
            """递归构建集合节点（含子集合与直接条目）。"""
            node = QTreeWidgetItem([f"📁 {coll.name}"])
            node.setData(0, Qt.ItemDataRole.UserRole, {"kind": "collection", "key": coll.key})
            node.setToolTip(0, coll.name)
            for cid in coll.child_ids:
                child = lib.get_collection(cid)
                if child is not None:
                    node.addChild(build_collection(child))
            for item in lib.get_items_in_collection(coll.collection_id):
                classified_ids.add(item.item_id)
                node.addChild(self._make_item_node(item))
            return node

        for coll in lib.get_collections_tree():
            self._tree.addTopLevelItem(build_collection(coll))

        # 未分类条目
        for item in lib.get_all_items():
            if item.item_id not in classified_ids:
                unclassified.addChild(self._make_item_node(item))
        if unclassified.childCount() > 0:
            self._tree.addTopLevelItem(unclassified)

        self._tree.expandAll()
        self._restore_expanded(expanded)
        self._update_status()

    def _make_item_node(self, item) -> QTreeWidgetItem:
        year = f" ({item.year})" if item.year else ""
        has_pdf = bool(item.pdf_path) and os.path.isfile(item.pdf_path)
        marker = "📄" if has_pdf else "⚪"
        text = f"{marker} {item.title or '[无标题]'}{year}"
        node = QTreeWidgetItem([text])
        node.setData(0, Qt.ItemDataRole.UserRole, {
            "kind": "item",
            "item_id": item.item_id,
            "pdf_path": item.pdf_path if has_pdf else "",
        })
        node.setToolTip(0, (item.title or "") + (f"\n{item.pdf_path}" if has_pdf else "\n（无 PDF 附件）"))
        return node

    def _collect_expanded(self) -> set[str]:
        keys: set[str] = set()

        def walk(item: QTreeWidgetItem):
            if item.isExpanded():
                data = item.data(0, Qt.ItemDataRole.UserRole) or {}
                if data.get("kind") == "collection" and data.get("key"):
                    keys.add(data["key"])
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))
        return keys

    def _restore_expanded(self, keys: set[str]):
        if not keys:
            return

        def walk(item: QTreeWidgetItem):
            data = item.data(0, Qt.ItemDataRole.UserRole) or {}
            if data.get("kind") == "collection" and data.get("key") in keys:
                item.setExpanded(True)
            for i in range(item.childCount()):
                walk(item.child(i))

        for i in range(self._tree.topLevelItemCount()):
            walk(self._tree.topLevelItem(i))

    def _update_status(self):
        lib = self._library
        if lib is None or not lib.is_available:
            self._status_label.setText("未连接 Zotero（可点击右侧刷新或稍后同步）")
        else:
            self._status_label.setText(
                f"✅ 已连接（{lib.item_count} 篇文献 · {lib.collection_count} 个集合）"
            )

    # ---- 交互 ----

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int):
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") == "item" and data.get("pdf_path"):
            self.pdf_selected.emit(data["pdf_path"])

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        menu = QMenu(self)
        menu.setStyleSheet(
            "QMenu { background: #24253a; color: #cfd2e3; border: 1px solid #3b3d54; }"
            "QMenu::item:selected { background: #3b3d54; }"
        )
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if data and data.get("kind") == "item":
            a = menu.addAction("  📖 在阅读器中打开")
            a.setEnabled(bool(data.get("pdf_path")))
            a.triggered.connect(lambda: self._open_item(data))
            a = menu.addAction("  🔄 手动同步")
            a.triggered.connect(self._on_manual_refresh)
            menu.exec(self._tree.viewport().mapToGlobal(pos))
            return
        if data and data.get("kind") == "collection":
            a = menu.addAction("  🔄 手动同步")
            a.triggered.connect(self._on_manual_refresh)
            menu.exec(self._tree.viewport().mapToGlobal(pos))

    def _open_item(self, data: dict):
        if data.get("pdf_path"):
            self.pdf_selected.emit(data["pdf_path"])

    def _on_manual_refresh(self):
        if self._watcher is not None:
            self._watcher.request_reload()
        else:
            self.refresh()

    # ---- 同步回调 ----

    def _on_watcher_changed(self, diff: dict):
        self.refresh()
        parts = []
        if diff.get("added_items"):
            parts.append(f"+{diff['added_items']} 文献")
        if diff.get("removed_items"):
            parts.append(f"-{diff['removed_items']} 文献")
        if diff.get("added_collections"):
            parts.append(f"+{diff['added_collections']} 集合")
        if diff.get("removed_collections"):
            parts.append(f"-{diff['removed_collections']} 集合")
        if diff.get("modified_items"):
            parts.append(f"~{diff['modified_items']} 文献变更")
        msg = " · ".join(parts) if parts else "Zotero 已同步"
        self._status_label.setText(f"🔁 {msg}")

    def _on_watcher_status(self, msg: str):
        self._status_label.setText(msg)

    def _on_watcher_error(self, err: str):
        self._status_label.setText(f"⚠️ {err}")
        QMessageBox.warning(self, "Zotero 同步失败", err)
