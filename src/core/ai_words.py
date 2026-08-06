"""AI 味禁词表 —— 本地零 LLM 检测机械化表达（用于编辑器实时标黄）。

英文表来自 awesome-ai-research-writing 的「去 AI 味」prompt 附带的单词清单，
中文表为常见 AI 生成痕迹表达。仅作提示，不代替人工判断。
"""

from __future__ import annotations

import re

EN_WORDS: list[str] = [
    "Accentuate", "Ador", "Amass", "Ameliorate", "Amplify", "Alleviate",
    "Ascertain", "Advocate", "Articulate", "Bear", "Bolster", "Bustling",
    "Cherish", "Conceptualize", "Conjecture", "Consolidate", "Convey",
    "Culminate", "Decipher", "Demonstrate", "Depict", "Devise", "Delineate",
    "Delve", "Delve Into", "Diverge", "Disseminate", "Elucidate", "Endeavor",
    "Engage", "Enumerate", "Envision", "Enduring", "Exacerbate", "Expedite",
    "Foster", "Galvanize", "Harmonize", "Hone", "Innovate", "Inscription",
    "Integrate", "Interpolate", "Intricate", "Lasting", "Leverage", "Manifest",
    "Mediate", "Nurture", "Nuance", "Nuanced", "Obscure", "Opt", "Originates",
    "Perceive", "Perpetuate", "Permeate", "Pivotal", "Ponder", "Prescribe",
    "Prevailing", "Profound", "Recapitulate", "Reconcile", "Rectify",
    "Rekindle", "Reimagine", "Scrutinize", "Substantiate", "Tailor",
    "Testament", "Transcend", "Traverse", "Underscore", "Unveil", "Vibrant",
]

# 常用多词 AI 味搭配（在 EN_WORDS 之外单独匹配整短语）
EN_PHRASES: list[str] = [
    "first and foremost",
    "it is worth noting that",
    "it is worth mentioning that",
    "plays a pivotal role",
    "in the realm of",
    "a testament to",
    "delve into",
]

CN_WORDS: list[str] = [
    "毋庸置疑", "耦合内聚", "不可磨灭的贡献", "范式转移", "颠覆性",
    "切中要害", "痛点", "令人惊叹", "赋能", "彰显", "综上所述",
    "由此可见", "值得注意的是", "值得一提的是", "亟待解决", "至关重要",
    "卓越", "显著提升", "巨大潜力", "深刻洞察", "本质而言", "毋庸置疑地",
]

_EN_PATTERNS = [
    re.compile(r"\b" + re.escape(w.lower()) + r"(?:s|es|ed|d|ing|ly)?\b")
    for w in EN_WORDS
]
_EN_PHRASE_PATTERNS = [re.compile(re.escape(p)) for p in EN_PHRASES]


def match_ai_words(text: str) -> list[tuple[int, int]]:
    """返回文本中所有疑似 AI 味词/短语的 [start, end) 区间（已合并重叠）。

    Args:
        text: 待检测文本。

    Returns:
        排序且不重叠的 (start, end) 列表。
    """
    if not text:
        return []
    low = text.lower()
    hits: list[tuple[int, int]] = []

    for pat in _EN_PATTERNS:
        for m in pat.finditer(low):
            hits.append((m.start(), m.end()))
    for pat in _EN_PHRASE_PATTERNS:
        for m in pat.finditer(low):
            hits.append((m.start(), m.end()))
    for w in CN_WORDS:
        start = 0
        while True:
            idx = text.find(w, start)
            if idx < 0:
                break
            hits.append((idx, idx + len(w)))
            start = idx + 1

    hits.sort()
    merged: list[tuple[int, int]] = []
    for s, e in hits:
        if merged and s < merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged
