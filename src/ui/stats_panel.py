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


class WrapCheckBox(QCheckBox):
    """文字自动折行的任务复选框（QCheckBox 原生不支持 wordWrap）。

    内嵌一个 wordWrap 的 QLabel 承载文字；标签不处理的鼠标事件会冒泡回
    复选框，点文字即可正常勾选。完成后给文字标签打 done 属性置灰划线。
    """

    INDICATOR_CLEARANCE = 23  # 勾选框指示器 16px + 间距 7px（与全局 QSS 一致）

    def __init__(self, text: str, parent=None):
        super().__init__(parent)
        self.setObjectName("planTask")
        self._label = QLabel(text, self)
        self._label.setWordWrap(True)
        self._label.setObjectName("planTaskText")
        self._label.setProperty("done", False)
        self._label.setCursor(Qt.CursorShape.PointingHandCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(self.INDICATOR_CLEARANCE, 0, 0, 0)
        lay.setSpacing(0)
        lay.addWidget(self._label)
        self.toggled.connect(self._sync_done_style)

    def text(self) -> str:
        return self._label.text()

    def setText(self, text: str) -> None:
        self._label.setText(text)

    def _sync_done_style(self, checked: bool) -> None:
        self._label.setProperty("done", bool(checked))
        self._label.style().unpolish(self._label)
        self._label.style().polish(self._label)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        inner = max(w - self.INDICATOR_CLEARANCE, 60)
        return max(super().sizeHint().height(),
                   self._label.heightForWidth(inner))


def _fmt_value(field: str, value: int) -> str:
    if field == "write_chars":
        return f"{value:,}"
    return str(value)


class HeatmapWidget(QWidget):
    """GitHub 风格热力图：最近 N 天 × 7 行周网格，自绘。

    单元格随面板宽度自适应放大（上限 22px），顶部预留月份标签条，
    标签按绘制宽度避让，不再互相重叠或被裁切。
    """

    date_picked = Signal(str)  # "2026-08-24"

    GAP = 4
    MARGIN = 8
    LABEL_H = 16       # 顶部月份标签条高度（含间距）
    MAX_CELL = 22      # 单元格上限：热力图过宽时不再继续放大

    def __init__(self, parent=None):
        super().__init__(parent)
        self._series: list[tuple[str, int]] = []
        self._field = "read_minutes"
        self.setMouseTracking(True)
        self._update_min_height()

    # ---------- 几何 ----------

    def _cell_size(self) -> int:
        """按当前宽度算出能放下的最大单元格边长（10 ~ MAX_CELL）。"""
        if not self._series:
            return 12
        weeks = (len(self._series) + 6) // 7
        avail = max(self.width() - 2 * self.MARGIN, weeks)
        cell = (avail - (weeks - 1) * self.GAP) // weeks
        return int(max(10, min(self.MAX_CELL, cell)))

    def _geometry(self) -> tuple[int, int]:
        """返回 (cell, step)；原点 x = MARGIN，y = LABEL_H。"""
        cell = self._cell_size()
        return cell, cell + self.GAP

    def _update_min_height(self) -> None:
        cell, step = self._geometry()
        self.setMinimumHeight(self.LABEL_H + 7 * step + self.MARGIN)

    def set_data(self, series: list[tuple[str, int]], field: str) -> None:
        self._series = series
        self._field = field
        self._update_min_height()
        self.update()

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._update_min_height()  # 宽度变化 → 单元格尺寸变化
        self.update()

    def _cell_rect(self, col: int, row: int) -> QRect:
        _cell, step = self._geometry()
        x = self.MARGIN + col * step
        y = self.LABEL_H + row * step
        return QRect(x, y, self._cell_size(), self._cell_size())

    def _layout_cells(self) -> list[tuple[int, int, str, int]]:
        """把序列铺成 [(col, row, day_str, value)]。"""
        if not self._series:
            return []
        first = date.fromisoformat(self._series[0][0])
        offset = (first.weekday() + 1) % 7  # 首日所在行（列 = 周，行 = 星期）
        return [
            ((offset + i) // 7, (offset + i) % 7, day_str, value)
            for i, (day_str, value) in enumerate(self._series)
        ]

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
        today = date.today().isoformat()
        cells = self._layout_cells()
        for col, row, day_str, value in cells:
            rect = self._cell_rect(col, row)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QColor(HEAT_LEVELS[self._level(value)]))
            painter.drawRect(rect)
            if day_str == today:
                painter.setPen(QPen(QColor("#3478f6"), 1.5))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawRect(rect.adjusted(-1, -1, 0, 0))
        # 月份标签：每月首个单元格所在列上方绘制，按像素宽度避让不重叠
        font = painter.font()
        font.setPointSize(9)
        painter.setFont(font)
        fm = painter.fontMetrics()
        painter.setPen(QColor("#6e6e73"))
        last_month = ""
        last_right = -1
        for col, row, day_str, _value in cells:
            if row != 0:
                continue  # 只看每列首行
            d = date.fromisoformat(day_str)
            month = d.strftime("%Y-%m")
            if month == last_month:
                continue
            last_month = month
            text = d.strftime("%Y-%m")
            x = self._cell_rect(col, 0).x()
            if x <= last_right + 6:
                continue  # 与上一个标签放不下：跳过这个月（下个月再看）
            painter.drawText(x, self.LABEL_H - 4, text)
            last_right = x + fm.horizontalAdvance(text)
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
        self._edit_editor: QLineEdit | None = None

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

    def _key_for(self, d: date) -> str:
        """任意日期在该页签下的存储键（与 StatsTracker 的口径一致）。"""
        if self._scope == "daily":
            return d.isoformat()
        if self._scope == "weekly":
            return _week_key(d)
        return _month_key(d)

    def _date_key(self) -> str:
        return self._key_for(self._cursor)

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

    # ---------- 任务编辑 ----------

    def _begin_edit(self, plan_id: str, text: str, cb: QCheckBox) -> None:
        """把任务行切换为行内编辑框（Enter 确认，Esc 取消）。"""
        if self._edit_editor is not None:
            self._end_edit()
            self._refresh()
            return
        row = cb.parentWidget()
        if row is None:
            return
        editor = QLineEdit(text)
        editor.setToolTip("回车保存，Esc 取消")
        lay = row.layout()
        idx = lay.indexOf(cb)
        cb.setVisible(False)
        lay.insertWidget(idx, editor, 1)
        self._edit_editor = editor
        editor.returnPressed.connect(
            lambda: self._commit_edit(plan_id, editor.text()))
        editor.installEventFilter(self)
        editor.setFocus()
        editor.selectAll()

    def _commit_edit(self, plan_id: str, text: str) -> None:
        self._end_edit()
        if text.strip():
            self._tracker.edit_plan(self._scope, plan_id, text)
        self._refresh()

    def _end_edit(self) -> None:
        if self._edit_editor is not None:
            self._edit_editor.setParent(None)
            self._edit_editor.deleteLater()
            self._edit_editor = None

    def eventFilter(self, obj, event):
        """编辑框 Esc 取消；任务文本双击进入编辑。"""
        from PySide6.QtCore import QEvent
        if event.type() == QEvent.Type.KeyPress and obj is self._edit_editor:
            if event.key() == Qt.Key.Key_Escape:
                self._end_edit()
                self._refresh()
                return True
        elif event.type() == QEvent.Type.MouseButtonDblClick \
                and isinstance(obj, QCheckBox) \
                and hasattr(obj, "_plan_id"):
            self._begin_edit(obj._plan_id, obj.text(), obj)
            return True
        return super().eventFilter(obj, event)

    def _refresh(self) -> None:
        self._edit_editor = None  # 行将整体重建，编辑框引用一并作废
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
        # 「回到今天」只在视图偏离当前周期时出现，今天/本周/本月不占位置
        self._today_btn.setVisible(key != self._key_for(date.today()))
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
            cb = WrapCheckBox(p["text"])
            cb.setChecked(bool(p.get("done")))
            cb.setToolTip("双击文字可修改任务")
            cb._plan_id = p["id"]
            cb.installEventFilter(self)
            cb.toggled.connect(
                lambda _c, pid=p["id"]: self._toggle_task(pid, _c))
            row_layout.addWidget(cb, 1)
            up_btn = QPushButton("↑")
            up_btn.setObjectName("iconBtn")
            up_btn.setFixedWidth(26)
            up_btn.setToolTip("上移任务")
            up_btn.clicked.connect(
                lambda _c=False, pid=p["id"]: self._move_task(pid, -1))
            row_layout.addWidget(up_btn)
            down_btn = QPushButton("↓")
            down_btn.setObjectName("iconBtn")
            down_btn.setFixedWidth(26)
            down_btn.setToolTip("下移任务")
            down_btn.clicked.connect(
                lambda _c=False, pid=p["id"]: self._move_task(pid, 1))
            row_layout.addWidget(down_btn)
            edit_btn = QPushButton("✎")
            edit_btn.setObjectName("iconBtn")
            edit_btn.setFixedWidth(26)
            edit_btn.setToolTip("编辑任务（回车保存，Esc 取消）")
            edit_btn.clicked.connect(
                lambda _c=False, pid=p["id"], txt=p["text"], box=cb:
                self._begin_edit(pid, txt, box))
            row_layout.addWidget(edit_btn)
            del_btn = QPushButton("✕")
            del_btn.setObjectName("iconBtn")
            del_btn.setFixedWidth(26)
            del_btn.setToolTip("删除任务")
            del_btn.clicked.connect(
                lambda _c=False, pid=p["id"]: self._delete_task(pid))
            row_layout.addWidget(del_btn)
            self._list_layout.insertWidget(self._list_layout.count() - 1, row)
            self._task_rows.append((p["id"], cb, del_btn))

    def _move_task(self, plan_id: str, delta: int) -> None:
        self._tracker.move_plan(self._scope, plan_id, delta)
        self._refresh()


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
        # 不再单占一条标题行：标题并入左侧数据面板顶部，面板开关放到那里右端
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

        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        layout.setSpacing(0)
        layout.addWidget(splitter, 1)

    # 侧栏宽度按窗口比例分配，与阅读/检索工作台同一口径
    _PLANS_W_MIN, _PLANS_W_MAX = 340, 430

    def _set_plans_visible(self, visible: bool) -> None:
        self._plans_panel.setVisible(bool(visible))
        if visible:
            plans_w = max(self._PLANS_W_MIN,
                          min(self._PLANS_W_MAX, int(self.width() * 0.30)))
            self._stats_splitter.setSizes(
                [max(560, self.width() - plans_w - 20), plans_w])
        else:
            self._stats_splitter.setSizes([max(560, self.width() - 40), 0])

    def _build_data_panel(self) -> QScrollArea:
        panel = QFrame()
        panel.setObjectName("statsDataPanel")
        panel.setMinimumWidth(420)
        v = QVBoxLayout(panel)
        v.setContentsMargins(12, 12, 12, 12)
        v.setSpacing(10)

        # 标题行：标题 + 计划面板开关（替代原先那条近乎空白的整宽标题带）
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("工作强度与计划")
        title.setObjectName("actionTitle")
        head.addWidget(title)
        hint = QLabel("阅读 / 检索 / 写作统计 · 日周月计划")
        hint.setObjectName("actionStatus")
        head.addWidget(hint)
        head.addStretch()

        self._plans_toggle = QPushButton("🗓 计划面板")
        self._plans_toggle.setObjectName("paneToggle")
        self._plans_toggle.setCheckable(True)
        self._plans_toggle.setChecked(True)
        self._plans_toggle.setCursor(Qt.CursorShape.PointingHandCursor)
        self._plans_toggle.setToolTip("显示或隐藏右侧计划面板（每日/每周/每月）")
        self._plans_toggle.toggled.connect(self._set_plans_visible)
        head.addWidget(self._plans_toggle)
        v.addLayout(head)

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
        tv.addLayout(t_header)
        # 今日概览格子：容器承载多行网格，行数随宽度自适应（见 _relayout_today_grid）
        self._today_host = QWidget()
        today_host_layout = QVBoxLayout(self._today_host)
        today_host_layout.setContentsMargins(0, 0, 0, 0)
        today_host_layout.setSpacing(8)
        self._today_grid = QHBoxLayout()
        self._today_grid.setSpacing(8)
        self._today_grid.setContentsMargins(0, 0, 0, 0)
        today_host_layout.addLayout(self._today_grid)
        tv.addWidget(self._today_host)
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
        hv.addLayout(h_header)

        # 热力图是 7 行 × 周数的日历网格，宽度由周数决定（3 个月仅 13 周
        # ≈ 340px），不会随窗口变宽而放大。因此把 Top 榜与它并排放进同一行：
        # 宽窗口下右半边由 Top 榜填充，而不是让热力图孤零零占一张大卡、
        # 右边留出几百像素的空气。
        self._heatmap = HeatmapWidget()
        self._heatmap.date_picked.connect(self._on_date_picked)
        hv.addWidget(self._heatmap)

        legend = QHBoxLayout()
        legend.setSpacing(4)
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
        legend.addSpacing(18)

        self._field_combo = QComboBox()
        for f in FIELDS:
            self._field_combo.addItem(FIELD_LABELS[f], f)
        self._field_combo.currentIndexChanged.connect(self._on_field_changed)
        legend.addWidget(self._field_combo)
        self._range_combo = QComboBox()
        for label, days in RANGE_OPTIONS:
            self._range_combo.addItem(label, days)
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        legend.addWidget(self._range_combo)
        legend.addStretch()
        hv.addLayout(legend)

        # 阅读 Top 榜（与热力图同行，撑满右半边）
        top_card = QFrame()
        top_card.setObjectName("topPapersCard")
        topv = QVBoxLayout(top_card)
        topv.setContentsMargins(14, 12, 14, 12)
        topv.setSpacing(6)
        top_title = QLabel("阅读 Top 榜（累计时长）")
        top_title.setObjectName("titleLabel")
        topv.addWidget(top_title)
        top_scroll = QScrollArea()
        top_scroll.setWidgetResizable(True)
        top_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        top_scroll.setFrameShape(QFrame.Shape.NoFrame)
        top_host = QWidget()
        self._top_list = QVBoxLayout(top_host)
        self._top_list.setContentsMargins(0, 2, 6, 0)
        self._top_list.setSpacing(4)
        self._top_list.addStretch()
        top_scroll.setWidget(top_host)
        topv.addWidget(top_scroll, 1)

        # 热力图列宽由周数决定（不会随卡片变宽而放大），等分两栏即可：
        # 给热力图 3 份反而在卡片内留出大片空白，不如让两卡等宽齐整。
        charts_row = QHBoxLayout()
        charts_row.setSpacing(10)
        charts_row.addWidget(heat_card, 1)
        charts_row.addWidget(top_card, 1)
        v.addLayout(charts_row)
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

    # 单个概览格子的最小可用宽度：再窄数值与标签就会挤成上下两行窄条
    _TODAY_CELL_MIN_W = 104

    def _relayout_today_grid(self) -> None:
        """按可用宽度给今日概览格子分行：宽窗口一行铺满，窄窗口自动折行。

        7 个格子固定挤在一行时，1024px 窗口下每个只剩约 75px，
        数值与标签会被压成两行窄条；折行后每格都保持可读宽度。
        """
        cells = getattr(self, "_today_cells", [])
        host_layout = self._today_host.layout()
        if not cells or host_layout is None:
            return
        avail = self._today_host.width() or self.width() or 900
        per_row = max(1, min(len(cells),
                             (avail + 8) // (self._TODAY_CELL_MIN_W + 8)))
        # 按「需要几行」反算每行格数，使各行长度均衡：
        # 直接按容量切会得到 5+2、末行两格拉得远宽于首行；改为 4+3 后行内行间齐整。
        if len(cells) > per_row:
            rows_needed = -(-len(cells) // per_row)  # 向上取整
            per_row = -(-len(cells) // rows_needed)
        # 拆掉现有行（格子部件本身保留，仅重新分配所属行）
        while host_layout.count():
            item = host_layout.takeAt(0)
            lay = item.layout()
            if lay is not None:
                while lay.count():
                    lay.takeAt(0)
                lay.deleteLater()
        rows = [cells[i:i + per_row] for i in range(0, len(cells), per_row)]
        for row in rows:
            row_layout = QHBoxLayout()
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.setSpacing(8)
            for cell in row:
                row_layout.addWidget(cell, 1)
            host_layout.addLayout(row_layout)
        # 兼容既有引用：_today_grid 指向首行
        self._today_grid = (host_layout.itemAt(0).layout()
                            if host_layout.count() else self._today_grid)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._relayout_today_grid()

    def _refresh(self) -> None:
        summary = self._tracker.today_summary()
        # 今日概览格子
        while self._today_grid.count():
            item = self._today_grid.takeAt(0)
            w = item.widget()
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        self._today_cells = []
        for f in FIELDS:
            cell = QFrame()
            cell.setObjectName("todayCell")
            cell.setMinimumWidth(self._TODAY_CELL_MIN_W)
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
            self._today_cells.append(cell)
        self._relayout_today_grid()
        # 连续活跃天数不再展示（用户要求隐藏）
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
        top = self._tracker.top_papers(10)
        for i, p in enumerate(top, start=1):
            row = QLabel(
                f"{i}. {p.get('title', '')[:48]}"
                f"{'…' if len(p.get('title', '')) > 48 else ''}"
                f"　{p.get('minutes', 0)} 分钟 · 打开 {p.get('opens', 0)} 次")
            row.setObjectName("topRow")
            row.setWordWrap(True)
            self._top_list.addWidget(row)
        if not top:
            empty = QLabel("还没有阅读记录，打开一篇论文开始积累吧")
            empty.setObjectName("subtitleLabel")
            empty.setWordWrap(True)
            self._top_list.addWidget(empty)
        self._top_list.addStretch()  # 行数少时行贴顶，不被拉伸摊开

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
