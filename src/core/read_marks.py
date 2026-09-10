"""读完标记：按 PDF 路径记录「已读」状态，纯本地零 LLM。

- 存储：{data_root}/.paperwb/states/read_marks.json（路径规范化为键，Windows 大小写/分隔符不敏感）
- 单例 store() 带 changed 信号：阅读工具栏一键标记，左右文献列表刷新 ✓ 前缀
- Zotero 附件与本地导入 PDF 共用同一套标记（只读铁律不变：只记录，不写 Zotero）
"""

from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Signal

from ..utils.config import get_states_dir

_MARKS_FILE = "read_marks.json"


def _norm_key(pdf_path: str) -> str:
    return os.path.normcase(os.path.normpath(os.path.abspath(pdf_path)))


class ReadMarkStore(QObject):
    """已读标记存储 + 变更信号。"""

    changed = Signal(str, bool)  # (pdf_path 原始路径, 是否已读)

    def __init__(self, marks_dir: str | Path | None = None,
                 parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._marks: dict[str, dict] = {}
        self._loaded = False
        self._dir = Path(marks_dir) if marks_dir else None

    # ---------- 持久化 ----------

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        f = self._marks_file()
        if not f.exists():
            return
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return
        if isinstance(data, dict):
            self._marks = {k: v for k, v in data.items()
                           if isinstance(v, dict)}

    def _marks_file(self) -> Path:
        return (self._dir or get_states_dir()) / _MARKS_FILE

    def _save(self) -> None:
        try:
            d = self._marks_file()
            d.parent.mkdir(parents=True, exist_ok=True)
            d.write_text(
                json.dumps(self._marks, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass

    # ---------- 查询 / 标记 ----------

    def is_read(self, pdf_path: str) -> bool:
        if not pdf_path:
            return False
        self._ensure_loaded()
        return bool(self._marks.get(_norm_key(pdf_path), {}).get("read"))

    def set_read(self, pdf_path: str, read: bool) -> None:
        if not pdf_path:
            return
        self._ensure_loaded()
        key = _norm_key(pdf_path)
        if read:
            self._marks[key] = {"read": True,
                                "marked_at": datetime.now().isoformat(timespec="seconds")}
        else:
            self._marks.pop(key, None)
        self._save()
        self.changed.emit(pdf_path, read)


_store: ReadMarkStore | None = None


def store() -> ReadMarkStore:
    """全局单例（延迟创建，跟随当前 data_root）。"""
    global _store
    if _store is None:
        _store = ReadMarkStore()
    return _store


def is_read(pdf_path: str) -> bool:
    return store().is_read(pdf_path)


def set_read(pdf_path: str, read: bool) -> None:
    store().set_read(pdf_path, read)


def toggle_read(pdf_path: str) -> bool:
    read = not is_read(pdf_path)
    set_read(pdf_path, read)
    return read
