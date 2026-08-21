# -*- coding: utf-8 -*-
"""DocDiffController 自测（offscreen）：diff 渲染/锚点/导航/接受拒绝。

覆盖：渲染锚点类型、导航、接受/拒绝、全部接受/拒绝、手动编辑后锚点重建。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {detail}")


from PySide6.QtWidgets import QApplication, QTextEdit
from PySide6.QtGui import QTextCursor

app = QApplication([])

from src.core.doc_diff import DocDiffController

# ---------- 1. 渲染与锚点 ----------
edit = QTextEdit()
ctrl = DocDiffController(edit)
original = "这是原始文本，包含一些内容。"
polished = "这是修改后的文本，包含更多内容。"
ctrl.render(original, polished)
check("渲染锚点存在", ctrl.has_changes, str(ctrl.anchor_count))
kinds = {k for _s, _e, k in ctrl.change_anchors}
check("锚点类型", kinds <= {"insert", "delete", "replace"}, repr(kinds))
# 内联 diff 渲染后 toPlainText 含删除线文本（新旧混杂），不等于 polished；
# 全部接受后才等于 polished（见测试 4）
rendered = edit.toPlainText()
check("渲染含新增文本", "修改后" in rendered and "更多" in rendered, rendered)
check("渲染含删除线文本", "原始" in rendered and "一些" in rendered, rendered)

# ---------- 2. 导航 ----------
ctrl.navigate(1)  # 下一处
check("导航不崩溃", ctrl._current_anchor_idx >= 0, str(ctrl._current_anchor_idx))

# ---------- 3. 接受当前 ----------
n_before = ctrl.anchor_count
ctrl._current_anchor_idx = 0
ctrl.apply_change(accept=True)
check("接受后锚点减少", ctrl.anchor_count == n_before - 1,
      f"{n_before} -> {ctrl.anchor_count}")

# ---------- 4. 全部接受 → 文本等于 polished ----------
ctrl2 = DocDiffController(QTextEdit())
ctrl2.render(original, polished)
ctrl2.accept_all()
check("全部接受后无锚点", not ctrl2.has_changes)
check("全部接受后文本", ctrl2.accepted_text() == polished, ctrl2.accepted_text())

# ---------- 5. 全部拒绝 → 文本等于 original ----------
ctrl3 = DocDiffController(QTextEdit())
ctrl3.render(original, polished)
ctrl3.reject_all()
check("全部拒绝后无锚点", not ctrl3.has_changes)
check("全部拒绝后文本", ctrl3.accepted_text() == original, ctrl3.accepted_text())

# ---------- 6. 手动编辑后锚点重建 ----------
ctrl4 = DocDiffController(QTextEdit())
ctrl4.render(original, polished)
# 模拟用户手动删除一段绿色新增（通过 textChanged 触发重建）
cursor = ctrl4._edit.textCursor()
cursor.setPosition(0)
cursor.setPosition(len(ctrl4._edit.toPlainText()), QTextCursor.MoveMode.KeepAnchor)
ctrl4._edit.setTextCursor(cursor)
ctrl4._edit.textCursor().removeSelectedText()
ctrl4.on_text_changed()
check("手动编辑后重建不崩溃", ctrl4._edit.toPlainText() == "")

# ---------- 7. 引用高亮 ----------
ctrl5 = DocDiffController(QTextEdit())
ctrl5.render("引言 (Smith et al., 2020) 指出 [1,2] 相关。", "引言 (Smith et al., 2020) 指出 [1,2] 相关。")
check("引用高亮不崩溃", True)

# ---------- 8. CommentFixWorker prompt 注入（风格指南 + Zotero 证据） ----------
import unittest.mock as mock

from src.ui.writing_panel import CommentFixWorker

_captured: dict = {}


class FakeLLM:
    def chat_sync(self, messages, **kw):
        _captured["msgs"] = messages
        return '{"changes": []}'


class FakeCoach:
    def build_polish_system_prompt(self, wt):
        return "学术风格指南：引用详略度极其重要，句式正式。"


class FakeZotero:
    def get_all_items(self):
        return []


# 无 coach/zotero：prompt 无风格与证据段
w0 = CommentFixWorker(
    FakeLLM(),
    [{"author": "导师", "text": "口语化", "paragraph": "这段文字比较口语化。",
      "paragraph_index": 0}],
)
w0.run()
_p0 = _captured["msgs"][1]["content"]
check("无 coach 无风格段", "【风格约束】" not in _p0)
check("无 zotero 无证据段", "【引文原文证据】" not in _p0)

# 带 coach + zotero → prompt 注入风格指南与引文证据
with mock.patch("src.core.unified_writer.UnifiedWriter") as MockUW:
    MockUW.return_value._build_citation_sources.return_value = (
        "--- 引文 (Smith et al., 2020) ---\n证据段落文本")
    w1 = CommentFixWorker(
        FakeLLM(),
        [{"author": "导师", "text": "口语化",
          "paragraph": "Smith et al. (2020) 发现了关键机制。", "paragraph_index": 0}],
        coach=FakeCoach(), zotero=FakeZotero(), writing_type="综述",
    )
    w1.run()
_p1 = _captured["msgs"][1]["content"]
check("风格指南注入", "【风格约束】" in _p1 and "学术风格指南" in _p1)
check("Zotero 证据注入", "【引文原文证据】" in _p1 and "Smith et al." in _p1)

# ---------- 9. WritingPanel 批注定位与降级（离屏构建） ----------
from src.core.docx_io import DocxComment
from src.ui.writing_panel import WritingPanel

wp = WritingPanel()
wp.editor.setPlainText("第一段：鸟类羽色发育的黑色素细胞研究。\n"
                        "第二段：单细胞测序揭示了新的调控通路。")
wp._word_comments = [
    DocxComment(comment_id="0", author="导师", text="这里要改",
                paragraph_index=0, char_start=4, char_end=10),
]
wp._word_paragraphs_snapshot = [
    "第一段：鸟类羽色发育的黑色素细胞研究。",
    "第二段：单细胞测序揭示了新的调控通路。",
]
wp._render_comments()
check("批注常驻标记生成", len(wp._comment_marks) == 1,
      str(len(wp._comment_marks)))
_span = wp._comment_span(wp._word_comments[0])
check("批注精确偏移", _span == (4, 10), repr(_span))
# ExtraSelection 选区有效性（PySide6 构造函数会丢选区，须属性赋值）
_sels = wp.editor.extraSelections()
check("批注标记选区有效",
      bool(_sels) and _sels[0].cursor.hasSelection()
      and _sels[0].cursor.selectedText() == "鸟类羽色发育",
      repr([s.cursor.selectedText() for s in _sels]))
wp._highlight_range(0, 4)
_sels2 = wp.editor.extraSelections()
check("定位高亮选区有效",
      any(s.cursor.hasSelection() for s in _sels2))
wp.editor.setPlainText("改过的第一段内容不一样了。\n第二段")
_span2 = wp._comment_span(wp._word_comments[0])
check("偏移失效降级", _span2 == (0, len("改过的第一段内容不一样了。")),
      repr(_span2))
_hints = wp._get_comment_hints_for_span(0, 5)
check("选区批注提示", "这里要改" in _hints, repr(_hints))
wp.shutdown()
check("仅核查入口", hasattr(wp, "_verify_btn") and not hasattr(wp, "_review_btn"))
check("自动评价入口", hasattr(wp, "_start_auto_review"))

print()
for name in PASS:
    print(f"[PASS] {name}")
for name in FAIL:
    print(f"[FAIL] {name}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
