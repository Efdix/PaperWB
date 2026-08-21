"""文献智能工作台 —— 库内 RAG 问答 + 定向文献巡视。

三栏布局::

    ┌──────────────┬──────────────────────────┬───────────────┐
    │ 检索方向管理   │  库内问答（对话区）          │ 文献推荐流      │
    │ 方向卡片列表   │  回答带 [n] 角标            │ 定时巡视结果    │
    │ + 新方向      │  参考文献 → 跳转阅读工作台    │ 新文献卡片      │
    └──────────────┴──────────────────────────┴───────────────┘

职责划分：本工作台面向整个 Zotero 库（跨文献综合问答、定时文献巡视）；
单篇论文的阅读问答仍在阅读工作台。
"""

from __future__ import annotations

import os
import urllib.parse
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTabWidget, QTextEdit,
    QVBoxLayout, QWidget,
)

from ..core.library_qa import LibraryQAEngine
from ..core.literature_scout import (
    MAX_INTERVAL_HOURS, ScoutManager, ScoutTopic, papers_to_ris, save_csv,
)
from ..utils.threads import track
from .chat_panel import ChatBubble

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.zotero_parser import ZoteroLibrary


# ============================================================
# 后台线程
# ============================================================

class IndexBuildWorker(QThread):
    """后台构建全库索引（元数据 + PDF 全文，支持增量与中断）。"""

    progress = Signal(int, int, str)
    finished_signal = Signal(int, int)   # (文献数, 段落数)
    error = Signal(str)

    def __init__(self, engine: LibraryQAEngine, items: list, force: bool = False,
                 parent=None):
        super().__init__(parent)
        self._engine = engine
        self._items = items
        self._force = force

    def run(self) -> None:
        try:
            self._engine.set_items(self._items)
            stats = self._engine.refresh_fulltext(
                self._items,
                progress_cb=lambda d, t, name: self.progress.emit(d, t, name),
                interrupt_cb=self.isInterruptionRequested,
                force=self._force,
            )
            self.finished_signal.emit(stats["items"], stats["chunks"])
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))


class LibraryQAWorker(QThread):
    """库内问答：检索证据（CPU 段）→ 流式调用解析接口。"""

    chunk_received = Signal(str)
    answer_finished = Signal(list)       # references 列表
    error = Signal(str)
    done = Signal()

    def __init__(self, client: "LLMClient", engine: LibraryQAEngine,
                 question: str, metadata_only: bool, history: list[dict],
                 parent=None):
        super().__init__(parent)
        self._client = client
        self._engine = engine
        self._question = question
        self._metadata_only = metadata_only
        self._history = history

    def run(self) -> None:
        try:
            messages, refs = self._engine.prepare_messages(
                self._question, self._history,
                metadata_only=self._metadata_only)
            for chunk in self._client.chat_stream(messages):
                if self.isInterruptionRequested():
                    return  # 已取消：不再投递旧问答的后续内容
                self.chunk_received.emit(chunk)
            self.answer_finished.emit(refs)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))
        finally:
            self.done.emit()


# ============================================================
# 检索方向编辑对话框
# ============================================================

class TopicEditDialog(QDialog):
    """新建/编辑检索方向。"""

    def __init__(self, collections: list[tuple[str, str]],
                 topic: ScoutTopic | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建检索方向" if topic is None else "编辑检索方向")
        self.setMinimumSize(540, 470)
        self._base = topic

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._name_edit = QLineEdit(topic.name if topic else "")
        self._name_edit.setPlaceholderText("例如：羽色发育机制")
        form.addRow("方向名称：", self._name_edit)

        self._keywords_edit = QTextEdit()
        self._keywords_edit.setPlaceholderText(
            "每行一个 PubMed 英文检索式，例如：\n"
            "avian feather melanocyte single-cell\n"
            "bird plumage pigmentation scRNA-seq")
        self._keywords_edit.setMaximumHeight(130)
        if topic and topic.keywords:
            self._keywords_edit.setPlainText("\n".join(topic.keywords))
        form.addRow("检索关键词：", self._keywords_edit)

        self._collection_combo = QComboBox()
        self._collection_combo.addItem("全库比对（推荐）", "")
        for key, label in collections:
            self._collection_combo.addItem(label, key)
        if topic and topic.collection_key:
            idx = self._collection_combo.findData(topic.collection_key)
            if idx >= 0:
                self._collection_combo.setCurrentIndex(idx)
        self._collection_combo.setToolTip(
            "巡视发现的新文献只与该集合内的文献比对去重；\n"
            "「已在此集合」的新文献会被折叠。默认与整个 Zotero 库比对。")
        form.addRow("限定集合：", self._collection_combo)

        interval_row = QWidget()
        interval_lay = QHBoxLayout(interval_row)
        interval_lay.setContentsMargins(0, 0, 0, 0)
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(1, MAX_INTERVAL_HOURS)
        self._interval_spin.setSuffix(" 小时")
        self._interval_spin.setValue(topic.interval_hours if topic else 24)
        self._limit_spin = QSpinBox()
        self._limit_spin.setRange(5, 50)
        self._limit_spin.setSuffix(" 条/次")
        self._limit_spin.setValue(topic.limit if topic else 15)
        interval_lay.addWidget(self._interval_spin)
        interval_lay.addWidget(QLabel("每次获取："))
        interval_lay.addWidget(self._limit_spin)
        interval_lay.addStretch()
        form.addRow("巡视周期：", interval_row)

        self._llm_cb = QCheckBox("AI 模糊比对（应对 DOI 缺失/标题改写，消耗解析接口）")
        self._llm_cb.setChecked(bool(topic.use_llm_match) if topic else False)
        form.addRow("", self._llm_cb)

        self._enabled_cb = QCheckBox("启用定时巡视")
        self._enabled_cb.setChecked(topic.enabled if topic else True)
        form.addRow("", self._enabled_cb)

        layout.addLayout(form)
        layout.addStretch()

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("保存")
        buttons.button(QDialogButtonBox.StandardButton.Ok).setObjectName("primaryBtn")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self._name_edit.text().strip():
            QMessageBox.warning(self, "提示", "请填写方向名称。")
            return
        kws = [line.strip() for line in
               self._keywords_edit.toPlainText().splitlines() if line.strip()]
        if not kws:
            QMessageBox.warning(
                self, "提示", "请至少填写一个 PubMed 检索关键词（每行一个）。")
            return
        self.accept()

    def topic_result(self) -> ScoutTopic:
        """对话框 accept 后取结果（保留原 id / last_run）。"""
        base = self._base
        return ScoutTopic(
            id=base.id if base else datetime.now().strftime("t%H%M%S%f"),
            name=self._name_edit.text().strip(),
            keywords=[line.strip() for line in
                      self._keywords_edit.toPlainText().splitlines() if line.strip()],
            collection_key=self._collection_combo.currentData() or "",
            interval_hours=self._interval_spin.value(),
            limit=self._limit_spin.value(),
            enabled=self._enabled_cb.isChecked(),
            use_llm_match=self._llm_cb.isChecked(),
            last_run=base.last_run if base else "",
            last_new=base.last_new if base else 0,
        )


# ============================================================
# 方向卡片
# ============================================================

class TopicCard(QFrame):
    """单个检索方向的卡片。"""

    run_requested = Signal(str)
    edit_requested = Signal(str)
    delete_requested = Signal(str)
    toggle_requested = Signal(str, bool)
    search_web_requested = Signal(list)

    def __init__(self, topic: ScoutTopic, parent=None):
        super().__init__(parent)
        self.setObjectName("topicCard")
        self._topic = topic
        self._running = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(6)

        top = QHBoxLayout()
        top.setSpacing(8)
        self._name_label = QLabel()
        self._name_label.setObjectName("sectionLabel")
        self._name_label.setWordWrap(True)
        self._toggle_btn = QPushButton()
        self._toggle_btn.setObjectName("topicToggle")
        self._toggle_btn.setCheckable(True)
        self._toggle_btn.setFixedWidth(74)
        self._toggle_btn.clicked.connect(
            lambda checked: self.toggle_requested.emit(self._topic.id, checked))
        top.addWidget(self._name_label, 1)
        top.addWidget(self._toggle_btn)
        layout.addLayout(top)

        self._kw_label = QLabel()
        self._kw_label.setObjectName("subtitleLabel")
        self._kw_label.setWordWrap(True)
        layout.addWidget(self._kw_label)

        self._meta_label = QLabel()
        self._meta_label.setObjectName("subtitleLabel")
        self._meta_label.setWordWrap(True)
        layout.addWidget(self._meta_label)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        run_btn = QPushButton("立即巡视")
        run_btn.setObjectName("secondaryBtn")
        run_btn.setToolTip("立即检索一次 PubMed 并过滤库内已有")
        run_btn.clicked.connect(lambda: self.run_requested.emit(self._topic.id))
        web_btn = QPushButton("🔍")
        web_btn.setObjectName("iconBtn")
        web_btn.setToolTip("生成检索式并在 PubMed 网页打开")
        web_btn.clicked.connect(
            lambda: self.search_web_requested.emit(list(self._topic.keywords)))
        edit_btn = QPushButton("✎")
        edit_btn.setObjectName("iconBtn")
        edit_btn.setToolTip("编辑方向")
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self._topic.id))
        del_btn = QPushButton("✕")
        del_btn.setObjectName("iconBtn")
        del_btn.setToolTip("删除方向")
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self._topic.id))
        btns.addWidget(run_btn)
        btns.addWidget(web_btn)
        btns.addStretch()
        btns.addWidget(edit_btn)
        btns.addWidget(del_btn)
        layout.addLayout(btns)

        self._refresh_labels()

    def _refresh_labels(self) -> None:
        t = self._topic
        suffix = " ⏳" if self._running else ""
        self._name_label.setText((t.name or "（未命名方向）") + suffix)
        kws = "；".join(t.keywords[:4]) + ("…" if len(t.keywords) > 4 else "")
        self._kw_label.setText(f"关键词：{kws or '（未设置）'}")
        last = t.last_run[:16].replace("T", " ") if t.last_run else "未运行"
        running = " · 巡视中…" if self._running else ""
        self._meta_label.setText(
            f"每 {t.interval_hours} 小时 · 上次 {last} · 新增 {t.last_new}{running}")
        self._toggle_btn.blockSignals(True)
        self._toggle_btn.setChecked(t.enabled)
        self._toggle_btn.setText("已启用" if t.enabled else "已停用")
        self._toggle_btn.blockSignals(False)

    def update_topic(self, topic: ScoutTopic) -> None:
        self._topic = topic
        self._refresh_labels()

    def set_running(self, running: bool) -> None:
        if self._running == running:
            return
        self._running = running
        self._refresh_labels()


# ============================================================
# 推荐流卡片
# ============================================================

class ScoutCard(QFrame):
    """单条巡视发现的文献卡片。"""

    ignore_requested = Signal(str)

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("scoutCard")
        self._entry = entry
        p = entry.get("paper", {})

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)

        title = QLabel(p.get("title", "") or "（无标题）")
        title.setObjectName("sectionLabel")
        title.setWordWrap(True)
        layout.addWidget(title)

        meta = QLabel(f"{p.get('authors', '')} ({p.get('year', '')})  "
                      f"{p.get('journal', '')}".strip())
        meta.setObjectName("subtitleLabel")
        meta.setWordWrap(True)
        layout.addWidget(meta)

        chips = QHBoxLayout()
        chips.setSpacing(6)
        topic_chip = QLabel(entry.get("topic", ""))
        topic_chip.setObjectName("feedChip")
        topic_chip.setProperty("kind", "topic")
        chips.addWidget(topic_chip)
        chips.addStretch()
        layout.addLayout(chips)

        abstract = (p.get("abstract") or "").strip()
        if abstract:
            shown = abstract[:200] + ("…" if len(abstract) > 200 else "")
            abs_label = QLabel(shown)
            abs_label.setObjectName("subtitleLabel")
            abs_label.setWordWrap(True)
            abs_label.setToolTip(abstract)
            layout.addWidget(abs_label)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        pubmed_btn = QPushButton("PubMed ↗")
        pubmed_btn.setObjectName("secondaryBtn")
        pubmed_btn.setToolTip(p.get("url", "在 PubMed 打开这篇文献"))
        pubmed_btn.clicked.connect(
            lambda: self._open_url(p.get("url", "")))
        cite_btn = QPushButton("复制引文")
        cite_btn.setObjectName("secondaryBtn")
        cite_btn.setToolTip("复制 (Author et al., Year) 格式引用")
        cite_btn.clicked.connect(self._copy_citation)
        ignore_btn = QPushButton("忽略")
        ignore_btn.setObjectName("softBtn")
        ignore_btn.setToolTip("从推荐流移除（之后不再重复推送）")
        ignore_btn.clicked.connect(
            lambda: self.ignore_requested.emit(entry.get("id", "")))
        btns.addWidget(pubmed_btn)
        btns.addWidget(cite_btn)
        btns.addStretch()
        btns.addWidget(ignore_btn)
        layout.addLayout(btns)

    @staticmethod
    def _open_url(url: str) -> None:
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _copy_citation(self) -> None:
        p = self._entry.get("paper", {})
        first = (p.get("authors", "") or "Unknown").split(" ")[0] or "Unknown"
        marker = f"({first} et al., {p.get('year', '?')})"
        QApplication.clipboard().setText(marker)


# ============================================================
# 参考文献卡片（问答回答下方）
# ============================================================

class ReferenceListCard(QFrame):
    """回答引用的库内文献列表，点击可跳阅读工作台打开 PDF。"""

    open_requested = Signal(str)

    def __init__(self, refs: list[dict], parent=None):
        super().__init__(parent)
        self.setObjectName("refCard")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(4)

        header = QLabel("参考文献（点击打开原文）")
        header.setObjectName("sectionLabel")
        layout.addWidget(header)

        for r in refs:
            title = r.get("title", "") or ""
            shown = title[:60] + ("…" if len(title) > 60 else "")
            page = f" · 第 {r.get('page', 0)} 页" if r.get("page") else ""
            if r.get("pdf_path"):
                text = f"📄 [{r['n']}] {r.get('authors', '')} ({r.get('year', '')}) {shown}{page}"
                btn = QPushButton(text)
                btn.setObjectName("refRow")
                btn.setCursor(Qt.CursorShape.PointingHandCursor)
                btn.setToolTip(f"{title}\n{r['pdf_path']}")
                btn.clicked.connect(
                    lambda _c=False, path=r["pdf_path"]:
                        self.open_requested.emit(path))
            else:
                btn = QPushButton(
                    f"⚪ [{r['n']}] {r.get('authors', '')} ({r.get('year', '')}) "
                    f"{shown} · 无 PDF 附件")
                btn.setObjectName("refRow")
                btn.setEnabled(False)
                btn.setToolTip(title)
            layout.addWidget(btn)


# ============================================================
# 主面板
# ============================================================

class WorkbenchPanel(QWidget):
    """文献智能工作台：检索方向（左）+ 库内问答（中）+ 文献推荐流（右）。

    信号:
        open_pdf_requested(str): 用户点击参考文献，请求在阅读工作台打开 PDF。
    """

    open_pdf_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchPanel")
        self._library: "ZoteroLibrary | None" = None
        self._text_client: "LLMClient | None" = None
        self._engine = LibraryQAEngine()
        self._engine_ready = False
        self._items_snapshot: list = []
        self._collections: list[tuple[str, str]] = []

        self._qa_worker: LibraryQAWorker | None = None
        self._index_worker: IndexBuildWorker | None = None
        self._ai_worker: "PaperSearchWorker | None" = None
        self._qa_busy = False
        self._qa_history: list[dict] = []
        self._current_ai_bubble: ChatBubble | None = None
        self._welcome: QLabel | None = None
        self._feed_empty: QLabel | None = None
        self._running_topics: set[str] = set()

        self._manager = ScoutManager(self)
        self._manager.topics_changed.connect(self._render_topics)
        self._manager.topic_running.connect(self._on_topic_running)
        self._manager.results_ready.connect(self._on_scout_results)
        self._ai_searcher = None  # 测试可注入假多源检索器

        self._setup_ui()
        self._manager.status_msg.connect(self._feed_status.setText)
        self._insert_welcome()
        self._render_topics()
        self._render_feed()

    # ================= UI 构建 =================

    def _setup_ui(self) -> None:
        header = QFrame()
        header.setObjectName("workspaceHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)
        header_layout.setSpacing(8)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        eyebrow = QLabel("文献发现与综合")
        eyebrow.setObjectName("eyebrowLabel")
        title_box.addWidget(eyebrow)
        title = QLabel("文献工作台")
        title.setObjectName("titleLabel")
        title_box.addWidget(title)
        header_layout.addLayout(title_box)
        subtitle = QLabel("跨文献问答、AI 检索与定向巡视集中在同一工作区")
        subtitle.setObjectName("subtitleLabel")
        header_layout.addWidget(subtitle)
        header_layout.addStretch()

        self._topic_toggle = QPushButton("检索方向")
        self._topic_toggle.setObjectName("paneToggle")
        self._topic_toggle.setCheckable(True)
        self._topic_toggle.setChecked(True)
        header_layout.addWidget(self._topic_toggle)
        self._feed_toggle = QPushButton("推荐流")
        self._feed_toggle.setObjectName("paneToggle")
        self._feed_toggle.setCheckable(True)
        self._feed_toggle.setChecked(True)
        header_layout.addWidget(self._feed_toggle)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.setOpaqueResize(False)
        self._topic_panel = self._build_topic_panel()
        splitter.addWidget(self._topic_panel)

        center = QWidget()
        center_layout = QVBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        self._center_tabs = QTabWidget()
        self._center_tabs.setDocumentMode(True)
        self._center_tabs.addTab(self._build_qa_panel(), "库内问答")
        self._center_tabs.addTab(self._build_ai_search_panel(), "AI 检索")
        center_layout.addWidget(self._center_tabs)
        splitter.addWidget(center)

        self._feed_panel = self._build_feed_panel()
        splitter.addWidget(self._feed_panel)
        splitter.setSizes([280, 640, 400])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setStretchFactor(2, 0)
        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, False)
        splitter.setCollapsible(2, True)
        self._workbench_splitter = splitter
        self._topic_toggle.toggled.connect(self._set_topic_visible)
        self._feed_toggle.toggled.connect(self._set_feed_visible)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(header)
        layout.addWidget(splitter, 1)

    def _build_topic_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("topicPanel")
        panel.setMinimumWidth(240)
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 14, 12)
        v.setSpacing(8)

        title = QLabel("检索方向")
        title.setObjectName("titleLabel")
        v.addWidget(title)
        subtitle = QLabel("定时巡视 PubMed · 自动滤除库内已有")
        subtitle.setObjectName("subtitleLabel")
        subtitle.setWordWrap(True)
        v.addWidget(subtitle)

        new_btn = QPushButton("+ 新方向")
        new_btn.setObjectName("primaryBtn")
        new_btn.setToolTip("创建一个研究方向，定时检索 PubMed 新文献")
        new_btn.clicked.connect(self._on_new_topic)
        v.addWidget(new_btn)

        self._topic_scroll = QScrollArea()
        self._topic_scroll.setWidgetResizable(True)
        host = QWidget()
        self._topic_layout = QVBoxLayout(host)
        self._topic_layout.setContentsMargins(0, 4, 2, 0)
        self._topic_layout.setSpacing(8)
        self._topic_layout.addStretch()
        self._topic_scroll.setWidget(host)
        v.addWidget(self._topic_scroll, 1)
        return panel

    def _build_qa_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("qaPanel")
        panel.setMinimumWidth(360)
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 14, 12)
        v.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 8)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("库内问答")
        title.setObjectName("titleLabel")
        title_box.addWidget(title)
        subtitle = QLabel("面向整个 Zotero 库的跨文献问答 · 回答带 [n] 角标")
        subtitle.setObjectName("subtitleLabel")
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        v.addLayout(header)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet("background-color: #e4e0d8; max-height: 1px;")
        v.addWidget(sep)

        self._qa_scroll = QScrollArea()
        self._qa_scroll.setWidgetResizable(True)
        self._qa_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        host = QWidget()
        host.setObjectName("chatMessages")
        self._msg_layout = QVBoxLayout(host)
        self._msg_layout.setAlignment(Qt.AlignmentFlag.AlignTop)
        self._msg_layout.setSpacing(8)
        self._msg_layout.addStretch()
        self._qa_scroll.setWidget(host)
        v.addWidget(self._qa_scroll, 1)

        input_frame = QFrame()
        input_frame.setObjectName("qaInput")
        input_layout = QVBoxLayout(input_frame)
        input_layout.setContentsMargins(0, 10, 0, 0)
        input_layout.setSpacing(8)

        self._ask_input = QTextEdit()
        self._ask_input.setPlaceholderText(
            "向你的文献库提问，按 Ctrl+Enter 发送。\n"
            "例如：这两篇关于羽色发育的结论矛盾吗？库内有哪些用单细胞测序的文献？")
        self._ask_input.setMaximumHeight(110)
        self._ask_input.setMinimumHeight(56)
        self._ask_input.installEventFilter(self)
        input_layout.addWidget(self._ask_input)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        self._lib_only_cb = QCheckBox("只问库")
        self._lib_only_cb.setToolTip(
            "开启后不检索 PDF 全文，只根据条目元数据（标题/作者/摘要）回答，\n"
            "适合「我有哪些关于 X 的文献」这类清点式问题。")
        ctrl.addWidget(self._lib_only_cb)
        ctrl.addStretch()
        rebuild_btn = QPushButton("重建索引")
        rebuild_btn.setObjectName("secondaryBtn")
        rebuild_btn.setToolTip("强制重建全库全文索引（PDF 更换附件后使用）")
        rebuild_btn.clicked.connect(lambda: self._start_index_build(force=True))
        ctrl.addWidget(rebuild_btn)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("softBtn")
        clear_btn.setToolTip("清空当前问答会话")
        clear_btn.clicked.connect(self._on_clear_chat)
        ctrl.addWidget(clear_btn)
        self._ask_btn = QPushButton("提问 ✈")
        self._ask_btn.setObjectName("primaryBtn")
        self._ask_btn.setEnabled(False)
        self._ask_btn.clicked.connect(self._on_ask)
        ctrl.addWidget(self._ask_btn)
        input_layout.addLayout(ctrl)

        self._qa_status = QLabel("索引：未构建")
        self._qa_status.setObjectName("subtitleLabel")
        self._qa_status.setWordWrap(True)
        input_layout.addWidget(self._qa_status)

        v.addWidget(input_frame)
        return panel

    def _build_ai_search_panel(self) -> QFrame:
        """AI 检索页签：自然语言描述需求 → 自动生成检索式 → 多源检索。"""
        panel = QFrame()
        panel.setObjectName("aiSearchPanel")
        panel.setMinimumWidth(360)
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 14, 12)
        v.setSpacing(8)

        header = QHBoxLayout()
        header.setContentsMargins(0, 0, 0, 0)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("AI 检索")
        title.setObjectName("titleLabel")
        title_box.addWidget(title)
        subtitle = QLabel("自然语言描述需求，自动检索 PubMed + arXiv 并滤除库内已有")
        subtitle.setObjectName("subtitleLabel")
        subtitle.setWordWrap(True)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        v.addLayout(header)

        self._ai_input = QTextEdit()
        self._ai_input.setPlaceholderText(
            "描述你想找的文献，例如：\n"
            "· 近三年鸟类羽色发育中黑色素细胞分化的单细胞研究\n"
            "· 2024 年以来关于羽毛图案形成的机制综述\n\n"
            "按 Ctrl+Enter 检索")
        self._ai_input.setMaximumHeight(110)
        self._ai_input.setMinimumHeight(56)
        self._ai_input.installEventFilter(self)
        v.addWidget(self._ai_input)

        ctrl = QHBoxLayout()
        ctrl.setSpacing(8)
        ctrl.addStretch()
        self._ai_search_btn = QPushButton("开始检索 ✈")
        self._ai_search_btn.setObjectName("primaryBtn")
        self._ai_search_btn.setEnabled(False)
        self._ai_search_btn.clicked.connect(self._on_ai_search)
        ctrl.addWidget(self._ai_search_btn)
        v.addLayout(ctrl)

        self._ai_log = QLabel("")
        self._ai_log.setObjectName("subtitleLabel")
        self._ai_log.setWordWrap(True)
        self._ai_log.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        v.addWidget(self._ai_log)

        self._ai_scroll = QScrollArea()
        self._ai_scroll.setWidgetResizable(True)
        host = QWidget()
        self._ai_results_layout = QVBoxLayout(host)
        self._ai_results_layout.setContentsMargins(0, 4, 2, 0)
        self._ai_results_layout.setSpacing(8)
        self._ai_results_layout.addStretch()
        self._ai_scroll.setWidget(host)
        v.addWidget(self._ai_scroll, 1)

        return panel

    def _set_topic_visible(self, visible: bool) -> None:
        self._topic_panel.setVisible(bool(visible))
        if visible:
            self._workbench_splitter.setSizes([280, max(420, self.width() - 700), 360])
        else:
            self._workbench_splitter.setSizes([0, max(600, self.width() - 390), 360])

    def _set_feed_visible(self, visible: bool) -> None:
        self._feed_panel.setVisible(bool(visible))
        if visible:
            self._workbench_splitter.setSizes([280, max(420, self.width() - 700), 360])
        else:
            self._workbench_splitter.setSizes([280, max(600, self.width() - 330), 0])

    def _build_feed_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("feedPanel")
        panel.setMinimumWidth(330)
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 14, 12)
        v.setSpacing(8)

        title = QLabel("文献推荐流")
        title.setObjectName("titleLabel")
        v.addWidget(title)
        self._feed_status = QLabel("未运行巡视 · 新文献自动推送至此")
        self._feed_status.setObjectName("subtitleLabel")
        self._feed_status.setWordWrap(True)
        v.addWidget(self._feed_status)

        self._feed_scroll = QScrollArea()
        self._feed_scroll.setWidgetResizable(True)
        host = QWidget()
        self._feed_layout = QVBoxLayout(host)
        self._feed_layout.setContentsMargins(0, 4, 2, 0)
        self._feed_layout.setSpacing(8)
        self._feed_layout.addStretch()
        self._feed_scroll.setWidget(host)
        v.addWidget(self._feed_scroll, 1)

        footer = QHBoxLayout()
        footer.setSpacing(6)
        ris_btn = QPushButton("导出 RIS")
        ris_btn.setObjectName("secondaryBtn")
        ris_btn.setToolTip("导出推荐流中的新文献为 RIS，可在 Zotero 中手动导入")
        ris_btn.clicked.connect(self._on_export_ris)
        csv_btn = QPushButton("导出 CSV")
        csv_btn.setObjectName("secondaryBtn")
        csv_btn.clicked.connect(self._on_export_csv)
        clear_btn = QPushButton("清空")
        clear_btn.setObjectName("softBtn")
        clear_btn.clicked.connect(self._on_clear_feed)
        footer.addWidget(ris_btn)
        footer.addWidget(csv_btn)
        footer.addStretch()
        footer.addWidget(clear_btn)
        v.addLayout(footer)
        return panel

    # ================= 依赖注入 =================

    def set_text_client(self, client: "LLMClient | None") -> None:
        self._text_client = client
        self._manager.set_llm_client(client)
        self._apply_ask_state()
        self._ai_search_btn.setEnabled(client is not None)

    def set_ai_searcher(self, searcher) -> None:
        """注入自定义多源检索器（测试用；None = 运行时默认）。"""
        self._ai_searcher = searcher

    def set_zotero_library(self, library: "ZoteroLibrary | None") -> None:
        self._library = library
        items: list = []
        if library is not None and library.is_available:
            try:
                items = library.get_all_items()
            except Exception:  # noqa: BLE001
                items = []
        self._items_snapshot = items
        self._engine_ready = False
        self._build_collections()
        self._manager.set_match_pool(self._build_pool())
        self._manager.start()
        self._start_index_build()
        self._apply_ask_state()

    def on_zotero_changed(self) -> None:
        """Zotero 周期同步发现变化后：重建比对池 + 增量刷新索引。"""
        self.set_zotero_library(self._library)

    def reload_storage(self) -> bool:
        """数据根目录切换后重新绑定索引、巡视和推荐流。"""
        for attr in ("_index_worker", "_qa_worker", "_ai_worker"):
            worker = getattr(self, attr)
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(3_000):
                    self._feed_status.setText("已有文献任务正在退出，稍后再切换数据目录")
                    return False
            setattr(self, attr, None)
        self._qa_busy = False
        if not self._manager.reload_storage():
            self._feed_status.setText("已有巡视任务正在退出，稍后再切换数据目录")
            return False
        self._engine = LibraryQAEngine()
        self._engine_ready = False
        self._on_clear_chat()
        self._render_topics()
        self._render_feed()
        if self._library is not None:
            self._manager.set_match_pool(self._build_pool())
            self._manager.start()
            self._start_index_build()
        return True

    def shutdown(self) -> None:
        """停止巡视定时器并请求中断后台线程（关窗时调用）。"""
        self._manager.shutdown()
        for w in (self._index_worker, self._qa_worker, self._ai_worker):
            if w is not None and w.isRunning():
                w.requestInterruption()

    def has_busy_workers(self) -> bool:
        qa = self._qa_worker is not None and self._qa_worker.isRunning()
        idx = self._index_worker is not None and self._index_worker.isRunning()
        ai = self._ai_worker is not None and self._ai_worker.isRunning()
        return qa or idx or ai or self._manager.has_busy_workers()

    # ================= 库快照工具 =================

    def _build_collections(self) -> None:
        """集合下拉数据：[(key, '父 / 子' 显示名)]。"""
        self._collections = []
        lib = self._library
        if lib is None:
            return
        by_id = {c.collection_id: c for c in lib.collections}
        for c in lib.get_collections_tree():
            self._walk_collections(c, by_id)

    def _walk_collections(self, coll, by_id: dict) -> None:
        """递归收集 (key, 显示名)。"""
        names = [coll.name or ""]
        pid = coll.parent_id
        seen = {coll.collection_id}
        while pid is not None and pid in by_id and pid not in seen:
            parent = by_id[pid]
            names.append(parent.name or "")
            seen.add(pid)
            pid = parent.parent_id
        self._collections.append((coll.key, " / ".join(reversed(names))))
        for cid in coll.child_ids:
            child = by_id.get(cid)
            if child is not None:
                self._walk_collections(child, by_id)

    def _build_pool(self) -> list[dict]:
        """构建巡视比对池：条目快照 + 其所属集合（含祖先）的 key 集合。"""
        lib = self._library
        if lib is None:
            return []
        by_id = {c.collection_id: c for c in lib.collections}

        def ancestor_keys(c) -> set[str]:
            keys = {c.key}
            pid = c.parent_id
            seen = {c.collection_id}
            while pid is not None and pid in by_id and pid not in seen:
                parent = by_id[pid]
                keys.add(parent.key)
                seen.add(pid)
                pid = parent.parent_id
            return keys

        coll_keys_by_item: dict[int, set[str]] = {}
        for c in lib.collections:
            keys = ancestor_keys(c)
            for iid in c.item_ids:
                coll_keys_by_item.setdefault(iid, set()).update(keys)

        pool: list[dict] = []
        for it in self._items_snapshot:
            first_last = it.first_author_last if it.authors else ""
            pool.append({
                "key": it.key, "title": it.title, "doi": it.doi,
                "authors": first_last, "year": it.year,
                "collections": sorted(coll_keys_by_item.get(it.item_id, set())),
            })
        return pool

    # ================= 索引构建 =================

    def _start_index_build(self, force: bool = False) -> None:
        if self._index_worker is not None and self._index_worker.isRunning():
            self._qa_status.setText("索引：正在构建中，请稍候…")
            return
        if not self._items_snapshot:
            self._engine_ready = False
            self._qa_status.setText(
                "未检测到 Zotero 文献库——请在「设置 → Zotero 文献库路径设置」配置")
            self._apply_ask_state()
            return
        self._qa_status.setText("索引：准备构建…")
        worker = IndexBuildWorker(self._engine, self._items_snapshot, force)
        track(worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        self._index_worker = worker
        worker.progress.connect(self._on_index_progress)
        worker.finished_signal.connect(self._on_index_done)
        worker.error.connect(self._on_index_error)
        worker.start()
        self._apply_ask_state()

    def _on_index_progress(self, done: int, total: int, name: str) -> None:
        self._qa_status.setText(f"索引：{done}/{total} · {name}")

    def _on_index_done(self, items: int, chunks: int) -> None:
        if self.sender() is not self._index_worker:
            return
        self._index_worker = None
        self._engine_ready = True
        self._qa_status.setText(f"索引就绪 · {items} 篇全文 / {chunks} 段")
        self._apply_ask_state()

    def _on_index_error(self, err: str) -> None:
        if self.sender() is not self._index_worker:
            return
        self._index_worker = None
        # set_items 已执行的话元数据级问答仍可用
        self._engine_ready = self._engine.is_ready
        self._qa_status.setText(
            f"全文索引构建失败（{err}）"
            + ("；元数据问答可用" if self._engine_ready else ""))
        self._apply_ask_state()

    # ================= 库内问答 =================

    def _apply_ask_state(self) -> None:
        base = self._text_client is not None and self._engine_ready
        self._ask_input.setEnabled(base)
        self._ask_btn.setEnabled(base and not self._qa_busy)

    def eventFilter(self, obj, event) -> bool:
        # 构建顺序问题：eventFilter 可能在 _ai_input 创建前被调用，用 getattr 守卫
        if (obj is getattr(self, "_ask_input", None)
                or obj is getattr(self, "_ai_input", None)) \
                and event.type() == QEvent.Type.KeyPress:
            if (event.key() == Qt.Key.Key_Return
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                if obj is getattr(self, "_ai_input", None):
                    self._on_ai_search()
                else:
                    self._on_ask()
                return True
        return super().eventFilter(obj, event)

    def _insert_welcome(self) -> None:
        if self._welcome is not None:
            return
        welcome = QLabel(
            "欢迎使用文献工作台\n\n"
            "这里可以向你的整个 Zotero 文献库提问，例如：\n"
            "· 这两篇关于 X 的研究结论是否矛盾？\n"
            "· 我的库里有哪些使用单细胞测序的文献？\n"
            "· 总结一下 2023 年以来关于 Y 方向的进展\n\n"
            "回答会标注 [n] 角标，点击参考文献可跳到阅读工作台打开原文。\n"
            "首次使用会自动为库内 PDF 构建全文索引（只读，不改动 Zotero 数据）。"
        )
        welcome.setWordWrap(True)
        welcome.setStyleSheet(
            "color: #718180; background-color: #f5f8f6; border: 1px solid #e1ebe7; "
            "border-radius: 12px; padding: 18px; font-size: 13px; line-height: 1.8;"
        )
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, welcome)
        self._welcome = welcome

    def _remove_welcome(self) -> None:
        if self._welcome is not None:
            self._welcome.hide()
            self._welcome.setParent(None)
            self._welcome.deleteLater()
            self._welcome = None

    def _on_ask(self) -> None:
        if self._qa_busy:
            return
        text = self._ask_input.toPlainText().strip()
        if not text:
            return
        if self._text_client is None:
            QMessageBox.warning(self, "未配置解析接口",
                                "请先在「设置 → API 接口设置」中配置解析接口。")
            return
        if not self._engine_ready:
            QMessageBox.information(self, "索引未就绪", "全库索引正在构建，请稍候。")
            return

        self._ask_input.clear()
        self._remove_welcome()
        self._qa_history.append({"role": "user", "content": text})
        bubble = ChatBubble("user", text)
        self._insert_msg(bubble)

        self._qa_busy = True
        self._apply_ask_state()
        ai_bubble = ChatBubble("assistant", "AI 正在检索文献库…", thinking=True)
        self._insert_msg(ai_bubble)
        self._current_ai_bubble = ai_bubble

        worker = LibraryQAWorker(
            self._text_client, self._engine, text,
            metadata_only=self._lib_only_cb.isChecked(),
            history=list(self._qa_history[:-1]))
        track(worker)
        self._qa_worker = worker
        worker.chunk_received.connect(self._on_qa_chunk)
        worker.answer_finished.connect(self._on_qa_answer)
        worker.error.connect(self._on_qa_error)
        worker.done.connect(self._on_qa_done)
        worker.start()

    def _insert_msg(self, widget: QWidget) -> None:
        self._msg_layout.insertWidget(self._msg_layout.count() - 1, widget)
        QTimer.singleShot(50, lambda: self._qa_scroll.verticalScrollBar().setValue(
            self._qa_scroll.verticalScrollBar().maximum()))

    def _on_qa_chunk(self, chunk: str) -> None:
        if self.sender() is not self._qa_worker:
            return  # 旧 worker 的残余流直接丢弃
        if self._current_ai_bubble is not None:
            self._current_ai_bubble.append_content(chunk)

    def _on_qa_answer(self, refs: list) -> None:
        if self.sender() is not self._qa_worker:
            return
        self._finish_ai_bubble()
        if refs:
            card = ReferenceListCard(refs)
            card.open_requested.connect(self.open_pdf_requested.emit)
            self._insert_msg(card)

    def _on_qa_error(self, err: str) -> None:
        if self.sender() is not self._qa_worker:
            return
        if self._current_ai_bubble is not None:
            self._current_ai_bubble.append_content(f"\n\n❌ 错误：{err}")
        self._finish_ai_bubble()

    def _on_qa_done(self) -> None:
        if self.sender() is not self._qa_worker:
            return
        self._finish_ai_bubble()  # 中断路径：气泡可能还开着
        self._qa_worker = None
        self._qa_busy = False
        self._apply_ask_state()

    def _finish_ai_bubble(self) -> None:
        bubble = self._current_ai_bubble
        if bubble is None:
            return
        self._current_ai_bubble = None
        bubble.set_thinking(False)
        content = bubble.get_content().strip()
        if not content:
            content = "（回答被中断或模型未返回内容）"
            bubble.set_content(content)
        self._qa_history.append({"role": "assistant", "content": content})

    def _on_clear_chat(self) -> None:
        self._qa_history.clear()
        self._current_ai_bubble = None
        self._qa_busy = False
        while self._msg_layout.count():
            item = self._msg_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        self._msg_layout.addStretch()
        self._welcome = None
        self._insert_welcome()
        self._apply_ask_state()

    # ================= 检索方向 =================

    def _render_topics(self) -> None:
        while self._topic_layout.count() > 1:  # 保留末尾 stretch
            item = self._topic_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        topics = self._manager.topics()
        if not topics:
            empty = QLabel("尚未创建研究方向。\n点击「+ 新方向」，PaperWB 会按周期\n"
                           "自动检索 PubMed 并过滤库内已有文献。")
            empty.setObjectName("subtitleLabel")
            empty.setWordWrap(True)
            empty.setStyleSheet(
                "color: #718180; background-color: #f5f8f6; "
                "border: 1px solid #e1ebe7; border-radius: 12px; padding: 14px;")
            self._topic_layout.insertWidget(0, empty)
            return
        for t in topics:
            card = TopicCard(t)
            if t.id in self._running_topics:
                card.set_running(True)
            tid = t.id
            card.run_requested.connect(self._manager.run_topic_now)
            card.edit_requested.connect(lambda tid=tid: self._on_edit_topic(tid))
            card.delete_requested.connect(lambda tid=tid: self._on_delete_topic(tid))
            card.toggle_requested.connect(
                lambda tid, enabled, _t=t: self._on_toggle_topic(tid, enabled))
            card.search_web_requested.connect(self._on_search_web)
            self._topic_layout.insertWidget(self._topic_layout.count() - 1, card)

    def _on_new_topic(self) -> None:
        dlg = TopicEditDialog(self._collections, None, self)
        if dlg.exec():
            self._manager.upsert_topic(dlg.topic_result())

    def _on_edit_topic(self, topic_id: str) -> None:
        topic = self._manager.get_topic(topic_id)
        if topic is None:
            return
        dlg = TopicEditDialog(self._collections, topic, self)
        if dlg.exec():
            self._manager.upsert_topic(dlg.topic_result())

    def _on_delete_topic(self, topic_id: str) -> None:
        topic = self._manager.get_topic(topic_id)
        name = topic.name if topic else "该方向"
        r = QMessageBox.question(
            self, "删除检索方向",
            f"确定删除方向「{name}」？\n已推送的推荐流记录会保留。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            self._manager.remove_topic(topic_id)

    def _on_toggle_topic(self, topic_id: str, enabled: bool) -> None:
        self._manager.set_enabled(topic_id, enabled)

    def _on_topic_running(self, topic_id: str, running: bool) -> None:
        if running:
            self._running_topics.add(topic_id)
        else:
            self._running_topics.discard(topic_id)
        self._render_topics()

    def _on_search_web(self, keywords: list) -> None:
        if not keywords:
            return
        term = " OR ".join(keywords)
        url = ("https://pubmed.ncbi.nlm.nih.gov/?term="
               + urllib.parse.quote(term))
        QDesktopServices.openUrl(QUrl(url))

    # ================= AI 检索 =================

    def _on_ai_search(self) -> None:
        if self._ai_worker is not None and self._ai_worker.isRunning():
            return
        text = self._ai_input.toPlainText().strip()
        if not text:
            return
        if self._text_client is None:
            QMessageBox.warning(self, "未配置纯文本接口",
                                "请先在「设置 → API 接口设置」中配置纯文本接口。")
            return

        self._ai_search_btn.setEnabled(False)
        self._ai_search_btn.setText("检索中...")
        self._ai_log.setText("正在生成检索方案...")

        from ..core.literature_search import PaperSearchWorker
        worker = PaperSearchWorker(
            text, client=self._text_client, pool=self._build_pool(), limit=10,
            searcher=self._ai_searcher)
        track(worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        self._ai_worker = worker
        worker.log.connect(self._on_ai_log)
        worker.results_ready.connect(self._on_ai_results)
        worker.error.connect(self._on_ai_error)
        worker.done.connect(self._on_ai_done)
        worker.start()

    def _on_ai_log(self, msg: str) -> None:
        self._ai_log.setText(msg)

    def _on_ai_results(self, papers: list) -> None:
        if self.sender() is not self._ai_worker:
            return
        self._clear_ai_results()
        if not papers:
            empty = QLabel("未检索到符合条件的文献，可换一种描述再试。")
            empty.setObjectName("subtitleLabel")
            empty.setWordWrap(True)
            empty.setStyleSheet(
                "color: #718180; background-color: #f5f8f6; "
                "border: 1px solid #e1ebe7; border-radius: 12px; padding: 14px;")
            self._ai_results_layout.insertWidget(
                self._ai_results_layout.count() - 1, empty)
            return
        for p in papers:
            entry = {
                "id": f"{p.get('pmid') or p.get('arxiv_id') or p.get('doi') or ''}@AI检索",
                "topic": "AI 检索",
                "added_at": datetime.now().isoformat(timespec="seconds"),
                "paper": p,
            }
            card = ScoutCard(entry)
            card.ignore_requested.connect(self._on_ignore_feed)
            self._ai_results_layout.insertWidget(
                self._ai_results_layout.count() - 1, card)

    def _on_ai_error(self, err: str) -> None:
        if self.sender() is not self._ai_worker:
            return
        self._ai_log.setText(f"检索失败：{err}")

    def _on_ai_done(self) -> None:
        if self.sender() is not self._ai_worker:
            return
        self._ai_worker = None
        self._ai_search_btn.setEnabled(self._text_client is not None)
        self._ai_search_btn.setText("开始检索 ✈")

    def _clear_ai_results(self) -> None:
        while self._ai_results_layout.count() > 1:  # 保留末尾 stretch
            item = self._ai_results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()

    # ================= 推荐流 =================

    def add_to_feed(self, papers: list[dict], label: str) -> int:
        """把任意检索结果推入推荐流（文献补充对话框等外部调用）。"""
        return self._manager.push_to_feed(papers, label)

    def _render_feed(self) -> None:
        while self._feed_layout.count() > 1:  # 保留末尾 stretch
            item = self._feed_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.hide()
                w.setParent(None)
                w.deleteLater()
        entries = self._manager.feed_items()
        if not entries:
            empty = QLabel("暂无推荐。创建检索方向后，巡视发现的\n新文献会出现在这里。")
            empty.setObjectName("subtitleLabel")
            empty.setWordWrap(True)
            empty.setStyleSheet(
                "color: #718180; background-color: #f5f8f6; "
                "border: 1px solid #e1ebe7; border-radius: 12px; padding: 14px;")
            self._feed_layout.insertWidget(0, empty)
            self._feed_empty = empty
            return
        self._feed_empty = None
        for entry in entries:
            self._add_feed_card(entry)

    def _add_feed_card(self, entry: dict) -> None:
        if self._feed_empty is not None:
            self._feed_empty.hide()
            self._feed_empty.setParent(None)
            self._feed_empty.deleteLater()
            self._feed_empty = None
        card = ScoutCard(entry)
        card.ignore_requested.connect(self._on_ignore_feed)
        # 新卡片插到最前（stretch 之前）
        self._feed_layout.insertWidget(0, card)

    def _on_scout_results(self, topic_name: str, entries: list) -> None:
        for entry in entries:
            self._add_feed_card(entry)

    def _on_ignore_feed(self, entry_id: str) -> None:
        self._manager.ignore_feed_item(entry_id)
        sender = self.sender()
        if sender is not None:
            sender.setParent(None)
            sender.deleteLater()
        if not self._manager.feed_items():
            self._render_feed()

    def _on_clear_feed(self) -> None:
        if not self._manager.feed_items():
            return
        r = QMessageBox.question(
            self, "清空推荐流", "确定清空全部推荐记录？（不影响已保存的检索方向）",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if r == QMessageBox.StandardButton.Yes:
            self._manager.clear_feed()
            self._render_feed()

    def _feed_papers(self) -> list[dict]:
        return [e.get("paper", {}) for e in self._manager.feed_items()]

    def _on_export_ris(self) -> None:
        papers = self._feed_papers()
        if not papers:
            QMessageBox.information(self, "提示", "推荐流中没有可导出的文献。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 RIS", "scout_papers.ris", "RIS 文件 (*.ris)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write(papers_to_ris(papers))
            QMessageBox.information(
                self, "导出成功",
                f"已导出 {len(papers)} 篇文献。\n在 Zotero 中：文件 → 导入，选择该 RIS 文件即可。")
        except OSError as e:
            QMessageBox.critical(self, "导出失败", str(e))

    def _on_export_csv(self) -> None:
        papers = self._feed_papers()
        if not papers:
            QMessageBox.information(self, "提示", "推荐流中没有可导出的文献。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 CSV", "scout_papers.csv", "CSV 文件 (*.csv)")
        if not path:
            return
        try:
            save_csv(path, papers)
            QMessageBox.information(self, "导出成功", f"已导出 {len(papers)} 篇文献。")
        except OSError as e:
            QMessageBox.critical(self, "导出失败", str(e))
