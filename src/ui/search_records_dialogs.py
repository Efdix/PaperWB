"""检索记录库对话框 —— 黑名单管理与检索历史查看。"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QVBoxLayout,
)

from ..core.search_records import (
    BlacklistEntry, BlacklistStore, clear_history, load_history,
)


class BlacklistDialog(QDialog):
    """黑名单管理：查看 / 移除 / 清空。"""

    def __init__(self, store: BlacklistStore, parent=None):
        super().__init__(parent)
        self.setWindowTitle("检索黑名单管理")
        self.resize(620, 480)
        self.setMinimumSize(480, 380)
        self._store = store
        self._changed = False  # 是否发生过移除/清空（关闭后由调用方刷新）

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        title = QLabel("检索黑名单")
        title.setObjectName("titleLabel")
        layout.addWidget(title)
        hint = QLabel(
            "拉黑后的文献不会再出现在 AI 检索、按库推荐、定时巡视和文献补充的"
            "结果中（按 DOI 与标题双重匹配）。")
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(
            "QListWidget { background-color: #fffdfa; border: 1px solid #e5e1d9; "
            "border-radius: 8px; font-size: 12px; color: #29434a; }"
            "QListWidget::item { padding: 6px; border-bottom: 1px solid #eef0ec; }")
        layout.addWidget(self._list, 1)

        self._count_label = QLabel()
        self._count_label.setObjectName("subtitleLabel")
        layout.addWidget(self._count_label)

        btns = QHBoxLayout()
        self._remove_btn = QPushButton("移除选中")
        self._remove_btn.setObjectName("secondaryBtn")
        self._remove_btn.clicked.connect(self._on_remove)
        btns.addWidget(self._remove_btn)
        self._clear_btn = QPushButton("清空黑名单")
        self._clear_btn.setObjectName("dangerBtn")
        self._clear_btn.clicked.connect(self._on_clear)
        btns.addWidget(self._clear_btn)
        btns.addStretch()
        close = QPushButton("关闭")
        close.setObjectName("primaryBtn")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        layout.addLayout(btns)

        self._reload()

    def _reload(self) -> None:
        self._list.clear()
        entries = self._store.items()
        for e in entries:
            label = e.title or e.doi or e.title_norm
            bits = [label]
            if e.doi and e.title:
                bits.append(f"DOI: {e.doi}")
            if e.source:
                bits.append(f"来源：{e.source}")
            if e.reason:
                bits.append(f"原因：{e.reason}")
            if e.added_at:
                bits.append(e.added_at[:16].replace("T", " "))
            item = QListWidgetItem("\n".join(bits))
            item.setData(Qt.ItemDataRole.UserRole, e.key)
            item.setToolTip("\n".join(bits))
            self._list.addItem(item)
        self._count_label.setText(f"共 {len(entries)} 条")
        has = bool(entries)
        self._remove_btn.setEnabled(has)
        self._clear_btn.setEnabled(has)

    def _on_remove(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        key = item.data(Qt.ItemDataRole.UserRole)
        if key and self._store.remove_by_key(str(key)):
            self._changed = True
            self._reload()

    def _on_clear(self) -> None:
        if not len(self._store):
            return
        r = QMessageBox.question(
            self, "清空黑名单", "确定清空全部黑名单？之后这些文献可能重新出现在检索结果中。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self._store.clear()
            self._changed = True
            self._reload()

    @property
    def changed(self) -> bool:
        return self._changed


class SearchHistoryDialog(QDialog):
    """检索历史（记录库）：按时间倒序查看历次检索。"""

    def __init__(self, parent=None, search_dir=None):
        super().__init__(parent)
        self.setWindowTitle("检索记录")
        self.resize(560, 500)
        self.setMinimumSize(460, 380)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        title = QLabel("检索记录")
        title.setObjectName("titleLabel")
        layout.addWidget(title)
        hint = QLabel("历次检索自动留档（最近 200 条）：AI 检索、按库推荐、定时巡视与文献补充。")
        hint.setObjectName("subtitleLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget()
        self._list.setAlternatingRowColors(True)
        self._list.setStyleSheet(
            "QListWidget { background-color: #fffdfa; border: 1px solid #e5e1d9; "
            "border-radius: 8px; font-size: 12px; color: #29434a; }"
            "QListWidget::item { padding: 6px; border-bottom: 1px solid #eef0ec; }")
        layout.addWidget(self._list, 1)

        items = load_history(search_dir)
        for h in items:
            ts = str(h.get("ts", ""))[:16].replace("T", " ")
            kind = h.get("kind", "")
            req = h.get("request", "") or ""
            n = h.get("n_results", 0)
            detail = h.get("detail", "")
            first = f"{ts} · [{kind}] {req[:80]}（{n} 条）"
            item = QListWidgetItem(first)
            tip_lines = [first]
            if detail:
                tip_lines.append(detail)
            item.setToolTip("\n".join(tip_lines))
            self._list.addItem(item)
        if not items:
            self._list.addItem("暂无检索记录")

        btns = QHBoxLayout()
        self._clear_btn = QPushButton("清空记录")
        self._clear_btn.setObjectName("softBtn")
        self._clear_btn.clicked.connect(self._on_clear)
        btns.addWidget(self._clear_btn)
        btns.addStretch()
        close = QPushButton("关闭")
        close.setObjectName("primaryBtn")
        close.clicked.connect(self.accept)
        btns.addWidget(close)
        layout.addLayout(btns)

    def _on_clear(self) -> None:
        r = QMessageBox.question(
            self, "清空检索记录", "确定清空全部检索记录？（不影响黑名单）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            clear_history()
            self._list.clear()
            self._list.addItem("暂无检索记录")
