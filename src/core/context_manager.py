"""上下文管理器 —— 管理 PDF 文本与对话上下文，token 超限时自动截断。"""

from __future__ import annotations

import re


class ContextManager:
    """管理 PDF 文本和对话上下文，按 token 预算自动截断。

    论文问答统一使用 BM25 检索增强：只发送与问题最相关的段落（带章节/页码），
    不再区分短文/长文走不同策略。
    """

    CHARS_PER_TOKEN = 2  # 保守估算：中英文混合约 2 字符 ≈ 1 token

    def __init__(self, max_tokens: int = 1_000_000) -> None:
        self.max_tokens = max_tokens
        self._pdf_text: str = ""
        self._structured_doc: object | None = None  # StructuredDocument
        self._chat_history: list[dict] = []
        self._retriever = None  # Bm25Retriever（懒构建）
        self._retriever_key: int = 0  # 结构化文档 id，用于索引失效判断

    # ---- 公共 API ----

    def load_pdf_text(self, text: str) -> None:
        """加载新 PDF 纯文本，同时清空历史对话。"""
        self._pdf_text = text
        self._structured_doc = None
        self._chat_history.clear()
        self._invalidate_retriever()

    def load_structured_doc(self, doc: object) -> None:
        """加载结构化文档 —— 聊天时可引用 metadata/figures/references 等完整信息。"""
        self._structured_doc = doc
        self._invalidate_retriever()

    def load_history(self, history: list[dict]) -> None:
        """从持久化存储恢复对话历史。"""
        self._chat_history = history.copy()

    def get_history(self) -> list[dict]:
        """返回当前对话历史的副本。"""
        return list(self._chat_history)

    @property
    def has_pdf(self) -> bool:
        """是否已加载 PDF 文本或结构化文档。"""
        return bool(self._pdf_text) or self._structured_doc is not None

    def get_full_context_for_estimation(self) -> str:
        """返回 PDF 全文 + 对话历史用于 token 估算。"""
        history_text = "".join(m["content"] for m in self._chat_history)
        return self._build_full_context() + history_text

    def estimate_tokens(self, text: str) -> int:
        """粗略估算文本占用的 token 数。"""
        return len(text) // self.CHARS_PER_TOKEN

    def add_to_history(self, role: str, content: str) -> None:
        """向对话历史追加一条消息。"""
        self._chat_history.append({"role": role, "content": content})

    def clear_history(self) -> None:
        """清空对话历史。"""
        self._chat_history.clear()

    def build_messages(self, user_query: str) -> list[dict]:
        """构建发送给 LLM 的完整消息列表，超出 token 预算时自动截断。

        上下文拼接顺序：system prompt → 论文正文 → 元信息 → 图表描述 → 参考文献 → 对话历史 → 当前问题。
        """
        system_prompt = self._build_system_prompt()
        messages: list[dict] = [{"role": "system", "content": system_prompt}]

        # 为 AI 回复预留约 4000 token
        used = self.estimate_tokens(system_prompt) + self.estimate_tokens(user_query)
        budget = self.max_tokens - used - 4000

        # 构建完整上下文
        full_context = self._build_full_context()
        pdf_section = self._build_pdf_section(user_query, full_context, budget)
        messages.append({"role": "user", "content": pdf_section})

        history_budget = budget - self.estimate_tokens(pdf_section)
        messages.extend(self._get_recent_history(history_budget))
        messages.append({"role": "user", "content": user_query})

        return messages

    # ---- 内部方法 ----

    def _build_pdf_section(self, user_query: str, full_context: str, budget: int) -> str:
        """构建发给 LLM 的论文上下文 —— 统一走 BM25 检索增强。

        只发送与问题最相关的段落（带章节/页码），检索为空时才退回全量截断。
        """
        hits = self._retrieve(user_query, top_k=6)
        if hits:
            chunks_text = "\n\n".join(
                f"[{c['section'] or '正文'} · 第 {c['page']} 页]\n{c['text']}"
                for c in hits
            )
            chunks_text = "【检索到的论文相关段落】\n" + chunks_text
            # 预算允许时附带元信息/图表/参考文献，便于引文与元信息类问题
            extra = self._build_aux_context()
            if extra and self.estimate_tokens(chunks_text) + self.estimate_tokens(extra) < budget:
                chunks_text += "\n\n" + extra
            if self.estimate_tokens(chunks_text) < budget:
                return chunks_text
        return self._truncate_text(full_context, budget)

    def _invalidate_retriever(self) -> None:
        self._retriever = None
        self._retriever_key = 0

    def _ensure_retriever(self):
        """懒构建段落索引：优先结构化文档正文，否则对纯文本按段落切块。"""
        doc = self._structured_doc
        if doc is None and not self._pdf_text:
            return None
        key = id(doc) if doc is not None else ("text", id(self._pdf_text))
        if self._retriever is not None and self._retriever_key == key:
            return self._retriever
        from .retriever import Bm25Retriever
        chunks = []
        if doc is not None:
            cur_section = ""
            for e in doc.display_elements:
                if e.element_type == "subtitle" and e.text:
                    cur_section = e.text
                if e.text and e.element_type in ("body", "abstract_body"):
                    chunks.append({
                        "text": e.text,
                        "section": e.section_name or cur_section,
                        "page": e.page,
                    })
        elif self._pdf_text:
            for para in re.split(r"\n\s*\n", self._pdf_text):
                p = para.strip()
                if len(p) >= 20:
                    chunks.append({"text": p, "section": "", "page": 0})
        retriever = Bm25Retriever()
        retriever.index(chunks)
        self._retriever = retriever
        self._retriever_key = key
        return retriever

    def _retrieve(self, query: str, top_k: int = 5) -> list[dict]:
        retriever = self._ensure_retriever()
        if retriever is None:
            return []
        return retriever.search(query, top_k=top_k)

    def _build_full_context(self) -> str:
        """构建完整的论文上下文：正文 + 元信息 + 图表 + 参考文献。"""
        parts = []

        # 正文部分（纯文本或 display_elements）
        if self._pdf_text:
            parts.append("【论文正文】\n" + self._pdf_text)
        elif self._structured_doc is not None:
            doc = self._structured_doc
            display_text = "\n\n".join(
                e.text for e in doc.display_elements
                if e.text and e.element_type not in ("header_footer", "publisher_logo")
            )
            if display_text:
                parts.append("【论文正文】\n" + display_text)

        aux = self._build_aux_context()
        if aux:
            parts.append(aux)

        if not parts:
            return self._pdf_text or ""

        return "\n\n".join(parts)

    def _build_aux_context(self) -> str:
        """构建元信息 + 图表描述 + 参考文献（结构化文档的非正文部分）。"""
        doc = self._structured_doc
        if doc is None:
            return ""
        parts = []

        # 元信息（作者、单位、出版信息等）
        if doc.metadata_pool:
            meta_text = "\n".join(
                f"[{e.element_type}] {e.text}"
                for e in doc.metadata_pool if e.text
            )
            if meta_text:
                parts.append("【元信息（作者/出版信息）】\n" + meta_text)

        # 图表描述
        if doc.figures:
            fig_text = "\n".join(
                f"图{e.element_id}: {e.image_caption or ''} — {e.image_description or ''}"
                for e in doc.figures
                if (e.image_caption or e.image_description)
            )
            if fig_text:
                parts.append("【图表描述】\n" + fig_text)

        # 参考文献
        if doc.references:
            ref_text = "\n".join(
                f"[{i+1}] {r.text}"
                for i, r in enumerate(doc.references) if r.text
            )
            if ref_text:
                parts.append("【参考文献】\n" + ref_text)

        return "\n\n".join(parts)

    @staticmethod
    def _build_system_prompt() -> str:
        return (
            "你是一位专业的科研文献分析助手。你的任务是帮助用户理解和分析学术论文。\n\n"
            "要求：\n"
            "1. 基于提供的论文原文内容回答问题，不要编造信息\n"
            "2. 回答要准确、专业、有条理\n"
            "3. 如果引用原文，注明所在页码\n"
            "4. 如果问题超出论文范围，诚实说明\n"
            "5. 使用中文回答，专业术语可保留英文并附中文解释\n"
            "6. 注意利用提供的元信息、图表描述、参考文献等结构化数据来回答相关问题"
        )

    def _truncate_text(self, text: str, token_budget: int) -> str:
        """截断文本：保留前 70% + 后 30%，中间用省略标记替换。"""
        char_budget = token_budget * self.CHARS_PER_TOKEN
        if len(text) <= char_budget:
            return text
        head_size = int(char_budget * 0.7)
        tail_size = int(char_budget * 0.3)
        skipped = len(text) - head_size - tail_size
        return (
            text[:head_size]
            + f"\n\n...（中间部分已省略，约 {skipped} 字符）...\n\n"
            + text[-tail_size:]
        )

    def _get_recent_history(self, token_budget: int) -> list[dict]:
        """从对话历史末尾向前取消息，直到超出 token 预算。"""
        if not self._chat_history:
            return []
        result: list[dict] = []
        used = 0
        for msg in reversed(self._chat_history):
            msg_tokens = self.estimate_tokens(msg["content"])
            if used + msg_tokens > token_budget:
                break
            result.insert(0, msg)
            used += msg_tokens
        return result
