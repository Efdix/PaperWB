"""统一文献检索核心 —— 多源检索 + LLM 检索式生成 + 统一去重合并。

设计目标：所有「检索外部文献」的能力（定时巡视、草稿推断推荐、主动询问）
都通过本模块执行，替代此前各调用点各自硬编码 PubMedSearcher 的分散实现。

统一数据模型：pubmed_searcher.PubMedPaper（含 source 字段标注来源）。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import TYPE_CHECKING

from PySide6.QtCore import QThread, Signal

from .json_utils import parse_json_response
from .openalex import OpenAlexSearcher
from .pubmed_searcher import PubMedPaper, PubMedSearcher, retry_urlopen
from .reference_match import find_library_match, normalize_doi, normalize_title

if TYPE_CHECKING:
    from .llm_client import LLMClient

USER_AGENT = "PaperWB/2.0"
MAX_PLAN_QUERIES = 12        # 检索方案总条数上限
MAX_PER_SOURCE = 4           # LLM 生成时每源条数上限
ARXIV_DELAY = 0.4            # arXiv 请求间隔（秒）
VALID_SOURCES = ("pubmed", "arxiv", "openalex")
REFINE_FEEDBACK_TITLES = 40  # 两轮闭环回喂的第 1 轮标题上限


def paper_to_dict(p: PubMedPaper) -> dict:
    """统一文献 dict 格式（feed / 导出 / 卡片共用）。"""
    return {
        "pmid": p.pmid, "title": p.title, "authors": p.authors,
        "year": p.year, "journal": p.journal, "doi": p.doi,
        "abstract": p.abstract, "url": p.url, "source": p.source,
        "arxiv_id": p.arxiv_id, "cited_by": p.cited_by,
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
        root = ET.fromstring(retry_urlopen(req, timeout=30))
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
# OpenAlex 检索源（免费学术文献库，密钥可选）
# ============================================================

OPENALEX_API_URL = "https://api.openalex.org/works"


def test_openalex_connection(api_key: str = "") -> tuple[bool, str]:
    """测试 OpenAlex 可达性与密钥有效性，返回 (ok, message)。

    密钥可空：无 key 模式同样可用（每日约 100 次搜索的免费额度），
    用于设置对话框「测试」按钮。
    """
    params = {"search": "malaria", "per-page": "1"}
    key = (api_key or "").strip()
    if key:
        params["api_key"] = key
    url = f"{OPENALEX_API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode())
            remaining = (resp.headers.get("x-ratelimit-remaining")
                         or resp.headers.get("x-ratelimit-remaining-day") or "")
            limit = (resp.headers.get("x-ratelimit-limit")
                     or resp.headers.get("x-ratelimit-limit-day") or "")
        count = data.get("meta", {}).get("count", "?")
        msg = f"连接成功 · 检索正常（示例命中 {count} 条）"
        if remaining:
            msg += f" · 今日剩余额度 {remaining}" + (f"/{limit}" if limit else "")
        msg += "（未配置密钥，走免费额度）" if not key else "（密钥已生效）"
        return True, msg
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, f"密钥无效或无权限（HTTP {e.code}），请核对后重试"
        if e.code == 429:
            return False, "请求过于频繁或今日免费额度已用完（HTTP 429），明日自动重置"
        return False, f"OpenAlex 返回错误 HTTP {e.code}: {e.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        return False, f"网络错误：{e}"


# ============================================================
# 多源统一检索器
# ============================================================

def _pubmed_query_with_filters(item: dict, query: str) -> str:
    """把年份/文献类型过滤翻译为 PubMed 检索式后缀（服务端生效）。"""
    parts = [f"({query})"]
    yf, yt = item.get("year_from"), item.get("year_to")
    if isinstance(yf, int) and isinstance(yt, int) and yf <= yt:
        parts.append(f"{yf}:{yt}[dp]")
    elif isinstance(yf, int):
        parts.append(f"{yf}:3000[dp]")
    elif isinstance(yt, int):
        parts.append(f"1900:{yt}[dp]")
    doc_type = str(item.get("doc_type", "") or "").strip().lower()
    if doc_type == "review":
        parts.append("review[pt]")
    elif doc_type == "original":
        parts.append("NOT review[pt]")
    return " AND ".join(parts)


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
    """统一检索入口：按检索方案路由到 PubMed / arXiv / OpenAlex，跨源合并去重。

    使用方式:
        searcher = MultiSourceSearcher()
        papers = searcher.search([
            {"source": "openalex", "query": "avian feather melanocyte",
             "year_from": 2022, "year_to": 2026},
            {"source": "pubmed", "query": "single-cell transcriptomics plumage",
             "doc_type": "review"},
        ], limit=10)

    过滤落地方式：PubMed 把年份/类型翻译为检索式后缀（服务端）；
    OpenAlex 走原生 filter（search_plan）；arXiv 无类型概念，由调用方
    在客户端做年份过滤。
    """

    def __init__(self, pubmed=None, arxiv=None, openalex=None) -> None:
        self._pubmed = pubmed  # 测试可注入假检索器（含 search(queries, limit) 即可）
        self._arxiv = arxiv
        self._openalex = openalex

    def search(self, plan: list[dict], limit: int = 10) -> list[PubMedPaper]:
        """执行检索方案并跨源合并去重，按年份降序。"""
        if not plan:
            return []
        pubmed_queries: list[str] = []
        arxiv_queries: list[str] = []
        openalex_items: list[dict] = []
        for item in plan[:MAX_PLAN_QUERIES]:
            if not isinstance(item, dict):
                continue
            q = str(item.get("query", "") or "").strip()
            src = str(item.get("source", "") or "").strip().lower()
            if not q:
                continue
            if src not in VALID_SOURCES:
                src = "pubmed"
            if src == "openalex":
                openalex_items.append(item)
            elif src == "arxiv":
                arxiv_queries.append(q)
            else:
                pubmed_queries.append(_pubmed_query_with_filters(item, q))

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
        if openalex_items:
            searcher = (self._openalex if self._openalex is not None
                        else OpenAlexSearcher())
            try:
                if hasattr(searcher, "search_plan"):
                    papers.extend(searcher.search_plan(openalex_items, limit=limit))
                else:  # 测试假检索器只实现 search(queries, limit)
                    papers.extend(searcher.search(
                        [str(i.get("query", "")) for i in openalex_items],
                        limit=limit))
            except Exception:
                pass

        merged = merge_papers(papers)
        merged.sort(key=lambda p: p.year, reverse=True)
        return merged


# ============================================================
# LLM 检索式生成
# ============================================================

SEARCH_PLAN_PROMPT = """你是学术文献检索专家。请把用户的文献检索需求分解为可直接执行的检索方案。

【用户需求】
{request}

## 可用检索源
- openalex：全学科约 2.5 亿篇（覆盖 PubMed 与 arXiv 大部分内容），通用主力
- pubmed：生物医学最新索引，可用 MeSH 受控词与布尔逻辑（AND/OR）
- arxiv：物理/数学/计算机/生物预印本

## 输出（严格 JSON，不要加解释）
{{"queries": [{{"source": "openalex", "query": "英文检索式", "year_from": 2022, "year_to": 2026, "doc_type": "review"}}]}}

要求：
- source 只能是 "openalex"/"pubmed"/"arxiv"，每个源最多 {max_per_source} 条；优先用 openalex
- 检索式用英文，做同义词与缩写扩展（如 single-cell sequencing / scRNA-seq）
- 覆盖需求的不同侧面（主题/机制/方法/综述），宁缺毋滥
- 检索式具体而非泛泛（如 "avian feather melanocyte scRNA-seq" 而非 "bird color"）
- 能从需求推断年份范围时必填 year_from/year_to（整数）
- 需要综述时 doc_type 填 "review"，只要研究论文填 "original"，否则省略该字段"""


def _safe_year(v) -> int | None:
    """年份字段容错解析（1900-2100 之外的值视为缺失）。"""
    try:
        y = int(v)
    except (TypeError, ValueError):
        return None
    return y if 1900 <= y <= 2100 else None


def _attach_plan_filters(src_item: dict, entry: dict) -> None:
    """把方案条目里的可选过滤字段（年份/类型）安全拷入 entry。"""
    yf = _safe_year(src_item.get("year_from"))
    yt = _safe_year(src_item.get("year_to"))
    if yf is not None:
        entry["year_from"] = yf
    if yt is not None:
        entry["year_to"] = yt
    dt = str(src_item.get("doc_type", "") or "").strip().lower()
    if dt in ("review", "original"):
        entry["doc_type"] = dt


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
                src = "openalex"
            entry = {"source": src, "query": q}
            _attach_plan_filters(item, entry)
            plan.append(entry)
        return plan if plan else None
    except Exception:
        return None


# ============================================================
# 结果反思（两轮闭环）
# ============================================================

REFINE_PLAN_PROMPT = """你是学术文献检索专家。用户提出了检索需求，第 1 轮检索已返回部分结果。
请分析结果质量并决定是否需要补充检索。

【用户需求】
{request}

【第 1 轮命中文献】（序号. [来源/年份] 标题）
{listing}

## 分析任务
1. 找出与需求不切题的命中（术语偏差/范围过宽/文献类型不符）
2. 找出覆盖缺口：缺综述？缺方法学？缺某个侧面？
3. 若结果已足够覆盖需求则停止；否则给出与第 1 轮不重复的补充检索式

## 输出（严格 JSON，不要解释）
{{"enough": false, "off_topic": [1, 3], "queries": [{{"source": "openalex", "query": "英文检索式", "year_from": 2022, "year_to": 2026, "doc_type": "review"}}]}}

要求：
- enough 为 true 时 queries 必须是空数组
- off_topic 只填确认不切题的序号，宁缺毋滥（1 基，对应上方列表）
- 补充检索式 source 只能是 "openalex"/"pubmed"/"arxiv"，每个源最多 2 条"""


def reflect_on_results(client, request_text: str,
                       papers: list[PubMedPaper]) -> dict | None:
    """两轮闭环：把第 1 轮命中回喂 LLM 做缺口分析与不切题标记。

    Returns:
        {"enough": bool, "off_topic": set[int], "queries": list[dict]}；
        无 client / 失败返回 None（调用方按单轮收尾）。
    """
    if client is None or not papers:
        return None
    listing = "\n".join(
        f"{i}. [{p.source}/{p.year}] {p.title[:100]}"
        for i, p in enumerate(papers[:REFINE_FEEDBACK_TITLES], 1))
    prompt = (REFINE_PLAN_PROMPT
              .replace("{request}", request_text.strip())
              .replace("{listing}", listing))
    try:
        resp = client.chat_sync(
            [{"role": "system", "content": "只返回 JSON，不要解释。"},
             {"role": "user", "content": prompt}],
            timeout=120.0, json_mode=True)
        data = parse_json_response(resp) or {}
        n_listed = min(len(papers), REFINE_FEEDBACK_TITLES)
        off: set[int] = set()
        for i in (data.get("off_topic") or []):
            try:
                idx = int(i)
            except (TypeError, ValueError):
                continue
            if 1 <= idx <= n_listed:
                off.add(idx)
        queries: list[dict] = []
        for item in data.get("queries") or []:
            if not isinstance(item, dict):
                continue
            q = str(item.get("query", "") or "").strip()
            src = str(item.get("source", "") or "").strip().lower()
            if not q:
                continue
            entry = {"source": src if src in VALID_SOURCES else "openalex",
                     "query": q}
            _attach_plan_filters(item, entry)
            queries.append(entry)
        return {"enough": bool(data.get("enough")), "off_topic": off,
                "queries": queries[:6]}
    except Exception:
        return None


# ============================================================
# 过滤与排序
# ============================================================

def _filter_note(item: dict) -> str:
    """日志用：方案条目的过滤条件摘要。"""
    bits: list[str] = []
    yf, yt = item.get("year_from"), item.get("year_to")
    if isinstance(yf, int) or isinstance(yt, int):
        bits.append(f"{yf if isinstance(yf, int) else '…'}–"
                    f"{yt if isinstance(yt, int) else '…'}")
    if item.get("doc_type") == "review":
        bits.append("综述")
    elif item.get("doc_type") == "original":
        bits.append("研究论文")
    return f"（{'·'.join(bits)}）" if bits else ""


def _plan_year_bounds(plans: list[list[dict]]) -> tuple[int | None, int | None]:
    """汇总各轮方案的全部年份边界 → 全局包络（宽界，避免误杀）。"""
    yfs = [it["year_from"] for pl in plans for it in pl
           if isinstance(it.get("year_from"), int)]
    yts = [it["year_to"] for pl in plans for it in pl
           if isinstance(it.get("year_to"), int)]
    return (min(yfs) if yfs else None, max(yts) if yts else None)


def _year_allowed(paper: PubMedPaper, ylo: int | None, yhi: int | None) -> bool:
    if ylo is None and yhi is None:
        return True
    y = str(paper.year or "")
    if not y.isdigit():  # 年份缺失不误杀
        return True
    yi = int(y)
    return (ylo is None or yi >= ylo) and (yhi is None or yi <= yhi)


def rank_papers(papers: list[PubMedPaper]) -> list[PubMedPaper]:
    """加权排序：年份新为主（0.7），被引高为辅（0.3，OpenAlex 提供）。

    年份在观测范围内归一（缺失按 0.5）；被引按 log(1+n) 归一。
    """
    import math
    years = [int(p.year) for p in papers if str(p.year or "").isdigit()]
    if not years:
        return papers
    ylo, yhi = min(years), max(years)
    span = max(yhi - ylo, 1)
    max_cited = max((p.cited_by for p in papers), default=0)
    cden = math.log1p(max_cited) if max_cited > 0 else 0.0

    def _score(p: PubMedPaper) -> float:
        ys = ((int(p.year) - ylo) / span
              if str(p.year or "").isdigit() else 0.5)
        cs = (math.log1p(max(p.cited_by, 0)) / cden) if cden else 0.0
        return 0.7 * ys + 0.3 * cs

    return sorted(papers, key=_score, reverse=True)


# ============================================================
# 统一后台检索任务
# ============================================================

def run_paper_search(request_text: str, client=None, pool: list[dict] | None = None,
                     limit: int = 10, searcher: MultiSourceSearcher | None = None,
                     log_cb=None, interrupt_cb=None, rounds: int = 2,
                     filter_library: bool = True) -> list[dict]:
    """统一检索核心逻辑（纯函数，供 QThread 与同步调用共用）。

    Args:
        request_text: 检索需求文本（自然语言或关键词）。
        client: LLMClient | None（检索式生成与两轮反思；None/失败降级为原文检索）。
        pool: 库内条目快照（find_library_match 过滤/标注用）。
        limit: 每源每查询上限。
        searcher: 测试可注入假多源检索器。
        log_cb: 过程日志回调（str → None）。
        interrupt_cb: 中断检查回调（() → bool）。
        rounds: 2 = 两轮闭环（第 1 轮 → LLM 缺口反思 → 补充第 2 轮），
            1 = 单轮（定时巡视用，控制请求量）。
        filter_library: True = 剔除库中已有文献（原行为）；
            False = 保留全部结果，每条附加 "in_library" 标记（True/False）。

    Returns:
        去重、过滤、排序后的文献 dict 列表（paper_to_dict 格式）。
    """
    if log_cb is None:
        log_cb = lambda _msg: None  # noqa: E731
    if interrupt_cb is None:
        interrupt_cb = lambda: False  # noqa: E731

    plans: list[list[dict]] = []
    plan = generate_search_plan(client, request_text)
    two_rounds = rounds >= 2 and client is not None
    if plan:
        for item in plan:
            log_cb(f"检索 {item['source']}：{item['query']}{_filter_note(item)}")
    else:
        query = request_text.strip()
        plan = [{"source": "pubmed", "query": query},
                {"source": "arxiv", "query": query}]
        log_cb("（未配置 LLM 或生成失败，直接用原文检索）")
        two_rounds = False
    plans.append(plan)

    s = searcher if searcher is not None else MultiSourceSearcher()
    round1_limit = max(5, limit // 2) if two_rounds else limit
    papers = s.search(plan, limit=round1_limit)
    if interrupt_cb():
        return []

    if two_rounds and papers:
        log_cb(f"第 1 轮命中 {len(papers)} 篇 · 缺口分析中…")
        feedback = reflect_on_results(client, request_text, papers)
        if feedback is not None:
            if feedback["off_topic"]:
                kept = [p for i, p in enumerate(papers, 1)
                        if i not in feedback["off_topic"]]
                log_cb(f"剔除不切题命中 {len(papers) - len(kept)} 篇")
                papers = kept
            if feedback["enough"] or not feedback["queries"]:
                log_cb("结果已足够覆盖需求，跳过第 2 轮")
            else:
                plan2 = feedback["queries"]
                plans.append(plan2)
                for item in plan2:
                    log_cb(f"第 2 轮补充 {item['source']}："
                           f"{item['query']}{_filter_note(item)}")
                papers.extend(s.search(plan2, limit=limit))
                if interrupt_cb():
                    return []

    merged = merge_papers(papers)
    # 客户端年份过滤：PubMed/OpenAlex 已在服务端按各自条目过滤，
    # 这里只兜底 arXiv（无服务端语法），用全局包络宽界过滤
    ylo, yhi = _plan_year_bounds(plans)
    if ylo is not None or yhi is not None:
        before = len(merged)
        merged = [p for p in merged
                  if p.source != "arxiv" or _year_allowed(p, ylo, yhi)]
        if before != len(merged):
            log_cb(f"年份过滤剔除 {before - len(merged)} 篇"
                   f"（{ylo or '…'}–{yhi or '…'}）")
    merged = rank_papers(merged)

    matches = [find_library_match(p.title, p.doi, pool or []) for p in merged]
    if filter_library:
        new_papers = [p for p, m in zip(merged, matches) if not m]
        log_cb(
            f"共检索 {len(merged)} 篇 · 去重后 {len(new_papers)} 篇 · "
            f"{len(merged) - len(new_papers)} 篇已在库内")
    else:
        new_papers = list(merged)
        n_in = sum(1 for m in matches if m)
        if n_in:
            log_cb(f"共检索 {len(merged)} 篇 · 其中 {n_in} 篇已在库内（已标注，未剔除）")
    out = [paper_to_dict(p) for p in new_papers]
    if not filter_library:
        for d, m in zip(out, matches):
            d["in_library"] = bool(m)
    return out


class PaperSearchWorker(QThread):
    """统一后台检索：LLM 生成检索式 → 多源检索 → 两轮反思补充 → 库内过滤。

    信号:
        log(str): 检索过程日志（检索式 / 轮次 / 命中数）。
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
                 rounds: int = 2, filter_library: bool = True, parent=None):
        super().__init__(parent)
        self._request = request_text
        self._client = client       # LLMClient | None（检索式生成 + 两轮反思）
        self._pool = pool or []     # 库内条目快照
        self._limit = limit
        self._searcher = searcher   # 测试可注入假多源检索器
        self._rounds = rounds
        self._filter_library = filter_library

    def run(self) -> None:
        try:
            papers = run_paper_search(
                self._request, client=self._client, pool=self._pool,
                limit=self._limit, searcher=self._searcher, rounds=self._rounds,
                log_cb=self.log.emit, interrupt_cb=self.isInterruptionRequested,
                filter_library=self._filter_library,
            )
            if self.isInterruptionRequested():
                return
            self.results_ready.emit(papers)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))
        finally:
            self.done.emit()
