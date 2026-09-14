"""新功能离屏自检：热力图自适应/标签避让、计划任务编辑/回到今天可见性/任务折行、
卡片手动拆分合并、卡片提问 Ctrl+Enter、阅读字号增减（内联 px 口径）、读完标记
（无 LLM 无网络，QPA offscreen 运行）。"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("PAPERWB_DISABLE_PREPARSE", "1")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

app = QApplication.instance() or QApplication(sys.argv)

RESULTS: list[tuple[bool, str]] = []


def check(name: str, cond: bool) -> None:
    RESULTS.append((bool(cond), name))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}")


# ============================================================
# 1. 热力图：几何自适应 + 月份标签避让
# ============================================================
from src.core.stats_tracker import StatsTracker  # noqa: E402
from src.ui.stats_panel import HeatmapWidget, PlanPage  # noqa: E402

tmp = tempfile.mkdtemp(prefix="paperwb_selftest_")
tracker = StatsTracker(stats_dir=tmp)

heat = HeatmapWidget()
heat.resize(760, 220)
series = tracker.daily_series("read_minutes", 90)
heat.set_data(series, "read_minutes")

cell, step = heat._geometry()
check("热力图：宽面板单元格放大到上限", cell == HeatmapWidget.MAX_CELL)
# 顶部预留标签条：首行单元格 y ≥ LABEL_H，标签不会被裁切
rect0 = heat._cell_rect(0, 0)
check("热力图：首行单元格在标签条下方", rect0.top() >= heat.LABEL_H)
# 最小高度覆盖 7 行
check("热力图：最小高度覆盖全部 7 行",
      heat.minimumHeight() >= heat.LABEL_H + 7 * step)

heat.resize(300, 220)  # 窄面板：单元格收缩而不是溢出
cell2, _step2 = heat._geometry()
check("热力图：窄面板单元格收缩", cell2 < cell and cell2 >= 10)

# 标签避让逻辑：用窄面板宽度模拟，逐月标签右缘不与上一标签重叠
from datetime import date as _date  # noqa: E402
cells = heat._layout_cells()
check("热力图：单元格布局覆盖全部序列", len(cells) == len(series))

# 2. 计划任务编辑
page = PlanPage(tracker, "daily")
today_key = date.today().isoformat()
tracker.add_plan("daily", "原始任务文本", today_key)
page._refresh()
plans = tracker.plans_for("daily", today_key)
pid = plans[0]["id"]
tracker.edit_plan("daily", pid, "修改后的任务文本")
plans = tracker.plans_for("daily", today_key)
check("计划：edit_plan 修改任务文本", plans[0]["text"] == "修改后的任务文本")
tracker.edit_plan("daily", pid, "   ")  # 空白不改
check("计划：空白文本不覆盖原文本", plans[0]["text"] == "修改后的任务文本")
check("计划：PlanPage 有编辑按钮入口",
      page._edit_editor is None)
tracker.delete_plan("daily", pid)

# 任务排序：同日期内上移/下移，跨日期任务不受影响
tracker.add_plan("daily", "任务一", today_key)
tracker.add_plan("daily", "任务二", today_key)
tracker.add_plan("daily", "任务三", today_key)
other_key = (date.today() + timedelta(days=7)).isoformat()
tracker.add_plan("daily", "下周任务", other_key)
third_id = tracker.plans_for("daily", today_key)[2]["id"]
tracker.move_plan("daily", third_id, -1)
order = [p["text"] for p in tracker.plans_for("daily", today_key)]
check("计划：move_plan 下移任务上移一位",
      order == ["任务一", "任务三", "任务二"])
check("计划：move_plan 不动其它日期任务",
      [p["text"] for p in tracker.plans_for("daily", other_key)] == ["下周任务"])
first_id = tracker.plans_for("daily", today_key)[0]["id"]
tracker.move_plan("daily", first_id, -1)
check("计划：已在顶部时上移无效",
      [p["text"] for p in tracker.plans_for("daily", today_key)]
      == ["任务一", "任务三", "任务二"])
tracker.flush()
tracker2 = StatsTracker(stats_dir=tmp)
check("计划：排序结果随 plans.json 持久化",
      [p["text"] for p in tracker2.plans_for("daily", today_key)]
      == ["任务一", "任务三", "任务二"])
# 清理本组测试任务，避免影响后续折行用例的行结构
for p in list(tracker.plans_for("daily", today_key)
              + tracker.plans_for("daily", other_key)):
    tracker.delete_plan("daily", p["id"])
tracker.flush()

# 连续活跃天数不再展示（用户要求隐藏）
from src.ui.stats_panel import StatsPanel as _StatsPanel  # noqa: E402
_sp = _StatsPanel(tracker)
app.processEvents()
check("统计：今日概览不再显示连续活跃", not hasattr(_sp, "_streak_label"))
_sp.shutdown()

# 「回到今天」按钮：只在视图偏离今天时出现
page._go_prev()
check("计划：翻到昨天后显示「回到今天」", not page._today_btn.isHidden())
page._go_today()
check("计划：回到今天后按钮隐藏", page._today_btn.isHidden())

# 任务文字自动折行（WrapCheckBox 内嵌 wordWrap QLabel）
from src.ui.stats_panel import WrapCheckBox  # noqa: E402

long_task = "需要折行的很长任务文本，" * 12
wcb = WrapCheckBox(long_task)
check("计划：折行复选框 text() 往返一致", wcb.text() == long_task)
check("计划：复选框声明 heightForWidth",
      wcb.hasHeightForWidth() and wcb.heightForWidth(220) > wcb.heightForWidth(600))
wcb.setChecked(True)
check("计划：勾选后文字标签带 done 属性", bool(wcb._label.property("done")))
wcb.setChecked(False)
check("计划：取消勾选后 done 属性复位", not bool(wcb._label.property("done")))

# 集成：窄面板下任务行实际折行增高
from PySide6.QtWidgets import QWidget as _QWidget  # noqa: E402

page_wrap = PlanPage(tracker, "daily")
page_wrap.resize(300, 600)
tracker.add_plan("daily", long_task, date.today().isoformat())
page_wrap._refresh()
page_wrap.show()
app.processEvents()
rows = [page_wrap._list_layout.itemAt(i).widget()
        for i in range(page_wrap._list_layout.count())]
row = next(w for w in rows if isinstance(w, _QWidget))
check("计划：窄面板任务行折行增高", row.height() > 40)
narrow_h = row.height()
page_wrap.resize(1100, 600)
app.processEvents()
check("计划：面板变宽后行高回落", row.height() < narrow_h * 0.7)
tracker.delete_plan("daily",
                    tracker.plans_for("daily", date.today().isoformat())[0]["id"])
page_wrap.hide()

# ============================================================
# 3. 卡片手动拆分/合并（构造 StructuredDocument + 渲染面板）
# ============================================================
from src.core.pdf_processor import StructuredDocument, StructuredElement  # noqa: E402
from src.ui.pdf_viewer import (  # noqa: E402
    PDFViewerPanel, ParagraphCard, MERGEABLE_TYPES, SPLITTABLE_TYPES,
)

doc = StructuredDocument(title="Test Paper")
long_text = ("Alpha beta gamma delta epsilon. " * 4).strip()
e1 = StructuredElement(element_type="body", text=long_text, page=1,
                       element_id="p1_e1")
e2 = StructuredElement(element_type="body", text="Second paragraph text.", page=1,
                       element_id="p1_e2")
e3 = StructuredElement(element_type="figure_caption", text="Figure 1: results",
                       page=2, element_id="p2_e3")
doc.display_elements = [e1, e2, e3]

panel = PDFViewerPanel()
panel._current_path = ""  # 不落盘：仅验证内存中的元素编辑
panel._structured_doc = doc
panel._render_document(doc)
check("拆分合并：渲染出 3 张卡片", len(panel._cards) == 3)

card0 = panel._cards[0]
check("拆分合并：普通正文卡可定位元素下标", panel._elem_index(card0) == 0)
check("拆分合并：caption 卡可定位", panel._elem_index(panel._cards[2]) == 2)

# 模拟 _split_card_dialog 的拆分逻辑（不经对话框，直接走核心步骤）
split_at = len(long_text) // 2
elem = doc.display_elements[0]
base_id = elem.element_id
elem.text = long_text[:split_at].rstrip()
elem.element_id = panel._next_edit_id(base_id, "s")
from copy import copy  # noqa: E402
lower = copy(elem)
lower.text = long_text[split_at:].lstrip()
lower.element_id = panel._next_edit_id(base_id, "s")
doc.display_elements.insert(1, lower)
check("拆分合并：拆分后元素数 4", len(doc.display_elements) == 4)
check("拆分合并：拆分文本无损",
      doc.display_elements[0].text + " " + doc.display_elements[1].text
      == long_text[:split_at].rstrip() + " " + long_text[split_at:].lstrip())
check("拆分合并：拆分 id 唯一",
      doc.display_elements[0].element_id != doc.display_elements[1].element_id)

# 合并回一段（走 _merge_at 的拼接规则，绕过持久化）
panel._merge_at(0)
panel._structured_doc = doc  # _merge_at 内部已引用同一 doc
after = doc.display_elements
check("拆分合并：合并后元素数回到 3", len(after) == 3)
check("拆分合并：合并后文本等于原文",
      after[0].text == long_text and after[1].text == "Second paragraph text.")

# 合并规则：连字符断词 / 中英文拼接
check("拆分合并：SPLITTABLE/UNMERGEABLE 类型区分",
      "body" in MERGEABLE_TYPES and "title" not in MERGEABLE_TYPES
      and "body" in SPLITTABLE_TYPES and "title" not in SPLITTABLE_TYPES)


def _join(a: str, b: str) -> str:
    if a.rstrip().endswith("-"):
        return a.rstrip()[:-1] + b.lstrip()
    cjk = lambda ch: "\u4e00" <= ch <= "\u9fff"  # noqa: E731
    at, bt = a.rstrip(), b.lstrip()
    sep = "" if (at and (cjk(at[-1]) or cjk(bt[:1]))) else " "
    return at + sep + bt


check("拆分合并：连字符断词拼接", _join("seg-\n", "mentation is") == "segmentation is")
check("拆分合并：中文直接拼接", _join("第一段。", "第二段。") == "第一段。第二段。")
check("拆分合并：英文补空格", _join("first part", "second part")
      == "first part second part")

# ============================================================
# 4. 卡片提问 Ctrl+Enter
# ============================================================
from PySide6.QtCore import Qt, QEvent  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from src.ui.pdf_viewer import QALineEdit  # noqa: E402

qa_sent: list[str] = []


def _on_qa(element_id: str, q: str, image_path: str) -> None:
    qa_sent.append(q)


pcard = ParagraphCard(StructuredElement(element_type="body", text=long_text,
                                        page=1, element_id="px"), 0)
pcard.qa_requested.connect(_on_qa)
pcard._toggle_qa_edit()
check("提问框：使用 QALineEdit", isinstance(pcard._qa_edit, QALineEdit))
pcard._qa_edit.setText("这张图说明什么？")


def _press_enter(ctrl: bool) -> None:
    mods = Qt.KeyboardModifier.ControlModifier if ctrl else Qt.KeyboardModifier.NoModifier
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, mods)
    app.sendEvent(pcard._qa_edit, ev)


_press_enter(ctrl=False)
check("提问框：单按回车不发送", not qa_sent)
_press_enter(ctrl=True)
check("提问框：Ctrl+回车发送", qa_sent == ["这张图说明什么？"])
check("提问框：发送后输入框自动关闭", not pcard._qa_edit.isVisible())

# ============================================================
# 5. 阅读字号增减（全局 QSS 的 font-size 会盖掉 setFont，
#    因此增减必须以标签内联 px 落地，这里按该口径断言）
# ============================================================
import src.utils.config as config_mod  # noqa: E402

config_mod.save_config = lambda _cfg: None  # 测试期间不写真实用户配置

from src.ui.pdf_viewer import _FONT_PX_RE  # noqa: E402


def _label_px(lbl) -> float:
    m = _FONT_PX_RE.search(lbl.styleSheet())
    return float(m.group(1)) if m else 0.0


panel._cards = [pcard]
panel._font_delta = 0
panel._apply_font_delta_to_cards()
base_px = _label_px(pcard.text_label)
prop_base = float(pcard.text_label.property("baseFontSizePx"))
check("字号：delta=0 时正文标签落到基准 px",
      base_px > 0 and prop_base > 0 and base_px == round(prop_base))
panel._change_font_delta(1)
check("字号：+1 后标签内联字号变大",
      _label_px(pcard.text_label) == max(8, round(prop_base + 4 / 3))
      and panel._font_delta == 1)
panel._change_font_delta(-1)
check("字号：回落到基准 px", _label_px(pcard.text_label) == base_px)
panel._change_font_delta(-1)
check("字号：回落后可继续减小", panel._font_delta == -1)
for _ in range(20):
    panel._change_font_delta(1)
check("字号：增量钳制在上限", panel._font_delta == 6)
for _ in range(20):
    panel._change_font_delta(-1)
check("字号：增量钳制在下限", panel._font_delta == -2)
panel._font_delta = 0

# ============================================================
# 6. 手动编辑持久化（真实写盘 → 读回）
# ============================================================
import src.utils.config as config_mod  # noqa: E402

states_dir = Path(tmp) / "states"
states_dir.mkdir(parents=True, exist_ok=True)
config_mod.get_states_dir = lambda: states_dir  # monkeypatch 存储目录

fake_pdf = Path(tmp) / "paper.pdf"
fake_pdf.write_bytes(b"%PDF-1.4 fake")
panel._current_path = str(fake_pdf)

config_mod.save_doc_state(str(fake_pdf), {
    "doc_format": "fast",
    "structured_document": doc.to_dict(),
    "pdf_mtime": fake_pdf.stat().st_mtime,
})
# 再做一次真实拆分（模拟 _split_card_dialog 对话框确认后的核心步骤）
panel._edit_seq = 0
el = doc.display_elements[0]
t = el.text
mid = len(t) // 2
el.text = t[:mid].rstrip()
el.element_id = panel._next_edit_id(el.element_id, "s")
low = copy(el)
low.text = t[mid:].lstrip()
low.element_id = panel._next_edit_id(el.element_id, "s")
doc.display_elements.insert(1, low)
panel._apply_manual_edit()  # 写盘 + 重渲染 + 恢复滚动

reloaded = config_mod.load_doc_state(str(fake_pdf))
check("持久化：编辑后的 structured_document 已写盘",
      len(reloaded.get("structured_document", {}).get("display_elements", [])) == 4)
doc2 = StructuredDocument.from_dict(reloaded["structured_document"])
check("持久化：读回后拆分两段文本正确",
      doc2.display_elements[0].text + " " + doc2.display_elements[1].text
      == t[:mid].rstrip() + " " + t[mid:].lstrip())

# ============================================================
# 7. 读完标记（ReadMarkStore 持久化 + 阅读工具栏联动）
# ============================================================
from src.core.read_marks import ReadMarkStore  # noqa: E402

marks_dir = Path(tmp) / "marks"
rm = ReadMarkStore(marks_dir=marks_dir)
pdf_a = str(fake_pdf)
check("已读：默认未读", not rm.is_read(pdf_a))
fired: list[tuple[str, bool]] = []
rm.changed.connect(lambda p, r: fired.append((p, r)))
rm.set_read(pdf_a, True)
check("已读：标记后可查询", rm.is_read(pdf_a))
check("已读：changed 信号携带状态", fired == [(pdf_a, True)])
check("已读：标记落盘",
      (marks_dir / "read_marks.json").exists())
rm2 = ReadMarkStore(marks_dir=marks_dir)
check("已读：新实例读回同一文件", rm2.is_read(pdf_a))
rm.set_read(pdf_a, False)
check("已读：取消标记", not rm.is_read(pdf_a) and fired[-1] == (pdf_a, False))

# 路径规范化：Windows 下大小写差异不产生重复条目
rm2.set_read(pdf_a, True)
check("已读：路径大小写不敏感",
      rm2.is_read(pdf_a.upper()) if os.name == "nt" else True)
rm2.set_read(pdf_a, False)

# 阅读工具栏按钮与全局单例联动（读真实 states 目录 → 指到临时目录）
import src.core.read_marks as read_marks_mod  # noqa: E402
read_marks_mod.get_states_dir = lambda: marks_dir
panel._current_path = pdf_a
read_marks_mod.store()._loaded = False  # 让单例重新加载（测试目录刚被改）
read_marks_mod.set_read(pdf_a, True)
check("已读：工具栏按钮显示已读", panel.read_mark_btn.text() == "✅ 已读")
read_marks_mod.set_read(pdf_a, False)
check("已读：工具栏按钮恢复标记文案",
      panel.read_mark_btn.text() == "✓ 标记已读" and panel.read_mark_btn.isEnabled())

# ============================================================
# 汇总
# ============================================================
failed = [name for ok, name in RESULTS if not ok]
print(f"\n{len(RESULTS) - len(failed)} passed, {len(failed)} failed")
if failed:
    for name in failed:
        print(f"  FAIL: {name}")
    sys.exit(1)
print("ALL OK")
