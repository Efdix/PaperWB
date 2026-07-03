"""文献补充对话框 —— LLM 分析 → 用户反馈循环 → PubMed 检索（循环式交互）。"""

from __future__ import annotations

import csv
import json as _json
import re

from PySide6.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTextEdit, QPushButton,
    QLabel, QScrollArea, QFrame, QFileDialog, QProgressBar,
    QApplication, QListWidget, QListWidgetItem, QMessageBox, QSizePolicy,
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
            response = self._client.chat_sync(messages, timeout=120.0, max_tokens=8000)
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
            response = self._client.chat_sync(messages, timeout=120.0, max_tokens=8000)
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
    """文献补充交互对话框 —— 循环式：LLM 分析 ↔ 反馈 ↔ PubMed 检索。

    用户可以：
    1. 查看 LLM 对草稿的分析（遗漏方向 + 检索关键词）
    2. 发送反馈让 LLM 重新分析（可多轮）
    3. 执行 PubMed 检索查看文献
    4. 查看检索结果后继续反馈重新分析 + 重新检索
    5. 随时导出 CSV 或关闭
    """

    ANALYSIS_PROMPT = """你是学术文献检索专家。请分析以下综述草稿，找出可能遗漏的研究方向、列出你已知的相关文献、并生成 PubMed 检索关键词。

【草稿】
{draft_text}

## 任务

1. 归纳草稿覆盖的研究方向（列出具体方向名称、大致文献数量、最新年份）
2. **列出你已知的、与遗漏方向直接相关的具体文献**（如果你脑海中有明确的论文，直接写出标题、作者、年份、DOI 和为什么推荐它）
3. 找出草稿可能遗漏的研究方向（横向遗漏：完全没覆盖的领域；纵向遗漏：已覆盖但缺少的最新文献）
4. 为每个遗漏方向生成 3-5 条 PubMed 检索用的英文关键词

## 输出格式

请严格返回 JSON（不要加 Markdown 标记）：

{
  "covered_domains": [{"domain": "方向名", "paper_count": 0, "latest_year": ""}],
  "known_papers": [{"title": "论文标题", "authors": "Author et al.", "year": 2024, "doi": "10.xxx/xxx", "relevance": "此文献与遗漏方向X直接相关，提供了xxx证据"}],
  "gaps": [{"domain": "遗漏方向名", "reason": "为什么需要补充", "search_queries": ["关键词1", "关键词2"]}]
}

注意：
- known_papers: 只列你确实知道的具体论文（标题不能编造），没有把握就留空数组 []
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

请重新生成完整的 JSON 分析结果（格式不变，包含 covered_domains、known_papers、gaps）。特别注意用户指出的理解错误，务必修正。"""

    def __init__(self, client, coach=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("文献补充")
        self.resize(900, 680)
        self.setMinimumSize(700, 520)

        self._client = client
        self._coach = coach
        self._draft_text = ""
        self._analysis_data: dict | None = None
        self._worker: LitAnalysisWorker | LitRefineWorker | PubMedSearchWorker | None = None
        self._search_keywords: list[str] = []

        self._setup_ui()

    def set_draft_text(self, text: str):
        self._draft_text = text
        self._start_analysis()

    def _setup_ui(self):
        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        # ---- LLM 理解（可滚动） ----
        analysis_header = QLabel("LLM 理解（遗漏方向分析）")
        analysis_header.setStyleSheet("color: #a9b1d6; font-weight: bold; font-size: 13px; padding: 2px 0;")
        layout.addWidget(analysis_header)

        self._analysis_scroll = QScrollArea()
        self._analysis_scroll.setWidgetResizable(True)
        self._analysis_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._analysis_scroll.setStyleSheet(
            "QScrollArea { background-color: #1e2030; border: 1px solid #2a2c3d; border-radius: 6px; }"
            "QScrollBar:vertical { background: #1a1b26; width: 8px; }"
            "QScrollBar::handle:vertical { background: #3b3d54; border-radius: 4px; min-height: 30px; }"
        )

        self._analysis_label = QLabel("正在分析...")
        self._analysis_label.setWordWrap(True)
        self._analysis_label.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self._analysis_label.setStyleSheet(
            "color: #cfd2e3; font-size: 13px; line-height: 1.7; "
            "background-color: transparent; padding: 12px;"
        )
        self._analysis_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._analysis_scroll.setWidget(self._analysis_label)
        layout.addWidget(self._analysis_scroll, 2)

        # ---- 反馈输入 ----
        feedback_label = QLabel("反馈（告诉 LLM 哪里理解不对，可多轮对话）")
        feedback_label.setStyleSheet("color: #a9b1d6; font-weight: bold; font-size: 13px; padding: 2px 0;")
        layout.addWidget(feedback_label)

        self._feedback_edit = QTextEdit()
        self._feedback_edit.setPlaceholderText("例如：方向3不对，我讨论的是体表图案的细胞机制而非代谢通路，把那几个代谢方向去掉...")
        self._feedback_edit.setMaximumHeight(80)
        self._feedback_edit.setStyleSheet(
            "QTextEdit { background-color: #24253a; color: #cfd2e3; "
            "border: 1px solid #3b3d54; border-radius: 6px; "
            "padding: 8px; font-size: 13px; }"
            "QTextEdit:focus { border-color: #7aa2f7; }"
        )
        layout.addWidget(self._feedback_edit)

        # ---- 按钮行 ----
        btn_row = QHBoxLayout()
        btn_row.setSpacing(8)

        self._refine_btn = QPushButton("反馈并重新分析")
        self._refine_btn.setToolTip("将你的反馈发送给 LLM，重新分析遗漏方向")
        self._refine_btn.clicked.connect(self._on_refine)
        self._refine_btn.setEnabled(False)
        btn_row.addWidget(self._refine_btn)

        self._search_btn = QPushButton("开始 PubMed 检索")
        self._search_btn.setToolTip("用当前分析结果中的关键词检索 PubMed")
        self._search_btn.clicked.connect(self._on_search)
        self._search_btn.setEnabled(False)
        btn_row.addWidget(self._search_btn)

        self._export_known_btn = QPushButton("导出LLM推断文献列表")
        self._export_known_btn.setToolTip("将 LLM 直接推荐的文献导出为 CSV")
        self._export_known_btn.clicked.connect(self._export_known_csv)
        self._export_known_btn.setVisible(False)
        btn_row.addWidget(self._export_known_btn)

        btn_row.addStretch()

        self._progress = QProgressBar()
        self._progress.setVisible(False)
        self._progress.setMaximumHeight(14)
        self._progress.setMaximumWidth(200)
        self._progress.setStyleSheet(
            "QProgressBar { background-color: #24253a; border: 1px solid #3b3d54; border-radius: 7px; }"
            "QProgressBar::chunk { background-color: #7aa2f7; border-radius: 6px; }"
        )
        btn_row.addWidget(self._progress)
        layout.addLayout(btn_row)

        # ---- 检索结果（初始隐藏） ----
        results_header = QLabel("PubMed 检索结果")
        results_header.setStyleSheet("color: #a9b1d6; font-weight: bold; font-size: 13px; padding: 2px 0;")
        layout.addWidget(results_header)

        self._results_list = QListWidget()
        self._results_list.setStyleSheet(
            "QListWidget { background-color: #1e2030; border: 1px solid #2a2c3d; border-radius: 6px; font-size: 13px; }"
            "QListWidget::item { padding: 8px; border-bottom: 1px solid #2a2c3d; }"
            "QListWidget::item:hover { background-color: #24253a; }"
        )
        layout.addWidget(self._results_list, 1)

        # 底部按钮
        bottom_row = QHBoxLayout()
        bottom_row.addStretch()
        self._export_btn = QPushButton("导出 CSV")
        self._export_btn.clicked.connect(self._export_csv)
        self._export_btn.setVisible(False)
        bottom_row.addWidget(self._export_btn)
        close_btn = QPushButton("关闭")
        close_btn.clicked.connect(self.reject)
        bottom_row.addWidget(close_btn)
        layout.addLayout(bottom_row)

    # ---- 分析 ----

    def _start_analysis(self):
        if not self._draft_text.strip() or not self._client:
            self._analysis_label.setText("<span style='color: #8a8ea6;'>（无文本或未配置 API）</span>")
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
            f"<span style='color: #8a8ea6;'>{err}</span><br><br>"
            '<span style="color: #a9b1d6;">请在下方反馈中补充你的理解，点 \u201c反馈并重新分析\u201d。</span>'
        )
        self._set_busy(False)
        self._refine_btn.setEnabled(True)
        self._feedback_edit.setFocus()

    def _render_analysis(self, data: dict):
        lines = ["<b style='color: #7aa2f7;'>已覆盖方向:</b>"]
        for d in data.get("covered_domains", []):
            lines.append(
                f"<span style='color: #a9b1d6;'>  &middot; {d.get('domain', '?')}</span> "
                f"<span style='color: #8a8ea6;'>({d.get('paper_count', 0)} 篇, 最新 {d.get('latest_year', '?')})</span>"
            )

        known = data.get("known_papers", [])
        if known:
            lines.append("<br><b style='color: #bb9af7;'>📚 LLM 推断推荐的文献（可导出）:</b>")
            for i, p in enumerate(known):
                title = p.get("title", "?")
                authors = p.get("authors", "?")
                year = p.get("year", "")
                doi = p.get("doi", "")
                relevance = p.get("relevance", "")
                doi_str = f" DOI: {doi}" if doi else ""
                lines.append(
                    f"  <b style='color: #cfd2e3;'>{i+1}. {authors} ({year}) {title}</b>"
                    f"<span style='color: #636688;'>{doi_str}</span><br>"
                    f"  <span style='color: #8a8ea6;'>  → {relevance}</span><br>"
                )
            self._known_papers = known
            self._export_known_btn.setVisible(True)
        else:
            self._known_papers = []
            self._export_known_btn.setVisible(False)

        gaps = data.get("gaps", [])
        if gaps:
            lines.append("<br><b style='color: #e0af68;'>遗漏方向 + 搜索关键词:</b>")
            for i, g in enumerate(gaps):
                queries = g.get("search_queries", [])
                kw_str = ", ".join(f"<span style='color: #9ece6a;'><i>{q}</i></span>" for q in queries[:5])
                reason = g.get("reason", "")[:300]
                lines.append(
                    f"  <b style='color: #cfd2e3;'>{i+1}. {g.get('domain', '?')}</b><br>"
                    f"  <span style='color: #8a8ea6;'>原因: {reason}</span><br>"
                    f"  <span style='color: #636688;'>关键词: {kw_str}</span><br>"
                )
        else:
            lines.append("<br><b style='color: #9ece6a;'>未检测到明显遗漏方向。</b>")

        self._analysis_label.setText("<br>".join(lines))

    # ---- 反馈修正 ----

    def _on_refine(self):
        feedback = self._feedback_edit.toPlainText().strip()
        if not feedback:
            QMessageBox.warning(self, "提示", "请先在下方输入框中填写反馈意见，告诉 LLM 哪里理解不对。")
            return
        if not self._draft_text:
            return

        # 如果上次分析失败（_analysis_data 为 None），构造一个空分析传给 RefineWorker
        prev = self._analysis_data or {"covered_domains": [], "gaps": [], "_error": "上次分析失败"}

        self._set_busy(True, "LLM 正在根据反馈重新分析...")
        self._refine_btn.setEnabled(False)
        self._search_btn.setEnabled(False)

        self._worker = LitRefineWorker(self._client, self._draft_text, prev, feedback)
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
            QMessageBox.information(self, "提示", "当前分析未检测到遗漏方向，无需检索。")
            return

        queries = []
        for g in gaps:
            queries.extend(g.get("search_queries", []))
        if not queries:
            return

        self._search_keywords = queries
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
        # 检索完成后重新启用反馈和检索按钮，支持循环
        self._refine_btn.setEnabled(True)
        self._search_btn.setEnabled(True)
        self._export_btn.setVisible(True)
        self._results_list.clear()

        kw_str = ", ".join(self._search_keywords[:6])
        self._results_list.addItem(f"检索关键词: {kw_str}")

        if not papers:
            self._results_list.addItem("（PubMed 未返回结果，可修改反馈后重新分析再检索）")
            return

        self._results_list.addItem(f"共找到 {len(papers)} 篇文献:")
        for p in papers[:30]:
            text = f"{p.authors} ({p.year})  {p.journal}\n{p.title}"
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, p)
            self._results_list.addItem(item)

        self._results_list.addItem("")
        self._results_list.addItem("💡 如结果不理想，可修改反馈后重新分析，再次检索。")

    def _on_search_error(self, err: str):
        self._set_busy(False)
        self._refine_btn.setEnabled(True)
        self._search_btn.setEnabled(True)
        self._results_list.clear()
        self._results_list.addItem(f"检索失败: {err}")
        self._results_list.addItem("💡 可修改反馈后重新分析，再次检索。")

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
            QMessageBox.warning(self, "导出失败", f"导出 CSV 时出错：{e}")

    def _export_known_csv(self):
        """导出 LLM 推断的已知文献列表为 CSV。"""
        papers = getattr(self, '_known_papers', [])
        if not papers:
            QMessageBox.information(self, "提示", "当前分析中 LLM 没有列出已知文献。")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "导出 LLM 推断文献列表", "llm_inferred_papers.csv",
            "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as f:
                writer = csv.writer(f)
                writer.writerow(["title", "authors", "year", "doi", "relevance"])
                for p in papers:
                    writer.writerow([
                        p.get("title", ""),
                        p.get("authors", ""),
                        p.get("year", ""),
                        p.get("doi", ""),
                        p.get("relevance", ""),
                    ])
        except Exception as e:
            QMessageBox.warning(self, "导出失败", f"导出 CSV 时出错：{e}")

    # ---- 工具 ----

    def _set_busy(self, busy: bool, msg: str = ""):
        self._progress.setVisible(busy)
        self._progress.setRange(0, 0 if busy else 100)
        if msg:
            self._progress.setFormat(msg)

    @staticmethod
    def _parse_json(raw: str) -> dict | None:
        """解析 LLM 返回，多层容错 + 兜底降级。"""
        if not raw or not raw.strip():
            return None
        text = raw.strip()

        # 尝试 1: 直接解析
        try:
            return _json.loads(text)
        except (_json.JSONDecodeError, TypeError):
            pass

        # 尝试 2: ```json ... ```
        for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
            m = re.search(pattern, text, re.DOTALL)
            if m:
                try:
                    return _json.loads(m.group(1).strip())
                except (_json.JSONDecodeError, TypeError):
                    pass

        # 尝试 3: 提取 { ... }
        first = text.find('{')
        last = text.rfind('}')
        if first >= 0 and last > first:
            json_str = text[first:last + 1]
            try:
                return _json.loads(json_str)
            except (_json.JSONDecodeError, TypeError):
                pass
            # 尝试 3b: 清洗未转义换行符
            try:
                cleaned = re.sub(r'(?<!\\)"\s*\n\s*', r'\\n', json_str)
                cleaned = re.sub(r'(?<!\\)\n\s*"', r'\\n"', cleaned)
                cleaned = cleaned.replace('\uff5b', '{').replace('\uff5d', '}')
                return _json.loads(cleaned)
            except Exception:
                pass

        # 尝试 4: 中文花括号替换后重试
        alt = text.replace('\uff5b', '{').replace('\uff5d', '}')
        first_a = alt.find('{')
        last_a = alt.rfind('}')
        if first_a >= 0 and last_a > first_a and (first_a != first or last_a != last):
            try:
                return _json.loads(alt[first_a:last_a + 1])
            except (_json.JSONDecodeError, TypeError):
                pass

        # 兜底降级: 把 LLM 原始返回作为分析文本展示
        return {
            "covered_domains": [],
            "gaps": [{"domain": "LLM 原始回复（JSON 解析失败）", "reason": text[:500], "search_queries": []}],
        }
