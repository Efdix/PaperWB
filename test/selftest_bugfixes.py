# -*- coding: utf-8 -*-
"""本次 bug 修复的核心逻辑自测（纯逻辑，无 GUI、无 LLM）。"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {detail}")


# ---------- 1. _strip_watermarks 不再损坏 T cell / B cells ----------
from src.core.pdf_processor import _strip_watermarks

t = _strip_watermarks("T cell receptor activation in B cells on X axis was studied")
check("T cell 保留", "T cell" in t, t)
check("B cells 保留", "B cells" in t, t)
check("X axis 保留", "X axis" in t, t)
t2 = _strip_watermarks("AStudy of G protein; ARTICLE IN PRESS 3 pro fi ling data")
check("AStudy 修复", "A Study" in t2, t2)
check("水印剥离", "ARTICLE" not in t2, t2)
check("连字修复", "profiling" in t2, t2)
check("G protein 保留", "G protein" in t2, t2)

# ---------- 2. parse_json_response 顶层数组/字符串返回 None ----------
from src.core.json_utils import parse_json_response

check("顶层数组→None", parse_json_response('[1,2,3]') is None)
check("字符串→None", parse_json_response('"hello"') is None)
check("正常dict", parse_json_response('{"a": 1}') == {"a": 1})
check("代码块提取", parse_json_response('```json\n{"a": 2}\n```') == {"a": 2})
check("空串→None", parse_json_response("") is None)
check("None→None", parse_json_response(None) is None)

# ---------- 3. draft_reviewer.format_review_for_polish 空采纳不注入 ----------
from src.core.draft_reviewer import DraftReviewer

review = {
    "section_analysis": [{"section": "Intro", "citation_status": "偏少"}],
    "_accepted_items": [],  # 用户全部拒绝
    "_rejected_items": [{"category": "x", "title": "y", "suggestion": "z"}],
}
out = DraftReviewer.format_review_for_polish(review)
check("空采纳不注入", out == "", repr(out))

review_old = {  # 旧格式无 _accepted_items → 回退重建
    "section_analysis": [{"section": "Intro", "citation_status": "偏少",
                          "citation_count": 1, "citation_benchmark": 5}],
    "redundancy": None,  # null 防护
    "figure_suggestions": None,
    "terminology_consistency": None,
    "transition_summary_gaps": None,
}
out2 = DraftReviewer.format_review_for_polish(review_old)
check("旧格式回退注入", "偏少" in out2, repr(out2))

review_acc = {"_accepted_items": [{"category": "术语", "title": "t", "suggestion": "s"}]}
out3 = DraftReviewer.format_review_for_polish(review_acc)
check("采纳项注入", "术语" in out3, repr(out3))

# ---------- 4. unified_writer 引文识别出口规范化 ----------
from src.core.unified_writer import UnifiedWriter


class FakeClient:
    def chat_sync(self, *a, **k):
        return '{"citations": [{"author_hint": null, "year_hint": 2024, "original_marker": 5}, "junk", {"author_hint": " Smith ", "year_hint": "2020", "original_marker": "(Smith, 2020)"}]}'


cits = UnifiedWriter.extract_citations_via_llm("x (Smith, 2020)", 1, FakeClient())
check("引文规范化长度", len(cits) == 2, repr(cits))
check("null→空串", cits[0]["author_hint"] == "", repr(cits))
check("年份转str", cits[0]["year_hint"] == "2024", repr(cits))
check("marker转str", cits[0]["original_marker"] == "5", repr(cits))
check("strip生效", cits[1]["author_hint"] == "Smith", repr(cits))

# ---------- 5. writing_coach 库名非法字符校验 ----------
from src.core.writing_coach import WritingCoach

coach = WritingCoach()
try:
    coach.create_profile('bad:name')
    check("非法库名拒绝", False)
except ValueError:
    check("非法库名拒绝", True)
try:
    coach.create_profile('ok_name_测试1')
    check("合法库名通过", True)
    coach.delete_profile('ok_name_测试1')
except ValueError as e:
    check("合法库名通过", False, str(e))

# ---------- 6. Zotero _auto_detect 非 Windows 分支可执行 ----------
from src.core.zotero_parser import ZoteroLibrary

src_text = open(os.path.join(os.path.dirname(__file__), "..", "src", "core", "zotero_parser.py"), encoding="utf-8").read()
check("auto_detect 用 Path 除法", 'home / "Zotero"' in src_text and 'home = Path.home()' in src_text)

# ---------- 7. citation 相关正则 ----------
from src.ui.writing_panel import WritingPanel  # 仅验证 import 链（Qt 部分懒加载）


def _count(text):
    return WritingPanel._count_citation_markers(text)


check("引文计数", _count("(Smith et al., 2020) and (Wang & Li, 2021) plus [1,2] and [3-5]") >= 5)

# ---------- 8. 接缝检测 + 规则组装冒烟 ----------
from src.core.pdf_processor import build_document_fast, find_cross_page_seams

page_data = [
    {"page": 1, "elements": [
        {"id": "p1_e1", "type": "title", "text": "Test Paper", "bbox": [0, 0, 100, 20]},
        {"id": "p1_e2", "type": "body", "text": "A" * 60 + " the continuation starts", "bbox": [0, 30, 100, 60]},
    ]},
    {"page": 2, "elements": [
        {"id": "p2_e1", "type": "body", "text": "and continues here with more words to satisfy", "bbox": [0, 0, 100, 30]},
    ]},
]
seams = find_cross_page_seams(page_data)
check("接缝检测", len(seams) == 1, repr(seams))
merged = {"p1_e2|p2_e1": {"with_id": "p2_e1", "merged_text": "merged full text here"}}
doc = build_document_fast(page_data, merged)
body_texts = [e.text for e in doc.display_elements if e.element_type == "body"]
check("接缝合并生效", "merged full text here" in body_texts, repr(body_texts))

print(f"\n通过 {len(PASS)} 项，失败 {len(FAIL)} 项")
if FAIL:
    for f in FAIL:
        print("  FAIL:", f)
    sys.exit(1)
print("ALL OK")
