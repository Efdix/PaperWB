"""库内问答面板 —— 面向整个 Zotero 库的跨文献 RAG 问答。

作为阅读工作台「论文问答」侧栏的第二个页签（全文献库）：
单篇论文的问答在 ChatPanel（本篇论文），本面板回答关于整个文献库的问题。
回答带 [n] 角标，参考文献卡片点击后在阅读器中直接打开对应 PDF。

数据流::

    ZoteroLibrary.get_all_items()
      → IndexBuildWorker 后台构建元数据 + PDF 全文索引（lib_index/fulltext.json）
      → LibraryQAWorker 检索证据并流式调用解析接口
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from PySide6.QtCore import QEvent, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox, QFrame, QHBoxLayout, QLabel, QMessageBox, QPushButton,
    QScrollArea, QTextEdit, QVBoxLayout, QWidget,
)

from ..core.library_qa import LibraryQAEngine
from ..utils.config import load_config
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
# 参考文献卡片（问答回答下方）
# ============================================================

class ReferenceListCard(QFrame):
    """回答引用的库内文献列表，点击可在阅读器中打开 PDF。"""

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

class LibraryQAPanel(QWidget):
    """库内问答面板：全库索引 + 跨文献问答（阅读工作台「全文献库」页签）。

    信号:
        open_pdf_requested(str): 用户点击参考文献，请求在阅读器中打开 PDF。
        preparse_toggled(bool): 「后台建库解析」开关变化（主窗口持久化并启停）。
        index_built(): 全库索引构建完成（主窗口据此启动后台预解析）。
    """

    open_pdf_requested = Signal(str)
    preparse_toggled = Signal(bool)
    index_built = Signal()
    qa_asked = Signal()  # 统计埋点：一次全库问答发起

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("qaPanel")
        self._library: "ZoteroLibrary | None" = None
        self._text_client: "LLMClient | None" = None
        self._engine = LibraryQAEngine()
        self._engine_ready = False
        self._items_snapshot: list = []
        self._key_by_pdf: dict[str, str] = {}

        self._qa_worker: LibraryQAWorker | None = None
        self._index_worker: IndexBuildWorker | None = None
        self._qa_busy = False
        self._qa_history: list[dict] = []
        self._current_ai_bubble: ChatBubble | None = None
        self._welcome: QLabel | None = None

        self._setup_ui()
        self._insert_welcome()

    # ================= UI 构建 =================

    def _setup_ui(self) -> None:
        v = QVBoxLayout(self)
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
        self._preparse_cb = QCheckBox("后台建库解析")
        self._preparse_cb.setToolTip(
            "空闲时在后台本地解析全库 PDF（零 token，用户操作优先）：\n"
            "· 库问答证据升级为去噪、带章节的结构化正文\n"
            "· 已预解析的文献点开秒开\n"
            "· 跨页段落在你打开阅读时才做一次 LLM 精修")
        self._preparse_cb.setChecked(
            load_config().get("preparse_enabled", True))
        self._preparse_cb.toggled.connect(self.preparse_toggled.emit)
        ctrl.addWidget(self._preparse_cb)
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

        self._preparse_status = QLabel("")
        self._preparse_status.setObjectName("subtitleLabel")
        self._preparse_status.setWordWrap(True)
        self._preparse_status.hide()
        input_layout.addWidget(self._preparse_status)

        v.addWidget(input_frame)

    # ================= 依赖注入 =================

    def set_text_client(self, client: "LLMClient | None") -> None:
        self._text_client = client
        self._apply_ask_state()

    def set_zotero_library(self, library: "ZoteroLibrary | None") -> None:
        self._library = library
        items: list = []
        if library is not None and library.is_available:
            try:
                items = library.get_all_items()
            except Exception:  # noqa: BLE001
                items = []
        self._items_snapshot = items
        self._key_by_pdf = {
            (getattr(it, "pdf_path", "") or ""): it.key
            for it in items if getattr(it, "pdf_path", "")
        }
        self._engine_ready = False
        self._start_index_build()
        self._apply_ask_state()

    def on_zotero_changed(self) -> None:
        """Zotero 周期同步发现变化后：增量刷新索引。"""
        self.set_zotero_library(self._library)

    def reload_storage(self) -> bool:
        """数据根目录切换后重新绑定索引。"""
        for attr in ("_index_worker", "_qa_worker"):
            worker = getattr(self, attr)
            if worker is not None and worker.isRunning():
                worker.requestInterruption()
                if not worker.wait(3_000):
                    self._qa_status.setText("已有问答任务正在退出，稍后再切换数据目录")
                    return False
            setattr(self, attr, None)
        self._qa_busy = False
        self._engine = LibraryQAEngine()
        self._engine_ready = False
        self._on_clear_chat()
        if self._library is not None:
            self._start_index_build()
        return True

    def shutdown(self) -> None:
        """请求中断后台线程（关窗时调用）。"""
        for w in (self._index_worker, self._qa_worker):
            if w is not None and w.isRunning():
                w.requestInterruption()

    def has_busy_workers(self) -> bool:
        qa = self._qa_worker is not None and self._qa_worker.isRunning()
        idx = self._index_worker is not None and self._index_worker.isRunning()
        return qa or idx

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
        # 先收口预解析遗留的单篇刷新，避免本次构建从盘上读回旧状态
        self._engine.flush()
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
        self.index_built.emit()

    # ---- 后台预解析协作（主窗口接线） ----

    def item_key_for_pdf(self, pdf_path: str) -> str:
        """PDF 路径 → Zotero 条目 key（快照映射，供单篇索引刷新）。"""
        return self._key_by_pdf.get(pdf_path or "", "")

    def refresh_engine_item(self, key: str) -> bool:
        """单篇升级全文索引（预解析/阅读精修完成时由主窗口调用）。"""
        if not key:
            return False
        return self._engine.refresh_item(key)

    def flush_engine(self, reindex: bool = True) -> None:
        """落盘（并可选重建检索器）累积的单篇索引刷新。"""
        self._engine.flush(reindex=reindex)

    def set_preparse_status(self, text: str) -> None:
        """后台建库解析状态行；空串隐藏。"""
        if text:
            self._preparse_status.setText(text)
            self._preparse_status.show()
        else:
            self._preparse_status.hide()

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
        if obj is self._ask_input \
                and event.type() == QEvent.Type.KeyPress:
            if (event.key() == Qt.Key.Key_Return
                    and event.modifiers() & Qt.KeyboardModifier.ControlModifier):
                self._on_ask()
                return True
        return super().eventFilter(obj, event)

    def _insert_welcome(self) -> None:
        if self._welcome is not None:
            return
        welcome = QLabel(
            "欢迎使用库内问答\n\n"
            "这里可以向你的整个 Zotero 文献库提问，例如：\n"
            "· 这两篇关于 X 的研究结论是否矛盾？\n"
            "· 我的库里有哪些使用单细胞测序的文献？\n"
            "· 总结一下 2023 年以来关于 Y 方向的进展\n\n"
            "回答会标注 [n] 角标，点击参考文献可直接在阅读器中打开原文。\n"
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
        self.qa_asked.emit()

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
