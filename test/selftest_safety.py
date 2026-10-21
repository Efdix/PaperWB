# -*- coding: utf-8 -*-
"""数据安全回归：原子写、损坏备份、知识库路径、zip-slip、Word 原子写回。

这些用例对应审计中“会丢用户数据/可被恶意包利用”的路径。全部使用临时目录，
无 LLM、无网络、无真实 Zotero。
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.utils.atomic import (  # noqa: E402
    atomic_write_json, atomic_write_text, backup_corrupt_file, is_path_inside,
    read_json_safe,
)
from src.core import kb_sync as kb  # noqa: E402
from src.core.writing_coach import validate_profile_name  # noqa: E402

FAILS: list[str] = []
COUNT = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(name)


def test_atomic() -> None:
    with tempfile.TemporaryDirectory() as td:
        p = Path(td) / "state.json"
        atomic_write_json(p, {"a": 1, "中文": "值"})
        check("原子写落盘", json.loads(p.read_text(encoding="utf-8"))["中文"] == "值")
        check("无临时文件残留", not list(Path(td).glob(".*.tmp")), str(list(Path(td).iterdir())))

        p.write_text("{broken", encoding="utf-8")
        data = read_json_safe(p, {"fallback": True})
        check("损坏 JSON 返回默认值", data == {"fallback": True})
        corrupt = list(Path(td).glob("state.json.corrupt-*"))
        check("损坏文件被备份保留", len(corrupt) == 1, str(corrupt))

        atomic_write_text(Path(td) / "draft.txt", "正文")
        check("文本原子写", (Path(td) / "draft.txt").read_text(encoding="utf-8") == "正文")


def test_profile_names() -> None:
    for bad in (".", "..", "CON", "NUL", "a.", "含/斜杠", "含:冒号", " 首空格", "尾空格 "):
        try:
            validate_profile_name(bad)
            ok = False
        except ValueError:
            ok = True
        check(f"非法知识库名被拒：{bad!r}", ok)
    for good in ("正常库", "KB-2026_v2", "带 空格 的名字"):
        try:
            validate_profile_name(good)
            ok = True
        except ValueError:
            ok = False
        check(f"合法知识库名通过：{good!r}", ok)


def _make_pkg(path: Path, name: str = "foo") -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(kb.MANIFEST_NAME, json.dumps({
            "format": kb.FORMAT_NAME,
            "format_version": kb.FORMAT_VERSION,
            "profiles": {name: {"writing_type": "综述", "personal_count": 0,
                                "journal_count": 0}},
            "files": [f"writing_kb/{name}/config.json",
                      f"drafts/{name}.txt"],
        }, ensure_ascii=False))
        zf.writestr(f"writing_kb/{name}/config.json", json.dumps({
            "name": name, "writing_type": "综述",
            "personal_papers": [{"filename": "x.pdf", "original_path": "D:/old.pdf", "text": "t"}],
        }, ensure_ascii=False))
        zf.writestr(f"drafts/{name}.txt", "draft-body")


def test_kb_copy() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "writing_kb"
        root.mkdir()
        (root / "foo").mkdir()
        (root / "foo" / "config.json").write_text(
            json.dumps({"name": "foo", "writing_type": "综述"}), encoding="utf-8")

        z = Path(td) / "pkg.zip"
        _make_pkg(z)
        rep = kb.import_archive(
            z, ["foo"], conflict_policy=kb.CONFLICT_COPY,
            kb_dir=root, drafts_dir=Path(td) / "drafts",
            history_dir=Path(td) / "history", reviews_dir=Path(td) / "reviews")
        check("副本导入成功", rep.renamed == [("foo", "foo_导入")] and not rep.failed,
              f"renamed={rep.renamed} failed={rep.failed}")
        cfg = json.loads((root / "foo_导入" / "config.json").read_text(encoding="utf-8"))
        check("副本 config.name 跟随新目录名", cfg.get("name") == "foo_导入", cfg.get("name"))
        check("副本 original_path 已清空",
              cfg["personal_papers"][0]["original_path"] == "")
        check("副本草稿按新库名落盘",
              (Path(td) / "drafts" / "foo_导入.txt").exists())
        check("原库 config 未被改写",
              json.loads((root / "foo" / "config.json").read_text(encoding="utf-8"))["name"] == "foo")

        # 恶意库名不得进入预览或写入
        bad = Path(td) / "bad.zip"
        _make_pkg(bad, "..")
        pkgs = kb.list_packages(bad, kb_dir=root)
        check("恶意库名不进导入预览", all(p.name != ".." for p in pkgs), str([p.name for p in pkgs]))
        rep2 = kb.import_archive(bad, [".."], conflict_policy=kb.CONFLICT_OVERWRITE, kb_dir=root)
        check("恶意库名导入被拒", bool(rep2.failed), str(rep2.failed))


def test_zip_slip() -> None:
    bad = [
        "../evil.txt",
        "/abs.txt",
        "\\abs.txt",
        "writing_kb/foo/C:/Users/x/evil.exe",
        "writing_kb/foo/a.txt:stream",
        "writing_kb/foo/../../x",
    ]
    for name in bad:
        check(f"zip-slip 拒绝：{name}", not kb._is_safe_arcname(name))
    for name in ("manifest.json", "writing_kb/foo/config.json", "drafts/foo.txt"):
        check(f"zip-slip 放行：{name}", kb._is_safe_arcname(name))

    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        check("路径包含判断：子目录", is_path_inside(root / "a" / "b", root))
        check("路径包含判断：父目录被拒", not is_path_inside(root / ".." / "x", root))


def test_docx_atomic() -> None:
    from docx import Document
    from src.core.docx_io import read_docx, write_docx
    with tempfile.TemporaryDirectory() as td:
        path = Path(td) / "paper.docx"
        doc = Document()
        doc.add_paragraph("原始正文")
        doc.save(str(path))

        # 目标只读：临时文件写入成功，但 os.replace 必须失败；原文件保持完整。
        os.chmod(path, stat.S_IREAD)
        failed = False
        try:
            write_docx(path, ["被拒绝的新正文"])
        except Exception:
            failed = True
        finally:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        check("只读目标写回失败", failed)
        check("失败后原稿未被截断", read_docx(path).to_plain_text().strip() == "原始正文",
              read_docx(path).to_plain_text())
        check("失败后无临时文件", not list(Path(td).glob(".*.tmp")), str(list(Path(td).iterdir())))


def test_doc_id_migration() -> None:
    from src.utils import config as cfg
    p1 = "C:/Papers/Paper.PDF"
    p2 = "c:\\papers\\paper.pdf"
    check("路径规范化后文档 ID 一致", cfg._doc_id(p1) == cfg._doc_id(p2),
          f"{cfg._doc_id(p1)} != {cfg._doc_id(p2)}")

    with tempfile.TemporaryDirectory() as td:
        base = Path(td)
        legacy = base / f"{cfg._legacy_doc_id(p1)}.json"
        legacy.write_text('{"old": true}', encoding="utf-8")
        f = cfg._doc_file(base, p1)
        check("旧缓存键迁移到新键", f.exists() and not legacy.exists(), str(f))
        check("迁移后内容不变",
              json.loads(f.read_text(encoding="utf-8")) == {"old": True})


def test_paper_identity() -> None:
    from src.core.literature_scout import paper_key
    a = {"doi": "https://doi.org/10.1000/ABC", "title": "A paper"}
    b = {"doi": "10.1000/abc", "pmid": "12345", "title": "A paper"}
    check("跨源 DOI 身份一致", paper_key(a) == paper_key(b) == "doi:10.1000/abc",
          f"{paper_key(a)} / {paper_key(b)}")
    c = {"title": "A  Paper!", "pmid": ""}
    d = {"title": "a paper", "pmid": ""}
    check("无 DOI 时标题兜底一致", paper_key(c) == paper_key(d) == "title:apaper",
          f"{paper_key(c)} / {paper_key(d)}")


def test_state_meta() -> None:
    from src.utils.config import _state_meta
    meta = _state_meta({
        "doc_format": "fast", "fast_version": 10, "pdf_size": 1,
        "pdf_mtime_ns": 2, "seams_final": True,
        "structured_document": {"display_elements": [{"text": "x" * 1000}]},
    })
    check("状态摘要含版本与指纹",
          meta["doc_format"] == "fast" and meta["pdf_size"] == 1
          and meta["has_structured_document"] is True)
    check("状态摘要不携带正文", "structured_document" not in meta
          and "display_elements" not in str(meta))


def test_theme_tokens() -> None:
    from src.ui.styles import STYLESHEET
    from src.ui.tokens import PRIMARY, SURFACE
    check("QSS 无遗留 $ 占位符", "$" not in STYLESHEET)
    check("QSS 主色来自 token", PRIMARY in STYLESHEET)
    check("QSS 背景色来自 token", SURFACE in STYLESHEET)


def test_context_and_manifest() -> None:
    from src.core.context_manager import ContextManager
    from src.core.pdf_processor import PageManifest

    cm = ContextManager(10_000_000)
    check("上下文窗口被钳制", cm.max_tokens == ContextManager.MAX_CONTEXT_TOKENS, cm.max_tokens)
    check("中文 token 估算", cm.estimate_tokens("中" * 1000) == 1000,
          str(cm.estimate_tokens("中" * 1000)))
    check("英文 token 估算", 240 <= cm.estimate_tokens("a" * 1000) <= 260,
          str(cm.estimate_tokens("a" * 1000)))

    m = PageManifest(pdf_path="x.pdf", pdf_md5="h", total_pages=2,
                     pdf_mtime=1.0, pdf_size=123, pdf_mtime_ns=456,
                     pages={1: "done", 2: "pending"})
    m2 = PageManifest.from_dict(m.to_dict())
    check("manifest 内容指纹往返", (m2.pdf_size, m2.pdf_mtime_ns) == (123, 456),
          f"{m2.pdf_size}/{m2.pdf_mtime_ns}")


def main() -> int:
    for fn in (test_atomic, test_profile_names, test_kb_copy, test_zip_slip,
               test_docx_atomic, test_doc_id_migration, test_paper_identity,
               test_state_meta, test_theme_tokens, test_context_and_manifest):
        print(f"\n--- {fn.__name__} ---")
        fn()
    print(f"\n{COUNT - len(FAILS)} passed, {len(FAILS)} failed")
    if FAILS:
        print("FAILED:", FAILS)
        return 1
    print("ALL OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
