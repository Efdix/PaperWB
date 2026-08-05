"""LLM API 客户端 —— 统一的 OpenAI 兼容接口，支持流式与同步调用。"""

from __future__ import annotations

from collections.abc import Generator

from openai import OpenAI


class LLMClient:
    """统一的 LLM API 客户端，封装 OpenAI 兼容接口。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def chat_stream(self, messages: list[dict]) -> Generator[str, None, None]:
        """流式对话生成器，自动跳过 reasoning_content（如 DeepSeek R1）。"""
        response = self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            temperature=0.3,
        )
        for chunk in response:
            delta = chunk.choices[0].delta
            if delta.content:
                yield delta.content

    def chat_sync(self, messages: list[dict], timeout: float = 120.0,
                  max_tokens: int | None = None) -> str:
        """同步对话，返回完整回复文本。

        支持纯文本和视觉（图片+文本）两种消息格式。
        视觉格式：content 为 list，包含 {"type":"text",...} 和 {"type":"image_url",...}

        Args:
            messages: 消息列表
            timeout: API 调用超时秒数（默认 120s）
            max_tokens: 最大生成 token 数（None=不限制）
        """
        kwargs: dict = dict(
            model=self.model,
            messages=messages,
            stream=False,
            timeout=timeout,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        response = self._client.chat.completions.create(**kwargs)
        content = response.choices[0].message.content
        return content or ""


# ---- 预设提供商 ----

PROVIDERS: dict[str, dict] = {
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "description": "DeepSeek V4 系列（1M 上下文 | Flash 实惠 / Pro 最强）",
    },
    "Mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "models": ["mimo-v2.5", "mimo-v2.5-pro"],
        "description": "Mimo 大模型系列",
    },
    "OpenCode Go": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "models": [
            "glm-5.2", "glm-5.1",
            "kimi-k2.7-code", "kimi-k2.6",
            "deepseek-v4-pro", "deepseek-v4-flash",
            "mimo-v2.5", "mimo-v2.5-pro",
        ],
        "description": "OpenCode Go — GLM / Kimi / DeepSeek / MiMo",
    },
    "OpenCode Zen": {
        "base_url": "https://opencode.ai/zen/v1",
        "models": [
            "glm-5.2", "glm-5.1", "glm-5",
            "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5",
            "deepseek-v4-pro", "deepseek-v4-flash",
            "deepseek-v4-flash-free",
            "mimo-v2.5-free",
            "north-mini-code-free",
            "nemotron-3-ultra-free",
            "minimax-m3", "minimax-m2.7", "minimax-m2.5",
            "grok-build-0.1",
            "big-pickle",
        ],
        "description": "OpenCode Zen — 含免费模型（/chat/completions）",
    },
    "自定义": {
        "base_url": "",
        "models": [],
        "description": "自定义 OpenAI 兼容接口",
    },
}
