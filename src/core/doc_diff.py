"""QTextEdit 内联 diff 控制器 —— 渲染/锚点/导航/接受拒绝。

从 diff_dialog 抽取的共享逻辑，供两处复用：
1. DiffDialog（润色结果对比对话框）
2. 写作面板编辑器（WorkBuddy 式「人机双写」修订：AI 修改内联渲染，用户就地审阅）

渲染约定（渲染与锚点探测/拒绝删除必须使用同一份定义）：
- 删除 = 红字 + 红底 + 删除线
- 新增 = 绿字 + 绿底
- 未变 = 灰蓝
"""

from __future__ import annotations

import difflib
import re

from PySide6.QtGui import QColor, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import QTextEdit

# 新增文本的背景色（渲染与锚点探测/拒绝删除共用同一份定义）
INSERT_BG = QColor("#e2f3ee")
_DEL_BG = QColor("#fbe9e6")
_EQUAL_FG = QColor("#526b6c")
_DEL_FG = QColor("#b24f4a")
_INS_FG = QColor("#278273")
_CITE_FG = QColor("#b87835")

# 引用标记高亮正则（与 writing_panel._count_citation_markers 同口径）
_CITATION_PATTERNS = [
    (r'\(([^)]*\d{4}[a-z]?)\)'),
    (r'\[(\d+(?:[,\-]\d+)*)\]'),
    (r'（[^）]*?\d{4}）'),
    (r'[A-Z][a-z]+等（\d{4}）'),
    (r'[A-Z]\w+(?:\s+(?:et al\.|& [A-Z]\w+))?,\s*\d{4}[a-z]?'),
]


def _plain_fmt() -> QTextCharFormat:
    """普通文本格式（接受/拒绝后恢复用）。"""
    fmt = QTextCharFormat()
    fmt.setForeground(_EQUAL_FG)
    fmt.setBackground(QColor(0, 0, 0, 0))
    fmt.setFontStrikeOut(False)
    return fmt


class DocDiffController:
    """绑定一个 QTextEdit 的 diff 控制器。"""

    def __init__(self, edit: QTextEdit) -> None:
        self._edit = edit
        self._change_anchors: list[tuple[int, int, str]] = []  # (start, end, type)
        self._current_anchor_idx = -1
        self._skip_recompute = False
        self._on_changed = None  # 锚点变化回调（审阅工具栏刷新用）

    # ---- 属性 ----

    @property
    def change_anchors(self) -> list[tuple[int, int, str]]:
        return list(self._change_anchors)

    @property
    def has_changes(self) -> bool:
        return bool(self._change_anchors)

    @property
    def anchor_count(self) -> int:
        return len(self._change_anchors)

    def set_on_changed(self, cb) -> None:
        """锚点数量/位置变化时回调（用于刷新外部审阅工具栏）。"""
        self._on_changed = cb

    def _notify(self) -> None:
        if self._on_changed is not None:
            self._on_changed()

    # ---- 渲染 ----

    def render(self, original: str, polished: str,
               highlight_citations: bool = True) -> None:
        """把 original→polished 的 diff 渲染进编辑器。"""
        self._skip_recompute = True
        try:
            self._edit.clear()
            self._change_anchors = []
            matcher = difflib.SequenceMatcher(None, original, polished)

            fmt_equal = self._fmt(_EQUAL_FG)
            fmt_del = self._fmt(_DEL_FG, bg=_DEL_BG)
            fmt_insert = self._fmt(_INS_FG, bg=INSERT_BG)

            cursor = self._edit.textCursor()
            cursor.beginEditBlock()

            for tag, i1, i2, j1, j2 in matcher.get_opcodes():
                if tag == "equal":
                    self._insert(cursor, original[i1:i2], fmt_equal)
                elif tag == "delete":
                    start = cursor.position()
                    del_fmt = QTextCharFormat(fmt_del)
                    del_fmt.setFontStrikeOut(True)
                    self._insert(cursor, original[i1:i2], del_fmt)
                    self._change_anchors.append((start, cursor.position(), "delete"))
                elif tag == "insert":
                    start = cursor.position()
                    self._insert(cursor, polished[j1:j2], fmt_insert)
                    self._change_anchors.append((start, cursor.position(), "insert"))
                elif tag == "replace":
                    start = cursor.position()
                    del_fmt = QTextCharFormat(fmt_del)
                    del_fmt.setFontStrikeOut(True)
                    self._insert(cursor, original[i1:i2], del_fmt)
                    self._insert(cursor, polished[j1:j2], fmt_insert)
                    self._change_anchors.append((start, cursor.position(), "replace"))

            cursor.endEditBlock()
            self._current_anchor_idx = -1
            if highlight_citations:
                self.highlight_citations()
        finally:
            self._skip_recompute = False
        self._notify()

    @staticmethod
    def _fmt(fg: QColor, bg: QColor | None = None) -> QTextCharFormat:
        fmt = QTextCharFormat()
        fmt.setForeground(fg)
        if bg is not None:
            fmt.setBackground(bg)
        return fmt

    @staticmethod
    def _insert(cursor: QTextCursor, text: str, fmt: QTextCharFormat) -> None:
        if not text:
            return
        cursor.insertText(text, fmt)

    # ---- 锚点重建（用户手动编辑后） ----

    def recompute_anchors(self) -> None:
        """从当前文档的字符格式重建修改锚点。

        - 删除线 → delete
        - 绿底（非删除线）→ insert
        - 相邻 delete+insert 合并为 replace
        """
        doc = self._edit.document()
        n = doc.characterCount()
        probe = QTextCursor(doc)  # QTextDocument 无 characterFormat，需经 cursor 探测
        deletes: list[tuple[int, int]] = []
        inserts: list[tuple[int, int]] = []

        def _fmt_at(pos: int):
            probe.setPosition(pos)
            return probe.charFormat()

        i = 0
        while i < n:
            fmt = _fmt_at(i)
            if fmt.fontStrikeOut():
                j = i
                while j < n and _fmt_at(j).fontStrikeOut():
                    j += 1
                deletes.append((i, j))
                i = j
                continue
            bg = fmt.background().color()
            if bg.isValid() and bg == INSERT_BG:
                j = i
                while j < n:
                    f2 = _fmt_at(j)
                    b2 = f2.background().color()
                    if (not f2.fontStrikeOut()
                            and b2.isValid() and b2 == INSERT_BG):
                        j += 1
                    else:
                        break
                inserts.append((i, j))
                i = j
                continue
            i += 1

        anchors: list[tuple[int, int, str]] = []
        d_idx = 0
        ins_idx = 0
        while d_idx < len(deletes) or ins_idx < len(inserts):
            d = deletes[d_idx] if d_idx < len(deletes) else None
            ins = inserts[ins_idx] if ins_idx < len(inserts) else None
            if d and ins and d[1] == ins[0]:
                anchors.append((d[0], ins[1], "replace"))
                d_idx += 1
                ins_idx += 1
            elif d and (not ins or d[0] < ins[0]):
                anchors.append((d[0], d[1], "delete"))
                d_idx += 1
            else:
                anchors.append((ins[0], ins[1], "insert"))
                ins_idx += 1

        self._change_anchors = anchors
        self._current_anchor_idx = -1
        self._notify()

    def on_text_changed(self) -> None:
        """编辑器 textChanged 信号入口（渲染期间跳过）。"""
        if self._skip_recompute:
            return
        self.recompute_anchors()

    # ---- 引用高亮 ----

    def highlight_citations(self) -> None:
        """高亮引用标记（只改前景色，避免覆盖绿底/红底导致锚点探测失效）。"""
        doc = self._edit.document()
        plain = doc.toPlainText()
        h_fmt = QTextCharFormat()
        h_fmt.setForeground(_CITE_FG)
        cursor = QTextCursor(doc)
        for pattern in _CITATION_PATTERNS:
            for m in re.finditer(pattern, plain):
                cursor.setPosition(m.start())
                cursor.setPosition(m.end(), QTextCursor.MoveMode.KeepAnchor)
                cursor.mergeCharFormat(h_fmt)

    # ---- 导航 ----

    def navigate(self, delta: int) -> None:
        """从当前光标位置查找上一处/下一处修改并选中。"""
        if not self._change_anchors:
            return
        cur_pos = self._edit.textCursor().position()
        total = len(self._change_anchors)

        if delta > 0:
            for i in range(total):
                if self._change_anchors[i][0] > cur_pos:
                    idx = i
                    break
            else:
                idx = 0
        else:
            idx = -1
            for i in range(total):
                if self._change_anchors[i][1] >= cur_pos:
                    break
                idx = i
            if idx < 0:
                idx = total - 1

        start, end, _ = self._change_anchors[idx]
        cursor = self._edit.textCursor()
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._edit.setTextCursor(cursor)
        self._edit.setFocus()
        self._current_anchor_idx = idx
        self._notify()

    # ---- 接受/拒绝 ----

    def apply_change(self, accept: bool) -> None:
        """接受/拒绝当前选中的修改处。"""
        if not self._change_anchors:
            return
        idx = self._current_anchor_idx
        if idx < 0:
            return

        start, end, kind = self._change_anchors[idx]
        doc = self._edit.document()
        old_len = len(doc.toPlainText())

        cursor = QTextCursor(doc)
        cursor.setPosition(start)
        cursor.setPosition(end, QTextCursor.MoveMode.KeepAnchor)
        self._skip_recompute = True
        cursor.beginEditBlock()

        if accept:
            if kind == "delete":
                cursor.removeSelectedText()
            elif kind == "insert":
                cursor.mergeCharFormat(_plain_fmt())
            else:  # replace: remove red-strikethrough, un-format green
                self._strip_strikethrough_in_selection(cursor, start, end)
        else:
            if kind == "delete":
                cursor.mergeCharFormat(_plain_fmt())
            elif kind == "insert":
                cursor.removeSelectedText()
            else:  # replace: remove green, un-format red
                self._strip_green_in_selection(cursor, start, end)

        cursor.endEditBlock()
        self._skip_recompute = False
        new_len = len(doc.toPlainText())
        delta = new_len - old_len

        self._change_anchors.pop(idx)
        for i in range(idx, len(self._change_anchors)):
            s, e, k = self._change_anchors[i]
            self._change_anchors[i] = (s + delta, e + delta, k)

        self.highlight_citations()

        total = len(self._change_anchors)
        if total > 0:
            if idx >= total:
                idx = total - 1
            self._current_anchor_idx = idx
            s, e, _ = self._change_anchors[idx]
            cursor = QTextCursor(doc)
            cursor.setPosition(s)
            cursor.setPosition(e, QTextCursor.MoveMode.KeepAnchor)
            self._edit.setTextCursor(cursor)
        self._edit.setFocus()
        self._notify()

    def accept_all(self) -> None:
        """全部接受（从第一处开始循环处理）。"""
        while self._change_anchors:
            self._current_anchor_idx = 0
            self.apply_change(accept=True)

    def reject_all(self) -> None:
        """全部拒绝（从第一处开始循环处理）。"""
        while self._change_anchors:
            self._current_anchor_idx = 0
            self.apply_change(accept=False)

    def accepted_text(self) -> str:
        """全部接受后的最终文本（调用前需先 accept_all）。"""
        return self._edit.toPlainText()

    # ---- 选区工具 ----

    @staticmethod
    def _strip_strikethrough_in_selection(cursor: QTextCursor,
                                          sel_start: int, sel_end: int) -> None:
        """删除选区内的删除线文本，保留其余。"""
        cursor.setPosition(sel_start)
        while cursor.position() < sel_end:
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter,
                                QTextCursor.MoveMode.KeepAnchor)
            if cursor.charFormat().fontStrikeOut():
                cursor.removeSelectedText()
                sel_end -= 1
            else:
                cursor.clearSelection()
        cursor.setPosition(sel_start)
        cursor.setPosition(sel_end, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(_plain_fmt())

    @staticmethod
    def _strip_green_in_selection(cursor: QTextCursor,
                                  sel_start: int, sel_end: int) -> None:
        """删除选区内的绿色文本，保留其余。"""
        cursor.setPosition(sel_start)
        while cursor.position() < sel_end:
            cursor.movePosition(QTextCursor.MoveOperation.NextCharacter,
                                QTextCursor.MoveMode.KeepAnchor)
            cf = cursor.charFormat()
            if cf.background().color() == INSERT_BG:
                cursor.removeSelectedText()
                sel_end -= 1
            else:
                cursor.clearSelection()
        cursor.setPosition(sel_start)
        cursor.setPosition(sel_end, QTextCursor.MoveMode.KeepAnchor)
        cursor.mergeCharFormat(_plain_fmt())
