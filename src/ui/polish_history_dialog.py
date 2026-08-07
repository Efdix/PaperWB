"""润色历史查看对话框 —— 查看/复制/回插历史润色结果。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QListWidgetItem,
    QLabel, QPushButton, QSplitter, QTextEdit, QApplication, QWidget,
    QMessageBox,
)
from PySide6.QtCore import Qt, Signal


class PolishHistoryDialog(QDialog):
    """展示当前知识库的润色历史（最近 20 条），支持复制与回插。"""

    insert_requested = Signal(str)

    def __init__(self, history: list[dict], parent=None):
        super().__init__(parent)
        self._history = history
        self.setWindowTitle("润色历史")
        self.resize(820, 560)
        self.setMinimumSize(600, 400)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.Window
        )
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)

        self._list = QListWidget()
        self._list.setStyleSheet(
            "QListWidget { background-color: #fffdfa; color: #29434a; "
            "border: 1px solid #e5e1d9; border-radius: 8px; font-size: 12px; }"
            "QListWidget::item { padding: 7px 8px; }"
            "QListWidget::item:selected { background-color: #dff2ec; }"
        )
        self._list.currentRowChanged.connect(self._on_select)
        splitter.addWidget(self._list)

        detail = QWidget()
        detail.setStyleSheet("background-color: #f4f1eb;")
        dl = QVBoxLayout(detail)
        dl.setContentsMargins(0, 0, 0, 0)
        dl.setSpacing(6)

        original_label = QLabel("原文")
        original_label.setStyleSheet("color: #617674; font-size: 12px; font-weight: bold;")
        dl.addWidget(original_label)
        self._original_view = QTextEdit()
        self._original_view.setReadOnly(True)
        self._original_view.setStyleSheet(
            "QTextEdit { background-color: #fffdfa; color: #29434a; "
            "border: 1px solid #e5e1d9; border-radius: 8px; font-size: 13px; }"
        )
        dl.addWidget(self._original_view, 1)

        polished_label = QLabel("润色后")
        polished_label.setStyleSheet("color: #278273; font-size: 12px; font-weight: bold;")
        dl.addWidget(polished_label)
        self._polished_view = QTextEdit()
        self._polished_view.setReadOnly(True)
        self._polished_view.setStyleSheet(
            "QTextEdit { background-color: #fffdfa; color: #29434a; "
            "border: 1px solid #e5e1d9; border-radius: 8px; font-size: 13px; }"
        )
        dl.addWidget(self._polished_view, 1)

        notes_label = QLabel("引文核查备注")
        notes_label.setStyleSheet("color: #b87835; font-size: 12px; font-weight: bold;")
        dl.addWidget(notes_label)
        self._notes_view = QTextEdit()
        self._notes_view.setReadOnly(True)
        self._notes_view.setMaximumHeight(120)
        self._notes_view.setStyleSheet(
            "QTextEdit { background-color: #fffdfa; color: #526b6c; "
            "border: 1px solid #e5e1d9; border-radius: 8px; font-size: 12px; }"
        )
        dl.addWidget(self._notes_view)

        splitter.addWidget(detail)
        splitter.setSizes([220, 600])
        layout.addWidget(splitter, 1)

        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        copy_btn = QPushButton("复制润色后文本")
        copy_btn.setObjectName("secondaryBtn")
        copy_btn.clicked.connect(self._copy_polished)
        copy_btn.setStyleSheet(
            "QPushButton { background: #e9efed; color: #29434a; "
            "border-radius: 7px; padding: 7px 16px; font-size: 13px; }"
            "QPushButton:hover { background: #dcebe7; }"
        )
        btn_row.addWidget(copy_btn)
        insert_btn = QPushButton("插入到光标处")
        insert_btn.setObjectName("primaryBtn")
        insert_btn.clicked.connect(self._insert_at_cursor)
        insert_btn.setStyleSheet(
            "QPushButton { background: #147c7c; color: #ffffff; font-weight: bold; "
            "border-radius: 7px; padding: 7px 16px; font-size: 13px; }"
            "QPushButton:hover { background: #0e696a; }"
        )
        btn_row.addWidget(insert_btn)
        btn_row.addStretch()
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.accept)
        close_btn.setStyleSheet(
            "QPushButton { background: #e9efed; color: #29434a; "
            "border-radius: 7px; padding: 7px 16px; font-size: 13px; }"
            "QPushButton:hover { background: #dcebe7; }"
        )
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        self._populate()

    def _populate(self):
        self._list.clear()
        if not self._history:
            self._list.addItem("（暂无润色历史）")
            return
        for i, entry in enumerate(self._history):
            ts = entry.get("timestamp", "")[:16].replace("T", " ")
            preview = (entry.get("polished_text", "") or "").strip().replace("\n", " ")
            item = QListWidgetItem(f"[{ts}] {preview[:60]}")
            item.setData(Qt.ItemDataRole.UserRole, i)
            self._list.addItem(item)
        self._list.setCurrentRow(0)

    def _current_entry(self) -> dict | None:
        row = self._list.currentRow()
        if row < 0 or not self._history:
            return None
        return self._history[row]

    def _on_select(self, _row: int):
        entry = self._current_entry()
        if not entry:
            return
        self._original_view.setPlainText(entry.get("original", ""))
        self._polished_view.setPlainText(entry.get("polished_text", ""))
        notes = entry.get("citation_notes", []) or []
        lines = []
        for n in notes:
            if isinstance(n, dict):
                marker = n.get("marker", "?")
                status = n.get("status", "?")
                note = n.get("note", "")
                lines.append(f"[{status}] {marker}: {note}")
        sup = entry.get("supervisor_notes", []) or []
        for s in sup:
            if isinstance(s, dict):
                lines.append(f"[批注-{s.get('action', '?')}] {s.get('suggestion', '')}: {s.get('note', '')}")
        self._notes_view.setPlainText("\n".join(lines) if lines else "（无备注）")

    def _copy_polished(self):
        entry = self._current_entry()
        if not entry:
            return
        text = entry.get("polished_text", "")
        if text:
            QApplication.clipboard().setText(text)
            self.statusTip()

    def _insert_at_cursor(self):
        entry = self._current_entry()
        if not entry:
            return
        text = entry.get("polished_text", "")
        if not text:
            QMessageBox.information(self, "提示", "该条记录没有润色文本")
            return
        self.insert_requested.emit(text)
        self.accept()
