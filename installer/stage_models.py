# -*- coding: utf-8 -*-
"""把本机 HuggingFace 缓存中的 Docling 模型复制到安装包 staging 目录。

从默认 HF 缓存（~/.cache/huggingface/hub，或 HF_HUB_CACHE 指定的目录）中
提取 Docling 解析所需的两个模型，复制为标准 HF 缓存布局：

    installer/models_cache/hub/
    ├── models--docling-project--docling-layout-heron/    版式识别模型（~164 MB）
    │   ├── refs/、snapshots/<hash>/（实文件平铺）
    └── models--docling-project--docling-models/          TableFormer 表格模型（~342 MB）
        └── snapshots/.../model_artifacts/tableformer/{accurate,fast}

复制时解引用符号链接（snapshots 内换成实文件）并剔除 blobs 与 .lock，
保证目标机器上 docling 的 snapshot_download 直接离线命中。
应用侧由 src/core/docling_parser.py 检测 <安装目录>/models/hub 并重定向
HF_HUB_CACHE，无需联网。

用法（PaperWB conda 环境）:
    python installer/stage_models.py                 # 输出到 installer/models_cache/hub
    python installer/stage_models.py --dest models   # 输出到仓库根 models/hub（联调验证用）

本机缓存缺模型时先运行 _gen_prewarm_models.py（直接下载到 models/hf_cache/hub，
无需启动应用解析），再重跑本脚本。
"""

from __future__ import annotations

import argparse
import importlib.util
import os
import re
import shutil
import sys
from pathlib import Path

# 模型仓库名 → 快照内必须存在的关键文件/目录（相对快照根）
REQUIRED: dict[str, list[str]] = {
    "models--docling-project--docling-layout-heron": [
        os.path.join("model.safetensors"),
    ],
    "models--docling-project--docling-models": [
        os.path.join("model_artifacts", "tableformer", "accurate"),
        os.path.join("model_artifacts", "tableformer", "fast"),
    ],
}

# docling 运行时按 revision 请求模型仓（离线模式下 huggingface_hub 靠 refs/<revision>
# 解析到快照，缺 ref 即抛 LocalEntryNotFoundError）。prewarm 下载通常只写 refs/main，
# 这里兜底正则提取已安装 docling 源码里的钉扎 revision；提取失败回退此表。
# docling 升级后请核对 docling/models/stages/table_structure/table_structure_model.py。
_DOC_REVISION_DEFAULTS: dict[str, list[str]] = {
    "models--docling-project--docling-models": ["v2.3.0"],
}


def _pinned_revisions(repo_dirname: str, docling_pkg: Path | None) -> list[str]:
    """提取 docling 源码中对某模型仓钉扎的 revision 列表（如 v2.3.0）。"""
    repo_id = repo_dirname.removeprefix("models--").replace("--", "/")
    revisions: set[str] = set()
    if docling_pkg is not None and docling_pkg.is_dir():
        try:
            for py in docling_pkg.rglob("*.py"):
                try:
                    lines = py.read_text(encoding="utf-8", errors="ignore").splitlines()
                except OSError:
                    continue
                for i, line in enumerate(lines):
                    if f'"{repo_id}"' in line or f"'{repo_id}'" in line:
                        for near in lines[i : i + 6]:
                            for m in re.finditer(
                                r'revision\s*=\s*["\']([^"\']+)["\']', near
                            ):
                                revisions.add(m.group(1))
        except OSError:
            pass
    if not revisions:
        revisions = set(_DOC_REVISION_DEFAULTS.get(repo_dirname, []))
    return sorted(revisions)


def _hf_hub_root() -> Path:
    """本机 HuggingFace hub 缓存根目录。

    优先级：HF_HUB_CACHE 环境变量 > 项目内 models/hf_cache/hub（prewarm 脚本
    _gen_prewarm_models.py 的下载落点）> HF_HOME/hub > 用户默认缓存。
    """
    cache = os.environ.get("HF_HUB_CACHE", "")
    if cache:
        return Path(cache)
    project_hub = Path(__file__).resolve().parent.parent / "models" / "hf_cache" / "hub"
    if project_hub.is_dir():
        return project_hub
    home = os.environ.get("HF_HOME", "")
    if home:
        return Path(home) / "hub"
    return Path.home() / ".cache" / "huggingface" / "hub"


def _dir_size_mb(path: Path) -> float:
    total = 0
    for root, _dirs, files in os.walk(path):
        for f in files:
            try:
                total += os.path.getsize(os.path.join(root, f))
            except OSError:
                pass
    return total / 1024 / 1024


def _validate_repo(repo_dir: Path, required: list[str]) -> str:
    """校验某个模型仓库缓存是否含带关键文件的快照，返回该快照路径（失败抛异常）。"""
    snaps = repo_dir / "snapshots"
    if not snaps.is_dir():
        raise FileNotFoundError(f"缺少 snapshots 目录: {repo_dir}")
    for snap in sorted(snaps.iterdir()):
        if not snap.is_dir():
            continue
        if all((snap / rel).exists() for rel in required):
            return str(snap)
    raise FileNotFoundError(
        f"{repo_dir.name} 的所有快照均缺关键文件 {required}。\n"
        f"请先在本机启动 PaperWB 并解析任意 PDF 一次以预热模型缓存，再重跑本脚本。"
    )


def stage(dest_root: Path) -> int:
    hub = _hf_hub_root()
    if not hub.is_dir():
        print(f"[FAIL] HuggingFace 缓存目录不存在: {hub}\n"
              f"请先启动 PaperWB 并解析任意 PDF 一次（模型自动下载），再重跑本脚本。",
              file=sys.stderr)
        return 1

    total_mb = 0.0
    for repo, required in REQUIRED.items():
        src = hub / repo
        print(f"== {repo}")
        if not src.is_dir():
            print(f"[FAIL] 缓存中不存在（{src}）。\n"
                  f"请先启动 PaperWB 并解析任意 PDF 一次以预热模型缓存，再重跑本脚本。",
                  file=sys.stderr)
            return 1
        try:
            snap = _validate_repo(src, required)
        except FileNotFoundError as e:
            print(f"[FAIL] {e}", file=sys.stderr)
            return 1
        print(f"   源快照: {snap}")

        dst = dest_root / repo
        if dst.exists():
            shutil.rmtree(dst)
        # symlinks=False（默认）：快照里的符号链接解引用为实文件，目标机器无需链接权限
        shutil.copytree(src, dst)
        # blobs 只服务符号链接布局；快照已是实文件后可剔除，.lock 为下载锁无运行时价值
        blobs = dst / "blobs"
        if blobs.is_dir():
            shutil.rmtree(blobs)
        for lock in dst.rglob("*.lock"):
            lock.unlink(missing_ok=True)

        # 离线解析靠 refs/<revision> 定位快照。运行时只会请求 docling 源码钉扎的
        # revision（TableFormer=v2.3.0、heron=main）；prewarm 缓存可能额外残留
        # 旧 ref（如 docling-models 的 main，commit 与 tag 不同）——一并剔除，否则
        # 对应快照会重复塞进安装包。ref 缺失时才补写（tag 与 main 同 commit 的常见
        # 情形；不同 commit 时 prewarm 本就应有该 ref，缺了会由终检兜底报错）。
        refs_dir = dst / "refs"
        main_ref = refs_dir / "main"
        spec = importlib.util.find_spec("docling")
        docling_pkg = Path(spec.origin).parent if spec and spec.origin else None
        pins = _pinned_revisions(repo, docling_pkg)
        if main_ref.is_file():
            commit = main_ref.read_text(encoding="utf-8").strip()
            for rev in pins:
                ref = refs_dir / rev
                if not ref.is_file():
                    ref.write_text(commit + "\n", encoding="utf-8")
                    print(f"   补写 ref: {rev} → {commit[:12]}")
        if pins:
            for ref in refs_dir.iterdir():
                if ref.is_file() and ref.name not in pins:
                    ref.unlink()
                    print(f"   剔除运行时不会请求的 ref: {ref.name}")

        # 裁剪不可达快照：源缓存可能残留多个历史 commit（各 342 MB），全量拷贝
        # 会成倍撑大安装包；只保留剩余 ref 指向的快照。
        referenced = {
            p.read_text(encoding="utf-8").strip()
            for p in refs_dir.iterdir() if p.is_file()
        }
        snaps_dir = dst / "snapshots"
        if snaps_dir.is_dir():
            for snap in snaps_dir.iterdir():
                if snap.is_dir() and snap.name not in referenced:
                    shutil.rmtree(snap)
                    stale_tree = dst / "trees" / f"{snap.name}.json"
                    if stale_tree.is_file():
                        stale_tree.unlink()
                    print(f"   裁剪不可达快照: {snap.name[:12]}")

        # 终检：每个 ref 指向的快照必须存在且含关键文件（安装版离线加载的硬前提）
        for ref in sorted(refs_dir.iterdir()):
            if not ref.is_file():
                continue
            target = snaps_dir / ref.read_text(encoding="utf-8").strip()
            if not target.is_dir() or not all(
                (target / rel).exists() for rel in REQUIRED[repo]
            ):
                print(f"[FAIL] ref {ref.name} → 快照缺失或缺关键文件 {REQUIRED[repo]}",
                      file=sys.stderr)
                return 1

        mb = _dir_size_mb(dst)
        total_mb += mb
        print(f"   已复制 → {dst}（{mb:.0f} MB）")

    print(f"DONE: 共 {total_mb:.0f} MB → {dest_root}")
    return 0


def main() -> int:
    repo = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser(description="Docling 模型 staging（安装包预置）")
    parser.add_argument(
        "--dest", default=str(repo / "installer" / "models_cache" / "hub"),
        help="目标 hub 目录（默认 installer/models_cache/hub；联调可用 --dest models）",
    )
    args = parser.parse_args()
    dest_root = Path(args.dest).resolve()
    dest_root.mkdir(parents=True, exist_ok=True)
    return stage(dest_root)


if __name__ == "__main__":
    sys.exit(main())
