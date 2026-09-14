"""Word (.docx) 读写核心 v2 —— run 级最小侵入写回 + 段落对齐 + 修订写回。

设计原则：
- docx 原件是格式真相源；编辑器里是纯文本工作视图
- 写回 = 最小侵入补丁：
  * 文本未变的段落一个字节都不动（既有修订/引用域/格式天然保留）
  * 变了的段落按字符级 diff 在 run 粒度重组：匹配片段按原 run 的字符
    格式（rPr）重建，未匹配片段替换为新文本；批注锚点/书签/脚注引用/
    行内图片/超链接/域指令原位保留
- 段落数量/顺序按文本相似度对齐（SequenceMatcher），中间插段不再导致
  样式错位；新增段落继承相邻段落样式
- 修订模式（track_changes=True）：删除文本包 w:del（w:t→w:delText）、
  插入文本包 w:ins（author=PaperWB），Word 中可逐处接受/拒绝；域结果区
  与容器（超链接等）内的改动退化为整段 del+ins，保证 OOXML 合法
"""

from __future__ import annotations

import difflib
import re
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn


@dataclass
class DocxComment:
    """一条审阅批注。"""

    comment_id: str = ""
    author: str = ""
    date: str = ""
    text: str = ""
    anchor_text: str = ""      # 被批注的原文片段（尽力提取）
    paragraph_index: int = -1  # 锚定段落（0 基；-1 = 未锚定）
    char_start: int = -1       # 段内字符偏移（批注起点；-1 = 未解析）
    char_end: int = -1         # 段内字符偏移（批注终点；-1 = 未解析）

    def to_dict(self) -> dict:
        return {
            "comment_id": self.comment_id, "author": self.author,
            "date": self.date, "text": self.text,
            "anchor_text": self.anchor_text,
            "paragraph_index": self.paragraph_index,
            "char_start": self.char_start,
            "char_end": self.char_end,
        }


@dataclass
class DocxContent:
    """读入的 Word 文档内容。"""

    paragraphs: list[str] = field(default_factory=list)   # 段落文本（\n 分隔）
    styles: list[str] = field(default_factory=list)       # 每段样式名（与 paragraphs 对齐）
    comments: list[DocxComment] = field(default_factory=list)
    has_revisions: bool = False                            # 存在修订（track changes）
    path: str = ""
    # 每段引文标记的精确区间 [(start, end, 文本)]（段内偏移；上标/引用域
    # 结果中的纯编号）。供润色文本保护定位；写回侧直接从段落 XML 识别。
    citation_tokens: list[list[tuple[int, int, str]]] = field(default_factory=list)

    def to_plain_text(self) -> str:
        return "\n".join(self.paragraphs)

    def all_citation_tokens(self, offset_of_para: list[int] | None = None
                            ) -> list[tuple[int, int, str]]:
        """全文档引文标记，换算成全文坐标（offset_of_para 为每段起始偏移）。"""
        out: list[tuple[int, int, str]] = []
        for pi, toks in enumerate(self.citation_tokens):
            base = offset_of_para[pi] if offset_of_para else 0
            for s, e, t in toks:
                out.append((base + s, base + e, t))
        return out


def _para_text(p) -> str:
    """提取段落纯文本（含修订合并：w:ins 计入、w:del 不计入）。"""
    parts: list[str] = []
    for node in p._p.iter():
        tag = node.tag
        if tag == qn("w:t"):
            parts.append(node.text or "")
        elif tag == qn("w:tab"):
            parts.append("\t")
        elif tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
        elif tag == qn("w:delText"):
            pass  # 已删除文本不显示
    return "".join(parts)


def _para_has_revision(p) -> bool:
    """段落是否含修订标记（w:ins / w:del）。"""
    return (p._p.find(qn("w:ins")) is not None
            or p._p.find(qn("w:del")) is not None)


def _para_style_name(p) -> str:
    try:
        return p.style.name if p.style is not None else ""
    except Exception:  # noqa: BLE001
        return ""


def read_docx(path: str | Path) -> DocxContent:
    """读取 .docx：段落文本 + 样式名 + 批注 + 修订标记 + 引文标记区间。"""
    path = str(path)
    doc = Document(path)
    content = DocxContent(path=path)

    for p in doc.paragraphs:
        content.paragraphs.append(_para_text(p))
        content.styles.append(_para_style_name(p))
        if _para_has_revision(p):
            content.has_revisions = True
        # 引文标记区间：连续上标/域结果 span 合并后是纯编号的片段
        toks: list[tuple[int, int, str]] = []
        idx = _index_paragraph(p)
        buf_start = -1
        buf = ""
        for sp in idx.spans:
            if sp.superscript or sp.in_field_result:
                if buf_start < 0:
                    buf_start = sp.start
                buf += _run_text(sp.run)
                continue
            if buf and citation_key(buf) is not None:
                toks.append((buf_start, buf_start + len(buf), buf))
            buf_start = -1
            buf = ""
        if buf and citation_key(buf) is not None:
            toks.append((buf_start, buf_start + len(buf), buf))
        content.citation_tokens.append(toks)

    content.comments = _parse_comments(doc, content.paragraphs)
    return content


def _parse_comments(doc, paragraphs: list[str]) -> list[DocxComment]:
    """解析审阅批注（comments.xml + 段落 commentRangeStart/End 锚定）。

    字符级偏移：按段落 XML 子节点顺序累积 w:t 文本长度，在
    commentRangeStart/End 处记录段内偏移（与 _para_text 同一遍历口径）。
    """
    comments_part = _get_comments_part(doc)
    if comments_part is None:
        return []

    comments_xml = comments_part._element
    by_id: dict[str, dict] = {}
    for c in comments_xml.findall(qn("w:comment")):
        cid = c.get(qn("w:id"), "")
        author = c.get(qn("w:author"), "")
        date = c.get(qn("w:date"), "")
        text = "".join(t.text or "" for t in c.iter(qn("w:t")))
        by_id[cid] = {"author": author, "date": date, "text": text}

    if not by_id:
        return []

    # 段落级锚定 + 字符级偏移：遍历每段 XML，累积文本长度
    spans: dict[str, tuple[int, int, int]] = {}  # cid -> (para_idx, char_start, char_end)
    for pi, p in enumerate(doc.paragraphs):
        offset = 0
        current_start: dict[str, int] = {}
        for node in p._p.iter():
            tag = node.tag
            if tag == qn("w:commentRangeStart"):
                cid = node.get(qn("w:id"), "")
                if cid:
                    current_start[cid] = offset
            elif tag == qn("w:commentRangeEnd"):
                cid = node.get(qn("w:id"), "")
                if cid and cid in current_start:
                    spans[cid] = (pi, current_start[cid], offset)
                    current_start.pop(cid)
            elif tag == qn("w:t"):
                offset += len(node.text or "")
            elif tag == qn("w:delText"):
                pass  # 与 _para_text 同口径：已删除文本不计入
        # 未闭合的批注（仅 commentRangeStart）：锚定到段尾
        for cid, start in current_start.items():
            if cid not in spans:
                spans[cid] = (pi, start, offset)

    comments: list[DocxComment] = []
    for cid, info in by_id.items():
        span = spans.get(cid)
        if span is not None:
            pi, cs, ce = span
        else:
            # 兜底：仅段落级（老文档可能只有 commentReference 无 Range）
            pi = -1
            cs = ce = -1
            for pi2, p in enumerate(doc.paragraphs):
                for node in p._p.iter():
                    if node.tag == qn("w:commentReference"):
                        if node.get(qn("w:id")) == cid:
                            pi = pi2
                            break
                if pi >= 0:
                    break
        anchor = paragraphs[pi] if 0 <= pi < len(paragraphs) else ""
        # 锚定文本：优先取被批注的精确片段，其次截取段落前 60 字符
        if 0 <= cs < ce <= len(anchor):
            anchor_text = anchor[cs:ce][:60]
        else:
            anchor_text = anchor[:60]
        comments.append(DocxComment(
            comment_id=cid,
            author=info["author"],
            date=info["date"],
            text=info["text"],
            anchor_text=anchor_text,
            paragraph_index=pi,
            char_start=cs if 0 <= cs < ce else -1,
            char_end=ce if 0 <= cs < ce else -1,
        ))
    return comments


def _get_comments_part(doc):
    """获取 comments part（关系挂在 document part 上，非 package 级）。"""
    try:
        return doc.part.part_related_by(
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/comments"
        )
    except (KeyError, ValueError):
        return None


# ==================================================================
# v2 写回内核：段落索引 / run 级合并 / 修订写回
# ==================================================================

# 段落级零宽锚点：必须原位保留、不参与文本合并的元素
_ANCHOR_TAGS = frozenset({
    qn("w:bookmarkStart"), qn("w:bookmarkEnd"),
    qn("w:commentRangeStart"), qn("w:commentRangeEnd"),
    qn("w:proofErr"), qn("w:permStart"), qn("w:permEnd"),
})
_START_ANCHOR_TAGS = frozenset({
    qn("w:commentRangeStart"), qn("w:bookmarkStart"), qn("w:permStart"),
})
# 透明容器：其内部 run 递归索引（超链接/内容控件/智能标记等）
_CONTAINER_TAGS = frozenset({
    qn("w:hyperlink"), qn("w:sdt"), qn("w:smartTag"), qn("w:customXml"),
})
# 文本 run 被删除时需抢救到新 run 的零宽内容
_ANCHOR_CONTENT_TAGS = frozenset({
    qn("w:footnoteReference"), qn("w:endnoteReference"),
    qn("w:commentReference"), qn("w:drawing"), qn("w:object"),
})

# ------------------------------------------------------------------
# 引文标记保护：Word 文档里的引文编号通常是「上标数字」（手动格式）或
# Zotero 引用域的域结果。编辑器是纯文本视图，AI 润色常把它改写成
# "[1]" 之类的方括号样式 —— 写回后上标格式就丢了。这里给出统一口径：
# 识别"引文样式"片段（上标 run 或域结果区）中的纯编号文本，写回时
# 数字序列没变的格式改写一律还原为原文（连 run 都不动）。
# ------------------------------------------------------------------
_CITE_STRIP_RE = re.compile(r"^[\[\(（〔【]?([0-9]{1,3}(?:\s*[,，;；\-–—]\s*[0-9]{1,3}){0,8})[\]\)）〕】]?$")
_CITE_CORE_RE = re.compile(r"^[0-9]{1,3}(?:\s*[,，;；\-–—]\s*[0-9]{1,3}){0,8}$")


def citation_key(text: str) -> str | None:
    """把引文标记文本规范化为数字序列键；非引文样式文本返回 None。

    "1" / "[1]" / "6,7" / "[6-9]" / "（10,11）" → "1" / "1" / "6,7" / "6-9" / "10,11"；
    分隔符统一成半角逗号/连字符，空白剔除。
    """
    s = (text or "").strip()
    if not s or len(s) > 20:
        return None
    m = _CITE_STRIP_RE.match(s)
    core = m.group(1) if m else (_CITE_CORE_RE.match(s).group(0) if _CITE_CORE_RE.match(s) else None)
    if core is None:
        return None
    core = core.replace("，", ",").replace("；", ";").replace("；", ",")
    core = core.replace(";", ",").replace("–", "-").replace("—", "-")
    core = re.sub(r"\s*", "", core)
    parts = [p.strip() for p in core.split(",")]
    norm: list[str] = []
    for p in parts:
        if "-" in p:
            a, _, b = p.partition("-")
            if not (a.isdigit() and b.isdigit()):
                return None
            norm.append(f"{int(a)}-{int(b)}")
        elif p.isdigit():
            norm.append(str(int(p)))
        else:
            return None
    return ",".join(norm)


def _rpr_is_superscript(rpr) -> bool:
    """rPr 是否为上标（w:vertAlign val="superscript"）。"""
    if rpr is None:
        return False
    va = rpr.find(qn("w:vertAlign"))
    return va is not None and va.get(qn("w:val")) == "superscript"


_CITE_WINDOW_MAX_EVENTS = 8   # 格式改写窗口最多跨越的事件数
_CITE_WINDOW_MAX_CHARS = 40   # 窗口新文本长度上限（防误扫长句）


def protect_citations_in_text(original: str, polished: str,
                              token_spans: list[tuple[int, int, str]]) -> str:
    """文本级引文保护：撤销 polished 对已知引文标记的纯格式改写。

    token_spans 是 original 坐标系内的引文标记精确区间 [(start, end, 文本)]
    （来自绑定 docx 的上标/域结果识别）。AI 给上标编号套上方括号（1→[1]）、
    或把 "[1]" 裸化时，polished 会被还原为原文标记；引文编号真实改动
    （1→2）、全新的引用不受影响。
    """
    if not token_spans or not original or polished == original:
        return polished
    sm = difflib.SequenceMatcher(None, original, polished, autojunk=False)
    ops = sm.get_opcodes()
    edits: list[tuple[int, int, str]] = []  # (polished起, polished止, 替换文本)

    for ts, te, tok in token_spans:
        covering = [n for n, op in enumerate(ops) if op[1] < te and op[2] > ts]
        if not covering:
            continue
        n0, n1 = covering[0], covering[-1]
        # 窗口向外吸收相邻 insert（AI 新插入的配对方括号落在标记两侧）
        while n0 > 0 and ops[n0 - 1][0] == "insert":
            n0 -= 1
        while n1 + 1 < len(ops) and ops[n1 + 1][0] == "insert":
            n1 += 1
        first, last = ops[n0], ops[n1]
        old_combined = original[first[1]:last[2]]
        new_combined = polished[first[3]:last[4]]
        if old_combined != tok or new_combined == tok:
            continue
        if len(new_combined) <= _CITE_WINDOW_MAX_CHARS \
                and citation_key(new_combined) == citation_key(tok) \
                and citation_key(tok) is not None:
            edits.append((first[3], last[4], tok))

    if not edits:
        return polished
    out: list[str] = []
    pos = 0
    for a, b, repl in sorted(edits):
        if a < pos:
            continue  # 与上一个窗口重叠：放弃这条（保号优先）
        out.append(polished[pos:a])
        out.append(repl)
        pos = b
    out.append(polished[pos:])
    return "".join(out)


@dataclass
class _Span:
    """一段连续可见文本（一个 w:r 承载）。"""

    run: object                    # 所属 w:r 元素
    container: object | None       # 所在透明容器（None = 段落直接子级）
    start: int                     # 段内文本起偏移
    end: int
    rpr: object | None             # run 的字符格式（深拷贝，供重建片段）
    in_field_result: bool = False  # 位于 fldChar separate…end 的域结果区
    superscript: bool = False      # rPr 为上标（引文编号常见样式）


@dataclass
class _ParaIndex:
    spans: list[_Span] = field(default_factory=list)
    anchors: list = field(default_factory=list)      # [(offset, 段落级锚点元素)]
    anchor_runs: list = field(default_factory=list)  # [(offset, 零宽内容 run)]
    has_revision: bool = False
    text: str = ""


def _run_text(r) -> str:
    """run 的可见文本（w:t + w:tab→\t + w:br/cr→\n），与 _para_text 同口径。"""
    parts: list[str] = []
    for node in r.iter():
        tag = node.tag
        if tag == qn("w:t"):
            parts.append(node.text or "")
        elif tag == qn("w:tab"):
            parts.append("\t")
        elif tag in (qn("w:br"), qn("w:cr")):
            parts.append("\n")
    return "".join(parts)


def _index_paragraph(p) -> _ParaIndex:
    """按文档顺序索引段落：文本 span / 零宽锚点 / 域状态 / 修订检测。

    span 按偏移连续铺满 idx.text，与 _para_text 同一遍历口径。
    """
    idx = _ParaIndex(has_revision=_para_has_revision(p))
    state = {"offset": 0, "field": 0}  # field: 0 指令外 / 1 指令内 / 2 结果区

    def _visit_run(r, container):
        fld = r.find(qn("w:fldChar"))
        if fld is not None:
            ft = fld.get(qn("w:fldCharType"))
            if ft == "begin":
                state["field"] = 1
            elif ft == "separate":
                state["field"] = 2
            elif ft == "end":
                state["field"] = 0
            idx.anchor_runs.append((state["offset"], r))
            return
        if r.find(qn("w:instrText")) is not None:
            idx.anchor_runs.append((state["offset"], r))
            return
        text = _run_text(r)
        if text:
            rpr = r.find(qn("w:rPr"))
            idx.spans.append(_Span(
                run=r, container=container,
                start=state["offset"], end=state["offset"] + len(text),
                rpr=deepcopy(rpr) if rpr is not None else None,
                in_field_result=(state["field"] == 2),
                superscript=_rpr_is_superscript(rpr),
            ))
            state["offset"] += len(text)
            return
        # 零宽 run：脚注引用/批注引用/行内图片等
        for node in r.iter():
            if node.tag in _ANCHOR_CONTENT_TAGS:
                idx.anchor_runs.append((state["offset"], r))
                return

    def _visit_runs_of(elem, container):
        saved = state["field"]
        for r in elem.iter(qn("w:r")):
            _visit_run(r, container)
        state["field"] = saved

    def _visit(elem, container=None):
        for child in elem:
            tag = child.tag
            if tag == qn("w:pPr"):
                continue
            if tag in _ANCHOR_TAGS:
                idx.anchors.append((state["offset"], child))
            elif tag == qn("w:del"):
                idx.has_revision = True  # 已删除文本不计入，可整段跳过
            elif tag == qn("w:ins"):
                idx.has_revision = True
                _visit_runs_of(child, container)
            elif tag in _CONTAINER_TAGS:
                _visit_runs_of(child, child)
            elif tag == qn("w:r"):
                _visit_run(child, container)
            else:
                idx.anchors.append((state["offset"], child))  # 未知元素保守保留

    _visit(p._p)
    idx.text = "".join(_run_text(sp.run) for sp in idx.spans)
    return idx


class _Reviser:
    """修订标记工厂：w:ins / w:del 共享 author 与递增 w:id。"""

    def __init__(self, author: str = "PaperWB"):
        self.author = author
        self.date = datetime.now().strftime("%Y-%m-%dT%H:%M:%SZ")
        self._id = 9000

    def _make(self, tag: str):
        el = OxmlElement(tag)
        el.set(qn("w:id"), str(self._id))
        el.set(qn("w:author"), self.author)
        el.set(qn("w:date"), self.date)
        self._id += 1
        return el

    def new_ins(self):
        return self._make("w:ins")

    def new_del(self):
        return self._make("w:del")


def _split_text_pieces(text: str) -> list[tuple[str, str]]:
    """把文本切分为 (内容, 类型) 序列，类型 text/br/tab，供 run 内容重建。"""
    out: list[tuple[str, str]] = []
    buf = ""
    for ch in text:
        if ch == "\n":
            if buf:
                out.append((buf, "text"))
                buf = ""
            out.append(("", "br"))
        elif ch == "\t":
            if buf:
                out.append((buf, "text"))
                buf = ""
            out.append(("", "tab"))
        else:
            buf += ch
    if buf:
        out.append((buf, "text"))
    return out


def _new_run(p_el, text: str, rpr_template):
    """按模板 rPr 新建 run；\n 转 w:br、\t 转 w:tab。空文本返回 None。"""
    pieces = _split_text_pieces(text)
    if not pieces:
        return None
    r = OxmlElement("w:r")
    if rpr_template is not None:
        r.append(deepcopy(rpr_template))
    for content, kind in pieces:
        if kind == "text":
            t = OxmlElement("w:t")
            t.set(qn("xml:space"), "preserve")
            t.text = content
            r.append(t)
        elif kind == "br":
            r.append(OxmlElement("w:br"))
        else:
            r.append(OxmlElement("w:tab"))
    return r


def _run_to_deltext(node) -> None:
    """节点内所有 w:t 改名 w:delText（修订删除语义）。"""
    for t in node.iter(qn("w:t")):
        t.tag = qn("w:delText")
        t.set(qn("xml:space"), "preserve")


def _rescue_and_remove(run) -> None:
    """删除文本 run，但把其中的脚注引用/图片/批注引用等零宽内容保留下来。"""
    parent = run.getparent()
    if parent is None:
        return
    keep = [node for node in list(run) if node.tag in _ANCHOR_CONTENT_TAGS]
    if keep:
        new_r = OxmlElement("w:r")
        rpr = run.find(qn("w:rPr"))
        if rpr is not None:
            new_r.append(deepcopy(rpr))
        for node in keep:
            new_r.append(node)
        parent.insert(parent.index(run), new_r)
    parent.remove(run)


def _unwrap_revisions(p_el) -> None:
    """接受段落既有修订：w:ins 拆包保留内容，w:del 整体移除。"""
    for child in list(p_el):
        if child.tag == qn("w:ins"):
            pos = list(p_el).index(child)
            for node in list(child):
                child.remove(node)
                p_el.insert(pos, node)
                pos += 1
            p_el.remove(child)
        elif child.tag == qn("w:del"):
            p_el.remove(child)


def _content_start_index(p_el, idx: _ParaIndex) -> int:
    """段落内容起点：pPr 与前导零宽锚点之后。"""
    i = 0
    if len(p_el) and p_el[0].tag == qn("w:pPr"):
        i = 1
    lead = {el for off, el in idx.anchors if off == 0}
    lead |= {el for off, el in idx.anchor_runs if off == 0}
    while i < len(p_el) and p_el[i] in lead:
        i += 1
    return i


def _span_at(spans, pos: int):
    for sp in spans:
        if sp.start <= pos < sp.end:
            return sp
    return None


def _repair_comment_ranges(p_el) -> None:
    """修复合并后退化的批注范围（起止之间已无正文）→ 重锚到整段内容。

    仍包住正文的范围保持原位（精确定位）；只有空范围才重锚。
    """
    starts = [c for c in p_el if c.tag == qn("w:commentRangeStart")]
    ends = [c for c in p_el if c.tag == qn("w:commentRangeEnd")]
    if not starts or not ends:
        return

    def _text_len_between(a, b) -> int:
        n = 0
        seen = False
        for child in p_el:
            if child is a:
                seen = True
                continue
            if child is b:
                break
            if seen:
                for t in child.iter(qn("w:t")):
                    n += len(t.text or "")
        return n

    ppr = p_el.find(qn("w:pPr"))
    for s_el in starts:
        cid = s_el.get(qn("w:id"))
        e_el = next((x for x in ends if x.get(qn("w:id")) == cid), None)
        if e_el is None:
            continue
        if _text_len_between(s_el, e_el) > 0:
            continue  # 范围仍包住正文：保留精确锚定
        p_el.remove(s_el)
        p_el.remove(e_el)
        ppr.addnext(s_el) if ppr is not None else p_el.insert(0, s_el)
        p_el.append(e_el)


def _window_covering_citation_style(idx: _ParaIndex, pieces: list[list]) -> bool:
    """窗口内所有旧文本区域是否都被引文样式 span（上标/域结果）完全覆盖。"""
    regions = [(s, e) for _kind, _sp, s, e, _nf in pieces if e > s]
    if not regions:
        return False
    for s, e in regions:
        covering = [sp for sp in idx.spans if sp.start < e and sp.end > s]
        if not covering or covering[0].start > s or covering[-1].end < e:
            return False
        if not all(sp.superscript or sp.in_field_result for sp in covering):
            return False
    return True


def _frags_for_regions(idx: _ParaIndex, pieces: list[list]) -> list[list]:
    """把窗口的旧文本区域按 span 铺成 frag 事件（原 run 原样保留）。"""
    frags: list[list] = []
    for _kind, _sp, s, e, _nf in pieces:
        if e <= s:
            continue
        pos = s
        while pos < e:
            sp = _span_at(idx.spans, pos)
            e2 = min(sp.end, e)
            frags.append(["frag", sp, pos, e2, ""])
            pos = e2
    return frags


def _brackets_balanced(s: str) -> bool:
    """括号是否配平（中英文括号混计；用于判定改写窗口已闭合）。"""
    opens = sum(s.count(c) for c in "[(（〔【{")
    closes = sum(s.count(c) for c in "])）〕】}")
    return opens == closes


def _keep_citation_style_events(idx: _ParaIndex,
                                merged: list[list]) -> list[list]:
    """把「引文标记格式改写」的事件窗口还原为原样（原 run 零改动）。

    识别两种形态（键 = 数字序列）：
    - 单个 replace gap：旧 "1" → 新 "[1]"
    - insert-equal-insert 窗口：equal "1" 两侧插入 "[" 与 "]"
    判据：窗口旧文本是纯编号、新旧文本规范化后数字序列一致且括号配平
    （防止 "[1" 处提前命中漏掉闭合括号），旧文本区域全部由上标/域结果
    span 承载。命中则整个窗口回退为原文——上标引文不会因 AI 加方括号
    而丢失格式，引用域结果也不会被改写。
    必须在整段退化判定之前执行，否则域结果区的一处改写会毁掉整段引用域。
    """
    n = len(merged)
    out: list[list] = []
    i = 0
    while i < n:
        ev = merged[i]
        if ev[0] != "gap":
            out.append(ev)
            i += 1
            continue
        pieces = [ev]
        hit = False
        while True:
            old_buf = "".join(idx.text[s:e] for _k, _sp, s, e, _nf in pieces)
            new_buf = "".join(
                (nf if kind == "gap" else idx.text[s:e])
                for kind, _sp, s, e, nf in pieces)
            k_old = citation_key(old_buf)
            if (k_old is not None and old_buf != new_buf
                    and citation_key(new_buf) == k_old
                    and _brackets_balanced(old_buf) and _brackets_balanced(new_buf)
                    and _window_covering_citation_style(idx, pieces)):
                out.extend(_frags_for_regions(idx, pieces))
                i += len(pieces)
                hit = True
                break
            if (i + len(pieces) >= n or len(pieces) >= _CITE_WINDOW_MAX_EVENTS
                    or len(new_buf) > _CITE_WINDOW_MAX_CHARS):
                break
            pieces.append(merged[i + len(pieces)])
        if not hit:
            out.append(ev)
            i += 1
    return out


def _merge_para_text(p, new_text: str, reviser: _Reviser | None,
                     _force: bool = False) -> None:
    """把段落文本合并为 new_text：run 粒度最小侵入。

    reviser 非空 → 删除/插入以修订（w:del/w:ins）写回；
    _force=True → 跳过"段内已有修订"分派（供整段替换兜底递归调用）。
    """
    idx = _index_paragraph(p)
    if idx.text == new_text:
        return  # 未变段落零改动
    if idx.has_revision and not _force:
        # 段内已有修订：精细合并会嵌套修订结构 → 接受后整段处理
        if reviser is not None:
            _replace_para_track(p, new_text, reviser, idx)
        else:
            _replace_para_plain(p, new_text, idx)
        return
    if not idx.spans:
        if new_text:
            r = _new_run(p._p, new_text, None)
            if r is not None:
                if reviser is not None:
                    ins = reviser.new_ins()
                    ins.append(r)
                    r = ins
                p._p.insert(_content_start_index(p._p, idx), r)
        return

    p_el = p._p
    sm = difflib.SequenceMatcher(None, idx.text, new_text, autojunk=False)

    # ---- 事件化：equal→frag / delete·replace·insert→gap ----
    events: list[list] = []  # [kind, span, s, e, new_frag]
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            pos = i1
            while pos < i2:
                sp = _span_at(idx.spans, pos)
                e = min(sp.end, i2)
                events.append(["frag", sp, pos, e, ""])
                pos = e
        elif tag == "insert":
            hint = _span_at(idx.spans, max(i1 - 1, 0))
            events.append(["gap", hint, i1, i1, new_text[j1:j2]])
        else:  # replace / delete
            hint = _span_at(idx.spans, i1)
            events.append(["gap", hint, i1, i2,
                           new_text[j1:j2] if tag == "replace" else ""])
    # 相邻 gap 合并（get_opcodes 可能 delete+insert 相邻）
    merged: list[list] = []
    for ev in events:
        if ev[0] == "gap" and merged and merged[-1][0] == "gap":
            merged[-1][3] = ev[3]
            merged[-1][4] += ev[4]
        else:
            merged.append(ev)

    # 引文标记保护：仅格式改写（1→[1]）的数字序列视为未变，原 run 不动。
    merged = _keep_citation_style_events(idx, merged)

    # 修订模式的边界情况：域结果区/容器内的改动退化为整段 del+ins，
    # 避免 w:del 嵌进超链接等非法结构、或改写引用域结果
    if reviser is not None:
        for ev in merged:
            s, e = ev[2], ev[3]
            if e <= s:
                continue
            if any(sp.start < e and sp.end > s
                   and (sp.in_field_result or sp.container is not None)
                   for sp in idx.spans):
                _replace_para_track(p, new_text, reviser, idx)
                return

    # 整段覆盖且未被切分的 span → 原样保留（零改动）
    frag_count: dict[int, list] = {}
    for ev in merged:
        if ev[0] == "frag":
            frag_count.setdefault(id(ev[1]), []).append((ev[2], ev[3]))

    touched: set[int] = set()
    ops: list[tuple] = []
    for ev in merged:
        if ev[0] == "frag":
            sp = ev[1]
            plist = frag_count[id(sp)]
            if len(plist) == 1 and plist[0] == (sp.start, sp.end):
                ops.append(("orig", sp, 0, 0, ""))
            else:
                touched.add(id(sp))
                ops.append(("frag", sp, ev[2], ev[3], ""))
        else:
            _hint, s, e, nf = ev[1], ev[2], ev[3], ev[4]
            if e > s:
                for sp in idx.spans:
                    if sp.start < e and sp.end > s:
                        touched.add(id(sp))
            ops.append(("gap", _hint, s, e, nf))
    if not touched and not any(op[0] == "gap" and op[4] for op in ops):
        return

    # ---- 执行：按文档顺序重建内容 ----
    prev_elem = None       # 上一个已落位内容元素
    first_moved: list = [] # 无前驱的落位元素（最终插到内容起点）
    del_open = None        # 进行中的 w:del 容器
    del_parent = None
    last_was_del = False
    last_rpr = None

    def _place(el, after):
        if after is not None and after.getparent() is not None:
            after.addnext(el)
        elif after is not None:
            # after 还在待插入队列（未入树）：紧跟其后保持文档顺序
            try:
                first_moved.insert(first_moved.index(after) + 1, el)
            except ValueError:
                first_moved.append(el)
        else:
            first_moved.append(el)

    for kind, sp, s, e, nf in ops:
        # 一律以承载 span 自身的 rPr 重建：frag/orig 保持原片段格式，
        # gap（删除区替换/插入点延续）取 hint span 的格式（替换谁就像谁、
        # 插在前一个 run 后就延续谁）。不再回退 last_rpr —— 否则紧跟
        # 上标引文的普通文字会被误染成上标。
        rpr = sp.rpr if sp is not None else None
        if kind == "orig":
            del_open = None
            last_was_del = False
            prev_elem = sp.run
            last_rpr = sp.rpr if sp.rpr is not None else last_rpr
            continue
        if kind == "frag":
            del_open = None
            last_was_del = False
            r = _new_run(p_el, idx.text[s:e], rpr)
            if r is not None:
                _place(r, prev_elem)
                prev_elem = r
            last_rpr = sp.rpr if sp.rpr is not None else last_rpr
            continue
        # gap：旧 [s,e) 未匹配 → 新文本 nf
        old_frag = idx.text[s:e]
        if reviser is not None and old_frag:
            parent = sp.run.getparent() if sp is not None else p_el
            if del_open is not None and last_was_del and del_parent is parent:
                r = _new_run(p_el, old_frag, rpr)
                if r is not None:
                    _run_to_deltext(r)
                    del_open.append(r)
            else:
                r = _new_run(p_el, old_frag, rpr)
                if r is not None:
                    _run_to_deltext(r)
                    del_el = reviser.new_del()
                    del_el.append(r)
                    _place(del_el, prev_elem)
                    del_open = del_el
                    del_parent = parent
            if del_open is not None:
                prev_elem = del_open
            last_was_del = True
        if nf:
            r = _new_run(p_el, nf, rpr)
            if r is not None:
                if reviser is not None:
                    ins = reviser.new_ins()
                    ins.append(r)
                    r = ins
                _place(r, prev_elem)
                prev_elem = r
            del_open = None  # 插入内容隔断删除组
            last_was_del = False
        if sp is not None and sp.rpr is not None:
            last_rpr = sp.rpr

    insert_at = _content_start_index(p_el, idx)
    for el in reversed(first_moved):
        p_el.insert(insert_at, el)

    # 移除被替换的原 span run（抢救其中的零宽内容）
    for sp in idx.spans:
        if id(sp) in touched:
            _rescue_and_remove(sp.run)
    _repair_comment_ranges(p_el)


def _replace_para_plain(p, new_text: str,
                        idx: _ParaIndex | None = None) -> None:
    """整段直接替换（段内已有修订时的非修订模式兜底）。

    先接受既有修订（w:ins 拆包 / w:del 移除），再走常规合并。
    """
    idx = idx or _index_paragraph(p)
    if idx.text == new_text:
        return
    _unwrap_revisions(p._p)
    _merge_para_text(p, new_text, None, _force=True)


def _replace_para_track(p, new_text: str, reviser: _Reviser,
                        idx: _ParaIndex | None = None) -> None:
    """整段修订替换：旧内容包 w:del（w:t→w:delText），新文本包 w:ins。"""
    idx = idx or _index_paragraph(p)
    if idx.text == new_text:
        return
    p_el = p._p
    rpr = idx.spans[0].rpr if idx.spans else None
    del_el = reviser.new_del()
    for child in list(p_el):
        if child.tag == qn("w:pPr") or child.tag in _ANCHOR_TAGS:
            continue
        if child.tag == qn("w:del"):
            p_el.remove(child)  # 既有删除直接接受
            continue
        if child.tag == qn("w:ins"):
            pos = list(p_el).index(child)
            for node in list(child):
                child.remove(node)
                p_el.insert(pos, node)
                pos += 1
            p_el.remove(child)
            continue
        p_el.remove(child)
        _run_to_deltext(child)
        del_el.append(child)
    p_el.insert(_content_start_index(p_el, idx), del_el)
    if new_text:
        ins_el = reviser.new_ins()
        r = _new_run(p_el, new_text, rpr)
        if r is not None:
            ins_el.append(r)
        del_el.addnext(ins_el)
    _repair_comment_ranges(p_el)


def _mark_para_mark_deleted(p_el, reviser: _Reviser) -> None:
    """段落标记删除：pPr/rPr/w:del（接受修订后该段与下一段合并）。"""
    ppr = p_el.find(qn("w:pPr"))
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        p_el.insert(0, ppr)
    rpr = ppr.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        ppr.append(rpr)  # schema 中 rPr 是 pPr 最后一个子元素
    rpr.append(reviser.new_del())


def _mark_para_mark_inserted(p_el, reviser: _Reviser) -> None:
    """段落标记插入：pPr/rPr/w:ins（新段落整体是一条插入修订）。"""
    ppr = p_el.find(qn("w:pPr"))
    if ppr is None:
        ppr = OxmlElement("w:pPr")
        p_el.insert(0, ppr)
    rpr = ppr.find(qn("w:rPr"))
    if rpr is None:
        rpr = OxmlElement("w:rPr")
        ppr.append(rpr)
    rpr.append(reviser.new_ins())


def _has_keeper_anchors(p_el) -> bool:
    """段落是否承载批注/书签锚点（删除整段会丢失锚点）。"""
    for tag in ("w:commentRangeStart", "w:commentRangeEnd",
                "w:commentReference", "w:bookmarkStart", "w:bookmarkEnd"):
        if p_el.find(f".//{qn(tag)}") is not None:
            return True
    return False


def _remove_paragraphs(paras, reviser: _Reviser | None) -> None:
    """删除对齐后多余的段落。

    - 承载批注/书签锚点的段落只清文本不删元素（锚点不能丢）
    - 修订模式：内容包 w:del + 段落标记删除，Word 中接受后才真正消失
    """
    for p in paras:
        p_el = p._p
        if _has_keeper_anchors(p_el):
            _merge_para_text(p, "", reviser)
            continue
        if reviser is not None:
            del_el = reviser.new_del()
            for child in list(p_el):
                if child.tag == qn("w:pPr"):
                    continue
                p_el.remove(child)
                _run_to_deltext(child)
                del_el.append(child)
            ppr = p_el.find(qn("w:pPr"))
            if ppr is not None:
                ppr.addnext(del_el)
            else:
                p_el.insert(0, del_el)
            _mark_para_mark_deleted(p_el, reviser)
        else:
            p_el.getparent().remove(p_el)


def _clone_style_paragraph(style_src, text: str,
                           reviser: _Reviser | None) -> object:
    """按样式源段落克隆新 <w:p>：继承 pPr 与首个文本 run 的 rPr。

    克隆体会剥离内容元素（批注/书签/脚注引用/图片/域等），避免 ID 冲突
    与对象重复；正文由 text 重新生成。
    """
    if style_src is not None:
        new_p = deepcopy(style_src._p)
    else:
        new_p = OxmlElement("w:p")
    rpr = None
    for r in new_p.findall(qn("w:r")):
        if _run_text(r) and r.find(qn("w:rPr")) is not None:
            rpr = deepcopy(r.find(qn("w:rPr")))
            break
    for child in list(new_p):
        if child.tag != qn("w:pPr"):
            new_p.remove(child)
    if reviser is not None:
        _mark_para_mark_inserted(new_p, reviser)
        ins = reviser.new_ins()
        r = _new_run(new_p, text, rpr)
        if r is not None:
            ins.append(r)
        new_p.append(ins)
    else:
        r = _new_run(new_p, text, rpr)
        if r is not None:
            new_p.append(r)
    return new_p


def write_docx(path: str | Path, paragraphs: list[str],
               styles: list[str] | None = None,
               comments=None,
               track_changes: bool = False,
               author: str = "PaperWB") -> None:
    """原位写回 .docx —— v2：段落对齐 + run 级最小侵入合并。

    - 文本未变的段落零改动（既有修订/引用域/格式天然保留）
    - 变了的段落按字符 diff 重组 run：匹配片段按原 rPr 重建，未匹配片段
      替换为新文本；批注锚点/书签/脚注引用/行内图片/超链接/域指令保留
    - 段落数量按文本相似度对齐；新增段落继承相邻段落样式
    - track_changes=True：删除包 w:del、插入包 w:ins（author 默认 PaperWB），
      Word 中可逐处接受/拒绝；域结果区与容器内的改动退化为整段修订
    - styles/comments 参数仅为兼容旧调用签名，不再使用
    """
    path = str(path)
    doc = Document(path)  # 原位打开：所有 part（含 comments/页眉等）保留
    reviser = _Reviser(author) if track_changes else None

    body_paras = list(doc.paragraphs)
    new_texts = [str(x) for x in paragraphs]
    old_texts = [_para_text(p) for p in body_paras]

    sm = difflib.SequenceMatcher(None, old_texts, new_texts, autojunk=False)
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            continue  # 未变段落零改动
        if tag == "replace":
            common = min(i2 - i1, j2 - j1)
            for k in range(common):
                _merge_para_text(body_paras[i1 + k], new_texts[j1 + k], reviser)
            _remove_paragraphs(body_paras[i1 + common:i2], reviser)
            src = body_paras[i1 + common - 1] if i1 + common > 0 else (
                body_paras[i2] if i2 < len(body_paras) else None)
            _insert_paragraphs(doc, body_paras, i2,
                               new_texts[j1 + common:j2], src, reviser)
        elif tag == "delete":
            _remove_paragraphs(body_paras[i1:i2], reviser)
        elif tag == "insert":
            src = body_paras[i1 - 1] if i1 > 0 else (
                body_paras[i1] if i1 < len(body_paras) else None)
            _insert_paragraphs(doc, body_paras, i1,
                               new_texts[j1:j2], src, reviser)

    doc.save(path)


def _insert_paragraphs(doc, body_paras, old_index: int, texts: list[str],
                       style_src, reviser: _Reviser | None) -> None:
    """在 body_paras[old_index] 之前插入新段落（继承 style_src 样式）。"""
    for text in texts:
        new_p = _clone_style_paragraph(style_src, text, reviser)
        if old_index < len(body_paras):
            body_paras[old_index]._p.addprevious(new_p)
        else:
            body = doc.element.body
            sectpr = body.find(qn("w:sectPr"))
            if sectpr is not None:
                sectpr.addprevious(new_p)
            else:
                body.append(new_p)


def has_unsaved_changes(path: str | Path, current_text: str) -> bool:
    """判断磁盘文件与当前文本是否一致（用于未保存提示）。"""
    try:
        content = read_docx(path)
        return content.to_plain_text() != current_text
    except Exception:  # noqa: BLE001
        return True
