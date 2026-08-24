"""按库推荐 —— 以 Zotero 集合（含子集合）为种子的文献推荐。

两路互补::

    ① OpenAlex 引文推荐（主路，不耗 LLM）：种子 → related_works + 引用者
       聚合，按「被多少个种子关联」排序 —— 推荐学术谱系上相近/后续的文献
    ② LLM 画像检索（辅路，可选）：种子标题/年份 → LLM 归纳研究方向并生成
       英文检索式 → 走统一三源检索 —— 补充主题相近但引文谱系较远的文献

结果合并去重、过滤库内已有与种子自身；每条带 rec_source 标注
（"引文推荐"/"画像检索"），引文路另有 linked 字段（关联种子数）。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QThread, Signal

from .json_utils import parse_json_response
from .literature_search import (
    VALID_SOURCES, MultiSourceSearcher, PubMedPaper, _attach_plan_filters,
    merge_papers, paper_to_dict,
)
from .openalex import recommend_by_citations, resolve_openalex_works
from .reference_match import find_library_match, normalize_doi, normalize_title

MAX_SEEDS = 50              # 引文路种子上限（控制 OpenAlex 请求量）
PROFILE_MAX_TITLES = 40     # 画像提示词最多携带的标题数
PROFILE_QUERIES_MIN, PROFILE_QUERIES_MAX = 4, 6

PROFILE_PROMPT = """你是学术检索专家。以下是用户某个文献集合内的代表性文献（标题+年份）。
请归纳该集合的研究主题、核心研究对象与常用方法，然后生成英文检索式用于发现该方向的新文献。

【集合文献清单】
{listing}

## 输出（严格 JSON，不要解释）
{{"summary": "一句话概括该集合的研究方向", "queries": [{{"source": "openalex", "query": "英文检索式", "year_from": {default_year}}}]}}

要求：
- source 只能是 "openalex"/"pubmed"/"arxiv"，总数 {n_min}-{n_max} 条，优先 openalex
- 检索式覆盖主题/方法/对象不同侧面，用英文并做同义词扩展
- 不要照抄清单里的标题作检索式"""


def build_seeds(pool: list[dict], collection_key: str = "") -> list[dict]:
    """从比对池条目快照构建种子。

    pool 条目的 collections 含其所属集合（含祖先）的 key 集合，故按键匹配
    即天然包含子集合条目；collection_key 为空表示全库。有 DOI 的种子排在
    前面（OpenAlex 解析成功率高），上限 MAX_SEEDS。
    """
    entries = pool or []
    if collection_key:
        entries = [e for e in entries
                   if collection_key in (e.get("collections") or [])]
    seeds = [{"doi": (e.get("doi") or ""), "title": (e.get("title") or ""),
              "year": e.get("year") or "", "key": e.get("key") or ""}
             for e in entries]
    seeds = [s for s in seeds if s["doi"] or s["title"]]
    seeds.sort(key=lambda s: 0 if s["doi"] else 1)
    return seeds[:MAX_SEEDS]


def _profile_plan(client, seeds: list[dict], year_from: int | None,
                  log_cb: Callable[[str], None]) -> list[dict]:
    """LLM 画像：种子清单 → 归纳方向 → 检索式（失败返回空列表）。"""
    listing = "\n".join(f"- {s['title']} ({s['year']})" for s in seeds
                        if s.get("title"))
    default_year = year_from if year_from else 2015
    prompt = (PROFILE_PROMPT
              .replace("{listing}", listing)
              .replace("{default_year}", str(default_year))
              .replace("{n_min}", str(PROFILE_QUERIES_MIN))
              .replace("{n_max}", str(PROFILE_QUERIES_MAX)))
    try:
        resp = client.chat_sync(
            [{"role": "system", "content": "只返回 JSON，不要解释。"},
             {"role": "user", "content": prompt}],
            timeout=120.0, json_mode=True)
        data = parse_json_response(resp) or {}
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
            if year_from is not None and "year_from" not in entry:
                entry["year_from"] = year_from
            queries.append(entry)
        summary = str(data.get("summary") or "").strip()
        if summary:
            log_cb(f"集合画像：{summary[:60]}")
        return queries[:PROFILE_QUERIES_MAX]
    except Exception:
        return []


def recommend_from_library(
    seeds: list[dict],
    pool: list[dict] | None,
    client=None,
    year_from: int | None = None,
    limit: int = 20,
    searcher: MultiSourceSearcher | None = None,
    log_cb: Callable[[str], None] | None = None,
    interrupt_cb: Callable[[], bool] | None = None,
) -> tuple[list[dict], dict]:
    """两路推荐 → 合并去重 → 过滤库内已有与种子自身。

    Returns:
        (papers, stats)。papers 每条为 paper_to_dict + rec_source
        （"引文推荐"/"画像检索"，引文路在前）+ linked（关联种子数，引文路）；
        stats = {"seeds", "resolved", "citation_hits", "profile_hits"}。
    """
    if log_cb is None:
        log_cb = lambda _msg: None  # noqa: E731
    if interrupt_cb is None:
        interrupt_cb = lambda: False  # noqa: E731

    stats = {"seeds": len(seeds), "resolved": 0,
             "citation_hits": 0, "profile_hits": 0}
    lib_pool = pool or []
    seed_dois = {normalize_doi(s["doi"]) for s in seeds if s.get("doi")}
    seed_titles = {normalize_title(s["title"]) for s in seeds if s.get("title")}

    def _known(p: PubMedPaper) -> bool:
        if normalize_doi(p.doi) and normalize_doi(p.doi) in seed_dois:
            return True
        if normalize_title(p.title) and normalize_title(p.title) in seed_titles:
            return True
        return find_library_match(p.title, p.doi, lib_pool) is not None

    picked: list[tuple[PubMedPaper, str, int]] = []

    # ① OpenAlex 引文推荐（主路）
    try:
        log_cb(f"解析 {len(seeds)} 个种子 → OpenAlex …")
        ids = resolve_openalex_works(seeds, log_cb=log_cb)
        valid_ids = [w for w in ids if w]
        stats["resolved"] = len(valid_ids)
        if valid_ids and not interrupt_cb():
            log_cb(f"引文图谱扩展（{len(valid_ids)} 个种子命中）…")
            pairs = recommend_by_citations(
                valid_ids, limit=limit, min_year=year_from,
                exclude_ids=set(valid_ids), log_cb=log_cb,
                interrupt_cb=interrupt_cb)
            for p, cnt in pairs:
                if not _known(p):
                    picked.append((p, "引文推荐", cnt))
    except Exception as e:  # noqa: BLE001
        log_cb(f"引文推荐失败：{e}")

    # ② LLM 画像检索（辅路，可选）
    if client is not None and not interrupt_cb():
        queries = _profile_plan(client, seeds, year_from, log_cb)
        if queries:
            for q in queries:
                log_cb(f"画像检索 {q['source']}：{q['query']}")
            s = searcher if searcher is not None else MultiSourceSearcher()
            try:
                for p in s.search(queries, limit=limit):
                    if not _known(p):
                        picked.append((p, "画像检索", 0))
            except Exception as e:  # noqa: BLE001
                log_cb(f"画像检索失败：{e}")

    # 合并去重（引文路排前，画像路补后）
    merged = merge_papers([p for p, _, _ in picked])
    by_paper = {id(p): (src, cnt) for p, src, cnt in picked}
    out: list[dict] = []
    for p in merged:
        src, cnt = by_paper.get(id(p), ("画像检索", 0))
        d = paper_to_dict(p)
        d["rec_source"] = src
        if src == "引文推荐":
            d["linked"] = cnt
            stats["citation_hits"] += 1
        else:
            stats["profile_hits"] += 1
        out.append(d)
    log_cb(f"推荐完成 · 引文路 {stats['citation_hits']} 篇 · "
           f"画像路 {stats['profile_hits']} 篇")
    return out, stats


class LibraryRecommendWorker(QThread):
    """按库推荐后台任务：种子 → 引文推荐 + 画像检索 → 合并过滤。

    信号:
        log(str): 过程日志（种子解析 / 引文扩展 / 画像检索式）。
        results_ready(list): 推荐 dict 列表（含 rec_source/linked）。
        error(str): 失败信息。
        done(): 线程必然退出。
    """

    log = Signal(str)
    results_ready = Signal(list)
    error = Signal(str)
    done = Signal()

    def __init__(self, seeds: list[dict], pool: list[dict] | None, client=None,
                 year_from: int | None = None, limit: int = 20,
                 searcher: MultiSourceSearcher | None = None, parent=None):
        super().__init__(parent)
        self._seeds = seeds
        self._pool = pool or []
        self._client = client
        self._year_from = year_from
        self._limit = limit
        self._searcher = searcher

    def run(self) -> None:
        try:
            papers, _stats = recommend_from_library(
                self._seeds, self._pool, client=self._client,
                year_from=self._year_from, limit=self._limit,
                searcher=self._searcher,
                log_cb=self.log.emit, interrupt_cb=self.isInterruptionRequested)
            if self.isInterruptionRequested():
                return
            self.results_ready.emit(papers)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))
        finally:
            self.done.emit()
