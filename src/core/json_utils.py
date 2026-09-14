"""通用 JSON 解析工具 —— 多层容错解析 LLM 返回的 JSON。

统一了项目中多处重复的 JSON 容错解析逻辑：
1. 直接解析
2. 提取 ```json ... ``` 代码块
3. 提取第一个 { 到最后一个 }
4. 清洗未转义换行符后重试
5. 中文全角花括号替换后重试
6. 清理尾随逗号、转义字符串内的裸控制字符
7. 截断修复：模型输出撞上长度上限时 JSON 会从中间断开，
   丢弃末尾不完整元素并补齐未闭合的括号，返回已解析的前缀

``parse_json_response_verbose`` 额外返回诊断信息（是否发生截断修复、
使用的策略），供长报告类调用方判断结果是否完整。
"""

from __future__ import annotations

import json as _json
import re

# 字符串内需要转义的裸控制字符（JSON 规范不允许字面出现在字符串中）
_CONTROL_ESCAPES = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}


def _try_loads(text: str):
    """解析 JSON 对象；失败或顶层非 dict 时返回 None。"""
    try:
        obj = _json.loads(text)
    except (_json.JSONDecodeError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def _extract_braces(text: str) -> str | None:
    """取第一个 { 到最后一个 } 之间的内容（截断时可能没有收尾 }）。"""
    first = text.find("{")
    if first < 0:
        return None
    last = text.rfind("}")
    if last > first:
        return text[first:last + 1]
    return text[first:]


def _escape_raw_control_chars(text: str) -> str:
    """转义字符串内部的裸控制字符（换行/制表符等）。

    LLM 常在字符串值中直接输出换行而非 ``\\n``。既有的
    「换行紧邻引号」规则只能救回行首/行尾的情况，字符串中段的换行
    仍需按状态机逐字符处理：只有处于字符串内部的控制字符才需要转义。
    """
    out: list[str] = []
    in_string = False
    escaped = False
    for ch in text:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            out.append(ch)
            escaped = True
            continue
        if ch == '"':
            in_string = not in_string
            out.append(ch)
            continue
        if in_string and ch in _CONTROL_ESCAPES:
            out.append(_CONTROL_ESCAPES[ch])
            continue
        if in_string and ord(ch) < 0x20:
            out.append("\\u%04x" % ord(ch))
            continue
        out.append(ch)
    return "".join(out)


def _strip_trailing_commas(text: str) -> str:
    """删除对象/数组末尾的多余逗号：``{"a":1,}`` / ``[1,2,]``。"""
    return re.sub(r",(\s*[}\]])", r"\1", text)


def _repair_truncated(text: str) -> str | None:
    """修复被截断的 JSON：丢弃末尾不完整元素，补齐未闭合的括号。

    只在输出因长度上限被切断时生效。策略是从尾部逐步回退到最近的
    元素边界，再补齐引号/括号；返回仍可解析的 JSON 文本，无法修复时
    返回 None。
    """
    # 先把字符串内未转义的换行清掉，否则回退边界判断会被打断
    text = _escape_raw_control_chars(text)
    in_string = False
    escaped = False
    stack: list[str] = []
    # 记录每个层级的「安全截断点」：刚闭合一个完整元素、且位于容器内
    cut_points: list[tuple[int, list[str]]] = []
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if in_string:
            if ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]":
            if not stack:
                continue
            stack.pop()
            if stack:
                cut_points.append((i + 1, list(stack)))
        elif ch == "," and stack:
            cut_points.append((i, list(stack)))

    # 从后往前找第一个能补齐后解析成功的位置
    for pos, pending in reversed(cut_points):
        candidate = text[:pos].rstrip().rstrip(",").rstrip()
        if not candidate:
            continue
        if candidate.endswith((":", ",")):
            continue
        repaired = candidate + "".join(reversed(pending))
        if _try_loads(repaired) is not None:
            return repaired
    return None


def parse_json_response_verbose(raw: str | None) -> tuple[dict | None, dict]:
    """解析 LLM 返回的 JSON，并给出诊断信息。

    Returns:
        (解析结果或 None, info)。info 字段：
        - ``strategy``: 命中的解析策略名（未命中为 ""）
        - ``truncated``: 是否通过截断修复才解析成功
    """
    info: dict = {"strategy": "", "truncated": False}
    if not raw or not raw.strip():
        return None, info
    text = raw.strip()

    # 1. 直接解析
    obj = _try_loads(text)
    if obj is not None:
        info["strategy"] = "direct"
        return obj, info

    # 2. ```json ... ``` 或 ``` ... ```
    for pattern in [r'```json\s*\n?(.*?)\n?```', r'```\s*\n?(.*?)\n?```']:
        m = re.search(pattern, text, re.DOTALL)
        if m:
            obj = _try_loads(m.group(1).strip())
            if obj is not None:
                info["strategy"] = "codeblock"
                return obj, info

    # 3. 提取 { ... }
    json_str = _extract_braces(text)
    if json_str:
        obj = _try_loads(json_str)
        if obj is not None:
            info["strategy"] = "braces"
            return obj, info

        # 3b. 清洗未转义换行符（LLM 常见错误）
        try:
            cleaned = re.sub(r'(?<!\\)"\s*\n\s*', r'\\n', json_str)
            cleaned = re.sub(r'(?<!\\)\n\s*"', r'\\n"', cleaned)
            obj = _try_loads(cleaned)
            if obj is not None:
                info["strategy"] = "newline"
                return obj, info
        except Exception:
            pass

        # 3c. 字符串内裸控制字符 + 尾随逗号
        try:
            cleaned = _strip_trailing_commas(_escape_raw_control_chars(json_str))
            obj = _try_loads(cleaned)
            if obj is not None:
                info["strategy"] = "control_chars"
                return obj, info
        except Exception:
            pass

    # 4. 中文全角花括号替换后重试
    try:
        alt = text.replace('\uff5b', '{').replace('\uff5d', '}')
        alt_json = _extract_braces(alt)
        if alt_json:
            obj = _try_loads(_strip_trailing_commas(alt_json))
            if obj is not None:
                info["strategy"] = "fullwidth"
                return obj, info
    except Exception:
        pass

    # 5. 截断修复（输出撞上长度上限，JSON 从中间断开）
    try:
        repaired = _repair_truncated(json_str or text)
        if repaired:
            obj = _try_loads(repaired)
            if obj is not None:
                info["strategy"] = "truncated"
                info["truncated"] = True
                return obj, info
    except Exception:
        pass

    return None, info


def parse_json_response(raw: str | None) -> dict | None:
    """解析 LLM 返回的 JSON，成功返回 dict，失败返回 None。

    顶层不是 JSON 对象（数组/字符串/数字）时同样返回 None：
    所有调用方都按 dict 使用，返回其它类型会在 ``"error" in result``
    或 ``result.get`` 处产生更晦涩的崩溃。
    """
    result, _info = parse_json_response_verbose(raw)
    return result


def dump_failed_response(tag: str, raw: str) -> str:
    """把解析失败的 LLM 原文追加写入日志目录，供事后诊断。

    弹窗只显示前 200 字符，无法据此定位问题（截断？未转义字符？），
    完整原文落盘后可直接比对。

    Returns:
        日志文件路径（写入失败时返回空串，绝不抛异常）。
    """
    try:
        import datetime
        from ..utils.config import get_log_dir
        f = get_log_dir() / "diagnostics.log"
        stamp = datetime.datetime.now().isoformat(timespec="seconds")
        with open(f, "a", encoding="utf-8") as fh:
            fh.write(f"\n===== {stamp} | {tag} | {len(raw or '')} chars =====\n")
            fh.write(raw or "")
            fh.write("\n")
        return str(f)
    except Exception:
        return ""
