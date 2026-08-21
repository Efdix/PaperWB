"""统一文献检索核心 —— 多源检索 + LLM 检索式生成 + 统一去重合并。

设计目标：所有「检索外部文献」的能力（定时巡视、草稿推断推荐、主动询问）
都通过本模块执行，替代此前各调用点各自硬编码 PubMedSearcher 的分散实现。

统一数据模型：pubmed_searcher.PubMedPaper（含 source 字段标注来源）。
"""

from __future__ import annotations

import re
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from .json_utils import parse_json_response
from .pubmed_searcher import PubMedPaper, PubMedSearcher
from .reference_match import find_library_match, normalize_doi, normalize_title

if TYPE_CHECKING:
    from .llm_client import LLMClient

USER_AGENT = "PaperWB/2.0"
MAX_PLAN_QUERIES = 12        # 检索方案总条数上限
MAX_PER_SOURCE = 4           # LLM 生成时每源条数上限
ARXIV_DELAY = 0.4            # arXiv 请求间隔（秒）
VALID_SOURCES = ("pubmed", "arxiv")


def paper_to_dict(p: PubMedPaper) -> dict:
    """统一文献 dict 格式（feed / 导出 / 卡片共用）。"""
    return {
        "pmid": p.pmid, "title": p.title, "authors": p.authors,
        "year": p.year, "journal": p.journal, "doi": p.doi,
        "abstract": p.abstract, "url": p.url, "source": p.source,
        "arxiv_id": p.arxiv_id,
    }


# ============================================================
# arXiv 检索器
# ============================================================

class ArxivSearcher:
    """arXiv API 检索器（export.arxiv.org/api/query，Atom XML，免费无 key）。"""

    API_URL = "https://export.arxiv.org/api/query"

    def __init__(self, delay: float = ARXIV_DELAY) -> None:
        self._delay = delay

    def search(self, queries: list[str], limit: int = 10) -> list[PubMedPaper]:
        """对每个查询词检索 arXiv，按 arxiv_id 去重后返回（年份降序）。"""
        papers: list[PubMedPaper] = []
        seen_ids: set[str] = set()
        for qi, query in enumerate(queries[:12]):
            if qi > 0:
                time.sleep(self._delay)
            try:
                for p in self._fetch(query, limit):
                    if p.arxiv_id and p.arxiv_id in seen_ids:
                        continue
                    if p.arxiv_id:
                        seen_ids.add(p.arxiv_id)
                    papers.append(p)
            except Exception:
                continue  # 单查询失败静默跳过，与 PubMed 检索器一致
        papers.sort(key=lambda p: p.year, reverse=True)
        return papers

    def _fetch(self, query: str, limit: int) -> list[PubMedPaper]:
        """单次 arXiv API 请求并解析 Atom feed。"""
        terms = query.strip().split()
        search_query = " AND ".join(terms) if terms else query.strip()
        params = urllib.parse.urlencode({
            "search_query": f"all:{search_query}",
            "start": 0,
            "max_results": limit,
        })
        url = f"{self.API_URL}?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(req, timeout=30) as resp:
            root = ET.fromstring(resp.read())
        ns = "{http://www.w3.org/2005/Atom}"
        return [p for p in
                (self._parse_entry(e) for e in root.findall(f"{ns}entry"))
                if p is not None]

    @staticmethod
    def _parse_entry(entry: ET.Element) -> PubMedPaper | None:
        """解析单条 Atom entry → PubMedPaper（source="arxiv"）。"""
        try:
            ns = "{http://www.w3.org/2005/Atom}"
            arxiv_ns = "{http://arxiv.org/schemas/atom}"

            def _text(tag: str) -> str:
                el = entry.find(f"{ns}{tag}")
                return (el.text or "").strip() if el is not None else ""

            def _a_text(tag: str) -> str:
                el = entry.find(f"{arxiv_ns}{tag}")
                return (el.text or "").strip() if el is not None else ""

            title = " ".join(_text("title").split())
            summary = " ".join(_text("summary").split())
            published = _text("published")      # 2024-01-15T08:00:00Z
            year = published[:4] if len(published) >= 4 else ""

            url = ""
            for link in entry.findall(f"{ns}link"):
                if link.get("rel") == "alternate":
                    url = link.get("href", "") or ""
                    break
            arxiv_id = ""
            if url:
                arxiv_id = url.rsplit("/", 1)[-1]
                m = re.match(r"^(.+?)v\d+$", arxiv_id)
                if m:
                    arxiv_id = m.group(1)
            author_els = entry.findall(f"{ns}author/{ns}name")
            authors_list = [a.text.strip() for a in author_els if a.text and a.text.strip()]
            authors_str = ", ".join(authors_list[:5])
            if len(authors_list) > 5:
                authors_str += " et al."

            return PubMedPaper(
                pmid="", title=title, authors=authors_str, year=year,
                journal=_a_text("journal_ref") or "", doi=_a_text("doi"),
                abstract=summary, url=url, source="arxiv", arxiv_id=arxiv_id,
            )
        except Exception:
            return None


# ============================================================
# 多源统一检索器
# ============================================================

def merge_papers(papers: list[PubMedPaper]) -> list[PubMedPaper]:
    """跨源合并去重：DOI 归一优先，其次标题归一；PubMed 条目排前故优先保留。"""
    merged: list[PubMedPaper] = []
    seen_doi: set[str] = set()
    seen_title: set[str] = set()
    for p in papers:
        doi = normalize_doi(p.doi)
        title = normalize_title(p.title)
        if doi and doi in seen_doi:
            continue
        if title and title in seen_title:
            continue
        if doi:
            seen_doi.add(doi)
        if title:
            seen_title.add(title)
        merged.append(p)
    return merged


class MultiSourceSearcher:
    """统一检索入口：按检索方案路由到 PubMed / arXiv，跨源合并去重。

    使用方式:
        searcher = MultiSourceSearcher()
        papers = searcher.search([
            {"source": "pubmed", "query": "avian feather melanocyte"},
            {"source": "arxiv", "query": "single-cell transcriptomics plumage"},
        ], limit=10)
    """

    def __init__(self, pubmed=None, arxiv=None) -> None:
        self._pubmed = pubmed  # 测试可注入假检索器（含 search(queries, limit) 即可）
        self._arxiv = arxiv

    def search(self, plan: list[dict], limit: int = 10) -> list[PubMedPaper]:
        """执行检索方案并跨源合并去重，按年份降序。"""
        if not plan:
            return []
        pubmed_queries: list[str] = []
        arxiv_queries: list[str] = []
        for item in plan[:MAX_PLAN_QUERIES]:
            if not isinstance(item, dict):
                continue
            q = str(item.get("query", "") or "").strip()
            src = str(item.get("source", "") or "").strip().lower()
            if not q:
                continue
            if src not in VALID_SOURCES:
                src = "pubmed"
            (arxiv_queries if src == "arxiv" else pubmed_queries).append(q)

        papers: list[PubMedPaper] = []
        if pubmed_queries:
            searcher = self._pubmed if self._pubmed is not None else PubMedSearcher()
            try:
                papers.extend(searcher.search(pubmed_queries, limit=limit))
            except Exception:
                pass
        if arxiv_queries:
            searcher = self._arxiv if self._arxiv is not None else ArxivSearcher()
            try:
                papers.extend(searcher.search(arxiv_queries, limit=limit))
            except Exception:
                pass

        merged = merge_papers(papers)
        merged.sort(key=lambda p: p.year, reverse=True)
        return merged


# ============================================================
# LLM 检索式生成
# ============================================================

SEARCH_PLAN_PROMPT = """你是学术文献检索专家。请把用户的文献检索需求分解为可直接执行的检索式。

【用户需求】
{request}

## 输出（严格 JSON，不要加解释）
{{"queries": [{{"source": "pubmed", "query": "英文检索式"}}, {{"source": "arxiv", "query": "英文检索式"}}]}}

要求：
- source 只能是 "pubmed" 或 "arxiv"，每个源最多 {max_per_source} 条
- 检索式用英文；PubMed 可用引号短语与布尔逻辑（AND/OR）
- 覆盖用户需求的不同侧面（主题/机制/方法），宁缺毋滥
- 检索式具体而非泛泛（如 "avian feather melanocyte scRNA-seq" 而非 "bird color"）"""


def generate_search_plan(client, request_text: str,
                         max_per_source: int = MAX_PER_SOURCE) -> list[dict] | None:
    """LLM 生成多源检索方案；无 client / 失败返回 None（调用方降级为原文检索）。"""
    if client is None or not request_text.strip():
        return None
    prompt = (SEARCH_PLAN_PROMPT
              .replace("{request}", request_text.strip())
              .replace("{max_per_source}", str(max_per_source)))
    try:
        resp = client.chat_sync(
            [{"role": "system", "content": "只返回 JSON，不要解释。"},
             {"role": "user", "content": prompt}],
            timeout=120.0, json_mode=True)
        data = parse_json_response(resp) or {}
        plan: list[dict] = []
        for item in data.get("queries") or []:
            if not isinstance(item, dict):
                continue
            q = str(item.get("query", "") or "").strip()
            src = str(item.get("source", "") or "").strip().lower()
            if not q:
                continue
            if src not in VALID_SOURCES:
                src = "pubmed"
            plan.append({"source": src, "query": q})
        return plan if plan else None
    except Exception:
        return None


# ============================================================
# 统一后台检索任务
# ============================================================

def run_paper_search(request_text: str, client=None, pool: list[dict] | None = None,
                     limit: int = 10, searcher: MultiSourceSearcher | None = None,
                     log_cb=None, interrupt_cb=None) -> list[dict]:
    """统一检索核心逻辑（纯函数，供 QThread 与同步调用共用）。

    Args:
        request_text: 检索需求文本（自然语言或关键词）。
        client: LLMClient | None（检索式生成；None/失败降级为原文检索）。
        pool: 库内条目快照（find_library_match 过滤用）。
        limit: 每源每查询上限。
        searcher: 测试可注入假多源检索器。
        log_cb: 过程日志回调（str → None）。
        interrupt_cb: 中断检查回调（() -> bool）。

    Returns:
        去重、库内过滤后的文献 dict 列表（paper_to_dict 格式）。
    """
    if log_cb is None:
        log_cb = lambda _msg: None
    if interrupt_cb is None:
        interrupt_cb = lambda: False

    plan = generate_search_plan(client, request_text)
    if plan:
        for item in plan:
            log_cb(f"检索 {item['source']}：{item['query']}")
    else:
        query = request_text.strip()
        plan = [{"source": "pubmed", "query": query},
                {"source": "arxiv", "query": query}]
        log_cb("（未配置 LLM 或生成失败，直接用原文检索）")

    s = searcher if searcher is not None else MultiSourceSearcher()
    papers = s.search(plan, limit=limit)
    if interrupt_cb():
        return []

    new_papers = [p for p in papers
                  if not find_library_match(p.title, p.doi, pool or [])]
    log_cb(
        f"共检索 {len(papers)} 篇 · 去重后 {len(new_papers)} 篇 · "
        f"{len(papers) - len(new_papers)} 篇已在库内")
    return [paper_to_dict(p) for p in new_papers]


class PaperSearchWorker(QThread):
    """统一后台检索：LLM 生成检索式 → 多源检索 → 库内过滤。

    信号:
        log(str): 检索过程日志（检索式 / 来源 / 命中数）。
        results_ready(list): 去重过滤后的文献 dict 列表（paper_to_dict 格式）。
        error(str): 检索失败信息。
        done(): 线程必然退出。
    """

    log = Signal(str)
    results_ready = Signal(list)
    error = Signal(str)
    done = Signal()

    def __init__(self, request_text: str, client=None, pool: list[dict] | None = None,
                 limit: int = 10, searcher: MultiSourceSearcher | None = None,
                 parent=None):
        super().__init__(parent)
        self._request = request_text
        self._client = client       # LLMClient | None（检索式生成）
        self._pool = pool or []     # 库内条目快照
        self._limit = limit
        self._searcher = searcher   # 测试可注入假多源检索器

    def run(self) -> None:
        try:
            papers = run_paper_search(
                self._request, client=self._client, pool=self._pool,
                limit=self._limit, searcher=self._searcher,
                log_cb=self.log.emit, interrupt_cb=self.isInterruptionRequested,
            )
            if self.isInterruptionRequested():
                return
            self.results_ready.emit(papers)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))
        finally:
            self.done.emit()
