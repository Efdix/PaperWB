"""Diff 并排对比对话框 —— 原始 vs 润色后，带引文核查备注。"""

from __future__ import annotations

import difflib

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QScrollArea, QFrame, QSplitter, QSizePolicy,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor, QColor


class DiffDialog(QDialog):
    """润色与核查结果展示对话框。

    布局:
        ┌──────────────────────────────────────────┐
        │  原始文本          │  润色后文本           │
        │  (着色标记差异)     │  (着色标记差异)       │
        ├──────────────────────────────────────────┤
        │  引文核查备注 (citation_notes)            │
        ├──────────────────────────────────────────┤
        │          [替换原文]    [取消]              │
        └──────────────────────────────────────────┘
    """

    def __init__(self, original: str, polished: str,
                 citation_notes: list[dict] | None = None,
                 supervisor_notes: list[dict] | None = None,
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 润色与核查结果")
        self.resize(1100, 750)
        self.setMinimumSize(800, 500)

        self._original = original
        self._polished = polished
        self._citation_notes = citation_notes or []
        self._supervisor_notes = supervisor_notes or []
        self._accepted = False

        self._setup_ui()
        self._render_diff()
        self._render_notes()
        self._render_supervisor_notes()

    @property
    def accepted(self) -> bool:
        return self._accepted

    def get_polished_text(self) -> str:
        return self._polished

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ---- Diff 并排区域 ----
        diff_label = QLabel("红色 = 删除 | 绿色 = 新增 | 灰色 = 未变")
        diff_label.setStyleSheet("color: #a9b1d6; font-size: 12px; padding: 2px 8px;")
        layout.addWidget(diff_label)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.setOpaqueResize(False)

        # 原始文本
        orig_frame = self._make_panel("原始文本")
        self._orig_edit = QTextEdit()
        self._orig_edit.setReadOnly(True)
        self._orig_edit.setStyleSheet(
            "QTextEdit { background-color: #1e2030; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; "
            "padding: 12px; font-size: 14px; line-height: 1.8; }"
        )
        orig_frame.layout().addWidget(self._orig_edit)
        splitter.addWidget(orig_frame)

        # 润色后文本
        polished_frame = self._make_panel("润色后文本")
        self._polished_edit = QTextEdit()
        self._polished_edit.setReadOnly(True)
        self._polished_edit.setStyleSheet(
            "QTextEdit { background-color: #1e2030; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; "
            "padding: 12px; font-size: 14px; line-height: 1.8; }"
        )
        polished_frame.layout().addWidget(self._polished_edit)
        splitter.addWidget(polished_frame)
        splitter.setSizes([500, 500])

        layout.addWidget(splitter, 2)

        # ---- 引文核查备注 ----
        self._notes_area = None
        self._notes_layout = None
        self._sup_area = None
        self._sup_layout = None

        if self._citation_notes:
            notes_label = QLabel("引文核查备注")
            notes_label.setStyleSheet(
                "color: #c4d3ff; font-weight: bold; font-size: 13px; padding: 4px 0;"
            )
            layout.addWidget(notes_label)

            self._notes_area = QScrollArea()
            self._notes_area.setWidgetResizable(True)
            self._notes_area.setMaximumHeight(130)
            self._notes_area.setStyleSheet(
                "QScrollArea { background-color: #1a1b26; border: 1px solid #2a2c3d; "
                "border-radius: 6px; }"
                "QScrollArea > QWidget > QWidget { background-color: #1a1b26; }"
            )
            self._notes_widget = QFrame()
            self._notes_widget.setStyleSheet("background-color: #1a1b26;")
            self._notes_layout = QVBoxLayout(self._notes_widget)
            self._notes_layout.setSpacing(4)
            self._notes_layout.setContentsMargins(8, 6, 8, 6)
            self._notes_area.setWidget(self._notes_widget)
            layout.addWidget(self._notes_area)

        if self._supervisor_notes:
            sup_label = QLabel("导师意见处理")
            sup_label.setStyleSheet(
                "color: #ffc777; font-weight: bold; font-size: 13px; padding: 4px 0;"
            )
            layout.addWidget(sup_label)

            self._sup_area = QScrollArea()
            self._sup_area.setWidgetResizable(True)
            self._sup_area.setMaximumHeight(100)
            self._sup_area.setStyleSheet(
                "QScrollArea { background-color: #1a1b26; border: 1px solid #2a2c3d; "
                "border-radius: 6px; }"
                "QScrollArea > QWidget > QWidget { background-color: #1a1b26; }"
            )
            self._sup_widget = QFrame()
            self._sup_widget.setStyleSheet("background-color: #1a1b26;")
            self._sup_layout = QVBoxLayout(self._sup_widget)
            self._sup_layout.setSpacing(4)
            self._sup_layout.setContentsMargins(8, 6, 8, 6)
            self._sup_area.setWidget(self._sup_widget)
            layout.addWidget(self._sup_area)

        # ---- 按钮 ----
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)
        accept_btn = QPushButton("替换原文")
        accept_btn.setObjectName("successBtn")
        accept_btn.clicked.connect(self._on_accept)
        btn_layout.addWidget(accept_btn)
        layout.addLayout(btn_layout)

    @staticmethod
    def _make_panel(title: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet("background-color: #1a1b26; border: none;")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(4, 0, 4, 4)
        header = QLabel(title)
        header.setStyleSheet("color: #c4d3ff; font-weight: bold; font-size: 13px; padding: 2px 8px;")
        lay.addWidget(header)
        return frame

    def _render_diff(self):
        """计算并渲染字符级 diff。"""
        matcher = difflib.SequenceMatcher(None, self._original, self._polished)

        orig_fmt_equal = self._fmt(QColor("#a9b1d6"))
        orig_fmt_del = self._fmt(QColor("#f7768e"), bg=QColor("#2d1a22"))
        orig_fmt_replace = self._fmt(QColor("#f7768e"), bg=QColor("#2d1a22"))
        orig_fmt_insert = self._fmt(QColor("#a9b1d6"))

        pol_fmt_equal = self._fmt(QColor("#a9b1d6"))
        pol_fmt_replace = self._fmt(QColor("#9ece6a"), bg=QColor("#1d2a1d"))
        pol_fmt_insert = self._fmt(QColor("#9ece6a"), bg=QColor("#1d2a1d"))
        pol_fmt_del = self._fmt(QColor("#a9b1d6"))

        orig_cursor = self._orig_edit.textCursor()
        pol_cursor = self._polished_edit.textCursor()

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                self._insert(orig_cursor, self._original[i1:i2], orig_fmt_equal)
                self._insert(pol_cursor, self._polished[j1:j2], pol_fmt_equal)
            elif tag == "delete":
                self._insert(orig_cursor, self._original[i1:i2], orig_fmt_del)
            elif tag == "insert":
                self._insert(pol_cursor, self._polished[j1:j2], pol_fmt_insert)
            elif tag == "replace":
                self._insert(orig_cursor, self._original[i1:i2], orig_fmt_replace)
                self._insert(pol_cursor, self._polished[j1:j2], pol_fmt_replace)

    def _render_notes(self):
        """渲染引文核查备注列表。"""
        if not hasattr(self, '_notes_layout'):
            return

        for note in self._citation_notes:
            marker = note.get("marker", "?")
            status = note.get("status", "unchecked")
            text = note.get("note", "")

            status_icons = {
                "accurate": ("✅", "#9ece6a"),
                "corrected": ("🔧", "#e0af68"),
                "partial": ("⚠️", "#e0af68"),
                "unchecked": ("❓", "#636688"),
            }
            icon, color = status_icons.get(status, ("❓", "#636688"))

            row = QFrame()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(8)

            icon_label = QLabel(icon)
            icon_label.setStyleSheet(f"color: {color}; font-size: 14px; background: transparent;")
            icon_label.setFixedWidth(24)
            row_layout.addWidget(icon_label)

            text_label = QLabel(f"<b>{marker}</b> {text}")
            text_label.setWordWrap(True)
            text_label.setStyleSheet(f"color: #e2e5f2; font-size: 12px; padding: 2px 0;")
            row_layout.addWidget(text_label, 1)

            self._notes_layout.addWidget(row)

        self._notes_layout.addStretch()

    def _render_supervisor_notes(self):
        """渲染导师意见处理列表。"""
        if not hasattr(self, '_sup_layout') or self._sup_layout is None:
            return
        if not self._supervisor_notes:
            return

        for note in self._supervisor_notes:
            suggestion = note.get("suggestion", "")
            action = note.get("action", "applied")
            text = note.get("note", "")

            action_icons = {
                "applied": ("✅", "#9ece6a"),
                "modified": ("🔧", "#e0af68"),
                "flagged": ("⚠️", "#e0af68"),
            }
            icon, color = action_icons.get(action, ("📝", "#636688"))

            row = QFrame()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(8)

            icon_label = QLabel(icon)
            icon_label.setStyleSheet(f"color: {color}; font-size: 14px; background: transparent;")
            icon_label.setFixedWidth(24)
            row_layout.addWidget(icon_label)

            label_text = f"<b>{suggestion[:120]}</b>"
            if text:
                label_text += f"<br><span style='color: #a9b1d6;'>{text}</span>"
            text_label = QLabel(label_text)
            text_label.setWordWrap(True)
            text_label.setStyleSheet("color: #e2e5f2; font-size: 12px; padding: 2px 0;")
            row_layout.addWidget(text_label, 1)

            self._sup_layout.addWidget(row)

        self._sup_layout.addStretch()

    @staticmethod
    def _fmt(color: QColor, bg: QColor | None = None) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(color)
        if bg:
            fmt.setBackground(bg)
        return fmt

    @staticmethod
    def _insert(cursor: QTextCursor, text: str, fmt: QTextCharFormat):
        cursor.insertText(text, fmt)

    def _on_accept(self):
        self._accepted = True
        self.accept()
