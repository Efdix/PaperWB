"""文献匹配公共工具 —— DOI/标题规范化与库内查重。

写作面板的文献补充对话框与文献工作台的定时巡视共用本模块，
统一「同一篇文献」的判定口径：
1. 规范化 DOI 精确匹配（本地零成本）
2. 规范化标题精确匹配（去符号/空格/大小写）
3. （可选）LLM 批量模糊比对 —— 应对检索结果 DOI 缺失/标题改写
"""

from __future__ import annotations

import re

from .json_utils import parse_json_response

_DOI_PREFIX_RE = re.compile(
    r"^(https?://(dx\.)?doi\.org/|doi:\s*|info:doi/)", re.IGNORECASE)


def normalize_doi(doi: str) -> str:
    """规范化 DOI：去首尾空白、统一小写、剥掉 doi.org 前缀。"""
    if not doi:
        return ""
    d = doi.strip().lower()
    d = _DOI_PREFIX_RE.sub("", d)
    return d.rstrip("/")


def normalize_title(title: str) -> str:
    """规范化标题：仅保留小写字母数字，用于精确比对。"""
    return re.sub(r"[^a-z0-9]", "", (title or "").lower())


def find_library_match(title: str, doi: str, pool: list[dict]) -> dict | None:
    """在条目快照池中查找同一篇文献。

    Args:
        title: 检索结果标题。
        doi: 检索结果 DOI（可为空）。
        pool: 条目快照列表，每项至少含 {"title", "doi"}。

    Returns:
        命中的池内条目 dict；未命中返回 None。
    """
    n_doi = normalize_doi(doi)
    if n_doi:
        for it in pool:
            if normalize_doi(it.get("doi", "")) == n_doi:
                return it
    n_title = normalize_title(title)
    if n_title:
        for it in pool:
            if normalize_title(it.get("title", "")) == n_title:
                return it
    return None


def llm_match_titles(client, candidates: list[dict], pool: list[dict],
                     pool_limit: int = 400) -> dict[int, int]:
    """二级模糊比对：LLM 批量判断检索结果与库内文献是否同一篇。

    Args:
        client: LLMClient（解析接口即可，走 json_mode）。
        candidates: [{"title", "authors", "year"}, ...] 待比对候选。
        pool: 库内条目快照（含 title/authors/year），超过 pool_limit 截断。

    Returns:
        {候选索引: 库内索引}（均 0 基）；失败或无匹配返回 {}。
        任何异常都吞掉并返回空 dict —— 二级匹配只是锦上添花。
    """
    if client is None or not candidates or not pool:
        return {}
    pool_limited = pool[:pool_limit]

    def _fmt(idx: int, d: dict) -> str:
        return (f"{idx}: {d.get('title', '')} | "
                f"{d.get('authors', '')} | {d.get('year', '')}")

    lib_lines = "\n".join(_fmt(i, d) for i, d in enumerate(pool_limited))
    cand_lines = "\n".join(_fmt(i, d) for i, d in enumerate(candidates))
    prompt = (
        "判断下方【检索结果】中哪些与【库内文献】是同一篇论文"
        "（标题改写、作者与年份一致即可视为同一篇）。\n\n"
        f"【库内文献】\n{lib_lines}\n\n"
        f"【检索结果】\n{cand_lines}\n\n"
        '只返回 JSON：{"match": [{"candidate": 检索结果编号, "library": 库内编号}]}；'
        '没有匹配返回 {"match": []}。'
    )
    try:
        resp = client.chat_sync(
            [{"role": "system", "content": "只返回 JSON，不要解释。"},
             {"role": "user", "content": prompt}],
            timeout=120.0, json_mode=True)
        data = parse_json_response(resp) or {}
        out: dict[int, int] = {}
        for m in data.get("match") or []:
            if not isinstance(m, dict):
                continue
            try:
                ci, li = int(m.get("candidate")), int(m.get("library"))
            except (TypeError, ValueError):
                continue
            if 0 <= ci < len(candidates) and 0 <= li < len(pool_limited):
                out[ci] = li
        return out
    except Exception:
        return {}
