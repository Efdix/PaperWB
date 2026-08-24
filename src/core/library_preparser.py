"""后台全库预解析调度器 —— 空闲时串行驱动两阶段管线（纯本地，零 LLM）。

与阅读共用同一套 PDFProcessor / page_cache / states 缓存（按 PDF 路径
哈希键 + mtime 失效）：任一方先解析过，另一方直接复用。预解析以
「初步整合」模式落盘（``start_stage2(preliminary=True)``：规则接缝
合并写入 ``merged_seams_prelim``，states 标记 ``seams_final=False``），
用户点开该文献时再由阅读侧做一次 LLM 接缝精修并定稿。

节流策略：同一时刻仅解析一篇（Docling 全局转换锁本就串行），篇间小憩；
用户发起的解析/整合永远优先（``should_yield`` 回调让路）。失败篇记录
到 ``lib_index/preparse.json``（文件未变不重试），跨启动按缓存完整性
自动续跑。
"""

from __future__ import annotations

import json
import os
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal

from .pdf_processor import FAST_DOCUMENT_VERSION, PDFProcessor
from ..utils.config import get_lib_index_dir, load_doc_state

PREPARSE_STATE_VERSION = 1
BETWEEN_DOCS_MS = 1500   # 篇间小憩：降低后台建库对交互的影响
YIELD_RECHECK_MS = 5000  # 用户侧有在途解析时稍后再试
WATCHDOG_MS = 15000      # 在途文献的信号丢失看门狗周期


def doc_state_is_parsed(pdf_path: str) -> bool:
    """states 缓存是否已是当前版本的有效整合结果。

    阅读时正常解析过与后台建库产出的初步整合都算「已解析」：两边共用
    同一份缓存，谁先解析过另一边就不必再来一遍。
    """
    try:
        state = load_doc_state(pdf_path)
    except Exception:  # noqa: BLE001
        return False
    if state.get("doc_format") != "fast":
        return False
    if not state.get("structured_document"):
        return False
    try:
        if int(state.get("fast_version", 0) or 0) != FAST_DOCUMENT_VERSION:
            return False
        mtime = os.path.getmtime(pdf_path)
    except (OSError, TypeError, ValueError):
        return False
    return abs(float(state.get("pdf_mtime", 0.0) or 0.0) - mtime) <= 1.0


class LibraryPreparser(QObject):
    """串行预解析 Zotero 库全部 PDF（主线程状态机 + 处理器自带 QThread）。"""

    progress = Signal(int, int, str)   # done, total, 当前文件名（空串 = 汇总）
    item_done = Signal(str)            # pdf_path（本篇初步整合已完整落盘）
    state_changed = Signal(str)        # running / paused / idle

    def __init__(self, should_yield: Callable[[], bool] | None = None,
                 parent=None) -> None:
        super().__init__(parent)
        self._should_yield = should_yield or (lambda: False)
        self._pending: list[str] = []
        self._done = 0
        self._total = 0
        self._state = "idle"
        self._processor: PDFProcessor | None = None
        self._current_path = ""
        self._doc_advanced = False  # 当前篇已收到终态信号（防重复推进）
        self._watchdog = QTimer(self)
        self._watchdog.setInterval(WATCHDOG_MS)
        self._watchdog.timeout.connect(self._on_watchdog)
        self._load_errors()

    # ---- 失败记忆（lib_index/preparse.json，文件未变不重试） ----

    @property
    def _errors_path(self):
        return get_lib_index_dir() / "preparse.json"

    def _load_errors(self) -> None:
        self._errors: dict[str, float] = {}
        try:
            data = json.loads(self._errors_path.read_text(encoding="utf-8"))
            errs = data.get("errors") if isinstance(data, dict) else None
            if isinstance(errs, dict):
                self._errors = {str(k): float(v) for k, v in errs.items()}
        except (OSError, ValueError, TypeError):
            pass

    def _save_errors(self) -> None:
        try:
            self._errors_path.write_text(
                json.dumps({"version": PREPARSE_STATE_VERSION,
                            "errors": self._errors}, ensure_ascii=False),
                encoding="utf-8")
        except OSError:
            pass

    @staticmethod
    def _mtime_of(path: str) -> float | None:
        try:
            return os.path.getmtime(path)
        except OSError:
            return None

    # ---- 队列与状态 ----

    def set_queue(self, paths: list[str]) -> None:
        """（重）设预解析队列：跳过已解析与失败未变的篇目，保持稳定顺序。

        运行中调用会替换剩余队列（当前篇继续跑完）；计数从头起算。
        """
        seen: set[str] = set()
        queue: list[str] = []
        for p in paths:
            p = (p or "").strip()
            if not p or p in seen:
                continue
            seen.add(p)
            if doc_state_is_parsed(p):
                continue  # 阅读或上轮建库已解析过
            mtime = self._mtime_of(p)
            if mtime is None or self._errors.get(p) == mtime:
                continue  # 文件缺失或失败后未变
            queue.append(p)
        self._pending = queue
        self._done = 0
        self._total = len(queue)
        if self._state == "running":
            self.progress.emit(self._done, self._total, "")

    @property
    def state(self) -> str:
        return self._state

    @property
    def current_processor(self) -> PDFProcessor | None:
        """当前篇处理器 —— 阅读侧点开同一文献时接管复用（含在途解析）。"""
        return self._processor if self._current_path else None

    def active_processor(self, path: str) -> PDFProcessor | None:
        if path and path == self._current_path:
            return self._processor
        return None

    def start(self) -> None:
        if self._state == "running":
            return
        self._set_state("running")
        self._schedule_next(0)

    def pause(self) -> None:
        """暂停调度：当前篇让其在后台跑完（结果仍有效），不再取下一篇。"""
        if self._state != "running":
            return
        self.progress.emit(self._done, self._total, "")
        self._set_state("paused")

    def resume(self) -> None:
        if self._state != "paused":
            return
        self.start()

    def stop(self) -> None:
        """完全停止（关窗/关闭开关）：取消当前篇并回到 idle。"""
        self._set_state("idle")
        self._watchdog.stop()
        self._release_processor(cancel=True)

    def cancel_if_active(self, path: str) -> None:
        """外部（重跑/删除缓存）要求取消在途的预解析篇目。

        不动状态机：看门狗在其退出后收尾跳篇（不记失败）。
        """
        if path and path == self._current_path and self._processor is not None:
            self._processor.cancel()
            QTimer.singleShot(200, self._on_watchdog)

    # ---- 内部状态机 ----

    def _set_state(self, s: str) -> None:
        if self._state != s:
            self._state = s
            self.state_changed.emit(s)

    def _schedule_next(self, delay_ms: int) -> None:
        QTimer.singleShot(max(delay_ms, 0), self._next)

    def _next(self) -> None:
        if self._state != "running":
            return
        if self._should_yield():
            # 用户侧解析在途：后台建库让路，稍后再试
            self._schedule_next(YIELD_RECHECK_MS)
            return
        path = self._pending.pop(0) if self._pending else ""
        if not path:
            # 先发汇总进度再转 idle：state_changed(idle) 会隐藏状态行
            self.progress.emit(self._done, self._total, "")
            self._set_state("idle")
            return
        self._current_path = path
        self._doc_advanced = False
        self.progress.emit(self._done, self._total, os.path.basename(path))
        try:
            # 后台建库零 LLM：不注入客户端，Stage 2 走规则兜底
            proc = PDFProcessor(path, None)
        except Exception:  # noqa: BLE001  PDF 打不开/manifest 创建失败
            self._record_error(path)
            self._advance()
            return
        self._processor = proc
        proc.stage1_error.connect(self._on_stage1_error)
        proc.stage2_error.connect(self._on_stage2_error)
        proc.stage1_complete.connect(self._on_stage1_complete)
        proc.stage2_finished.connect(self._on_stage2_finished)
        self._watchdog.start()
        if proc.is_stage1_complete:
            self._on_stage1_complete(path)
        else:
            proc.start_stage1()

    def _on_stage1_complete(self, path: str) -> None:
        if path != self._current_path or self._processor is None:
            return
        if self._doc_advanced:
            return  # stage2 已接手（点开文献被阅读侧接管等场景）
        try:
            self._processor.start_stage2(preliminary=True)
        except Exception:  # noqa: BLE001
            self._record_error(path)
            self._advance()

    def _on_stage2_finished(self, path: str) -> None:
        """初步整合文档已落盘（规则接缝合并在同一调用栈内随后补写）。"""
        if path != self._current_path or self._doc_advanced:
            return
        self._doc_advanced = True
        self._errors.pop(path, None)
        self._save_errors()
        self._advance()
        # 等规则接缝合并写盘后再通知索引升级：延迟到当前调用栈之外
        QTimer.singleShot(0, lambda p=path: self._emit_item_done(p))

    def _emit_item_done(self, path: str) -> None:
        if doc_state_is_parsed(path):  # 防御：落盘异常时不升级索引
            self.item_done.emit(path)

    def _on_stage1_error(self, path: str, page_num: int, error_msg: str) -> None:
        if path != self._current_path or self._doc_advanced:
            return
        self._record_error(path)
        self._advance()

    def _on_stage2_error(self, path: str, error_msg: str) -> None:
        if path != self._current_path or self._doc_advanced:
            return
        self._record_error(path)
        self._advance()

    def _record_error(self, path: str) -> None:
        mtime = self._mtime_of(path)
        if mtime is not None:
            self._errors[path] = mtime
            self._save_errors()

    def _advance(self) -> None:
        self._watchdog.stop()
        self._doc_advanced = True
        self._done += 1
        self._release_processor(cancel=False)
        if self._state == "running":
            self._schedule_next(BETWEEN_DOCS_MS)

    def _release_processor(self, cancel: bool) -> None:
        proc = self._processor
        self._processor = None
        self._current_path = ""
        if proc is not None and cancel:
            proc.cancel()

    def _on_watchdog(self) -> None:
        """看门狗：处理器已闲但终态信号丢失（外部取消/异常路径）→ 跳篇。"""
        proc = self._processor
        if proc is None or not self._current_path:
            self._watchdog.stop()
            return
        if self._doc_advanced:
            return
        if proc.is_busy:
            return  # 正常在途（Docling 解析大 PDF 可能要数分钟）
        self._advance()  # 不记失败：可能是被外部取消，下次还会重试
