"""LLM API 客户端 —— 统一的 OpenAI 兼容接口，支持流式与同步调用。"""

from __future__ import annotations

import time
from collections.abc import Generator

from openai import (
    OpenAI, BadRequestError, APIConnectionError, APITimeoutError,
    InternalServerError, RateLimitError,
)


class LLMClient:
    """统一的 LLM API 客户端，封装 OpenAI 兼容接口。"""

    def __init__(self, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    @staticmethod
    def _retry(fn, max_retries: int = 2, base_delay: float = 2.0):
        """对瞬时错误（限流/网络/超时/5xx）做指数退避重试。"""
        last_exc: Exception | None = None
        for attempt in range(max_retries + 1):
            try:
                return fn()
            except (RateLimitError, APIConnectionError,
                    APITimeoutError, InternalServerError) as e:
                last_exc = e
                if attempt >= max_retries:
                    break
                time.sleep(base_delay * (2 ** attempt))
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("重试逻辑异常")

    def chat_stream(self, messages: list[dict]) -> Generator[str, None, None]:
        """流式对话生成器，自动跳过 reasoning_content（如 DeepSeek R1）。"""
        response = self._retry(lambda: self._client.chat.completions.create(
            model=self.model,
            messages=messages,
            stream=True,
            temperature=0.3,
        ))
        for chunk in response:
            # 部分 OpenAI 兼容服务（含 usage 的尾包）会发 choices 为空的 chunk
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    def chat_sync(self, messages: list[dict], timeout: float = 120.0,
                  max_tokens: int | None = None, json_mode: bool = False) -> str:
        """同步对话，返回完整回复文本。

        支持纯文本和视觉（图片+文本）两种消息格式。
        视觉格式：content 为 list，包含 {"type":"text",...} 和 {"type":"image_url",...}

        Args:
            messages: 消息列表
            timeout: API 调用超时秒数（默认 120s）
            max_tokens: 最大生成 token 数（None=不限制）
            json_mode: 要求输出 JSON 对象（response_format=json_object）。
                       部分兼容服务不支持该参数时自动降级为普通请求。
        """
        kwargs: dict = dict(
            model=self.model,
            messages=messages,
            stream=False,
            timeout=timeout,
        )
        if max_tokens is not None:
            kwargs["max_tokens"] = max_tokens
        use_json = False
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}
            use_json = True

        try:
            response = self._retry(lambda: self._client.chat.completions.create(**kwargs))
        except BadRequestError:
            if use_json:
                # 该接口不支持 response_format → 降级普通请求
                kwargs.pop("response_format", None)
                response = self._retry(lambda: self._client.chat.completions.create(**kwargs))
            else:
                raise
        content = response.choices[0].message.content
        return content or ""


# ---- 预设提供商 ----

PROVIDERS: dict[str, dict] = {
    "DeepSeek": {
        "base_url": "https://api.deepseek.com",
        "models": ["deepseek-v4-flash", "deepseek-v4-pro"],
        "description": "DeepSeek V4 系列（1M 上下文，默认思考模式 | Flash 实惠 / Pro 最强）",
    },
    "GLM（智谱）": {
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "models": [
            # 文本（GLM-5 系列 → GLM-4 系列）
            "glm-5.3", "glm-5.2", "glm-5.1", "glm-5", "glm-5-turbo",
            "glm-4.7", "glm-4.7-flashx", "glm-4.6",
            "glm-4.5-air", "glm-4.5-airx", "glm-4-long", "glm-4-flashx-250414",
            # 免费文本模型
            "glm-4.7-flash", "glm-4.5-flash", "glm-4-flash-250414",
            # 视觉模型（可用于图表解读）
            "glm-5v-turbo", "glm-4.6v",
            # 免费视觉模型
            "glm-4.6v-flash", "glm-4.1v-thinking-flash", "glm-4v-flash",
        ],
        "description": (
            "智谱 GLM 全系列（含 GLM-5）。免费模型：glm-4.7-flash / glm-4.5-flash / "
            "glm-4-flash-250414 / glm-4.6v-flash / glm-4.1v-thinking-flash / glm-4v-flash；"
            "带 V 的为视觉模型，可用于图表解读。"
        ),
    },
    "Mimo": {
        "base_url": "https://api.xiaomimimo.com/v1",
        "models": ["mimo-v2.5", "mimo-v2.5-pro"],
        "description": "小米 MiMo V2.5 系列（1M 上下文；Pro 为深度思考旗舰，V2 旧系列已下线）",
    },
    "OpenCode Go": {
        "base_url": "https://opencode.ai/zen/go/v1",
        "models": [
            "glm-5.2", "glm-5.1",
            "kimi-k2.7-code", "kimi-k2.6",
            "deepseek-v4-pro", "deepseek-v4-flash",
            "mimo-v2.5", "mimo-v2.5-pro",
        ],
        "description": "OpenCode Go 订阅 — GLM / Kimi / DeepSeek / MiMo",
    },
    "OpenCode Zen": {
        "base_url": "https://opencode.ai/zen/v1",
        "models": [
            "glm-5.2", "glm-5.1", "glm-5",
            "kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5",
            "deepseek-v4-pro", "deepseek-v4-flash",
            "minimax-m3", "minimax-m2.7", "minimax-m2.5",
            # 免费模型（/chat/completions 可直接调用）
            "big-pickle",
            "mimo-v2.5-free",
            "hy3-free",
            "nemotron-3-ultra-free",
            "nemotron-3.5-lightning-free",
        ],
        "description": "OpenCode Zen — GLM / Kimi / DeepSeek / MiniMax，含多个免费模型（/chat/completions）",
    },
    "Ollama": {
        "base_url": "https://ollama.com/v1",
        "models": [
            "deepseek-v4-flash:0731-cloud",
            "gemma4:cloud",
            "gpt-oss:120b-cloud",
        ],
        "description": (
            "Ollama 云端（https://ollama.com/v1，需 API Key）。模型名遵循 name:tag 格式，"
            "云端变体标签以 -cloud 结尾；目录还提供 deepseek-v4-pro / kimi-k3 / glm-5.x / "
            "minimax-m3 等，可在模型框手动输入对应标签。"
        ),
    },
    "自定义": {
        "base_url": "",
        "models": [],
        "description": "自定义 OpenAI 兼容接口",
    },
}
