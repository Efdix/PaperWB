"""检索工作台 —— 自然语言 AI 检索 + 定时文献巡视。

两栏布局（左检索右巡视）::

    ┌──────────────────────────────┬──────────────────┐
    │ AI 检索（主区）                 │ 定时巡视          │
    │ 自然语言 → 三源检索+两轮闭环     │  方向卡片 + 新方向 │
    │ 按库推荐 / 检索结果卡片          │ ──────────────   │
    │                              │  巡视结果/推荐流   │
    └──────────────────────────────┴──────────────────┘

巡视方向的设置与巡视结果在同一栏内上下相邻；职责划分：本工作台只负责
文献检索与巡视，单篇论文问答与全库跨文献问答都在阅读工作台的
「论文问答」侧栏（本篇论文 / 全文献库 两个页签）。
"""

from __future__ import annotations

import urllib.parse
from datetime import datetime
from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, QThread, QUrl, Signal
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QFrame, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QScrollArea, QSpinBox, QSplitter, QTextEdit,
    QVBoxLayout, QWidget,
)

from ..core.library_recommender import (
    LibraryRecommendWorker, build_seeds,
)
from ..core.literature_scout import (
    MAX_INTERVAL_HOURS, ScoutManager, ScoutTopic, papers_to_ris, save_csv,
)
from ..utils.config import get_easyscholar_api_key
from ..utils.threads import track

if TYPE_CHECKING:
    from ..core.llm_client import LLMClient
    from ..core.zotero_parser import ZoteroLibrary


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
# 巡视结果卡片
# ============================================================

class ScoutCard(QFrame):
    """单条巡视发现的文献卡片。"""

    ignore_requested = Signal(str)
    translate_requested = Signal(dict)   # 携带 paper dict

    def __init__(self, entry: dict, parent=None):
        super().__init__(parent)
        self.setObjectName("scoutCard")
        self._entry = entry
        p = entry.get("paper", {})

        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        self._layout = layout

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
        self._chips_layout = chips
        topic_chip = QLabel(entry.get("topic", ""))
        topic_chip.setObjectName("feedChip")
        topic_chip.setProperty("kind", "topic")
        chips.addWidget(topic_chip)
        if p.get("in_library"):
            lib_chip = QLabel("已在库中")
            lib_chip.setObjectName("feedChip")
            lib_chip.setProperty("kind", "library")
            lib_chip.setToolTip("这篇文献与本地 Zotero 库中已有条目匹配")
            chips.addWidget(lib_chip)
        cited = 0
        try:
            cited = int(p.get("cited_by") or 0)
        except (TypeError, ValueError):
            cited = 0
        if cited > 0:
            cited_chip = QLabel(f"被引 {cited}")
            cited_chip.setObjectName("feedChip")
            cited_chip.setToolTip("OpenAlex 统计的被引次数")
            chips.addWidget(cited_chip)
        linked = 0
        try:
            linked = int(entry.get("linked") or 0)
        except (TypeError, ValueError):
            linked = 0
        if linked > 0:
            linked_chip = QLabel(f"关联种子 {linked}")
            linked_chip.setObjectName("feedChip")
            linked_chip.setToolTip("与推荐源集合中 3 篇以上文献存在引文关联" if linked >= 3
                                   else "与推荐源集合文献的引文关联数")
            chips.addWidget(linked_chip)
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

        self._trans_label: QLabel | None = None
        self._trans_box: QWidget | None = None
        self._trans_btn: QPushButton | None = None
        self._if_chip: QLabel | None = None

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
        trans_btn = QPushButton("🌐 翻译")
        trans_btn.setObjectName("secondaryBtn")
        trans_btn.setToolTip("用纯文本 LLM 接口翻译标题与摘要")
        trans_btn.clicked.connect(self._on_translate_clicked)
        self._trans_btn = trans_btn
        ignore_btn = QPushButton("忽略")
        ignore_btn.setObjectName("softBtn")
        ignore_btn.setToolTip("从推荐流移除（之后不再重复推送）")
        ignore_btn.clicked.connect(
            lambda: self.ignore_requested.emit(entry.get("id", "")))
        btns.addWidget(pubmed_btn)
        btns.addWidget(cite_btn)
        btns.addWidget(trans_btn)
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

    def _on_translate_clicked(self) -> None:
        if self._trans_box is not None:
            self._toggle_translation()
            return
        self.translate_requested.emit(self._entry.get("paper", {}))

    def _toggle_translation(self) -> None:
        """收起已展示的译文（再次点击翻译按钮时）。"""
        if self._trans_box is not None:
            self._trans_box.hide()
            self._trans_box.setParent(None)
            self._trans_box.deleteLater()
            self._trans_box = None
            self._trans_label = None
        if self._trans_btn is not None:
            self._trans_btn.setText("🌐 翻译")

    def set_translation(self, text: str) -> None:
        """插入/更新译文块（摘要下方、按钮行上方）。"""
        if self._trans_box is None:
            self._trans_box = QWidget()
            tbox = QVBoxLayout(self._trans_box)
            tbox.setContentsMargins(0, 0, 0, 0)
            tbox.setSpacing(3)
            self._trans_label = QLabel()
            self._trans_label.setObjectName("subtitleLabel")
            self._trans_label.setWordWrap(True)
            self._trans_label.setStyleSheet(
                "background-color: #f0f6fb; border: 1px solid #d5e5f2; "
                "border-radius: 8px; padding: 6px 8px;")
            tbox.addWidget(self._trans_label)
            # 译文块插在按钮行（layout 末尾）之前
            self._layout.insertWidget(self._layout.count() - 1, self._trans_box)
        if self._trans_label is not None:
            self._trans_label.setText(text)
            self._trans_label.setToolTip(text)
        self._trans_box.show()
        if self._trans_btn is not None:
            self._trans_btn.setText("收起译文")
            self._trans_btn.setToolTip("收起译文")

    def set_impact(self, text: str) -> None:
        """影响因子查询完成后动态插入/更新 IF chip（插入 stretch 之前）。"""
        if text:
            text = text.strip()
        if not text:
            return
        if getattr(self, "_if_chip", None) is not None:
            self._if_chip.setText(text)
            self._if_chip.setToolTip("数据来源 EasyScholar")
            return
        chip = QLabel(text)
        chip.setObjectName("feedChip")
        chip.setToolTip("数据来源 EasyScholar")
        # 插到 stretch 之前
        self._chips_layout.insertWidget(self._chips_layout.count() - 1, chip)
        self._if_chip = chip


class CardTranslateWorker(QThread):
    """结果卡片翻译：标题+摘要 → 中文，纯文本 LLM 接口（chat_sync）。"""

    finished = Signal(str, str)   # (paper_id, 译文)
    error = Signal(str, str)      # (paper_id, 错误信息)

    def __init__(self, client: "LLMClient", paper_id: str, text: str, parent=None):
        super().__init__(parent)
        self._client = client
        self._paper_id = paper_id
        self._text = text

    def run(self) -> None:
        try:
            result = self._client.chat_sync([
                {"role": "system", "content": (
                    "你是一位学术论文专业翻译，精通中英双语与科研写作。请将用户提供的英文文献标题与摘要译成中文。\n\n"
                    "翻译要求：\n1. 术语准确，符合科研习惯表达\n"
                    "2. 标题单独一行，摘要紧随其后，保持原结构\n"
                    "3. 只输出译文本身，不要添加任何解释、注释或原文"
                )},
                {"role": "user", "content": self._text},
            ])
            if not self.isInterruptionRequested():
                self.finished.emit(self._paper_id, result or "（空译文）")
        except Exception as e:  # noqa: BLE001
            if not self.isInterruptionRequested():
                self.error.emit(self._paper_id, str(e))


# ============================================================
# 主面板
# ============================================================

class WorkbenchPanel(QWidget):
    """检索工作台：AI 检索（左，主区）+ 定时巡视与结果（右，同栏相邻）。"""

    # 统计埋点：一次检索/推荐完成（结果数）、一次巡视完成（方向名、新增数）
    search_completed = Signal(int)
    scout_completed = Signal(str, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("workbenchPanel")
        self._library: "ZoteroLibrary | None" = None
        self._text_client: "LLMClient | None" = None
        self._items_snapshot: list = []
        self._collections: list[tuple[str, str]] = []

        self._ai_worker: "PaperSearchWorker | None" = None
        self._rec_worker: LibraryRecommendWorker | None = None
        self._ai_searcher = None  # 测试可注入假多源检索器
        self._feed_empty: QLabel | None = None
        self._running_topics: set[str] = set()
        # 卡片翻译与影响因子查询：worker 保活引用 + 目标卡片映射
        self._trans_workers: dict[str, CardTranslateWorker] = {}
        self._trans_cards: dict[str, "ScoutCard"] = {}
        self._if_worker: "QThread | None" = None
        self._if_cards: dict[str, "ScoutCard"] = {}
        self._pending_if_jobs: list[tuple[str, str]] = []
        # 按库推荐：级联集合选择链 + 集合树数据
        self._rec_combos: list[QComboBox] = []
        self._coll_nodes: dict[str, dict] = {}
        self._coll_roots: list[str] = []

        self._manager = ScoutManager(self)
        self._manager.topics_changed.connect(self._render_topics)
        self._manager.topic_running.connect(self._on_topic_running)
        self._manager.results_ready.connect(self._on_scout_results)

        self._setup_ui()
        self._manager.status_msg.connect(self._feed_status.setText)
        self._refresh_rec_controls([])
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
        eyebrow = QLabel("文献检索与巡视")
        eyebrow.setObjectName("eyebrowLabel")
        title_box.addWidget(eyebrow)
        title = QLabel("检索工作台")
        title.setObjectName("titleLabel")
        title_box.addWidget(title)
        header_layout.addLayout(title_box)
        subtitle = QLabel("自然语言多源检索与按库推荐，定向巡视新文献并自动滤除库内已有")
        subtitle.setObjectName("subtitleLabel")
        header_layout.addWidget(subtitle)
        header_layout.addStretch()

        self._scout_toggle = QPushButton("巡视面板")
        self._scout_toggle.setObjectName("paneToggle")
        self._scout_toggle.setCheckable(True)
        self._scout_toggle.setChecked(True)
        self._scout_toggle.setToolTip("显示或隐藏右侧巡视面板（方向与结果）")
        header_layout.addWidget(self._scout_toggle)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.setOpaqueResize(False)
        self._ai_panel = self._build_ai_search_panel()
        splitter.addWidget(self._ai_panel)
        self._scout_panel = self._build_scout_panel()
        splitter.addWidget(self._scout_panel)
        splitter.setSizes([720, 430])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, True)
        self._workbench_splitter = splitter
        self._scout_toggle.toggled.connect(self._set_scout_visible)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(header)
        layout.addWidget(splitter, 1)

    def _build_ai_search_panel(self) -> QFrame:
        """AI 检索主区：自然语言描述需求 → 自动生成检索式 → 多源检索。"""
        panel = QFrame()
        panel.setObjectName("aiSearchPanel")
        panel.setMinimumWidth(420)
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
        subtitle = QLabel("自然语言三源检索（OpenAlex / PubMed / arXiv）· 两轮闭环 · 自动滤除库内已有")
        subtitle.setObjectName("subtitleLabel")
        subtitle.setWordWrap(True)
        title_box.addWidget(subtitle)
        header.addLayout(title_box)
        header.addStretch()
        self._rec_mode_cb = QCheckBox("📚 按库推荐")
        self._rec_mode_cb.setToolTip(
            "勾选后切换为按库推荐：以某个 Zotero 集合（含其全部子级）的文献\n"
            "作为推荐源；取消勾选回到自然语言检索。两种模式互斥显示。")
        self._rec_mode_cb.toggled.connect(self._set_recommend_mode)
        header.addWidget(self._rec_mode_cb)
        v.addLayout(header)

        self._ai_input = QTextEdit()
        self._ai_input.setPlaceholderText(
            "描述你想找的文献，例如：\n"
            "· 近三年鸟类羽色发育中黑色素细胞分化的单细胞研究\n"
            "· 2024 年以来关于羽毛图案形成的机制综述\n\n"
            "两轮检索：先初检，AI 分析缺口后自动补充第二轮\n"
            "按 Ctrl+Enter 检索")
        self._ai_input.setMaximumHeight(120)
        self._ai_input.setMinimumHeight(64)
        self._ai_input.installEventFilter(self)
        v.addWidget(self._ai_input)

        self._ai_ctrl_widget = QWidget()
        ctrl = QHBoxLayout(self._ai_ctrl_widget)
        ctrl.setContentsMargins(0, 0, 0, 0)
        ctrl.setSpacing(8)
        self._filter_library_cb = QCheckBox("过滤库内已有")
        self._filter_library_cb.setToolTip(
            "勾选 = 从结果中剔除本地 Zotero 库中已有的文献；\n"
            "不勾选 = 保留全部结果，并在卡片上标注「已在库中」")
        ctrl.addWidget(self._filter_library_cb)
        ctrl.addStretch()
        self._ai_search_btn = QPushButton("开始检索 ✈")
        self._ai_search_btn.setObjectName("primaryBtn")
        self._ai_search_btn.setEnabled(False)
        self._ai_search_btn.clicked.connect(self._on_ai_search)
        ctrl.addWidget(self._ai_search_btn)
        v.addWidget(self._ai_ctrl_widget)

        # 模式二：按库推荐（级联集合选择，勾选头部复选框后显示，与检索互斥）
        self._rec_range_row = QWidget()
        rec_row = QHBoxLayout(self._rec_range_row)
        rec_row.setContentsMargins(0, 0, 0, 0)
        rec_row.setSpacing(8)
        rec_label = QLabel("推荐范围：")
        rec_label.setObjectName("subtitleLabel")
        rec_row.addWidget(rec_label)
        combos_host = QWidget()
        self._rec_combos_layout = QHBoxLayout(combos_host)
        self._rec_combos_layout.setContentsMargins(0, 0, 0, 0)
        self._rec_combos_layout.setSpacing(6)
        rec_row.addWidget(combos_host, 1)
        self._rec_btn = QPushButton("📚 按库推荐")
        self._rec_btn.setObjectName("primaryBtn")
        self._rec_btn.setEnabled(False)
        self._rec_btn.setToolTip("以所选范围（该级及其全部子级）的文献为种子：\n"
                                 "OpenAlex 引文图谱推荐 + AI 集合画像检索")
        self._rec_btn.clicked.connect(self._on_library_recommend)
        rec_row.addWidget(self._rec_btn)
        self._rec_range_row.setVisible(False)
        v.addWidget(self._rec_range_row)

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

    def _build_scout_panel(self) -> QWidget:
        """右栏巡视面板：方向管理在上，巡视结果（推荐流）在下，同栏相邻。"""
        container = QWidget()
        container.setMinimumWidth(340)
        v = QVBoxLayout(container)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(10)

        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setHandleWidth(6)
        splitter.setOpaqueResize(False)
        splitter.addWidget(self._build_topic_panel())
        splitter.addWidget(self._build_feed_panel())
        splitter.setSizes([300, 430])
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setCollapsible(0, True)
        splitter.setCollapsible(1, False)
        self._scout_splitter = splitter
        v.addWidget(splitter)
        return container

    def _build_topic_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("topicPanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 14, 12)
        v.setSpacing(8)

        title = QLabel("定时巡视")
        title.setObjectName("titleLabel")
        v.addWidget(title)
        subtitle = QLabel("按方向周期检索 PubMed · 自动滤除库内已有")
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

    def _build_feed_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("feedPanel")
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 14, 12)
        v.setSpacing(8)

        title = QLabel("巡视结果")
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

    def _set_scout_visible(self, visible: bool) -> None:
        self._scout_panel.setVisible(bool(visible))
        if visible:
            self._workbench_splitter.setSizes([max(600, self.width() - 470), 430])
        else:
            self._workbench_splitter.setSizes([max(600, self.width() - 30), 0])

    # ================= 依赖注入 =================

    def set_text_client(self, client: "LLMClient | None") -> None:
        self._text_client = client
        self._manager.set_llm_client(client)
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
        self._build_collections()
        self._build_collection_tree()
        pool = self._build_pool()
        self._manager.set_match_pool(pool)
        self._manager.start()
        self._refresh_rec_controls(pool)

    def on_zotero_changed(self) -> None:
        """Zotero 周期同步发现变化后：重建比对池。"""
        self.set_zotero_library(self._library)

    def reload_storage(self) -> bool:
        """数据根目录切换后重新绑定巡视和推荐流。"""
        for attr in ("_ai_worker", "_rec_worker"):
            worker = getattr(self, attr)
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(3_000):
                    self._feed_status.setText("已有检索任务正在退出，稍后再切换数据目录")
                    return False
            setattr(self, attr, None)
        if not self._manager.reload_storage():
            self._feed_status.setText("已有巡视任务正在退出，稍后再切换数据目录")
            return False
        self._render_topics()
        self._render_feed()
        if self._library is not None:
            pool = self._build_pool()
            self._manager.set_match_pool(pool)
            self._manager.start()
            self._refresh_rec_controls(pool)
        return True

    def shutdown(self) -> None:
        """停止巡视定时器并请求中断后台线程（关窗时调用）。"""
        self._manager.shutdown()
        for w in (self._ai_worker, self._rec_worker, self._if_worker):
            if w is not None and w.isRunning():
                w.requestInterruption()
        for w in self._trans_workers.values():
            if w.isRunning():
                w.requestInterruption()

    def has_busy_workers(self) -> bool:
        ai = self._ai_worker is not None and self._ai_worker.isRunning()
        rec = self._rec_worker is not None and self._rec_worker.isRunning()
        return ai or rec or self._manager.has_busy_workers()
    # ================= 库快照工具 =================

    def _build_collections(self) -> None:
        """集合下拉数据：[(key, '父 / 子' 显示名)]，同级按名称排序。"""
        self._collections = []
        lib = self._library
        if lib is None:
            return
        by_id = {c.collection_id: c for c in lib.collections}
        for c in sorted(lib.get_collections_tree(), key=lambda c: c.name or ""):
            self._walk_collections(c, by_id)

    def _walk_collections(self, coll, by_id: dict) -> None:
        """递归收集 (key, 显示名)，同级按名称排序（01 → 02 → 03）。"""
        names = [coll.name or ""]
        pid = coll.parent_id
        seen = {coll.collection_id}
        while pid is not None and pid in by_id and pid not in seen:
            parent = by_id[pid]
            names.append(parent.name or "")
            seen.add(pid)
            pid = parent.parent_id
        self._collections.append((coll.key, " / ".join(reversed(names))))
        children = [by_id[cid] for cid in coll.child_ids if cid in by_id]
        for child in sorted(children, key=lambda c: c.name or ""):
            self._walk_collections(child, by_id)

    def _build_collection_tree(self) -> None:
        """按库推荐的级联数据：根 key 列表 + key→{name, children:[key]}，同级按名称排序。"""
        self._coll_nodes = {}
        self._coll_roots = []
        lib = self._library
        if lib is None:
            return
        by_id = {c.collection_id: c for c in lib.collections}

        def walk(c) -> str:
            self._coll_nodes[c.key] = {"name": c.name or "", "children": []}
            children = [by_id[cid] for cid in c.child_ids if cid in by_id]
            for child in sorted(children, key=lambda c: c.name or ""):
                self._coll_nodes[c.key]["children"].append(walk(child))
            return c.key

        for c in sorted(lib.get_collections_tree(), key=lambda c: c.name or ""):
            self._coll_roots.append(walk(c))

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

    # ================= 检索方向 =================

    def eventFilter(self, obj, event) -> bool:
        if obj is self._ai_input \
                and event.type() == QEvent.Type.KeyPress:
            if (event.key() == Qt.Key.Key_Return
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._on_ai_search()
                return True
        return super().eventFilter(obj, event)

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

    # ================= AI 检索 / 按库推荐 =================

    def _set_recommend_mode(self, on: bool) -> None:
        """模式互斥切换：自然语言检索 ⇄ 按库推荐。"""
        self._ai_input.setVisible(not on)
        self._ai_ctrl_widget.setVisible(not on)
        self._rec_range_row.setVisible(on)
        if on:
            self._ai_log.setText("选择推荐范围（任一级即含其全部子级）后点击「按库推荐」")
        else:
            self._ai_log.setText("")

    def _refresh_rec_controls(self, pool: list) -> None:
        """按库推荐控件：级联下拉重建 + 按钮可用性。"""
        self._truncate_rec_combos(0)
        self._append_rec_combo(0, "")
        self._rec_btn.setEnabled(bool(pool) and self._rec_worker is None)

    def _truncate_rec_combos(self, keep: int) -> None:
        """移除第 keep 级及更深的集合下拉。"""
        while len(self._rec_combos) > keep:
            combo = self._rec_combos.pop()
            try:
                combo.currentIndexChanged.disconnect()
            except (RuntimeError, TypeError):
                pass
            self._rec_combos_layout.removeWidget(combo)
            combo.setParent(None)
            combo.deleteLater()

    def _append_rec_combo(self, level: int, parent_key: str) -> None:
        """追加第 level 级集合下拉。

        第 0 级：全库 + 顶层集合；更深层：「（含全部子级）」+ 子集合。
        """
        combo = QComboBox()
        combo.setMinimumWidth(140)
        if level == 0:
            combo.addItem("全库", "")
            for k in self._coll_roots:
                combo.addItem(self._coll_nodes[k]["name"], k)
        else:
            combo.addItem("（含全部子级）", "")
            for k in self._coll_nodes.get(parent_key, {}).get("children", []):
                combo.addItem(self._coll_nodes[k]["name"], k)
        combo.currentIndexChanged.connect(
            lambda _idx, lv=level: self._on_rec_combo_changed(lv))
        self._rec_combos_layout.addWidget(combo)
        self._rec_combos.append(combo)

    def _on_rec_combo_changed(self, level: int) -> None:
        """某级选择变化：选中项有子集合则下钻一级，否则收起更深层。"""
        if level >= len(self._rec_combos):
            return
        key = self._rec_combos[level].currentData() or ""
        self._truncate_rec_combos(level + 1)
        if key and self._coll_nodes.get(key, {}).get("children"):
            self._append_rec_combo(level + 1, key)

    def _current_rec_key(self) -> str:
        """生效集合 key = 链上最深的非空选择（'' = 全库）。

        选中任一级即含其全部子级（比对池条目的 collections 含祖先键）。
        """
        for combo in reversed(self._rec_combos):
            key = combo.currentData() or ""
            if key:
                return key
        return ""

    def _current_rec_label(self) -> str:
        """日志用：级联选择路径名（如「动物学 / 鸟类」）。"""
        parts = [self._coll_nodes.get(combo.currentData(), {}).get("name", "")
                 for combo in self._rec_combos if combo.currentData()]
        return " / ".join(p for p in parts if p) if parts else "全库"

    def _on_ai_search(self) -> None:
        if self._ai_worker is not None and self._ai_worker.isRunning():
            return
        if self._rec_worker is not None and self._rec_worker.isRunning():
            self._ai_log.setText("按库推荐正在进行，请稍候…")
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
        self._rec_btn.setEnabled(False)
        self._ai_log.setText("正在生成检索方案...")

        from ..core.literature_search import PaperSearchWorker
        worker = PaperSearchWorker(
            text, client=self._text_client, pool=self._build_pool(), limit=10,
            searcher=self._ai_searcher, rounds=2,
            filter_library=self._filter_library_cb.isChecked())
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
        self._render_search_papers(papers, "AI 检索")
        self.search_completed.emit(len(papers))

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
        self._rec_btn.setEnabled(self._library is not None)

    def _on_library_recommend(self) -> None:
        """按库推荐：以所选集合（含子集合）文献为种子的两路推荐。"""
        if self._rec_worker is not None and self._rec_worker.isRunning():
            return
        if self._ai_worker is not None and self._ai_worker.isRunning():
            self._ai_log.setText("AI 检索正在进行，请稍候…")
            return
        pool = self._build_pool()
        collection_key = self._current_rec_key()
        seeds = build_seeds(pool, collection_key)
        if not seeds:
            QMessageBox.information(
                self, "无可用种子",
                "所选范围内没有可用文献（种子需要有 DOI 或标题）。\n"
                "请确认 Zotero 库已配置且集合非空。")
            return
        self._rec_btn.setEnabled(False)
        self._rec_btn.setText("推荐中...")
        self._ai_search_btn.setEnabled(False)
        self._ai_log.setText(
            f"按库推荐 · {self._current_rec_label()} · 种子 {len(seeds)} 篇")

        worker = LibraryRecommendWorker(
            seeds, pool, client=self._text_client, limit=20,
            searcher=self._ai_searcher)
        track(worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        self._rec_worker = worker
        worker.log.connect(self._on_ai_log)
        worker.results_ready.connect(self._on_rec_results)
        worker.error.connect(self._on_rec_error)
        worker.done.connect(self._on_rec_done)
        worker.start()

    def _on_rec_results(self, papers: list) -> None:
        if self.sender() is not self._rec_worker:
            return
        self._render_search_papers(papers, "按库推荐")
        self.search_completed.emit(len(papers))

    def _on_rec_error(self, err: str) -> None:
        if self.sender() is not self._rec_worker:
            return
        self._ai_log.setText(f"按库推荐失败：{err}")

    def _on_rec_done(self) -> None:
        if self.sender() is not self._rec_worker:
            return
        self._rec_worker = None
        self._rec_btn.setText("📚 按库推荐")
        self._rec_btn.setEnabled(self._library is not None)
        self._ai_search_btn.setEnabled(self._text_client is not None)

    def _render_search_papers(self, papers: list, chip_fallback: str) -> None:
        """把检索/推荐结果渲染为卡片（chip 区分来源，linked 带关联种子数）。"""
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
            chip = p.get("rec_source") or chip_fallback
            entry = {
                "id": f"{p.get('pmid') or p.get('arxiv_id') or p.get('doi') or ''}@{chip}",
                "topic": chip,
                "added_at": datetime.now().isoformat(timespec="seconds"),
                "paper": p,
                "linked": p.get("linked", 0),
            }
            card = ScoutCard(entry)
            card.ignore_requested.connect(self._on_ignore_feed)
            card.translate_requested.connect(self._on_translate_card)
            self._register_card(card)
            self._ai_results_layout.insertWidget(
                self._ai_results_layout.count() - 1, card)
        self._flush_if_queries()

    def _clear_ai_results(self) -> None:
        while self._ai_results_layout.count() > 1:  # 保留末尾 stretch
            item = self._ai_results_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                if isinstance(w, ScoutCard):
                    self._unregister_card(w)
                w.hide()
                w.setParent(None)
                w.deleteLater()

    # ================= 巡视结果（推荐流） =================

    def add_to_feed(self, papers: list[dict], label: str) -> int:
        """把任意检索结果推入推荐流（文献补充对话框等外部调用）。"""
        return self._manager.push_to_feed(papers, label)

    def _render_feed(self) -> None:
        while self._feed_layout.count() > 1:  # 保留末尾 stretch
            item = self._feed_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                if isinstance(w, ScoutCard):
                    self._unregister_card(w)
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
        card.translate_requested.connect(self._on_translate_card)
        self._register_card(card)
        self._flush_if_queries()
        # 新卡片插到最前（stretch 之前）
        self._feed_layout.insertWidget(0, card)

    def _on_scout_results(self, topic_name: str, entries: list) -> None:
        for entry in entries:
            self._add_feed_card(entry)
        self.scout_completed.emit(topic_name, len(entries))

    def _on_ignore_feed(self, entry_id: str) -> None:
        self._manager.ignore_feed_item(entry_id)
        sender = self.sender()
        if sender is not None:
            self._unregister_card(sender)
            sender.setParent(None)
            sender.deleteLater()
        if not self._manager.feed_items():
            self._render_feed()

    # ================= 卡片翻译 + 影响因子 =================

    @staticmethod
    def _paper_card_id(paper: dict) -> str:
        """卡片标识：pmid / arxiv_id / doi 优先取有值的。"""
        return (paper.get("pmid") or paper.get("arxiv_id")
                or paper.get("doi") or "").strip()

    def _register_card(self, card: "ScoutCard") -> None:
        """登记卡片：翻译映射始终登记；影响因子仅密钥已配置且期刊非空时登记。"""
        paper = card._entry.get("paper", {})
        pid = self._paper_card_id(paper)
        if not pid:
            return
        self._trans_cards[pid] = card
        if not get_easyscholar_api_key():
            return
        journal = (paper.get("journal") or "").strip()
        if journal:
            self._pending_if_jobs.append((pid, journal))
            self._if_cards[pid] = card

    def _unregister_card(self, card: "ScoutCard") -> None:
        """卡片销毁前从目标映射移除（避免回填已删除卡片）。"""
        for mapping in (self._trans_cards, self._if_cards):
            for key, c in list(mapping.items()):
                if c is card:
                    mapping.pop(key, None)

    def _flush_if_queries(self) -> None:
        """启动一次影响因子批量查询（仅当已登记任务且密钥已配置）。"""
        if not self._pending_if_jobs:
            return
        if self._if_worker is not None and self._if_worker.isRunning():
            return
        key = get_easyscholar_api_key()
        if not key:
            self._pending_if_jobs.clear()
            return
        from ..core.easyscholar import ImpactFactorWorker
        worker = ImpactFactorWorker(self._pending_if_jobs, key)
        track(worker)  # 运行期间保活
        self._if_worker = worker
        self._pending_if_jobs = []
        worker.results_ready.connect(self._on_if_results)
        worker.done.connect(self._on_if_done)
        worker.start()

    def _on_if_results(self, results: dict) -> None:
        for pid, data in (results or {}).items():
            card = self._if_cards.get(pid)
            if card is None:
                continue
            parts = []
            if data.get("if"):
                parts.append(f"IF {data['if']}")
            if data.get("sci5"):
                parts.append(f"5年IF {data['sci5']}")
            if parts:
                card.set_impact(" · ".join(parts))

    def _on_if_done(self) -> None:
        self._if_worker = None
        # 批量查询期间新登记的任务（如巡视新结果）继续处理
        self._flush_if_queries()

    def _on_translate_card(self, paper: dict) -> None:
        if self._text_client is None:
            QMessageBox.warning(
                self, "未配置纯文本接口",
                "翻译需要纯文本 LLM 接口。请先在「设置 → API 接口设置」"
                "中配置纯文本接口。")
            return
        pid = self._paper_card_id(paper)
        if not pid:
            QMessageBox.warning(self, "无法翻译", "这篇文献缺少可识别的标识。")
            return
        text = "标题：" + (paper.get("title") or "").strip()
        abstract = (paper.get("abstract") or "").strip()
        if abstract:
            text += "\n\n摘要：" + abstract
        worker = CardTranslateWorker(self._text_client, pid, text)
        track(worker)  # 运行期间保活，杜绝运行中 QThread 被 GC 销毁
        self._trans_workers[pid] = worker
        worker.finished.connect(self._on_card_translated)
        worker.error.connect(self._on_card_translate_error)
        worker.start()

    def _on_card_translated(self, paper_id: str, translation: str) -> None:
        self._trans_workers.pop(paper_id, None)
        card = self._trans_cards.get(paper_id)
        if card is not None:
            card.set_translation(translation)

    def _on_card_translate_error(self, paper_id: str, err: str) -> None:
        self._trans_workers.pop(paper_id, None)
        card = self._trans_cards.get(paper_id)
        if card is not None:
            card.set_translation(f"翻译失败：{err}")

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
