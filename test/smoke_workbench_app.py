# -*- coding: utf-8 -*-
"""检索工作台集成冒烟测试 —— offscreen 构造完整 MainWindow（真实配置，不调 LLM）。

验证：工作台 Tab/导航切换（阅读0→检索1→写作2）、页签式问答侧栏、Zotero 注入、
索引后台构建、关窗无崩溃。弹窗全部自动应答，避免无头环境阻塞。
"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
# 冒烟不跑后台全库预解析（真实 Docling 解析过重，其逻辑由 selftest 覆盖）
os.environ.setdefault("PAPERWB_DISABLE_PREPARSE", "1")

from src.utils.config import has_data_root

if not has_data_root():
    print("[SKIP] 未配置 data_root，跳过冒烟测试")
    sys.exit(0)

from PySide6.QtWidgets import QApplication, QMessageBox

# 无头环境自动应答所有弹窗
for name, ret in (("warning", "Ok"), ("critical", "Ok"),
                  ("information", "Ok"), ("about", "Ok"),
                  ("question", "No")):
    setattr(QMessageBox, name,
            staticmethod(lambda *a, _r=None, **k: QMessageBox.StandardButton.Ok))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name if cond else f"{name} {detail}")


app = QApplication([])

from src.app import MainWindow  # noqa: E402

win = MainWindow()
win.show()
check("主窗口构建", True)

# 工作台切换（顺序：阅读0 → 检索1 → 写作2）
win._switch_workspace(1)
check("Tab 切到检索工作台", win._main_tabs.currentIndex() == 1)
check("导航按钮三态", win._read_nav.isChecked() is False
      and win._write_nav.isChecked() is False
      and win._scout_nav.isChecked() is True)
check("面板已注入主窗口", win._workbench_panel is not None)

# 三工作台的侧栏收起/恢复
win._set_reader_library_visible(False)
check("阅读文献列表可收起", win.pdf_list.isHidden())
win._set_reader_library_visible(True)
win._set_reader_chat_visible(False)
check("阅读论文问答可收起", win.chat_tabs.isHidden())
win._set_reader_chat_visible(True)
check("问答侧栏双页签", win.chat_tabs.count() == 2
      and win.chat_tabs.tabText(0) == "本篇论文"
      and win.chat_tabs.tabText(1) == "全文献库")

win._writing_panel._set_inspector_visible(False)
check("写作工具检查器可隐藏", win._writing_panel._inspector_scroll.isHidden())
win._writing_panel._set_inspector_visible(True)
check("写作工具检查器可恢复", not win._writing_panel._inspector_scroll.isHidden())

win._workbench_panel._set_scout_visible(False)
check("检索巡视面板可收起", not win._workbench_panel._scout_panel.isVisible())
win._workbench_panel._set_scout_visible(True)
check("检索巡视面板可恢复", win._workbench_panel._scout_panel.isVisible())

# Zotero → 面板注入（全库问答 + 检索工作台共用同一 Zotero 库）
items = win.library_qa_panel._items_snapshot
if win._zotero is not None and win._zotero.is_available:
    check("Zotero 条目注入", len(items) > 0, f"items={len(items)}")
else:
    print(f"[SKIP] Zotero 未配置/不可用（items={len(items)}），跳过索引部分")

# 等索引构建（最多 60 秒，未完成也算不失败——大库首次构建慢）
if items:
    deadline = time.time() + 60.0
    while time.time() < deadline:
        app.processEvents()
        if win.library_qa_panel._engine_ready:
            break
        time.sleep(0.2)
    n_docs, n_chunks = win.library_qa_panel._engine.index_stats()
    if win.library_qa_panel._engine_ready:
        check("索引构建完成", n_docs >= 0 and n_chunks >= 0,
              f"docs={n_docs} chunks={n_chunks}")
        check("提问按钮状态同步",
              win.library_qa_panel._ask_input.isEnabled()
              == (win._llm_text is not None))
        print(f"[INFO] 索引: {n_docs} 篇全文 / {n_chunks} 段 · "
              f"状态文案: {win.library_qa_panel._qa_status.text()}")
    else:
        print("[INFO] 索引仍在构建（大库首次较慢），仅验证线程存活无崩溃")

# 切到写作再回阅读，验证工作台顺序
win._switch_workspace(2)
check("Tab 切到写作工作台", win._main_tabs.currentIndex() == 2)
check("写作导航选中", win._write_nav.isChecked() is True
      and win._scout_nav.isChecked() is False)
win._switch_workspace(0)
check("Tab 切回阅读", win._main_tabs.currentIndex() == 0)

# 统计工作台（第 4 工作台）
win._switch_workspace(3)
check("Tab 切到统计工作台", win._main_tabs.currentIndex() == 3)
check("统计导航选中", win._stats_nav.isChecked() is True
      and win._read_nav.isChecked() is False)
check("统计面板已注入", win._stats_panel is not None
      and win._stats_tracker is not None)
check("统计计划三页签", len(win._stats_panel._plan_pages) == 3)
# 计划添加/打勾走真实 tracker（写真实 stats 目录，冒烟可接受）
win._stats_panel._plan_pages["daily"]._add_input.setText("冒烟测试任务")
win._stats_panel._plan_pages["daily"]._add_task()
plans = win._stats_tracker.plans_for("daily", __import__("datetime").date.today().isoformat())
check("计划添加生效", any(p["text"] == "冒烟测试任务" for p in plans), repr(plans))
for p in plans:
    if p["text"] == "冒烟测试任务":
        win._stats_tracker.toggle_plan("daily", p["id"])
        win._stats_tracker.delete_plan("daily", p["id"])
win._stats_panel._set_plans_visible(False)
check("计划面板可收起", not win._stats_panel._plans_panel.isVisible())
win._stats_panel._set_plans_visible(True)
check("计划面板可恢复", win._stats_panel._plans_panel.isVisible())
win._switch_workspace(0)
check("统计切回阅读", win._main_tabs.currentIndex() == 0)

win.close()
app.processEvents()
check("关窗无崩溃", True)

for name in PASS:
    print(f"[PASS] {name}")
for name in FAIL:
    print(f"[FAIL] {name}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
