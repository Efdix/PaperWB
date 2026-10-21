# -*- coding: utf-8 -*-
"""预热 Docling 模型缓存到项目内（打包机一次性备料，不跑应用）。

下载 docling-layout-heron + docling-models 两仓到 models/hf_cache/hub，
之后 installer/stage_models.py 从同一 HF_HUB_CACHE 复制进安装包 staging。
用法: D:/science/Miniforge/envs/PaperWB/python.exe _gen_prewarm_models.py
"""

from __future__ import annotations

import os
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent
os.environ["HF_HUB_CACHE"] = str(REPO_ROOT / "models" / "hf_cache" / "hub")
os.environ["HF_ENDPOINT"] = "https://hf-mirror.com"
os.environ["HF_HUB_DISABLE_XET"] = "1"  # 与 docling_parser 镜像模式一致，避免 xet 分散缓存

from huggingface_hub import HfApi, snapshot_download  # noqa: E402
import huggingface_hub.file_download as _fd  # noqa: E402

# Windows 无开发者模式/管理员特权时符号链接创建会抛 WinError 1314（库的探测
# 偶发误判支持且不回退）。强制复制模式：快照里直接是实文件，体积 2× 但
# 与 stage_models 的"解引用复制"完全兼容，目标机器也无需链接权限。
_fd.are_symlinks_supported = lambda cache_dir=None: False
os.environ["HF_HUB_DISABLE_SYMLINKS_WARNING"] = "1"

# docling-models 整仓可能远大于 TableFormer 所需；超阈值只取表格模型子树
FULL_REPO_MAX_MB = 700
TABLEFORMER_PATTERNS = ["model_artifacts/tableformer/**"]

for repo in ("docling-project/docling-layout-heron", "docling-project/docling-models"):
    patterns = None
    try:
        info = HfApi().model_info(repo, files_metadata=True)
        total_mb = sum((f.size or 0) for f in (info.siblings or [])) / 1e6
        print(f"== {repo}: {len(info.siblings or [])} files, {total_mb:.0f} MB", flush=True)
        if repo.endswith("docling-models") and total_mb > FULL_REPO_MAX_MB:
            patterns = TABLEFORMER_PATTERNS
            print("   超过阈值，仅下载 model_artifacts/tableformer/**", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"== {repo}: 体积探测失败（{e}），直接整仓下载", flush=True)
    t0 = time.time()
    path = snapshot_download(repo, allow_patterns=patterns)
    print(f"   -> {path}（{time.time() - t0:.0f}s）", flush=True)

print("ALL DONE", flush=True)
