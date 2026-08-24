"""轻量本地检索器 —— BM25 关键词检索起步，预留向量检索升级接口。

用法:
    retriever = Bm25Retriever()
    retriever.index([{"text": "...", "section": "...", "page": 1}, ...])
    hits = retriever.search("问题", top_k=5)
    # hits: [{"text","section","page","score"}, ...]

后续升级向量检索：实现相同接口的 VectorRetriever 即可无缝替换。
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod

from rank_bm25 import BM25Okapi


def split_paragraphs(text: str, min_chars: int = 20) -> list[str]:
    """按空行切段并过滤短段（阅读问答/库内索引/引文核查三处共用的切块口径）。

    空行按宽松匹配（\\n\\s*\\n，含 "\\n \\n" 型）；短于 min_chars 的段
    （页眉页脚噪音）不返回。
    """
    paras: list[str] = []
    for para in re.split(r"\n\s*\n", text):
        p = para.strip()
        if len(p) >= min_chars:
            paras.append(p)
    return paras


def _tokenize(text: str) -> list[str]:
    """中文按字二元组 + 英文单词分词（兼顾中英文混合）。"""
    text = text.lower()
    tokens: list[str] = []
    # 英文单词 / 数字
    for w in re.findall(r"[a-z0-9][a-z0-9\-_]{1,}", text):
        tokens.append(w)
    # 中文连续串 → 二元组
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        if len(run) == 1:
            tokens.append(run)
        else:
            for i in range(len(run) - 1):
                tokens.append(run[i:i + 2])
    return tokens


class Retriever(ABC):
    """检索器抽象接口。"""

    @abstractmethod
    def index(self, chunks: list[dict]) -> None:
        """建立索引。chunks: [{"text","section","page"}, ...]"""

    @abstractmethod
    def search(self, query: str, top_k: int = 5) -> list[dict]:
        """返回按相关度排序的 top-k 段落。"""


class Bm25Retriever(Retriever):
    """BM25 关键词检索（离线、毫秒级、无重依赖）。"""

    def __init__(self) -> None:
        self._chunks: list[dict] = []
        self._bm25: BM25Okapi | None = None

    def index(self, chunks: list[dict]) -> None:
        """建立索引。chunks: [{"text","section","page",...}, ...]

        可选 "index_text" 字段：仅参与 BM25 打分（如「章节名 + 正文」），
        命中结果的 text 仍返回原始 text，不携带评分冗余。
        """
        self._chunks = [c for c in chunks if (c.get("text") or "").strip()]
        corpus = [_tokenize(c.get("index_text") or c["text"]) for c in self._chunks]
        self._bm25 = BM25Okapi(corpus) if corpus else None

    def search(self, query: str, top_k: int = 5) -> list[dict]:
        if not self._bm25 or not self._chunks:
            return []
        query_tokens = _tokenize(query)
        if not query_tokens:
            return []
        scores = self._bm25.get_scores(query_tokens)
        ranked = sorted(range(len(scores)), key=lambda i: scores[i], reverse=True)

        # 退化兜底：极小语料（≤2 篇）时 BM25 的 IDF 全为 0/负（项数过半的词
        # idf≤0），所有得分都 ≤0，直接截断会颗粒无收 —— 改按词面重叠排序
        # （与主索引一致，重叠统计含 index_text 评分字段）。
        if scores[ranked[0]] <= 0:
            query_set = set(query_tokens)

            def _overlap(i: int) -> int:
                c = self._chunks[i]
                return len(query_set & set(_tokenize(c.get("index_text") or c["text"])))

            overlapped = sorted(
                range(len(self._chunks)),
                key=lambda i: _overlap(i),
                reverse=True)
            results: list[dict] = []
            for i in overlapped:
                overlap = _overlap(i)
                if overlap <= 0:
                    break
                results.append(self._make_hit(self._chunks[i], float(overlap)))
                if len(results) >= top_k:
                    break
            return results

        results = []
        for i in ranked:
            if scores[i] <= 0:
                break
            results.append(self._make_hit(self._chunks[i], float(scores[i])))
            if len(results) >= top_k:
                break
        return results

    @staticmethod
    def _make_hit(chunk: dict, score: float) -> dict:
        hit = {
            "text": chunk.get("text", ""),
            "section": chunk.get("section", ""),
            "page": chunk.get("page", 0),
            "score": round(score, 3),
        }
        # 透传调用方附加的自定义键（如库级检索的条目 key "k"、章节 "s"）
        hit.update({k: v for k, v in chunk.items()
                    if k not in ("text", "section", "page", "index_text")})
        return hit
