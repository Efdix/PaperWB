"""
聊天面板 —— 侧边栏聊天界面
"""

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QScrollArea, QLabel, QSizePolicy, QFrame, QProgressBar,
)
from PySide6.QtCore import Qt, Signal, QTimer, QEvent, QSize
from PySide6.QtGui import QFont, QKeyEvent

from ..utils.layout import calc_layout_height





class ChatBubble(QFrame):
    """单条聊天气泡 —— 优化可读性"""

    def __init__(self, role: str, content: str, parent=None, thinking: bool = False):
        super().__init__(parent)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setProperty("role", role)
        self.setProperty("thinking", thinking)
        self.role = role
        self._thinking = thinking
        self._content_label: QLabel | None = None
        self._thinking_bar: QProgressBar | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 6, 10, 6)
        layout.setSpacing(4)

        # 角色标签
        role_label = QLabel("🤖 AI 回答" if role == "assistant" else "👤 你的问题")
        role_font = QFont("Microsoft YaHei UI", 11)
        role_font.setBold(True)
        role_label.setFont(role_font)
        role_label.setStyleSheet(
            "color: #147c7c; padding: 2px 0;" if role == "assistant"
            else "color: #b45d4a; padding: 2px 0;"
        )
        layout.addWidget(role_label)

        # 内容 —— 大字号，高对比度
        content_label = QLabel(content)
        content_label.setWordWrap(True)
        content_label.setTextFormat(Qt.TextFormat.MarkdownText)
        content_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse |
            Qt.TextInteractionFlag.LinksAccessibleByMouse
        )
        content_font = QFont("Microsoft YaHei UI", 13)
        content_font.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.2)
        content_label.setFont(content_font)
        content_label.setStyleSheet(
            "color: #29434a; line-height: 1.8; padding: 4px 0; font-size: 13px;"
        )
        layout.addWidget(content_label)
        self._content_label = content_label  # 保存引用，方便流式更新

        if thinking:
            thinking_bar = QProgressBar()
            thinking_bar.setObjectName("thinkingProgress")
            thinking_bar.setRange(0, 0)
            thinking_bar.setTextVisible(False)
            thinking_bar.setFixedHeight(6)
            thinking_bar.setStyleSheet(
                "QProgressBar { background: #e7f1ee; border: none; border-radius: 3px; }"
                "QProgressBar::chunk { background: #72b8aa; border-radius: 3px; }"
            )
            layout.addWidget(thinking_bar)
            self._thinking_bar = thinking_bar

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e5e1d9; max-height: 1px; margin-top: 4px;")
        layout.addWidget(sep)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        """根据给定宽度计算所需高度，使文字折行后气泡能正确撑高"""
        marg = self.contentsMargins()
        inner_w = max(w - marg.left() - marg.right(), 50)
        lay = self.layout()
        if lay is None:
            return 40
        h = marg.top() + marg.bottom() + calc_layout_height(lay, inner_w)
        return max(h, 40)

    def sizeHint(self):
        """确保 sizeHint 与 heightForWidth 一致"""
        base = super().sizeHint()
        return QSize(base.width(), self.heightForWidth(base.width()))

    def get_content(self) -> str:
        """获取当前文本内容"""
        return self._content_label.text() if self._content_label else ""

    def append_content(self, chunk: str):
        """追加文本（流式输出）"""
        if self._content_label:
            if self._thinking:
                self.set_thinking(False)
            self._content_label.setText(self._content_label.text() + chunk)
            self.updateGeometry()

    def set_thinking(self, thinking: bool) -> None:
        """切换等待提示；首个回复分片到达时清除占位文本。"""
        self._thinking = bool(thinking)
        self.setProperty("thinking", self._thinking)
        if self._thinking_bar is not None:
            self._thinking_bar.setVisible(self._thinking)
        if self._content_label is not None and not self._thinking:
            self._content_label.setText("")
        self.updateGeometry()

    def set_content(self, content: str) -> None:
        """设置非流式内容，供空回复和历史恢复使用。"""
        if self._content_label is not None:
            self._content_label.setText(content)
            self.updateGeometry()


class ChatPanel(QWidget):
    """聊天面板：消息列表 + 输入区"""

    send_message = Signal(str)       # 用户发送消息
    clear_requested = Signal()       # 请求清空对话

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("chatPanel")
        self._bubbles: list[ChatBubble] = []
        self._current_ai_bubble: ChatBubble | None = None
        self._input_enabled = False
        self._busy = False
        self._setup_ui()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 顶部工具栏
        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 8, 12, 8)

        title = QLabel("论文问答")
        title.setObjectName("titleLabel")
        toolbar.addWidget(title)

        toolbar.addStretch()

        self.token_label = QLabel("")
        self.token_label.setObjectName("subtitleLabel")
        self.token_label.setContentsMargins(0, 0, 8, 0)
        toolbar.addWidget(self.token_label)

        export_btn = QPushButton("导出记录")
        export_btn.setObjectName("secondaryBtn")
        export_btn.setToolTip("导出对话为 Markdown 文件")
        export_btn.clicked.connect(self._on_export)
        toolbar.addWidget(export_btn)

        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("softBtn")
        clear_btn.clicked.connect(self._on_clear)
        toolbar.addWidget(clear_btn)

        layout.addLayout(toolbar)

        # 分隔线
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px;")
        layout.addWidget(sep)

        # 消息滚动区域
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.msg_container = QWidget()
        self.msg_container.setObjectName("chatMessages")
        self.msg_layout = QVBoxLayout(self.msg_container)
        self.msg_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.msg_layout.addStretch()

        # 欢迎消息
        welcome = QLabel(
            "欢迎来到论文问答\n\n"
            "先从左侧 Zotero 文献库或其它文献中选择一篇 PDF，再在下方提出问题。\n"
            "AI 会基于当前论文内容回答，并保留你的对话记录。\n\n"
            "提示：首次使用请先在设置中配置阅读接口。"
        )
        welcome.setWordWrap(True)
        welcome.setStyleSheet(
            "color: #718180; background-color: #f5f8f6; border: 1px solid #e1ebe7; "
            "border-radius: 12px; padding: 18px; font-size: 13px; line-height: 1.8;"
        )
        self.msg_layout.insertWidget(0, welcome)

        self.scroll_area.setWidget(self.msg_container)
        layout.addWidget(self.scroll_area, 1)

        # 输入区
        input_frame = QFrame()
        input_frame.setObjectName("chatInput")
        input_layout = QVBoxLayout(input_frame)
        input_layout.setContentsMargins(12, 10, 12, 12)

        self.input_box = QTextEdit()
        self.input_box.setPlaceholderText("输入你的问题，按 Ctrl+Enter 发送...")
        self.input_box.setMaximumHeight(120)
        self.input_box.setMinimumHeight(64)
        input_layout.addWidget(self.input_box)

        btn_layout = QHBoxLayout()
        btn_layout.addStretch()

        self.send_btn = QPushButton("发送 ✈")
        self.send_btn.setObjectName("primaryBtn")
        self.send_btn.clicked.connect(self._on_send)
        self.send_btn.setEnabled(False)
        btn_layout.addWidget(self.send_btn)

        input_layout.addLayout(btn_layout)
        layout.addWidget(input_frame)

        # 快捷键：Ctrl+Enter 发送
        self.input_box.installEventFilter(self)

    def eventFilter(self, obj, event):
        """处理快捷键"""
        if obj == self.input_box and event.type() == QEvent.Type.KeyPress:
            if event.key() == Qt.Key.Key_Return and event.modifiers() & Qt.KeyboardModifier.ControlModifier:
                self._on_send()
                return True
        return super().eventFilter(obj, event)

    def _on_send(self):
        if self._busy:
            return
        text = self.input_box.toPlainText().strip()
        if not text:
            return
        self.input_box.clear()
        self.send_message.emit(text)

    def _on_clear(self):
        self.clear_requested.emit()

    def add_user_message(self, text: str):
        """添加用户消息气泡"""
        bubble = ChatBubble("user", text)
        self._insert_bubble(bubble)
        self._bubbles.append(bubble)

    def start_ai_response(self):
        """开始 AI 回复（创建空气泡用于流式填充）"""
        if self._busy:
            return
        self._busy = True
        self.send_btn.setEnabled(False)
        self.input_box.setEnabled(False)
        bubble = ChatBubble(
            "assistant",
            "AI 正在思考...",
            thinking=True,
        )
        self._insert_bubble(bubble)
        self._current_ai_bubble = bubble
        self._bubbles.append(bubble)

    def append_ai_text(self, chunk: str):
        """追加 AI 回复文本（流式）"""
        if self._current_ai_bubble:
            self._current_ai_bubble.append_content(chunk)
        self._scroll_to_bottom()

    def finish_ai_response(self):
        """完成 AI 回复"""
        if self._current_ai_bubble:
            self._current_ai_bubble.set_thinking(False)
            if not self._current_ai_bubble.get_content().strip():
                self._current_ai_bubble.set_content("（模型未返回内容）")
        self._current_ai_bubble = None
        self._busy = False
        self._apply_input_state()

    def clear_messages(self):
        """清除所有消息"""
        self._bubbles.clear()
        self._current_ai_bubble = None
        while self.msg_layout.count():
            item = self.msg_layout.takeAt(0)
            widget = item.widget()
            if widget:
                widget.hide()
                widget.setParent(None)
                widget.deleteLater()
        self.msg_layout.addStretch()
        self._busy = False
        self._apply_input_state()

    def _insert_bubble_from_history(self, role: str, content: str):
        """从历史记录恢复气泡（不做动画）"""
        bubble = ChatBubble(role, content)
        self._insert_bubble(bubble)
        self._bubbles.append(bubble)

    def set_input_enabled(self, enabled: bool):
        """设置输入框是否可用"""
        self._input_enabled = bool(enabled)
        self._apply_input_state()

    def _apply_input_state(self) -> None:
        enabled = self._input_enabled and not self._busy
        self.input_box.setEnabled(enabled)
        self.send_btn.setEnabled(enabled)

    def _insert_bubble(self, bubble: ChatBubble):
        """在 stretch 之前插入气泡"""
        self.msg_layout.insertWidget(self.msg_layout.count() - 1, bubble)
        self._scroll_to_bottom()

    def _scroll_to_bottom(self):
        """滚动到底部"""
        QTimer.singleShot(50, lambda: self.scroll_area.verticalScrollBar().setValue(
            self.scroll_area.verticalScrollBar().maximum()
        ))

    def _on_export(self):
        """导出对话为 Markdown 文件"""
        from PySide6.QtWidgets import QFileDialog, QMessageBox
        import datetime
        path, _ = QFileDialog.getSaveFileName(
            self, "导出对话", f"PaperWB_chat_{datetime.date.today()}.md", "Markdown (*.md)"
        )
        if not path:
            return
        lines = ["# PaperWB 对话记录\n", f"导出时间：{datetime.datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n---\n"]
        for bubble in self._bubbles:
            role = "🤖 AI" if bubble.role == "assistant" else "👤 用户"
            content = bubble.get_content()
            lines.append(f"### {role}\n\n{content}\n\n---\n")
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
            QMessageBox.information(self, "导出成功", f"对话已导出到：\n{path}")
        except OSError as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def set_token_count(self, count: int):
        """更新 Token 估算显示"""
        if count > 0:
            if count >= 1_000_000:
                self.token_label.setText(f"上下文约 {count/1_000_000:.1f} 百万令牌")
            elif count >= 1_000:
                self.token_label.setText(f"上下文约 {count/1_000:.0f} 千令牌")
            else:
                self.token_label.setText(f"上下文约 {count} 令牌")
        else:
            self.token_label.setText("")
