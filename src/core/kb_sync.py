"""写作知识库导出/导入 —— 跨机迁移与同步，纯本地零 LLM。

导出为单个 zip 包，导入时按库名还原。知识库是自包含的：范文全文
内联在 ``config.json`` 的 ``text`` 字段，另有 ``personal_papers/``、
``journal_papers/`` 下的 ``.txt`` 副本，因此整个目录拷到另一台机器即可
使用，无需重建（重建要重新烧 LLM token 做风格分析）。

可选一并迁移知识库派生的数据：编辑器草稿（drafts）、润色历史
（polish_history）、草稿整体评价（reviews）——三者都按 ``<库名>.<ext>`` 命名。

包结构（镜像源目录，零信息损失）::

    manifest.json                     # 格式版本、导出时间、各库概要、文件清单
    writing_kb/<库名>/config.json
    writing_kb/<库名>/{personal,journal}_papers/*.txt
    drafts/<库名>.txt                 # 可选
    polish_history/<库名>.json        # 可选
    reviews/<库名>.json               # 可选

可移植性：``config.json`` 里 ``personal_papers[].original_path`` 记录的是
**添加时所在机器**的 PDF 绝对路径（运行时从不读取，仅留档），导入时置空，
避免在新机器上留下指向不存在文件的死路径。

安全：zip-slip 防护（拒绝绝对路径与 ``..`` 条目）、库名校验
（复用 ``writing_coach.validate_profile_name``）、逐文件原子落盘；
覆盖同名库前自动备份到 ``writing_kb/.backup/<时间戳>/``，可回退。
"""

from __future__ import annotations

import json
import os
import shutil
import time
import zipfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from ..utils.config import (
    get_drafts_dir, get_polish_history_dir, get_reviews_dir, get_writing_kb_dir,
)
from .writing_coach import validate_profile_name

FORMAT_NAME = "paperwb-writing-kb"
FORMAT_VERSION = 1
MANIFEST_NAME = "manifest.json"

KB_ROOT = "writing_kb"
DRAFTS_ROOT = "drafts"
HISTORY_ROOT = "polish_history"
REVIEWS_ROOT = "reviews"
BACKUP_DIRNAME = ".backup"

# 与知识库同名的派生文件扩展名
DERIVED_EXTS = (".txt", ".json")

CONFLICT_SKIP = "skip"
CONFLICT_OVERWRITE = "overwrite"
CONFLICT_COPY = "copy"
CONFLICT_LABELS = {
    CONFLICT_SKIP: "跳过（保留本地）",
    CONFLICT_OVERWRITE: "覆盖（先自动备份）",
    CONFLICT_COPY: "存为副本（原名_导入）",
}


# ========== 数据模型 ==========

@dataclass
class KbSource:
    """一个待导出的知识库及其派生文件。"""

    name: str
    writing_type: str = ""
    personal_count: int = 0
    journal_count: int = 0
    has_writing_habits: bool = False
    has_journal_style: bool = False
    size: int = 0
    include_drafts: bool = False
    include_history: bool = False
    include_reviews: bool = False

    @property
    def summary(self) -> str:
        parts = [f"范文 {self.personal_count} 篇"]
        if self.journal_count:
            parts.append(f"期刊范文 {self.journal_count} 篇")
        if self.has_writing_habits:
            parts.append("写作习惯✓")
        if self.has_journal_style:
            parts.append("期刊格式✓")
        return " · ".join(parts)


@dataclass
class KbPackage:
    """一个包内知识库（导入预览用）。"""

    name: str
    writing_type: str = ""
    personal_count: int = 0
    journal_count: int = 0
    has_writing_habits: bool = False
    has_journal_style: bool = False
    size: int = 0
    has_drafts: bool = False
    has_history: bool = False
    has_reviews: bool = False
    exists_locally: bool = False
    exported_at: str = ""

    @property
    def summary(self) -> str:
        parts = [f"范文 {self.personal_count} 篇"]
        if self.journal_count:
            parts.append(f"期刊范文 {self.journal_count} 篇")
        if self.has_writing_habits:
            parts.append("写作习惯✓")
        if self.has_journal_style:
            parts.append("期刊格式✓")
        extras = [n for n, ok in (("草稿", self.has_drafts),
                                  ("润色历史", self.has_history),
                                  ("评价", self.has_reviews)) if ok]
        if extras:
            parts.append("/".join(extras))
        if self.exists_locally:
            parts.append("⚠ 本地已存在")
        return " · ".join(parts)


@dataclass
class ImportReport:
    """导入结果汇总。"""

    imported: list[str] = field(default_factory=list)
    overwritten: list[str] = field(default_factory=list)
    renamed: list[tuple[str, str]] = field(default_factory=list)  # (原包名, 本地名)
    skipped: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)   # (库名, 原因)
    backup_dir: str = ""

    @property
    def changed(self) -> bool:
        return bool(self.imported or self.overwritten or self.renamed)


# ========== 工具 ==========

def _dir_size(path: Path) -> int:
    """目录内所有文件的总字节数（遍历失败一律跳过）。"""
    total = 0
    try:
        for entry in path.rglob("*"):
            try:
                if entry.is_file():
                    total += entry.stat().st_size
            except OSError:
                continue
    except OSError:
        pass
    return total


def _read_profile_config(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _write_bytes_atomic(path: Path, data: bytes) -> None:
    """先写临时文件再原子替换，避免中断留下半截文件。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def sanitize_profile_config(data: dict) -> dict:
    """清除知识库配置里的机器相关字段（导入外部包时调用）。

    ``original_path`` 指向导出机器的 PDF 绝对路径，运行时从不读取，
    留在新机器上只会是死路径，故置空。
    """
    for key in ("personal_papers", "journal_papers"):
        papers = data.get(key)
        if not isinstance(papers, list):
            continue
        cleaned: list[dict] = []
        for p in papers:
            if not isinstance(p, dict):
                continue
            cleaned.append({
                "filename": str(p.get("filename", "") or ""),
                "original_path": "",
                "text": str(p.get("text", "") or ""),
            })
        data[key] = cleaned
    return data


def _is_safe_arcname(name: str) -> bool:
    """拒绝绝对路径与含 ``..`` 的压缩包条目（zip-slip 防护）。"""
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    if len(name) > 1 and name[1] == ":":  # Windows 盘符
        return False
    parts = name.replace("\\", "/").split("/")
    return not any(p == ".." for p in parts)


# ========== 导出 ==========

def collect_sources(kb_dir: Path | None = None) -> list[KbSource]:
    """扫描知识库目录，返回可导出的库清单（含体积估算）。"""
    root = kb_dir or get_writing_kb_dir()
    sources: list[KbSource] = []
    if not root.exists():
        return sources
    for entry in sorted(root.iterdir(), key=lambda p: p.name):
        if not entry.is_dir() or entry.name == BACKUP_DIRNAME:
            continue
        cfg_path = entry / "config.json"
        if not cfg_path.exists():
            continue
        data = _read_profile_config(cfg_path)
        sources.append(KbSource(
            name=entry.name,
            writing_type=str(data.get("writing_type", "") or ""),
            personal_count=len(data.get("personal_papers") or []),
            journal_count=len(data.get("journal_papers") or []),
            has_writing_habits=bool(data.get("writing_habits")),
            has_journal_style=bool(data.get("journal_style")),
            size=_dir_size(entry),
        ))
    return sources


def _derived_files(name: str, kind: str, base_dir: Path) -> list[Path]:
    """知识库派生的单文件（drafts/<名>.txt、reviews/<名>.json 等）。"""
    out: list[Path] = []
    for ext in DERIVED_EXTS:
        f = base_dir / f"{name}{ext}"
        if f.exists():
            out.append(f)
    return out


def build_manifest(sources: list[KbSource], written: list[tuple[str, int]]) -> dict:
    """构造包清单（各库概要 + 文件清单，供导入侧预览与校验）。"""
    by_name = {s.name: s for s in sources}
    profiles: dict[str, dict] = {}
    for arcname, size in written:
        parts = arcname.split("/")
        if len(parts) >= 2 and parts[0] == KB_ROOT:
            name = parts[1]
            src = by_name.get(name)
            info = profiles.setdefault(name, {
                "writing_type": src.writing_type if src else "",
                "personal_count": src.personal_count if src else 0,
                "journal_count": src.journal_count if src else 0,
                "has_writing_habits": src.has_writing_habits if src else False,
                "has_journal_style": src.has_journal_style if src else False,
                "has_drafts": False, "has_history": False, "has_reviews": False,
                "files": 0, "size": 0,
            })
            info["files"] += 1
            info["size"] += size
        elif len(parts) >= 2 and parts[0] in (DRAFTS_ROOT, HISTORY_ROOT, REVIEWS_ROOT):
            name = Path(parts[1]).stem
            info = profiles.get(name)
            if info is None:
                continue
            key = {DRAFTS_ROOT: "has_drafts", HISTORY_ROOT: "has_history",
                   REVIEWS_ROOT: "has_reviews"}[parts[0]]
            info[key] = True
    return {
        "format": FORMAT_NAME,
        "format_version": FORMAT_VERSION,
        "exported_at": datetime.now().isoformat(timespec="seconds"),
        "app": "PaperWB",
        "profiles": profiles,
        "files": [{"name": n, "size": s} for n, s in written],
    }


def write_archive(sources: list[KbSource], dest_zip: str | Path,
                  kb_dir: Path | None = None,
                  drafts_dir: Path | None = None,
                  history_dir: Path | None = None,
                  reviews_dir: Path | None = None,
                  progress_cb=None, interrupt_cb=None) -> dict:
    """把选中的知识库写入 zip 包。

    Args:
        sources: 待导出的库（含 include_* 开关）。
        dest_zip: 目标 .zip 路径（覆盖已有文件）。
        kb_dir/drafts_dir/history_dir/reviews_dir: 覆盖数据根（测试注入用）。
        progress_cb: ``(done, total, label)`` 进度回调。
        interrupt_cb: 返回 True 时中止（已写部分保留，由调用方清理）。

    Returns:
        {"profiles": n, "files": n, "bytes": n, "manifest": {...}}
    """
    root = kb_dir or get_writing_kb_dir()
    d_dir = drafts_dir or get_drafts_dir()
    h_dir = history_dir or get_polish_history_dir()
    r_dir = reviews_dir or get_reviews_dir()

    # 先收集全部 (源路径, 包内路径)，便于计算总数与进度
    entries: list[tuple[Path, str]] = []
    for src in sources:
        pdir = root / src.name
        if not pdir.exists():
            continue
        for f in sorted(pdir.rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(root).as_posix()
            entries.append((f, f"{KB_ROOT}/{rel}"))
        if src.include_drafts:
            for f in _derived_files(src.name, DRAFTS_ROOT, d_dir):
                entries.append((f, f"{DRAFTS_ROOT}/{f.name}"))
        if src.include_history:
            for f in _derived_files(src.name, HISTORY_ROOT, h_dir):
                entries.append((f, f"{HISTORY_ROOT}/{f.name}"))
        if src.include_reviews:
            for f in _derived_files(src.name, REVIEWS_ROOT, r_dir):
                entries.append((f, f"{REVIEWS_ROOT}/{f.name}"))

    total = len(entries)
    dest = Path(dest_zip)
    dest.parent.mkdir(parents=True, exist_ok=True)
    written: list[tuple[str, int]] = []
    total_bytes = 0

    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, (src_file, arcname) in enumerate(entries):
            if interrupt_cb is not None and interrupt_cb():
                break
            try:
                data = src_file.read_bytes()
            except OSError:
                continue
            zf.writestr(arcname, data)
            written.append((arcname, len(data)))
            total_bytes += len(data)
            if progress_cb is not None:
                progress_cb(i + 1, total, src_file.name)

        manifest = build_manifest(sources, written)
        zf.writestr(MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

    return {
        "profiles": len({n.split("/")[1] for n, _ in written
                         if n.startswith(KB_ROOT + "/") and n.count("/") >= 2}),
        "files": len(written),
        "bytes": total_bytes,
        "manifest": manifest,
    }


# ========== 导入 ==========

def read_manifest(zip_path: str | Path) -> dict:
    """读取并校验包清单。

    Raises:
        ValueError: 不是 PaperWB 知识库包，或版本不受支持。
    """
    try:
        with zipfile.ZipFile(zip_path) as zf:
            raw = zf.read(MANIFEST_NAME)
    except (zipfile.BadZipFile, KeyError, OSError) as e:
        raise ValueError(f"无法读取知识库包：{e}") from e
    try:
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ValueError(f"包清单已损坏：{e}") from e
    if not isinstance(data, dict) or data.get("format") != FORMAT_NAME:
        raise ValueError("这不是 PaperWB 写作知识库导出包")
    version = data.get("format_version")
    if not isinstance(version, int) or version > FORMAT_VERSION:
        raise ValueError(
            f"包格式版本 {version} 高于当前程序支持的 {FORMAT_VERSION}，"
            "请升级 PaperWB 后再导入")
    return data


def list_packages(zip_path: str | Path, kb_dir: Path | None = None) -> list[KbPackage]:
    """列出包内知识库（含与本地同名冲突标记），供导入预览。"""
    manifest = read_manifest(zip_path)
    root = kb_dir or get_writing_kb_dir()
    profiles = manifest.get("profiles")
    if not isinstance(profiles, dict):
        return []
    out: list[KbPackage] = []
    for name, info in sorted(profiles.items()):
        if not isinstance(info, dict):
            continue
        out.append(KbPackage(
            name=str(name),
            writing_type=str(info.get("writing_type", "") or ""),
            personal_count=int(info.get("personal_count", 0) or 0),
            journal_count=int(info.get("journal_count", 0) or 0),
            has_writing_habits=bool(info.get("has_writing_habits")),
            has_journal_style=bool(info.get("has_journal_style")),
            size=int(info.get("size", 0) or 0),
            has_drafts=bool(info.get("has_drafts")),
            has_history=bool(info.get("has_history")),
            has_reviews=bool(info.get("has_reviews")),
            exists_locally=(root / str(name)).exists(),
            exported_at=str(manifest.get("exported_at", "") or ""),
        ))
    return out


def _backup_existing(name: str, kb_dir: Path,
                     drafts_dir: Path, history_dir: Path,
                     reviews_dir: Path, stamp: str) -> None:
    """覆盖前把同名库及其派生文件备份到 writing_kb/.backup/<时间戳>/。"""
    backup_root = kb_dir / BACKUP_DIRNAME / stamp
    src_dir = kb_dir / name
    if src_dir.exists():
        target = backup_root / KB_ROOT / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src_dir, target, dirs_exist_ok=True)
    for base, label in ((drafts_dir, DRAFTS_ROOT), (history_dir, HISTORY_ROOT),
                        (reviews_dir, REVIEWS_ROOT)):
        for ext in DERIVED_EXTS:
            f = base / f"{name}{ext}"
            if not f.exists():
                continue
            target = backup_root / label / f.name
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                shutil.copy2(f, target)
            except OSError:
                continue


def _unique_name(base: str, kb_dir: Path, taken: set[str]) -> str:
    """为「存为副本」生成不冲突的库名：原名_导入 / 原名_导入2 …"""
    candidate = f"{base}_导入"
    n = 2
    while (kb_dir / candidate).exists() or candidate in taken:
        candidate = f"{base}_导入{n}"
        n += 1
    return candidate


def import_archive(zip_path: str | Path, selected: list[str],
                   conflict_policy: str = CONFLICT_SKIP,
                   kb_dir: Path | None = None,
                   drafts_dir: Path | None = None,
                   history_dir: Path | None = None,
                   reviews_dir: Path | None = None,
                   progress_cb=None, interrupt_cb=None) -> ImportReport:
    """从包中导入选中的知识库。

    Args:
        selected: 要导入的库名列表（包内原名）。
        conflict_policy: 本地同名时的策略（skip / overwrite / copy）。
        progress_cb: ``(done, total, label)`` 进度回调。
        interrupt_cb: 返回 True 时中止（已导入部分保留）。

    Returns:
        ImportReport（含备份目录，可据此回退）。
    """
    root = kb_dir or get_writing_kb_dir()
    d_dir = drafts_dir or get_drafts_dir()
    h_dir = history_dir or get_polish_history_dir()
    r_dir = reviews_dir or get_reviews_dir()
    root.mkdir(parents=True, exist_ok=True)

    report = ImportReport()
    manifest = read_manifest(zip_path)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    wanted = [str(n) for n in selected if n]
    taken: set[str] = set()
    if not wanted:
        return report  # 未选任何库 = 无操作，不视为错误

    with zipfile.ZipFile(zip_path) as zf:
        names = [n for n in zf.namelist() if _is_safe_arcname(n)]
        total = len(wanted)
        for i, pkg_name in enumerate(wanted):
            if interrupt_cb is not None and interrupt_cb():
                break
            if progress_cb is not None:
                progress_cb(i + 1, total, pkg_name)
            try:
                validate_profile_name(pkg_name)
            except ValueError as e:
                report.failed.append((pkg_name, str(e)))
                continue

            target_name = pkg_name
            dest_dir = root / target_name
            if dest_dir.exists():
                if conflict_policy == CONFLICT_SKIP:
                    report.skipped.append(pkg_name)
                    continue
                if conflict_policy == CONFLICT_COPY:
                    target_name = _unique_name(pkg_name, root, taken)
                    dest_dir = root / target_name
                    report.renamed.append((pkg_name, target_name))
                else:
                    report.overwritten.append(pkg_name)
                if conflict_policy == CONFLICT_OVERWRITE:
                    try:
                        _backup_existing(pkg_name, root, d_dir, h_dir, r_dir, stamp)
                        report.backup_dir = str(root / BACKUP_DIRNAME / stamp)
                    except OSError as e:
                        report.failed.append((pkg_name, f"备份失败：{e}"))
                        continue
                    try:
                        if dest_dir.exists():
                            shutil.rmtree(dest_dir)
                    except OSError as e:
                        report.failed.append((pkg_name, f"无法替换旧库：{e}"))
                        continue
            taken.add(target_name)

            prefix = f"{KB_ROOT}/{pkg_name}/"
            ok = True
            try:
                for arcname in names:
                    if arcname == MANIFEST_NAME or not arcname.startswith(prefix):
                        continue
                    rel = arcname[len(prefix):]
                    if not rel:
                        continue
                    data = zf.read(arcname)
                    if rel == "config.json":
                        # 清掉源机器的 PDF 绝对路径
                        try:
                            cfg = json.loads(data.decode("utf-8"))
                            if isinstance(cfg, dict):
                                data = json.dumps(sanitize_profile_config(cfg),
                                                  ensure_ascii=False,
                                                  indent=2).encode("utf-8")
                        except (json.JSONDecodeError, UnicodeDecodeError):
                            pass
                    _write_bytes_atomic(dest_dir / rel, data)
            except (OSError, zipfile.BadZipFile) as e:
                ok = False
                report.failed.append((pkg_name, str(e)))

            if ok:
                # 派生单文件：仅当包内确实含有且目标库名未变时才还原
                if target_name == pkg_name:
                    for src_root, base_dir in ((DRAFTS_ROOT, d_dir),
                                               (HISTORY_ROOT, h_dir),
                                               (REVIEWS_ROOT, r_dir)):
                        for arcname in names:
                            if not arcname.startswith(f"{src_root}/"):
                                continue
                            fname = arcname[len(src_root) + 1:]
                            if "/" in fname or Path(fname).stem != pkg_name:
                                continue
                            try:
                                _write_bytes_atomic(base_dir / fname,
                                                    zf.read(arcname))
                            except (OSError, zipfile.BadZipFile):
                                continue
                if target_name != pkg_name:
                    report.renamed = [r for r in report.renamed
                                      if r[1] != target_name] + [(pkg_name, target_name)]
                if pkg_name not in report.overwritten:
                    report.imported.append(target_name)

    # overwritten 已计入 imported 之外的清单，去重避免重复统计
    report.imported = [n for n in report.imported if n not in report.overwritten]
    return report


# ========== 后台线程 ==========

class KbExportWorker(QThread):
    """后台导出知识库（打包可能耗时，避免阻塞 UI）。"""

    progress = Signal(int, int, str)
    error = Signal(str)
    done = Signal(dict)

    def __init__(self, sources: list[KbSource], dest_zip: str, parent=None):
        super().__init__(parent)
        self._sources = sources
        self._dest = dest_zip

    def run(self):
        try:
            result = write_archive(
                self._sources, self._dest,
                progress_cb=lambda d, t, n: self.progress.emit(d, t, n),
                interrupt_cb=self.isInterruptionRequested)
            if self.isInterruptionRequested():
                try:
                    Path(self._dest).unlink(missing_ok=True)
                except OSError:
                    pass
                return
            result["path"] = self._dest
            self.done.emit(result)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))


class KbImportWorker(QThread):
    """后台导入知识库。"""

    progress = Signal(int, int, str)
    error = Signal(str)
    done = Signal(object)

    def __init__(self, zip_path: str, selected: list[str],
                 conflict_policy: str, parent=None):
        super().__init__(parent)
        self._zip = zip_path
        self._selected = selected
        self._policy = conflict_policy

    def run(self):
        try:
            report = import_archive(
                self._zip, self._selected, self._policy,
                progress_cb=lambda d, t, n: self.progress.emit(d, t, n),
                interrupt_cb=self.isInterruptionRequested)
            if self.isInterruptionRequested():
                return
            self.done.emit(report)
        except Exception as e:  # noqa: BLE001
            self.error.emit(str(e))


def human_size(num: int) -> str:
    """字节数 → 人类可读（导出/导入对话框共用）。"""
    size = float(num or 0)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} GB"
