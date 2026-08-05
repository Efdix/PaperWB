"""草稿整体评价报告对话框 —— 可交互：采纳/忽略/编辑每个发现项，保存后注入润色。"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QScrollArea, QWidget,
    QLabel, QFrame, QPushButton, QTextEdit, QCheckBox, QFileDialog,
)
from PySide6.QtCore import Qt, Signal


class ReviewDialog(QDialog):
    """可交互的草稿评价报告对话框。

    信号:
        review_saved(dict): 用户保存评价时发出，携带最终的评价结果。
    """

    review_saved = Signal(dict)

    def __init__(self, result: dict, profile_name: str = "", parent=None):
        super().__init__(parent)
        self._result = result
        self._profile_name = profile_name
        self._editors: list[dict] = []  # [{element, checkbox, text_edit}]
        self.setWindowTitle("草稿整体评价报告")
        self.resize(640, 750)
        self.setMinimumSize(500, 450)
        self.setWindowFlags(
            self.windowFlags()
            | Qt.WindowType.WindowMaximizeButtonHint
            | Qt.WindowType.WindowMinimizeButtonHint
            | Qt.WindowType.Window
        )
        self._setup_ui()

    # ---- UI 构建 ----

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setStyleSheet("QScrollArea { border: none; background: #1a1b26; }")

        container = QWidget()
        container.setStyleSheet("background: #1a1b26;")
        self._cl = QVBoxLayout(container)
        self._cl.setContentsMargins(24, 20, 24, 20)
        self._cl.setSpacing(10)

        # 总体评分
        self._build_overall()

        # 各部分分析 (actionable)
        self._build_section_analysis()

        # 过渡与小结 (actionable)
        self._build_transition_gaps()

        # 冗余 (actionable)
        self._build_redundancy()

        # 术语一致性 (actionable)
        self._build_terminology()

        # 覆盖分析 (reference)
        self._build_coverage()

        # 文献时效性 (reference)
        self._build_timeliness()

        # 批判性深度 (reference)
        self._build_critical_depth()

        # 图表建议 (actionable)
        self._build_figure_suggestions()

        self._cl.addStretch()
        scroll.setWidget(container)
        layout.addWidget(scroll)

        # 底部按钮
        btn_row = QWidget()
        btn_row.setStyleSheet("background: #1a1b26;")
        btn_lo = QHBoxLayout(btn_row)
        btn_lo.setContentsMargins(24, 10, 24, 16)
        btn_lo.addStretch()

        cancel_btn = QPushButton("\u53d6\u6d88")
        cancel_btn.clicked.connect(self.reject)
        cancel_btn.setStyleSheet(
            "QPushButton { background: #3b3d54; color: #cfd2e3; "
            "border-radius: 6px; padding: 6px 18px; font-size: 13px; }"
            "QPushButton:hover { background: #4a4d6a; }"
        )
        btn_lo.addWidget(cancel_btn)

        save_btn = QPushButton("\ud83d\udcbe \u4fdd\u5b58\u8bc4\u4ef7")
        save_btn.clicked.connect(self._on_save)
        save_btn.setStyleSheet(
            "QPushButton { background: #7aa2f7; color: #1a1b26; font-weight: bold; "
            "border-radius: 6px; padding: 6px 24px; font-size: 13px; }"
            "QPushButton:hover { background: #89b4fa; }"
        )
        btn_lo.addWidget(save_btn)

        export_btn = QPushButton("\ud83d\udcc4 \u5bfc\u51fa TXT")
        export_btn.clicked.connect(self._export_txt)
        export_btn.setStyleSheet(
            "QPushButton { background: #3b3d54; color: #cfd2e3; "
            "border-radius: 6px; padding: 6px 18px; font-size: 13px; }"
            "QPushButton:hover { background: #4a4d6a; }"
        )
        btn_lo.addWidget(export_btn)
        layout.addWidget(btn_row)

    # ---- 辅助方法 ----

    @staticmethod
    def _section_header(title: str, color: str = "#7aa2f7") -> QLabel:
        header = QLabel(title)
        header.setStyleSheet(
            f"color: {color}; font-size: 15px; font-weight: bold; "
            f"border-left: 3px solid {color}; padding-left: 10px; margin-top: 10px;"
        )
        return header

    @staticmethod
    def _sep():
        s = QFrame()
        s.setFrameShape(QFrame.Shape.HLine)
        s.setStyleSheet("background-color: #2a2c3d; max-height: 1px;")
        return s

    @staticmethod
    def _static_text(content: str, color: str = "#a9b1d6") -> QLabel:
        body = QLabel(content)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        body.setStyleSheet(
            f"color: {color}; font-size: 12px; line-height: 1.7; "
            "padding: 6px 12px; background: #1e2030; border-radius: 6px;"
        )
        return body

    def _add_finding_card(self, category: str, title: str, suggestion: str = "",
                          accepted: bool = True) -> None:
        """添加一个可交互的发现项卡片：复选框 + 标题 + 可编辑建议。"""
        card = QFrame()
        card.setStyleSheet(
            "QFrame { background: #1e2030; border-radius: 8px; "
            "padding: 2px; margin-bottom: 4px; }"
        )
        card_layout = QVBoxLayout(card)
        card_layout.setContentsMargins(12, 8, 12, 8)
        card_layout.setSpacing(4)

        # 顶部行：复选框 + 类别标签 + 标题
        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        cb = QCheckBox()
        cb.setChecked(accepted)
        cb.setStyleSheet(
            "QCheckBox { color: #a9b1d6; font-size: 12px; }"
            "QCheckBox::indicator { width: 16px; height: 16px; }"
        )
        top_row.addWidget(cb)

        cat_label = QLabel(f"<span style='color:#565a7a;'>{category}</span>")
        cat_label.setTextFormat(Qt.TextFormat.RichText)
        top_row.addWidget(cat_label)

        title_label = QLabel(title)
        title_label.setWordWrap(True)
        title_label.setStyleSheet("color: #cfd2e3; font-size: 12px; font-weight: bold;")
        top_row.addWidget(title_label, 1)
        card_layout.addLayout(top_row)

        # 建议编辑框
        edit = QTextEdit()
        edit.setPlainText(suggestion)
        edit.setMaximumHeight(60)
        edit.setMinimumHeight(40)
        edit.setStyleSheet(
            "QTextEdit { background-color: #24253a; color: #a9b1d6; "
            "border: 1px solid #3b3d54; border-radius: 4px; "
            "padding: 4px 6px; font-size: 11px; }"
            "QTextEdit:focus { border-color: #7aa2f7; }"
        )
        card_layout.addWidget(edit)

        def _on_toggle(checked: bool):
            edit.setReadOnly(not checked)
            if checked:
                edit.setStyleSheet(
                    "QTextEdit { background-color: #24253a; color: #a9b1d6; "
                    "border: 1px solid #3b3d54; border-radius: 4px; "
                    "padding: 4px 6px; font-size: 11px; }"
                    "QTextEdit:focus { border-color: #7aa2f7; }"
                )
            else:
                edit.setStyleSheet(
                    "QTextEdit { background-color: #1a1b26; color: #565a7a; "
                    "border: 1px solid #2a2c3d; border-radius: 4px; "
                    "padding: 4px 6px; font-size: 11px; }"
                )

        cb.toggled.connect(_on_toggle)

        self._editors.append({
            "checkbox": cb,
            "text_edit": edit,
            "category": category,
            "title": title,
        })
        self._cl.addWidget(card)

    # ---- 各区块构建 ----

    def _build_overall(self):
        result = self._result
        grade = result.get("overall_grade", "?")
        grade_color = {
            "A+": "#9ece6a", "A": "#9ece6a",
            "B+": "#e0af68", "B": "#e0af68",
            "C": "#f7768e", "D": "#f7768e",
        }.get(grade, "#a9b1d6")

        grade_text = (
            f"<span style='color:#cfd2e3; font-size:16px;'>总体评分：</span>"
            f"<span style='color:{grade_color}; font-size:28px; font-weight:bold;'>{grade}</span>"
        )
        grade_widget = QLabel(grade_text)
        grade_widget.setTextFormat(Qt.TextFormat.RichText)
        grade_widget.setStyleSheet("padding: 6px 0;")
        self._cl.addWidget(grade_widget)

        overall = result.get("overall_summary", "")
        if overall:
            self._cl.addWidget(self._static_text(overall, "#cfd2e3"))
        self._cl.addWidget(self._sep())

    def _build_section_analysis(self):
        sa = self._result.get("section_analysis", [])
        if not sa:
            return
        self._cl.addWidget(self._section_header("各部分分析", "#7aa2f7"))

        for s in sa:
            section = s.get("section", "?")
            issues: list[str] = []

            # 引用问题
            cs = s.get("citation_status", "")
            if cs and cs not in ("达标", "无基准"):
                issues.append(
                    f"引用{s.get('citation_count','?')}篇（基准{s.get('citation_benchmark','?')}篇）— {cs}"
                )
            cdi = s.get("citation_detail_issue")
            if cdi:
                issues.append(cdi)

            # 字数问题
            ws = s.get("word_count_status", "")
            if ws and ws not in ("达标", "无基准"):
                issues.append(
                    f"字数约{s.get('word_count','?')}字（基准{s.get('word_count_benchmark','?')}字）— {ws}"
                )

            # 段数问题
            ps = s.get("paragraph_count_status", "")
            if ps and ps not in ("达标", "无基准"):
                issues.append(
                    f"段数{s.get('paragraph_count','?')}（基准{s.get('paragraph_benchmark','?')}）— {ps}"
                )

            # 小结
            if not s.get("has_summary"):
                issues.append("缺少章节小结")

            # 段落问题
            psi = s.get("paragraph_size_issue")
            if psi:
                issues.append(str(psi))

            # 其他
            for oi in s.get("other_issues", []):
                issues.append(str(oi))

            if issues:
                self._add_finding_card(
                    category=section,
                    title="；".join(issues),
                    suggestion=f"请在「{section}」章节中修正以上问题。",
                )

    def _build_transition_gaps(self):
        tsg = self._result.get("transition_summary_gaps", {}) or {}
        gaps = tsg.get("gaps", [])
        missing_sums = tsg.get("missing_summaries", [])
        if not gaps and not missing_sums:
            return
        self._cl.addWidget(self._sep())
        self._cl.addWidget(self._section_header("过渡与小结", "#e0af68"))

        for g in gaps:
            if g.get("severity") in ("缺失", "偏弱"):
                self._add_finding_card(
                    category=f"过渡 — {g.get('between', '?')}",
                    title=f"{g.get('severity', '?')}",
                    suggestion=g.get("suggestion", ""),
                )

        for ms in missing_sums:
            self._add_finding_card(
                category="缺少小结",
                title=str(ms),
                suggestion=f"在「{ms}」末尾添加 1-2 句小结段落。",
            )

    def _build_redundancy(self):
        items = self._result.get("redundancy", {}).get("items", [])
        if not items:
            return
        self._cl.addWidget(self._sep())
        self._cl.addWidget(self._section_header("冗余检查", "#f7768e"))

        for r in items:
            locs = "、".join(str(x) for x in r.get("locations", []))
            self._add_finding_card(
                category="冗余",
                title=f"{r.get('point', '?')}（{locs}）",
                suggestion=f"建议删除或合并重复内容：{r.get('point', '?')}",
            )

    def _build_terminology(self):
        issues = self._result.get("terminology_consistency", {}).get("issues", [])
        if not issues:
            return
        self._cl.addWidget(self._sep())
        self._cl.addWidget(self._section_header("术语一致性", "#e0af68"))

        for t in issues:
            variants = " / ".join(str(x) for x in t.get("variants", []))
            self._add_finding_card(
                category="术语不统一",
                title=f"{t.get('concept', '?')}：{variants}",
                suggestion=t.get("suggestion", ""),
            )

    def _build_coverage(self):
        ca = self._result.get("coverage_analysis", {}) or {}
        cd_list = ca.get("covered_domains", [])
        over = ca.get("overrepresented", "")
        missing = ca.get("missing_or_thin", "")
        sug = ca.get("suggestion", "")
        if not (cd_list or over or missing or sug):
            return
        self._cl.addWidget(self._sep())
        self._cl.addWidget(self._section_header("覆盖分析（参考）", "#bb9af7"))

        if cd_list:
            items = []
            for d in cd_list:
                dom = d.get("domain", "?")
                cov = d.get("coverage", "?")
                cov_color = {"充分": "#9ece6a", "一般": "#e0af68", "薄弱": "#f7768e"}.get(cov, "#a9b1d6")
                items.append(f"<span style='color:{cov_color};'>{cov}</span> — {dom}")
            self._cl.addWidget(self._static_text("\n".join(f"· {i}" for i in items)))
        if over:
            self._cl.addWidget(self._static_text(f"占比过大: {over}", "#f7768e"))
        if missing:
            self._cl.addWidget(self._static_text(f"薄弱或遗漏: {missing}", "#e0af68"))
        if sug:
            self._cl.addWidget(self._static_text(f"建议: {sug}", "#9ece6a"))

    def _build_timeliness(self):
        tl = self._result.get("timeliness", {}) or {}
        total = tl.get("total_citations", "?")
        r3y = tl.get("recent_3yr", "?")
        cla = tl.get("classic_before_3yr", "?")
        ass = tl.get("assessment", "")
        sug = tl.get("suggestion", "")
        if not total and not ass:
            return
        self._cl.addWidget(self._sep())
        self._cl.addWidget(self._section_header("文献时效性（参考）", "#e0af68"))

        ass_color = {
            "优秀": "#9ece6a", "合理": "#9ece6a",
            "偏旧": "#e0af68", "缺乏近3年文献": "#f7768e",
        }.get(ass, "#a9b1d6")
        content = f"共 {total} 篇引用：近 3 年 {r3y} 篇，3 年前 {cla} 篇"
        if ass:
            content += f"\n评价：<span style='color:{ass_color};'>{ass}</span>"
        if sug:
            content += f"\n建议：{sug}"
        self._cl.addWidget(self._static_text(content))

    def _build_critical_depth(self):
        cdepth = self._result.get("critical_depth", {}) or {}
        if not cdepth:
            return
        checks = []
        labels = [
            ("has_comparison", "横向对比"),
            ("has_contradiction_discussion", "矛盾讨论"),
            ("has_gap_analysis", "研究缺口"),
            ("has_future_directions", "未来方向"),
        ]
        for key, label in labels:
            val = cdepth.get(key, False)
            icon = "\u2705" if val else "\u274c"
            color = "#9ece6a" if val else "#636688"
            checks.append(f"<span style='color:{color};'>{icon} {label}</span>")
        ass = cdepth.get("assessment", "")
        sug = cdepth.get("suggestion", "")
        self._cl.addWidget(self._sep())
        self._cl.addWidget(self._section_header("批判性深度（参考）", "#bb9af7"))
        text = "  |  ".join(checks)
        if ass:
            text += f"\n\n{ass}"
        if sug:
            text += f"\n\n建议：{sug}"
        self._cl.addWidget(self._static_text(text))

    def _build_figure_suggestions(self):
        items = self._result.get("figure_suggestions", {}).get("items", [])
        if not items:
            return
        self._cl.addWidget(self._sep())
        self._cl.addWidget(self._section_header("图表建议", "#e0af68"))

        for f in items:
            self._add_finding_card(
                category=f.get("type", "图表"),
                title=f.get("location", "?"),
                suggestion=f.get("purpose", ""),
            )

    # ---- 保存 ----

    def _on_save(self):
        """收集所有采纳项，构建结构化评价结果，持久化并发出信号。"""
        review = dict(self._result)  # 浅拷贝

        # 收集采纳/忽略状态
        accepted_items: list[dict] = []
        rejected_items: list[dict] = []
        for editor in self._editors:
            item = {
                "category": editor["category"],
                "title": editor["title"],
                "suggestion": editor["text_edit"].toPlainText().strip(),
            }
            if editor["checkbox"].isChecked():
                accepted_items.append(item)
            else:
                rejected_items.append(item)

        review["_accepted_items"] = accepted_items
        review["_rejected_items"] = rejected_items

        # 持久化
        if self._profile_name:
            from ..utils.config import save_review
            save_review(self._profile_name, review)

        self.review_saved.emit(review)
        self.accept()

    # ---- 导出 TXT ----

    def _export_txt(self):
        """将评价结果格式化为可读文本并导出为 TXT 文件。"""
        path, _ = QFileDialog.getSaveFileName(
            self, "导出评价报告", "草稿评价报告.txt", "文本文件 (*.txt);;所有文件 (*.*)"
        )
        if not path:
            return

        result = self._result
        lines: list[str] = []
        w = lines.append

        w("=" * 50)
        w("草稿整体评价报告")
        w("=" * 50)
        w(f"总体评分: {result.get('overall_grade', '?')}")
        w("")
        w(result.get("overall_summary", ""))
        w("")

        # 各部分分析
        sa = result.get("section_analysis", [])
        if sa:
            w("-" * 40)
            w("各部分分析")
            w("-" * 40)
            for s in sa:
                w(f"\n【{s.get('section', '?')}】")
                w(f"  字数: {s.get('word_count', '?')} (基准: {s.get('word_count_benchmark', -1)}) → {s.get('word_count_status', '?')}")
                w(f"  段数: {s.get('paragraph_count', '?')} (基准: {s.get('paragraph_benchmark', -1)}) → {s.get('paragraph_count_status', '?')}")
                w(f"  引用: {s.get('citation_count', '?')} 篇 (基准: {s.get('citation_benchmark', -1)}) → {s.get('citation_status', '?')}")
                w(f"  小结: {'有' if s.get('has_summary') else '缺少'}")
                if s.get("citation_detail_issue"):
                    w(f"  引用详略: {s['citation_detail_issue']}")
                if s.get("paragraph_size_issue"):
                    w(f"  段落问题: {s['paragraph_size_issue']}")
                for oi in s.get("other_issues", []):
                    w(f"  其他: {oi}")
            w("")

        # 过渡与小结
        tsg = result.get("transition_summary_gaps", {})
        gaps = tsg.get("gaps", []) if tsg else []
        missing = tsg.get("missing_summaries", []) if tsg else []
        if gaps or missing:
            w("-" * 40)
            w("过渡与小结")
            w("-" * 40)
            for g in gaps:
                w(f"  {g.get('severity', '?')} — {g.get('between', '?')}: {g.get('suggestion', '')}")
            for ms in missing:
                w(f"  缺少小结: {ms}")
            w("")

        # 覆盖分析
        ca = result.get("coverage_analysis", {})
        if ca:
            w("-" * 40)
            w("覆盖分析")
            w("-" * 40)
            for d in ca.get("covered_domains", []):
                w(f"  {d.get('coverage', '?')}: {d.get('domain', '?')}")
            if ca.get("overrepresented"):
                w(f"  占比过大: {ca['overrepresented']}")
            if ca.get("missing_or_thin"):
                w(f"  薄弱/遗漏: {ca['missing_or_thin']}")
            if ca.get("suggestion"):
                w(f"  建议: {ca['suggestion']}")
            w("")

        # 时效性
        tl = result.get("timeliness", {})
        if tl:
            w("-" * 40)
            w("文献时效性")
            w("-" * 40)
            w(f"  共 {tl.get('total_citations', '?')} 篇: 近3年 {tl.get('recent_3yr', '?')} 篇, 3年前 {tl.get('classic_before_3yr', '?')} 篇")
            w(f"  评价: {tl.get('assessment', '?')}")
            if tl.get("suggestion"):
                w(f"  建议: {tl['suggestion']}")
            w("")

        # 批判性深度
        cd = result.get("critical_depth", {})
        if cd:
            w("-" * 40)
            w("批判性深度")
            w("-" * 40)
            checks = [
                ("横向对比", cd.get("has_comparison")),
                ("矛盾讨论", cd.get("has_contradiction_discussion")),
                ("研究缺口", cd.get("has_gap_analysis")),
                ("未来方向", cd.get("has_future_directions")),
            ]
            for label, val in checks:
                w(f"  {'[x]' if val else '[ ]'} {label}")
            if cd.get("assessment"):
                w(f"  评价: {cd['assessment']}")
            if cd.get("suggestion"):
                w(f"  建议: {cd['suggestion']}")
            w("")

        # 冗余
        rd = result.get("redundancy", {}).get("items", [])
        if rd:
            w("-" * 40)
            w("冗余检查")
            w("-" * 40)
            for r in rd:
                locs = "、".join(str(x) for x in r.get("locations", []))
                w(f"  {r.get('point', '?')} ({locs})")
            w("")

        # 图表建议
        fs = result.get("figure_suggestions", {}).get("items", [])
        if fs:
            w("-" * 40)
            w("图表建议")
            w("-" * 40)
            for f in fs:
                w(f"  {f.get('location', '?')} — {f.get('type', '?')}: {f.get('purpose', '?')}")
            w("")

        # 术语
        tc = result.get("terminology_consistency", {}).get("issues", [])
        if tc:
            w("-" * 40)
            w("术语一致性")
            w("-" * 40)
            for t in tc:
                variants = " / ".join(str(x) for x in t.get("variants", []))
                w(f"  {t.get('concept', '?')}: {variants} → {t.get('suggestion', '?')}")
            w("")

        try:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines))
        except OSError as e:
            from PySide6.QtWidgets import QMessageBox
            QMessageBox.warning(self, "导出失败", str(e))
