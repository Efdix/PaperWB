"""统计工作台数据层：按天活动聚合 + 阅读时长计时 + 日/周/月计划 CRUD。

- 活动记录：内存按天聚合，QTimer 周期 flush 落盘（stats.json），关窗/切存储时强制 flush
- 阅读时长：start_reading/stop_reading 秒级累计，落盘时 ceil 到分钟；同一 PDF 重复打开去重
- 计划：daily/weekly/monthly 三类，date_key 分别为 "2026-08-24" / 周一 "2026-08-18" / "2026-08"
- 纯本地零 LLM；目录可注入（stats_dir=None 走 config.get_stats_dir()），便于测试
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from ..utils.config import get_stats_dir

# 热力图展示的字段（与 stats.json 的 days 键一一对应）
FIELDS = ("read_minutes", "read_papers", "qa_count", "search_count",
          "scout_count", "import_count", "write_chars")

MAX_DAYS = 400          # 按天活动保留上限（约 13 个月）
MAX_PAPERS = 100        # 单篇聚合保留上限
FLUSH_INTERVAL_MS = 60_000

_EMPTY_DAY = {"read_minutes": 0, "read_papers": 0, "qa_count": 0,
              "search_count": 0, "scout_count": 0, "import_count": 0,
              "write_chars": 0}


def _day_key(d: date) -> str:
    return d.isoformat()


def _week_key(d: date) -> str:
    """每周计划挂到所在周的周一。"""
    return (d - timedelta(days=d.weekday())).isoformat()


def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _paper_id(pdf_path: str) -> str:
    return hashlib.md5(pdf_path.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now()


class StatsTracker(QObject):
    """统计与计划管理器。所有变更 emit changed() 驱动面板刷新。"""

    changed = Signal()

    def __init__(self, stats_dir: str | Path | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._dir = Path(stats_dir) if stats_dir else get_stats_dir()
        self._days: dict[str, dict] = {}
        self._papers: dict[str, dict] = {}
        self._plans: dict[str, list[dict]] = {"daily": [], "weekly": [], "monthly": []}
        self._reading_path: str = ""
        self._reading_title: str = ""
        self._reading_started: float = 0.0
        self._dirty = False
        self._load()
        self._flush_timer = QTimer(self)
        self._flush_timer.timeout.connect(self.flush)
        self._flush_timer.start(FLUSH_INTERVAL_MS)

    # ---------- 持久化 ----------

    def _load(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        stats_f = self._dir / "stats.json"
        if stats_f.exists():
            try:
                data = json.loads(stats_f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            if isinstance(data, dict):
                days = data.get("days", {})
                self._days = {k: dict(_EMPTY_DAY, **v) for k, v in days.items()
                              if isinstance(v, dict)}
                papers = data.get("papers", {})
                self._papers = {k: v for k, v in papers.items()
                                if isinstance(v, dict)}
        plans_f = self._dir / "plans.json"
        if plans_f.exists():
            try:
                data = json.loads(plans_f.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                data = {}
            if isinstance(data, dict):
                for scope in self._plans:
                    items = data.get(scope, [])
                    self._plans[scope] = [p for p in items
                                          if isinstance(p, dict) and p.get("id")]

    def flush(self) -> None:
        """落盘当前内存状态；无变更时跳过（不重写文件）。"""
        if not self._dirty:
            return
        self._dirty = False
        try:
            self._dir.mkdir(parents=True, exist_ok=True)
            (self._dir / "stats.json").write_text(
                json.dumps({"days": self._days, "papers": self._papers},
                           ensure_ascii=False, indent=1), encoding="utf-8")
            (self._dir / "plans.json").write_text(
                json.dumps(self._plans, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass

    def reload(self) -> None:
        """数据根目录切换后重读磁盘（先落盘旧目录状态）。"""
        self.flush()
        self._days.clear()
        self._papers.clear()
        self._plans = {"daily": [], "weekly": [], "monthly": []}
        self._load()
        self.changed.emit()

    def _mark_dirty(self) -> None:
        self._dirty = True
        self.changed.emit()

    # ---------- 活动记录 ----------

    def _day(self, d: date) -> dict:
        key = _day_key(d)
        day = self._days.get(key)
        if day is None:
            day = dict(_EMPTY_DAY)
            self._days[key] = day
        return day

    def _bump(self, field: str, amount: int = 1, d: date | None = None) -> None:
        day = self._day(d or _now().date())
        day[field] = day.get(field, 0) + amount
        self._trim_days()
        self._mark_dirty()

    def _trim_days(self) -> None:
        if len(self._days) > MAX_DAYS:
            for k in sorted(self._days)[:len(self._days) - MAX_DAYS]:
                del self._days[k]

    def _trim_papers(self) -> None:
        if len(self._papers) > MAX_PAPERS:
            for k in sorted(self._papers,
                            key=lambda k: self._papers[k].get("minutes", 0))[:len(self._papers) - MAX_PAPERS]:
                del self._papers[k]

    def start_reading(self, pdf_path: str, title: str = "") -> None:
        """开始/续计某篇 PDF 的阅读时长；同一 PDF 重复打开不重复计时。"""
        if not pdf_path:
            return
        if self._reading_path == pdf_path:
            return
        self.stop_reading()
        self._reading_path = pdf_path
        self._reading_title = title or pdf_path
        self._reading_started = time.monotonic()

    def stop_reading(self) -> None:
        """结算当前阅读时长（秒累计，落盘时 ceil 到分钟）。"""
        if not self._reading_path:
            return
        path = self._reading_path
        title = self._reading_title
        elapsed = time.monotonic() - self._reading_started
        self._reading_path = ""
        self._reading_title = ""
        if elapsed < 1.0:
            return
        minutes = math.ceil(elapsed / 60)
        day = self._day(_now().date())
        day["read_minutes"] = day.get("read_minutes", 0) + minutes
        day["read_papers"] = day.get("read_papers", 0) + 1
        pid = _paper_id(path)
        paper = self._papers.get(pid)
        if paper is None:
            paper = {"title": title, "minutes": 0, "opens": 0}
            self._papers[pid] = paper
        paper["minutes"] = paper.get("minutes", 0) + minutes
        paper["opens"] = paper.get("opens", 0) + 1
        self._trim_papers()
        self._trim_days()
        self._mark_dirty()

    def record_qa(self, n: int = 1) -> None:
        self._bump("qa_count", n)

    def record_search(self, hits: int = 0) -> None:
        self._bump("search_count", 1)

    def record_scout(self) -> None:
        self._bump("scout_count", 1)

    def record_import(self) -> None:
        self._bump("import_count", 1)

    def record_draft(self, chars: int) -> None:
        """写作字数：当日取最大值（草稿自动保存快照）。"""
        day = self._day(_now().date())
        if chars > day.get("write_chars", 0):
            day["write_chars"] = chars
            self._trim_days()
            self._mark_dirty()

    def record_polish(self, old_len: int, new_len: int) -> None:
        """润色采纳：写作字数按润色后文本长度更新（取较大值）。"""
        day = self._day(_now().date())
        if new_len > day.get("write_chars", 0):
            day["write_chars"] = new_len
            self._trim_days()
            self._mark_dirty()

    # ---------- 查询 ----------

    def daily_series(self, field: str, days: int) -> list[tuple[str, int]]:
        """最近 days 天（含今天）的 (日期, 数值) 序列，缺失天补 0。"""
        if field not in FIELDS:
            field = "read_minutes"
        today = _now().date()
        out: list[tuple[str, int]] = []
        for i in range(days - 1, -1, -1):
            d = today - timedelta(days=i)
            day = self._days.get(_day_key(d))
            out.append((_day_key(d), int(day.get(field, 0)) if day else 0))
        return out

    def today_summary(self) -> dict:
        return dict(self._day(_now().date()))

    def top_papers(self, n: int = 10) -> list[dict]:
        return sorted(self._papers.values(),
                      key=lambda p: p.get("minutes", 0), reverse=True)[:n]

    def streak_days(self) -> int:
        """从今天（或昨天，今天尚无活动时）向前连续有活动的天数。"""
        today = _now().date()
        d = today
        if not self._days.get(_day_key(d)):
            d -= timedelta(days=1)
        streak = 0
        while self._days.get(_day_key(d)):
            streak += 1
            d -= timedelta(days=1)
        return streak

    # ---------- 计划 CRUD ----------

    def add_plan(self, scope: str, text: str, date_key: str) -> None:
        if scope not in self._plans or not text.strip():
            return
        self._plans[scope].append({
            "id": f"{int(time.time() * 1000)}",
            "text": text.strip(),
            "done": False,
            "date": date_key,
            "created_at": _now().isoformat(timespec="seconds"),
        })
        self._mark_dirty()

    def toggle_plan(self, scope: str, plan_id: str) -> None:
        for p in self._plans.get(scope, []):
            if p["id"] == plan_id:
                p["done"] = not p.get("done", False)
                self._mark_dirty()
                return

    def delete_plan(self, scope: str, plan_id: str) -> None:
        items = self._plans.get(scope, [])
        for i, p in enumerate(items):
            if p["id"] == plan_id:
                del items[i]
                self._mark_dirty()
                return

    def plans_for(self, scope: str, date_key: str) -> list[dict]:
        return [p for p in self._plans.get(scope, []) if p.get("date") == date_key]

    def plan_completion(self, scope: str, date_key: str) -> tuple[int, int]:
        items = self.plans_for(scope, date_key)
        return sum(1 for p in items if p.get("done")), len(items)
