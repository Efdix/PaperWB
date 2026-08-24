# -*- coding: utf-8 -*-
"""统计工作台核心逻辑自测（无 LLM、无真实网络；UI 用 offscreen 模式构造）。

覆盖：阅读时长结算与去重 / 事件按天聚合 / flush-reload 往返（tempfile 注入目录）/
400 天与 Top100 裁剪 / streak 连续活跃 / 计划 CRUD 与完成率 / 热力图数据映射 /
StatsPanel 离屏构建。
"""
import os
import shutil
import sys
import tempfile
import time
from datetime import date, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {detail}")


from src.core.stats_tracker import (  # noqa: E402
    FIELDS, StatsTracker, _month_key, _week_key,
)

# ---------- 1. 阅读时长结算与去重 ----------

tmp = tempfile.mkdtemp(prefix="paperwb_stats_test_")
try:
    t = StatsTracker(stats_dir=tmp)
    t.start_reading(r"C:\papers\a.pdf", "Paper A")
    time.sleep(1.2)
    t.stop_reading()
    day = t.today_summary()
    check("阅读时长按分钟累计", day["read_minutes"] == 1, repr(day))
    check("阅读篇数 +1", day["read_papers"] == 1, repr(day))
    top = t.top_papers(10)
    check("Top 榜记录标题", len(top) == 1 and top[0]["title"] == "Paper A", repr(top))
    check("Top 榜分钟数", top[0]["minutes"] == 1 and top[0]["opens"] == 1, repr(top))

    # 同一 PDF 连续 start 幂等：不重置计时，一次 stop 只结算一次
    t.start_reading(r"C:\papers\a.pdf", "Paper A")
    t.start_reading(r"C:\papers\a.pdf", "Paper A")
    time.sleep(1.2)
    t.stop_reading()
    day = t.today_summary()
    check("同篇连续 start 幂等", day["read_papers"] == 2, repr(day))
    check("同篇时长累加", day["read_minutes"] == 2, repr(day))
    top = t.top_papers(10)
    check("同篇 opens 累加", top[0]["opens"] == 2, repr(top))

    # 短于 1 秒不结算
    t.start_reading(r"C:\papers\b.pdf", "Paper B")
    t.stop_reading()
    check("短阅读不结算", t.today_summary()["read_papers"] == 2, repr(t.today_summary()))

    # 切换文献自动结算上一篇
    t.start_reading(r"C:\papers\c.pdf", "Paper C")
    time.sleep(1.2)
    t.start_reading(r"C:\papers\d.pdf", "Paper D")
    check("切换自动结算", t.today_summary()["read_papers"] == 3, repr(t.today_summary()))
    t.stop_reading()

    # ---------- 2. 事件聚合 ----------

    t.record_qa(2)
    t.record_search(5)
    t.record_scout()
    t.record_import()
    t.record_draft(1200)
    t.record_draft(800)  # 当日取 max
    t.record_polish(100, 1500)
    day = t.today_summary()
    check("问答聚合", day["qa_count"] == 2, repr(day))
    check("检索聚合", day["search_count"] == 1, repr(day))
    check("巡视聚合", day["scout_count"] == 1, repr(day))
    check("导入聚合", day["import_count"] == 1, repr(day))
    check("写作字数取 max", day["write_chars"] == 1500, repr(day))

    # ---------- 3. flush / reload 往返 ----------

    t.flush()
    t2 = StatsTracker(stats_dir=tmp)
    day2 = t2.today_summary()
    check("reload 后今日数据一致", day2 == day, repr(day2))
    check("reload 后 Top 榜一致", len(t2.top_papers(10)) == 2, repr(t2.top_papers(10)))
    check("reload 后计划一致", t2.plans_for("daily", date.today().isoformat()) ==
          t.plans_for("daily", date.today().isoformat()))

    # ---------- 4. 计划 CRUD 与完成率 ----------

    today_key = date.today().isoformat()
    t.add_plan("daily", "读三篇论文", today_key)
    t.add_plan("daily", "写综述引言", today_key)
    t.add_plan("daily", "   ", today_key)  # 空白不添加
    t.add_plan("weekly", "本周完成文献综述", _week_key(date.today()))
    t.add_plan("monthly", "本月投稿", _month_key(date.today()))
    plans = t.plans_for("daily", today_key)
    check("每日计划添加", len(plans) == 2, repr(plans))
    check("空白任务忽略", len(t.plans_for("daily", today_key)) == 2)
    done, total = t.plan_completion("daily", today_key)
    check("完成率初始 0/2", done == 0 and total == 2, f"{done}/{total}")
    t.toggle_plan("daily", plans[0]["id"])
    done, total = t.plan_completion("daily", today_key)
    check("打勾后 1/2", done == 1 and total == 2, f"{done}/{total}")
    t.toggle_plan("daily", plans[0]["id"])  # 取消打勾
    done, _ = t.plan_completion("daily", today_key)
    check("再点取消打勾", done == 0, str(done))
    t.toggle_plan("daily", plans[0]["id"])
    t.delete_plan("daily", plans[1]["id"])
    check("删除任务", len(t.plans_for("daily", today_key)) == 1)
    check("周计划独立", len(t.plans_for("weekly", _week_key(date.today()))) == 1)
    check("月计划独立", len(t.plans_for("monthly", _month_key(date.today()))) == 1)
    check("周/月键格式", _week_key(date(2026, 8, 20)) == "2026-08-17"
          and _month_key(date(2026, 8, 20)) == "2026-08")

    # ---------- 5. streak 连续活跃（独立目录，避免磁盘数据污染） ----------

    tmp5 = tempfile.mkdtemp(prefix="paperwb_stats_streak_")
    try:
        t3 = StatsTracker(stats_dir=tmp5)
        # 手工注入历史天（含今天，从今天起算）
        for i in range(0, 4):
            d = (date.today() - timedelta(days=i)).isoformat()
            t3._days[d] = dict(t3._days.get(d, {}), qa_count=1)
        check("连续活跃 4 天（含今天）", t3.streak_days() == 4, str(t3.streak_days()))
        # 今天无活动时从昨天起算
        t4 = StatsTracker(stats_dir=tmp5)
        for i in range(1, 4):
            d = (date.today() - timedelta(days=i)).isoformat()
            t4._days[d] = dict(t4._days.get(d, {}), qa_count=1)
        check("今天无活动从昨天起算", t4.streak_days() == 3, str(t4.streak_days()))
        # 间断：昨天+前天有、3 天前无 → 从昨天起算连续 2 天
        t5 = StatsTracker(stats_dir=tmp5)
        t5._days[(date.today() - timedelta(days=1)).isoformat()] = {"qa_count": 1}
        t5._days[(date.today() - timedelta(days=2)).isoformat()] = {"qa_count": 1}
        t5._days[(date.today() - timedelta(days=4)).isoformat()] = {"qa_count": 1}
        check("间断只算连续段", t5.streak_days() == 2, str(t5.streak_days()))
    finally:
        shutil.rmtree(tmp5, ignore_errors=True)

    # ---------- 6. 裁剪：400 天 / Top100 ----------

    t6 = StatsTracker(stats_dir=tmp)
    for i in range(420):
        d = (date.today() - timedelta(days=i)).isoformat()
        t6._days[d] = dict(t6._days.get(d, {}), qa_count=1)
    t6._trim_days()
    check("400 天裁剪", len(t6._days) == 400, str(len(t6._days)))
    check("保留最近 400 天", min(t6._days) == (date.today() - timedelta(days=399)).isoformat(),
          min(t6._days))
    for i in range(120):
        t6._papers[f"p{i}"] = {"title": f"Paper {i}", "minutes": i, "opens": 1}
    t6._trim_papers()
    check("Top100 裁剪", len(t6._papers) == 100, str(len(t6._papers)))
    check("保留分钟数最多的", max(t6._papers.values(), key=lambda p: p["minutes"])["minutes"] == 119)

    # ---------- 7. 热力图数据映射 ----------

    series = t.daily_series("qa_count", 7)
    check("序列长度 7", len(series) == 7, str(len(series)))
    check("序列末端是今天", series[-1][0] == date.today().isoformat(), series[-1][0])
    check("序列首端是 6 天前", series[0][0] == (date.today() - timedelta(days=6)).isoformat())
    check("今天 qa 值正确", series[-1][1] == 2, str(series[-1][1]))
    check("未知字段回退到阅读时长", t.daily_series("nope", 3)[-1][1] == 3)
    check("字段全集", set(FIELDS) == {"read_minutes", "read_papers", "qa_count",
                                      "search_count", "scout_count", "import_count",
                                      "write_chars"})

    # ---------- 8. StatsPanel 离屏构建 ----------

    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from src.ui.stats_panel import StatsPanel
    panel = StatsPanel(t)
    panel.show()
    app.processEvents()
    check("StatsPanel 离屏构建", panel.isVisible())
    check("热力图数据已注入", len(panel._heatmap._series) == 90, str(len(panel._heatmap._series)))
    check("今日概览格子 7 个", panel._today_grid.count() == 7, str(panel._today_grid.count()))
    check("Top 榜 2 篇", panel._top_list.count() == 2, str(panel._top_list.count()))
    # 计划页签三页
    check("计划三页签", len(panel._plan_pages) == 3, repr(list(panel._plan_pages)))
    # 热力图点击联动：直接调 jump_to
    panel._plan_pages["daily"].jump_to((date.today() - timedelta(days=2)).isoformat())
    check("计划翻页到 2 天前", panel._plan_pages["daily"]._cursor == date.today() - timedelta(days=2))
    panel._plan_pages["daily"]._go_today()
    check("回到今天", panel._plan_pages["daily"]._cursor == date.today())
    # 指标切换
    panel._field_combo.setCurrentIndex(list(FIELDS).index("write_chars"))
    app.processEvents()
    check("指标切换刷新", panel._field == "write_chars")
    panel.close()
    panel.shutdown()
    check("面板关窗无崩溃", True)
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
for name in PASS:
    print(f"[PASS] {name}")
for name in FAIL:
    print(f"[FAIL] {name}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
