"""Diff 并排对比对话框 —— 原始 vs 润色后，带引文核查备注 + AI 对话 + 同步滚动。"""

from __future__ import annotations

import difflib
import json as _json
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QScrollArea, QFrame, QSplitter, QSizePolicy, QLineEdit,
)
from PySide6.QtCore import Qt
from PySide6.QtGui import QFont, QTextCharFormat, QTextCursor, QColor

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.writing_coach import WritingCoach
    from ..core.zotero_parser import ZoteroLibrary


class DiffDialog(QDialog):
    """润色与核查结果展示对话框。

    布局:
        ┌──────────────────────────────────────────┐
        │  原始文本            │  润色后文本         │ ← QSplitter 水平
        │  (着色标记差异)       │  (着色标记差异)     │
        │  ←──同步滚动──→      │                    │
        ├──────────────────────────────────────────┤ ← QSplitter 垂直
        │  引文核查备注                              │ ← 可拖拽高度
        ├──────────────────────────────────────────┤ ← QSplitter 垂直
        │  💬 AI 对话                               │ ← 可拖拽高度
        │  ┌──────────────────────────────┐ [发送]  │
        │  │ 输入疑问...                   │         │
        │  └──────────────────────────────┘         │
        ├──────────────────────────────────────────┤
        │  [根据对话重新润色]  [替换原文]  [取消]     │
        └──────────────────────────────────────────┘
    """

    def __init__(self, original: str, polished: str,
                 citation_notes: list[dict] | None = None,
                 supervisor_notes: list[dict] | None = None,
                 write_client: "LLMClient | None" = None,
                 coach: "WritingCoach | None" = None,
                 zotero: "ZoteroLibrary | None" = None,
                 writing_type: str = "综述",
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("AI 润色与核查结果")
        self.resize(1100, 750)
        self.setMinimumSize(800, 550)

        self._original = original
        self._polished = polished
        self._citation_notes = citation_notes or []
        self._supervisor_notes = supervisor_notes or []
        self._write_client = write_client
        self._coach = coach
        self._zotero = zotero
        self._writing_type = writing_type
        self._accepted = False
        self._syncing = False

        self._chat_history: list[dict] = []  # [{"role": "user"|"assistant", "content": str}]

        self._setup_ui()
        self._render_diff()
        self._render_notes()
        self._render_supervisor_notes()

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

        # == 顶层: Diff 并排区域 ==
        diff_container = QFrame()
        diff_container.setStyleSheet("background-color: #1a1b26; border: none;")
        diff_layout = QVBoxLayout(diff_container)
        diff_layout.setContentsMargins(8, 8, 8, 4)
        diff_layout.setSpacing(4)

        diff_label = QLabel("红色 = 删除 | 绿色 = 新增 | 灰色 = 未变  (左右同步滚动)")
        diff_label.setStyleSheet("color: #a9b1d6; font-size: 12px; padding: 2px 8px;")
        diff_layout.addWidget(diff_label)

        diff_splitter = QSplitter(Qt.Orientation.Horizontal)
        diff_splitter.setHandleWidth(3)
        diff_splitter.setOpaqueResize(False)

        orig_frame = self._make_panel("原始文本")
        self._orig_edit = QTextEdit()
        self._orig_edit.setReadOnly(True)
        self._orig_edit.setStyleSheet(
            "QTextEdit { background-color: #1e2030; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; "
            "padding: 12px; font-size: 14px; line-height: 1.8; }"
        )
        self._orig_edit.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(v, self._orig_edit, self._polished_edit)
        )
        orig_frame.layout().addWidget(self._orig_edit)
        diff_splitter.addWidget(orig_frame)

        polished_frame = self._make_panel("润色后文本")
        self._polished_edit = QTextEdit()
        self._polished_edit.setReadOnly(True)
        self._polished_edit.setStyleSheet(
            "QTextEdit { background-color: #1e2030; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; "
            "padding: 12px; font-size: 14px; line-height: 1.8; }"
        )
        self._polished_edit.verticalScrollBar().valueChanged.connect(
            lambda v: self._sync_scroll(v, self._polished_edit, self._orig_edit)
        )
        polished_frame.layout().addWidget(self._polished_edit)
        diff_splitter.addWidget(polished_frame)
        diff_splitter.setSizes([500, 500])

        diff_layout.addWidget(diff_splitter, 1)
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
        self._chat_input.setPlaceholderText("输入疑问，如：Prakash那篇关于鳞片细胞的表述是否准确？")
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

        self._rerun_btn = QPushButton("根据对话重新润色")
        self._rerun_btn.setToolTip("将聊天历史作为额外指令，让 LLM 重新润色并核查引文")
        self._rerun_btn.clicked.connect(self._on_rerun_polish)
        self._rerun_btn.setStyleSheet(
            "QPushButton { background-color: #3b3d54; color: #e0af68; font-weight: bold; "
            "border: 1px solid #e0af68; border-radius: 6px; padding: 8px 20px; font-size: 13px; }"
            "QPushButton:hover { background-color: #4a4d6a; }"
        )
        btn_layout.addWidget(self._rerun_btn)

        btn_layout.addStretch()

        cancel_btn = QPushButton("取消")
        cancel_btn.clicked.connect(self.reject)
        btn_layout.addWidget(cancel_btn)

        accept_btn = QPushButton("替换原文")
        accept_btn.setObjectName("successBtn")
        accept_btn.clicked.connect(self._on_accept)
        btn_layout.addWidget(accept_btn)

        outer.addLayout(btn_layout)

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

    # ============================================================
    # 同步滚动
    # ============================================================

    def _sync_scroll(self, value: int, source: QTextEdit, target: QTextEdit):
        if self._syncing:
            return
        self._syncing = True
        src_bar = source.verticalScrollBar()
        tgt_bar = target.verticalScrollBar()
        src_max = max(src_bar.maximum(), 1)
        tgt_max = max(tgt_bar.maximum(), 1)
        ratio = value / src_max
        tgt_bar.setValue(int(ratio * tgt_max))
        self._syncing = False

    # ============================================================
    # Diff 渲染
    # ============================================================

    def _render_diff(self):
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
            text_label.setStyleSheet("color: #e2e5f2; font-size: 12px; padding: 2px 0;")
            row_layout.addWidget(text_label, 1)

            self._notes_layout.addWidget(row)

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

        # 构建上下文
        context = (
            f"【原始文本】\n{self._original[:3000]}\n\n"
            f"【润色后文本】\n{self._polished[:3000]}\n\n"
            f"【引文核查结果】\n{_json.dumps(self._citation_notes, ensure_ascii=False)[:2000]}\n\n"
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
                    reply = self._client.chat_sync(self._messages, timeout=120.0, max_tokens=800)
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
        label.setStyleSheet(
            f"background-color: {'#24253a' if role == 'user' else '#1e2030'}; "
            f"border-radius: 6px; padding: 6px 10px; font-size: 13px; line-height: 1.6;"
        )
        self._chat_layout.addWidget(label)

        # 滚动到底部
        QApplication = __import__('PySide6.QtWidgets', fromlist=['QApplication']).QApplication
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
    # 根据对话重新润色
    # ============================================================

    def _on_rerun_polish(self):
        if not self._chat_history:
            return
        if not self._write_client:
            return

        self._rerun_btn.setEnabled(False)
        self._rerun_btn.setText("⏳ 重新润色中...")

        # 构建重新润色的 prompt
        chat_context = self._build_chat_history_text()
        system_prompt = "你是学术写作助手。用户对上次润色结果给出了反馈意见。请根据反馈重新润色原文，同时核查引文。输出格式与之前相同（JSON）。"

        # Style guide
        style_context = ""
        if self._coach:
            style_context = self._coach.build_polish_system_prompt(self._writing_type)

        # Citation sources
        from ..core.unified_writer import UnifiedWriter
        uw = UnifiedWriter()
        citation_sources = uw._build_citation_sources(self._original, self._zotero)

        prompt = (
            f"【风格约束】\n{style_context}\n\n"
            f"【原始文本】\n{self._original}\n\n"
            f"【之前的润色结果】\n{self._polished}\n\n"
            f"【用户反馈（对话历史）】\n{chat_context}\n\n"
            f"【可用引文原文】\n{citation_sources}\n\n"
            f"请根据用户的反馈，重新生成润色后的文字，并更新引文核查结果。\n"
            f"输出格式：\n"
            f'{{\n  "polished_text": "...",\n  "citation_notes": [...],\n  "supervisor_notes": [...]\n}}'
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": prompt},
        ]

        from PySide6.QtCore import QThread, Signal as QtSignal

        class RerunWorker(QThread):
            finished_sig = QtSignal(str)
            error_sig = QtSignal(str)

            def __init__(self, client, messages):
                super().__init__()
                self._client = client
                self._messages = messages

            def run(self):
                try:
                    reply = self._client.chat_sync(self._messages, timeout=180.0, max_tokens=4000)
                    self.finished_sig.emit(reply or "")
                except Exception as e:
                    self.error_sig.emit(str(e))

        def on_rerun_done(raw: str):
            result = uw._parse_response(raw)
            if result:
                self._polished = result.get("polished_text", self._polished)
                new_notes = result.get("citation_notes", [])
                if new_notes:
                    self._citation_notes = new_notes
                new_sup = result.get("supervisor_notes", [])
                if new_sup:
                    self._supervisor_notes = new_sup

                # 重新渲染 diff
                self._orig_edit.clear()
                self._polished_edit.clear()
                self._render_diff()

                # 重新渲染 notes
                while self._notes_layout.count():
                    item = self._notes_layout.takeAt(0)
                    if item.widget():
                        item.widget().deleteLater()
                self._render_notes()
                self._render_supervisor_notes()

                # 添加系统提示
                self._chat_history.append({"role": "assistant", "content": "已根据对话历史重新润色。请查看上方更新的结果。"})
                self._refresh_chat_bubbles()

            self._rerun_btn.setEnabled(True)
            self._rerun_btn.setText("根据对话重新润色")

        def on_rerun_error(err: str):
            self._chat_history.append({"role": "assistant", "content": f"重新润色失败：{err}"})
            self._refresh_chat_bubbles()
            self._rerun_btn.setEnabled(True)
            self._rerun_btn.setText("根据对话重新润色")

        self._rerun_worker = RerunWorker(self._write_client, messages)
        self._rerun_worker.finished_sig.connect(on_rerun_done)
        self._rerun_worker.error_sig.connect(on_rerun_error)
        self._rerun_worker.start()

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
        self.accept()
