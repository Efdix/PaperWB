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

    # 新增文本的背景色（渲染与锚点探测/拒绝删除必须使用同一份定义，
    # 曾经两处各自硬编码导致绿色永远探测不到、拒绝后新增文本残留）
    INSERT_BG = QColor("#e2f3ee")

    def __init__(self, original: str, polished: str,
                 citation_notes: list[dict] | None = None,
                 supervisor_notes: list[dict] | None = None,
                 modification_log: list[str] | None = None,
                 logic_issues: list[dict] | None = None,
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
        self._logic_issues = logic_issues or []
        self._citation_sources_text = citation_sources_text
        self._write_client = write_client
        self._coach = coach
        self._zotero = zotero
        self._writing_type = writing_type
        self._accepted = False
        self._diff_ctrl: DocDiffController | None = None  # _setup_ui 后创建
        self._current_anchor_idx = -1
        self._skip_recompute = False  # 防止渲染/apply 过程中的 textChanged 干扰锚点重算

        self._chat_history: list[dict] = []  # [{"role": "user"|"assistant", "content": str}]

        self._setup_ui()
        from ..core.doc_diff import DocDiffController
        self._diff_ctrl = DocDiffController(self._diff_edit)
        self._diff_ctrl.set_on_changed(self._on_anchors_changed)
        self._render_diff()
        self._render_notes()
        self._render_supervisor_notes()
        self._render_logic_issues()
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
        diff_container.setStyleSheet("background-color: #f4f1eb; border: none;")
        diff_layout = QVBoxLayout(diff_container)
        diff_layout.setContentsMargins(8, 8, 8, 4)
        diff_layout.setSpacing(4)

        diff_label = QLabel("红色删除线 = 删除 | 绿色 = 新增 | 灰蓝 = 未变")
        diff_label.setStyleSheet("color: #617674; font-size: 12px; padding: 2px 8px;")
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
        self._accept_btn.setObjectName("successBtn")
        nav_row.addWidget(self._accept_btn)
        self._reject_btn = QPushButton("\u274c \u62d2\u7edd")
        self._reject_btn.setToolTip("\u62d2\u7edd\u5f53\u524d\u4fee\u6539\uff08\u4fdd\u7559\u539f\u6587\uff0c\u79fb\u9664\u65b0\u589e\uff09")
        self._reject_btn.clicked.connect(self._reject_current)
        self._reject_btn.setObjectName("dangerBtn")
        nav_row.addWidget(self._reject_btn)
        nav_row.addStretch()
        self._anchor_label = QLabel("修改: 0 处")
        self._anchor_label.setStyleSheet("color: #718180; font-size: 11px;")
        nav_row.addWidget(self._anchor_label)
        diff_layout.addLayout(nav_row)

        self._diff_edit = QTextEdit()
        self._diff_edit.setStyleSheet(
            "QTextEdit { background-color: #fffdfa; color: #29434a; "
            "border: 1px solid #d9e1de; border-radius: 8px; "
            "padding: 12px; font-size: 14px; line-height: 2.0; }"
        )
        self._diff_edit.textChanged.connect(self._on_diff_text_changed)
        diff_layout.addWidget(self._diff_edit, 1)
        self._main_splitter.addWidget(diff_container)

        # == 中层: 引文核查备注 ==
        notes_container = QFrame()
        notes_container.setStyleSheet("background-color: #f4f1eb; border: none;")
        notes_lo = QVBoxLayout(notes_container)
        notes_lo.setContentsMargins(8, 2, 8, 4)
        notes_lo.setSpacing(2)

        notes_label = QLabel("引文核查备注")
        notes_label.setStyleSheet(
            "color: #1e3b42; font-weight: bold; font-size: 13px; padding: 2px 8px;"
        )
        notes_lo.addWidget(notes_label)

        self._notes_widget = QFrame()
        self._notes_widget.setStyleSheet("background-color: #fffdfa;")
        self._notes_layout = QVBoxLayout(self._notes_widget)
        self._notes_layout.setSpacing(4)
        self._notes_layout.setContentsMargins(8, 4, 8, 4)

        self._notes_scroll = QScrollArea()
        self._notes_scroll.setWidgetResizable(True)
        self._notes_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._notes_scroll.setStyleSheet(
            "QScrollArea { background-color: #fffdfa; border: 1px solid #e5e1d9; border-radius: 8px; }"
            "QScrollArea > QWidget > QWidget { background-color: #fffdfa; }"
        )
        self._notes_scroll.setWidget(self._notes_widget)
        notes_lo.addWidget(self._notes_scroll)

        self._main_splitter.addWidget(notes_container)

        # == 底层: AI 对话 ==
        chat_container = QFrame()
        chat_container.setStyleSheet("background-color: #f4f1eb; border: none;")
        chat_lo = QVBoxLayout(chat_container)
        chat_lo.setContentsMargins(8, 2, 8, 4)
        chat_lo.setSpacing(2)

        chat_label = QLabel("💬 AI 对话（对修改有疑问可直接提问）")
        chat_label.setStyleSheet(
            "color: #147c7c; font-weight: bold; font-size: 13px; padding: 2px 8px;"
        )
        chat_label.setVisible(False)
        chat_lo.addWidget(chat_label)
        self._chat_label = chat_label

        self._chat_scroll = QScrollArea()
        self._chat_scroll.setWidgetResizable(True)
        self._chat_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chat_scroll.setStyleSheet(
            "QScrollArea { background-color: #fffdfa; border: 1px solid #e5e1d9; border-radius: 8px; }"
        )
        self._chat_widget = QFrame()
        self._chat_widget.setStyleSheet("background-color: #fffdfa;")
        self._chat_layout = QVBoxLayout(self._chat_widget)
        self._chat_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._chat_layout.setSpacing(6)
        self._chat_layout.setContentsMargins(10, 8, 10, 8)

        chat_placeholder = QLabel(
            "<span style='color: #718180;'>对 AI 的某个修改有疑问？在这里提问，AI 会基于原文和润色结果给出解释。</span>"
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
            "QLineEdit { background-color: #ffffff; color: #29434a; "
            "border: 1px solid #d9e1de; border-radius: 8px; "
            "padding: 6px 10px; font-size: 13px; }"
            "QLineEdit:focus { border-color: #54aaa0; }"
        )
        self._chat_input.returnPressed.connect(self._send_chat)
        chat_input_row.addWidget(self._chat_input)
        send_btn = QPushButton("发送")
        send_btn.clicked.connect(self._send_chat)
        send_btn.setObjectName("primaryBtn")
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
    # Diff 渲染（委托 DocDiffController，与写作面板修订共用同一实现）
    # ============================================================

    def _render_diff(self):
        self._diff_ctrl.render(self._original, self._polished)
        self._anchor_label.setText(f"修改: {self._diff_ctrl.anchor_count} 处")

    def _on_anchors_changed(self):
        """锚点变化回调：刷新计数标签。"""
        if self._anchor_label is not None:
            self._anchor_label.setText(f"修改: {self._diff_ctrl.anchor_count} 处")

    def _on_diff_text_changed(self):
        """用户手动编辑 diff 后重算修改锚点（位置偏移后仍可正常导航/接受/拒绝）。"""
        if self._skip_recompute:
            return
        self._diff_ctrl.on_text_changed()

    def _navigate_change(self, delta: int):
        self._diff_ctrl.navigate(delta)
        self._current_anchor_idx = self._diff_ctrl._current_anchor_idx

    def _accept_current(self):
        self._diff_ctrl.apply_change(accept=True)
        self._current_anchor_idx = self._diff_ctrl._current_anchor_idx

    def _reject_current(self):
        self._diff_ctrl.apply_change(accept=False)
        self._current_anchor_idx = self._diff_ctrl._current_anchor_idx

    # ============================================================
    # 引文核查备注
    # ============================================================

    def _render_notes(self):
        if self._notes_layout is None:
            return
        if not self._citation_notes:
            no_note = QLabel(
                "<span style='color: #718180;'>（无引文核查结果，可检查是否检测到引用标记或 Zotero 是否连接）</span>"
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
                "accurate": "准",
                "corrected": "改",
                "partial": "半",
                "unchecked": "待",
            }
            status_colors = {
                "accurate": "#278273",
                "corrected": "#b87835",
                "partial": "#b87835",
                "unchecked": "#82908d",
            }
            icon = status_icons.get(status, "待")
            color = status_colors.get(status, "#82908d")

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
            text_label.setStyleSheet("color: #29434a; font-size: 12px; padding: 2px 0;")
            row_layout.addWidget(text_label, 1)

            self._notes_layout.addWidget(row)

        self._notes_layout.addStretch()

    def _render_supervisor_notes(self):
        if not self._supervisor_notes:
            return

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px; margin: 4px 0;")
        self._notes_layout.addWidget(sep)

        sup_header = QLabel("<b>导师意见处理</b>")
        sup_header.setStyleSheet("color: #b87835; font-size: 12px; padding: 2px 0;")
        self._notes_layout.addWidget(sup_header)

        for note in self._supervisor_notes:
            suggestion = note.get("suggestion", "")
            action = note.get("action", "applied")
            text = note.get("note", "")

            action_icons = {"applied": "采", "modified": "改", "flagged": "注"}
            action_colors = {"applied": "#278273", "modified": "#b87835", "flagged": "#b87835"}
            icon = action_icons.get(action, "注")
            color = action_colors.get(action, "#82908d")

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
                label_text += f"<br><span style='color: #617674;'>{text}</span>"
            text_label = QLabel(label_text)
            text_label.setWordWrap(True)
            text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            text_label.setStyleSheet("color: #29434a; font-size: 12px; padding: 2px 0;")
            row_layout.addWidget(text_label, 1)

            self._notes_layout.addWidget(row)

    def _render_logic_issues(self):
        """渲染红线检查发现的致命问题（只报致命逻辑/术语/语法错误）。"""
        if not self._logic_issues:
            return

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px; margin: 4px 0;")
        self._notes_layout.addWidget(sep)

        header = QLabel("<b>红线检查</b>")
        header.setStyleSheet("color: #b24f4a; font-size: 12px; padding: 2px 0;")
        self._notes_layout.addWidget(header)

        for issue in self._logic_issues:
            if not isinstance(issue, dict):
                continue
            itype = issue.get("type", "问题")
            quote = issue.get("quote", "")
            explanation = issue.get("explanation", "")
            suggestion = issue.get("suggestion", "")

            card = QFrame()
            card.setStyleSheet(
                "QFrame { background: #fff5f2; border: 1px solid #efd5cf; "
                "border-radius: 6px; margin: 2px 0; }"
            )
            cl = QVBoxLayout(card)
            cl.setContentsMargins(8, 6, 8, 6)
            cl.setSpacing(2)

            head = QLabel(f"<span style='color:#b24f4a; font-weight:bold;'>[{itype}]</span>")
            head.setWordWrap(True)
            cl.addWidget(head)

            if quote:
                q = QLabel(f"原文：{quote}")
                q.setWordWrap(True)
                q.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                q.setStyleSheet("color: #617674; font-size: 12px;")
                cl.addWidget(q)
            if explanation:
                e = QLabel(f"说明：{explanation}")
                e.setWordWrap(True)
                e.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                e.setStyleSheet("color: #29434a; font-size: 12px;")
                cl.addWidget(e)
            if suggestion:
                s = QLabel(f"建议：{suggestion}")
                s.setWordWrap(True)
                s.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
                s.setStyleSheet("color: #147c7c; font-size: 12px;")
                cl.addWidget(s)

            self._notes_layout.addWidget(card)

    def _render_modification_log(self):
        """渲染任务修改说明（中译英 等任务的 modification_log）。"""
        if not self._modification_log:
            return

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px; margin: 4px 0;")
        self._notes_layout.addWidget(sep)

        header = QLabel("<b>修改说明</b>")
        header.setStyleSheet("color: #147c7c; font-size: 12px; padding: 2px 0;")
        self._notes_layout.addWidget(header)

        for item in self._modification_log:
            text = str(item)
            if not text:
                continue
            label = QLabel(f"· {text}")
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            label.setStyleSheet("color: #617674; font-size: 12px; padding: 1px 0;")
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
            f"【红线检查结果】\n{_json.dumps(self._logic_issues, ensure_ascii=False)}\n\n"
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
            try:
                self._chat_input.setEnabled(True)
                self._chat_input.setFocus()
                # 替换占位气泡
                self._chat_history.append({"role": "assistant", "content": reply})
                self._refresh_chat_bubbles()
            except RuntimeError:
                pass  # 对话框已销毁（关闭后迟到的回复）

        def on_error(err: str):
            try:
                self._chat_input.setEnabled(True)
                self._chat_history.append({"role": "assistant", "content": f"对话出错：{err}"})
                self._refresh_chat_bubbles()
            except RuntimeError:
                pass

        from ..utils.threads import track
        self._chat_worker = ChatWorker(self._write_client, messages)
        track(self._chat_worker)  # 关闭对话框后线程仍保活至自然退出，防析构崩溃
        self._chat_worker.finished_sig.connect(on_done)
        self._chat_worker.error_sig.connect(on_error)
        self._chat_worker.start()

    def closeEvent(self, event):
        w = getattr(self, "_chat_worker", None)
        if w is not None and w.isRunning():
            w.requestInterruption()  # 线程由注册表保活，自然退出
        super().closeEvent(event)

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
            color = "#147c7c"
            prefix = "你"
        elif role == "assistant":
            color = "#278273"
            prefix = "AI"
        else:
            color = "#b87835"
            prefix = "系统"

        label = QLabel(f"<b style='color: {color};'>{prefix}:</b> "
                       f"<span style='color: #29434a;'>{text}</span>")
        label.setWordWrap(True)
        label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        label.setStyleSheet(
            f"background-color: {'#e7f3ef' if role == 'user' else '#fffdfa'}; "
            f"border: 1px solid {'#c6e3dc' if role == 'user' else '#e5e1d9'}; "
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

    def _on_accept(self):
        if self._diff_ctrl is not None and self._diff_ctrl.has_changes:
            # 尚有未逐项处理的修改：直接取纯文本会把红色删除线文本一并
            # 回插编辑器（新旧混杂），先确认并按「全部接受」处理
            from PySide6.QtWidgets import QMessageBox
            ret = QMessageBox.question(
                self, "还有未处理的修改",
                f"还有 {self._diff_ctrl.anchor_count} 处修改未逐项处理。\n\n"
                "确定替换时将全部按【接受】处理（保留新增、移除删除）。",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ret != QMessageBox.StandardButton.Yes:
                return
            self._diff_ctrl.accept_all()
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
