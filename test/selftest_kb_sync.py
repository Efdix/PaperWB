# -*- coding: utf-8 -*-
"""写作知识库导出/导入核心逻辑自测（纯逻辑，无 GUI、无 LLM、无网络）。

覆盖：导出/导入 round-trip、清单校验、三种同名冲突策略、zip-slip 防护、
original_path 清洗、覆盖前备份、派生文件（草稿/润色历史/评价）迁移。
"""
import json
import shutil
import sys
import os
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.core.kb_sync import (
    CONFLICT_COPY, CONFLICT_OVERWRITE, CONFLICT_SKIP, FORMAT_NAME, FORMAT_VERSION,
    collect_sources, import_archive, list_packages, read_manifest,
    sanitize_profile_config, write_archive,
)
from src.core.writing_coach import validate_profile_name

PASS = []
FAIL = []


def check(name, cond, detail=""):
    if cond:
        PASS.append(name)
    else:
        FAIL.append(f"{name} {detail}")


class Tree:
    """临时数据根：模拟 {data_root}/.paperwb 下的各目录。"""

    def __init__(self, base: Path, make: bool = True):
        self.root = base
        if make:
            base.mkdir(parents=True, exist_ok=True)

    def mk_kb(self, name, papers=1, journal=0, drafts=False, history=False,
              reviews=False, text="正文", original_path=r"C:\Users\Other\Desktop\paper.pdf"):
        kb = self.root / "writing_kb" / name
        (kb / "personal_papers").mkdir(parents=True, exist_ok=True)
        if journal:
            (kb / "journal_papers").mkdir(parents=True, exist_ok=True)
        cfg = {
            "name": name,
            "writing_type": "综述",
            "created_at": "2026-01-01T00:00:00",
            "updated_at": "2026-01-01T00:00:00",
            "personal_papers": [
                {"filename": f"p{i}.pdf", "original_path": original_path,
                 "text": f"{text}{i}"} for i in range(papers)
            ],
            "journal_papers": [
                {"filename": f"j{i}.pdf", "original_path": original_path,
                 "text": f"期刊{i}"} for i in range(journal)
            ],
            "writing_habits": {"tone_voice": "academic"} if papers else None,
            "journal_style": {"citation_format": "[1]"} if journal else None,
        }
        (kb / "config.json").write_text(json.dumps(cfg, ensure_ascii=False),
                                        encoding="utf-8")
        for i in range(papers):
            (kb / "personal_papers" / f"p{i}.pdf.txt").write_text(
                f"{text}{i}", encoding="utf-8")
        for i in range(journal):
            (kb / "journal_papers" / f"j{i}.pdf.txt").write_text(
                f"期刊{i}", encoding="utf-8")
        if drafts:
            (self.root / "drafts").mkdir(parents=True, exist_ok=True)
            (self.root / "drafts" / f"{name}.txt").write_text("我的草稿", encoding="utf-8")
        if history:
            (self.root / "polish_history").mkdir(parents=True, exist_ok=True)
            (self.root / "polish_history" / f"{name}.json").write_text(
                json.dumps([{"before": "a"}], ensure_ascii=False), encoding="utf-8")
        if reviews:
            (self.root / "reviews").mkdir(parents=True, exist_ok=True)
            (self.root / "reviews" / f"{name}.json").write_text(
                json.dumps({"overall_grade": "B"}), encoding="utf-8")

    def dirs(self):
        return dict(
            kb_dir=self.root / "writing_kb",
            drafts_dir=self.root / "drafts",
            history_dir=self.root / "polish_history",
            reviews_dir=self.root / "reviews",
        )


def export(tree: Tree, names, dest, drafts=False, history=False, reviews=False):
    srcs = collect_sources(tree.dirs()["kb_dir"])
    for s in srcs:
        if s.name in names:
            s.include_drafts = drafts
            s.include_history = history
            s.include_reviews = reviews
    return write_archive([s for s in srcs if s.name in names], dest, **tree.dirs())


def main():
    tmp = Path(tempfile.mkdtemp(prefix="paperwb_kbsync_"))
    try:
        src = Tree(tmp / "src")
        dst = Tree(tmp / "dst")

        src.mk_kb("库A", papers=2, journal=1, drafts=True, history=True, reviews=True)
        src.mk_kb("库B", papers=1)

        # ---------- 1. 扫描与体积 ----------
        srcs = collect_sources(src.dirs()["kb_dir"])
        check("扫描知识库", len(srcs) == 2, [s.name for s in srcs])
        a = next(s for s in srcs if s.name == "库A")
        check("统计范文数", a.personal_count == 2 and a.journal_count == 1, a)
        check("统计分析结果", a.has_writing_habits and a.has_journal_style, a)
        check("体积估算非零", a.size > 0, a.size)

        # ---------- 2. 导出与清单 ----------
        zip_path = tmp / "kb.zip"
        res = export(src, ["库A", "库B"], zip_path,
                     drafts=True, history=True, reviews=True)
        check("导出文件生成", zip_path.exists() and res["files"] > 0, res)
        check("导出库计数", res["profiles"] == 2, res["profiles"])

        manifest = read_manifest(zip_path)
        check("清单格式名", manifest["format"] == FORMAT_NAME)
        check("清单版本", manifest["format_version"] == FORMAT_VERSION)
        check("清单含两库", set(manifest["profiles"]) == {"库A", "库B"},
              list(manifest["profiles"]))
        check("清单标记草稿", manifest["profiles"]["库A"]["has_drafts"] is True)
        check("清单标记评价", manifest["profiles"]["库A"]["has_reviews"] is True)
        check("清单标记期刊", manifest["profiles"]["库A"]["has_journal_style"] is True)

        with zipfile.ZipFile(zip_path) as zf:
            names = zf.namelist()
        check("包含 manifest", "manifest.json" in names)
        check("镜像 kb 目录", "writing_kb/库A/config.json" in names)
        check("包含范文 txt", "writing_kb/库A/personal_papers/p0.pdf.txt" in names)
        check("包含草稿", "drafts/库A.txt" in names)
        check("包含润色历史", "polish_history/库A.json" in names)
        check("包含评价", "reviews/库A.json" in names)
        check("不含 _last_profile", not any("_last_profile" in n for n in names))

        # ---------- 3. 导入预览 ----------
        pkgs = list_packages(zip_path, kb_dir=dst.dirs()["kb_dir"])
        check("预览两库", len(pkgs) == 2, [p.name for p in pkgs])
        check("预览无冲突", not any(p.exists_locally for p in pkgs))

        # ---------- 4. 空目录导入 ----------
        rep = import_archive(zip_path, ["库A", "库B"], **dst.dirs())
        check("导入两库", sorted(rep.imported) == ["库A", "库B"], rep.imported)
        check("导入无失败", not rep.failed, rep.failed)
        check("草稿已还原", (dst.dirs()["drafts_dir"] / "库A.txt").exists())
        check("润色历史已还原", (dst.dirs()["history_dir"] / "库A.json").exists())
        check("评价已还原", (dst.dirs()["reviews_dir"] / "库A.json").exists())
        check("范文 txt 已还原",
              (dst.dirs()["kb_dir"] / "库A" / "personal_papers" / "p0.pdf.txt").exists())

        cfg = json.loads((dst.dirs()["kb_dir"] / "库A" / "config.json")
                         .read_text(encoding="utf-8"))
        check("original_path 置空", cfg["personal_papers"][0]["original_path"] == "",
              cfg["personal_papers"][0])
        check("范文全文保留", cfg["personal_papers"][0]["text"] == "正文0")
        check("写作习惯保留", cfg["writing_habits"] == {"tone_voice": "academic"})
        check("期刊格式保留", cfg["journal_style"] == {"citation_format": "[1]"})
        check("库名正确", cfg["name"] == "库A")

        # ---------- 5. 同名冲突：跳过 ----------
        (dst.dirs()["kb_dir"] / "库A" / "config.json").write_text(
            json.dumps({"name": "库A", "personal_papers": [], "marker": "local"},
                       ensure_ascii=False), encoding="utf-8")
        rep = import_archive(zip_path, ["库A"], conflict_policy=CONFLICT_SKIP,
                             **dst.dirs())
        check("跳过策略-记入 skipped", rep.skipped == ["库A"], rep.skipped)
        check("跳过策略-未改动本地",
              json.loads((dst.dirs()["kb_dir"] / "库A" / "config.json")
                         .read_text(encoding="utf-8")).get("marker") == "local")
        check("跳过策略-无备份", not rep.backup_dir, rep.backup_dir)

        # ---------- 6. 同名冲突：覆盖（先备份） ----------
        rep = import_archive(zip_path, ["库A"], conflict_policy=CONFLICT_OVERWRITE,
                             **dst.dirs())
        check("覆盖策略-记入 overwritten", "库A" in rep.overwritten, rep.overwritten)
        check("覆盖策略-生成备份目录", bool(rep.backup_dir), rep.backup_dir)
        backup = Path(rep.backup_dir)
        check("备份含旧 config", (backup / "writing_kb" / "库A" / "config.json").exists())
        check("备份含旧草稿", (backup / "drafts" / "库A.txt").exists())
        new_cfg = json.loads((dst.dirs()["kb_dir"] / "库A" / "config.json")
                             .read_text(encoding="utf-8"))
        check("覆盖后为新内容", new_cfg.get("marker") is None
              and len(new_cfg.get("personal_papers", [])) == 2, new_cfg.get("marker"))
        check("覆盖后无重复计数", rep.imported == [], rep.imported)

        # ---------- 7. 同名冲突：存为副本 ----------
        rep = import_archive(zip_path, ["库A"], conflict_policy=CONFLICT_COPY,
                             **dst.dirs())
        check("副本策略-记录重命名", rep.renamed == [("库A", "库A_导入")], rep.renamed)
        check("副本策略-新目录存在",
              (dst.dirs()["kb_dir"] / "库A_导入" / "config.json").exists())
        check("副本策略-原库未动",
              (dst.dirs()["kb_dir"] / "库A" / "config.json").exists())
        # 再次导入同名副本 → 递增后缀
        rep2 = import_archive(zip_path, ["库A"], conflict_policy=CONFLICT_COPY,
                              **dst.dirs())
        check("副本策略-后缀递增", rep2.renamed == [("库A", "库A_导入2")], rep2.renamed)

        # ---------- 8. 备份目录不被当作知识库 ----------
        names_after = [s.name for s in collect_sources(dst.dirs()["kb_dir"])]
        check("备份目录不进扫描", ".backup" not in names_after, names_after)

        # ---------- 9. zip-slip 防护 ----------
        evil = tmp / "evil.zip"
        with zipfile.ZipFile(evil, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "format": FORMAT_NAME, "format_version": FORMAT_VERSION,
                "profiles": {"恶库": {"personal_count": 0, "journal_count": 0}},
            }, ensure_ascii=False))
            zf.writestr("writing_kb/恶库/config.json", "{}")
            zf.writestr("../../evil.txt", "pwned")
            zf.writestr("writing_kb/恶库/../../../evil2.txt", "pwned")
        evil_dst = Tree(tmp / "evil_dst")
        rep = import_archive(evil, ["恶库"], **evil_dst.dirs())
        check("zip-slip-导入正常完成", rep.imported == ["恶库"], rep.imported)
        check("zip-slip-未写出上级目录",
              not (tmp / "evil.txt").exists() and not (tmp / "evil2.txt").exists())
        check("zip-slip-仅写入目标目录",
              (evil_dst.dirs()["kb_dir"] / "恶库" / "config.json").exists())

        # ---------- 10. 清单校验 ----------
        not_pkg = tmp / "random.zip"
        with zipfile.ZipFile(not_pkg, "w") as zf:
            zf.writestr("hello.txt", "hi")
        try:
            read_manifest(not_pkg)
            check("非本应用包被拒", False)
        except ValueError:
            check("非本应用包被拒", True)

        future = tmp / "future.zip"
        with zipfile.ZipFile(future, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "format": FORMAT_NAME, "format_version": FORMAT_VERSION + 99,
                "profiles": {},
            }))
        try:
            read_manifest(future)
            check("高版本包被拒", False)
        except ValueError as e:
            check("高版本包被拒", "版本" in str(e), str(e))

        # ---------- 11. 库名校验（导入侧） ----------
        bad_pkg = tmp / "bad_name.zip"
        bad_name = "非法:名字"
        with zipfile.ZipFile(bad_pkg, "w") as zf:
            zf.writestr("manifest.json", json.dumps({
                "format": FORMAT_NAME, "format_version": FORMAT_VERSION,
                "profiles": {bad_name: {"personal_count": 0, "journal_count": 0}},
            }, ensure_ascii=False))
            zf.writestr(f"writing_kb/{bad_name}/config.json", "{}")
        rep = import_archive(bad_pkg, [bad_name], **Tree(tmp / "bad_dst").dirs())
        check("非法库名被拒", bool(rep.failed) and not rep.imported, rep)

        # ---------- 12. sanitize_profile_config 幂等且不破坏结构 ----------
        data = {"personal_papers": [{"filename": "a.pdf", "original_path": "/x/a.pdf",
                                     "text": "t", "extra": 1}],
                "journal_papers": "not-a-list"}
        out = sanitize_profile_config(dict(data))
        check("清洗保留 filename/text",
              out["personal_papers"][0]["filename"] == "a.pdf"
              and out["personal_papers"][0]["text"] == "t")
        check("清洗置空 original_path",
              out["personal_papers"][0]["original_path"] == "")
        check("清洗丢弃未知键", "extra" not in out["personal_papers"][0])
        check("清洗容忍异常类型", out["journal_papers"] == "not-a-list")

        # ---------- 13. 空选择不崩 ----------
        rep = import_archive(zip_path, [], **Tree(tmp / "empty_dst").dirs())
        check("空选择返回空报告", not rep.changed and not rep.failed, rep)

        # ---------- 14. validate_profile_name 契约 ----------
        for good in ("正常库名", "KB-2026_v2", "带 空格 的名字"):
            try:
                validate_profile_name(good)
                ok = True
            except ValueError:
                ok = False
            check(f"合法名通过: {good}", ok)
        for bad in ("含:冒号", "含/斜杠", "含\\反斜杠", " 首空格", "尾空格 ",
                    "换行\n符", ""):
            try:
                validate_profile_name(bad)
                ok = False
            except ValueError:
                ok = True
            check(f"非法名被拒: {bad!r}", ok)

    finally:
        shutil.rmtree(tmp, ignore_errors=True)


main()

print()
for name in PASS:
    print("PASS", name)
for name in FAIL:
    print("FAIL", name)
print()
print(f"通过 {len(PASS)} / 失败 {len(FAIL)}")
sys.exit(1 if FAIL else 0)
