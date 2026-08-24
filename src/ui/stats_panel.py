"""统计工作台：GitHub 风格热力图 + 今日概览 + 阅读 Top 榜 + 日/周/月计划。

左栏（数据视图）：今日概览卡、热力图卡（指标/时间范围可切换，悬停看数值，
点击某天联动右侧计划翻页）、阅读 Top 榜卡。
右栏（计划管理）：每日/每周/每月三页签，日期翻页可回顾补记，任务打勾置灰。
纯本地零 LLM；数据由 StatsTracker 提供，变更经 changed 信号驱动刷新。
"""

from __future__ import annotations

from datetime import date, timedelta

from PySide6.QtCore import QDate, QPoint, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QScrollArea, QSplitter, QTabWidget, QToolTip,
    QVBoxLayout, QWidget,
)

from ..core.stats_tracker import FIELDS, StatsTracker, _month_key, _week_key

# 热力图 5 档 GitHub 绿（0 → 高）
HEAT_LEVELS = ("#ebedf0", "#9be9a8", "#40c463", "#30a14e", "#216e39")

FIELD_LABELS = {
    "read_minutes": "阅读时长（分钟）",
    "read_papers": "阅读篇数",
    "qa_count": "问答次数",
    "search_count": "检索次数",
    "scout_count": "巡视次数",
    "import_count": "导入文献",
    "write_chars": "写作字数",
}

RANGE_OPTIONS = (("3 个月", 90), ("6 个月", 180), ("12 个月", 365))

SCOPE_LABELS = {
    "daily": "每日计划",
    "weekly": "每周计划",
    "monthly": "每月计划",
}


def _fmt_value(field: str, value: int) -> str:
    if field == "write_chars":
        return f"{value:,}"
    return str(value)


class HeatmapWidget(QWidget):
    """GitHub 风格热力图：最近 N 天 × 7 行周网格，自绘。"""

    date_picked = Signal(str)  # "2026-08-24"

    CELL = 12
    GAP = 3
    MARGIN = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series: list[tuple[str, int]] = []
        self._field = "read_minutes"
        self.setMouseTracking(True)
        self.setMinimumHeight(7 * (self.CELL + self.GAP) + 2 * self.MARGIN + 18)

    def set_data(self, series: list[tuple[str, int]], field: str) -> None:
        self._series = series
        self._field = field
        self.update()

    def _cell_rect(self, col: int, row: int) -> QRect:
        x = self.MARGIN + col * (self.CELL + self.GAP)
        y = self.MARGIN + row * (self.CELL + self.GAP)
        return QRect(x, y, self.CELL, self.CELL)

    def _level(self, value: int) -> int:
        if value <= 0:
            return 0
        if self._field == "write_chars":
            # 字数跨度大：按 500/2000/5000/10000 分档
            thresholds = (500, 2000, 5000, 10000)
        else:
            thresholds = (1, 3, 6, 12)
        for i, t in enumerate(thresholds, start=1):
            if value <= t:
                return i
        return 4

    def paintEvent(self, event) -> None:
        if not self._series:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
        today = date.today()
        weeks = (len(self._series) + 6) // 7
        # 首列对齐到周日（GitHub 布局：列 = 周，行 = 星期）
        first = date.fromisoformat(self._series[0][0])
        offset = (first.weekday() + 1) % 7  # 首日所在行
        for i, (day_str, value) in enumerate(self._series):
            col = (offset + i) // 7
            row = (offset + i) % 7
            rect = self._cell_rect(col, row)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(HEAT_LEVELS[self._level(value)]))
            painter.drawRect(rect)
            if day_str == today.isoformat():
                painter.setPen(QPen(QColor("#3478f6"), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.adjusted(-1, -1, 0, 0))
        # 月份标签（每列首行上方）
        painter.setPen(QColor("#6e6e73"))
        font = painter.font()
        font.setPointSize(8)
        painter.setFont(font)
        last_month = ""
        for i, (day_str, _value) in enumerate(self._series):
            col = (offset + i) // 7
            if i % 7 != (7 - offset) % 7:
                continue
            d = date.fromisoformat(day_str)
            month = d.strftime("%Y-%m")
            if month != last_month:
                last_month = month
                painter.drawText(
                    self.MARGIN + col * (self.CELL + self.GAP),
                    self.MARGIN - 3, d.strftime("%Y-%m"))
        painter.end()

    def _index_at(self, pos: QPoint) -> int | None:
        if not self._series:
            return None
        first = date.fromisoformat(self._series[0][0])
        offset = (first.weekday() + 1) % 7
        for i, (day_str, _value) in enumerate(self._series):
            col = (offset + i) // 7
            row = (offset + i) % 7
            if self._cell_rect(col, row).contains(pos):
                return i
        return None

    def mouseMoveEvent(self, event) -> None:
        idx = self._index_at(event.position().toPoint())
        if idx is None:
            QToolTip.hideText()
            return
        day_str, value = self._series[idx]
        QToolTip.showText(
            event.globalPosition().toPoint(),
            f"{day_str} · {FIELD_LABELS.get(self._field, self._field)}："
            f"{_fmt_value(self._field, value)}",
            self)

    def mousePressEvent(self, event) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        idx = self._index_at(event.position().toPoint())
        if idx is not None:
            self.date_picked.emit(self._series[idx][0])


class PlanPage(QWidget):
    """单个计划页签：日期翻页 + 完成率 + 任务列表（打勾/删除/添加）。"""

    def __init__(self, tracker: StatsTracker, scope: str, parent=None):
        super().__init__(parent)
        self._tracker = tracker
        self._scope = scope
        self._cursor = date.today()
        self._task_rows: list[tuple[str, QCheckBox, QPushButton]] = []

        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)

        nav = QHBoxLayout()
        nav.setContentsMargins(0, 0, 0, 0)
        nav.setSpacing(6)
        self._prev_btn = QPushButton("◀")
        self._prev_btn.setObjectName("iconBtn")
        self._prev_btn.setFixedWidth(32)
        self._prev_btn.clicked.connect(self._go_prev)
        nav.addWidget(self._prev_btn)
        self._date_label = QLabel()
        self._date_label.setObjectName("sectionLabel")
        self._date_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        nav.addWidget(self._date_label, 1)
        self._next_btn = QPushButton("▶")
        self._next_btn.setObjectName("iconBtn")
        self._next_btn.setFixedWidth(32)
        self._next_btn.clicked.connect(self._go_next)
        nav.addWidget(self._next_btn)
        self._today_btn = QPushButton("回到今天")
        self._today_btn.setObjectName("softBtn")
        self._today_btn.clicked.connect(self._go_today)
        nav.addWidget(self._today_btn)
        v.addLayout(nav)

        self._completion_label = QLabel()
        self._completion_label.setObjectName("subtitleLabel")
        v.addWidget(self._completion_label)

        self._list_widget = QWidget()
        self._list_layout = QVBoxLayout(self._list_widget)
        self._list_layout.setContentsMargins(0, 0, 0, 0)
        self._list_layout.setSpacing(4)
        self._list_layout.addStretch()
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self._list_widget)
        v.addWidget(scroll, 1)

        add_row = QHBoxLayout()
        add_row.setContentsMargins(0, 0, 0, 0)
        add_row.setSpacing(6)
        self._add_input = QLineEdit()
        self._add_input.setPlaceholderText("添加任务，回车确认…")
        self._add_input.returnPressed.connect(self._add_task)
        add_row.addWidget(self._add_input, 1)
        self._add_btn = QPushButton("添加")
        self._add_btn.setObjectName("primaryBtn")
        self._add_btn.clicked.connect(self._add_task)
        add_row.addWidget(self._add_btn)
        v.addLayout(add_row)

        self._refresh()

    # ---------- 日期导航 ----------

    def _date_key(self) -> str:
        if self._scope == "daily":
            return self._cursor.isoformat()
        if self._scope == "weekly":
            return _week_key(self._cursor)
        return _month_key(self._cursor)

    def _date_label_text(self) -> str:
        if self._scope == "daily":
            return self._cursor.strftime("%Y-%m-%d（%A）")
        if self._scope == "weekly":
            monday = self._cursor - timedelta(days=self._cursor.weekday())
            sunday = monday + timedelta(days=6)
            return f"{monday.strftime('%Y-%m-%d')} ~ {sunday.strftime('%Y-%m-%d')}"
        return self._cursor.strftime("%Y-%m")

    def _go_prev(self) -> None:
        if self._scope == "daily":
            self._cursor -= timedelta(days=1)
        elif self._scope == "weekly":
            self._cursor -= timedelta(days=7)
        else:
            month = self._cursor.month - 1
            year = self._cursor.year
            if month == 0:
                month = 12
                year -= 1
            self._cursor = self._cursor.replace(year=year, month=month)
        self._refresh()

    def _go_next(self) -> None:
        if self._scope == "daily":
            self._cursor += timedelta(days=1)
        elif self._scope == "weekly":
            self._cursor += timedelta(days=7)
        else:
            month = self._cursor.month + 1
            year = self._cursor.year
            if month == 13:
                month = 1
                year += 1
            self._cursor = self._cursor.replace(year=year, month=month)
        self._refresh()

    def _go_today(self) -> None:
        self._cursor = date.today()
        self._refresh()

    def jump_to(self, day_str: str) -> None:
        """热力图点击某天 → 翻到该天所在周期。"""
        try:
            self._cursor = date.fromisoformat(day_str)
        except ValueError:
            return
        self._refresh()

    # ---------- 任务 ----------

    def _add_task(self) -> None:
        text = self._add_input.text().strip()
        if not text:
            return
        self._tracker.add_plan(self._scope, text, self._date_key())
        self._add_input.clear()
        self._refresh()

    def _toggle_task(self, plan_id: str, _checked: bool) -> None:
        self._tracker.toggle_plan(self._scope, plan_id)
        self._refresh()

    def _delete_task(self, plan_id: str) -> None:
        self._tracker.delete_plan(self._scope, plan_id)
        self._refresh()

    def _refresh(self) -> None:
        for _pid, cb, btn in self._task_rows:
            cb.setParent(None)
            cb.deleteLater()
            btn.setParent(None)
            btn.deleteLater()
        self._task_rows.clear()
        # 清空 stretch 之外的旧行
        while self._list_layout.count() > 1:
            item = self._list_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()

        key = self._date_key()
        done, total = self._tracker.plan_completion(self._scope, key)
        self._date_label.setText(self._date_label_text())
        if total:
            self._completion_label.setText(
                f"完成率 {done}/{total} · {int(done / total * 100)}%")
        else:
            self._completion_label.setText("暂无任务")
        # 过去日期未完成 → 橙色提示
        overdue = (self._cursor < date.today()
                   and self._scope == "daily" and total > done)
        self._date_label.setProperty("overdue", overdue)
        self._date_label.style().unpolish(self._date_label)
        self._date_label.style().polish(self._date_label)

        for p in self._tracker.plans_for(self._scope, key):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(8, 4, 8, 4)
            row_layout.setSpacing(8)
            cb = QCheckBox(p["text"])
            cb.setChecked(bool(p.get("done")))
            cb.setObjectName("planTask")
            cb.toggled.connect(
                lambda _c, pid=p["id"]: self._toggle_task(pid, _c))
            row_layout.addWidget(cb, 1)
            del_btn = QPushButton("✕")
            del_btn.setObjectName("iconBtn")
            del_btn.setFixedWidth(26)
            del_btn.setToolTip("删除任务")
            del_btn.clicked.connect(
                lambda _c=False, pid=p["id"]: self._delete_task(pid))
            row_layout.addWidget(del_btn)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._task_rows.append((p["id"], cb, del_btn))


class StatsPanel(QWidget):
    """统计工作台：左·数据视图（概览/热力图/Top 榜），右·计划管理。"""

    def __init__(self, tracker: StatsTracker, parent=None):
        super().__init__(parent)
        self.setObjectName("statsPanel")
        self._tracker = tracker
        self._field = "read_minutes"
        self._range_days = 90
        self._tracker.changed.connect(self._refresh)
        self._setup_ui()
        self._refresh()

    # ================= UI 构建 =================

    def _setup_ui(self) -> None:
        header = QFrame()
        header.setObjectName("workspaceHeader")
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(16, 10, 16, 10)
        header_layout.setSpacing(8)
        title_box = QVBoxLayout()
        title_box.setSpacing(1)
        eyebrow = QLabel("工作强度与计划")
        eyebrow.setObjectName("eyebrowLabel")
        title_box.addWidget(eyebrow)
        title = QLabel("统计工作台")
        title.setObjectName("titleLabel")
        title_box.addWidget(title)
        header_layout.addLayout(title_box)
        subtitle = QLabel("每日工作热力图与阅读/检索/写作统计，日周月计划打勾管理")
        subtitle.setObjectName("subtitleLabel")
        header_layout.addWidget(subtitle)
        header_layout.addStretch()

        self._plans_toggle = QPushButton("计划面板")
        self._plans_toggle.setObjectName("paneToggle")
        self._plans_toggle.setCheckable(True)
        self._plans_toggle.setChecked(True)
        self._plans_toggle.setToolTip("显示或隐藏右侧计划面板（每日/每周/每月）")
        header_layout.addWidget(self._plans_toggle)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(3)
        splitter.setOpaqueResize(False)
        self._data_panel = self._build_data_panel()
        splitter.addWidget(self._data_panel)
        self._plans_panel = self._build_plans_panel()
        splitter.addWidget(self._plans_panel)
        splitter.setSizes([760, 400])
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, True)
        self._stats_splitter = splitter
        self._plans_toggle.toggled.connect(self._set_plans_visible)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        layout.addWidget(header)
        layout.addWidget(splitter, 1)

    def _set_plans_visible(self, visible: bool) -> None:
        self._plans_panel.setVisible(bool(visible))
        if visible:
            self._stats_splitter.setSizes([max(500, self.width() - 420), 400])
        else:
            self._stats_splitter.setSizes([max(500, self.width() - 40), 0])

    def _build_data_panel(self) -> QScrollArea:
        panel = QFrame()
        panel.setObjectName("statsDataPanel")
        panel.setMinimumWidth(420)
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 14, 12)
        v.setSpacing(10)

        # 今日概览卡
        today_card = QFrame()
        today_card.setObjectName("todayCard")
        tv = QVBoxLayout(today_card)
        tv.setContentsMargins(14, 12, 14, 12)
        tv.setSpacing(8)
        t_header = QHBoxLayout()
        t_title = QLabel("今日概览")
        t_title.setObjectName("titleLabel")
        t_header.addWidget(t_title)
        t_header.addStretch()
        self._streak_label = QLabel()
        self._streak_label.setObjectName("streakChip")
        t_header.addWidget(self._streak_label)
        tv.addLayout(t_header)
        self._today_grid = QHBoxLayout()
        self._today_grid.setSpacing(8)
        tv.addLayout(self._today_grid)
        v.addWidget(today_card)

        # 热力图卡
        heat_card = QFrame()
        heat_card.setObjectName("heatmapCard")
        hv = QVBoxLayout(heat_card)
        hv.setContentsMargins(14, 12, 14, 12)
        hv.setSpacing(8)
        h_header = QHBoxLayout()
        h_title = QLabel("工作热力图")
        h_title.setObjectName("titleLabel")
        h_header.addWidget(h_title)
        h_header.addStretch()
        self._field_combo = QComboBox()
        for f in FIELDS:
            self._field_combo.addItem(FIELD_LABELS[f], f)
        self._field_combo.currentIndexChanged.connect(self._on_field_changed)
        h_header.addWidget(self._field_combo)
        self._range_combo = QComboBox()
        for label, days in RANGE_OPTIONS:
            self._range_combo.addItem(label, days)
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        h_header.addWidget(self._range_combo)
        hv.addLayout(h_header)
        self._heatmap = HeatmapWidget()
        self._heatmap.date_picked.connect(self._on_date_picked)
        hv.addWidget(self._heatmap)
        legend = QHBoxLayout()
        legend.setSpacing(4)
        legend.addStretch()
        less = QLabel("少")
        less.setObjectName("subtitleLabel")
        legend.addWidget(less)
        for color in HEAT_LEVELS:
            swatch = QLabel()
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background-color: {color}; border-radius: 2px;")
            legend.addWidget(swatch)
        more = QLabel("多")
        more.setObjectName("subtitleLabel")
        legend.addWidget(more)
        legend.addStretch()
        hv.addLayout(legend)
        v.addWidget(heat_card)

        # 阅读 Top 榜卡
        top_card = QFrame()
        top_card.setObjectName("topPapersCard")
        topv = QVBoxLayout(top_card)
        topv.setContentsMargins(14, 12, 14, 12)
        topv.setSpacing(8)
        top_title = QLabel("阅读 Top 榜（累计时长）")
        top_title.setObjectName("titleLabel")
        topv.addWidget(top_title)
        self._top_list = QVBoxLayout()
        self._top_list.setSpacing(4)
        topv.addLayout(self._top_list)
        v.addWidget(top_card)
        v.addStretch()

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(panel)
        return scroll

    def _build_plans_panel(self) -> QFrame:
        panel = QFrame()
        panel.setObjectName("planCard")
        v = QVBoxLayout(panel)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(8)
        title = QLabel("计划管理")
        title.setObjectName("titleLabel")
        v.addWidget(title)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        self._plan_pages: dict[str, PlanPage] = {}
        for scope in ("daily", "weekly", "monthly"):
            page = PlanPage(self._tracker, scope)
            self._plan_pages[scope] = page
            tabs.addTab(page, SCOPE_LABELS[scope])
        v.addWidget(tabs, 1)
        return panel

    # ================= 刷新 =================

    def _refresh(self) -> None:
        summary = self._tracker.today_summary()
        # 今日概览格子
        while self._today_grid.count():
            item = self._today_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for f in FIELDS:
            cell = QFrame()
            cell.setObjectName("todayCell")
            cv = QVBoxLayout(cell)
            cv.setContentsMargins(8, 6, 8, 6)
            cv.setSpacing(2)
            value = QLabel(_fmt_value(f, summary.get(f, 0)))
            value.setObjectName("todayValue")
            value.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cv.addWidget(value)
            label = QLabel(FIELD_LABELS[f].split("（")[0])
            label.setObjectName("subtitleLabel")
            label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            cv.addWidget(label)
            self._today_grid.addWidget(cell)
        streak = self._tracker.streak_days()
        self._streak_label.setText(f"🔥 连续活跃 {streak} 天")

        self._heatmap.set_data(
            self._tracker.daily_series(self._field, self._range_days),
            self._field)

        # 阅读 Top 榜
        while self._top_list.count():
            item = self._top_list.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for i, p in enumerate(self._tracker.top_papers(10), start=1):
            row = QLabel(
                f"{i}. {p.get('title', '')[:48]}"
                f"{'…' if len(p.get('title', '')) > 48 else ''}"
                f"　{p.get('minutes', 0)} 分钟 · 打开 {p.get('opens', 0)} 次")
            row.setObjectName("topRow")
            row.setWordWrap(True)
            self._top_list.addWidget(row)
        if not self._tracker.top_papers(1):
            empty = QLabel("还没有阅读记录，打开一篇论文开始积累吧")
            empty.setObjectName("subtitleLabel")
            self._top_list.addWidget(empty)

    def _on_field_changed(self, _index: int) -> None:
        self._field = self._field_combo.currentData()
        self._refresh()

    def _on_range_changed(self, _index: int) -> None:
        self._range_days = self._range_combo.currentData()
        self._refresh()

    def _on_date_picked(self, day_str: str) -> None:
        for page in self._plan_pages.values():
            page.jump_to(day_str)

    # ================= 生命周期 =================

    def reload_storage(self) -> None:
        self._tracker.reload()
        self._refresh()

    def shutdown(self) -> None:
        self._tracker.flush()
