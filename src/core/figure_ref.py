"""图表引用解析：把提问文本里的「图1 / 图一 / Figure 2 / Fig. 3A / 表2」
解析成结构化引用，并在结构化文档的图表清单中定位对应截图。

用途：阅读工作台聊天框直接问「图一讲了什么」时自动附图——此前必须在
图片卡片下方点「解读图片」再输入问题，模型才看得到图。

纯逻辑、无 Qt、无网络，便于自测。
"""

from __future__ import annotations

import re

# 中文数字（支持到 99：十、十一、二十三……）
_CN_DIGITS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5,
              "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}
# 「试图/插图/拼图/地图…」里的图不是图表引用，负向前瞻剔除高频误报词
_CN_DENY = "试插拼地版绘意蓝识配"

# 中文引用：图表 + 编号（阿拉伯或中文数字），可带区间（图1-3）
_REF_CN = re.compile(
    r"(?<![" + _CN_DENY + r"])"
    r"([图表])\s*"
    r"([Ss]?[0-9]{1,3}|[一二三四五六七八九十]{1,3})"
    r"(?:\s*[-–~到至]\s*([0-9]{1,3}))?"
)
# 英文引用：Figure(s)/Fig(s)./Table(s) + 编号，可带区间与 and/、并列编号
_REF_EN = re.compile(
    r"\b(Figures?|Figs?\.?|Tables?)\s*([Ss]?[0-9]{1,3})"
    r"(?:\s*[-–~]\s*([0-9]{1,3}))?"
    r"(?:\s*(?:and|和|与|、|,|，)\s*([Ss]?[0-9]{1,3}))?",
    re.IGNORECASE,
)
# 图注开头的标签：Figure 1. / Fig. 2: / 图1 / Table 3 / 表S2
_CAPTION_LABEL = re.compile(r"^\s*(Figure|Fig\.?|Table|图|表)\s*\.?\s*([Ss]?[0-9]{1,3})",
                            re.IGNORECASE)

MAX_RANGE_SPAN = 6        # 区间最多展开 6 个编号，防「图1-100」类误写
MAX_ATTACH_IMAGES = 3     # 单次最多附图数（控制多模态消息体积）


def _cn_to_int(s: str) -> int | None:
    """中文数字（≤99）转 int；无法解析返回 None。"""
    if not s:
        return None
    if s in _CN_DIGITS:
        return _CN_DIGITS[s]
    m = re.fullmatch(r"([一二三四五六七八九]?)十([一二三四五六七八九]?)", s)
    if m:
        tens = _CN_DIGITS[m.group(1)] if m.group(1) else 1
        ones = _CN_DIGITS[m.group(2)] if m.group(2) else 0
        return tens * 10 + ones
    return None


def _norm_num(raw: str) -> str | None:
    """编号归一化：中文数字 → 阿拉伯；去前导零；保留 S 前缀（小写）。"""
    s = (raw or "").strip()
    if not s:
        return None
    supp = s[:1] in ("S", "s")
    body = s[1:] if supp else s
    if body.isdigit():
        n = int(body)
    else:
        n = _cn_to_int(body)
        if n is None:
            return None
    return f"s{n}" if supp else str(n)


def _kind_of_cn(ch: str) -> str:
    return "figure" if ch == "图" else "table"


def _kind_of_label(label: str) -> str:
    if label in ("图", "表"):
        return _kind_of_cn(label)
    return "table" if label.lower().startswith("tab") else "figure"


def _display(kind: str, token: str) -> str:
    label = "图" if kind == "figure" else "表"
    t = token[1:] if token.startswith("s") else token
    return f"{label}S{t}" if token.startswith("s") else f"{label}{t}"


def parse_figure_refs(text: str) -> list[tuple[str, str, str]]:
    """提取文本中的图表引用。

    Returns:
        [(kind, token, display)]，kind = "figure"|"table"，token 归一化编号
        （"1"、"s1"），display 为展示用标签（"图1"、"表S2"）；按出现顺序去重。
    """
    out: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()

    def _add(kind: str, token: str | None) -> None:
        if not token or (kind, token) in seen:
            return
        seen.add((kind, token))
        out.append((kind, token, _display(kind, token)))

    def _add_range(kind: str, start_raw: str, end_raw: str | None) -> None:
        start = _norm_num(start_raw)
        if start is None:
            return
        _add(kind, start)
        if not end_raw or start.startswith("s"):
            return  # 补充材料编号不做区间展开
        try:
            lo, hi = int(start), int(end_raw)
        except (TypeError, ValueError):
            return
        if hi > lo and hi - lo < MAX_RANGE_SPAN:
            for n in range(lo + 1, hi + 1):
                _add(kind, str(n))

    for m in _REF_CN.finditer(text):
        _add_range(_kind_of_cn(m.group(1)), m.group(2), m.group(3))
    for m in _REF_EN.finditer(text):
        kind = _kind_of_label(m.group(1))
        _add_range(kind, m.group(2), m.group(3))
        if m.group(4):
            _add(kind, _norm_num(m.group(4)))
    return out


def caption_token(kind: str, caption: str) -> str | None:
    """从图注开头解析编号标签；图注不以 Figure/Fig/图/Table/表 开头返回 None。

    kind 不符（把表注当图）同样返回 None。
    """
    m = _CAPTION_LABEL.match(caption or "")
    if not m or _kind_of_label(m.group(1)) != kind:
        return None
    return _norm_num(m.group(2))


def match_figure_entries(
        refs: list[tuple[str, str, str]],
        entries: list[dict],
        limit: int = MAX_ATTACH_IMAGES,
) -> tuple[list[dict], list[str]]:
    """把引用匹配到文档图表条目上。

    Args:
        refs: parse_figure_refs 的结果。
        entries: [{"kind": "figure"|"table", "caption": str, "path": str}]，
                 按文档顺序。
        limit: 最多返回条目数（附图体积控制）。

    Returns:
        (matched, missed)：matched 为去重后的条目（附 display 标签）；
        missed 为未找到对应图表的引用展示标签（供状态栏提示）。
    """
    by_token: dict[tuple[str, str], dict] = {}
    for e in entries:
        if not e.get("path"):
            continue
        tok = caption_token(e.get("kind", "figure"), e.get("caption", ""))
        if tok and (e["kind"], tok) not in by_token:
            by_token[(e["kind"], tok)] = e
    matched: list[dict] = []
    taken: set[int] = set()
    missed: list[str] = []
    for kind, token, display in refs:
        e = by_token.get((kind, token))
        if e is None:
            missed.append(display)
            continue
        if id(e) in taken:
            continue
        taken.add(id(e))
        item = dict(e)
        item["display"] = display
        matched.append(item)
    if len(matched) > limit:
        for item in matched[limit:]:
            missed.append(f"{item['display']}（超出附图上限）")
        matched = matched[:limit]
    return matched, missed
