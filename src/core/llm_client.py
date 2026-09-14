"""LLM API 客户端 —— 统一的 OpenAI 兼容接口，支持流式与同步调用。"""

from __future__ import annotations

import time
from collections.abc import Generator

from openai import (
    OpenAI, BadRequestError, APIConnectionError, APITimeoutError,
    InternalServerError, RateLimitError,
)


def _mentions_max_tokens(err: Exception) -> bool:
    """判断 400 错误是否与 max_tokens 有关（各提供商上限不一）。"""
    text = str(err).lower()
    return "max_tokens" in text or "max output" in text or "maximum length" in text


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
                  max_tokens: int | None = None, json_mode: bool = False,
                  return_meta: bool = False):
        """同步对话，返回完整回复文本。

        支持纯文本和视觉（图片+文本）两种消息格式。
        视觉格式：content 为 list，包含 {"type":"text",...} 和 {"type":"image_url",...}

        Args:
            messages: 消息列表
            timeout: API 调用超时秒数（默认 120s）
            max_tokens: 最大生成 token 数（None=不限制）
            json_mode: 要求输出 JSON 对象（response_format=json_object）。
                       部分兼容服务不支持该参数时自动降级为普通请求。
            return_meta: True 时返回 (text, meta)；meta 含 finish_reason，
                       可据此判断输出是否因长度上限被截断（"length"）。
                       用返回值而非实例属性传递，因为 client 被多线程共享。

        Returns:
            默认返回回复文本；return_meta=True 时返回 (文本, meta dict)。
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
        except BadRequestError as e:
            # 部分兼容服务不支持这些参数 → 逐个去掉后降级重试
            retried = False
            if use_json:
                kwargs.pop("response_format", None)
                retried = True
            if "max_tokens" in kwargs and _mentions_max_tokens(e):
                kwargs.pop("max_tokens", None)
                retried = True
            if not retried:
                raise
            response = self._retry(lambda: self._client.chat.completions.create(**kwargs))
        choice = response.choices[0] if response.choices else None
        content = choice.message.content if choice else ""
        if return_meta:
            finish = getattr(choice, "finish_reason", None) if choice else None
            return content or "", {"finish_reason": finish}
        return content or ""


# ---- 预设提供商 ----

# 已知支持视觉（图片输入）的模型名（小写）。用于：
# 1) 设置对话框多模态 tab 的模型下拉标注「（视觉）」；
# 2) 旧配置迁移时判断解析接口模型是否可作多模态接口。
VISION_MODELS: frozenset[str] = frozenset({
    "glm-5v-turbo", "glm-4.6v", "glm-4.6v-flash",
    "glm-4.1v-thinking-flash", "glm-4v-flash",
    "deepseek-v4-flash-vision-exp",
})

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
        # 与 Go 端点 /v1/models 实际返回一致（OpenAI 兼容 chat/completions）
        "models": [
            # GLM / DeepSeek
            "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5",
            "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
            # Kimi / MiniMax / Qwen / MiMo / 其它
            "kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5",
            "minimax-m3", "minimax-m2.7", "minimax-m2.5",
            "qwen3.8-max", "qwen3.8-flash", "qwen3.7-max", "qwen3.7-plus",
            "qwen3.6-plus", "qwen3.5-plus",
            "mimo-v2.5", "mimo-v2.5-pro", "mimo-v2-pro", "mimo-v2-omni",
            "hy4-preview", "hy3", "hy3-preview",
            "longcat-2.0", "omen-alpha",
            "gpt-5.6-luna", "grok-4.6", "grok-4.5",
            "muse-spark-1.3-contributor", "muse-spark-1.2-contributor",
        ],
        "description": (
            "OpenCode Go 订阅（Go 端点共 35 个模型）— GLM / Kimi / DeepSeek / MiniMax / "
            "Qwen / MiMo / Hy / LongCat / GPT / Grok 等；muse-spark 系列为 Contributor 档限区模型。"
        ),
    },
    "OpenCode Zen": {
        "base_url": "https://opencode.ai/zen/v1",
        # Zen 的 /v1/models 还含 GPT/Claude/Gemini/Grok 等名牌模型，但它们走
        # /responses、/messages 等原生端点，OpenAI chat/completions 只兼容下列模型
        "models": [
            "glm-5.3", "glm-5.3-flash", "glm-5.2", "glm-5.1", "glm-5",
            "kimi-k3", "kimi-k2.7-code", "kimi-k2.6", "kimi-k2.5",
            "deepseek-v4-pro", "deepseek-v4-flash", "deepseek-v4-flash-vision-exp",
            "minimax-m3", "minimax-m2.7", "minimax-m2.5",
            # 免费模型（/chat/completions 可直接调用）
            "big-pickle",
            "mimo-v2.5-free",
            "deepseek-v4-flash-free",
            "ling-3.0-flash-fin-free",
            "nemotron-3-ultra-free",
            "nemotron-3.5-lightning-free",
            "muse-spark-1.3-contributor-free",
            "muse-spark-1.2-contributor-free",
        ],
        "description": (
            "OpenCode Zen 按量付费 — GLM / Kimi / DeepSeek / MiniMax；免费模型：big-pickle、"
            "mimo-v2.5-free、deepseek-v4-flash-free、ling-3.0-flash-fin-free、nemotron-3-ultra-free、"
            "nemotron-3.5-lightning-free、muse-spark 系（数据可能用于训练）。"
            "GPT / Claude / Gemini / Grok / Qwen 走原生端点，不兼容本应用的 chat/completions 调用。"
        ),
    },
    "Ollama": {
        "base_url": "https://ollama.com/v1",
        # ollama.com 云端目录（带 cloud 能力的模型）；云专属模型标签为 :cloud，
        # 本地同款模型的云端变体在参数标签后加 -cloud
        "models": [
            "glm-5.3:cloud", "glm-5.3-flash:cloud", "glm-5.2:cloud", "glm-5.1:cloud",
            "deepseek-v4-flash:cloud", "deepseek-v4-flash:0731-cloud",
            "deepseek-v4-pro:cloud",
            "kimi-k3:cloud", "kimi-k2.7-code:cloud", "kimi-k2.6:cloud",
            "minimax-m3:cloud", "minimax-m2.7:cloud",
            "qwen3.5:122b-cloud",
            "gemma4:cloud",
            "mistral-large-3:cloud",
            "nemotron-3-ultra:cloud", "nemotron-3-super:120b-cloud",
            "gpt-oss:120b-cloud", "gpt-oss:20b-cloud",
        ],
        "description": (
            "Ollama 云端（https://ollama.com/v1，需 API Key）。模型名遵循 name:tag 格式，"
            "云专属模型标签为 :cloud（如 kimi-k3:cloud），本地同款模型的云端变体在参数标签后加 "
            "-cloud（如 gpt-oss:120b-cloud）；模型框支持手动输入目录中的其它标签。"
        ),
    },
    "自定义": {
        "base_url": "",
        "models": [],
        "description": "自定义 OpenAI 兼容接口",
    },
}
