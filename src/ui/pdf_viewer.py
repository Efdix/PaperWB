"""PDF 阅读器面板 v2 —— 两阶段管线 + 结构化渲染。"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from PySide6.QtWidgets import (
    QWidget, QVBoxLayout, QHBoxLayout, QScrollArea, QPushButton,
    QLabel, QFrame, QFileDialog, QApplication, QProgressBar,
    QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QThread, QSize, QPoint
from PySide6.QtGui import QFont, QPixmap

from ..utils.layout import calc_layout_height

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.pdf_processor import (
        StructuredElement, StructuredDocument, PDFProcessor,
    )

PLACEHOLDER_TEXT = (
    "📄 从左侧论文库选择或拖拽 PDF 开始阅读\n\n"
    "• 导入后自动 AI 解析论文结构（逐页分析）\n"
    "• 解析完成后点击论文查看结构化阅读视图\n"
    "• 重要图片和表格自动截图展示\n"
    "• 英文段落一键翻译为中文"
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


class ParagraphCard(QFrame):
    """结构化段落卡片 —— 仅对结构标签词句做视觉区分。"""
    translate_requested = Signal(int, str)

    def __init__(self, elem: "StructuredElement", index: int, parent=None):
        super().__init__(parent)
        self._index = index
        self._elem = elem
        self._text = elem.text
        self._is_english = self._detect_en(self._text)
        self._translated = False
        self._trans_text = ""
        self._setup_ui()

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        marg = self.contentsMargins()
        inner_w = max(w - marg.left() - marg.right(), 50)
        lay = self.layout()
        if lay is None:
            return 40
        h = marg.top() + marg.bottom() + calc_layout_height(lay, inner_w)
        return max(h, 40)

    def sizeHint(self):
        base = super().sizeHint()
        return QSize(base.width(), self.heightForWidth(base.width()))

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
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setMinimumWidth(0)

        etype = self._elem.element_type
        priority = self._elem.display_priority
        level = self._elem.heading_level
        is_key = self._is_key_section()

        if priority == "collapsed":
            self.setVisible(False)
            self._make_card_base("#1a1b26", "#2a2c3d")
            layout = QVBoxLayout(self)
            layout.setContentsMargins(0, 0, 0, 0)
            self.text_label = QLabel()
            self.text_label.setVisible(False)
            layout.addWidget(self.text_label)
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

    def _make_card_base(self, bg: str = "#1a1b26", border: str = "#2a2c3d"):
        self.setStyleSheet(
            f"ParagraphCard {{ background-color: {bg}; border: 1px solid {border}; "
            f"border-radius: 10px; margin: 4px 8px; }}"
        )

    def _setup_title_card(self):
        self._make_card_base("#1a1b26", "#7aa2f7")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 16)
        layout.setSpacing(8)
        f = QFont("Microsoft YaHei UI", 20)
        f.setBold(True)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet("color: #7aa2f7; padding: 4px 0; letter-spacing: 0.5px;")
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

    def _setup_key_subtitle_card(self, level: int):
        """关键章节标题（如 Introduction、Results）—— 醒目的暖金色。"""
        self._make_card_base("#1a1b26", "#e0af68")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 16, 20, 10)
        layout.setSpacing(6)
        sizes = {1: 18, 2: 16, 3: 14}
        f = QFont("Microsoft YaHei UI", sizes.get(level, 16))
        f.setBold(True)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet(
            "color: #e0af68; padding: 6px 0; "
            "border-left: 4px solid #e0af68; padding-left: 14px;"
        )
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

    def _setup_subtitle_card(self, level: int):
        """普通小节标题。"""
        self._make_card_base("#1a1b26", "#3b3d54")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 8)
        layout.setSpacing(4)
        sizes = {1: 16, 2: 14, 3: 13}
        colors = {1: "#bb9af7", 2: "#9ece6a", 3: "#8a8ea6"}
        f = QFont("Microsoft YaHei UI", sizes.get(level, 14))
        f.setBold(True)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet(
            f"color: {colors.get(level, '#a9b1d6')}; padding: 3px 0; "
            f"border-left: 3px solid {colors.get(level, '#3b3d54')}; padding-left: 10px;"
        )
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

    def _setup_key_abstract_heading_card(self):
        """关键摘要标签 —— 暖金色突出。"""
        self._make_card_base("#1e2035", "#e0af68")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 6)
        header = QLabel("📝 摘要")
        header.setStyleSheet("color: #e0af68; font-size: 16px; font-weight: bold;")
        layout.addWidget(header)
        self.text_label = QLabel("")
        layout.addWidget(self.text_label)

    def _setup_abstract_heading_card(self):
        self._make_card_base("#1e2035", "#3b3d54")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 6)
        header = QLabel("📝 摘要")
        header.setStyleSheet("color: #bb9af7; font-size: 14px; font-weight: bold;")
        layout.addWidget(header)
        self.text_label = QLabel("")
        layout.addWidget(self.text_label)

    def _setup_meta_card(self):
        self._make_card_base("#1e2030", "#2a2c3d")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 8, 20, 8)
        layout.setSpacing(4)
        labels = {"authors": "👤 作者信息", "affiliations": "🏛️ 作者单位", "metadata": "📋 出版信息"}
        header = QLabel(labels.get(self._elem.element_type, "📋 信息"))
        header.setStyleSheet("color: #565a7a; font-size: 10px; font-weight: bold;")
        layout.addWidget(header)
        f = QFont("Microsoft YaHei UI", 11)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet("color: #636688; line-height: 1.5; padding: 2px 0;")
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

    def _setup_abstract_card(self):
        self._make_card_base("#1e2035", "#3b3d54")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(6)
        f = QFont("Microsoft YaHei UI", 13)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet(
            "color: #cfd2e3; line-height: 1.9; padding: 4px 0; "
            "border-left: 3px solid #bb9af7; padding-left: 12px;"
        )
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

    def _setup_special_card(self, etype: str):
        self._make_card_base("#1a1b26", "#2a2c3d")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 10, 20, 10)
        layout.setSpacing(6)
        labels = {"keywords": "🔑 关键词", "acknowledgment": "🙏 致谢", "appendix": "📎 附录"}
        header = QLabel(labels.get(etype, ""))
        header.setStyleSheet("color: #8a8ea6; font-size: 12px; font-weight: bold;")
        layout.addWidget(header)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(QFont("Microsoft YaHei UI", 12))
        self.text_label.setStyleSheet("color: #a9b1d6; line-height: 1.7; padding: 4px 0;")
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

    def _setup_caption_card(self):
        self._make_card_base("#1a1b26", "#2a2c3d")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 6, 20, 6)
        layout.setSpacing(4)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(QFont("Microsoft YaHei UI", 11))
        self.text_label.setStyleSheet("color: #8a8ea6; line-height: 1.5; padding: 2px 0; font-style: italic;")
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

    def _setup_reference_card(self):
        self._make_card_base("#1a1b26", "#252740")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 6, 20, 6)
        layout.setSpacing(2)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(QFont("Microsoft YaHei UI", 10))
        self.text_label.setStyleSheet("color: #565a7a; line-height: 1.4; padding: 2px 0;")
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

    def _setup_body_card(self):
        """正文段落 —— 保持原样，不做特殊区分。"""
        self._make_card_base("#1a1b26", "#2a2c3d")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 12, 20, 12)
        layout.setSpacing(8)
        f = QFont("Segoe UI" if self._is_english else "Microsoft YaHei UI", 13)
        if self._is_english:
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 0.3)
        self.text_label = QLabel(self._text)
        self.text_label.setFont(f)
        self.text_label.setStyleSheet("color: #cfd2e3; line-height: 1.9; padding: 4px 0; background-color: transparent;")
        self.text_label.setWordWrap(True)
        self.text_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(self.text_label)

        if len(self._text.strip()) > 20:
            sep = QFrame()
            sep.setFrameShape(QFrame.Shape.HLine)
            sep.setStyleSheet("background-color: #2a2c3d; max-height: 1px;")
            layout.addWidget(sep)
            self.zh_label = QLabel()
            self.zh_label.setWordWrap(True)
            self.zh_label.setFont(QFont("Microsoft YaHei UI", 12))
            self.zh_label.setStyleSheet("color: #9ece6a; line-height: 1.7; padding: 4px 0;")
            self.zh_label.setVisible(False)
            self.zh_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(self.zh_label)

            btn_row = QHBoxLayout()
            self.trans_btn = QPushButton("🌐 翻译")
            self.trans_btn.setFixedWidth(90)
            self.trans_btn.clicked.connect(self._request_translate)
            btn_row.addWidget(self.trans_btn)
            self.re_trans_btn = QPushButton("🔄 重新翻译")
            self.re_trans_btn.setFixedWidth(100)
            self.re_trans_btn.clicked.connect(self._on_re_translate)
            self.re_trans_btn.setVisible(False)
            self.re_trans_btn.setStyleSheet(
                "QPushButton { background-color: #2a2c3d; color: #e0af68; border: 1px solid #3b3d54; "
                "border-radius: 4px; padding: 4px 10px; font-size: 12px; }"
                "QPushButton:hover { background-color: #3b3d54; }"
            )
            btn_row.addWidget(self.re_trans_btn)
            btn_row.addStretch()
            layout.addLayout(btn_row)

    def _request_translate(self):
        if not self._translated and hasattr(self, 'trans_btn'):
            self.trans_btn.setText("⏳")
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

    def show_translation_error(self, err: str):
        if hasattr(self, 'trans_btn'):
            self.trans_btn.setText("❌ 失败")
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

    @property
    def is_body(self) -> bool:
        return self._elem.element_type in ("body", "abstract_body")


class ImageCard(QFrame):
    """图片/表格卡片。"""

    MAX_IMAGE_WIDTH = 560
    MARGIN_LR = 16
    MARGIN_TB = 12

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
        self._setup_ui()

    def _setup_ui(self):
        etype = self._elem.element_type
        icon = "🖼️" if etype == "figure" else "📊"
        label_text = f"{icon} 第 {self._page} 页{'插图' if etype == 'figure' else '表格'}"
        self.setStyleSheet(
            "ImageCard { background-color: #1a1b26; border: 1px solid #2a2c3d; "
            "border-radius: 10px; margin: 6px 12px; }"
        )
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(self.MARGIN_LR, self.MARGIN_TB, self.MARGIN_LR, self.MARGIN_TB)
        layout.setSpacing(8)
        page_label = QLabel(label_text)
        page_label.setStyleSheet("color: #9599b5; font-size: 11px;")
        page_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(page_label)
        self._img_label = QLabel()
        self._img_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        layout.addWidget(self._img_label)
        if self._caption:
            cap = QLabel(self._caption)
            cap.setWordWrap(True)
            cap.setFont(QFont("Microsoft YaHei UI", 11))
            cap.setStyleSheet("color: #8a8ea6; font-style: italic; padding: 4px 0;")
            cap.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(cap)
        if self._description:
            desc = QLabel(f"💡 {self._description}")
            desc.setWordWrap(True)
            desc.setFont(QFont("Microsoft YaHei UI", 11))
            desc.setStyleSheet("color: #7aa2f7; padding: 4px 0;")
            desc.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            layout.addWidget(desc)
        self._load_pixmap()

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
        self.setFixedHeight(self._calc_total_height(card_w))

    def _calc_total_height(self, card_w: int) -> int:
        h = self.MARGIN_TB * 2 + 30
        h += self._img_label.height() if self._img_label else 0
        if self._caption:
            h += 40
        if self._description:
            h += 40
        return max(h, 80)


class PDFViewerPanel(QWidget):
    """PDF 阅读器主面板 v2 —— 两阶段管线展示。"""

    pdf_loaded = Signal(str)
    pdf_path_changed = Signal(str)
    follow_up_question = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._current_path: str = ""
        self._parse_client: LLMClient | None = None
        self._translate_client: LLMClient | None = None
        self._processor: PDFProcessor | None = None
        self._structured_doc: StructuredDocument | None = None
        self._cards: list[ParagraphCard | ImageCard] = []
        self._trans_workers: dict[int, TranslationWorker] = {}
        self._auto_translate: bool = False
        self._stage1_complete: bool = False
        self._stage1_errors: int = 0
        self._setup_ui()

    def set_parse_client(self, client: "LLMClient | None"):
        self._parse_client = client

    def set_translate_client(self, client: "LLMClient | None"):
        self._translate_client = client

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        toolbar = QHBoxLayout()
        toolbar.setContentsMargins(12, 8, 12, 8)
        title = QLabel("📖 阅读")
        title.setObjectName("titleLabel")
        toolbar.addWidget(title)
        toolbar.addStretch()

        self.auto_trans_btn = QPushButton("🔄 自动翻译：关")
        self.auto_trans_btn.setToolTip("开启后，滚动到可见区域的英文段落将自动翻译")
        self.auto_trans_btn.clicked.connect(self._on_toggle_auto_translate)
        self.auto_trans_btn.setEnabled(False)
        toolbar.addWidget(self.auto_trans_btn)

        self.integrate_btn = QPushButton("🧠 AI 整合")
        self.integrate_btn.setToolTip("分析完成后点击此处将各页结果整合为结构化文档")
        self.integrate_btn.clicked.connect(self._on_request_integrate)
        self.integrate_btn.setEnabled(False)
        self.integrate_btn.setVisible(False)
        toolbar.addWidget(self.integrate_btn)

        layout.addLayout(toolbar)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #2a2c3d; max-height: 1px;")
        layout.addWidget(sep)

        info = QHBoxLayout()
        info.setContentsMargins(12, 4, 12, 4)
        self.info_label = QLabel("尚未加载 PDF — 从左侧论文库选择或拖拽 PDF 文件")
        self.info_label.setObjectName("subtitleLabel")
        info.addWidget(self.info_label)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.progress_bar.setVisible(False)
        self.progress_bar.setMaximumWidth(200)
        self.progress_bar.setMaximumHeight(16)
        self.progress_bar.setStyleSheet(
            "QProgressBar { background-color: #24253a; border: 1px solid #3b3d54; "
            "border-radius: 7px; }"
            "QProgressBar::chunk { background-color: #7aa2f7; border-radius: 6px; }"
        )
        info.addWidget(self.progress_bar)
        info.addStretch()
        layout.addLayout(info)

        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll_area.setStyleSheet("QScrollArea { border: none; background: #1a1b26; }")
        self.container = QWidget()
        self.container.setMinimumWidth(0)
        self.container.setStyleSheet("background: #1a1b26;")
        self.container.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.card_layout = QVBoxLayout(self.container)
        self.card_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.card_layout.setSpacing(0)
        self.card_layout.setContentsMargins(0, 10, 0, 20)
        self.placeholder = QLabel(PLACEHOLDER_TEXT)
        self.placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.placeholder.setStyleSheet("color: #636688; padding: 80px 40px; font-size: 15px;")
        self.card_layout.addWidget(self.placeholder)
        self.scroll_area.setWidget(self.container)
        layout.addWidget(self.scroll_area, 1)
        self.scroll_area.verticalScrollBar().valueChanged.connect(self._on_scroll)

    def get_current_path(self) -> str:
        return self._current_path

    def _open_pdf(self):
        path, _ = QFileDialog.getOpenFileName(self, "选择 PDF", "", "PDF (*.pdf);;All (*.*)")
        if path:
            self.load_pdf(path)

    def load_pdf(self, file_path: str):
        """加载 PDF —— 检查缓存 → Stage1 → Stage2。"""
        self._teardown_processor()
        self._reset_view()
        self._current_path = file_path

        if not self._parse_client:
            self.info_label.setText("⚠️ 未配置阅读-解析 API")
            self.info_label.setStyleSheet("color: #e0af68;")
            return

        # ---- 先查 Stage 2 整合缓存 ----
        from ..utils.config import load_doc_state
        cached_state = load_doc_state(file_path)
        cached_doc = cached_state.get("structured_document")
        if cached_doc:
            try:
                from ..core.pdf_processor import StructuredDocument
                doc = StructuredDocument.from_dict(cached_doc)
                self._structured_doc = doc
                self._render_document(doc)
                self.info_label.setText(f"📖 {doc.title or '论文'} — 从缓存加载")
                self.info_label.setStyleSheet("color: #9ece6a;")
                self.progress_bar.setVisible(True)
                self.progress_bar.setValue(100)
                self.auto_trans_btn.setEnabled(True)
                self.pdf_path_changed.emit(file_path)
                full_text = "\n\n".join(e.text for e in doc.display_elements if e.text)
                self.pdf_loaded.emit(full_text)
                return
            except Exception:
                pass  # 缓存损坏，走正常流程

        self.info_label.setText("⏳ 初始化 PDF 处理器...")
        self.info_label.setStyleSheet("color: #e0af68;")
        QApplication.processEvents()

        try:
            from ..core.pdf_processor import PDFProcessor
            self._processor = PDFProcessor(file_path, self._parse_client)
            manifest = self._processor.manifest

            if manifest and manifest.is_complete:
                done = manifest.done_count
                total = manifest.total_pages
                self.info_label.setText(f"✅ 已有 {done}/{total} 页缓存，点击「AI 整合」开始阅读")
                self.info_label.setStyleSheet("color: #9ece6a;")
                self.progress_bar.setVisible(True)
                self.progress_bar.setValue(100)
                self._stage1_complete = True
                self.integrate_btn.setVisible(True)
                self.integrate_btn.setEnabled(True)
                self.pdf_path_changed.emit(file_path)
                return

            self._processor.stage1_progress.connect(self._on_stage1_progress)
            self._processor.stage1_complete.connect(self._on_stage1_complete)
            self._processor.stage1_error.connect(self._on_stage1_error)
            self._processor.stage2_finished.connect(self._on_stage2_finished)
            self._processor.stage2_error.connect(self._on_stage2_error)

            self._stage1_complete = False
            self.progress_bar.setVisible(True)
            self.progress_bar.setValue(0)
            self.integrate_btn.setVisible(False)
            self._processor.start_stage1()
        except Exception as e:
            import traceback
            traceback.print_exc()
            self.info_label.setText(f"❌ 初始化失败：{e}")
            self.info_label.setStyleSheet("color: #f7768e;")

    def _on_stage1_progress(self, pdf_path: str, current: int, total: int):
        pct = int(current / max(total, 1) * 100)
        self.progress_bar.setValue(pct)
        self.info_label.setText(f"⏳ AI 分析第 {current}/{total} 页...")
        self.info_label.setStyleSheet("color: #e0af68;")

    def _on_stage1_complete(self, pdf_path: str):
        self._stage1_complete = True
        self.progress_bar.setValue(100)
        manifest = self._processor.manifest if self._processor else None
        total = manifest.total_pages if manifest else 0
        errors = manifest.error_count if manifest else 0
        msg = f"✅ 分析完成：{manifest.done_count if manifest else 0}/{total} 页"
        if errors > 0:
            msg += f"（{errors} 页失败）"
        msg += " — 点击「AI 整合」开始阅读"
        self.info_label.setText(msg)
        self.info_label.setStyleSheet("color: #9ece6a;")
        self.integrate_btn.setVisible(True)
        self.integrate_btn.setEnabled(True)
        self.pdf_path_changed.emit(pdf_path)

    def _on_stage1_error(self, pdf_path: str, page_num: int, error_msg: str):
        self._stage1_errors += 1
        self.info_label.setText(f"⚠️ 第 {page_num} 页解析失败：{error_msg[:80]}")
        self.info_label.setStyleSheet("color: #f7768e;")
        self.progress_bar.setVisible(True)
        self.progress_bar.setToolTip(
            f"已有 {self._stage1_errors} 页解析失败，可在论文库右键菜单选择「重新逐页解析」重试"
        )

    def _on_request_integrate(self):
        if not self._processor:
            return
        self.integrate_btn.setEnabled(False)
        self.integrate_btn.setText("⏳ 整合中...")
        self.info_label.setText("⏳ 正在跨页整合，构建结构化文档...")
        self.info_label.setStyleSheet("color: #e0af68;")
        QApplication.processEvents()
        self._processor.start_stage2()

    def _on_stage2_finished(self, pdf_path: str, doc: "StructuredDocument"):
        self._structured_doc = doc
        self.integrate_btn.setVisible(False)
        self.progress_bar.setVisible(False)
        self._render_document(doc)

        from ..utils.config import save_doc_state
        save_doc_state(pdf_path, {"structured_document": doc.to_dict()})

        self.info_label.setText(f"📖 {doc.title or '论文'} — {len(doc.display_elements)} 个元素")
        self.info_label.setStyleSheet("color: #9ece6a;")
        self.auto_trans_btn.setEnabled(True)

        full_text = "\n\n".join(e.text for e in doc.display_elements if e.text)
        self.pdf_loaded.emit(full_text)

    def _on_stage2_error(self, pdf_path: str, error_msg: str):
        self.integrate_btn.setEnabled(True)
        self.integrate_btn.setText("🔄 重试整合")
        self.info_label.setText(f"⚠️ 整合失败：{error_msg}，可重试")
        self.info_label.setStyleSheet("color: #f7768e;")

    def _reset_view(self):
        self._structured_doc = None
        self._stage1_complete = False
        self._stage1_errors = 0
        for worker in self._trans_workers.values():
            if worker.isRunning():
                worker.quit()
                if not worker.wait(1000):
                    worker.terminate()
                    worker.wait()
        self._trans_workers.clear()
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self.placeholder.setVisible(True)
        self.progress_bar.setVisible(False)
        self.progress_bar.setValue(0)
        self.integrate_btn.setVisible(False)
        self.auto_trans_btn.setEnabled(False)

    def _render_document(self, doc: "StructuredDocument"):
        import os as _os
        for card in self._cards:
            card.setParent(None)
            card.deleteLater()
        self._cards.clear()
        self.placeholder.setVisible(False)

        image_base_dir = ""
        if self._processor:
            from ..utils.config import get_page_cache_dir
            image_base_dir = str(get_page_cache_dir(self._current_path))

        for i, elem in enumerate(doc.display_elements):
            if elem.element_type in ("figure", "table"):
                elem.image_path = self._resolve_image_path(elem, image_base_dir)
                card = ImageCard(elem, parent=self.container)
            elif elem.element_type in ("header_footer", "publisher_logo"):
                continue
            else:
                card = ParagraphCard(elem, i, parent=self.container)
                card.translate_requested.connect(self._on_translate_request)
            self._cards.append(card)
            self.card_layout.addWidget(card)
        self.card_layout.addStretch()

    def _resolve_image_path(self, elem, image_base_dir: str) -> str:
        """解析图表截图路径：优先元素自带路径，否则按 element_id 重建（兼容旧缓存）。

        裁剪文件名格式: page_{page:03d}_{element_id}.png。
        """
        if elem.image_path:
            p = elem.image_path
            if not _os.path.isabs(p) and image_base_dir:
                p = _os.path.join(image_base_dir, p)
            if _os.path.exists(p):
                return p
        if elem.element_id and image_base_dir:
            p = _os.path.join(
                image_base_dir, f"page_{elem.page:03d}_{elem.element_id}.png"
            )
            if _os.path.exists(p):
                return p
        return ""

    def _on_translate_request(self, idx: int, text: str):
        if not self._translate_client:
            return
        worker = self._trans_workers.get(idx)
        if worker and worker.isRunning():
            return
        worker = TranslationWorker(self._translate_client, idx, text)
        worker.finished.connect(self._on_translation_done)
        worker.error.connect(self._on_translation_error)
        self._trans_workers[idx] = worker
        worker.start()

    def _on_translation_done(self, idx: int, zh: str):
        self._trans_workers.pop(idx, None)
        for card in self._cards:
            if hasattr(card, '_index') and card._index == idx and isinstance(card, ParagraphCard):
                card.show_translation(zh)
                break

    def _on_translation_error(self, idx: int, err: str):
        self._trans_workers.pop(idx, None)
        for card in self._cards:
            if hasattr(card, '_index') and card._index == idx and isinstance(card, ParagraphCard):
                card.show_translation_error(err)
                break

    def _on_toggle_auto_translate(self):
        self._auto_translate = not self._auto_translate
        self.auto_trans_btn.setText(f"🔄 自动翻译：{'开' if self._auto_translate else '关'}")
        if self._auto_translate:
            self._on_scroll()

    def _on_scroll(self):
        if not self._auto_translate or not self._translate_client:
            return
        viewport = self.scroll_area.viewport()
        view_top = self.scroll_area.verticalScrollBar().value()
        view_bottom = view_top + viewport.height()
        for card in self._cards:
            if not isinstance(card, ParagraphCard):
                continue
            if not card.is_body or not card._is_english or card._translated:
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

    def _teardown_processor(self):
        """取消并断开旧的 PDF 处理器，避免切换论文时后台线程泄漏。"""
        if self._processor is None:
            return
        proc = self._processor
        try:
            for sig in (proc.stage1_progress, proc.stage1_complete, proc.stage1_error,
                        proc.stage2_finished, proc.stage2_error):
                if proc.receivers(sig) > 0:
                    sig.disconnect()
        except (RuntimeError, TypeError):
            pass
        proc.cancel()
        self._processor = None
