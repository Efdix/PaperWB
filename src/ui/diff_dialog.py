"""Diff 并排对比对话框 —— 原始 vs 润色后，带引文核查备注 + AI 对话 + 同步滚动。"""

from __future__ import annotations

import difflib
import json as _json
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QScrollArea, QFrame, QSplitter, QSizePolicy, QLineEdit,
    QApplication,
)
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor, QColor, QTextDocument

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.writing_coach import WritingCoach
    from ..core.zotero_parser import ZoteroLibrary


class DiffDialog(QDialog):
    """润色与核查结果展示对话框 — 内联diff + 导航工具栏 + 可编辑。"""

    accepted_signal = Signal(str)  # 非模态模式下发射润色后文本

    def __init__(self, original: str, polished: str,
                 citation_notes: list[dict] | None = None,
                 supervisor_notes: list[dict] | None = None,
                 modification_log: list[str] | None = None,
                 citation_sources_text: str = "",
                 write_client: "LLMClient | None" = None,
                 coach: "WritingCoach | None" = None,
                 zotero: "ZoteroLibrary | None" = None,
                 writing_type: str = "综述",
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 润色与核查结果")
        self.resize(1100, 750)
        self.setMinimumSize(800, 550)
        self.setWindowFlags(self.windowFlags() | Qt.WindowType.WindowMaximizeButtonHint | Qt.WindowType.WindowMinimizeButtonHint | Qt.WindowType.Window)

        self._original = original
        self._polished = polished
        self._citation_notes = citation_notes or []
        self._supervisor_notes = supervisor_notes or []
        self._modification_log = modification_log or []
        self._citation_sources_text = citation_sources_text
        self._write_client = write_client
        self._coach = coach
        self._zotero = zotero
        self._writing_type = writing_type
        self._accepted = False
        self._change_anchors: list[tuple[int, int, str]] = []  # (start, end, type)
        self._current_anchor_idx = -1
        self._skip_recompute = False  # 防止渲染/apply 过程中的 textChanged 干扰锚点重算

        self._chat_history: list[dict] = []  # [{"role": "user"|"assistant", "content": str}]

        self._setup_ui()
        self._render_diff()
        self._render_notes()
        self._render_supervisor_notes()
        self._render_modification_log()

    @property
    def accepted(self) -> bool:
        return self._accepted

    def get_polished_text(self) -> str:
        return self._polished

    # ============================================================
    # UI 构建
    # ============================================================

    def _setup_ui(self):
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        # ---- 主垂直 QSplitter: diff | notes | chat ----
        self._main_splitter = QSplitter(Qt.Orientation.Vertical)
        self._main_splitter.setHandleWidth(3)
        self._main_splitter.setOpaqueResize(False)

        # == 顶层: 内联 Diff 区域（单编辑框：红删绿增） ==
        diff_container = QFrame()
        diff_container.setStyleSheet("background-color: #1a1b26; border: none;")
        diff_layout = QVBoxLayout(diff_container)
        diff_layout.setContentsMargins(8, 8, 8, 4)
        diff_layout.setSpacing(4)

        diff_label = QLabel("红色删除线 = 删除 | 绿色 = 新增 | 灰蓝 = 未变")
        diff_label.setStyleSheet("color: #a9b1d6; font-size: 12px; padding: 2px 8px;")
        diff_layout.addWidget(diff_label)

        # 导航工具栏
        nav_row = QHBoxLayout()
        nav_row.setSpacing(6)
        self._prev_btn = QPushButton("\u25c0 \u4e0a\u4e00\u5904")
        self._prev_btn.setToolTip("\u8df3\u8f6c\u5230\u4e0a\u4e00\u5904\u4fee\u6539 (Ctrl+Up)")
        self._prev_btn.clicked.connect(lambda: self._navigate_change(-1))
        nav_row.addWidget(self._prev_btn)
        self._next_btn = QPushButton("\u4e0b\u4e00\u5904 \u25b6")
        self._next_btn.setToolTip("\u8df3\u8f6c\u5230\u4e0b\u4e00\u5904\u4fee\u6539 (Ctrl+Down)")
        self._next_btn.clicked.connect(lambda: self._navigate_change(1))
        nav_row.addWidget(self._next_btn)
        nav_row.addSpacing(8)
        self._accept_btn = QPushButton("\u2705 \u63a5\u53d7")
        self._accept_btn.setToolTip("\u63a5\u53d7\u5f53\u524d\u4fee\u6539\uff08\u4fdd\u7559\u65b0\u589e\uff0c\u79fb\u9664\u5220\u9664\uff09")
        self._accept_btn.clicked.connect(self._accept_current)
        self._accept_btn.setStyleSheet("QPushButton { color: #9ece6a; }")
        nav_row.addWidget(self._accept_btn)
        self._reject_btn = QPushButton("\u274c \u62d2\u7edd")
        self._reject_btn.setToolTip("\u62d2\u7edd\u5f53\u524d\u4fee\u6539\uff08\u4fdd\u7559\u539f\u6587\uff0c\u79fb\u9664\u65b0\u589e\uff09")
        self._reject_btn.clicked.connect(self._reject_current)
        self._reject_btn.setStyleSheet("QPushButton { color: #f7768e; }")
        nav_row.addWidget(self._reject_btn)
        nav_row.addStretch()
        self._anchor_label = QLabel("修改: 0 处")
        self._anchor_label.setStyleSheet("color: #636688; font-size: 11px;")
        nav_row.addWidget(self._anchor_label)
        diff_layout.addLayout(nav_row)

        self._diff_edit = QTextEdit()
        self._diff_edit.setStyleSheet(
            "QTextEdit { background-color: #1e2030; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; "
            "padding: 12px; font-size: 14px; line-height: 2.0; }"
        )
        self._diff_edit.textChanged.connect(self._on_diff_text_changed)
        diff_layout.addWidget(self._diff_edit, 1)
        self._main_splitter.addWidget(diff_container)

        # == 中层: 引文核查备注 ==
        notes_container = QFrame()
        notes_container.setStyleSheet("background-color: #1a1b26; border: none;")
        notes_lo = QVBoxLayout(notes_container)
        notes_lo.setContentsMargins(8, 2, 8, 4)
        notes_lo.setSpacing(2)

        notes_label = QLabel("引文核查备注")
        notes_label.setStyleSheet(
            "color: #c4d3ff; font-weight: bold; font-size: 13px; padding: 2px 8px;"
        )
        notes_lo.addWidget(notes_label)

        self._notes_widget = QFrame()
        self._notes_widget.setStyleSheet("background-color: #1a1b26;")
        self._notes_layout = QVBoxLayout(self._notes_widget)
        self._notes_layout.setSpacing(4)
        self._notes_layout.setContentsMargins(8, 4, 8, 4)

        self._notes_scroll = QScrollArea()
        self._notes_scroll.setWidgetResizable(True)
        self._notes_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._notes_scroll.setStyleSheet(
            "QScrollArea { background-color: #1a1b26; border: 1px solid #2a2c3d; border-radius: 6px; }"
            "QScrollArea > QWidget > QWidget { background-color: #1a1b26; }"
        )
        self._notes_scroll.setWidget(self._notes_widget)
        notes_lo.addWidget(self._notes_scroll)

        self._main_splitter.addWidget(notes_container)

        # == 底层: AI 对话 ==
        chat_container = QFrame()
        chat_container.setStyleSheet("background-color: #1a1b26; border: none;")
        chat_lo = QVBoxLayout(chat_container)
        chat_lo.setContentsMargins(8, 2, 8, 4)
        chat_lo.setSpacing(2)

        chat_label = QLabel("💬 AI 对话（对修改有疑问可直接提问）")
        chat_label.setStyleSheet(
            "color: #7aa2f7; font-weight: bold; font-size: 13px; padding: 2px 8px;"
        )
        chat_label.setVisible(False)
        chat_lo.addWidget(chat_label)
        self._chat_label = chat_label

        self._chat_scroll = QScrollArea()
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chat_scroll.setStyleSheet(
            "QScrollArea { background-color: #1e2030; border: 1px solid #2a2c3d; border-radius: 6px; }"
        )
        self._chat_widget = QFrame()
        self._chat_widget.setStyleSheet("background-color: #1e2030;")
        self._chat_layout = QVBoxLayout(self._chat_widget)
        self._chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chat_layout.setSpacing(6)
        self._chat_layout.setContentsMargins(10, 8, 10, 8)

        chat_placeholder = QLabel(
            "<span style='color: #636688;'>对 AI 的某个修改有疑问？在这里提问，AI 会基于原文和润色结果给出解释。</span>"
        )
        chat_placeholder.setWordWrap(True)
        self._chat_layout.addWidget(chat_placeholder)
        self._chat_placeholder = chat_placeholder

        self._chat_scroll.setWidget(self._chat_widget)
        self._chat_scroll.setVisible(False)
        chat_lo.addWidget(self._chat_scroll)

        # 聊天输入行
        chat_input_row = QHBoxLayout()
        chat_input_row.setSpacing(6)
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("输入疑问或希望 LLM 修改的部分")
        self._chat_input.setStyleSheet(
            "QLineEdit { background-color: #24253a; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; "
            "padding: 6px 10px; font-size: 13px; }"
            "QLineEdit:focus { border-color: #7aa2f7; }"
        )
        self._chat_input.returnPressed.connect(self._send_chat)
        chat_input_row.addWidget(self._chat_input)
        send_btn = QPushButton("发送")
        send_btn.clicked.connect(self._send_chat)
        send_btn.setStyleSheet(
            "QPushButton { background-color: #7aa2f7; color: #1a1b26; font-weight: bold; "
            "border-radius: 6px; padding: 6px 16px; font-size: 13px; }"
            "QPushButton:hover { background-color: #89b4fa; }"
        )
        chat_input_row.addWidget(send_btn)
        chat_lo.addLayout(chat_input_row)

        self._main_splitter.addWidget(chat_container)

        # 设置初始比例: diff占60%, notes占15%, chat占25%
        self._main_splitter.setStretchFactor(0, 6)
        self._main_splitter.setStretchFactor(1, 1)
        self._main_splitter.setStretchFactor(2, 3)
        self._main_splitter.setSizes([420, 100, 180])

        outer.addWidget(self._main_splitter, 1)

        # ---- 底部按钮行 ----
        btn_layout = QHBoxLayout()
        btn_layout.setContentsMargins(12, 6, 12, 10)
        btn_layout.setSpacing(8)

        export_chat_btn = QPushButton("导出对话记录")
        export_chat_btn.setToolTip("将 AI 对话导出为 Markdown 文件")
        export_chat_btn.clicked.connect(self._export_chat_md)
        btn_layout.addWidget(export_chat_btn)

        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        accept_btn = QPushButton("替换原文")
        accept_btn.setObjectName("successBtn")
        accept_btn.clicked.connect(self._on_accept)
        btn_layout.addWidget(accept_btn)

        outer.addLayout(btn_layout)

    # ============================================================
    # Diff 渲染（内联单编辑框：红色删除线删除，绿色新增）
    # ============================================================

    def _render_diff(self):
        self._skip_recompute = True
        try:
            self._diff_edit.clear()
            self._change_anchors = []
            matcher = difflib.SequenceMatcher(None, self._original, self._polished)

            fmt_equal = self._fmt(QColor("#a9b1d6"))
            fmt_del = self._fmt(QColor("#f7768e"), bg=QColor("#2d1a22"))
            fmt_insert = self._fmt(QColor("#9ece6a"), bg=QColor("#1d2a1d"))

            cursor = self._diff_edit.textCursor()
            cursor.beginEditBlock()

            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "equal":
                    self._insert(cursor, self._original[i1:i2], fmt_equal)
                elif tag == "delete":
                    start = cursor.position()
                    del_fmt = QTextCharFormat(fmt_del)
                    del_fmt.setFontStrikeOut(True)
                    self._insert(cursor, self._original[i1:i2], del_fmt)
                    self._change_anchors.append((start, cursor.position(), "delete"))
                elif tag == "insert":
                    start = cursor.position()
                    self._insert(cursor, self._polished[j1:j2], fmt_insert)
                    self._change_anchors.append((start, cursor.position(), "insert"))
                elif tag == "replace":
                    start = cursor.position()
                    del_fmt = QTextCharFormat(fmt_del)
                    del_fmt.setFontStrikeOut(True)
                    self._insert(cursor, self._original[i1:i2], del_fmt)
                    self._insert(cursor, self._polished[j1:j2], fmt_insert)
                    self._change_anchors.append((start, cursor.position(), "replace"))

            cursor.endEditBlock()
            self._current_anchor_idx = -1
            self._anchor_label.setText(f"修改: {len(self._change_anchors)} 处")
            self._highlight_citations()
        finally:
            self._skip_recompute = False

    def _on_diff_text_changed(self):
        """用户手动编辑 diff 后重算修改锚点（位置偏移后仍可正确导航/接受/拒绝）。"""
        if self._skip_recompute:
            return
        self._recompute_anchors()

    def _recompute_anchors(self):
        """从当前文档的字符格式重建修改锚点。

        - 删除线 → delete
        - 绿底（非删除线）→ insert
        - 相邻 delete+insert 合并为 replace
        """
        doc = self._diff_edit.document()
        n = doc.characterCount()
        deletes: list[tuple[int, int]] = []
        inserts: list[tuple[int, int]] = []

        i = 0
        while i < n:
            fmt = doc.characterFormat(i)
            if fmt.fontStrikeOut():
                j = i
                while j < n and doc.characterFormat(j).fontStrikeOut():
                    j += 1
                deletes.append((i, j))
                i = j
                continue
            bg = fmt.background().color()
            if bg.isValid() and bg.red() < 50 and bg.green() > 100 and bg.blue() < 50:
                j = i
                while j < n:
                    f2 = doc.characterFormat(j)
                    b2 = f2.background().color()
                    if (not f2.fontStrikeOut()
                            and b2.isValid() and b2.red() < 50
                            and b2.green() > 100 and b2.blue() < 50):
                        j += 1
                    else:
                        break
                inserts.append((i, j))
                i = j
                continue
            i += 1

        anchors: list[tuple[int, int, str]] = []
        d_idx = 0
        ins_idx = 0
        while d_idx < len(deletes) or ins_idx < len(inserts):
            d = deletes[d_idx] if d_idx < len(deletes) else None
            ins = inserts[ins_idx] if ins_idx < len(inserts) else None
            if d and ins and d[1] == ins[0]:
                anchors.append((d[0], ins[1], "replace"))
                d_idx += 1
                ins_idx += 1
            elif d and (not ins or d[0] < ins[0]):
                anchors.append((d[0], d[1], "delete"))
                d_idx += 1
            else:
                anchors.append((ins[0], ins[1], "insert"))
                ins_idx += 1

        self._change_anchors = anchors
        self._current_anchor_idx = -1
        self._anchor_label.setText(f"修改: {len(self._change_anchors)} 处")

    def _highlight_citations(self):
        """高亮引用标记：Author-Year / [n] / 中文格式。"""
        doc = self._diff_edit.document()
        plain = doc.toPlainText()
        h_fmt = QTextCharFormat()
        h_fmt.setBackground(QColor("#3d3520"))
        h_fmt.setForeground(QColor("#e0af68"))

        import re
        patterns = [
            (r'\(([^)]*\d{4}[a-z]?)\)'),
            (r'\[(\d+(?:[,\-]\d+)*)\]'),
            (r'（[^）]*?\d{4}）'),
            (r'[A-Z][a-z]+等（\d{4}）'),
            (r'[A-Z]\w+(?:\s+(?:et al\.|& [A-Z]\w+))?,\s*\d{4}[a-z]?'),
        ]
        cursor = QTextCursor(doc)
        for pattern in patterns:
            for m in re.finditer(pattern, plain):
                cursor.setPosition(m.start())
                cursor.setPosition(m.end(), QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(h_fmt)

    def _navigate_change(self, delta: int):
        """从当前光标位置查找上一处/下一处修改。"""
        if not self._change_anchors:
            return
        cur_pos = self._diff_edit.textCursor().position()
        total = len(self._change_anchors)

        if delta > 0:
            # 找下一个：第一个 start > cur_pos 的锚点
            for i in range(total):
                if self._change_anchors[i][0] > cur_pos:
                    idx = i
                    break
            else:
                idx = 0  # 循环到第一个
        else:
            # 找上一个：最后一个 end < cur_pos 的锚点
            idx = -1
            for i in range(total):
                if self._change_anchors[i][1] >= cur_pos:
                    break
                idx = i
            if idx < 0:
                idx = total - 1  # 循环到最后一个

        start, end, _ = self._change_anchors[idx]
        cursor = self._diff_edit.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._diff_edit.setTextCursor(cursor)
        self._diff_edit.setFocus()
        self._current_anchor_idx = idx
        self._anchor_label.setText(f"修改: {idx + 1}/{total}")

    def _accept_current(self):
        self._apply_change(accept=True)

    def _reject_current(self):
        self._apply_change(accept=False)

    def _apply_change(self, accept: bool):
        if not self._change_anchors:
            return
        idx = self._current_anchor_idx
        if idx < 0:
            return

        start, end, kind = self._change_anchors[idx]
        doc = self._diff_edit.document()
        old_len = len(doc.toPlainText())

        cursor = QTextCursor(doc)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._skip_recompute = True
        cursor.beginEditBlock()

        if accept:
            if kind == "delete":
                cursor.removeSelectedText()
            elif kind == "insert":
                plain_fmt = QTextCharFormat()
                plain_fmt.setForeground(QColor("#a9b1d6"))
                plain_fmt.setBackground(QColor(0, 0, 0, 0))
                plain_fmt.setFontStrikeOut(False)
                cursor.mergeCharFormat(plain_fmt)
            else:  # replace: remove red-strikethrough, un-format green
                self._strip_strikethrough_in_selection(cursor, start, end)
        else:
            if kind == "delete":
                plain_fmt = QTextCharFormat()
                plain_fmt.setForeground(QColor("#a9b1d6"))
                plain_fmt.setBackground(QColor(0, 0, 0, 0))
                plain_fmt.setFontStrikeOut(False)
                cursor.mergeCharFormat(plain_fmt)
            elif kind == "insert":
                cursor.removeSelectedText()
            else:  # replace: remove green, un-format red
                self._strip_green_in_selection(cursor, start, end)

        cursor.endEditBlock()
        self._skip_recompute = False
        new_len = len(doc.toPlainText())
        delta = new_len - old_len

        # 从锚点列表移除当前项，偏移后续锚点
        self._change_anchors.pop(idx)
        for i in range(idx, len(self._change_anchors)):
            s, e, k = self._change_anchors[i]
            self._change_anchors[i] = (s + delta, e + delta, k)

        total = len(self._change_anchors)
        self._anchor_label.setText(f"\u4fee\u6539: {total} \u5904")
        self._highlight_citations()

        if total > 0:
            if idx >= total:
                idx = total - 1
            self._current_anchor_idx = idx
            s, e, _ = self._change_anchors[idx]
            cursor = QTextCursor(doc)
            cursor.setPosition(s)
            cursor.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
            self._diff_edit.setTextCursor(cursor)
        self._diff_edit.setFocus()

    @staticmethod
    def _strip_strikethrough_in_selection(cursor: QTextCursor, sel_start: int, sel_end: int):
        """删除选区内的删除线文本，保留其余。"""
        cursor.setPosition(sel_start)
        while cursor.position() < sel_end:
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            if cursor.charFormat().fontStrikeOut():
                cursor.removeSelectedText()
                sel_end -= 1
            else:
                cursor.clearSelection()
        # 清除剩余文字的背景和前景格式
        cursor.setPosition(sel_start)
        cursor.setPosition(sel_end, QTextCursor.MoveMode.KeepAnchor)
        plain_fmt = QTextCharFormat()
        plain_fmt.setForeground(QColor("#a9b1d6"))
        plain_fmt.setBackground(QColor(0, 0, 0, 0))
        plain_fmt.setFontStrikeOut(False)
        cursor.mergeCharFormat(plain_fmt)

    @staticmethod
    def _strip_green_in_selection(cursor: QTextCursor, sel_start: int, sel_end: int):
        """删除选区内的绿色文本，保留其余。"""
        cursor.setPosition(sel_start)
        while cursor.position() < sel_end:
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter, QTextCursor.MoveMode.KeepAnchor)
            cf = cursor.charFormat()
            bg_c = cf.background().color()
            if bg_c.red() < 50 and bg_c.green() > 100 and bg_c.blue() < 50:
                cursor.removeSelectedText()
                sel_end -= 1
            else:
                cursor.clearSelection()
        cursor.setPosition(sel_start)
        cursor.setPosition(sel_end, QTextCursor.MoveMode.KeepAnchor)
        plain_fmt = QTextCharFormat()
        plain_fmt.setForeground(QColor("#a9b1d6"))
        plain_fmt.setBackground(QColor(0, 0, 0, 0))
        plain_fmt.setFontStrikeOut(False)
        cursor.mergeCharFormat(plain_fmt)

    # ============================================================
    # 引文核查备注
    # ============================================================

    def _render_notes(self):
        if self._notes_layout is None:
            return
        if not self._citation_notes:
            no_note = QLabel(
                "<span style='color: #636688;'>（无引文核查结果 — 可能未检测到引用标记或 Zotero 未连接）</span>"
            )
            no_note.setWordWrap(True)
            self._notes_layout.addWidget(no_note)
            return

        for note in self._citation_notes:
            if not isinstance(note, dict):
                continue
            marker = note.get("marker", "?")
            status = note.get("status", "unchecked")
            text = note.get("note", "")

            status_icons = {
                "accurate": "OK",
                "corrected": "FIX",
                "partial": "??",
                "unchecked": "--",
            }
            status_colors = {
                "accurate": "#9ece6a",
                "corrected": "#e0af68",
                "partial": "#e0af68",
                "unchecked": "#636688",
            }
            icon = status_icons.get(status, "--")
            color = status_colors.get(status, "#636688")

            row = QFrame()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(8)

            icon_label = QLabel(icon)
            icon_label.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: bold; "
                f"background: transparent; border: 1px solid {color}; border-radius: 3px; "
                f"padding: 1px 4px;"
            )
            icon_label.setFixedWidth(36)
            row_layout.addWidget(icon_label)

            text_label = QLabel(f"<b>{marker}</b>  {text}")
            text_label.setWordWrap(True)
            text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            text_label.setStyleSheet("color: #e2e5f2; font-size: 12px; padding: 2px 0;")
            row_layout.addWidget(text_label, 1)

            self._notes_layout.addWidget(row)

        self._notes_layout.addStretch()

    def _render_supervisor_notes(self):
        if not self._supervisor_notes:
            return

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #2a2c3d; max-height: 1px; margin: 4px 0;")
        self._notes_layout.addWidget(sep)

        sup_header = QLabel("<b>导师意见处理</b>")
        sup_header.setStyleSheet("color: #ffc777; font-size: 12px; padding: 2px 0;")
        self._notes_layout.addWidget(sup_header)

        for note in self._supervisor_notes:
            suggestion = note.get("suggestion", "")
            action = note.get("action", "applied")
            text = note.get("note", "")

            action_icons = {"applied": "OK", "modified": "FIX", "flagged": "!!"}
            action_colors = {"applied": "#9ece6a", "modified": "#e0af68", "flagged": "#e0af68"}
            icon = action_icons.get(action, "??")
            color = action_colors.get(action, "#636688")

            row = QFrame()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(4, 2, 4, 2)
            row_layout.setSpacing(8)

            icon_label = QLabel(icon)
            icon_label.setStyleSheet(
                f"color: {color}; font-size: 11px; font-weight: bold; "
                f"background: transparent; border: 1px solid {color}; border-radius: 3px; "
                f"padding: 1px 4px;"
            )
            icon_label.setFixedWidth(36)
            row_layout.addWidget(icon_label)

            label_text = f"<b>{suggestion[:120]}</b>"
            if text:
                label_text += f"<br><span style='color: #a9b1d6;'>{text}</span>"
            text_label = QLabel(label_text)
            text_label.setWordWrap(True)
            text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            text_label.setStyleSheet("color: #e2e5f2; font-size: 12px; padding: 2px 0;")
            row_layout.addWidget(text_label, 1)

            self._notes_layout.addWidget(row)

    def _render_modification_log(self):
        """渲染任务修改说明（去 AI 味 / 中译英 等任务的 modification_log）。"""
        if not self._modification_log:
            return

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #2a2c3d; max-height: 1px; margin: 4px 0;")
        self._notes_layout.addWidget(sep)

        header = QLabel("<b>修改说明</b>")
        header.setStyleSheet("color: #7aa2f7; font-size: 12px; padding: 2px 0;")
        self._notes_layout.addWidget(header)

        for item in self._modification_log:
            text = str(item)
            if not text:
                continue
            label = QLabel(f"· {text}")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setStyleSheet("color: #a9b1d6; font-size: 12px; padding: 1px 0;")
            self._notes_layout.addWidget(label)

    # ============================================================
    # AI 聊天
    # ============================================================

    def _send_chat(self):
        text = self._chat_input.text().strip()
        if not text:
            return
        if not self._write_client:
            # 显示聊天区以便看到系统消息
            self._reveal_chat()
            self._add_chat_bubble("system", "未配置写作 API，无法发起对话。")
            return

        self._reveal_chat()
        self._chat_input.setEnabled(False)
        self._chat_input.setText("")
        self._add_chat_bubble("user", text)
        self._add_chat_bubble("assistant", "⏳ 思考中...")
        self._chat_history.append({"role": "user", "content": text})

        # 构建上下文（含 PDF 全文，方便 LLM 回答具体文献问题）
        context = (
            f"【原始文本】\n{self._original}\n\n"
            f"【润色后文本】\n{self._polished}\n\n"
            f"【引文核查结果】\n{_json.dumps(self._citation_notes, ensure_ascii=False)}\n\n"
            f"【引文涉及的文献全文】（你可据此回答用户关于具体论文细节的疑问）\n{self._citation_sources_text}\n\n"
        )
        system_prompt = "你是学术写作助手的对话伙伴。用户对 AI 的润色修改有疑问，你需要基于原始文本和润色结果，解释修改的理由、引文的依据，或接受用户的指正。回答要简洁、有根据、不使用表情符号。回答长度控制在 200 字以内。"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": context + f"\n【对话历史】\n{self._build_chat_history_text()}\n\n【用户最新问题】\n{text}"},
        ]

        # 在后台线程中调用 LLM
        from PySide6.QtCore import QThread, Signal as QtSignal

        class ChatWorker(QThread):
            finished_sig = QtSignal(str)
            error_sig = QtSignal(str)

            def __init__(self, client, messages):
                super().__init__()
                self._client = client
                self._messages = messages

            def run(self):
                try:
                    reply = self._client.chat_sync(self._messages, timeout=120.0)
                    self.finished_sig.emit(reply or "")
                except Exception as e:
                    self.error_sig.emit(str(e))

        def on_done(reply: str):
            self._chat_input.setEnabled(True)
            self._chat_input.setFocus()
            # 替换占位气泡
            self._chat_history.append({"role": "assistant", "content": reply})
            self._refresh_chat_bubbles()

        def on_error(err: str):
            self._chat_input.setEnabled(True)
            self._chat_history.append({"role": "assistant", "content": f"对话出错：{err}"})
            self._refresh_chat_bubbles()

        self._chat_worker = ChatWorker(self._write_client, messages)
        self._chat_worker.finished_sig.connect(on_done)
        self._chat_worker.error_sig.connect(on_error)
        self._chat_worker.start()

    def _build_chat_history_text(self) -> str:
        lines = []
        for m in self._chat_history[-8:]:
            role = "用户" if m["role"] == "user" else "AI"
            lines.append(f"{role}: {m['content'][:500]}")
        return "\n".join(lines)

    def _reveal_chat(self):
        """首次发送消息时显示聊天记录区域。"""
        self._chat_scroll.setVisible(True)
        self._chat_label.setVisible(True)

    def _add_chat_bubble(self, role: str, text: str):
        if role == "user":
            color = "#7aa2f7"
            prefix = "你"
        elif role == "assistant":
            color = "#9ece6a"
            prefix = "AI"
        else:
            color = "#e0af68"
            prefix = "系统"

        label = QLabel(f"<b style='color: {color};'>{prefix}:</b> "
                       f"<span style='color: #cfd2e3;'>{text}</span>")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(
            f"background-color: {'#24253a' if role == 'user' else '#1e2030'}; "
            f"border-radius: 6px; padding: 6px 10px; font-size: 13px; line-height: 1.6;"
        )
        self._chat_layout.addWidget(label)

        # 滚动到底部
        QApplication.processEvents()
        sb = self._chat_scroll.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _refresh_chat_bubbles(self):
        # 清空并重新渲染所有气泡
        while self._chat_layout.count():
            item = self._chat_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._chat_placeholder = None
        for m in self._chat_history:
            self._add_chat_bubble(m["role"], m["content"])

    # ============================================================
    # 工具
    # ============================================================

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
        text = self._diff_edit.toPlainText()
        self.accepted_signal.emit(text)
        self.accept()

    def _export_chat_md(self):
        """导出 AI 对话为 Markdown 文件。"""
        if not self._chat_history:
            return
        from PySide6.QtWidgets import QFileDialog
        path, _ = QFileDialog.getSaveFileName(self, "导出对话记录", "polish_chat.md", "Markdown (*.md)")
        if not path:
            return
        lines = ["# AI 润色对话记录\n"]
        for m in self._chat_history:
            role = "**你**" if m["role"] == "user" else "**AI**"
            lines.append(f"\n{role}:\n{m['content']}\n")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError:
            pass
