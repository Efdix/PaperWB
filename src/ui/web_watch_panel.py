"""网页追踪面板 —— 监控机构主页/通知页的内容变化，可推送邮件提醒。

合规设计（与 core/web_watch.py 一致）：低频轮询、遵守 robots.txt、
自标识 UA；面板文案明确提示用户只监控公开页面。
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QFrame, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea, QSpinBox,
    QTextEdit, QVBoxLayout, QWidget,
)

from ..core.web_watch import (
    MAX_INTERVAL_HOURS, MIN_INTERVAL_HOURS, WebWatchManager, WatchedPage,
    is_valid_url,
)


class WatchEditDialog(QDialog):
    """新建/编辑监控网页。"""

    def __init__(self, page: WatchedPage | None = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("新建网页监控" if page is None else "编辑网页监控")
        self.setMinimumSize(540, 430)
        self._base = page

        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 20, 24, 18)
        layout.setSpacing(10)

        form = QFormLayout()
        form.setSpacing(8)

        self._name_edit = QLineEdit(page.name if page else "")
        self._name_edit.setPlaceholderText("例如：昆明动物所通知公告")
        form.addRow("名称：", self._name_edit)

        self._url_edit = QLineEdit(page.url if page else "")
        self._url_edit.setPlaceholderText("https://kiz.cas.cn/")
        form.addRow("网页地址：", self._url_edit)

        self._keywords_edit = QTextEdit()
        self._keywords_edit.setPlaceholderText(
            "可选。每行一个关键词，只关注含关键词的更新内容，例如：\n"
            "招聘\n招生\n基金\n留空 = 关注全部更新")
        self._keywords_edit.setMaximumHeight(90)
        if page and page.keywords:
            self._keywords_edit.setPlainText("\n".join(page.keywords))
        form.addRow("关注关键词：", self._keywords_edit)

        interval_row = QWidget()
        interval_lay = QHBoxLayout(interval_row)
        interval_lay.setContentsMargins(0, 0, 0, 0)
        self._interval_spin = QSpinBox()
        self._interval_spin.setRange(MIN_INTERVAL_HOURS, MAX_INTERVAL_HOURS)
        self._interval_spin.setSuffix(" 小时")
        self._interval_spin.setValue(page.interval_hours if page else 6)
        self._interval_spin.setToolTip(
            "自动检查间隔（最短 1 小时）。程序按规范低频访问，"
            "每次检查只发一个请求。")
        interval_lay.addWidget(self._interval_spin)
        interval_lay.addStretch()
        form.addRow("检查周期：", interval_row)

        self._email_cb = QCheckBox("有更新时发邮件提醒（需在设置中配置邮箱）")
        self._email_cb.setChecked(page.notify_email if page else True)
        form.addRow("", self._email_cb)

        self._enabled_cb = QCheckBox("启用监控")
        self._enabled_cb.setChecked(page.enabled if page else True)
        form.addRow("", self._enabled_cb)

        layout.addLayout(form)

        compliance = QLabel(
            "♻️ 合规说明：仅定时读取公开网页（遵守 robots.txt、自标识访问），"
            "不登录、不绕过任何访问限制；请只监控对公众开放的页面。")
        compliance.setObjectName("subtitleLabel")
        compliance.setWordWrap(True)
        layout.addWidget(compliance)
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
            QMessageBox.warning(self, "提示", "请填写监控名称。")
            return
        url = self._url_edit.text().strip()
        if not is_valid_url(url):
            QMessageBox.warning(self, "提示", "请填写有效的网页地址（http:// 或 https:// 开头）。")
            return
        self.accept()

    def page_result(self) -> WatchedPage:
        base = self._base
        return WatchedPage(
            id=base.id if base else datetime.now().strftime("w%H%M%S%f"),
            name=self._name_edit.text().strip(),
            url=self._url_edit.text().strip(),
            keywords=[line.strip() for line in
                      self._keywords_edit.toPlainText().splitlines() if line.strip()],
            interval_hours=self._interval_spin.value(),
            enabled=self._enabled_cb.isChecked(),
            notify_email=self._email_cb.isChecked(),
            last_check=base.last_check if base else "",
            last_change=base.last_change if base else "",
            last_new=base.last_new if base else 0,
            last_status=base.last_status if base else "",
        )


class WatchCard(QFrame):
    """单个监控网页的卡片。"""

    check_requested = Signal(str)
    edit_requested = Signal(str)
    delete_requested = Signal(str)
    toggle_requested = Signal(str, bool)
    open_requested = Signal(str)

    def __init__(self, page: WatchedPage, parent=None):
        super().__init__(parent)
        self.setObjectName("topicCard")
        self._page = page
        self._checking = False

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
            lambda checked: self.toggle_requested.emit(self._page.id, checked))
        top.addWidget(self._name_label, 1)
        top.addWidget(self._toggle_btn)
        layout.addLayout(top)

        self._url_label = QLabel()
        self._url_label.setObjectName("subtitleLabel")
        self._url_label.setWordWrap(True)
        layout.addWidget(self._url_label)

        self._meta_label = QLabel()
        self._meta_label.setObjectName("subtitleLabel")
        self._meta_label.setWordWrap(True)
        layout.addWidget(self._meta_label)

        btns = QHBoxLayout()
        btns.setSpacing(6)
        check_btn = QPushButton("立即检查")
        check_btn.setObjectName("secondaryBtn")
        check_btn.setToolTip("马上抓取一次该页面并与上次快照比对")
        check_btn.clicked.connect(lambda: self.check_requested.emit(self._page.id))
        open_btn = QPushButton("网页 ↗")
        open_btn.setObjectName("secondaryBtn")
        open_btn.setToolTip("在浏览器中打开该网页")
        open_btn.clicked.connect(lambda: self.open_requested.emit(self._page.url))
        edit_btn = QPushButton("✎")
        edit_btn.setObjectName("iconBtn")
        edit_btn.setToolTip("编辑监控")
        edit_btn.clicked.connect(lambda: self.edit_requested.emit(self._page.id))
        del_btn = QPushButton("✕")
        del_btn.setObjectName("iconBtn")
        del_btn.setToolTip("删除监控")
        del_btn.clicked.connect(lambda: self.delete_requested.emit(self._page.id))
        btns.addWidget(check_btn)
        btns.addWidget(open_btn)
        btns.addStretch()
        btns.addWidget(edit_btn)
        btns.addWidget(del_btn)
        layout.addLayout(btns)

        self._refresh_labels()

    def _refresh_labels(self) -> None:
        p = self._page
        suffix = " ⏳" if self._checking else ""
        self._name_label.setText((p.name or "（未命名）") + suffix)
        shown_url = p.url if len(p.url) <= 60 else p.url[:57] + "…"
        chips = []
        if p.keywords:
            chips.append("关键词: " + "；".join(p.keywords[:4]))
        if p.notify_email:
            chips.append("📧")
        self._url_label.setText(" · ".join([shown_url] + chips))
        last_check = p.last_check[:16].replace("T", " ") if p.last_check else "未检查"
        status = f" · {p.last_status}" if p.last_status else ""
        self._meta_label.setText(
            f"每 {p.interval_hours} 小时 · 上次检查 {last_check}{status}")
        self._toggle_btn.blockSignals(True)
        self._toggle_btn.setChecked(p.enabled)
        self._toggle_btn.setText("已启用" if p.enabled else "已停用")
        self._toggle_btn.blockSignals(False)

    def update_page(self, page: WatchedPage) -> None:
        self._page = page
        self._refresh_labels()

    def set_checking(self, checking: bool) -> None:
        if self._checking == checking:
            return
        self._checking = checking
        self._refresh_labels()


class WebWatchPanel(QWidget):
    """网页追踪面板：监控页管理 + 状态展示。

    信号:
        pages_changed(): 页面列表变化（供需要时刷新）。
    """

    pages_changed = Signal()

    def __init__(self, parent=None, watch_dir=None):
        super().__init__(parent)
        self.setObjectName("scoutSection")
        self._manager = WebWatchManager(self, watch_dir)
        self._manager.pages_changed.connect(self._on_pages_changed)
        self._manager.pages_changed.connect(self.pages_changed)
        self._manager.page_checked.connect(self._on_check_finished)
        self._manager.page_failed.connect(self._on_check_finished)
        self._cards: dict[str, WatchCard] = {}
        self._running: set[str] = set()

        v = QVBoxLayout(self)
        v.setContentsMargins(14, 12, 12, 10)
        v.setSpacing(8)

        head = QHBoxLayout()
        head.setSpacing(8)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        title = QLabel("网页追踪")
        title.setObjectName("sectionLabel")
        title_box.addWidget(title)
        subtitle = QLabel(
            "定时检查公开网页（如机构主页通知）· 可邮件提醒 · 遵守 robots.txt")
        subtitle.setObjectName("subtitleLabel")
        subtitle.setWordWrap(True)
        title_box.addWidget(subtitle)
        head.addLayout(title_box, 1)
        new_btn = QPushButton("+ 监控网页")
        new_btn.setObjectName("secondaryBtn")
        new_btn.setToolTip("添加一个公开网页（如 https://kiz.cas.cn/）作为监控目标")
        new_btn.clicked.connect(self._on_new_page)
        head.addWidget(new_btn)
        v.addLayout(head)

        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        host = QWidget()
        self._cards_layout = QVBoxLayout(host)
        self._cards_layout.setContentsMargins(0, 4, 2, 0)
        self._cards_layout.setSpacing(8)
        self._cards_layout.addStretch()
        self._scroll.setWidget(host)
        v.addWidget(self._scroll, 1)

        self._render_pages()

    # ---- 对外接口 ----

    def manager(self) -> WebWatchManager:
        return self._manager

    def start(self) -> None:
        self._manager.start()

    def shutdown(self) -> None:
        self._manager.shutdown()

    def has_busy_workers(self) -> bool:
        return self._manager.has_busy_workers()

    def reload_storage(self) -> bool:
        ok = self._manager.reload_storage()
        self._render_pages()
        return ok

    # ---- 内部 ----

    def _on_pages_changed(self) -> None:
        self._sync_cards()

    def _on_check_finished(self, page_id: str, *_args) -> None:
        """单页检查结束（成功或失败）：恢复卡片按钮态。"""
        self._running.discard(page_id)
        card = self._cards.get(page_id)
        if card is not None:
            card.set_checking(False)

    def _sync_cards(self) -> None:
        """按 manager 当前页面数据刷新卡片（就地更新，不重建）。"""
        pages = {p.id: p for p in self._manager.pages()}
        for pid in list(self._cards.keys()):
            if pid not in pages:
                card = self._cards.pop(pid)
                card.hide()
                card.setParent(None)
                card.deleteLater()
        for pid, page in pages.items():
            card = self._cards.get(pid)
            if card is None:
                card = WatchCard(page)
                card.check_requested.connect(self._on_check_page)
                card.edit_requested.connect(self._on_edit_page)
                card.delete_requested.connect(self._on_delete_page)
                card.toggle_requested.connect(self._on_toggle_page)
                card.open_requested.connect(self._open_url)
                self._cards[pid] = card
                self._cards_layout.insertWidget(self._cards_layout.count() - 1, card)
            else:
                card.update_page(page)
            card.set_checking(pid in self._running)

    def _render_pages(self) -> None:
        self._sync_cards()
        if not self._manager.pages():
            empty = QLabel(
                "尚未添加监控网页。\n点击「+ 监控网页」，填入机构主页等公开页面地址，\n"
                "页面出现新通知时会显示在这里并可选邮件提醒。")
            empty.setObjectName("subtitleLabel")
            empty.setWordWrap(True)
            empty.setStyleSheet(
                "color: #718180; background-color: #f5f8f6; "
                "border: 1px solid #e1ebe7; border-radius: 12px; padding: 14px;")
            self._empty = empty
            self._cards_layout.insertWidget(0, empty)
        elif getattr(self, "_empty", None) is not None:
            self._empty.hide()
            self._empty.setParent(None)
            self._empty.deleteLater()
            self._empty = None

    def _on_new_page(self) -> None:
        dlg = WatchEditDialog(None, self)
        if dlg.exec():
            self._manager.upsert_page(dlg.page_result())

    def _on_edit_page(self, page_id: str) -> None:
        page = self._manager.get_page(page_id)
        if page is None:
            return
        dlg = WatchEditDialog(page, self)
        if dlg.exec():
            self._manager.upsert_page(dlg.page_result())

    def _on_delete_page(self, page_id: str) -> None:
        page = self._manager.get_page(page_id)
        name = page.name if page else "该监控"
        r = QMessageBox.question(
            self, "删除网页监控",
            f"确定删除「{name}」的监控？已建立的基线快照会一并删除。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if r == QMessageBox.StandardButton.Yes:
            self._manager.remove_page(page_id)

    def _on_toggle_page(self, page_id: str, enabled: bool) -> None:
        self._manager.set_enabled(page_id, enabled)

    def _on_check_page(self, page_id: str) -> None:
        if self._manager.check_page_now(page_id):
            self._running.add(page_id)
            card = self._cards.get(page_id)
            if card is not None:
                card.set_checking(True)

    @staticmethod
    def _open_url(url: str) -> None:
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        if url:
            QDesktopServices.openUrl(QUrl(url))
