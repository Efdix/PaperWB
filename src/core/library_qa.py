"""文献库 RAG 问答引擎 —— 跨文献综合问答的索引与检索层。

数据流::

    ZoteroLibrary.get_all_items()
      ├─ 元数据（标题/作者/年份/期刊/摘要/DOI）→ 内存 BM25 轻索引（毫秒级重建）
      └─ PDF 附件 → PyMuPDF 按页抽段 → {data_root}/.paperwb/lib_index/fulltext.json
                    （键 = Zotero 条目 key，失效判据 = PDF mtime，支持增量刷新）

检索策略：元数据索引 + 全文索引混合 —— 元数据命中的条目加权排在前面
（用户点名某篇时优先定位），全文命中的条目携带最佳段落进入上下文。
「只问库」模式跳过全文索引，只做元数据级问答。

只读铁律：索引构建仅以只读方式打开 Zotero 的 PDF 原文件，绝不写 Zotero 目录。
线程模型：set_items / refresh_fulltext 在后台索引线程调用，prepare_messages
可在问答线程调用，状态交换由内部锁保护；BM25 检索对象一旦构建即不可变，
问答线程持有的旧引用在交换后依然自洽。
"""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Callable

from .retriever import Bm25Retriever
from ..utils.config import get_lib_index_dir

INDEX_VERSION = 2
STATE_FILENAME = "fulltext.json"

MIN_PARA_CHARS = 20          # 短于此的段落不进索引（页眉页脚噪音）
MAX_CHUNK_CHARS = 1200       # 单段入库上限
MAX_CHUNKS_PER_PDF = 4000    # 单篇 PDF 段落上限（防异常超大文件）

CTX_CHUNK_CHARS = 700        # 进入上下文的单段截断长度
CTX_CHUNKS_PER_ITEM = 2      # 每篇文献最多携带的段落数
CTX_MAX_ITEMS = 8            # 每次问答最多引用的文献数（全文模式）
META_MAX_ITEMS = 12          # 只问库模式下最多列出的条目数
FT_SCAN_HITS = 30            # 全文检索扫描的原始命中数（再按篇聚合）

SYSTEM_PROMPT = (
    "你是「文献工作台」的库内问答助手，帮助用户跨文献综合分析其 Zotero 文献库。\n"
    "规则：\n"
    "1. 只依据下方【文献库检索结果】中的内容回答；库内没有的信息不要编造\n"
    "2. 引用某条文献的证据时，在句末标注角标，如 [1] 或 [2][4]；"
    "角标只能使用检索结果中已有的编号\n"
    "3. 检索结果不足以回答时，明确说明库内缺少哪类文献，可建议补充方向\n"
    "4. 使用中文回答，专业术语可保留英文并附中文解释"
)


def extract_pdf_chunks(pdf_path: str) -> list[dict]:
    """用 PyMuPDF 抽取 PDF 段落块（只读打开）。

    Returns:
        [{"p": 页码(1基), "t": 段落文本}, ...]；失败返回空列表。
    """
    try:
        import fitz
        with fitz.open(pdf_path) as doc:
            chunks: list[dict] = []
            for pno, page in enumerate(doc, 1):
                text = page.get_text()
                if not text:
                    continue
                for para in re.split(r"\n\s*\n", text):
                    p = para.strip()
                    if len(p) >= MIN_PARA_CHARS:
                        chunks.append({"p": pno, "t": p[:MAX_CHUNK_CHARS]})
                        if len(chunks) >= MAX_CHUNKS_PER_PDF:
                            return chunks
            return chunks
    except Exception:
        return []


def _short_authors(authors: list[str]) -> str:
    """作者列表 → 'Smith' / 'Smith et al.' 的引文式短标注。"""
    if not authors:
        return "未知作者"
    first = (authors[0] or "").split(",")[0].strip() or (authors[0] or "作者")
    return first if len(authors) == 1 else f"{first} et al."


class LibraryQAEngine:
    """库内问答引擎：索引构建、混合检索、LLM 消息组装。"""

    def __init__(self, index_dir: str | Path | None = None) -> None:
        self._lock = threading.Lock()
        self._index_dir = Path(index_dir) if index_dir else get_lib_index_dir()
        # key -> 元数据快照 dict
        self._items: dict[str, dict] = {}
        self._meta_retriever: Bm25Retriever | None = None
        self._ft_retriever: Bm25Retriever | None = None
        # 持久化镜像：{"version", "items": {key: {"pdf","mtime","error"}}, "chunks": [...]}
        self._ft_state: dict = {"version": INDEX_VERSION, "items": {}, "chunks": []}

    # ---- 状态查询 ----

    @property
    def is_ready(self) -> bool:
        """元数据索引是否已构建（可做「只问库」级问答）。"""
        with self._lock:
            return self._meta_retriever is not None

    def index_stats(self) -> tuple[int, int]:
        """返回 (已索引全文的文献数, 段落总数)。"""
        with self._lock:
            state = self._ft_state
            items = state.get("items", {})
            n_docs = sum(1 for v in items.values() if not v.get("error"))
            return n_docs, len(state.get("chunks", []))

    # ---- 索引构建（后台线程调用） ----

    def set_items(self, items: list) -> None:
        """载入 Zotero 条目快照并重建元数据轻索引。

        Args:
            items: ZoteroItem 列表（zotero_parser.ZoteroItem）。
        """
        items_by_key: dict[str, dict] = {}
        meta_chunks: list[dict] = []
        for it in items:
            abstract = getattr(it, "abstract", "") or ""
            d = {
                "key": it.key,
                "title": it.title or "",
                "authors": list(getattr(it, "authors", []) or []),
                "year": it.year or "",
                "publication": it.publication or "",
                "doi": it.doi or "",
                "abstract": abstract,
                "pdf_path": it.pdf_path or "",
                "item_type": getattr(it, "item_type", ""),
            }
            items_by_key[it.key] = d
            text = " ".join(filter(None, [
                d["title"], " ".join(d["authors"]), d["year"],
                d["publication"], abstract, d["doi"],
            ]))
            if text.strip():
                meta_chunks.append({"text": text, "k": it.key})

        retriever = Bm25Retriever()
        retriever.index(meta_chunks)
        with self._lock:
            self._items = items_by_key
            self._meta_retriever = retriever

    def refresh_fulltext(
        self,
        items: list,
        progress_cb: Callable[[int, int, str], None] | None = None,
        interrupt_cb: Callable[[], bool] | None = None,
        force: bool = False,
    ) -> dict:
        """增量刷新全量索引并原子换入内存检索器。

        Args:
            items: ZoteroItem 列表（与 set_items 相同来源）。
            progress_cb: (已完成, 总数, 当前文件名) 进度回调。
            interrupt_cb: 返回 True 时中断（已完成部分仍会落盘）。
            force: True 时忽略缓存全量重建。

        Returns:
            {"items": 索引文献数, "chunks": 段落总数}
        """
        state = {"version": INDEX_VERSION, "items": {}, "chunks": []}
        if not force:
            loaded = self._load_state()
            if loaded is not None:
                state = loaded
        items_map: dict[str, dict] = state.setdefault("items", {})
        chunks_by_key: dict[str, list[dict]] = {}
        for c in state.get("chunks", []):
            chunks_by_key.setdefault(c.get("k", ""), []).append(c)
        changed = force  # 是否需要落盘（无实际变化时跳过 15MB 级重写）

        targets: list[tuple[str, str]] = []
        for it in items:
            pdf = (it.pdf_path or "").strip()
            if pdf:
                targets.append((it.key, pdf))
        target_keys = {k for k, _ in targets}

        # 移除已不在库中（或已无 PDF）的条目
        for k in list(items_map.keys()):
            if k not in target_keys:
                items_map.pop(k, None)
                chunks_by_key.pop(k, None)
                changed = True

        total = len(targets)
        for i, (key, pdf) in enumerate(targets):
            if interrupt_cb is not None and interrupt_cb():
                break
            try:
                mtime = os.path.getmtime(pdf)
            except OSError:
                mtime = -1.0
            entry = items_map.get(key) or {}

            if mtime <= 0:
                # PDF 消失：记录错误态避免每次启动反复重试
                if not entry.get("error"):
                    changed = True
                items_map[key] = {"pdf": pdf, "mtime": 0.0, "error": True}
                chunks_by_key.pop(key, None)
            elif force or entry.get("mtime") != mtime:
                chs = extract_pdf_chunks(pdf)
                chunks_by_key[key] = chs
                items_map[key] = {"pdf": pdf, "mtime": mtime, "error": False}
                changed = True
            # else: 缓存仍有效，跳过

            if progress_cb is not None:
                progress_cb(i + 1, total, os.path.basename(pdf))

        chunks_flat = [
            {"k": k, "p": c["p"], "t": c["t"]}
            for k, chs in chunks_by_key.items() for c in chs
        ]
        state["chunks"] = chunks_flat
        if changed:
            self._save_state(state)

        retriever = Bm25Retriever()
        retriever.index([
            {"text": c["t"], "page": c["p"], "k": c["k"]} for c in chunks_flat
        ])
        with self._lock:
            self._ft_retriever = retriever
            self._ft_state = state
        return {"items": len(chunks_by_key), "chunks": len(chunks_flat)}

    # ---- 检索与消息组装（问答线程调用） ----

    def prepare_messages(self, question: str, history: list[dict] | None = None,
                         metadata_only: bool = False) -> tuple[list[dict], list[dict]]:
        """检索库内证据并组装 LLM 消息。

        Returns:
            (messages, references)。references 每项：
            {"n","key","title","authors","year","pdf_path","page","has_pdf"}。
        """
        with self._lock:
            meta_r = self._meta_retriever
            ft_r = self._ft_retriever
            items = dict(self._items)

        picked: dict[str, dict] = {}
        if meta_r is not None:
            for hit in meta_r.search(question, top_k=META_MAX_ITEMS):
                k = hit.get("k")
                if k and k not in picked:
                    picked[k] = {"meta": hit.get("score", 0.0), "ft": 0.0, "chunks": []}
        if not metadata_only and ft_r is not None:
            per_item: dict[str, list[tuple[int, str, float]]] = {}
            for hit in ft_r.search(question, top_k=FT_SCAN_HITS):
                k = hit.get("k")
                if k:
                    per_item.setdefault(k, []).append(
                        (hit.get("page", 0), hit.get("text", ""), hit.get("score", 0.0)))
            ranked = sorted(
                per_item.items(),
                key=lambda kv: sum(h[2] for h in kv[1][:CTX_CHUNKS_PER_ITEM]),
                reverse=True)
            for k, chs in ranked[:CTX_MAX_ITEMS]:
                e = picked.setdefault(k, {"meta": 0.0, "ft": 0.0, "chunks": []})
                e["ft"] = sum(h[2] for h in chs[:CTX_CHUNKS_PER_ITEM])
                e["chunks"] = [(p, t) for p, t, _ in chs[:CTX_CHUNKS_PER_ITEM]]

        meta_keys = sorted((k for k, e in picked.items() if e["meta"] > 0),
                           key=lambda k: picked[k]["meta"], reverse=True)
        ft_only = sorted((k for k, e in picked.items() if e["meta"] <= 0),
                         key=lambda k: picked[k]["ft"], reverse=True)
        limit = META_MAX_ITEMS if metadata_only else CTX_MAX_ITEMS
        ordered_keys = (meta_keys + ft_only)[:limit]

        references: list[dict] = []
        blocks: list[str] = []
        for n, k in enumerate(ordered_keys, 1):
            d = items.get(k)
            if d is None:
                continue
            e = picked[k]
            author_str = _short_authors(d["authors"])
            head = f"[{n}] {author_str} ({d['year'] or '?'}). {d['title']}"
            if d["publication"]:
                head += f". {d['publication']}"
            if d["doi"]:
                head += f" DOI: {d['doi']}"
            lines = [head]
            if metadata_only and d["abstract"]:
                lines.append("    摘要: " + d["abstract"][:300])
            for page, text in e["chunks"]:
                lines.append(f"    （第 {page} 页）{text[:CTX_CHUNK_CHARS]}")
            blocks.append("\n".join(lines))
            references.append({
                "n": n, "key": k, "title": d["title"], "authors": author_str,
                "year": d["year"], "pdf_path": d["pdf_path"],
                "page": e["chunks"][0][0] if e["chunks"] else 0,
                "has_pdf": bool(d["pdf_path"]),
            })

        if blocks:
            context = "【文献库检索结果】\n\n" + "\n\n".join(blocks)
        else:
            context = "【文献库检索结果】\n（未检索到与问题相关的库内文献）"

        messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
        for m in (history or [])[-6:]:
            messages.append({"role": m.get("role", "user"), "content": m.get("content", "")})
        messages.append({
            "role": "user",
            "content": context + f"\n\n【用户问题】\n{question}",
        })
        return messages, references

    # ---- 持久化 ----

    def _state_path(self) -> Path:
        return self._index_dir / STATE_FILENAME

    def _load_state(self) -> dict | None:
        f = self._state_path()
        if not f.exists():
            return None
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if not isinstance(data, dict) or data.get("version") != INDEX_VERSION:
            return None
        return data

    def _save_state(self, state: dict) -> None:
        try:
            self._index_dir.mkdir(parents=True, exist_ok=True)
            f = self._state_path()
            tmp = f.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(state, ensure_ascii=False), encoding="utf-8")
            tmp.replace(f)
        except OSError:
            pass  # 磁盘异常时放弃落盘，内存索引仍然可用
