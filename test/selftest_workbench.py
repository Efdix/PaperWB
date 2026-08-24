# -*- coding: utf-8 -*-
"""检索工作台与库内问答核心逻辑自测（无 LLM、无真实网络；UI 用 offscreen 模式构造）。

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
    ScoutCard, TopicCard, TopicEditDialog, WorkbenchPanel,
)
from src.ui.library_qa_panel import LibraryQAPanel, ReferenceListCard
from src.ui.chat_panel import ChatBubble

# ChatBubble 修复回归：流式完成后 set_thinking(False) 不得清掉内容
_bb = ChatBubble("assistant", "AI 正在思考...", thinking=True)
_bb.append_content("流式内容ABC")
_bb.set_thinking(False)
check("气泡完成后不清内容", _bb.get_content() == "流式内容ABC", _bb.get_content())
_bb2 = ChatBubble("assistant", "AI 正在思考...", thinking=True)
_bb2.set_thinking(False)
check("占位文本首次切换清除", _bb2.get_content() == "", _bb2.get_content())

qa_panel = LibraryQAPanel()
qa_panel.set_text_client(None)
qa_panel.set_zotero_library(None)
check("空库状态文案", "Zotero" in qa_panel._qa_status.text(), qa_panel._qa_status.text())
check("未配置时提问禁用", not qa_panel._ask_btn.isEnabled())

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

qa_panel.shutdown()
check("shutdown 后无忙碌线程", not qa_panel.has_busy_workers())

# ---------- 7. QA worker 流式链路（假客户端） ----------
import time as _time

import src.core.literature_scout as scout_mod
from src.ui.library_qa_panel import LibraryQAWorker  # noqa: E402


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
    from src.ui.library_qa_panel import LibraryQAPanel as WB

    panel2 = WB()
    panel2.set_text_client(FakeStreamClient())
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

    # ---------- 9. Scout 全链路（假多源检索器，无网络） ----------
    from src.core.literature_search import MultiSourceSearcher
    from src.core.literature_scout import ScoutManager

    class FakeMultiSource:
        """假多源检索器：按 plan 返回 PubMed + arXiv 混合结果。"""

        def search(self, plan, limit=10):
            return [
                PubMedPaper(pmid="90002", title="Feather color development in birds",
                            authors="Old A", year="2020", journal="J2", doi="",
                            abstract="", url=""),
                PubMedPaper(pmid="90001", title="Brand new paper on wings",
                            authors="Nova R", year="2026", journal="J", doi="10.9/new",
                            abstract="abs", url="u"),
                PubMedPaper(pmid="", title="Preprint on wing morphogenesis",
                            authors="Pre A", year="2025", journal="",
                            doi="", abstract="", url="https://arxiv.org/abs/2501.1",
                            source="arxiv", arxiv_id="2501.1"),
            ]

    mgr = ScoutManager(scout_dir=tmp3)
    mgr.set_searcher(FakeMultiSource())
    try:
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
        # 90002 已在库内被滤掉；90001 与 arXiv 预印本都应进入 feed
        check("过滤库内已有", sorted(e["paper"]["pmid"] or e["paper"]["arxiv_id"]
                                    for e in entries) == ["2501.1", "90001"],
              repr(entries))
        check("feed 持久化", sorted(e["paper"]["pmid"] or e["paper"]["arxiv_id"]
                                    for e in load_feed(tmp3)) == ["2501.1", "90001"])
        check("seen 去重记忆", "90001" in load_seen(tmp3) and "2501.1" in load_seen(tmp3))
        t = mgr.get_topic("s1")
        check("last_run/last_new 更新", bool(t.last_run) and t.last_new == 2, repr(t))

        # 再次巡视：seen 过滤后无新结果
        got.clear()
        check("再次巡视启动", mgr.run_topic_now("s1") is True)
        deadline = _time.time() + 10
        while _time.time() < deadline and (mgr.has_busy_workers() or mgr._workers):
            app.processEvents()
            _time.sleep(0.02)
        check("去重记忆生效", got == [], repr(got))

        for entry in load_feed(tmp3):
            mgr.ignore_feed_item(entry["id"])
        check("忽略后 feed 为空", mgr.feed_items() == [])
        mgr.shutdown()
    finally:
        pass  # 假检索器无全局替换，无需恢复

    # ---------- 9c. AI 检索链路（统一核心：检索式生成 → 多源 → 库内过滤） ----------
    from src.core.literature_search import (
        generate_search_plan, merge_papers, run_paper_search,
    )

    class PlanLLM:
        """假 LLM：返回多源检索方案。"""

        def chat_sync(self, messages, **kw):
            return ('{"queries": [{"source": "pubmed", "query": "wing paper"},'
                    '{"source": "arxiv", "query": "wing morphogenesis"}]}')

    plan = generate_search_plan(PlanLLM(), "找翅膀相关的论文")
    check("检索式生成", plan == [{"source": "pubmed", "query": "wing paper"},
                               {"source": "arxiv", "query": "wing morphogenesis"}],
          repr(plan))
    check("检索式生成失败降级", generate_search_plan(BadLLM(), "x") is None)
    check("检索式生成无客户端", generate_search_plan(None, "x") is None)

    merged = merge_papers([
        PubMedPaper(pmid="1", title="Dup Title", doi="10.1/dup", source="pubmed"),
        PubMedPaper(pmid="", title="Dup Title 2", doi="10.1/dup", source="arxiv"),
    ])
    check("跨源合并去重", len(merged) == 1, repr(merged))

    logs = []
    papers = run_paper_search(
        "wing paper", client=None,  # 无 LLM → 原文检索降级
        pool=[{"title": "Brand new paper on wings", "doi": "10.9/new"}],
        limit=10, searcher=FakeMultiSource(), log_cb=logs.append,
    )
    check("统一检索降级路径", "未配置 LLM" in " ".join(logs), repr(logs))
    # 库内过滤：90001 的 DOI 10.9/new 在库内被滤掉，arXiv 预印本保留
    ids = sorted(p.get("pmid") or p.get("arxiv_id") for p in papers)
    check("统一检索库内过滤", ids == ["2501.1", "90002"], repr(ids))
    check("统一检索来源标注", {p.get("source") for p in papers} == {"pubmed", "arxiv"},
          repr(papers))

    # 面板 AI 检索页签（假 LLM + 假检索器，无网络）
    from src.ui.workbench_panel import WorkbenchPanel as WB2
    panel3 = WB2()
    panel3._ai_search_btn.setEnabled(True)
    panel3.set_ai_searcher(FakeMultiSource())
    panel3.set_text_client(PlanLLM())
    panel3._ai_input.setPlainText("翅膀形态发生预印本")
    panel3._on_ai_search()
    deadline = _time.time() + 10
    while _time.time() < deadline and panel3._ai_worker is not None:
        app.processEvents()
        _time.sleep(0.02)
    check("AI 检索无崩溃", panel3._ai_worker is None)
    panel3.shutdown()
    panel2.shutdown()

    # ---------- 9d. OpenAlex 解析与三源路由（fake 网络，无真实请求） ----------
    from src.core import openalex as oa_mod

    check("倒排摘要还原",
          oa_mod._abstract_from_inverted({"Hello": [0], "big": [1], "world": [2]})
          == "Hello big world")

    _work = {
        "id": "https://openalex.org/W1", "display_name": "Feather Color Study",
        "publication_year": 2024, "doi": "https://doi.org/10.1/x",
        "cited_by_count": 7,
        "authorships": [{"raw_author_name": "Smith J"}, {"raw_author_name": "Lee M"}],
        "primary_location": {"source": {"display_name": "Nature"},
                             "landing_page_url": "https://x.example/1"},
        "abstract_inverted_index": {"feather": [0], "color": [1]},
        "type": "article", "ids": {},
    }
    _wp = oa_mod._work_to_paper(_work)
    check("OpenAlex work→paper",
          _wp is not None and _wp.title == "Feather Color Study"
          and _wp.source == "openalex" and _wp.cited_by == 7
          and _wp.doi == "10.1/x" and _wp.journal == "Nature"
          and _wp.abstract == "feather color", repr(_wp))

    _oa_calls = []

    def _fake_oa_get(params):
        _oa_calls.append(params)
        return {"results": [dict(_work)]}

    _orig_oa_get = oa_mod._get
    oa_mod._get = _fake_oa_get
    try:
        _oa = oa_mod.OpenAlexSearcher(delay=0.0)
        _hits = _oa.search_plan(
            [{"source": "openalex", "query": "feather color",
              "year_from": 2022, "year_to": 2026, "doc_type": "review"}], limit=5)
        check("OpenAlex 检索解析", len(_hits) == 1 and _hits[0].doi == "10.1/x")
        check("OpenAlex 原生过滤",
              "publication_year:2022-2026" in _oa_calls[0]["filter"]
              and "type:review" in _oa_calls[0]["filter"]
              and "default.search:feather color" in _oa_calls[0]["filter"],
              repr(_oa_calls[0]))
    finally:
        oa_mod._get = _orig_oa_get

    _sink = {}

    class _SinkPubmed:
        def search(self, queries, limit=10):
            _sink["pubmed"] = list(queries)
            return [PubMedPaper(pmid="1", title="pubmed hit", year="2024")]

    class _SinkOA:
        def search(self, queries, limit=10):
            _sink["openalex_plain"] = list(queries)
            return []

        def search_plan(self, items, limit=10):
            _sink["openalex_items"] = list(items)
            return [PubMedPaper(title="oa hit", year="2025",
                                source="openalex", cited_by=9)]

    _mss = MultiSourceSearcher(pubmed=_SinkPubmed(), openalex=_SinkOA())
    _res = _mss.search([
        {"source": "pubmed", "query": "q1", "year_from": 2020,
         "year_to": 2024, "doc_type": "review"},
        {"source": "openalex", "query": "q2"},
    ], limit=10)
    check("三源路由-混合", any(r.source == "openalex" for r in _res))
    check("PubMed 过滤后缀",
          _sink["pubmed"] == ["(q1) AND 2020:2024[dp] AND review[pt]"],
          repr(_sink["pubmed"]))
    check("OpenAlex search_plan 接收条目",
          _sink["openalex_items"][0]["query"] == "q2", repr(_sink["openalex_items"]))

    # ---------- 9e. 检索式 v2 过滤字段与加权排序 ----------
    class PlanLLM2:
        def chat_sync(self, messages, **kw):
            return ('{"queries": ['
                    '{"source": "openalex", "query": "wing", "year_from": 2023, "doc_type": "review"},'
                    '{"source": "bogus", "query": "x", "year_from": "bad"}]}')

    _plan2 = generate_search_plan(PlanLLM2(), "找论文")
    check("检索式v2字段解析",
          bool(_plan2) and _plan2[0].get("year_from") == 2023
          and _plan2[0].get("doc_type") == "review", repr(_plan2))
    check("非法源回退openalex",
          _plan2[1]["source"] == "openalex" and "year_from" not in _plan2[1],
          repr(_plan2[1]))

    from src.core.literature_search import rank_papers
    _ps = [PubMedPaper(title="old-cited", year="2015", cited_by=1000),
           PubMedPaper(title="new", year="2026", cited_by=0),
           PubMedPaper(title="mid", year="2020", cited_by=30)]
    _ranked = rank_papers(_ps)
    check("加权排序", [p.title for p in _ranked] == ["new", "mid", "old-cited"],
          repr([p.title for p in _ranked]))

    # ---------- 9f. 两轮闭环检索 ----------
    class TwoRoundLLM:
        """第 1 次调用生成方案；第 2 次反思：enough=false + off_topic + 补充式。"""

        def __init__(self):
            self.calls = 0

        def chat_sync(self, messages, **kw):
            self.calls += 1
            if self.calls == 1:
                return '{"queries": [{"source": "pubmed", "query": "wing paper"}]}'
            return ('{"enough": false, "off_topic": [2], "queries": '
                    '[{"source": "openalex", "query": "extra query"}]}')

    _logs2 = []
    _papers2 = run_paper_search(
        "wing paper", client=TwoRoundLLM(),
        pool=[{"title": "Brand new paper on wings", "doi": "10.9/new",
               "authors": "", "year": "2026", "key": "KL", "collections": []}],
        limit=10, searcher=FakeMultiSource(), log_cb=_logs2.append)
    check("两轮-第2轮执行", any("第 2 轮补充" in m for m in _logs2), repr(_logs2))
    check("两轮-off_topic剔除", any("剔除不切题" in m for m in _logs2))
    _ids2 = sorted(p.get("pmid") or p.get("arxiv_id") for p in _papers2)
    check("两轮-最终结果", _ids2 == ["2501.1", "90002"], repr(_ids2))

    class EnoughLLM:
        def __init__(self):
            self.calls = 0

        def chat_sync(self, messages, **kw):
            self.calls += 1
            if self.calls == 1:
                return '{"queries": [{"source": "pubmed", "query": "wing paper"}]}'
            return '{"enough": true, "off_topic": [], "queries": []}'

    _logs3 = []
    run_paper_search("wing paper", client=EnoughLLM(), pool=[], limit=10,
                     searcher=FakeMultiSource(), log_cb=_logs3.append)
    check("两轮-enough提前终止", any("跳过第 2 轮" in m for m in _logs3), repr(_logs3))

    class OffTopicBounds:
        def chat_sync(self, messages, **kw):
            return '{"enough": true, "off_topic": [99, "x", 1], "queries": []}'

    from src.core.literature_search import reflect_on_results
    _fb = reflect_on_results(
        OffTopicBounds(), "q", [PubMedPaper(title=f"t{i}", year="2024")
                                for i in range(5)])
    check("off_topic 越界忽略", _fb is not None and _fb["off_topic"] == {1},
          repr(_fb))

    # ---------- 9g. 按库推荐（fake OpenAlex，无网络） ----------
    import src.core.library_recommender as rec_mod
    from src.core.library_recommender import build_seeds, recommend_from_library

    _pool3 = [
        {"key": "K1", "title": "Seed paper one", "doi": "10.1/s1",
         "year": "2020", "collections": ["PARENT"]},
        {"key": "K2", "title": "Seed paper two", "doi": "",
         "year": "2021", "collections": ["CHILD", "PARENT"]},
        {"key": "K3", "title": "Other collection", "doi": "10.1/s3",
         "year": "2019", "collections": ["OTHER"]},
    ]
    _seeds = build_seeds(_pool3, "PARENT")
    check("种子含子集合", [s["key"] for s in _seeds] == ["K1", "K2"], repr(_seeds))
    check("全库种子", len(build_seeds(_pool3, "")) == 3)

    class ProfileLLM:
        def chat_sync(self, messages, **kw):
            return ('{"summary": "羽色发育机制", "queries": '
                    '[{"source": "openalex", "query": "feather development"}]}')

    class FakeMS2:
        def search(self, plan, limit=10):
            return [PubMedPaper(title="Profile hit paper", year="2024",
                                doi="10.9/p1")]

    _orig_resolve = rec_mod.resolve_openalex_works
    _orig_reccit = rec_mod.recommend_by_citations
    rec_mod.resolve_openalex_works = (
        lambda seeds, log_cb=None:
        (["W1", "W3"] + [""] * len(seeds))[:len(seeds)])
    rec_mod.recommend_by_citations = lambda ids, **kw: [
        (PubMedPaper(title="Cited new paper", doi="10.9/c1", year="2025",
                     source="openalex", cited_by=12), 3),
        (PubMedPaper(title="Seed paper one", doi="10.1/s1", year="2020"), 1),
        (PubMedPaper(title="Other collection", doi="", year="2019"), 2),
    ]
    try:
        _out, _stats = recommend_from_library(
            _seeds, _pool3, client=ProfileLLM(), year_from=2020, limit=10,
            searcher=FakeMS2())
        _titles = [d["title"] for d in _out]
        check("推荐排除种子与库内",
              "Seed paper one" not in _titles and "Other collection" not in _titles,
              repr(_titles))
        check("两路合并", {d["rec_source"] for d in _out} == {"引文推荐", "画像检索"},
              repr(_out))
        check("引文路在前且带 linked",
              _out[0]["rec_source"] == "引文推荐" and _out[0].get("linked") == 3,
              repr(_out[0]))
        check("推荐统计", _stats["resolved"] == 2 and _stats["citation_hits"] == 1
              and _stats["profile_hits"] == 1, repr(_stats))
        _out2, _stats2 = recommend_from_library(_seeds, _pool3, client=None,
                                                limit=10)
        check("无LLM仅引文路",
              all(d["rec_source"] == "引文推荐" for d in _out2)
              and _stats2["profile_hits"] == 0, repr(_out2))
    finally:
        rec_mod.resolve_openalex_works = _orig_resolve
        rec_mod.recommend_by_citations = _orig_reccit

    check("面板按库推荐默认禁用", not panel3._rec_btn.isEnabled())
    check("默认自然语言检索模式", panel3._rec_range_row.isHidden()
          and not panel3._ai_input.isHidden()
          and not panel3._ai_ctrl_widget.isHidden()
          and panel3._rec_combos[0].count() == 1)  # 无库时仅「全库」
    panel3._rec_mode_cb.setChecked(True)
    check("勾选后互斥切换为按库推荐",
          not panel3._rec_range_row.isHidden()
          and panel3._ai_input.isHidden()
          and panel3._ai_ctrl_widget.isHidden())
    panel3._rec_mode_cb.setChecked(False)
    check("取消勾选恢复自然语言检索", panel3._rec_range_row.isHidden()
          and not panel3._ai_input.isHidden())

    # ---------- 9h. 级联集合选择（最小假 Zotero 库） ----------
    class _FakeColl:
        def __init__(self, key, name, cid, parent=None, children=(), items=()):
            self.key = key
            self.name = name
            self.collection_id = cid
            self.parent_id = parent
            self.child_ids = list(children)
            self.item_ids = list(items)

    class _FakeItem:
        key = "KI1"
        title = "Fake item"
        doi = "10.1/fake"
        year = "2024"
        item_id = 10
        authors = []
        first_author_last = ""

    _root = _FakeColl("KR", "动物学", 1, None, [2, 3], [10])
    _bird = _FakeColl("KB", "鸟类", 2, 1, [4])
    _ins = _FakeColl("KI", "昆虫", 3, 1)
    _feat = _FakeColl("KF", "羽色", 4, 2)

    class _FakeLib:
        is_available = True
        collections = [_root, _bird, _ins, _feat]

        @staticmethod
        def get_collections_tree():
            return [_root]

        @staticmethod
        def get_all_items():
            return [_FakeItem()]

    panel5 = WB2()
    panel5.set_zotero_library(_FakeLib())
    try:
        lvl0 = panel5._rec_combos[0]
        check("级联第0级选项", lvl0.count() == 2
              and lvl0.itemText(0) == "全库" and lvl0.itemText(1) == "动物学")
        check("第0级默认全库", panel5._current_rec_key() == ""
              and panel5._current_rec_label() == "全库")
        check("有库时推荐按钮可用", panel5._rec_btn.isEnabled())

        lvl0.setCurrentIndex(1)  # 动物学 → 下钻第 1 级
        check("选父级出现下级", len(panel5._rec_combos) == 2)
        lvl1 = panel5._rec_combos[1]
        check("第1级含全部子级选项", lvl1.count() == 3
              and lvl1.itemText(0) == "（含全部子级）"
              and lvl1.itemText(1) == "昆虫" and lvl1.itemText(2) == "鸟类")
        check("选父级key生效", panel5._current_rec_key() == "KR"
              and panel5._current_rec_label() == "动物学")

        lvl1.setCurrentIndex(2)  # 鸟类 → 下钻第 2 级
        check("继续下钻第2级", len(panel5._rec_combos) == 3
              and panel5._current_rec_key() == "KB"
              and panel5._current_rec_label() == "动物学 / 鸟类")

        panel5._rec_combos[2].setCurrentIndex(1)  # 羽色（叶子，不再下钻）
        check("最深级选择", len(panel5._rec_combos) == 3
              and panel5._current_rec_key() == "KF"
              and panel5._current_rec_label() == "动物学 / 鸟类 / 羽色")

        lvl1.setCurrentIndex(0)  # 回到（含全部子级）→ 收起更深层
        check("收起更深层", len(panel5._rec_combos) == 2
              and panel5._current_rec_key() == "KR")
        lvl0.setCurrentIndex(0)  # 全库
        check("回到全库", len(panel5._rec_combos) == 1
              and panel5._current_rec_key() == "")

        # 集合同级按名称排序（01 → 02 → 03）
        _colls_sorted = [
            _FakeColl("K1", "01 基础", 101, None, [202, 201]),
            _FakeColl("K3", "03 高级", 103, None),
            _FakeColl("K2", "02 进阶", 102, None),
            _FakeColl("KF2", "01 形态", 201, 101),
            _FakeColl("KB2", "02 羽色", 202, 101),
        ]

        class _FakeLib2:
            is_available = True
            collections = _colls_sorted

            @staticmethod
            def get_collections_tree():
                return [c for c in _colls_sorted if c.parent_id is None]

            @staticmethod
            def get_all_items():
                return []

        panel5b = WB2()
        panel5b.set_zotero_library(_FakeLib2())
        check("集合根级按名称排序",
              panel5b._coll_roots == ["K1", "K2", "K3"], repr(panel5b._coll_roots))
        check("集合子级按名称排序",
              panel5b._coll_nodes["K1"]["children"] == ["KF2", "KB2"],
              repr(panel5b._coll_nodes["K1"]["children"]))
        check("集合下拉按名称排序",
              [panel5b._collections[i][1] for i in range(5)]
              == ["01 基础", "01 基础 / 01 形态", "01 基础 / 02 羽色",
                  "02 进阶", "03 高级"],
              repr(panel5b._collections))
        panel5b.shutdown()
    finally:
        panel5.shutdown()

    # ---------- 9b. push_to_feed 外部推送（文献补充/主动检索共用） ----------
    mgr2 = ScoutManager(scout_dir=tmp3)
    got2 = []
    mgr2.results_ready.connect(lambda name, entries: got2.append((name, entries)))
    pushed = mgr2.push_to_feed([
        {"pmid": "91001", "title": "External pushed paper", "authors": "X",
         "year": "2024", "doi": "10.9/ext", "source": "pubmed"},
        {"pmid": "91002", "title": "Another pushed paper", "authors": "Y",
         "year": "2023", "doi": "10.9/ext2", "source": "arxiv"},
    ], "文献补充")
    check("push_to_feed 推送条数", pushed == 2, repr(pushed))
    check("push_to_feed 信号", len(got2) == 1 and got2[0][0] == "文献补充", repr(got2))
    # 重复推送同一篇 → 去重为 0
    pushed_dup = mgr2.push_to_feed([
        {"pmid": "91001", "title": "External pushed paper", "authors": "X",
         "year": "2024", "doi": "10.9/extra", "source": "pubmed"},
    ], "文献补充")
    check("push_to_feed 去重", pushed_dup == 0, repr(pushed_dup))
    mgr2.shutdown()
finally:
    shutil.rmtree(tmp3, ignore_errors=True)

# ---------- 9i. EasyScholar 影响因子解析与缓存（无网络） ----------
import json as _json
from pathlib import Path
import src.core.easyscholar as es_mod
from unittest.mock import patch

check("easyscholar 期刊名归一化",
      es_mod._normalize_journal("  Nature  Communications ") == "nature communications")

# patch retry_urlopen → 假 JSON 响应，走真实解析路径（不触发网络）
def _fake_retry_ok(req, timeout=30.0):
    return _json.dumps({
        "data": {"officialRank": {"all": {"sciif": "9.4"},
                                  "select": {"sciif5": "11.2"}}},
    }).encode()

with patch.object(es_mod, "retry_urlopen", side_effect=_fake_retry_ok):
    _if = es_mod.fetch_impact_factor("Nature", "sk-test")
check("easyscholar 响应解析", _if == {"if": "9.4", "sci5": "11.2"}, repr(_if))

def _fake_retry_empty(req, timeout=30.0):
    return _json.dumps({"data": {"officialRank": {"all": {}, "select": {}}}}).encode()

with patch.object(es_mod, "retry_urlopen", side_effect=_fake_retry_empty):
    check("easyscholar 未收录返回 None",
          es_mod.fetch_impact_factor("Unknown J", "k") is None)

# 缓存读写 + ImpactFactorWorker 命中缓存不重复请求
_es_dir = tempfile.mkdtemp()
with patch.object(es_mod, "_cache_path",
                  return_value=Path(_es_dir) / "if_cache.json"):
    es_mod.save_if_cache({"nature": {"if": "9.4", "sci5": "11.2"}})
    cache = es_mod.load_if_cache()
    check("easyscholar 缓存读写", cache.get("nature", {}).get("if") == "9.4",
          repr(cache))
    _calls: list[str] = []

    def _fake_fetch(name, key):
        _calls.append(name)
        return None

    with patch.object(es_mod, "fetch_impact_factor", side_effect=_fake_fetch), \
            patch.object(es_mod, "REQUEST_DELAY", 0):
        _w = es_mod.ImpactFactorWorker([("c1", "Nature"), ("c2", "bogus")], "k")
        _w.run()  # 同步执行（QThread.run 直调，无事件循环依赖）
    check("easyscholar 缓存命中不耗请求", _calls == ["bogus"], repr(_calls))
shutil.rmtree(_es_dir, ignore_errors=True)

# ---------- 9j. run_paper_search filter_library 开关 ----------
_pool4 = [
    {"key": "K1", "title": "Brand new paper on wings", "doi": "10.9/new",
     "authors": "", "year": "2026", "collections": []},
]
_papers_nofilter = run_paper_search(
    "wing paper", client=None, pool=_pool4, limit=10,
    searcher=FakeMultiSource(), filter_library=False,
)
check("不过滤保留库内文献", len(_papers_nofilter) == 3, repr(_papers_nofilter))
_in_lib = {p.get("pmid") or p.get("arxiv_id"): p.get("in_library")
           for p in _papers_nofilter}
check("in_library 标注正确",
      _in_lib == {"90002": False, "90001": True, "2501.1": False}, repr(_in_lib))
_papers_filter = run_paper_search(
    "wing paper", client=None, pool=_pool4, limit=10,
    searcher=FakeMultiSource(), filter_library=True,
)
check("过滤模式仍剔除库内",
      sorted(p.get("pmid") or p.get("arxiv_id") for p in _papers_filter)
      == ["2501.1", "90002"], repr(_papers_filter))

# ---------- 9k. ScoutCard 影响因子 / 已在库中 / 翻译信号 ----------
_scard = ScoutCard({
    "id": "y@t", "topic": "方向", "added_at": "",
    "paper": {"title": "T2", "authors": "A", "year": "2024", "journal": "Nature",
              "abstract": "", "url": "", "pmid": "2", "in_library": True}})
_scard.set_impact("IF 9.5")
chip_texts = [(_scard._chips_layout.itemAt(i).widget().text()
               if _scard._chips_layout.itemAt(i).widget() is not None else "")
              for i in range(_scard._chips_layout.count())]
check("in_library chip", "已在库中" in chip_texts, repr(chip_texts))
check("IF chip 动态插入", _scard._if_chip is not None
      and _scard._if_chip.text() == "IF 9.5", repr(getattr(_scard, "_if_chip", None)))
_got_sig = []
_scard.translate_requested.connect(_got_sig.append)
_scard._on_translate_clicked()
check("翻译按钮发出信号", len(_got_sig) == 1 and _got_sig[0].get("pmid") == "2",
      repr(_got_sig))
_scard.set_translation("译文内容")
check("译文块展示", _scard._trans_label is not None
      and _scard._trans_label.text() == "译文内容")
_scard._toggle_translation()
check("收起译文", _scard._trans_box is None)

# ---------- 9z. 后台建库：结构化索引 / 两级接缝 / 预解析调度 ----------
from src.core.pdf_processor import (
    DOCLING_PARSER_VERSION, PDFProcessor, build_document_fast,
    find_cross_page_seams,
)
from src.core.library_preparser import LibraryPreparser, doc_state_is_parsed
from src.core.library_qa import extract_structured_chunks
from src.core.context_manager import ContextManager
import src.utils.config as _config_mod
from src.utils.config import get_page_cache_dir, load_doc_state, save_doc_state

tmp_pp = tempfile.mkdtemp(prefix="paperwb_preparse_test_")
_orig_load_config = _config_mod.load_config
_config_mod.load_config = lambda: {**_orig_load_config(), "data_root": tmp_pp}
try:
    # 测试 PDF：3 页空白页（文本只存在于页缓存 JSON —— 索引若非空必是走了结构化）
    pdf2 = os.path.join(tmp_pp, "seam.pdf")
    _d = fitz.open()
    for _ in range(3):
        _d.new_page()
    _d.save(pdf2)
    _d.close()

    def _el(eid, etype, text, page):
        return {"id": eid, "type": etype, "text": text,
                "bbox": [10, 10, 580, 800], "page": page}

    page_data = [
        {"page": 1, "elements": [
            _el("p1_t", "title", "Seam test paper about plumage", 1),
            _el("p1_h", "subtitle", "Methods", 1),
            _el("p1_a", "body",
                "We quantified feather barbs and measured melanin concentration "
                "across developmental stages in the dorsal", 1),
        ]},
        {"page": 2, "elements": [
            _el("p2_a", "body",
                "tract of each chick embryo to compare pigment deposition rates "
                "across successive weeks of feather growth.", 2),
            _el("p2_c", "body",
                "Having established this baseline we then examined regenerating "
                "follicles during the induced molting", 2),
        ]},
        {"page": 3, "elements": [
            _el("p3_d", "body",
                "cycle in the ventral feather tracts of adult birds to assess "
                "spatial differences in melanin deposition.", 3),
            _el("p3_r", "reference",
                "Smith J. et al. (2020) Plumage coloration review. J Birds 1:1-2.", 3),
        ]},
    ]
    seams = find_cross_page_seams(page_data)
    check("跨页接缝候选检测", len(seams) == 2, repr([s["key"] for s in seams]))

    cache_dir = str(get_page_cache_dir(pdf2))
    for pg in page_data:
        with open(os.path.join(cache_dir, f"page_{pg['page']:03d}.json"),
                  "w", encoding="utf-8") as f:
            _json.dump(pg, f)
    with open(os.path.join(cache_dir, "_manifest.json"), "w", encoding="utf-8") as f:
        _json.dump({
            "pdf_path": pdf2, "pdf_md5": "x", "total_pages": 3,
            "pdf_mtime": os.path.getmtime(pdf2),
            "pages": {"1": "done", "2": "done", "3": "done"},
            "created_at": 0.0, "updated_at": 0.0,
            "integration_version": 0, "parser": DOCLING_PARSER_VERSION,
        }, f)

    # --- 后台建库：prelim 模式（零 LLM） ---
    proc = PDFProcessor(pdf2, None)
    check("页缓存识别为完整", proc.is_stage1_complete)
    proc.start_stage2(preliminary=True)
    state = load_doc_state(pdf2)
    check("初步规则合并入独立缓存",
          len(state.get("merged_seams_prelim") or {}) == 2
          and not state.get("merged_seams"),
          repr(list((state.get("merged_seams_prelim") or {}).keys())))
    check("seams_final 标记待精修", state.get("seams_final") is False,
          repr(state.get("seams_final")))
    check("初步整合文档落盘",
          state.get("doc_format") == "fast"
          and bool(state.get("structured_document")))
    check("处理器 seams_mode=prelim", proc.seams_mode == "prelim")
    check("已解析判定（阅读/建库互通）", doc_state_is_parsed(pdf2))

    # --- 阅读定稿（无 API：规则合并直接定稿，与旧行为一致） ---
    proc2 = PDFProcessor(pdf2, None)
    proc2.start_stage2()
    state2 = load_doc_state(pdf2)
    check("无 API 阅读定稿", len(state2.get("merged_seams") or {}) == 2
          and state2.get("seams_final") is True, repr(state2.get("seams_final")))
    check("定稿接缝移出初步缓存",
          not (set(state2.get("merged_seams_prelim") or {})
               & set(state2.get("merged_seams") or {})))

    # --- LLM 精修：接受一半、否决一半（否决的初步接缝应被剔除） ---
    state3 = load_doc_state(pdf2)
    state3["merged_seams_prelim"] = dict(state3.pop("merged_seams") or {})
    state3["seams_final"] = False
    save_doc_state(pdf2, state3)
    proc3 = PDFProcessor(pdf2, None)
    proc3._seam_candidates = list(seams)
    proc3._integrated_doc = build_document_fast(
        page_data, state3["merged_seams_prelim"])
    accepted = {seams[0]["key"]: {
        "with_id": seams[0]["element_id_b"],
        "merged_text": seams[0]["text_a"] + " " + seams[0]["text_b"]}}
    proc3._on_seam_merge_done(accepted, preliminary=False)
    state4 = load_doc_state(pdf2)
    check("LLM 接受者入定稿缓存",
          seams[0]["key"] in (state4.get("merged_seams") or {}))
    check("LLM 否决者移出初步缓存",
          seams[1]["key"] not in (state4.get("merged_seams_prelim") or {}),
          repr(list((state4.get("merged_seams_prelim") or {}).keys())))
    check("精修后定稿标记", state4.get("seams_final") is True)

    # --- 结构化抽取：只取正文、带章节与页码、剔除参考文献 ---
    chs = extract_structured_chunks(pdf2)
    check("结构化抽取非空", bool(chs), repr(chs))
    body_all = " ".join(c["t"] for c in chs)
    check("结构化剔除参考文献", "Smith" not in body_all, repr(body_all[:120]))
    check("结构化带章节名", chs and all(c.get("s") == "Methods" for c in chs),
          repr(chs[:2]))
    check("结构化带页码", sorted(c["p"] for c in chs) == [1, 2, 3],
          repr([c["p"] for c in chs]))

    # --- 库问答索引优先结构化 + 摘要行/章节标注 ---
    item_pre = ZoteroItem(item_id=3, key="KPRE",
                          title="Seam test paper about plumage",
                          authors=["A, B"], year="2024", publication="", doi="",
                          abstract="melanin deposition abstract", pdf_path=pdf2)
    engine2 = LibraryQAEngine(index_dir=tmp_pp)
    engine2.set_items([item_pre])
    stats_e = engine2.refresh_fulltext([item_pre])
    check("索引优先结构化（空白 PDF 仍入库）", stats_e["chunks"] == 3, repr(stats_e))
    messages_e, refs_e = engine2.prepare_messages("melanin concentration methods")
    ctx_e = messages_e[-1]["content"]
    check("全文模式带摘要行", "摘要: melanin deposition abstract" in ctx_e,
          ctx_e[:200])
    check("证据行带章节标注", "· Methods" in ctx_e, ctx_e[:400])
    check("全文模式命中 KPRE", refs_e and refs_e[0]["key"] == "KPRE", repr(refs_e))

    # --- refresh_item / flush：单篇升级与批量收口 ---
    check("单篇刷新生效", engine2.refresh_item("KPRE") is True)
    engine2.flush()
    stats_f = LibraryQAEngine(index_dir=tmp_pp).refresh_fulltext([item_pre])
    check("flush 后状态可复用", stats_f["chunks"] == 3, repr(stats_f))
    engine2._ft_dirty = False
    engine2._building = True
    check("构建中单篇转待办", engine2.refresh_item("KPRE") is False
          and "KPRE" in engine2._refresh_pending)
    engine2._building = False
    engine2._refresh_pending.clear()

    # --- 预解析调度：队列过滤与失败记忆 ---
    pdf_other = os.path.join(tmp_pp, "other.pdf")
    _d2 = fitz.open()
    _d2.new_page()
    _d2.save(pdf_other)
    _d2.close()
    pre = LibraryPreparser()
    pre.set_queue([pdf2, pdf_other, os.path.join(tmp_pp, "missing.pdf")])
    check("队列过滤已解析/缺失", pre._pending == [pdf_other], repr(pre._pending))
    pre._record_error(pdf_other)
    pre2 = LibraryPreparser()
    pre2.set_queue([pdf_other])
    check("失败未变不重试", pre2._pending == [], repr(pre2._pending))
    os.utime(pdf_other)
    pre2.set_queue([pdf_other])
    check("失败后文件变化重试", pre2._pending == [pdf_other], repr(pre2._pending))

    # --- mtime 变化 → 结构化失效（回退 PyMuPDF 裸文本） ---
    os.utime(pdf2, (1_000_000_000, 1_000_000_000))
    check("mtime 变化结构化失效", extract_structured_chunks(pdf2) is None)

    # --- 单篇问答：参考文献智能修剪 ---
    class _CElem:
        def __init__(self, etype, text=""):
            self.element_type = etype
            self.text = text
            self.page = 1
            self.element_id = f"{etype}_1"
            self.section_name = ""
            self.image_caption = ""
            self.image_description = ""

    class _CDoc:
        def __init__(self):
            self.display_elements = [_CElem(
                "body", "The melanocyte pathway controls feather pigmentation "
                        "and coloration across avian species in development.")]
            self.metadata_pool = []
            self.figures = []
            self.references = [
                _CElem("reference", f"Smith {i} et al. (2020) Ref paper {i}")
                for i in range(5)]

    cm = ContextManager()
    cm.load_structured_doc(_CDoc())
    msgs_plain = cm.build_messages("这篇论文的主要结论是什么")
    check("普通问题省略参考文献",
          "完整参考文献列表本次未附" in msgs_plain[1]["content"]
          and "Ref paper 3" not in msgs_plain[1]["content"])
    msgs_ref = cm.build_messages("第[3]条引用的作者是谁")
    check("编号引用问题附全列表", "Ref paper 3" in msgs_ref[1]["content"])
    msgs_cn = cm.build_messages("请核对参考文献")
    check("中文引用词命中附全列表", "Ref paper 3" in msgs_cn[1]["content"])

    # --- retriever index_text：章节词参与打分但不污染命中 ---
    r3 = Bm25Retriever()
    r3.index([{"text": "the algorithm converges fast",
               "index_text": "Methods the algorithm converges fast",
               "s": "Methods"}])
    h3 = r3.search("methods", top_k=3)
    check("index_text 参与打分", bool(h3), repr(h3))
    check("命中不带评分冗余",
          bool(h3) and h3[0]["text"] == "the algorithm converges fast"
          and "index_text" not in h3[0], repr(h3))
    check("命中透传章节 s", bool(h3) and h3[0].get("s") == "Methods", repr(h3))
finally:
    _config_mod.load_config = _orig_load_config
    shutil.rmtree(tmp_pp, ignore_errors=True)

# ---------- 10. app 模块导入 ----------
import src.app  # noqa: F401,E402
check("src.app 导入", True)


# ---------- 19. 跨页整合/首页噪声/文本清洗规则回归 ----------
from src.core.pdf_processor import (
    _strip_watermarks,
    _is_front_matter_noise,
    _is_affiliation_block,
    _is_bare_number_text,
    _looks_like_continuation,
    _join_cross_page_text,
    _front_matter_block_ids,
    find_cross_page_seams,
    build_document_fast,
    FAST_DOCUMENT_VERSION,
    _is_author_line,
    _is_keywords_line,
    _looks_like_abstract,
    _is_article_title,
)


# 软连字符 + nbsp + fi/fl/ffi 断裂修复
check("清洗：软连字符合并", _strip_watermarks("inter\xad twined") == "intertwined")
check("清洗：nbsp 归一", _strip_watermarks("foo\u00a0bar") == "foo bar")
check("清洗：窄 nbsp 归一", _strip_watermarks("foo\u202fbar") == "foo bar")
check("清洗：ffi 修复", _strip_watermarks("di ffi cult task") == "difficult task")
check("清洗：ff 修复", _strip_watermarks("e ff ort") == "effort")
check("清洗：fi 修复", _strip_watermarks("fi bers of high quality") == "fibers of high quality")
check("清洗：fl 修复", _strip_watermarks("fl ower of fl ame") == "flower of flame")
check("清洗：保留 to fi gure",
      _strip_watermarks("pro fi ling to fi gure out") == "profiling to figure out")
check("清洗：AStudy 连写修复", _strip_watermarks("AStudy of Genetics") == "A Study of Genetics")
check("清洗：字母间空格连字符合并",
      _strip_watermarks("Hong -Hu Meng lived in broad -leaved forests") ==
      "Hong-Hu Meng lived in broad-leaved forests")
check("清洗：数字侧 - 不动",
      _strip_watermarks("see pages 3 - 5 for details") == "see pages 3 - 5 for details")
check("清洗：水印剥离",
      _strip_watermarks("ARTICLE IN PRESS This is real text") == "This is real text")

# 首页噪声规则
check("首页噪声：栏目标签 Review",
      _is_front_matter_noise("Review", 1, "subtitle") is True)
check("首页噪声：栏目标签 Research Article",
      _is_front_matter_noise("Research Article", 1, "body") is True)
check("首页噪声：栏目标签 Brief Communication",
      _is_front_matter_noise("Brief Communication", 1, "subtitle") is True)
check("首页噪声：复数通讯作者",
      _is_front_matter_noise("*Authors for correspondence. Hong-Hu Meng. E-mail: m@x.org",
                              1, "body") is True)
check("首页噪声：贡献声明",
      _is_front_matter_noise("†These authors contributed equally to this work.",
                              1, "body") is True)
check("首页噪声：日期行 Published",
      _is_front_matter_noise("Published: 29 March 2024", 1, "body") is True)
check("首页噪声：学术编辑",
      _is_front_matter_noise("Academic Editor: Ren & Kallies", 1, "body") is True)
check("首页噪声：多邮箱行",
      _is_front_matter_noise("liushk@ouc.edu.cn qili66@ouc.edu.cn",
                              1, "body") is True)
check("首页噪声：句尾有点的邮箱行（回归）",
      _is_front_matter_noise("jiajepeng@nwpu.edu.cn.", 1, "body") is True)
check("首页噪声：版权声明",
      _is_front_matter_noise("Copyright © 2007 by Annual Reviews. All rights reserved",
                              1, "body") is True)
check("首页噪声：DOI 行",
      _is_front_matter_noise("10.1146/annurev.ecolsys.37.091305.110014",
                              1, "body") is True)
check("首页噪声：摘要正文不被误删",
      _is_front_matter_noise(
          "Plants are unique because they evolved miRNA-mediated regulation "
          "two decades ago, and they play crucial roles in gene regulation.",
          1, "body") is False)
check("首页噪声：非首页不生效",
      _is_front_matter_noise("Review", 5, "subtitle") is False)

# 单位块识别
check("单位块：多机构编号段",
      _is_affiliation_block(
          "1 Department of Biology, University of Cambridge, Cambridge, UK. "
          "2 Institute of Molecular Medicine, University of Oxford, Oxford, UK.",
          1) is True)
check("单位块：纯邮编孤立单位行",
      _is_affiliation_block("Texas 77555, and Florida Medical Entomology Laboratory",
                              1) is True)
check("单位块：含正文散文保留",
      _is_affiliation_block(
          "1 Department of Biology, University of Cambridge. "
          "Furthermore, this study provides evidence for novel regulatory mechanisms.",
          1) is False)
check("单位块：中文不参与",
      _is_affiliation_block("1 北京市某某大学 计算机系，北京 100084。", 1) is False)
check("单位块：超过 1500 字符不参与", _is_affiliation_block(
    "1 " + ("Department of Biology, " * 200), 1) is False)
check("单位块：第 3 页不参与",
      _is_affiliation_block("1 Department of Biology, University of Cambridge.",
                              3) is False)

# 裸数字识别
check("裸数字：单数字", _is_bare_number_text("31") is True)
check("裸数字：双数字", _is_bare_number_text("123") is True)
check("裸数字：含字母拒判", _is_bare_number_text("3a") is False)
check("裸数字：长数字拒判", _is_bare_number_text("1234") is False)
check("裸数字：空字符串", _is_bare_number_text("") is False)

# 跨页续写判定
check("续写：连字符结尾", _looks_like_continuation(
    "prolif-", "erating cells in culture dishes") is True)
check("续写：英文小写开头",
      _looks_like_continuation(
          "Most miRNAs function by base-pairing with target mRNA transcripts",
          "leading to either translational repression or mRNA cleavage.")
      is True)
check("续写：英文大写开头但上段无终止标点",
      _looks_like_continuation(
          "In this study we report high-quality whole-genome sequencing of the Bactrian camel, dromedary and alpaca",
          "RNA-seq analyses revealed population structure")
      is True)
check("续写：缩写点不算句终",
      _looks_like_continuation(
          "Comparative analyses of functional gene categories, e.g.",
          "DNA repair pathways in mammals")
      is True)
check("续写：上段句终则拒判",
      _looks_like_continuation(
          "These findings close the argument.",
          "This is a new sentence.")
      is False)
check("续写：参考文献条目开头拒判",
      _looks_like_continuation(
          "This result demonstrates a strong phenotypic effect.",
          "Padian K, Chiappe LM. 1998. The origin and early evolution of birds.")
      is False)
check("续写：中文无大小写",
      _looks_like_continuation(
          "本研究通过多组学整合分析揭示了细胞异质性",
          "并构建了相关调控网络图谱。")
      is False)
check("续写：中文跨页续文",
      _looks_like_continuation(
          "本研究通过多组学整合分析揭示了细胞异质性与组织特异性",
          "我们进一步构建了相关调控网络图谱并开展了功能验证实验")
      is True)
check("续写：太短拒判",
      _looks_like_continuation("abc", "defg hijk lmno pqr") is False)
check("续写：图注占位行拒接",
      _looks_like_continuation(
          "creatinine kinase activity measured in the renal cortex",
          "(legend on next page)") is False)
check("续写：图注分段标签拒接",
      _looks_like_continuation(
          "We then assessed tissue integrity across all experimental groups",
          "(A) Blood feeding experimental design.") is False)
check("拼接：连字符去除",
      _join_cross_page_text("prolif-", "eration") == "proliferation")
check("拼接：普通空格拼接",
      _join_cross_page_text("hello", "world") == "hello world")

# 模板区块识别
_check_page = {
    "page": 1,
    "elements": [
        {"id": "p1_e1", "type": "title", "text": "Title here"},
        {"id": "p1_e2", "type": "subtitle", "text": "Highlights"},
        {"id": "p1_e3", "type": "body", "text": "D Np63 remodels epidermal chromatin"},
        {"id": "p1_e4", "type": "subtitle", "text": "Authors"},
        {"id": "p1_e5", "type": "body", "text": "John Doe, Jane Roe"},
        {"id": "p1_e6", "type": "body",
         "text": ("The hippocampus is a key brain region for spatial navigation, "
                  "and its interactions with the prefrontal cortex support "
                  "episodic memory consolidation across extended timescales, "
                  "as demonstrated by decades of lesion studies, neuroimaging "
                  "work, and invasive electrophysiology in rodents and primates "
                  "alike. This accumulating evidence has reshaped our "
                  "understanding of memory systems and their role in flexible "
                  "behavior, highlighting how distributed networks cooperate "
                  "across cortical and subcortical circuits to represent "
                  "space, time, and context in an integrated manner. Recent "
                  "computational models further propose that such integration "
                  "emerges from the recurrent dynamics of hippocampal place "
                  "cells and grid cells in the medial entorhinal cortex.")},
    ],
}
_block = _front_matter_block_ids([_check_page])
check("模板区块：Highlights/Authors 块全收",
      _block == {"p1_e2", "p1_e3", "p1_e4", "p1_e5"})
check("模板区块：长正文不参与", "p1_e6" not in _block)

# 首页 front matter 分类（作者行/标题/摘要/关键词）
check("作者行：编号上标", _is_author_line(
    "Wei-hang Geng 1,2,3† , Xiao-ping Wang 1† , Li-feng Che 4,5† , Xin Wang 1 , "
    "Rui Liu 1 , Tong Zhou 1 , Christian Roos 6 , David M. Irwin 7 and Li Yu 1 *",
    1) is True)
check("作者行：缩写名", _is_author_line(
    "Allen W. Zhang 1,2,3 , Ciara O'Flanagan 1 , Elizabeth A. Chase 4 ,",
    1) is True)
check("作者行：字母上标（PNAS）", _is_author_line(
    "Eliza Duvall a , Cecil M. Benitez b , Krissie Tellez b , Martin Enge c ,",
    1) is True)
check("作者行：无上标拒判", _is_author_line(
    "This study was funded by the National Institutes of Health and the "
    "Wellcome Trust across multiple laboratories.", 1) is False)
check("作者行：编号开头单位行拒判", _is_author_line(
    "1 Collaborative Innovation Center of Nanfan and High-Efficiency Tropical "
    "Agriculture, Hainan University, Haikou, China", 1) is False)
check("作者行：句终标点拒判", _is_author_line(
    "This research was conducted at the University of Cambridge, UK.", 1) is False)
check("作者行：非首页不参与", _is_author_line(
    "John Doe 1 , Jane Roe 2 ,", 3) is False)
check("关键词行：Key words", _is_keywords_line("Key words: expansion, loss", 1) is True)
check("关键词行：Keywords 无冒号", _is_keywords_line("Keywords expansion, loss", 1) is True)
check("关键词行：非关键词拒判", _is_keywords_line("Key findings include", 1) is False)
check("标题：长标题识别", _is_article_title(
    "scGPT: toward building a foundation model for single-cell multi-omics "
    "using generative AI", {"bbox": [0, 0, 580, 80]}, 800.0) is True)
check("标题：期刊名过短拒判", _is_article_title(
    "nature methods", {"bbox": [0, 0, 580, 40]}, 800.0) is False)
check("标题：编号小节拒判", _is_article_title(
    "1 Introduction", {"bbox": [0, 0, 580, 500]}, 800.0) is False)
check("标题：Methods 小节拒判", _is_article_title(
    "Methods", {"bbox": [0, 0, 580, 500]}, 800.0) is False)
check("摘要：散文信号词", _looks_like_abstract(
    "The research of phenotypic convergence is of increasing importance in "
    "adaptive evolution. We collected measurements of three phalangeal indices "
    "of manual digit III from 203 individuals of 122 species representing "
    "arboreal and terrestrial locomotory modes, and our analyses reveal "
    "convergent signals in the evolution of these modes.",
    [{"text": "x"}], 0, 1) is True)
check("摘要：编号单位开头拒判", _looks_like_abstract(
    "1 State Key Laboratory for Conservation and Utilization of Bio-Resources "
    "in Yunnan, School of Life Sciences, Yunnan University, Kunming, China, "
    "2 Kunming Institute of Zoology, Chinese Academy of Sciences, Kunming, "
    "China, 3 Shanxi Institute of Zoology, Xi'an, China",
    12, 0, 1) is False)
check("摘要：太短拒判", _looks_like_abstract("Short text.", 1, 0, 1) is False)

# 配对池过滤与接缝检测（契约：上一页末段 × 下一页首段配对）
_seam_page_data = [
    {
        "page": 1,
        "elements": [
            {"id": "p1_e1", "type": "body", "text":
                "this is a long paragraph that ends in the middle of a sentence about the data."},
            {"id": "p1_e2", "type": "body", "text": "OPEN ACCESS"},
            {"id": "p1_e3", "type": "body", "text": "31"},
            {"id": "p1_e4", "type": "body", "text":
                "1 Department of Biology, University of Cambridge, Cambridge, UK"},
            {"id": "p1_e5", "type": "body", "text":
                "Most miRNAs function by base-pairing with target mRNA transcripts"},
        ],
    },
    {
        "page": 2,
        "elements": [
            {"id": "p2_e1", "type": "body", "text":
                "leading to either translational repression or mRNA cleavage."},
            {"id": "p2_e2", "type": "body", "text":
                "OPEN ACCESS"},  # 不该作为页首正文
            {"id": "p2_e3", "type": "body", "text":
                "We then examined the downstream targets in vitro."},
        ],
    },
]
_seams = find_cross_page_seams(_seam_page_data)
check("接缝：页脚/单位块/裸数字不入配对池",
      all("OPEN ACCESS" not in (s["text_a"] + s["text_b"]) for s in _seams)
      and all("Department of Biology" not in (s["text_a"] + s["text_b"]) for s in _seams))
check("接缝：上一页末段+小写开头真续文被检出",
      any(s["element_id_a"] == "p1_e5" and s["element_id_b"] == "p2_e1"
          for s in _seams))

# build_document_fast 同页续写合并 + 单位块/裸数字不进 display
_small_pages = [
    {"page": 1, "elements": [
        {"id": "p1_e1", "type": "title", "text": "Sample title"},
        {"id": "p1_e2", "type": "authors",
         "text": "John Doe, Jane Roe"},
        {"id": "p1_e3", "type": "body",
         "text": "1 Department of Biology, University of Cambridge, Cambridge, UK"},
        {"id": "p1_e4", "type": "body",
         "text": "2 Department of Genetics, University of Oxford, Oxford, UK"},
        {"id": "p1_e5", "type": "body", "text": "31"},
        {"id": "p1_e6", "type": "body",
         "text": "We tested Garnett on a benchmark scRNA-seq dataset comprising 94,571 immunophenotyped peripheral blood mononuclear cells"},
        {"id": "p1_e7", "type": "body",
         "text": "(PBMCs), generated with the 10X Chromium platform."},
        {"id": "p1_e8", "type": "body",
         "text": "Here, we report a putative magnetic receptor (Drosophila CG8198)."},
    ]},
]
_doc = build_document_fast(_small_pages)
_display_types = [e.element_type for e in _doc.display_elements]
_display_texts = [e.text for e in _doc.display_elements]
check("组装：裸数字不进 display", "31" not in _display_texts)
check("组装：单位块渲染为 affiliations 卡",
      any(e.element_type == "affiliations"
          and "Department of Biology, University of Cambridge" in e.text
          for e in _doc.display_elements))
check("组装：单位块入 metadata_pool 作为 affiliations",
      any(e.element_type == "affiliations"
          and "Department of Biology, University of Cambridge" in e.text
          for e in _doc.metadata_pool))
check("组装：同页断段被合并（PBMCs 续句并入上段）",
      any("immunophenotyped peripheral blood mononuclear cells "
          "(PBMCs), generated with the 10X Chromium platform." in t
          for t in _display_texts))

# FAST_DOCUMENT_VERSION 自增验证（确保旧缓存自动失效）
check("FAST_DOCUMENT_VERSION 已升级",
      FAST_DOCUMENT_VERSION >= 6)


# ---------- 汇总 ----------
print()
for name in PASS:
    print(f"[PASS] {name}")
for name in FAIL:
    print(f"[FAIL] {name}")
print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
sys.exit(1 if FAIL else 0)
