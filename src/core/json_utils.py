"""通用 JSON 解析工具 —— 多层容错解析 LLM 返回的 JSON。

统一了项目中多处重复的 JSON 容错解析逻辑：
1. 直接解析
2. 提取 ```json ... ``` 代码块
3. 提取第一个 { 到最后一个 }
4. 清洗未转义换行符后重试
5. 中文全角花括号替换后重试
"""

from __future__ import annotations

import json as _json
import re


def parse_json_response(raw: str | None) -> dict | None:
    """解析 LLM 返回的 JSON，成功返回 dict，失败返回 None。"""
    if not raw or not raw.strip():
        return None
    text = raw.strip()

    # 1. 直接解析
    try:
        return _json.loads(text)
    except (_json.JSONDecodeError, TypeError):
        pass

    # 2. ```json ... ``` 或 ``` ... ```
    for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            try:
                return _json.loads(m.group(1).strip())
            except (_json.JSONDecodeError, TypeError):
                pass

    # 3. 提取 { ... }
    first = text.find('{')
    last = text.rfind('}')
    if first >= 0 and last > first:
        json_str = text[first:last + 1]
        try:
            return _json.loads(json_str)
        except (_json.JSONDecodeError, TypeError):
            pass
        # 3b. 清洗未转义换行符（LLM 常见错误）
        try:
            cleaned = re.sub(r'(?<!\\)"\s*\n\s*', r'\\n', json_str)
            cleaned = re.sub(r'(?<!\\)\n\s*"', r'\\n"', cleaned)
            result = _json.loads(cleaned)
            if result is not None:
                return result
        except Exception:
            pass

    # 4. 中文全角花括号替换后重试
    try:
        alt = text.replace('\uff5b', '{').replace('\uff5d', '}')
        first_a = alt.find('{')
        last_a = alt.rfind('}')
        if first_a >= 0 and last_a > first_a:
            return _json.loads(alt[first_a:last_a + 1])
    except Exception:
        pass

    return None
