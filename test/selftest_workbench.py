# -*- coding: utf-8 -*-
"""文献工作台核心逻辑自测（无 LLM、无真实网络；UI 用 offscreen 模式构造）。

覆盖：reference_match 匹配口径 / retriever 透传 / library_qa 索引与 RAG
消息组装（含增量刷新与持久化）/ literature_scout 存储与导出 / UI 面板离屏构建。
"""
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {detail}")


# ---------- 1. reference_match ----------
from src.core.reference_match import (
    find_library_match, llm_match_titles, normalize_doi, normalize_title,
)

check("DOI 前缀剥除", normalize_doi("https://doi.org/10.1002/Abc.123") == "10.1002/abc.123")
check("DOI dx 前缀", normalize_doi("http://dx.doi.org/10.1/x") == "10.1/x")
check("DOI doi: 前缀", normalize_doi("doi: 10.2/y") == "10.2/y")
check("DOI 空", normalize_doi("") == "")
check("标题归一化", normalize_title("A Study-of: Feather_color!") == "astudyoffeathercolor")

pool = [
    {"key": "K1", "title": "Feather color development in birds", "doi": "10.1002/abc.123"},
    {"key": "K2", "title": "Unrelated paper", "doi": ""},
]
check("DOI 精确匹配", (find_library_match("Whatever Title", "10.1002/ABC.123", pool) or {}).get("key") == "K1")
check("标题精确匹配", (find_library_match("Feather Color Development in Birds!", "", pool) or {}).get("key") == "K1")
check("无匹配返回 None", find_library_match("Something else entirely", "10.9/zzz", pool) is None)


class FakeLLM:
    def chat_sync(self, messages, **kw):
        return '{"match": [{"candidate": 0, "library": 1}, {"candidate": 99, "library": 0}, "junk"]}'

mapping = llm_match_titles(FakeLLM(), [{"title": "A", "year": "2020"}], pool)
check("LLM 二级匹配合法索引", mapping == {0: 1}, repr(mapping))


class BadLLM:
    def chat_sync(self, messages, **kw):
        raise RuntimeError("boom")

check("LLM 二级匹配容错", llm_match_titles(BadLLM(), [{"title": "A"}], pool) == {})
check("LLM 二级匹配无客户端", llm_match_titles(None, [{"title": "A"}], pool) == {})

# ---------- 2. retriever 透传自定义键 ----------
from src.core.retriever import Bm25Retriever

r = Bm25Retriever()
r.index([{"text": "avian melanocyte single cell sequencing", "k": "ABC123", "page": 2}])
hits = r.search("avian melanocyte", top_k=3)
check("检索透传自定义键", bool(hits) and hits[0].get("k") == "ABC123", repr(hits))

# ---------- 3. ZoteroItem 摘要字段 ----------
from src.core.zotero_parser import ZoteroItem

it = ZoteroItem(item_id=1, key="K1")
check("ZoteroItem.abstract 字段", hasattr(it, "abstract") and it.abstract == "")

# ---------- 4. library_qa 引擎 ----------
import fitz

from src.core.library_qa import LibraryQAEngine, extract_pdf_chunks

tmp = tempfile.mkdtemp(prefix="paperwb_wb_test_")
try:
    # 构造一篇确定内容的测试 PDF
    pdf_path = os.path.join(tmp, "sample.pdf")
    doc = fitz.open()
    page = doc.new_page()
    text = ("The melanocyte development pathway regulates plumage pigmentation "
            "in birds. Melanocyte precursor cells migrate through the neural crest "
            "and differentiate into pigment cells controlling feather color.")
    page.insert_textbox(fitz.Rect(50, 60, 545, 400), text, fontsize=11)
    doc.save(pdf_path)
    doc.close()

    chunks = extract_pdf_chunks(pdf_path)
    check("PDF 抽段非空", len(chunks) >= 1 and "melanocyte" in chunks[0]["t"].lower(), repr(chunks[:1]))
    check("抽段含页码", chunks[0].get("p") == 1)
    check("抽段失败返回空", extract_pdf_chunks(os.path.join(tmp, "no_such.pdf")) == [])

    item1 = ZoteroItem(item_id=1, key="KEYPDF", title="Melanocyte pathways in plumage",
                       authors=["Smith, John", "Lee, Mary"], year="2023",
                       publication="Journal of Bird Biology", doi="10.1/mel",
                       abstract="plumage pigmentation melanocyte", pdf_path=pdf_path)
    item2 = ZoteroItem(item_id=2, key="KEYMETA", title="Neural crest migration review",
                       authors=["Chan, Wei"], year="2020", publication="",
                       doi="", abstract="", pdf_path="")
    items = [item1, item2]

    engine = LibraryQAEngine(index_dir=tmp)
    engine.set_items(items)
    check("元数据索引就绪", engine.is_ready)

    stats = engine.refresh_fulltext(items)
    check("全量索引文献数", stats["items"] == 1, repr(stats))
    check("全量索引段落数", stats["chunks"] == len(chunks), repr(stats))

    # 全文模式：命中 PDF 篇 + 元数据篇
    messages, refs = engine.prepare_messages("melanocyte plumage pigmentation")
    keys = [x["key"] for x in refs]
    check("全文检索命中 PDF 篇", "KEYPDF" in keys, repr(refs))
    check("refs 携带 pdf_path", refs and refs[0]["pdf_path"] == pdf_path, repr(refs))
    check("refs 编号连续", [x["n"] for x in refs] == list(range(1, len(refs) + 1)))
    ctx = messages[-1]["content"]
    check("上下文含文献块", "【文献库检索结果】" in ctx and "[1]" in ctx)
    check("上下文含页码标注", "（第 1 页）" in ctx)
    check("系统提示含角标规则", "角标" in messages[0]["content"])

    # 只问库模式：不检索全文，仅元数据
    messages2, refs2 = engine.prepare_messages("neural crest migration", metadata_only=True)
    check("只问库命中元数据篇", [x["key"] for x in refs2] == ["KEYMETA"], repr(refs2))
    check("只问库无页码段落", "（第 1 页）" not in messages2[-1]["content"])

    # 无命中
    _, refs3 = engine.prepare_messages("quantum computing blockchain")
    check("无命中 refs 为空", refs3 == [], repr(refs3))

    # 历史消息进入组装
    messages4, _ = engine.prepare_messages(
        "melanocyte", history=[{"role": "user", "content": "先前的问题"},
                               {"role": "assistant", "content": "先前的回答"}])
    check("历史进入消息", any(m["content"] == "先前的问题" for m in messages4[:-1]))

    # 增量刷新：mtime 不变时不重抽（新引擎实例直接复用缓存文件）
    engine_b = LibraryQAEngine(index_dir=tmp)
    stats_b = engine_b.refresh_fulltext(items)
    check("持久化复用", stats_b["items"] == 1 and stats_b["chunks"] == stats["chunks"], repr(stats_b))
    check("状态文件存在", os.path.isfile(os.path.join(tmp, "fulltext.json")))

    # PDF 更新（mtime 变化）→ 重建该篇
    os.utime(pdf_path)  # 更新 mtime
    stats_c = LibraryQAEngine(index_dir=tmp).refresh_fulltext(items)
    check("mtime 失效重建", stats_c["items"] == 1 and stats_c["chunks"] >= 1, repr(stats_c))

    # 条目移除后索引清理
    stats_d = LibraryQAEngine(index_dir=tmp).refresh_fulltext([item2])
    check("移除条目清理索引", stats_d["items"] == 0 and stats_d["chunks"] == 0, repr(stats_d))
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# ---------- 5. literature_scout 存储 ----------
from src.core.literature_scout import (
    ScoutTopic, load_feed, load_seen, load_topics, mark_seen,
    paper_to_dict, papers_to_ris, save_feed, save_topics,
)
from src.core.pubmed_searcher import PubMedPaper

tmp2 = tempfile.mkdtemp(prefix="paperwb_scout_test_")
try:
    t = ScoutTopic(id="t1", name="羽色", keywords=["bird plumage", "feather color"],
                   collection_key="", interval_hours=6, limit=20,
                   enabled=True, use_llm_match=True, last_run="2026-08-01T10:00:00",
                   last_new=3)
    save_topics([t], tmp2)
    loaded = load_topics(tmp2)
    check("topics 往返", len(loaded) == 1 and loaded[0].to_dict() == t.to_dict(), repr(loaded))

    # 异常数据容错（save_topics 整文件覆盖 → 两条一起存）
    t2 = ScoutTopic(id="t2", interval_hours=9999, keywords="a\nb\nc")
    save_topics([t, t2], tmp2)
    loaded2 = load_topics(tmp2)
    check("topics 异常值钳制", len(loaded2) == 2 and loaded2[1].interval_hours <= 168
          and loaded2[1].keywords == ["a", "b", "c"], repr(loaded2[1]))

    mark_seen(["111", "222"], tmp2)
    mark_seen(["333"], tmp2)
    seen = load_seen(tmp2)
    check("seen 去重记忆", set(seen.keys()) == {"111", "222", "333"}, repr(seen))

    paper = PubMedPaper(pmid="12345", title="A title", authors="Smith J, Lee M, et al.",
                        year="2024", journal="Nature", doi="10.1/x",
                        abstract="Abs.", url="https://pubmed.ncbi.nlm.nih.gov/12345/")
    d = paper_to_dict(paper)
    check("paper_to_dict", d["pmid"] == "12345" and d["year"] == "2024")

    ris = papers_to_ris([d])
    check("RIS 头尾", ris.startswith("TY  - JOUR") and "ER  - " in ris)
    check("RIS 作者规范化", "AU  - Smith, J." in ris and "AU  - Lee, M." in ris and "et al" not in ris, repr(ris))
    check("RIS 含 DOI/URL", "DO  - 10.1/x" in ris and "UR  - https://" in ris)

    feed = [{"id": "12345@羽色", "topic": "羽色", "added_at": "2026-08-01T10:00:00",
             "paper": d, "ignored": False}]
    save_feed(feed, tmp2)
    check("feed 往返", load_feed(tmp2)[0]["id"] == "12345@羽色")

    check("空目录 topics", load_topics(os.path.join(tmp2, "empty_sub")) == [])
finally:
    shutil.rmtree(tmp2, ignore_errors=True)

# ---------- 6. UI 离屏构建 ----------
from PySide6.QtWidgets import QApplication

app = QApplication.instance() or QApplication([])

from src.ui.workbench_panel import (
    ReferenceListCard, ScoutCard, TopicCard, TopicEditDialog, WorkbenchPanel,
)
from src.ui.chat_panel import ChatBubble

# ChatBubble 修复回归：流式完成后 set_thinking(False) 不得清掉内容
_bb = ChatBubble("assistant", "AI 正在思考...", thinking=True)
_bb.append_content("流式内容ABC")
_bb.set_thinking(False)
check("气泡完成后不清内容", _bb.get_content() == "流式内容ABC", _bb.get_content())
_bb2 = ChatBubble("assistant", "AI 正在思考...", thinking=True)
_bb2.set_thinking(False)
check("占位文本首次切换清除", _bb2.get_content() == "", _bb2.get_content())

panel = WorkbenchPanel()
panel.set_parse_client(None)
panel.set_zotero_library(None)
check("空库状态文案", "Zotero" in panel._qa_status.text(), panel._qa_status.text())
check("未配置时提问禁用", not panel._ask_btn.isEnabled())

dlg = TopicEditDialog([("KEY1", "集合A"), ("KEY2", "父 / 子")])
check("集合下拉含全库", dlg._collection_combo.count() == 3)

card = TopicCard(ScoutTopic(id="t1", name="方向", keywords=["k1", "k2"],
                            interval_hours=6, last_run="2026-08-01T10:00:00", last_new=2))
card.set_running(True)
check("卡片运行态", "⏳" in card._name_label.text(), card._name_label.text())

scard = ScoutCard({"id": "x@t", "topic": "方向", "added_at": "",
                   "paper": {"title": "T", "authors": "Smith J", "year": "2024",
                             "journal": "J", "abstract": "a" * 300, "url": "", "pmid": "1"}})
check("推荐卡摘要截断", scard.layout().count() >= 3)

rcard = ReferenceListCard([
    {"n": 1, "authors": "Smith et al.", "year": "2020", "title": "Some Title",
     "pdf_path": "C:/x.pdf", "page": 3, "has_pdf": True},
    {"n": 2, "authors": "Lee", "year": "2021", "title": "Other",
     "pdf_path": "", "page": 0, "has_pdf": False},
])
check("参考文献卡片构建", rcard.layout().count() == 3)  # header + 2 行

panel.shutdown()
check("shutdown 后无忙碌线程", not panel.has_busy_workers())

# ---------- 7. QA worker 流式链路（假客户端） ----------
import time as _time

import src.core.literature_scout as scout_mod
from src.ui.workbench_panel import LibraryQAWorker  # noqa: E402


class FakeStreamClient:
    def chat_stream(self, messages):
        yield "文献[1]指出黑色素细胞通路"
        yield "调控羽色发育（结论）。"


def make_sample_engine(tmpdir):
    pdf = os.path.join(tmpdir, "s.pdf")
    d = fitz.open()
    pg = d.new_page()
    pg.insert_textbox(
        fitz.Rect(50, 60, 545, 400),
        "The melanocyte development pathway regulates plumage pigmentation "
        "in birds through neural crest migration.", fontsize=11)
    d.save(pdf)
    d.close()
    it = ZoteroItem(item_id=1, key="KPDF", title="Melanocyte plumage",
                    authors=["Smith, John"], year="2023", pdf_path=pdf)
    eng = LibraryQAEngine(index_dir=tmpdir)
    eng.set_items([it])
    eng.refresh_fulltext([it])
    return eng, it


tmp3 = tempfile.mkdtemp(prefix="paperwb_qa_test_")
try:
    eng3, _item = make_sample_engine(tmp3)
    worker = LibraryQAWorker(FakeStreamClient(), eng3, "melanocyte plumage",
                             metadata_only=False, history=[])
    from src.utils.threads import track
    track(worker)
    chunks_got, refs_got, done_seen = [], [], []
    worker.chunk_received.connect(chunks_got.append)
    worker.answer_finished.connect(refs_got.append)
    worker.done.connect(lambda: done_seen.append(True))
    worker.start()
    # 等 done 投递（保证排队中的 chunk/answer 信号也已处理）
    deadline = _time.time() + 10
    while _time.time() < deadline and (worker.isRunning() or not done_seen):
        app.processEvents()
        _time.sleep(0.02)
    check("QA worker 流式拼接", "".join(chunks_got) == "文献[1]指出黑色素细胞通路调控羽色发育（结论）。",
          repr(chunks_got))
    check("QA worker 参考文献回传", refs_got and refs_got[0][0]["key"] == "KPDF", repr(refs_got))

    # ---------- 8. 面板级问答流程 ----------
    from src.ui.workbench_panel import ReferenceListCard, WorkbenchPanel as WB

    panel2 = WB()
    panel2.set_parse_client(FakeStreamClient())
    panel2._engine = eng3
    panel2._engine_ready = True
    panel2._apply_ask_state()
    check("就绪后提问可用", panel2._ask_btn.isEnabled())
    panel2._ask_input.setPlainText("melanocyte plumage")
    panel2._on_ask()
    deadline = _time.time() + 10
    while _time.time() < deadline and panel2._qa_busy:
        app.processEvents()
        _time.sleep(0.02)
    check("问答历史两条", len(panel2._qa_history) == 2, repr(panel2._qa_history))
    check("回答内容入库", "黑色素细胞" in panel2._qa_history[-1]["content"],
          repr(panel2._qa_history[-1:]))

    def _layout_widgets(lay):
        return [lay.itemAt(i).widget() for i in range(lay.count())
                if lay.itemAt(i).widget() is not None]

    check("参考文献卡片插入",
          any(isinstance(w, ReferenceListCard) for w in _layout_widgets(panel2._msg_layout)))

    # 引用点击信号 → open_pdf_requested
    ref_card = next(w for w in _layout_widgets(panel2._msg_layout)
                    if isinstance(w, ReferenceListCard))
    opened = []
    panel2.open_pdf_requested.connect(opened.append)
    ref_card.open_requested.emit(os.path.join(tmp3, "s.pdf"))
    check("引用跳转信号", opened == [os.path.join(tmp3, "s.pdf")], repr(opened))
    panel2.shutdown()

    # ---------- 9. Scout 全链路（假 PubMed，无网络） ----------
    from src.core.literature_scout import ScoutManager

    class FakePubMed:
        def search(self, queries, limit=10):
            return [
                PubMedPaper(pmid="90002", title="Feather color development in birds",
                            authors="Old A", year="2020", journal="J2", doi="",
                            abstract="", url=""),
                PubMedPaper(pmid="90001", title="Brand new paper on wings",
                            authors="Nova R", year="2026", journal="J", doi="10.9/new",
                            abstract="abs", url="u"),
            ]

    orig_searcher = scout_mod.PubMedSearcher
    scout_mod.PubMedSearcher = FakePubMed
    try:
        mgr = ScoutManager(scout_dir=tmp3)
        mgr.set_match_pool([
            {"key": "K1", "title": "Feather color development in birds",
             "doi": "10.1002/abc.123", "authors": "Smith", "year": "2020",
             "collections": []},
        ])
        # enabled=False：只验证手动巡视，不启动定时器
        mgr.upsert_topic(ScoutTopic(id="s1", name="方向A", keywords=["wing paper"],
                                    enabled=False))
        got = []
        mgr.results_ready.connect(lambda name, entries: got.append((name, entries)))
        check("手动巡视启动", mgr.run_topic_now("s1") is True)
        # 等 _workers 清空（done 回调投递完成，保证 found 也已处理）
        deadline = _time.time() + 10
        while _time.time() < deadline and (mgr.has_busy_workers() or mgr._workers):
            app.processEvents()
            _time.sleep(0.02)
        check("巡视产出结果", len(got) == 1, repr(got))
        entries = got[0][1] if got else []
        check("过滤库内已有", [e["paper"]["pmid"] for e in entries] == ["90001"],
              repr(entries))
        check("feed 持久化", [e["paper"]["pmid"] for e in load_feed(tmp3)] == ["90001"])
        check("seen 去重记忆", "90001" in load_seen(tmp3))
        t = mgr.get_topic("s1")
        check("last_run/last_new 更新", bool(t.last_run) and t.last_new == 1, repr(t))

        # 再次巡视：seen 过滤后无新结果
        got.clear()
        check("再次巡视启动", mgr.run_topic_now("s1") is True)
        deadline = _time.time() + 10
        while _time.time() < deadline and (mgr.has_busy_workers() or mgr._workers):
            app.processEvents()
            _time.sleep(0.02)
        check("去重记忆生效", got == [], repr(got))

        mgr.ignore_feed_item(load_feed(tmp3)[0]["id"])
        check("忽略后 feed 为空", mgr.feed_items() == [])
        mgr.shutdown()
    finally:
        scout_mod.PubMedSearcher = orig_searcher
finally:
    shutil.rmtree(tmp3, ignore_errors=True)

# ---------- 10. app 模块导入 ----------
import src.app  # noqa: F401,E402
check("src.app 导入", True)

# ---------- 汇总 ----------
print()
for name in PASS:
    print(f"[PASS] {name}")
for name in FAIL:
    print(f"[FAIL] {name}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
