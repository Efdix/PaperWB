"""PDF 阅读器面板 v2 —— 两阶段管线 + 结构化渲染。"""

from __future__ import annotations

from copy import copy
import os
import time
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QPushButton,
    QLabel, QFrame, QProgressBar, QLineEdit,
    QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QThread, QSize, QPoint, QTimer
from PySide6.QtGui import QFont, QPixmap

from ..utils.layout import calc_layout_height
from ..utils.threads import track

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.pdf_processor import (
        StructuredElement, StructuredDocument, PDFProcessor,
    )

PLACEHOLDER_TEXT = (
    "📄 从左侧 Zotero 文献库或其它文献中选择 PDF 开始阅读\n\n"
    "• 导入后自动 AI 解析论文结构（逐页分析）\n"
    "• 解析完成后点击论文查看结构化阅读视图\n"
    "• 重要图片和表格自动截图展示\n"
    "• 标题/摘要/正文/图表注均可一键翻译为中文"
)

# 关键章节名（subtitle/abstract_heading 匹配到则突出显示）
KEY_SECTIONS = frozenset({
    "abstract", "introduction", "results", "discussion",
    "conclusion", "methods", "method", "background",
    "related work", "summary", "findings",
})


class TranslationWorker(QThread):
    """翻译工作线程 —— 使用阅读-翻译 API。"""
    finished = Signal(int, str)
    error = Signal(int, str)
    done = Signal()  # 线程必然退出信号（run 的 finally 触发）

    def __init__(self, client: "LLMClient", idx: int, text: str):
        super().__init__()
        self._client = client
        self._idx = idx
        self._text = text

    def run(self):
        try:
            result = self._client.chat_sync([
                {"role": "system", "content": (
                    "你是一位学术论文专业翻译，精通中英双语与科研写作。请将用户提供的英文段落译成中文。\n\n"
                    "翻译要求：\n"
                    "1. 术语准确：专业术语首次出现时保留英文并括号注释中文（如 single-cell RNA-seq（单细胞 RNA 测序）），"
                    "后续沿用；人名、机构名、基因/蛋白名保留原文\n"
                    "2. 表达自然：按中文语序拆句重排，避免欧化句式与机翻腔；长难句可适当拆分\n"
                    "3. 忠实原意：不增删信息，不改变数字、单位、引用标记（如 [1]、Smith et al., 2020）\n"
                    "4. 保持段落结构与逻辑连接词\n"
                    "5. 只输出译文本身，不要添加任何解释、注释或原文。"
                )},
                {"role": "user", "content": self._text},
            ])
            self.finished.emit(self._idx, result)
        except Exception as e:
            self.error.emit(self._idx, str(e))
        finally:
            self.done.emit()


class ParagraphCard(QFrame):
    """结构化段落卡片 —— 仅对结构标签词句做视觉区分。"""
    translate_requested = Signal(int, str)
    qa_requested = Signal(str, str, str)  # (element_id, question, image_path 或 "")

    def __init__(self, elem: "StructuredElement", index: int, parent=None):
        super().__init__(parent)
        self._index = index
        self._elem = elem
        self._text = elem.text
        self._is_english = self._detect_en(self._text)
        self._translated = False
        self._trans_text = ""
        self._collapsed = False
        self._qa_edit: QLineEdit | None = None
        self._setup_ui()

    def hasHeightForWidth(self) -> bool:
        return True

    def sizeHint(self):
        """返回当前宽度下包含全部文字和控件的高度。"""
        base = super().sizeHint()
        width = max(base.width(), 640)
        return QSize(width, self.heightForWidth(width))

    def heightForWidth(self, w: int) -> int:
        marg = self.contentsMargins()
        inner_w = max(w - marg.left() - marg.right(), 50)
        lay = self.layout()
        if lay is None:
            return 40
        if self._collapsed and not getattr(self, "_expanded", False):
            return 52  # 折叠条高度
        h = marg.top() + marg.bottom() + calc_layout_height(lay, inner_w)
        return max(h, 40)

    def _sync_card_height(self, width: int | None = None) -> None:
        width = width or self.width() or 640
        if self.layout() is not None:
            self.layout().activate()
        required = self.heightForWidth(max(width, 50))
        if self.minimumHeight() != required:
            self.setMinimumHeight(required)
        self.updateGeometry()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._sync_card_height(self.width())

    def _detect_en(self, text: str) -> bool:
        if not text:
            return False
        ascii_chars = sum(1 for c in text if c.isascii() and c.isalpha())
        alpha_chars = sum(1 for c in text if c.isalpha())
        if alpha_chars == 0:
            return False
        return (ascii_chars / alpha_chars) > 0.5

    def _is_key_section(self) -> bool:
        """判断元素是否为关键章节的结构标签。"""
        etype = self._elem.element_type
        if etype not in ("subtitle", "abstract_heading"):
            return False
        sn = (self._elem.section_name or "").lower().strip()
        if sn in KEY_SECTIONS:
            return True
        # 也检查 text 本身（如 "Abstract"）
        txt = self._text.lower().strip().rstrip(".:：。")
        return txt in KEY_SECTIONS

    def _setup_ui(self):
        policy = QSizePolicy(
            QSizePolicy.Policy.Expanding,
            QSizePolicy.Policy.Preferred,
        )
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMinimumWidth(0)

        etype = self._elem.element_type
        priority = self._elem.display_priority
        level = self._elem.heading_level
        is_key = self._is_key_section()

        if priority == "collapsed":
            self._setup_collapsed_card()
            return

        if etype == "title":
            self._setup_title_card()
        elif etype == "subtitle" and is_key:
            self._setup_key_subtitle_card(level)
        elif etype == "subtitle":
            self._setup_subtitle_card(level)
        elif etype == "abstract_heading" and is_key:
            self._setup_key_abstract_heading_card()
        elif etype == "abstract_heading":
            self._setup_abstract_heading_card()
        elif etype in ("authors", "affiliations", "metadata"):
            self._setup_meta_card()
        elif etype == "abstract_body":
            self._setup_abstract_card()
        elif etype in ("keywords", "acknowledgment", "appendix"):
            self._setup_special_card(etype)
        elif etype in ("figure_caption", "table_caption"):
            self._setup_caption_card()
        elif etype == "reference":
            self._setup_reference_card()
        else:
            self._setup_body_card()
        self._sync_card_height(640)

    def _make_card_base(self, bg: str = "#ffffff", border: str = "#e5e5ea"):
        self.setStyleSheet(
            f"ParagraphCard {{ background-color: {bg}; border: 1px solid {border}; "
            f"border-radius: 10px; margin: 4px 8px; }}"
        )

    def _setup_title_card(self):
        self._make_card_base("#ffffff", "#e5e5ea")  # 标题卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(8)
        f = QFont("Microsoft YaHei UI", 20)
        f.setBold(True)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet("color: #2463c5; letter-spacing: 0.5px;")
        self.text_label.setContentsMargins(0, 4, 0, 4)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)
        self._append_translation_row(layout)

    def _setup_key_subtitle_card(self, level: int):
        """关键章节标题（如 Introduction、Results）—— 醒目的暖金色文字。"""
        self._make_card_base("#ffffff", "#e5e5ea")  # 关键章节标题卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 10)
        layout.setSpacing(6)
        sizes = {1: 18, 2: 16, 3: 14}
        f = QFont("Microsoft YaHei UI", sizes.get(level, 16))
        f.setBold(True)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet(
            "color: #a76d2b;"
        )
        self.text_label.setContentsMargins(14, 6, 0, 6)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)
        self._append_translation_row(layout)

    def _setup_subtitle_card(self, level: int):
        """普通小节标题。"""
        self._make_card_base("#ffffff", "#e5e5ea")  # 普通小节标题卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 8)
        layout.setSpacing(4)
        sizes = {1: 16, 2: 14, 3: 13}
        colors = {1: "#2463c5", 2: "#3478f6", 3: "#6e6e73"}
        f = QFont("Microsoft YaHei UI", sizes.get(level, 14))
        f.setBold(True)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet(
            f"color: {colors.get(level, '#526b6c')};"
        )
        self.text_label.setContentsMargins(10, 3, 0, 3)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)
        self._append_translation_row(layout)

    def _setup_key_abstract_heading_card(self):
        """关键摘要标签 —— 暖金色文字（保留原文小节名，如 Abstract）。"""
        self._make_card_base("#ffffff", "#e5e5ea")  # 关键摘要标签卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 6)
        header = QLabel(f"📝 {self._text or 'Abstract'}")
        header.setStyleSheet("color: #a76d2b; font-size: 16px; font-weight: bold;")
        layout.addWidget(header)
        self.text_label = QLabel("")
        layout.addWidget(self.text_label)

    def _setup_abstract_heading_card(self):
        self._make_card_base("#ffffff", "#e5e5ea")  # 摘要标题卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 6)
        header = QLabel(f"📝 {self._text or 'Abstract'}")
        header.setStyleSheet("color: #3e8e78; font-size: 14px; font-weight: bold;")
        layout.addWidget(header)
        self.text_label = QLabel("")
        layout.addWidget(self.text_label)

    def _setup_meta_card(self):
        self._make_card_base("#ffffff", "#e5e5ea")  # 作者/单位卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 8)
        layout.setSpacing(4)
        labels = {"authors": "👤 作者信息", "affiliations": "🏛️ 作者单位", "metadata": "📋 出版信息"}
        header = QLabel(labels.get(self._elem.element_type, "📋 信息"))
        header.setStyleSheet("color: #82908d; font-size: 10px; font-weight: bold;")
        layout.addWidget(header)
        # 单位/出版信息默认折叠（仅前 240 字符预览，点击展开全文），
        # 避免作者名后 16 张单位卡片刷屏影响阅读。
        preview = self._text
        if self._elem.element_type in ("affiliations", "metadata") and len(self._text) > 240:
            preview = self._text[:240].rstrip() + " …"
            toggle = QPushButton("展开全部")
            toggle.setCursor(Qt.CursorShape.PointingHandCursor)
            toggle.setStyleSheet(
                "QPushButton{border:none;background:transparent;color:#5b6f8a;"
                "font-size:11px;padding:2px 0;}QPushButton:hover{color:#2463c5;}"
            )
            toggle.clicked.connect(lambda: self._expand_meta_text(toggle))
            self._meta_toggle = toggle
            layout.addWidget(toggle)
            self.text_label = QLabel(preview)
            self._meta_collapsed = True
        else:
            self.text_label = QLabel(self._text)
            self._meta_collapsed = False
        f = QFont("Microsoft YaHei UI", 11)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet("color: #718180; line-height: 1.5;")
        self.text_label.setContentsMargins(0, 2, 0, 2)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)

    def _expand_meta_text(self, btn: "QPushButton") -> None:
        """点击「展开全部」：把折叠的全文填回 text_label。"""
        self.text_label.setText(self._text)
        btn.setText("收起")
        btn.clicked.disconnect()
        btn.clicked.connect(lambda: self._collapse_meta_text(btn))

    def _collapse_meta_text(self, btn: "QPushButton") -> None:
        self.text_label.setText(self._text[:240].rstrip() + " …")
        btn.setText("展开全部")
        btn.clicked.disconnect()
        btn.clicked.connect(lambda: self._expand_meta_text(btn))

    def _setup_abstract_card(self):
        self._make_card_base("#ffffff", "#e5e5ea")  # 摘要正文卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(6)
        f = QFont("Microsoft YaHei UI", 13)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet(
            "color: #29434a; line-height: 1.9;"
        )
        self.text_label.setContentsMargins(12, 4, 0, 4)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)
        self._append_translation_row(layout)

    def _setup_special_card(self, etype: str):
        self._make_card_base("#ffffff", "#e5e5ea")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(6)
        labels = {"keywords": "🔑 关键词", "acknowledgment": "🙏 致谢", "appendix": "📎 附录"}
        header = QLabel(labels.get(etype, ""))
        header.setStyleSheet("color: #617674; font-size: 12px; font-weight: bold;")
        layout.addWidget(header)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(QFont("Microsoft YaHei UI", 12))
        self.text_label.setStyleSheet("color: #526b6c; line-height: 1.7;")
        self.text_label.setContentsMargins(0, 4, 0, 4)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)
        if etype == "keywords":
            self._append_translation_row(layout, min_len=8)

    def _setup_caption_card(self):
        self._make_card_base("#ffffff", "#e5e5ea")  # 图注卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 6, 20, 6)
        layout.setSpacing(4)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(QFont("Microsoft YaHei UI", 11))
        self.text_label.setStyleSheet("color: #718180; line-height: 1.5; font-style: italic;")
        self.text_label.setContentsMargins(0, 2, 0, 2)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)
        self._append_translation_row(layout, min_len=8)

    def _setup_reference_card(self):
        self._make_card_base("#ffffff", "#e5e5ea")  # 参考文献卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 6, 20, 6)
        layout.setSpacing(2)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(QFont("Microsoft YaHei UI", 10))
        self.text_label.setStyleSheet("color: #82908d; line-height: 1.4;")
        self.text_label.setContentsMargins(0, 2, 0, 2)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)

    def _setup_collapsed_card(self):
        """折叠内容卡片 —— 显示为可展开的细条（默认折叠，点击展开全文）。"""
        self._make_card_base("#ffffff", "#e5e5ea")  # 折叠卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 8, 16, 8)
        layout.setSpacing(6)
        labels = {
            "reference": "📚 参考文献",
            "acknowledgment": "🙏 致谢",
            "appendix": "📎 附录",
            "metadata": "📋 出版信息",
        }
        name = labels.get(self._elem.element_type, "📋 折叠内容")
        self.toggle_btn = QPushButton(f"{name}（{len(self._text)} 字）▾")
        self.toggle_btn.setObjectName("secondaryBtn")
        self.toggle_btn.setCheckable(True)
        self.toggle_btn.clicked.connect(self._toggle_collapsed)
        layout.addWidget(self.toggle_btn)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(QFont("Microsoft YaHei UI", 10))
        self.text_label.setStyleSheet("color: #82908d; line-height: 1.5;")
        self.text_label.setContentsMargins(0, 2, 0, 2)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        self.text_label.setVisible(False)
        layout.addWidget(self.text_label)
        self._collapsed = True

    def _toggle_collapsed(self, checked: bool):
        self._expanded = bool(checked)
        if hasattr(self, "text_label"):
            self.text_label.setVisible(self._expanded)
        if hasattr(self, "toggle_btn"):
            name = "📚 参考文献" if self._elem.element_type == "reference" else (
                "🙏 致谢" if self._elem.element_type == "acknowledgment" else (
                    "📎 附录" if self._elem.element_type == "appendix" else "📋 折叠内容"))
            self.toggle_btn.setText(
                f"{name}（{len(self._text)} 字）{'▴' if self._expanded else '▾'}"
            )
        self.updateGeometry()
        self._sync_card_height(self.width())
        self.parent().updateGeometry() if self.parent() else None

    def add_qa_input(self, btn_text: str):
        """卡片问答：按钮弹出单行输入框，回车自动关闭并发送。"""
        lay = self.layout()
        if lay is None:
            return
        btn = QPushButton(btn_text)
        btn.setStyleSheet(
            "QPushButton { background-color: #eaf4f1; color: #147c7c; "
            "border: 1px solid #bfddd6; border-radius: 6px; padding: 3px 10px; "
            "font-size: 12px; }"
            "QPushButton:hover { background-color: #dcefe9; }"
        )
        btn.clicked.connect(self._toggle_qa_edit)
        lay.addWidget(btn)
        self.qa_btn = btn

    def _toggle_qa_edit(self):
        if self._qa_edit is None:
            self._qa_edit = QLineEdit()
            self._qa_edit.setPlaceholderText("输入问题，回车发送到右侧对话区（输入框自动关闭）")
            self._qa_edit.setStyleSheet(
                "QLineEdit { border: 1px solid #bfddd6; border-radius: 6px; "
                "padding: 6px 10px; font-size: 12px; }"
            )
            self._qa_edit.returnPressed.connect(self._submit_qa)
            self.layout().addWidget(self._qa_edit)
            self._qa_edit.setFocus()
        elif self._qa_edit.isVisible():
            self._qa_edit.setVisible(False)
        else:
            self._qa_edit.setVisible(True)
            self._qa_edit.setFocus()
        self._sync_card_height(self.width())

    def _submit_qa(self):
        if self._qa_edit is None:
            return
        q = self._qa_edit.text().strip()
        if q:
            self.qa_requested.emit(
                self._elem.element_id or f"index:{self._index}", q, "")
        self._qa_edit.setVisible(False)  # 回车后自动关闭
        self._qa_edit.clear()

    def _append_translation_row(self, layout: QVBoxLayout, with_qa: bool = False,
                                min_len: int = 4) -> None:
        """为卡片追加译文行与翻译按钮 —— 标题/摘要/正文/关键词/图表注均可翻译。

        Args:
            with_qa: 是否附带「问答」按钮（正文卡片用）。
            min_len: 原文去空白后至少这么长才提供翻译（过滤过短的碎片）。
        """
        if len(self._text.strip()) <= min_len:
            return
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setFixedHeight(1)
        sep.setStyleSheet("background-color: #e5e1d9;")
        layout.addWidget(sep)
        self.zh_label = QLabel()
        self.zh_label.setWordWrap(True)
        self.zh_label.setFont(QFont("Microsoft YaHei UI", 12))
        self.zh_label.setStyleSheet("color: #278273; line-height: 1.7;")
        self.zh_label.setContentsMargins(0, 4, 0, 4)
        self.zh_label.setVisible(False)
        self.zh_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.zh_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.zh_label)

        btn_row = QHBoxLayout()
        self.trans_btn = QPushButton("🌐 翻译")
        self.trans_btn.setFixedWidth(90)
        self.trans_btn.clicked.connect(self._request_translate)
        btn_row.addWidget(self.trans_btn)
        if with_qa:
            self.qa_btn = QPushButton("🤖 问答")
            self.qa_btn.setFixedWidth(80)
            self.qa_btn.clicked.connect(self._toggle_qa_edit)
            btn_row.addWidget(self.qa_btn)
        self.re_trans_btn = QPushButton("🔄 重新翻译")
        self.re_trans_btn.setFixedWidth(100)
        self.re_trans_btn.clicked.connect(self._on_re_translate)
        self.re_trans_btn.setVisible(False)
        self.re_trans_btn.setStyleSheet(
            "QPushButton { background-color: #fff4df; color: #a76d2b; border: 1px solid #efd4a4; "
            "border-radius: 6px; padding: 4px 10px; font-size: 12px; }"
            "QPushButton:hover { background-color: #ffebc6; }"
        )
        btn_row.addWidget(self.re_trans_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)

    def _setup_body_card(self):
        """正文段落 —— 保持原样，不做特殊区分。"""
        self._make_card_base("#ffffff", "#e5e5ea")  # 正文卡
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(8)
        f = QFont("Segoe UI" if self._is_english else "Microsoft YaHei UI", 13)
        if self._is_english:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet("color: #29434a; line-height: 1.9; background-color: transparent;")
        self.text_label.setContentsMargins(0, 4, 0, 4)
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.text_label.setCursor(Qt.CursorShape.IBeamCursor)
        layout.addWidget(self.text_label)
        self._append_translation_row(layout, with_qa=True, min_len=20)

    def _request_translate(self):
        if not self._translated and hasattr(self, 'trans_btn'):
            self.trans_btn.setText("翻译中")
            self.trans_btn.setEnabled(False)
            self.translate_requested.emit(self._index, self._text)

    def show_translation(self, zh: str):
        self._translated = True
        self._trans_text = zh
        if hasattr(self, 'zh_label'):
            self.zh_label.setText(zh)
            self.zh_label.setVisible(True)
        if hasattr(self, 'trans_btn'):
            self.trans_btn.setVisible(False)
        if hasattr(self, 're_trans_btn'):
            self.re_trans_btn.setVisible(True)
        self._sync_card_height(self.width())

    def show_translation_error(self, err: str):
        if hasattr(self, 'trans_btn'):
            self.trans_btn.setText("翻译失败")
            self.trans_btn.setEnabled(True)
            self.trans_btn.setToolTip(err)

    def _on_re_translate(self):
        self._translated = False
        if hasattr(self, 'zh_label'):
            self.zh_label.setVisible(False)
        if hasattr(self, 're_trans_btn'):
            self.re_trans_btn.setVisible(False)
        if hasattr(self, 'trans_btn'):
            self.trans_btn.setVisible(True)
        self._request_translate()


class ImageCard(QFrame):
    """图片/表格卡片。"""

    MAX_IMAGE_WIDTH = 560
    MARGIN_LR = 16
    MARGIN_TB = 12
    qa_requested = Signal(str, str, str)  # (element_id, question, image_path)

    def __init__(self, elem: "StructuredElement", parent=None):
        super().__init__(parent)
        self._elem = elem
        self._image_path = elem.image_path
        self._page = elem.page
        self._caption = elem.image_caption
        self._description = elem.image_description
        self._original_pixmap: QPixmap | None = None
        self._pixmap_loaded = False
        self._img_label: QLabel | None = None
        self._page_label: QLabel | None = None
        self._caption_label: QLabel | None = None
        self._desc_label: QLabel | None = None
        self._separator: QFrame | None = None
        self._qa_btn: QPushButton | None = None
        self._qa_edit: QLineEdit | None = None
        self._setup_ui()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        """按实际布局计算整卡高度，避免控件或分割线覆盖图片。"""
        marg = self.contentsMargins()
        inner_w = max(w - marg.left() - marg.right(), 50)
        lay = self.layout()
        if lay is None:
            return 80
        h = marg.top() + marg.bottom() + calc_layout_height(lay, inner_w)
        return max(h, 80)

    def _sync_card_height(self) -> None:
        if self.layout() is not None:
            self.layout().activate()
        required = self.heightForWidth(self.width() or 640)
        if self.minimumHeight() != required:
            self.setMinimumHeight(required)
        self.updateGeometry()

    def _setup_ui(self):
        etype = self._elem.element_type
        icon = "🖼️" if etype == "figure" else "📊"
        label_text = f"{icon} 第 {self._page} 页{'插图' if etype == 'figure' else '表格'}"
        self.setStyleSheet(
            "ImageCard { background-color: #fffdfa; border: 1px solid #e5e1d9; "
            "border-radius: 12px; margin: 8px 12px; }"
        )
        policy = QSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.MARGIN_LR, self.MARGIN_TB, self.MARGIN_LR, self.MARGIN_TB)
        layout.setSpacing(8)
        page_label = QLabel(label_text)
        page_label.setStyleSheet("color: #718180; font-size: 11px;")
        page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(page_label)
        self._page_label = page_label
        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._img_label)
        if self._caption:
            cap = QLabel(self._caption)
            cap.setWordWrap(True)
            cap.setFont(QFont("Microsoft YaHei UI", 11))
            cap.setStyleSheet("color: #718180; font-style: italic;")
            cap.setContentsMargins(0, 4, 0, 4)
            cap.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            cap.setCursor(Qt.CursorShape.IBeamCursor)
            layout.addWidget(cap)
            self._caption_label = cap
        if self._description:
            desc = QLabel(f"提示 · {self._description}")
            desc.setWordWrap(True)
            desc.setFont(QFont("Microsoft YaHei UI", 11))
            desc.setStyleSheet("color: #147c7c;")
            desc.setContentsMargins(0, 4, 0, 4)
            desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            desc.setCursor(Qt.CursorShape.IBeamCursor)
            layout.addWidget(desc)
            self._desc_label = desc

        self._separator = QFrame()
        self._separator.setFrameShape(QFrame.Shape.HLine)
        self._separator.setFixedHeight(1)
        self._separator.setStyleSheet("background-color: #e5e1d9;")
        layout.addWidget(self._separator)

        self._qa_btn = QPushButton("🔍 解读图片" if etype == "figure" else "📊 解读表格")
        self._qa_btn.setStyleSheet(
            "QPushButton { background-color: #eaf4f1; color: #147c7c; "
            "border: 1px solid #bfddd6; border-radius: 6px; padding: 2px 12px; "
            "font-size: 11px; }"
            "QPushButton:hover { background-color: #dcefe9; }"
        )
        self._qa_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._qa_btn.clicked.connect(self._toggle_qa_edit)
        btn_row = QHBoxLayout()
        btn_row.addStretch()
        btn_row.addWidget(self._qa_btn)
        btn_row.addStretch()
        layout.addLayout(btn_row)
        self._load_pixmap()

    def _toggle_qa_edit(self):
        if self._qa_edit is None:
            self._qa_edit = QLineEdit()
            self._qa_edit.setPlaceholderText("输入问题，回车发送到右侧对话区（输入框自动关闭）")
            self._qa_edit.setStyleSheet(
                "QLineEdit { border: 1px solid #bfddd6; border-radius: 6px; "
                "padding: 6px 10px; font-size: 12px; }"
            )
            self._qa_edit.returnPressed.connect(self._submit_qa)
            self.layout().addWidget(self._qa_edit)
            self._qa_edit.setFocus()
        elif self._qa_edit.isVisible():
            self._qa_edit.setVisible(False)
        else:
            self._qa_edit.setVisible(True)
            self._qa_edit.setFocus()
        self._sync_card_height()

    def _submit_qa(self):
        if self._qa_edit is None:
            return
        q = self._qa_edit.text().strip()
        if q:
            self.qa_requested.emit(
                self._elem.element_id or "", q, self._image_path or "")
        self._qa_edit.setVisible(False)  # 回车后自动关闭
        self._qa_edit.clear()

    def _load_pixmap(self):
        if self._pixmap_loaded:
            return
        self._pixmap_loaded = True
        if not self._image_path or not os.path.exists(self._image_path):
            return
        pixmap = QPixmap(self._image_path)
        if not pixmap.isNull():
            self._original_pixmap = pixmap

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply_pixmap()

    def showEvent(self, event):
        super().showEvent(event)
        if not self._pixmap_loaded:
            self._load_pixmap()
        self._apply_pixmap()

    def _apply_pixmap(self):
        card_w = self.width()
        if card_w <= 0 or not self._img_label:
            return
        pixmap = self._original_pixmap
        if not pixmap or pixmap.isNull():
            self._sync_card_height()
            return
        inner_w = max(card_w - self.MARGIN_LR * 2, 50)
        pw, ph = pixmap.width(), pixmap.height()
        if pw <= 0 or ph <= 0:
            return
        target_w = min(pw, inner_w, self.MAX_IMAGE_WIDTH)
        if pw > target_w:
            display = pixmap.scaledToWidth(target_w, Qt.TransformationMode.SmoothTransformation)
        else:
            display = pixmap
        self._img_label.setPixmap(display)
        self._img_label.setFixedHeight(display.height())
        # 以实际缩放结果为准重新计算，避免分割线进入图片底部。
        self._sync_card_height()


class PDFViewerPanel(QWidget):
    """PDF 阅读器主面板 v2 —— 两阶段管线展示。"""

    pdf_loaded = Signal(str)
    pdf_path_changed = Signal(str)
    follow_up_question = Signal(str, str)  # (question, image_path 或 "")
    structured_document_updated = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("readerPanel")
        self._current_path: str = ""
        self._text_client: LLMClient | None = None
        self._processor: PDFProcessor | None = None
        self._structured_doc: StructuredDocument | None = None
        self._cards: list[ParagraphCard | ImageCard] = []
        self._trans_workers: dict[int, TranslationWorker] = {}
        self._retired_trans_workers: list[TranslationWorker] = []
        self._doc_generation: int = 0  # 每次重置视图时自增，过期结果/翻译直接丢弃
        self._auto_translate: bool = False
        self._stage1_complete: bool = False
        self._stage1_errors: int = 0
        self._pending_integrate: bool = False
        self._stage2_timer: QTimer | None = None
        self._stage2_start_time: float = 0.0
        self._setup_ui()

    def set_text_client(self, client: "LLMClient | None"):
        """设置纯文本接口客户端（跨页接缝合并与段落翻译共用）。"""
        self._text_client = client
        # 兼容旧状态：如果此前等待过整合，配置变更后继续触发。
        if (self._pending_integrate and self._stage1_complete
                and self._current_path and self._structured_doc is None
                ):
            self._auto_start_stage2()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 8, 12, 8)
        title = QLabel("结构化阅读")
        title.setObjectName("titleLabel")
        toolbar.addWidget(title)
        toolbar.addStretch()

        self.auto_trans_btn = QPushButton("自动翻译：关")
        self.auto_trans_btn.setObjectName("secondaryBtn")
        self.auto_trans_btn.setToolTip("开启后，滚动到可见区域的英文段落将自动翻译")
        self.auto_trans_btn.clicked.connect(self._on_toggle_auto_translate)
        self.auto_trans_btn.setEnabled(False)
        toolbar.addWidget(self.auto_trans_btn)

        layout.addLayout(toolbar)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px;")
        layout.addWidget(sep)

        info = QHBoxLayout()
        info.setContentsMargins(12, 4, 12, 4)
        self.info_label = QLabel("尚未加载 PDF — 从左侧文献库选择 PDF 文件")
        self.info_label.setObjectName("subtitleLabel")
        info.addWidget(self.info_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setMaximumHeight(16)
        info.addWidget(self.progress_bar)
        info.addStretch()
        layout.addLayout(info)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.container = QWidget()
        self.container.setMinimumWidth(0)
        self.container.setObjectName("readerContent")
        self.container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.card_layout = QVBoxLayout(self.container)
        self.card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.card_layout.setSpacing(0)
        self.card_layout.setContentsMargins(0, 10, 0, 20)
        self.placeholder = QLabel(PLACEHOLDER_TEXT)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet(
            "color: #718180; background-color: #f7faf8; border: 1px dashed #c9ddd7; "
            "border-radius: 14px; padding: 70px 40px; font-size: 14px;"
        )
        self.card_layout.addWidget(self.placeholder)
        self.scroll_area.setWidget(self.container)
        layout.addWidget(self.scroll_area, 1)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def get_current_path(self) -> str:
        return self._current_path

    def load_pdf(self, file_path: str, existing_processor: "PDFProcessor | None" = None):
        """加载 PDF —— 检查缓存 → Stage1 → Stage2。

        Args:
            existing_processor: 该文献已在后台处理中的处理器（复用，不重复启动）。
        """
        self._detach_processor()
        self._reset_view()
        self._current_path = file_path

        # ---- 先查 Stage 2 整合缓存 ----
        from ..utils.config import load_doc_state
        cached_state = load_doc_state(file_path)
        cached_doc = cached_state.get("structured_document")
        from ..core.pdf_processor import FAST_DOCUMENT_VERSION
        cache_is_current = (
            cached_state.get("doc_format") == "fast"
            and cached_state.get("fast_version", 0) == FAST_DOCUMENT_VERSION
        )
        # PDF 被同名替换（mtime 变化）后缓存必须失效，否则永远显示旧解析
        try:
            built_mtime = float(cached_state.get("pdf_mtime", 0.0) or 0.0)
            cur_mtime = os.path.getmtime(file_path)
        except (OSError, TypeError, ValueError):
            built_mtime = cur_mtime = 0.0
        if built_mtime and cur_mtime and abs(built_mtime - cur_mtime) > 1.0:
            cache_is_current = False

        if cached_doc and cache_is_current:
            try:
                from ..core.pdf_processor import StructuredDocument
                doc = StructuredDocument.from_dict(cached_doc)
                self._structured_doc = doc
                failed = self._render_document(doc)
                self._restore_translation_state(cached_state)
                self._restore_document_preferences()
                title_msg = f"📖 {doc.title or '论文'} — 从缓存加载"
                if failed:
                    title_msg += f"（{failed} 个元素渲染失败已跳过）"
                self.info_label.setText(title_msg)
                self.info_label.setStyleSheet("color: #278273;")
                self.progress_bar.setVisible(False)
                self.progress_bar.setValue(100)
                self.auto_trans_btn.setEnabled(True)
                self._processor = existing_processor  # 保留引用，后台任务继续
                if existing_processor is not None:
                    # 后台接缝合并仍在跑时，完成信号也要能刷新当前视图
                    self._attach_processor_signals()
                self.pdf_path_changed.emit(file_path)
                full_text = "\n\n".join(e.text for e in doc.display_elements if e.text)
                self.pdf_loaded.emit(full_text)
                # 后台建库的初步整合结果：配置了纯文本接口时后台做一次接缝精修
                if cached_state.get("seams_final", True) is False \
                        and self._text_client is not None:
                    self._refine_prelim_seams()
                return
            except Exception:
                pass  # 缓存损坏，走正常流程

        self.info_label.setText("正在初始化 PDF 解析器...")
        self.info_label.setStyleSheet("color: #a76d2b;")

        try:
            from ..core.pdf_processor import PDFProcessor
            if existing_processor is not None:
                # 后台任务复用：仅接管信号，不重复创建处理器
                self._processor = existing_processor
            else:
                self._processor = PDFProcessor(file_path, self._text_client)
            self._attach_processor_signals()
            manifest = self._processor.manifest

            if manifest and manifest.is_complete:
                done = manifest.done_count
                total = manifest.total_pages
                self.info_label.setText(f"已有 {done}/{total} 页本地解析缓存，正在自动整合...")
                self.info_label.setStyleSheet("color: #278273;")
                self.progress_bar.setVisible(True)
                self.progress_bar.setValue(100)
                self._stage1_complete = True
                self.pdf_path_changed.emit(file_path)
                if existing_processor is not None and existing_processor.is_stage2_running:
                    # 后台整合仍在进行，完成时自动渲染
                    self.info_label.setText("该文献正在后台整合，完成时自动显示结果...")
                    return
                self._auto_start_stage2()
                return

            self._stage1_complete = False
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)
            if not self._processor.is_stage1_running:
                self._processor.start_stage1()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.info_label.setText(f"❌ 初始化失败：{e}")
            self.info_label.setStyleSheet("color: #b24f4a;")

    def _attach_processor_signals(self):
        """连接当前处理器信号到本面板。"""
        if self._processor is None:
            return
        self._processor.stage1_progress.connect(self._on_stage1_progress)
        self._processor.stage1_status.connect(self._on_stage1_status)
        self._processor.stage1_complete.connect(self._on_stage1_complete)
        self._processor.stage1_error.connect(self._on_stage1_error)
        self._processor.stage2_finished.connect(self._on_stage2_finished)
        self._processor.stage2_error.connect(self._on_stage2_error)
        self._processor.stage2_merged.connect(self._on_stage2_merged)

    def _on_stage1_progress(self, pdf_path: str, current: int, total: int):
        if pdf_path != self._current_path:
            return  # 后台其它论文的进度不污染当前界面
        if self._structured_doc is not None:
            return  # 文档已渲染完成：丢弃迟到的 stage1 进度，避免覆盖结果标题
        pct = int(current / max(total, 1) * 100)
        self.progress_bar.setValue(pct)
        self.info_label.setText(f"正在完成本地解析：第 {current}/{total} 页...")
        self.info_label.setStyleSheet("color: #a76d2b;")

    def _on_stage1_status(self, pdf_path: str, message: str):
        """展示 Docling 初始化阶段，避免首次加载时看起来像程序卡死。"""
        if pdf_path != self._current_path:
            return
        if self._structured_doc is not None:
            return  # 文档已渲染完成：丢弃迟到状态
        self.info_label.setText(message)
        self.info_label.setStyleSheet("color: #a76d2b;")

    def _on_stage1_complete(self, pdf_path: str):
        if pdf_path != self._current_path:
            return
        if self._structured_doc is not None:
            return  # 已构建完成：忽略重复/迟到的 stage1 完成事件
        self._stage1_complete = True
        self.progress_bar.setValue(100)
        manifest = self._processor.manifest if self._processor else None
        total = manifest.total_pages if manifest else 0
        errors = manifest.error_count if manifest else 0
        msg = f"本地解析完成：{manifest.done_count if manifest else 0}/{total} 页"
        if errors > 0:
            msg += f"（{errors} 页失败）"
        msg += "，正在构建结构化文档..."
        self.info_label.setText(msg)
        self.info_label.setStyleSheet("color: #278273;")
        self.pdf_path_changed.emit(pdf_path)
        self._auto_start_stage2()

    def _on_stage1_error(self, pdf_path: str, page_num: int, error_msg: str):
        if pdf_path != self._current_path:
            return  # 后台其它论文的失败不污染当前界面
        if self._structured_doc is not None:
            return  # 文档已渲染完成：丢弃迟到的失败事件
        self._stage1_errors += 1
        if page_num > 0:
            message = f"第 {page_num} 页解析失败：{error_msg[:80]}"
        else:
            message = f"本地解析失败：{error_msg[:120]}"
        self.info_label.setText(message)
        self.info_label.setStyleSheet("color: #b24f4a;")
        self.progress_bar.setVisible(True)
        self.progress_bar.setToolTip(
            f"已有 {self._stage1_errors} 页解析失败，可在论文库右键菜单选择「重新逐页解析」重试"
        )

    def _auto_start_stage2(self):
        """Stage 1 完成后自动启动跨页整合（无需手动点按）。

        幂等：文档已渲染、或 stage2 正在进行、或等待定时器已存在时直接返回，
        避免重复启动 stage2 与多个定时器叠加导致的「解析/构建」来回跳。
        """
        if not self._processor:
            return
        if self._structured_doc is not None:
            return
        # 处理器必须属于当前文献：切换文献瞬间 self._processor 仍是旧处理器，
        # 此时补触发 stage2 会用旧处理器构建旧文档，造成「解析/构建」来回跳。
        if getattr(self._processor, "_pdf_path", "") != self._current_path:
            return
        if self._processor.is_stage2_running:
            return
        if self._stage2_timer is not None:
            return  # 已有整合等待定时器在跑（stage2 尚未结束），不重复启动
        self._pending_integrate = False
        self.info_label.setText("正在构建结构化文档...")
        self.info_label.setStyleSheet("color: #a76d2b;")
        self._stage2_start_time = time.monotonic()
        self._stage2_timer = QTimer(self)
        self._stage2_timer.setInterval(5000)
        self._stage2_timer.timeout.connect(self._update_stage2_waiting)
        self._stage2_timer.start()
        self._processor.start_stage2()

    def _update_stage2_waiting(self):
        """整合期间定时刷新等待提示，避免看起来像卡死。"""
        if not self._processor or not self._processor.is_stage2_running:
            self._stop_stage2_timer()
            return
        elapsed = int(time.monotonic() - self._stage2_start_time)
        self.info_label.setText(
            f"正在后台合并跨页段落...已等待 {elapsed} 秒"
        )
        self.info_label.setStyleSheet("color: #a76d2b;")

    def _stop_stage2_timer(self):
        if getattr(self, "_stage2_timer", None) is not None:
            self._stage2_timer.stop()
            self._stage2_timer.deleteLater()
            self._stage2_timer = None

    def _on_stage2_finished(self, pdf_path: str, doc: "StructuredDocument"):
        if pdf_path != self._current_path:
            return  # 后台文献整合完成，结果已由处理器落盘
        try:
            self._stop_stage2_timer()
            self._structured_doc = doc
            self.progress_bar.setVisible(False)
            failed = self._render_document(doc)

            self._restore_translation_state()
            self._restore_document_preferences()

            title_msg = f"📖 {doc.title or '论文'} — {len(doc.display_elements)} 个元素"
            if failed:
                title_msg += f"（{failed} 个元素渲染失败已跳过）"
            self.info_label.setText(title_msg)
            self.info_label.setStyleSheet("color: #278273;")
            self.auto_trans_btn.setEnabled(True)

            full_text = "\n\n".join(e.text for e in doc.display_elements if e.text)
            self.pdf_loaded.emit(full_text)
            # 接手后台建库的在途处理器（或缓存秒开）后，初步整合文档仍需
            # 一次 LLM 接缝精修；延迟到当前调用栈外，让初步结果先落稳
            if (self._processor is not None
                    and self._processor.seams_mode == "prelim"
                    and self._text_client is not None):
                self.info_label.setText("已加载，正在后台精修跨页段落...")
                self.info_label.setStyleSheet("color: #a76d2b;")
                QTimer.singleShot(0, self._refine_prelim_seams)
        except Exception:
            import traceback
            traceback.print_exc()
            self._stop_stage2_timer()
            self.info_label.setText("❌ 渲染整合结果失败，可右键「重新解析整合」重试")
            self.info_label.setStyleSheet("color: #b24f4a;")

    def _refine_prelim_seams(self) -> None:
        """后台建库的初步整合文档 → 打开时用 LLM 精修跨页接缝并定稿。

        复用 start_stage2 的 final 路径：定稿接缝送 LLM 复核（初步规则
        版作为候选一并重评），完成后重建文档、刷新视图并置 seams_final。
        """
        if not self._current_path or self._text_client is None:
            return
        proc = self._processor
        if proc is None:
            # 缓存秒开路径没有在途处理器：为精修补建一个（读完缓存即闲）
            try:
                from ..core.pdf_processor import PDFProcessor
                proc = PDFProcessor(self._current_path, self._text_client)
            except Exception:  # noqa: BLE001
                return
            self._processor = proc
            self._attach_processor_signals()
        if getattr(proc, "_pdf_path", "") != self._current_path:
            return
        if proc.is_busy or proc.seams_mode not in ("", "prelim"):
            return  # 在途或已定稿
        proc.set_llm_client(self._text_client)
        self.info_label.setText("正在后台精修跨页段落...")
        self.info_label.setStyleSheet("color: #a76d2b;")
        proc.start_stage2()

    def _on_stage2_error(self, pdf_path: str, error_msg: str):
        if pdf_path != self._current_path:
            return
        self._stop_stage2_timer()
        self._pending_integrate = False
        self.info_label.setText(
            f"⚠️ 整合失败：{error_msg}（可在左侧文献列表右键 →「重新解析整合」重试）"
        )
        self.info_label.setStyleSheet("color: #b24f4a;")

    def _reset_view(self):
        self._structured_doc = None
        self._stage1_complete = False
        self._stage1_errors = 0
        self._pending_integrate = False
        self._stop_stage2_timer()
        self._doc_generation += 1  # 使旧线程的迟到结果（翻译/整合）全部失效
        for worker in self._trans_workers.values():
            if worker.isRunning():
                worker.requestInterruption()  # 不再 terminate：运行中线程等待自然退出
            self._retired_trans_workers.append(worker)
        self._trans_workers.clear()
        # 清扫已自然退出的退休翻译线程（保留引用直到真正退出，防 GC 崩溃）
        self._retired_trans_workers = [
            w for w in self._retired_trans_workers if w.isRunning()
        ]
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self.placeholder.setVisible(True)
        self.progress_bar.setVisible(False)
        self.progress_bar.setValue(0)
        self.auto_trans_btn.setEnabled(False)
        self._auto_translate = False
        self.auto_trans_btn.setText("自动翻译：关")

    def _render_document(self, doc: "StructuredDocument") -> int:
        """渲染结构化文档为卡片流。返回渲染失败（跳过）的元素个数。"""
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        # 清掉上次渲染追加的 spacer（widget 不动），避免接缝合并等
        # 重复渲染场景下布局项无限累积
        for i in range(self.card_layout.count() - 1, -1, -1):
            item = self.card_layout.itemAt(i)
            if item is not None and item.widget() is None:
                self.card_layout.takeAt(i)
        self.placeholder.setVisible(False)

        from ..utils.config import get_page_cache_dir
        image_base_dir = str(get_page_cache_dir(self._current_path))

        total = len(doc.display_elements)
        failed = 0
        for i, elem in enumerate(doc.display_elements):
            try:
                if elem.element_type in ("figure", "table"):
                    elem.image_path = self._resolve_image_path(elem, image_base_dir)
                    caption_text = (elem.image_caption or "").strip()
                    image_elem = copy(elem)
                    image_elem.image_caption = ""
                    card = ImageCard(image_elem, parent=self.container)
                    card.qa_requested.connect(self._on_card_qa)
                    self._cards.append(card)
                    self.card_layout.addWidget(card)

                    if caption_text:
                        caption_elem = copy(elem)
                        caption_elem.element_type = (
                            "figure_caption" if elem.element_type == "figure"
                            else "table_caption"
                        )
                        caption_elem.text = caption_text
                        caption_elem.image_path = ""
                        caption_elem.image_caption = ""
                        caption_elem.element_id = f"{elem.element_id}:caption"
                        caption_card = ParagraphCard(
                            caption_elem, i, parent=self.container,
                        )
                        caption_card.translate_requested.connect(
                            self._on_translate_request
                        )
                        caption_card.qa_requested.connect(self._on_card_qa)
                        self._cards.append(caption_card)
                        self.card_layout.addWidget(caption_card)
                    continue
                elif elem.element_type in ("header_footer", "publisher_logo"):
                    continue
                else:
                    card = ParagraphCard(elem, i, parent=self.container)
                    card.translate_requested.connect(self._on_translate_request)
                card.qa_requested.connect(self._on_card_qa)
                self._cards.append(card)
                self.card_layout.addWidget(card)
            except Exception:
                failed += 1
                import traceback
                traceback.print_exc()
        self.card_layout.addStretch()
        return failed

    def _on_card_qa(self, element_id: str, question: str, image_path: str):
        """卡片问答：转发到主窗口，附带图片路径（视觉模型解读图片）。"""
        if not question.strip():
            return
        self.follow_up_question.emit(question, image_path or "")

    def _on_stage2_merged(self, pdf_path: str, doc: "StructuredDocument"):
        """跨页接缝合并完成 → 用合并后的文档刷新视图。"""
        if pdf_path != self._current_path:
            return
        self._structured_doc = doc
        # 合并会移位后续元素的索引：先作废所有在途翻译（按旧索引回调
        # 会把 A 段译文写到 B 段卡片并错误持久化），再重建卡片
        self._doc_generation += 1
        for worker in self._trans_workers.values():
            if worker.isRunning():
                worker.requestInterruption()
            self._retired_trans_workers.append(worker)
        self._trans_workers.clear()
        self._retired_trans_workers = [
            w for w in self._retired_trans_workers if w.isRunning()
        ]
        self._render_document(doc)
        self._restore_translation_state()
        self.structured_document_updated.emit(pdf_path)

    @staticmethod
    def _translation_key(card: ParagraphCard) -> str:
        element_id = getattr(card._elem, "element_id", "")
        return element_id or f"index:{card._index}"

    def _restore_translation_state(self, state: dict | None = None) -> None:
        """恢复当前文献已经完成的段落翻译。"""
        if state is None:
            from ..utils.config import load_doc_state
            state = load_doc_state(self._current_path)
        translations = state.get("translations", {}) if isinstance(state, dict) else {}
        if not isinstance(translations, dict):
            return
        for card in self._cards:
            if not isinstance(card, ParagraphCard):
                continue
            zh = translations.get(self._translation_key(card))
            if zh:
                card.show_translation(str(zh))

    def _restore_document_preferences(self, state: dict | None = None) -> None:
        """恢复当前文献的阅读偏好。"""
        if state is None:
            from ..utils.config import load_doc_state
            state = load_doc_state(self._current_path)
        auto_translate = bool(state.get("auto_translate", False)) if isinstance(state, dict) else False
        self._auto_translate = auto_translate
        self.auto_trans_btn.setText(f"自动翻译：{'开' if auto_translate else '关'}")

    def _resolve_image_path(self, elem, image_base_dir: str) -> str:
        """解析图表截图路径：优先元素自带路径，否则按 element_id 重建（兼容旧缓存）。

        裁剪文件名格式: page_{page:03d}_{element_id}.png。
        """
        if elem.image_path:
            p = elem.image_path
            if not os.path.isabs(p) and image_base_dir:
                p = os.path.join(image_base_dir, p)
            if os.path.exists(p):
                return p
        if elem.element_id and image_base_dir:
            p = os.path.join(
                image_base_dir, f"page_{elem.page:03d}_{elem.element_id}.png"
            )
            if os.path.exists(p):
                return p
        return ""

    def _on_translate_request(self, idx: int, text: str):
        if not self._text_client:
            return
        worker = self._trans_workers.get(idx)
        if worker and worker.isRunning():
            return
        worker = TranslationWorker(self._text_client, idx, text)
        worker._gen = self._doc_generation
        track(worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        worker.finished.connect(self._on_translation_done)
        worker.error.connect(self._on_translation_error)
        self._trans_workers[idx] = worker
        worker.start()

    def _on_translation_done(self, idx: int, zh: str):
        worker = self._trans_workers.pop(idx, None)
        if worker is None or worker._gen != self._doc_generation:
            return  # 过期结果（已切换/重置视图）直接丢弃
        for card in self._cards:
            if hasattr(card, '_index') and card._index == idx and isinstance(card, ParagraphCard):
                card.show_translation(zh)
                from ..utils.config import load_doc_state, save_doc_state
                state = load_doc_state(self._current_path)
                translations = state.setdefault("translations", {})
                if isinstance(translations, dict):
                    translations[self._translation_key(card)] = zh
                save_doc_state(self._current_path, state)
                break

    def _on_translation_error(self, idx: int, err: str):
        worker = self._trans_workers.pop(idx, None)
        if worker is None or worker._gen != self._doc_generation:
            return  # 过期结果（已切换/重置视图）直接丢弃
        for card in self._cards:
            if hasattr(card, '_index') and card._index == idx and isinstance(card, ParagraphCard):
                card.show_translation_error(err)
                break

    def _on_toggle_auto_translate(self):
        self._auto_translate = not self._auto_translate
        self.auto_trans_btn.setText(f"自动翻译：{'开' if self._auto_translate else '关'}")
        from ..utils.config import load_doc_state, save_doc_state
        state = load_doc_state(self._current_path)
        state["auto_translate"] = self._auto_translate
        save_doc_state(self._current_path, state)
        if self._auto_translate:
            self._on_scroll()

    def _on_scroll(self):
        if not self._auto_translate or not self._text_client:
            return
        viewport = self.scroll_area.viewport()
        view_top = self.scroll_area.verticalScrollBar().value()
        view_bottom = view_top + viewport.height()
        for card in self._cards:
            if not isinstance(card, ParagraphCard):
                continue
            if not card._is_english or card._translated:
                continue
            if not hasattr(card, 'trans_btn') or card.trans_btn is None or not card.trans_btn.isVisible():
                continue
            if card._index in self._trans_workers:
                continue
            pos = card.mapTo(viewport, QPoint(0, 0))
            card_top = pos.y()
            card_bottom = card_top + card.height()
            if card_bottom > view_top and card_top < view_bottom:
                card._request_translate()

    @property
    def structured_document(self) -> "StructuredDocument | None":
        return self._structured_doc

    # ---- 生命周期 ----

    def clear_pdf(self) -> None:
        """清空当前文献并断开处理器，避免删除文件后迟到信号继续更新界面。"""
        self._detach_processor()
        self._reset_view()
        self._current_path = ""
        self.pdf_path_changed.emit("")

    def _detach_processor(self):
        """切换文献时只断开与旧处理器的信号连接，不取消后台任务。

        后台解析/整合继续运行（结果自动落盘），用户随时可切回该文献。
        """
        if self._processor is None:
            return
        proc = self._processor
        try:
            import warnings
            with warnings.catch_warnings():
                # PySide6 对无连接的 signal.disconnect() 会发一条无害 RuntimeWarning
                warnings.simplefilter("ignore", RuntimeWarning)
                for sig in (proc.stage1_progress, proc.stage1_status, proc.stage1_complete,
                            proc.stage1_error, proc.stage2_finished, proc.stage2_error,
                            proc.stage2_merged):
                    sig.disconnect()
        except (RuntimeError, TypeError, AttributeError):
            pass
        self._processor = None
