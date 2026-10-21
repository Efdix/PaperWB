"""原子文件写入与损坏文件保护。

项目里的配置、解析状态、草稿、知识库都是“整份 JSON/文本覆盖写”。直接
``Path.write_text`` 在断电、磁盘满、进程被杀时会留下半截文件；下一次读取
静默回退默认值，用户看到的是 API Key/翻译/草稿凭空消失。

这里提供统一原语：
- 写临时文件（同目录，保证 ``os.replace`` 不跨卷）→ flush + fsync → 原子替换；
- 读取失败时把损坏文件改名保留（``*.corrupt-<时间戳>``），绝不静默覆盖证据；
- 路径规范化/包含判断，供知识库导入等不可信路径使用。
"""

from __future__ import annotations

import json
import os
import tempfile
import time
import uuid
from pathlib import Path


def _tmp_path(path: Path) -> Path:
    """同目录临时文件名：避免跨卷 replace，也避免并发写者互相踩踏。"""
    return path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex[:8]}.tmp")


def atomic_write_bytes(path: str | Path, data: bytes, *, fsync: bool = True) -> None:
    """原子写入字节。失败时清理临时文件，原文件保持不变。

    fsync=False 用于可再生的页缓存/索引：仍有原子替换保护，但省掉每页一次
    磁盘同步，避免解析大 PDF 时被 fsync 拖慢。
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = _tmp_path(target)
    try:
        with open(tmp, "wb") as f:
            f.write(data)
            if fsync:
                f.flush()
                os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def atomic_write_text(path: str | Path, text: str, encoding: str = "utf-8",
                      *, fsync: bool = True) -> None:
    atomic_write_bytes(path, text.encode(encoding), fsync=fsync)


def atomic_write_json(path: str | Path, data: object, *, indent: int | None = 2,
                      fsync: bool = True) -> None:
    atomic_write_text(
        path,
        json.dumps(data, ensure_ascii=False, indent=indent),
        encoding="utf-8",
        fsync=fsync,
    )


def backup_corrupt_file(path: str | Path) -> str:
    """把损坏文件改名保留，返回备份路径；失败返回空串。

    为什么保留而不是删除：用户报障时这是唯一能还原“为什么配置没了”的证据。
    """
    src = Path(path)
    if not src.exists():
        return ""
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{uuid.uuid4().hex[:6]}"
    dst = src.with_name(f"{src.name}.corrupt-{stamp}")
    try:
        os.replace(src, dst)
        return str(dst)
    except OSError:
        return ""


def read_json_safe(path: str | Path, default: object) -> object:
    """读 JSON；文件不存在返回 default，损坏则备份后返回 default。"""
    p = Path(path)
    if not p.exists():
        return default
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        backup_corrupt_file(p)
        return default
    return data


def is_path_inside(child: str | Path, parent: str | Path) -> bool:
    """``child`` 解析后是否严格位于 ``parent`` 内（拒绝 ``..``/盘符/绝对路径）。"""
    try:
        c = Path(child).resolve(strict=False)
        p = Path(parent).resolve(strict=False)
        return c != p and p in c.parents
    except (OSError, RuntimeError, ValueError):
        return False
