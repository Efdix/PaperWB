"""Zotero 实时同步引擎 —— 监听 Zotero 数据库与附件目录，变更后后台重载并发出差异信号。

只读保障：
- 仅监听文件属性（QFileSystemWatcher），不写入 Zotero 目录
- 实际数据读取由 ZoteroLibrary 通过临时副本完成（见 zotero_parser.py）
- 本模块不产生任何对 Zotero 数据目录的写操作
"""

from __future__ import annotations

import os
import time

from PySide6.QtCore import QObject, QThread, QTimer, QFileSystemWatcher, Signal


class ZoteroReloadWorker(QThread):
    """后台重载 Zotero 库（避免大库阻塞 UI）。"""

    finished_signal = Signal(bool)
    error_signal = Signal(str)

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library

    def run(self) -> None:
        try:
            self._library.reload()
            self.finished_signal.emit(True)
        except Exception as e:  # noqa: BLE001
            self.error_signal.emit(str(e))


class ZoteroWatcher(QObject):
    """监听 Zotero 变更并驱动重载。

    信号:
        changed(dict): 差异摘要 {"added_items","removed_items","modified_items",
                                 "added_collections","removed_collections","modified_collections",
                                 "total_items","total_collections"}
        error(str): 重载失败信息
        status(str): 状态提示（可选）
    """

    changed = Signal(dict)
    error = Signal(str)
    status = Signal(str)

    DEBOUNCE_MS = 1000     # 文件变化防抖
    RESCAN_MS = 60_000     # 安全网：定期全量重扫（覆盖手动放置附件等漏检场景）

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._fs = QFileSystemWatcher(self)
        self._fs.directoryChanged.connect(self._on_fs_change)
        self._fs.fileChanged.connect(self._on_fs_change)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._trigger_reload)

        self._rescan = QTimer(self)
        self._rescan.timeout.connect(self._on_fs_change)

        self._worker: ZoteroReloadWorker | None = None
        self._last_snapshot: dict | None = None
        self._running = False
        self._dirty = False

    # ---- 公共 API ----

    def start(self) -> None:
        """开始监听（库应已 load() 过）。"""
        self._running = True
        self._watch_paths()
        self._last_snapshot = self._library.snapshot()
        self._rescan.start(self.RESCAN_MS)
        self.status.emit(f"已连接 Zotero（{self._library.item_count} 篇文献，实时同步中）")

    def stop(self) -> None:
        """停止监听并清理。"""
        self._running = False
        self._debounce.stop()
        self._rescan.stop()
        try:
            self._fs.removePaths(self._fs.directories() + self._fs.files())
        except Exception:
            pass
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(1000)

    def request_reload(self) -> None:
        """手动触发一次后台重载（配合面板的刷新按钮）。"""
        self._trigger_reload()

    # ---- 内部 ----

    def _watch_paths(self) -> None:
        """注册需要监听的文件/目录（数据库 + storage + 附件父目录）。"""
        try:
            self._fs.removePaths(self._fs.directories() + self._fs.files())
        except Exception:
            pass
        paths: set[str] = set()
        db = self._library.sqlite_path
        if db:
            for p in (db, db + "-wal", db + "-shm"):
                if os.path.isfile(p):
                    paths.add(p)
        storage = self._library.storage_dir
        if storage and os.path.isdir(storage):
            paths.add(storage)
            # 每个附件 PDF 的父目录（受条目数限制，几千条内可接受）
            for item in self._library.get_all_items():
                if item.pdf_path:
                    parent = os.path.dirname(item.pdf_path)
                    if parent and os.path.isdir(parent):
                        paths.add(parent)
        for p in paths:
            try:
                self._fs.addPath(p)
            except Exception:
                pass

    def _on_fs_change(self, *_args) -> None:
        if not self._running:
            return
        self._debounce.start(self.DEBOUNCE_MS)

    def _trigger_reload(self) -> None:
        if not self._running:
            return
        if self._worker is not None and self._worker.isRunning():
            self._dirty = True
            return
        self._start_reload()

    def _start_reload(self) -> None:
        self.status.emit("Zotero 发生变化，正在同步...")
        self._worker = ZoteroReloadWorker(self._library, self)
        self._worker.finished_signal.connect(self._on_reload_done)
        self._worker.error_signal.connect(self._on_reload_error)
        self._worker.start()

    def _on_reload_done(self, _ok: bool) -> None:
        self._worker = None
        new_snapshot = self._library.snapshot()
        diff = self._diff(self._last_snapshot, new_snapshot)
        self._last_snapshot = new_snapshot
        if any(v for k, v in diff.items() if k.startswith(("added", "removed", "modified"))):
            self.changed.emit(diff)
        # 重新监听（可能新增了附件目录）
        self._watch_paths()
        if self._dirty:
            self._dirty = False
            self._start_reload()

    def _on_reload_error(self, err: str) -> None:
        self._worker = None
        self.error.emit(f"Zotero 同步失败：{err}")

    @staticmethod
    def _diff(prev: dict | None, new: dict) -> dict:
        """对比两次快照，返回差异摘要。"""
        prev = prev or {}
        result: dict = {
            "added_items": 0, "removed_items": 0, "modified_items": 0,
            "added_collections": 0, "removed_collections": 0, "modified_collections": 0,
            "total_items": len(new.get("items", {})),
            "total_collections": len(new.get("collections", {})),
        }
        old_items = prev.get("items", {})
        new_items = new.get("items", {})
        old_colls = prev.get("collections", {})
        new_colls = new.get("collections", {})

        old_item_keys = set(old_items)
        new_item_keys = set(new_items)
        result["added_items"] = len(new_item_keys - old_item_keys)
        result["removed_items"] = len(old_item_keys - new_item_keys)
        result["modified_items"] = sum(
            1 for k in (old_item_keys & new_item_keys) if old_items[k] != new_items[k]
        )

        old_coll_keys = set(old_colls)
        new_coll_keys = set(new_colls)
        result["added_collections"] = len(new_coll_keys - old_coll_keys)
        result["removed_collections"] = len(old_coll_keys - new_coll_keys)
        result["modified_collections"] = sum(
            1 for k in (old_coll_keys & new_coll_keys) if old_colls[k] != new_colls[k]
        )
        return result
