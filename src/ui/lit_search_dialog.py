"""文献补充对话框 —— LLM 分析 → 用户反馈循环 → PubMed 检索。"""

from __future__ import annotations

import json as _json
import csv
import os
import re

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QScrollArea, QFrame, QFileDialog, QProgressBar,
    QApplication, QListWidget, QListWidgetItem, QSplitter,
)
from PySide6.QtCore import Qt, QThread, Signal


# ============================================================
# 后台线程
# ============================================================

class LitAnalysisWorker(QThread):
    """LLM 分析草稿 -> 输出遗漏方向和搜索关键词。"""
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, client, draft_text: str, system_prompt: str = ""):
        super().__init__()
        self._client = client
        self._draft = draft_text
        self._system = system_prompt

    def run(self):
        prompt = LitSearchDialog.ANALYSIS_PROMPT.replace("{draft_text}", self._draft[:8000])
        messages = [
            {"role": "system", "content": self._system or "你是学术文献检索专家。只返回 JSON，不要加解释。"},
            {"role": "user", "content": prompt},
        ]
        try:
            response = self._client.chat_sync(messages, timeout=120.0, max_tokens=4000)
            if not response or not response.strip():
                self.error.emit("LLM 返回空响应，请稍后重试")
                return
            data = LitSearchDialog._parse_json(response)
            if data:
                self.finished.emit(data)
            else:
                self.error.emit(f"无法解析 LLM 返回结果（响应长度 {len(response)} 字符）")
        except Exception as e:
            self.error.emit(str(e))


class LitRefineWorker(QThread):
    """根据用户反馈重新分析。"""
    finished = Signal(dict)
    error = Signal(str)

    def __init__(self, client, draft_text: str, previous: dict, feedback: str):
        super().__init__()
        self._client = client
        self._draft = draft_text
        self._previous = _json.dumps(previous, ensure_ascii=False, indent=2)
        self._feedback = feedback

    def run(self):
        prompt = (LitSearchDialog.REFINE_PROMPT
            .replace("{draft_text}", self._draft[:8000])
            .replace("{previous_analysis}", self._previous)
            .replace("{user_feedback}", self._feedback))
        messages = [
            {"role": "system", "content": "你是学术文献检索专家。只返回 JSON，不要加解释。"},
            {"role": "user", "content": prompt},
        ]
        try:
            response = self._client.chat_sync(messages, timeout=120.0, max_tokens=4000)
            if not response or not response.strip():
                self.error.emit("LLM 返回空响应，请稍后重试")
                return
            data = LitSearchDialog._parse_json(response)
            if data:
                self.finished.emit(data)
            else:
                self.error.emit(f"无法解析 LLM 返回结果（响应长度 {len(response)} 字符）")
        except Exception as e:
            self.error.emit(str(e))


class PubMedSearchWorker(QThread):
    """后台 PubMed 检索。"""
    progress = Signal(str)
    finished = Signal(list)
    error = Signal(str)

    def __init__(self, queries: list[str]):
        super().__init__()
        self._queries = queries

    def run(self):
        from ..core.pubmed_searcher import PubMedSearcher
        try:
            self.progress.emit("正在检索 PubMed...")
            searcher = PubMedSearcher()
            papers = searcher.search(self._queries, limit=10)
            self.finished.emit(papers)
        except Exception as e:
            self.error.emit(str(e))


# ============================================================
# 对话框
# ============================================================

class LitSearchDialog(QDialog):
    """文献补充三步交互对话框。

    Step 1: LLM 分析草稿
    Step 2: 用户反馈（可多轮循环）
    Step 3: PubMed 检索
    """

    ANALYSIS_PROMPT = """你是学术文献检索专家。请分析以下综述草稿，找出可能遗漏的研究方向并生成 PubMed 检索关键词。

【草稿】
{draft_text}

## 任务

1. 归纳草稿覆盖的研究方向（列出具体方向名称、大致文献数量、最新年份）
2. 找出草稿可能遗漏的研究方向（横向遗漏：完全没覆盖的领域；纵向遗漏：已覆盖但缺少的最新文献）
3. 为每个遗漏方向生成 3-5 条 PubMed 检索用的英文关键词

## 输出格式

请严格返回 JSON（不要加 Markdown 标记）：

{{
  "covered_domains": [{{"domain": "方向名", "paper_count": 0, "latest_year": ""}}],
  "gaps": [{{"domain": "遗漏方向名", "reason": "为什么需要补充", "search_queries": ["关键词1", "关键词2"]}}]
}}

注意：
- 每个 gap 的 search_queries 应该是能直接在 PubMed 搜索框中使用的英文关键词
- 关键词要具体而非泛泛，如 "avian melanocyte scRNA-seq" 而非 "bird color"
- 如果有长关键词，拆分为 2-3 条互补的短关键词"""

    REFINE_PROMPT = """你之前分析了一篇综述草稿，现在用户对你的分析给出了反馈。请根据反馈修正你的分析。

【草稿】
{draft_text}

【你上次的分析】
{previous_analysis}

【用户反馈】
{user_feedback}

请重新生成完整的 JSON 分析结果（格式不变）。特别注意用户指出的理解错误，务必修正。"""

    def __init__(self, client, coach=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文献补充")
        self.resize(900, 650)
        self.setMinimumSize(700, 500)

        self._client = client
        self._coach = coach
        self._draft_text = ""
        self._analysis_data: dict | None = None
        self._worker: LitAnalysisWorker | LitRefineWorker | PubMedSearchWorker | None = None

        self._setup_ui()

    def set_draft_text(self, text: str):
        self._draft_text = text
        # 自动触发分析
        self._start_analysis()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        # ---- Step 1 + 2: 分析结果 + 反馈 ----
        analysis_group = self._make_section("LLM 理解")

        self._analysis_label = QLabel("正在分析...")
        self._analysis_label.setWordWrap(True)
        self._analysis_label.setStyleSheet(
            "color: #cfd2e3; font-size: 13px; line-height: 1.6; "
            "background-color: #1e2030; border: 1px solid #2a2c3d; "
            "border-radius: 6px; padding: 12px;"
        )
        analysis_group.layout().addWidget(self._analysis_label)

        layout.addWidget(analysis_group)

        # ---- 反馈输入 ----
        feedback_group = self._make_section("反馈（告诉 LLM 哪里理解不对）")
        self._feedback_edit = QTextEdit()
        self._feedback_edit.setPlaceholderText("例如：方向3不对，我讨论的是体表图案的细胞机制而非代谢通路，把那几个代谢方向去掉...")
        self._feedback_edit.setMaximumHeight(90)
        self._feedback_edit.setStyleSheet(
            "QTextEdit { background-color: #24253a; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; "
            "padding: 8px; font-size: 13px; }"
            "QTextEdit:focus { border-color: #7aa2f7; }"
        )
        feedback_group.layout().addWidget(self._feedback_edit)
        layout.addWidget(feedback_group)

        # ---- 按钮行 ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)
        btn_row.addStretch()
        self._refine_btn = QPushButton("让 LLM 重新分析")
        self._refine_btn.clicked.connect(self._on_refine)
        self._refine_btn.setEnabled(False)
        btn_row.addWidget(self._refine_btn)
        self._search_btn = QPushButton("开始 PubMed 检索")
        self._search_btn.setObjectName("primaryBtn")
        self._search_btn.clicked.connect(self._on_search)
        self._search_btn.setEnabled(False)
        btn_row.addWidget(self._search_btn)
        skip_btn = QPushButton("跳过检索")
        skip_btn.clicked.connect(self.reject)
        btn_row.addWidget(skip_btn)
        layout.addLayout(btn_row)

        # ---- 进度条 ----
        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximumHeight(14)
        self._progress.setStyleSheet(
            "QProgressBar { background-color: #24253a; border: 1px solid #3b3d54; border-radius: 7px; }"
            "QProgressBar::chunk { background-color: #7aa2f7; border-radius: 6px; }"
        )
        layout.addWidget(self._progress)

        # ---- Step 3: 检索结果（初始隐藏） ----
        self._results_group = self._make_section("PubMed 检索结果")
        self._results_group.setVisible(False)
        self._results_list = QListWidget()
        self._results_list.setStyleSheet(
            "QListWidget { background-color: #1e2030; border: none; font-size: 13px; }"
            "QListWidget::item { padding: 8px; border-bottom: 1px solid #2a2c3d; }"
            "QListWidget::item:hover { background-color: #24253a; }"
        )
        self._results_group.layout().addWidget(self._results_list)
        layout.addWidget(self._results_group)

        # 结果页底部按钮
        result_btns = QHBoxLayout()
        result_btns.addStretch()
        self._export_btn = QPushButton("导出 CSV")
        self._export_btn.clicked.connect(self._export_csv)
        self._export_btn.setVisible(False)
        result_btns.addWidget(self._export_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        result_btns.addWidget(close_btn)
        layout.addLayout(result_btns)

    @staticmethod
    def _make_section(title: str) -> QFrame:
        frame = QFrame()
        frame.setStyleSheet("background-color: #1a1b26; border: none;")
        lay = QVBoxLayout(frame)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        header = QLabel(title)
        header.setStyleSheet("color: #a9b1d6; font-weight: bold; font-size: 13px; padding: 2px 0;")
        lay.addWidget(header)
        return frame

    # ---- 分析 ----

    def _start_analysis(self):
        if not self._draft_text.strip() or not self._client:
            self._analysis_label.setText("（无文本或未配置 API）")
            return

        self._set_busy(True, "LLM 正在分析草稿...")
        system_prompt = ""
        if self._coach:
            system_prompt = self._coach.build_writing_system_prompt("综述")

        self._worker = LitAnalysisWorker(self._client, self._draft_text, system_prompt)
        self._worker.finished.connect(self._on_analysis_done)
        self._worker.error.connect(self._on_analysis_error)
        self._worker.start()

    def _on_analysis_done(self, data: dict):
        self._analysis_data = data
        self._render_analysis(data)
        self._set_busy(False)
        self._refine_btn.setEnabled(True)
        self._search_btn.setEnabled(True)
        self._feedback_edit.setFocus()

    def _on_analysis_error(self, err: str):
        self._analysis_label.setText(
            f"<span style='color: #f7768e; font-weight: bold;'>分析失败</span><br>"
            f"<span style='color: #8a8ea6;'>{err}</span>"
        )
        self._set_busy(False)

    def _render_analysis(self, data: dict):
        lines = ["<b>已覆盖方向:</b>"]
        for d in data.get("covered_domains", []):
            lines.append(
                f"  · {d.get('domain', '?')} "
                f"({d.get('paper_count', 0)} 篇, 最新 {d.get('latest_year', '?')})"
            )

        gaps = data.get("gaps", [])
        if gaps:
            lines.append("<br><b>遗漏方向:</b>")
            for i, g in enumerate(gaps):
                queries = g.get("search_queries", [])
                kw_str = ", ".join(f"<i>{q}</i>" for q in queries[:5])
                lines.append(
                    f"  <b>{i+1}. {g.get('domain', '?')}</b><br>"
                    f"    原因: {g.get('reason', '')[:200]}<br>"
                    f"    关键词: {kw_str}"
                )
        else:
            lines.append("<br><b>未检测到明显遗漏方向。</b>")

        self._analysis_label.setText("<br>".join(lines))

    # ---- 反馈修正 ----

    def _on_refine(self):
        feedback = self._feedback_edit.toPlainText().strip()
        if not feedback:
            return
        if not self._analysis_data or not self._draft_text:
            return

        self._set_busy(True, "LLM 正在根据反馈重新分析...")
        self._refine_btn.setEnabled(False)
        self._search_btn.setEnabled(False)

        self._worker = LitRefineWorker(self._client, self._draft_text, self._analysis_data, feedback)
        self._worker.finished.connect(self._on_analysis_done)
        self._worker.error.connect(self._on_analysis_error)
        self._worker.start()
        self._feedback_edit.clear()

    # ---- PubMed 检索 ----

    def _on_search(self):
        if not self._analysis_data:
            return
        gaps = self._analysis_data.get("gaps", [])
        if not gaps:
            return

        queries = []
        for g in gaps:
            queries.extend(g.get("search_queries", []))

        if not queries:
            return

        self._set_busy(True, "正在检索 PubMed...")
        self._refine_btn.setEnabled(False)
        self._search_btn.setEnabled(False)

        self._worker = PubMedSearchWorker(queries)
        self._worker.progress.connect(lambda msg: self._progress.setFormat(msg))
        self._worker.finished.connect(self._on_search_done)
        self._worker.error.connect(self._on_search_error)
        self._worker.start()

    def _on_search_done(self, papers: list):
        self._set_busy(False)
        self._results_group.setVisible(True)
        self._export_btn.setVisible(True)
        self._results_list.clear()

        if not papers:
            self._results_list.addItem("（PubMed 未返回结果）")
            return

        self._results_list.addItem(f"共找到 {len(papers)} 篇文献:")
        for p in papers[:30]:
            text = f"{p.authors} ({p.year})  {p.journal}\n{p.title}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self._results_list.addItem(item)

    def _on_search_error(self, err: str):
        self._set_busy(False)
        self._results_group.setVisible(True)
        self._results_list.clear()
        self._results_list.addItem(f"检索失败: {err}")

    # ---- 导出 ----

    def _export_csv(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "导出文献列表", "literature_supplement.csv",
            "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            papers = []
            for i in range(1, self._results_list.count()):
                item = self._results_list.item(i)
                p = item.data(Qt.ItemDataRole.UserRole)
                if p:
                    papers.append(p)

            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["title", "authors", "year", "journal", "doi", "pmid", "url"])
                for p in papers:
                    writer.writerow([
                        p.title, p.authors, p.year, p.journal,
                        p.doi, p.pmid, p.url,
                    ])
        except Exception as e:
            pass

    # ---- 工具 ----

    def _set_busy(self, busy: bool, msg: str = ""):
        self._progress.setVisible(busy)
        self._progress.setRange(0, 0 if busy else 100)
        if msg:
            self._progress.setFormat(msg)

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        if not raw or not raw.strip():
            return None
        text = raw.strip()
        try:
            return _json.loads(text)
        except (_json.JSONDecodeError, TypeError):
            pass
        for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    return _json.loads(m.group(1).strip())
                except (_json.JSONDecodeError, TypeError):
                    pass
        first = text.find('{')
        last = text.rfind('}')
        if first >= 0 and last > first:
            try:
                return _json.loads(text[first:last + 1])
            except (_json.JSONDecodeError, TypeError):
                pass
        return None
