"""Zotero 文献库面板 —— 只读镜像 Zotero 集合树，周期同步（每 30 分钟）+ 手动刷新。"""

from __future__ import annotations

import os
import re
import sys
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QLabel, QPushButton, QFrame, QMenu, QMessageBox,
)
from PySide6.QtCore import Qt, Signal

if TYPE_CHECKING:
    from ..core.zotero_parser import ZoteroLibrary
    from ..core.zotero_watcher import ZoteroWatcher


_NUM_RE = re.compile(r"(\d+)")


def _nat_key(text: str) -> list:
    """自然排序键：'2' < '10'，'01' 在 '02' 之前。"""
    return [
        (0, int(part)) if part.isdigit() else (1, part)
        for part in _NUM_RE.split(text.lower())
        if part
    ]


class ZoteroPanel(QWidget):
    """Zotero 只读文献库树形视图。

    信号:
        pdf_selected(str): 用户点击了带 PDF 附件的条目，传出 PDF 绝对路径。
        reparse_requested(str): 清缓存后全流程重跑（解析+整合）。
    """

    pdf_selected = Signal(str)
    reparse_requested = Signal(str)

    def __init__(self, library: "ZoteroLibrary | None" = None,
                 watcher: "ZoteroWatcher | None" = None, parent=None):
        super().__init__(parent)
        self.setObjectName("zoteroPanel")
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
        header.setContentsMargins(16, 14, 14, 10)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("Zotero 文献库")
        title.setObjectName("titleLabel")
        title_box.addWidget(title)
        self._subtitle = QLabel("只读镜像 · 每 30 分钟自动同步")
        self._subtitle.setObjectName("subtitleLabel")
        self._subtitle.setWordWrap(True)
        title_box.addWidget(self._subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self._refresh_btn = QPushButton("刷新")
        self._refresh_btn.setObjectName("secondaryBtn")
        self._refresh_btn.setToolTip("立即手动同步 Zotero 文献库")
        self._refresh_btn.clicked.connect(self._on_manual_refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px;")
        layout.addWidget(sep)

        self._status_label = QLabel("未连接 Zotero")
        self._status_label.setObjectName("statusChip")
        self._status_label.setProperty("status", "warning")
        self._status_label.setContentsMargins(12, 5, 12, 5)
        self._status_label.setWordWrap(True)
        layout.addWidget(self._status_label)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setIndentation(16)
        self._tree.setAnimated(True)
        self._tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self._tree.customContextMenuRequested.connect(self._on_context_menu)
        self._tree.itemClicked.connect(self._on_item_clicked)
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
            for cid in sorted(coll.child_ids, key=self._child_id_sort_key):
                child = lib.get_collection(cid)
                if child is not None:
                    node.addChild(build_collection(child))
            for item in sorted(lib.get_items_in_collection(coll.collection_id),
                               key=self._item_sort_key):
                classified_ids.add(item.item_id)
                node.addChild(self._make_item_node(item))
            return node

        for coll in sorted(lib.get_collections_tree(), key=self._collection_sort_key):
            self._tree.addTopLevelItem(build_collection(coll))

        # 未分类条目
        for item in sorted(lib.get_all_items(), key=self._item_sort_key):
            if item.item_id not in classified_ids:
                unclassified.addChild(self._make_item_node(item))
        if unclassified.childCount() > 0:
            self._tree.addTopLevelItem(unclassified)

        # 默认全部收起（用户手动展开的集合在刷新后仍保留展开状态）
        self._restore_expanded(expanded)
        self._update_status()

    def _collection_sort_key(self, coll) -> list:
        """集合文件夹按名称自然排序（01 → 02 → 09 → 10）。"""
        return _nat_key(coll.name or "")

    def _child_id_sort_key(self, cid: int) -> list:
        """子集合按 ID 解析出集合对象后按名称自然排序。"""
        child = self._library.get_collection(cid) if self._library else None
        return _nat_key(child.name) if child is not None else [""]

    def _item_sort_key(self, item) -> tuple:
        """文献条目排序：有 PDF 的在前，按附件文件名自然排序，无 PDF 的按标题。"""
        has_pdf = bool(item.pdf_path) and os.path.isfile(item.pdf_path)
        fname = os.path.basename(item.pdf_path or "")
        return (
            0 if has_pdf else 1,
            _nat_key(fname) if has_pdf else _nat_key(item.title or ""),
        )

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
            self._subtitle.setText("只读镜像 · 每 30 分钟自动同步")
            self._set_status("未连接 Zotero · 可点击刷新", "warning")
        else:
            self._subtitle.setText(lib.data_dir or "Zotero 文献库")
            self._set_status(
                f"已连接 · {lib.item_count} 篇文献 · {lib.collection_count} 个集合",
                "ready",
            )

    def _set_status(self, text: str, status: str = "warning") -> None:
        self._status_label.setText(text)
        self._status_label.setProperty("status", status)
        self._status_label.style().unpolish(self._status_label)
        self._status_label.style().polish(self._status_label)

    # ---- 交互 ----

    def _on_item_clicked(self, item: QTreeWidgetItem, _col: int):
        data = item.data(0, Qt.ItemDataRole.UserRole) or {}
        if data.get("kind") == "item" and data.get("pdf_path"):
            self.pdf_selected.emit(data["pdf_path"])

    def _on_context_menu(self, pos):
        item = self._tree.itemAt(pos)
        if item is None:
            return  # 空白区域不弹出空菜单
        menu = QMenu(self)
        data = item.data(0, Qt.ItemDataRole.UserRole) if item else None
        if data and data.get("kind") == "item":
            has_pdf = bool(data.get("pdf_path"))
            a = menu.addAction("  📖 在阅读器中打开")
            a.setEnabled(has_pdf)
            a.triggered.connect(lambda: self._open_item(data))
            menu.addSeparator()
            a = menu.addAction("  🔄 重新解析整合")
            a.setEnabled(has_pdf)
            a.triggered.connect(lambda: self._on_reparse(data))
            a = menu.addAction("  📂 打开文件位置")
            a.setEnabled(has_pdf)
            a.triggered.connect(lambda: self._open_file_location(data))
            menu.addSeparator()
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

    # ---- 重新解析整合 / 打开文件位置（与「其它文献」面板行为一致）----

    def _confirm_rerun(self, title: str, message: str) -> bool:
        r = QMessageBox.question(
            self, title, message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        return r == QMessageBox.StandardButton.Yes

    def _on_reparse(self, data: dict):
        """请求重新解析并整合（缓存清除由 app 层在停止后台线程后执行）。"""
        path = data.get("pdf_path", "")
        if not path:
            return
        if self._confirm_rerun(
            "重新解析整合",
            "将清除该文献的逐页解析与整合结果并全流程重跑（解析+整合），\n继续？",
        ):
            self.reparse_requested.emit(path)

    def _open_file_location(self, data: dict):
        """在资源管理器中打开 PDF 所在位置并选中该文件。"""
        path = data.get("pdf_path", "")
        if not path:
            return
        from ..utils.files import open_file_location
        open_file_location(path, parent=self)

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
        self._set_status(f"已同步 · {msg}", "ready")

    def _on_watcher_status(self, msg: str):
        # 「正在同步」→ warning；常态「已连接/已同步」→ ready
        self._set_status(msg, "ready" if msg.startswith("已连接") else "warning")

    def _on_watcher_error(self, err: str):
        self._set_status(f"同步异常 · {err}", "warning")
        QMessageBox.warning(self, "Zotero 同步失败", err)
