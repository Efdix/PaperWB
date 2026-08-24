"""OpenAlex 检索与引文推荐 —— 免费学术文献库（约 2.5 亿作品，全学科覆盖）。

三个能力::

    OpenAlexSearcher          关键词检索 → PubMedPaper（与 PubMed/arXiv 检索器同接口）
    resolve_openalex_works    种子文献（DOI/标题）→ OpenAlex work id
    recommend_by_citations    种子 work id → 引文图谱推荐（related_works + 引用者聚合）

密钥完全可选：未配置时走免费额度（api.openalex.org）；配置后请求自动携带
`api_key`（设置 → API 接口设置 → 文献检索源）。
"""

from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from typing import Callable

from ..utils.config import get_openalex_api_key
from .pubmed_searcher import PubMedPaper, retry_urlopen
from .reference_match import normalize_doi

logger = logging.getLogger(__name__)

API_URL = "https://api.openalex.org/works"
USER_AGENT = "PaperWB/2.0"
REQUEST_DELAY = 0.12          # 请求间隔（秒），远低于 10 req/s 限制
SELECT_SEARCH = (
    "id,display_name,publication_year,doi,cited_by_count,authorships,"
    "primary_location,abstract_inverted_index,type,ids"
)
DETAILS_BATCH = 50            # 批量详情每批 work id 数


def _get(params: dict, timeout: float = 30.0) -> dict:
    """GET OpenAlex API → JSON dict（自动携带密钥，瞬时错误重试）。"""
    key = get_openalex_api_key()
    if key:
        params = dict(params)
        params["api_key"] = key
    url = f"{API_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    return json.loads(retry_urlopen(req, timeout=timeout).decode())


def _abstract_from_inverted(inv: dict | None) -> str:
    """把 OpenAlex 的倒排摘要索引还原为连续文本。"""
    if not inv:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inv.items():
        for i in idxs:
            positions.append((int(i), word))
    positions.sort()
    return " ".join(w for _, w in positions)


def _work_to_paper(w: dict) -> PubMedPaper | None:
    """OpenAlex work JSON → PubMedPaper（source="openalex"）。"""
    try:
        title = " ".join(str(w.get("display_name") or "").split())
        if not title:
            return None
        year = str(w.get("publication_year") or "")
        doi = str(w.get("doi") or "").replace("https://doi.org/", "").strip()

        authors: list[str] = []
        for a in (w.get("authorships") or [])[:5]:
            name = ((a or {}).get("raw_author_name") or "").strip()
            if name:
                authors.append(name)
        n_auth = len(w.get("authorships") or [])
        authors_str = ", ".join(authors) + (" et al." if n_auth > 5 else "")

        loc = (w.get("primary_location") or {})
        source = (loc.get("source") or {})
        journal = str(source.get("display_name") or "")
        url = str(loc.get("landing_page_url") or "") or str(w.get("id") or "")

        pmid = ""
        pmid_url = str((w.get("ids") or {}).get("pmid") or "")
        if pmid_url.rstrip("/").rsplit("/", 1)[-1].isdigit():
            pmid = pmid_url.rstrip("/").rsplit("/", 1)[-1]

        paper = PubMedPaper(
            pmid=pmid, title=title, authors=authors_str, year=year,
            journal=journal, doi=doi,
            abstract=_abstract_from_inverted(w.get("abstract_inverted_index")),
            url=url, source="openalex",
            cited_by=int(w.get("cited_by_count") or 0),
        )
        return paper
    except Exception:  # noqa: BLE001
        return None


class OpenAlexSearcher:
    """OpenAlex 关键词检索器（与 PubMed/Arxiv 检索器同接口）。"""

    def __init__(self, delay: float = REQUEST_DELAY) -> None:
        self._delay = delay
        self._last_request = 0.0

    def search(self, queries: list[str], limit: int = 10) -> list[PubMedPaper]:
        """对每个查询词检索 OpenAlex，去重后返回（年份降序）。"""
        items = [{"source": "openalex", "query": q} for q in queries]
        return self.search_plan(items, limit=limit)

    def search_plan(self, items: list[dict], limit: int = 10) -> list[PubMedPaper]:
        """按检索方案条目检索（支持 year_from/year_to/doc_type 原生过滤）。"""
        papers: list[PubMedPaper] = []
        seen_ids: set[str] = set()
        for qi, item in enumerate(items[:12]):
            q = str(item.get("query", "") or "").strip()
            if not q:
                continue
            if qi > 0:
                self._throttle()
            filters = [f"default.search:{q}"]
            yf, yt = item.get("year_from"), item.get("year_to")
            if isinstance(yf, int) and isinstance(yt, int) and yf <= yt:
                filters.append(f"publication_year:{yf}-{yt}")
            elif isinstance(yf, int):
                filters.append(f"publication_year:>{yf - 1}")
            elif isinstance(yt, int):
                filters.append(f"publication_year:<{yt + 1}")
            doc_type = str(item.get("doc_type", "") or "").strip().lower()
            if doc_type == "review":
                filters.append("type:review")
            elif doc_type == "original":
                filters.append("type:!review")
            params = {
                "filter": ",".join(filters),
                "per-page": str(max(1, min(limit, 100))),
                "sort": "relevance_score:desc",
                "select": SELECT_SEARCH,
            }
            try:
                data = self._get(params)
                for w in data.get("results", []):
                    p = _work_to_paper(w)
                    wid = str(w.get("id") or "")
                    if p is None or wid in seen_ids:
                        continue
                    seen_ids.add(wid)
                    papers.append(p)
            except Exception as e:  # noqa: BLE001
                logger.debug("OpenAlex 查询失败(%s): %s", q, e)
                continue
        papers.sort(key=lambda p: p.year, reverse=True)
        return papers

    def _get(self, params: dict) -> dict:
        self._last_request = time.monotonic()
        return _get(params)

    def _throttle(self) -> None:
        wait = self._delay - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)


# ============================================================
# 种子解析与引文推荐（按库推荐用）
# ============================================================

def resolve_openalex_works(
    seeds: list[dict], log_cb: Callable[[str], None] | None = None,
) -> list[str]:
    """把种子文献解析为 OpenAlex work id（DOI 优先，标题检索兜底）。

    Args:
        seeds: [{"doi", "title"}, ...]。
    Returns:
        与 seeds 对齐的 id 列表（'' 表示解析失败）。
    """
    out: list[str] = []
    for i, s in enumerate(seeds):
        wid = ""
        doi = normalize_doi(str(s.get("doi", "") or ""))
        title = str(s.get("title", "") or "").strip()
        try:
            if doi:
                data = _get({"filter": f"doi:{doi}", "per-page": "1", "select": "id"})
                results = data.get("results") or []
                if results:
                    wid = str(results[0].get("id") or "")
            if not wid and title:
                data = _get({"search": title[:300], "per-page": "1", "select": "id"})
                results = data.get("results") or []
                if results:
                    wid = str(results[0].get("id") or "")
        except Exception as e:  # noqa: BLE001
            logger.debug("种子解析失败(%s): %s", title[:40], e)
        out.append(wid)
        if log_cb:
            mark = "✓" if wid else "✗"
            log_cb(f"种子 {i + 1}/{len(seeds)} {mark} {title[:50]}")
        time.sleep(REQUEST_DELAY)
    return out


def recommend_by_citations(
    work_ids: list[str],
    limit: int = 30,
    per_seed: int = 20,
    min_year: int | None = None,
    exclude_ids: set[str] | None = None,
    log_cb: Callable[[str], None] | None = None,
    interrupt_cb: Callable[[], bool] | None = None,
) -> list[tuple[PubMedPaper, int]]:
    """引文图谱推荐：聚合各种子的 related_works 与引用者（cites）。

    候选按「被多少个种子关联」计数排序，批量取回详情。

    Returns:
        [(PubMedPaper, link_count)]，link_count = 关联种子数（≥1）。
    """
    valid_ids = [w for w in work_ids if w]
    if not valid_ids:
        return []
    exclude = exclude_ids or set()
    counts: dict[str, int] = {}

    def _add_candidate(wid: str, seed_pos: int) -> None:
        if not wid or wid in valid_ids or wid in exclude:
            return
        counts[wid] = counts.get(wid, 0) + 1

    year_filters = f",publication_year:>{min_year - 1}" if min_year else ""
    # 1) 各种子的 related_works + 引用者
    for pos, wid in enumerate(valid_ids):
        if interrupt_cb and interrupt_cb():
            return []
        try:
            data = _get({"filter": f"ids.openalex:{wid}",
                         "select": "id,related_works"})
            results = data.get("results") or []
            if results:
                for cand in (results[0].get("related_works") or [])[:per_seed]:
                    _add_candidate(str(cand), pos)
            time.sleep(REQUEST_DELAY)
            data = _get({
                "filter": f"cites:{wid}{year_filters}",
                "per-page": str(per_seed), "sort": "publication_date:desc",
                "select": "id",
            })
            for w in data.get("results", []):
                _add_candidate(str(w.get("id") or ""), pos)
            if log_cb:
                log_cb(f"引文扩展 {pos + 1}/{len(valid_ids)} · 候选累计 {len(counts)}")
        except Exception as e:  # noqa: BLE001
            logger.debug("引文扩展失败(%s): %s", wid, e)
        time.sleep(REQUEST_DELAY)

    if not counts:
        return []
    # 2) 按关联数排序取 top，批量取详情
    top = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    papers: list[tuple[PubMedPaper, int]] = []
    for i in range(0, len(top), DETAILS_BATCH):
        if interrupt_cb and interrupt_cb():
            break
        batch = top[i:i + DETAILS_BATCH]
        try:
            data = _get({
                "filter": "ids.openalex:" + "|".join(w for w, _ in batch),
                "per-page": str(DETAILS_BATCH), "select": SELECT_SEARCH,
            })
            by_id = {str(w.get("id") or ""): w for w in data.get("results", [])}
            for wid, cnt in batch:
                w = by_id.get(wid)
                if w is None:
                    continue
                p = _work_to_paper(w)
                if p is not None and p.title:
                    papers.append((p, cnt))
        except Exception as e:  # noqa: BLE001
            logger.debug("批量详情失败: %s", e)
        time.sleep(REQUEST_DELAY)
    return papers
