# -*- coding: utf-8 -*-
"""批量解析质量评估 —— 枚举 Zotero 库 PDF，跑两阶段管线（无 LLM），逐篇出卡片质量报告。

开发/回归用。对每篇文献执行与产品一致的解析路径（PDFProcessor：
Stage 1 走已有 page_cache 或 Docling 全新解析，Stage 2 规则组装），
随后输出卡片流前若干张、类型分布、front matter 完整性、目录污染、
超大混排正文块等质量指标。

注意：会按当前 FAST_DOCUMENT_VERSION 重写 states 缓存
（等同用户打开一次文献，page_cache 不动）。

用法:
    python test/eval_parse_quality.py --count 20 --out test/_eval/round1.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from src.utils.config import get_page_cache_root_dir, load_config
from src.core.pdf_processor import PDFProcessor, FAST_DOCUMENT_VERSION

_FRONT_LABEL_RE = re.compile(
    r"^(?:edited\s*by|reviewed\s*by|specialty\s*section|citation|"
    r"highlights?|authors?(?:\s+list)?|for\s+correspondence|correspondence|"
    r"academic\s+editor|reviewing\s+editor)\s*:?\s*$", re.IGNORECASE)

CARD_SHOW = 10      # 每篇展示前 N 张卡片
BLOB_WORDS = 1800   # 超过该词数的正文卡视为「混排大块」


def _cached_papers() -> list[dict]:
    """已有完整 page_cache + 有效 manifest 的文献（stage2 可秒级重跑）。"""
    out: list[dict] = []
    root = get_page_cache_root_dir()
    if not root.exists():
        return out
    for doc_dir in sorted(root.iterdir()):
        mp = doc_dir / "_manifest.json"
        if not mp.exists():
            continue
        try:
            manifest = json.loads(mp.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        pdf = manifest.get("pdf_path", "")
        if not pdf or not os.path.isfile(pdf):
            continue
        pages = manifest.get("pages") or {}
        total = int(manifest.get("total_pages", 0) or 0)
        if total <= 0 or sum(1 for s in pages.values() if s in ("done", "error")) < total:
            continue
        out.append({"pdf": pdf})
    return out


def _library_papers() -> list[dict]:
    """Zotero 库中带 PDF 的文献（用于补足未解析过的样本）。"""
    out: list[dict] = []
    cfg = load_config()
    zdir = cfg.get("zotero_data_dir", "")
    if not zdir:
        return out
    from src.core.zotero_parser import ZoteroLibrary
    try:
        lib = ZoteroLibrary(zdir)
        if lib.reload() == 0:
            return out
        for it in lib.get_all_items():
            p = getattr(it, "pdf_path", "")
            if p and os.path.isfile(p):
                out.append({"pdf": p})
    except Exception as e:  # noqa: BLE001
        print(f"[warn] Zotero 读取失败: {e}")
    return out


def _run_processor(app: QApplication, pdf: str,
                   stage1_timeout: float = 600.0) -> dict | None:
    """同步驱动一次完整两阶段解析，返回结构化文档 dict（失败返回 None）。"""
    proc = PDFProcessor(pdf, None)
    events: dict = {}

    def _on_stage2(path, doc):
        events["stage2_ok"] = True

    proc.stage2_finished.connect(_on_stage2)
    proc.stage1_error.connect(lambda *a: events.setdefault("stage1_err", a[2]))
    proc.stage2_error.connect(lambda *a: events.setdefault("stage2_err", a[1]))

    if not proc.is_stage1_complete:
        proc.start_stage1()
        deadline = time.monotonic() + stage1_timeout
        while time.monotonic() < deadline:
            app.processEvents()
            if proc.is_stage1_complete:
                break
            time.sleep(0.05)
        else:
            proc.cancel()
            return {"error": "stage1 timeout"}
        if events.get("stage1_err"):
            proc.cancel()
            return {"error": events["stage1_err"]}

    proc.start_stage2(preliminary=False)
    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        app.processEvents()
        if events["stage2_ok"]:
            break
        time.sleep(0.05)
    else:
        proc.cancel()
        return {"error": "stage2 timeout"}
    # 无 LLM 时接缝规则合并与缓存落盘在同一调用栈内；稍等收尾
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        app.processEvents()
        if not proc.is_busy:
            break
        time.sleep(0.05)
    doc = proc.cached_document
    if doc is None:
        return {"error": "no document"}
    return doc.to_dict()


def _evaluate(doc: dict, pdf: str) -> dict:
    el = doc.get("display_elements") or []
    toc = doc.get("toc") or []
    types: dict[str, int] = {}
    for e in el:
        t = e.get("element_type", "?")
        types[t] = types.get(t, 0) + 1
    bodies = [e for e in el if e.get("element_type") == "body"]
    big = [e for e in bodies if len((e.get("text") or "").split()) > BLOB_WORDS]
    return {
        "pdf": os.path.basename(pdf),
        "title": (doc.get("title") or "")[:70],
        "n": len(el),
        "types": types,
        "toc_junk": [t.get("title") for t in toc
                     if _FRONT_LABEL_RE.match(str(t.get("title") or ""))],
        "max_body_words": max((len((e.get("text") or "").split()) for e in bodies),
                              default=0),
        "blobs": [str(e.get("element_id")) for e in big],
        "front_cards": [
            {"t": e.get("element_type"), "id": e.get("element_id"),
             "s": (e.get("text") or "")[:60]}
            for e in el[:CARD_SHOW]
        ],
    }


def _print_paper(idx: int, r: dict) -> None:
    print(f"\n===== [{idx}] {r['pdf']}")
    print(f"    标题: {r['title']}")
    if r.get("error"):
        print(f"    ERROR: {r['error']}")
        return
    print(f"    卡片数: {r['n']} | 类型分布: {r['types']}")
    print(f"    最大正文卡词数: {r['max_body_words']} | 混排大块: {r['blobs'] or '无'}")
    print(f"    目录污染: {r['toc_junk'] or '无'}")
    for c in r["front_cards"]:
        print(f"      [{c['t']:<14}] {c['id']:<10} {c['s']}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--count", type=int, default=20)
    ap.add_argument("--out", default="", help="报告另存路径（JSON）")
    args = ap.parse_args()

    app = QApplication.instance() or QApplication([])

    cached = _cached_papers()
    lib = _library_papers()
    have = {p["pdf"] for p in cached}
    fresh = [p for p in lib if p["pdf"] not in have]
    sample = cached[: max(args.count, 20)]
    if len(sample) < args.count:
        sample += fresh[: args.count - len(sample)]

    print(f"样本 {len(sample)} 篇（已有页缓存 {sum(1 for p in sample if p['pdf'] in have)}，"
          f"需全新解析 {sum(1 for p in sample if p['pdf'] not in have)}）"
          f" | FAST_DOCUMENT_VERSION={FAST_DOCUMENT_VERSION}")
    print("=" * 78)

    results: list[dict] = []
    for i, p in enumerate(sample, 1):
        t0 = time.monotonic()
        doc = _run_processor(app, p["pdf"])
        r = _evaluate(doc, p["pdf"]) if doc else {
            "pdf": os.path.basename(p["pdf"]), "title": "", "error": "failed"}
        r["secs"] = round(time.monotonic() - t0, 1)
        results.append(r)
        _print_paper(i, r)

    print("\n" + "=" * 78)
    print("== 汇总（CSV: 序号,文件名,卡片数,最大正文词数,混排大块数,目录污染数,秒数）")
    for i, r in enumerate(results, 1):
        err = r.get("error", "")
        if err:
            print(f"{i},{r['pdf']},ERROR:{err},{r.get('secs')}")
            continue
        print(f"{i},{r['pdf']},{r['n']},{r['max_body_words']},"
              f"{len(r['blobs'])},{len(r['toc_junk'])},{r['secs']}")

    if args.out:
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(
            json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"\n报告已存: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
