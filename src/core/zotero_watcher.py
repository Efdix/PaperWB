"""Zotero 周期同步引擎 —— 启动时加载一次，此后每 30 分钟自动重载并发出差异信号。

只读保障：
- 不做文件事件监听（避免 Windows 高 I/O 自激循环），仅由 QTimer 周期触发
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
    """周期同步 Zotero 库（每 30 分钟自动重载一次，不做文件事件监听）并驱动重载。

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

    DEBOUNCE_MS = 1000     # 文件变化防抖（保留给旧的事件监听路径，默认不激活）
    SYNC_INTERVAL_MS = 30 * 60 * 1000  # 周期自动同步间隔：30 分钟

    def __init__(self, library, parent=None):
        super().__init__(parent)
        self._library = library
        self._fs = QFileSystemWatcher(self)
        self._fs.directoryChanged.connect(self._on_fs_change)
        self._fs.fileChanged.connect(self._on_fs_change)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.timeout.connect(self._trigger_reload)

        self._sync_timer = QTimer(self)
        self._sync_timer.timeout.connect(
            lambda: self.request_reload(show_status=False)
        )

        self._worker: ZoteroReloadWorker | None = None
        self._last_snapshot: dict | None = None
        self._running = False
        self._dirty = False

    # ---- 公共 API ----

    def start(self) -> None:
        """开始周期同步（不做文件事件监听，仅每 SYNC_INTERVAL_MS 重载一次）。"""
        self._running = True
        self._last_snapshot = self._library.snapshot()
        self._sync_timer.start(self.SYNC_INTERVAL_MS)
        self.status.emit(
            f"已连接 Zotero（{self._library.item_count} 篇文献，"
            f"每 {self.SYNC_INTERVAL_MS // 60_000} 分钟自动同步）"
        )

    def stop(self) -> None:
        """停止周期同步并清理。"""
        self._running = False
        self._debounce.stop()
        self._sync_timer.stop()
        self._remove_all_watched()
        if self._worker and self._worker.isRunning():
            self._worker.quit()
            self._worker.wait(1000)

    def request_reload(self, show_status: bool = True) -> None:
        """立即触发一次后台重载（周期定时与面板刷新按钮共用）。

        Args:
            show_status: True 时先显示「正在同步」状态（手动刷新），
                         False 时静默执行（周期定时，仅发现实质变化后提示）。
        """
        if self._last_snapshot is None:
            self._last_snapshot = self._library.snapshot()
        if self._worker is not None and self._worker.isRunning():
            return
        self._start_reload(show_status)

    # ---- 内部 ----

    def _remove_all_watched(self) -> None:
        """移除所有已注册的监听路径（列表为空时跳过，避免 Qt 空列表警告）。"""
        try:
            watched = self._fs.directories() + self._fs.files()
            if watched:
                self._fs.removePaths(watched)
        except Exception:
            pass

    def _watch_paths(self) -> None:
        """注册需要监听的文件/目录（数据库 + storage + 附件父目录）。"""
        self._remove_all_watched()
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

    def _start_reload(self, show_status: bool = True) -> None:
        if show_status:
            self.status.emit("正在重新同步 Zotero 文献库...")
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
        else:
            # 无实质变化也发一次状态，收掉「正在同步」提示
            self.status.emit(f"已连接 · {self._library.item_count} 篇文献 · 无变化")
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
